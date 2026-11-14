"""Sustained-motion regressions: FSM starvation and late SDK acknowledgements."""

import time

from h2_gateway.hardware import H2HardwareGateway

from test_hardware import RecordingLoco, wait_for


class SlowAckLoco(RecordingLoco):
    """ velocity() takes longer than a frame's validity, like a loaded DDS round trip."""

    def __init__(self, delay: float = 0.30):
        super().__init__()
        self.delay = delay

    def velocity(self, vx, vy, omega, duration):
        time.sleep(self.delay)
        return super().velocity(vx, vy, omega, duration)


def test_sustained_frames_keep_the_lease_alive():
    # Frames every 200 ms with 250 ms validity used to starve the FSM poll:
    # the state went stale, _ready() failed, and the lease was revoked
    # mid-motion. The watch loop must keep polling while frames flow.
    gateway = H2HardwareGateway(RecordingLoco())
    try:
        wait_for(gateway, lambda state: state["teleop_ready"])
        token = gateway.teleop_acquire()["token"]
        deadline = time.monotonic() + 2.5
        seq = 0
        while time.monotonic() < deadline:
            seq += 1
            reply = gateway.teleop_command(token, seq, "forward",
                                           time.time_ns() // 1_000_000 + 350)
            assert reply["accepted"], reply
            time.sleep(0.2)
        assert gateway.get_state()["teleop_active"] is True
        assert gateway.get_state()["status"] == "teleop"
    finally:
        gateway.close()


def test_late_sdk_acknowledgement_halts_motion_but_keeps_the_lease():
    gateway = H2HardwareGateway(SlowAckLoco(delay=0.30))
    loco = gateway._loco
    try:
        wait_for(gateway, lambda state: state["teleop_ready"])
        token = gateway.teleop_acquire()["token"]
        # remaining ≈ 350 ms -> duration 250 ms; the RPC sleeps 300 ms, so the
        # acknowledgement arrives after the frame's own deadline.
        reply = gateway.teleop_command(token, 1, "forward",
                                       time.time_ns() // 1_000_000 + 350)
        assert reply["accepted"] and reply["code"] == "stopped", reply
        # The late frame executed, then an ordered zero velocity follows it…
        assert loco.commands.get(timeout=1)[0] == (0.15, 0, 0)
        assert loco.commands.get(timeout=1)[0] == (0, 0, 0)
        # …and the lease survives: the next command goes straight through.
        reply = gateway.teleop_command(token, 2, "heartbeat",
                                       time.time_ns() // 1_000_000 + 350)
        assert reply["accepted"] and reply["code"] == "renewed", reply
        assert gateway.get_state()["teleop_active"] is True
    finally:
        gateway.close()
