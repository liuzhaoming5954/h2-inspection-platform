"""Environment telemetry: freshness window, degradation, and wire mapping."""

import io
import json
import time

import pytest

from h2_gateway.client import GatewayClient
from h2_gateway.hardware import AgentEnvironment, H2HardwareGateway
from h2_gateway.server import create_server

from test_hardware import RecordingLoco, wait_for


class FakeEnvironment:
    def __init__(self):
        self.reading = {"temperature_c": 21.3, "humidity_percent": 42.5, "captured_at": 1000.0}
        self.closed = False

    def sample(self):
        return self.reading

    def close(self):
        self.closed = True


def test_agent_environment_expires_at_original_capture_time(monkeypatch):
    wall_time, monotonic_time = 1002.0, 50.0
    monkeypatch.setattr(time, "time", lambda: wall_time)
    monkeypatch.setattr(time, "monotonic", lambda: monotonic_time)
    reading = {"temperature_c": 19.9, "humidity_percent": 6.6, "captured_at": 1000.0}
    payload = {"available": True, "sample": reading}
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()))
    env = AgentEnvironment("unused://")

    env._poll_once()
    assert env.sample() == reading

    # The same sample arrives again at age 7.5 s. A backwards wall clock
    # adjustment must not move the deadline of a sample already received.
    wall_time, monotonic_time = 1007.5, 55.5
    env._poll_once()
    wall_time, monotonic_time = 1006.0, 55.75
    env._poll_once()
    payload["available"] = False
    env._poll_once()

    monotonic_time = 56.0
    assert env.sample() == reading
    monotonic_time = 56.001
    assert env.sample() is None

    # A replay after expiry does not revive the reading, even with the adjusted clock.
    payload["available"] = True
    env._poll_once()
    assert env.sample() is None

    payload["sample"] = {**reading, "captured_at": 1005.5}
    env._poll_once()
    assert env.sample() == payload["sample"]


@pytest.mark.parametrize("timestamp", [None, "1000.0", True, float("nan"), float("inf"), 10**1000, 992.0, 1001.1])
def test_agent_environment_rejects_invalid_capture_times(monkeypatch, timestamp):
    monkeypatch.setattr(time, "time", lambda: 1001.0)
    sample = {"temperature_c": 19.9, "humidity_percent": 6.6}
    if timestamp is not None:
        sample["captured_at"] = timestamp
    payload = {"available": True, "sample": sample}
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()))
    env = AgentEnvironment("unused://")
    env._poll_once()
    assert env.sample() is None


def test_gateway_state_carries_environment_until_it_stales():
    environment = FakeEnvironment()
    gateway = H2HardwareGateway(RecordingLoco(), environment)
    try:
        wait_for(gateway, lambda state: state["teleop_ready"])
        state = gateway.get_state()
        assert state["temperature_c"] == 21.3
        assert state["humidity_percent"] == 42.5
        assert state["environment_captured_at"] == 1000.0

        environment.reading = None  # sensor agent offline
        state = gateway.get_state()
        assert "temperature_c" not in state
        assert "humidity_percent" not in state
        assert "environment_captured_at" not in state
    finally:
        gateway.close()
    assert environment.closed


def test_environment_fields_over_grpc():
    environment = FakeEnvironment()
    gateway = H2HardwareGateway(RecordingLoco(), environment)
    server, port = create_server(gateway, port=0)
    server.start()
    try:
        with GatewayClient(f"127.0.0.1:{port}") as client:
            deadline = time.monotonic() + 2
            state = client.get_state()
            while state["teleop_ready"] is not True and time.monotonic() < deadline:
                time.sleep(0.02)
                state = client.get_state()
            assert state["simulated"] is False
            assert state["temperature_c"] == 21.3
            assert state["humidity_percent"] == 42.5
            assert state["environment_captured_at"] == 1000.0

            environment.reading = None
            deadline = time.monotonic() + 2
            state = client.get_state()
            while state["temperature_c"] is not None and time.monotonic() < deadline:
                time.sleep(0.02)
                state = client.get_state()
            assert state["temperature_c"] is None
            assert state["humidity_percent"] is None
            assert state["environment_captured_at"] is None
    finally:
        server.stop(grace=1).wait()
        gateway.close()
