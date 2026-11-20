from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event, local
import time

import pytest

from h2_gateway.hardware import H2HardwareGateway, _SDKLock
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
    return gateway.teleop_command(token, sequence, action, time.monotonic_ns() // 1_000_000 + ttl)


def test_deadline_and_heartbeat_do_not_extend_motion(hardware):
    gateway, loco = hardware
    token = gateway.teleop_acquire()["token"]
    assert gateway.teleop_acquire()["code"] == "teleop_busy"
    assert send(gateway, token, 1, "forward", ttl=-1)["code"] == "expired"
    # 超出网关指令时间窗口的截止时间无效。
    assert send(gateway, token, 2, "forward", ttl=3000)["code"] == "invalid_expiry"
    assert loco.commands.empty()
    assert send(gateway, token, 3, "forward")["accepted"]
    speed, duration = loco.commands.get(timeout=1)
    assert speed == (0.5, 0, 0)
    assert 0 < duration <= 0.25
    assert send(gateway, token, 3, "backward")["code"] == "stale_sequence"
    assert send(gateway, token, 4, "heartbeat")["accepted"]
    # 运动指令帧过期时停步，心跳无法延长帧的有效期。
    assert loco.commands.get(timeout=1)[0] == (0, 0, 0)
    # 租约仍然有效，新的运动指令可恢复运动。
    assert send(gateway, token, 5, "forward")["accepted"]
    assert loco.commands.get(timeout=1)[0] == (0.5, 0, 0)
    assert loco.commands.get(timeout=1)[0] == (0, 0, 0)
    assert gateway.get_state()["motion"] == "stop_requested"
    assert loco.commands.empty()


def test_slow_actions_use_reduced_speeds(hardware):
    """慢速档动作走独立的低速度表，供演示序列执行器精确逼近。"""
    from h2_gateway import hardware as hardware_module
    gateway, loco = hardware
    token = gateway.teleop_acquire()["token"]
    assert send(gateway, token, 1, "forward_slow")["accepted"]
    assert loco.commands.get(timeout=1)[0] == (0.4, 0, 0)
    assert send(gateway, token, 2, "turn_right_slow")["accepted"]
    assert loco.commands.get(timeout=1)[0] == (0, 0, -0.4)
    assert send(gateway, token, 3, "stop")["accepted"]
    assert loco.commands.get(timeout=1)[0] == (0, 0, 0)
    # 环境变量按幅值覆盖默认速度；非法值回退默认，防止拼写错误给出零速度。
    assert hardware_module._speed_from_env("H2_V_TEST_MISSING", 0.5) == 0.5
    assert hardware_module.VELOCITIES["forward_slow"][0] == 0.4


def test_speed_env_override_parses_magnitude(monkeypatch):
    from h2_gateway import hardware as hardware_module
    monkeypatch.setenv("H2_V_TEST_SLOW", "0.33")
    assert hardware_module._speed_from_env("H2_V_TEST_SLOW", 0.5) == 0.33
    monkeypatch.setenv("H2_V_TEST_NEGATIVE", "-1")
    assert hardware_module._speed_from_env("H2_V_TEST_NEGATIVE", 0.5) == 0.5
    monkeypatch.setenv("H2_V_TEST_GARBAGE", "fast")
    assert hardware_module._speed_from_env("H2_V_TEST_GARBAGE", 0.5) == 0.5


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
    assert loco.commands.get(timeout=1)[0] == (0, 0, -0.55)
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


def test_an_ordered_stop_from_the_owner_needs_no_valid_deadline(hardware):
    gateway, loco = hardware
    token = gateway.teleop_acquire()["token"]
    assert send(gateway, token, 1, "forward")["accepted"]
    assert loco.commands.get(timeout=1)[0] == (0.5, 0, 0)
    # 停步允许使用已过期的截止时间，仍需验证租约归属和指令顺序。
    assert send(gateway, token, 2, "forward", ttl=3000)["code"] == "invalid_expiry"
    assert send(gateway, "wrong-token", 2, "stop", ttl=1200)["code"] == "invalid_token"
    assert send(gateway, token, 1, "stop", ttl=1200)["code"] == "stale_sequence"
    assert gateway.teleop_command(token, 2, "stop", None)["code"] == "stopped"
    assert loco.commands.get(timeout=1)[0] == (0, 0, 0)


@pytest.mark.parametrize("action", ["forward", "heartbeat"])
def test_cancelled_non_stop_does_not_reach_sdk_or_consume_sequence(hardware, action):
    gateway, loco = hardware
    token = gateway.teleop_acquire()["token"]
    deadline = time.monotonic_ns() // 1_000_000 + 350
    result = gateway.teleop_command(token, 1, action, deadline, is_active=lambda: False)
    assert result["code"] == "expired"
    assert loco.commands.empty()
    assert gateway.teleop_command(token, 1, "stop", None, is_active=lambda: False)["code"] == "stopped"
    assert loco.commands.get_nowait()[0] == (0, 0, 0)


@pytest.mark.parametrize("direction_sequence", [1, 3])
def test_queued_stop_then_direction_outrank_status(hardware, monkeypatch, direction_sequence):
    gateway, loco = hardware
    token = gateway.teleop_acquire()["token"]
    lock = gateway._lock
    thread_state = local()
    wait_for_lock = lock._changed.wait_for

    def notify_waiting(predicate, timeout=None):
        waiting = getattr(thread_state, "waiting", None)
        if waiting is not None:
            waiting.set()
        return wait_for_lock(predicate, timeout)

    def submit_waiting(executor, operation, *args):
        waiting = Event()

        def run():
            thread_state.waiting = waiting
            return operation(*args)

        future = executor.submit(run)
        assert waiting.wait(1), "operation did not reach the SDK lock"
        return future

    monkeypatch.setattr(lock._changed, "wait_for", notify_waiting)
    with ThreadPoolExecutor(max_workers=3) as executor:
        # 模拟 SDK 操作正在执行，再按与实际优先级相反的顺序
        # 将状态查询、方向指令和停步请求入队。
        with lock.hold(_SDKLock.SAFETY):
            status = submit_waiting(executor, gateway.get_state)
            direction = submit_waiting(executor, send, gateway, token, direction_sequence, "forward")
            stop = submit_waiting(executor, send, gateway, token, 2, "stop")
        assert stop.result(timeout=1)["code"] == "stopped"
        assert loco.commands.get_nowait()[0] == (0, 0, 0)
        if direction_sequence == 1:
            assert direction.result(timeout=1)["code"] == "stale_sequence"
            assert status.result(timeout=1)["motion"] == "stop_requested"
            assert loco.commands.empty()
        else:
            assert direction.result(timeout=1)["code"] == "applied"
            assert status.result(timeout=1)["motion"] == "forward"
            assert loco.commands.get_nowait()[0] == (0.5, 0, 0)


def test_controller_link_loss_latches_unconfirmed_stop(hardware):
    gateway, loco = hardware
    state_before_loss = gateway.get_state()["updated_at"]
    token = gateway.teleop_acquire()["token"]
    assert send(gateway, token, 1, "forward")["accepted"]
    assert loco.commands.get(timeout=1)[0] == (0.5, 0, 0)
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
    assert loco.commands.get(timeout=1)[0] == (-0.5, 0, 0)
    assert loco.commands.get(timeout=1)[0] == (0, 0, 0)
    assert gateway.teleop_acquire()["code"] == "hardware_unavailable"
