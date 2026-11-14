"""Structured h2_event log lines: frames, zero-velocity requests, lease changes."""

import json
import logging
import time

import pytest

from h2_gateway.hardware import H2HardwareGateway

from test_hardware import hardware, send, wait_for
from test_hardware_sustained import SlowAckLoco


def events(caplog):
    parsed = []
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith("h2_event "):
            parsed.append(json.loads(message[len("h2_event "):]))
    return parsed


@pytest.fixture
def logged(caplog):
    caplog.set_level(logging.INFO, logger="h2_gateway.hardware")
    return caplog


def test_admitted_frame_event_carries_timing(hardware, logged):
    gateway, loco = hardware
    token = gateway.teleop_acquire()["token"]
    assert send(gateway, token, 1, "forward")["accepted"]
    frames = [event for event in events(logged) if event["event"] == "teleop_frame"]
    assert len(frames) == 1
    frame = frames[0]
    assert frame["seq"] == 1
    assert frame["action"] == "forward"
    assert frame["result"] == "applied"
    assert 0 < frame["duration_ms"] <= 250
    assert frame["remaining_ms"] >= frame["duration_ms"]
    assert frame["sdk_ms"] >= 0
    assert "mono_ms" in frame


def test_frame_expiry_logs_zero_velocity(hardware, logged):
    gateway, loco = hardware
    token = gateway.teleop_acquire()["token"]
    assert send(gateway, token, 1, "forward")["accepted"]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        zeros = [event for event in events(logged)
                 if event["event"] == "zero_velocity" and event["reason"] == "frame_expired"]
        if zeros:
            break
        time.sleep(0.02)
    assert zeros, "an expiring motion frame must request zero velocity"
    assert zeros[0]["ack"] is True
    assert zeros[0]["sdk_ms"] >= 0


def test_stop_release_and_lease_events(hardware, logged):
    gateway, loco = hardware
    token = gateway.teleop_acquire()["token"]
    assert send(gateway, token, 1, "forward")["accepted"]
    assert send(gateway, token, 2, "stop")["accepted"]
    assert gateway.teleop_release(token)["accepted"]
    zeros = [event for event in events(logged) if event["event"] == "zero_velocity"]
    assert [(zero["reason"], zero["ack"]) for zero in zeros] == [("stop", True)]
    leases = [event for event in events(logged) if event["event"] == "lease"]
    assert leases[0]["change"] == "acquired"
    assert any(lease["change"] == "revoked" and lease["reason"] == "release"
               for lease in leases)


def test_rejections_are_logged(hardware, logged):
    gateway, loco = hardware
    token = gateway.teleop_acquire()["token"]
    assert send(gateway, token, 1, "forward", ttl=-1)["code"] == "expired"
    assert send(gateway, token, 2, "forward", ttl=3000)["code"] == "invalid_expiry"
    assert send(gateway, token, 2, "forward")["accepted"]
    assert send(gateway, token, 2, "stop")["code"] == "stale_sequence"
    assert gateway.teleop_command("wrong-token", 9, "stop", None)["code"] == "invalid_token"
    codes = [event["code"] for event in events(logged) if event["event"] == "teleop_reject"]
    assert codes == ["expired", "invalid_expiry", "stale_sequence", "invalid_token"]


def test_late_acknowledgement_logs_zero_reason(caplog):
    caplog.set_level(logging.INFO, logger="h2_gateway.hardware")
    gateway = H2HardwareGateway(SlowAckLoco(delay=0.30))
    try:
        wait_for(gateway, lambda state: state["teleop_ready"])
        token = gateway.teleop_acquire()["token"]
        reply = send(gateway, token, 1, "forward")
        assert reply["accepted"] and reply["code"] == "stopped"
        assert any(event["event"] == "teleop_frame" and event["result"] == "late_ack_zeroed"
                   for event in events(caplog))
        assert any(event["event"] == "zero_velocity" and event["reason"] == "late_frame"
                   for event in events(caplog))
    finally:
        gateway.close()
