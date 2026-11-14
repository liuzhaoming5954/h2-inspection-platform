"""Hardware command deadlines use PC3 monotonic time and fail closed."""

import time

import pytest

from h2_gateway.hardware import H2HardwareGateway, MAX_HARDWARE_COMMAND_MS
from test_hardware import RecordingLoco, wait_for


def now_ms():
    return time.monotonic_ns() // 1_000_000


@pytest.fixture
def ready_gateway():
    loco = RecordingLoco()
    gateway = H2HardwareGateway(loco)
    try:
        wait_for(gateway, lambda state: state["teleop_ready"])
        yield gateway, loco, gateway.teleop_acquire()["token"]
    finally:
        gateway.close()


@pytest.mark.parametrize("jump", [-52_000_000_000, 52_000_000_000])
def test_wall_clock_jump_does_not_affect_hardware_commands(ready_gateway, monkeypatch, jump):
    gateway, loco, token = ready_gateway
    wall_time = time.time_ns()
    monkeypatch.setattr(time, "time_ns", lambda: wall_time + jump)
    assert gateway.teleop_command(token, 1, "heartbeat", now_ms() + 300)["code"] == "renewed"
    assert gateway.teleop_command(token, 2, "forward", now_ms() + 300)["code"] == "applied"
    assert loco.commands.get_nowait()[0] == (0.15, 0.0, 0.0)
    assert gateway.teleop_command(token, 3, "stop", None)["code"] == "stopped"
    assert loco.commands.get_nowait()[0] == (0.0, 0.0, 0.0)


@pytest.mark.parametrize("action", ["forward", "heartbeat"])
def test_invalid_deadlines_do_not_renew_lease(ready_gateway, action):
    gateway, loco, token = ready_gateway
    lease_until = gateway._lease_until
    for deadline, expected in [(None, "invalid_expiry"), (now_ms() - 1, "expired"),
                               (now_ms() + MAX_HARDWARE_COMMAND_MS + 100, "invalid_expiry")]:
        assert gateway.teleop_command(token, 1, action, deadline)["code"] == expected
    assert gateway._lease_until == lease_until
    assert loco.commands.empty()
