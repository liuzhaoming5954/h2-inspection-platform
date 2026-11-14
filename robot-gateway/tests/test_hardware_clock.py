"""Cross-clock tolerance semantics: healthy commands survive skew, stale motion dies."""

import time

from h2_gateway.hardware import CLOCK_SKEW_TOLERANCE_MS, GATEWAY_MAX_FUTURE_MS, H2HardwareGateway

from test_hardware import RecordingLoco, wait_for


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def make_ready_gateway():
    gateway = H2HardwareGateway(RecordingLoco())
    try:
        wait_for(gateway, lambda state: state["teleop_ready"])
        reply = gateway.teleop_acquire()
        assert reply["accepted"], reply
        return gateway, reply["token"]
    except Exception:
        gateway.close()
        raise


def test_heartbeat_survives_healthy_skew_both_directions():
    gateway, token = make_ready_gateway()
    try:
        # Host behind PC3 by most of the tolerance (stale per the old check).
        past = now_ms() - int(CLOCK_SKEW_TOLERANCE_MS * 0.95)
        reply = gateway.teleop_command(token, 1, "heartbeat", past)
        assert reply["accepted"] and reply["code"] == "renewed", reply

        # Host ahead of PC3 by well beyond the old 500 ms future cap.
        future = now_ms() + int(GATEWAY_MAX_FUTURE_MS * 0.95)
        reply = gateway.teleop_command(token, 2, "heartbeat", future)
        assert reply["accepted"] and reply["code"] == "renewed", reply
    finally:
        gateway.close()


def test_heartbeat_rejected_beyond_tolerance():
    gateway, token = make_ready_gateway()
    try:
        too_far_future = now_ms() + GATEWAY_MAX_FUTURE_MS + 500
        reply = gateway.teleop_command(token, 1, "heartbeat", too_far_future)
        assert not reply["accepted"] and reply["code"] == "invalid_expiry", reply

        too_stale = now_ms() - CLOCK_SKEW_TOLERANCE_MS - 500
        reply = gateway.teleop_command(token, 2, "heartbeat", too_stale)
        assert not reply["accepted"] and reply["code"] == "expired", reply
    finally:
        gateway.close()


def test_stale_movement_command_is_still_rejected():
    gateway, token = make_ready_gateway()
    try:
        stale = now_ms() - 1500  # inside heartbeat tolerance, but motion must die
        reply = gateway.teleop_command(token, 1, "forward", stale)
        assert not reply["accepted"] and reply["code"] == "expired", reply
    finally:
        gateway.close()


def test_stop_keeps_full_clock_skew_exemption():
    gateway, token = make_ready_gateway()
    try:
        huge_skew = now_ms() - 60_000
        reply = gateway.teleop_command(token, 1, "stop", huge_skew)
        assert reply["accepted"] and reply["code"] == "stopped", reply
    finally:
        gateway.close()
