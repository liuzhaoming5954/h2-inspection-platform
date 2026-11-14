from queue import Queue
from threading import Event
import time

import pytest

from h2_gateway.hardware import H2HardwareGateway
from h2_gateway.server import create_server


class RecordingLoco:
    def __init__(self):
        self.state = (601, 0)
        self.commands = Queue()
        self.reject_motion = Event()
        self.reject_stop = Event()

    def fsm(self):
        if self.state is None:
            raise TimeoutError("state service offline")
        return self.state

    def velocity(self, vx, vy, omega, duration):
        speed = (vx, vy, omega)
        self.commands.put((speed, duration))
        if (self.reject_motion if any(speed) else self.reject_stop).is_set():
            raise TimeoutError("SDK acknowledgement lost")


@pytest.fixture
def hardware():
    loco = RecordingLoco()
    gateway = H2HardwareGateway(loco)
    try:
        wait_for(gateway, lambda state: state["teleop_ready"])
        yield gateway, loco
    finally:
        gateway.close()


def wait_for(gateway, condition):
    deadline = time.monotonic() + 2
    while True:
        state = gateway.get_state()
        if condition(state):
            return state
        if time.monotonic() >= deadline:
            pytest.fail(f"Timed out waiting for H2 state: {state}")
        time.sleep(0.01)


def send(gateway, token, sequence, action, ttl=350):
    return gateway.teleop_command(token, sequence, action, time.time_ns() // 1_000_000 + ttl)


def test_deadline_and_heartbeat_do_not_extend_motion(hardware):
    gateway, loco = hardware
    token = gateway.teleop_acquire()["token"]
    assert gateway.teleop_acquire()["code"] == "teleop_busy"
    assert send(gateway, token, 1, "forward", ttl=-1)["code"] == "expired"
    # Future bound is clock-skew tolerant; 3 s ahead is still absurd.
    assert send(gateway, token, 2, "forward", ttl=3000)["code"] == "invalid_expiry"
    assert loco.commands.empty()
    assert send(gateway, token, 3, "forward")["accepted"]
    speed, duration = loco.commands.get(timeout=1)
    assert speed == (0.15, 0, 0)
    assert 0 < duration <= 0.25
    assert send(gateway, token, 3, "backward")["code"] == "stale_sequence"
    assert send(gateway, token, 4, "heartbeat")["accepted"]
    assert loco.commands.get(timeout=1)[0] == (0, 0, 0)
    assert send(gateway, token, 5, "forward")["code"] == "invalid_token"
    assert gateway.get_state()["motion"] == "stop_requested"
    assert loco.commands.empty()


@pytest.mark.parametrize("stop_fails", [False, True])
def test_uncertain_movement_revokes_lease_and_requests_zero(hardware, stop_fails):
    gateway, loco = hardware
    token = gateway.teleop_acquire()["token"]
    loco.reject_motion.set()
    if stop_fails:
        loco.reject_stop.set()
    reply = send(gateway, token, 1, "turn_right")
    assert not reply["accepted"]
    assert reply["code"] == ("stop_unconfirmed" if stop_fails else "sdk_rejected")
    assert loco.commands.get(timeout=1)[0] == (0, 0, -0.25)
    assert loco.commands.get(timeout=1)[0] == (0, 0, 0)
    state = gateway.get_state()
    assert not state["teleop_active"]
    if stop_fails:
        assert state["status"] == "fault" and state["motion"] == "unknown"
        assert gateway.teleop_acquire()["code"] == "stop_unconfirmed"
        loco.reject_stop.clear()


def test_wrong_fsm_or_offline_does_not_enable_movement(hardware):
    gateway, loco = hardware
    for fsm in [(4, None), (601, 1), None]:
        loco.state = fsm
        if fsm is None:
            wait_for(gateway, lambda state: state["status"] == "offline")
            code = "hardware_unavailable"
        else:
            wait_for(gateway, lambda state: (state.get("fsm_id"), state.get("fsm_mode")) == fsm)
            code = "robot_not_ready"
        assert gateway.teleop_acquire()["code"] == code
    assert loco.commands.empty()


def test_clock_skew_never_blocks_an_ordered_stop_from_the_owner(hardware):
    gateway, loco = hardware
    token = gateway.teleop_acquire()["token"]
    assert send(gateway, token, 1, "forward")["accepted"]
    assert loco.commands.get(timeout=1)[0] == (0.15, 0, 0)
    # A skewed far-future deadline still cannot move the robot, but an
    # ordered stop under the same skew must go through.
    assert send(gateway, token, 2, "forward", ttl=3000)["code"] == "invalid_expiry"
    assert send(gateway, "wrong-token", 2, "stop", ttl=1200)["code"] == "invalid_token"
    assert send(gateway, token, 1, "stop", ttl=1200)["code"] == "stale_sequence"
    assert send(gateway, token, 2, "stop", ttl=1200)["code"] == "stopped"
    assert loco.commands.get(timeout=1)[0] == (0, 0, 0)


def test_controller_link_loss_latches_unconfirmed_stop(hardware):
    gateway, loco = hardware
    state_before_loss = gateway.get_state()["updated_at"]
    token = gateway.teleop_acquire()["token"]
    assert send(gateway, token, 1, "forward")["accepted"]
    assert loco.commands.get(timeout=1)[0] == (0.15, 0, 0)
    loco.reject_motion.set()
    loco.reject_stop.set()
    loco.state = None
    assert send(gateway, token, 2, "stop")["code"] == "stop_unconfirmed"
    state = wait_for(gateway, lambda state: state["status"] == "fault")
    assert not state["teleop_active"]
    assert loco.commands.get(timeout=1)[0] == (0, 0, 0)

    loco.state = (601, 0)
    loco.reject_motion.clear()
    loco.reject_stop.clear()
    state = wait_for(gateway, lambda state: state.get("fsm_mode") == 0 and state["updated_at"] != state_before_loss)
    assert state["status"] == "fault" and not state["teleop_ready"]
    assert gateway.teleop_acquire()["code"] == "stop_unconfirmed"
    assert not send(gateway, token, 3, "forward")["accepted"]
    assert loco.commands.empty()


def test_shutdown_requests_zero_and_gateway_refuses_nonlocal_bind(hardware):
    gateway, loco = hardware
    with pytest.raises(ValueError, match="loopback"):
        create_server(gateway, host="0.0.0.0", port=0)
    token = gateway.teleop_acquire()["token"]
    assert send(gateway, token, 1, "backward")["accepted"]
    gateway.close()
    assert loco.commands.get(timeout=1)[0] == (-0.15, 0, 0)
    assert loco.commands.get(timeout=1)[0] == (0, 0, 0)
    assert gateway.teleop_acquire()["code"] == "hardware_unavailable"
