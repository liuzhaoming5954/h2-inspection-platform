"""Environment telemetry: freshness window, degradation, and wire mapping."""

import time

import pytest

from h2_gateway.client import GatewayClient
from h2_gateway.hardware import AgentEnvironment, H2HardwareGateway
from h2_gateway.server import create_server

from test_hardware import RecordingLoco, wait_for


class FakeEnvironment:
    def __init__(self):
        self.reading = {"temperature_c": 21.3, "humidity_percent": 42.5}
        self.closed = False

    def sample(self):
        return self.reading

    def close(self):
        self.closed = True


def test_agent_environment_keeps_last_fresh_reading():
    readings = [{"temperature_c": 19.9, "humidity_percent": 6.6}]
    fetches = {"count": 0}

    def fetch():
        fetches["count"] += 1
        return readings[-1]

    env = AgentEnvironment("unused://", interval=0.05, fresh_seconds=0.3, fetch=fetch).start()
    try:
        deadline = time.monotonic() + 2
        while env.sample() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert env.sample() == {"temperature_c": 19.9, "humidity_percent": 6.6}

        readings.append(None)  # agent goes down; last good value must survive
        time.sleep(0.15)
        assert env.sample() is not None

        time.sleep(0.5)  # past fresh_seconds with no updates
        assert env.sample() is None
    finally:
        env.close()


def test_agent_environment_rejects_malformed_payloads():
    def fetch():
        return {"temperature_c": "warm", "humidity_percent": None}

    env = AgentEnvironment("unused://", interval=0.05, fresh_seconds=1.0, fetch=fetch).start()
    try:
        time.sleep(0.15)
        assert env.sample() is None
    finally:
        env.close()


def test_gateway_state_carries_environment_until_it_stales():
    environment = FakeEnvironment()
    gateway = H2HardwareGateway(RecordingLoco(), environment)
    try:
        wait_for(gateway, lambda state: state["teleop_ready"])
        state = gateway.get_state()
        assert state["temperature_c"] == 21.3
        assert state["humidity_percent"] == 42.5

        environment.reading = None  # sensor agent offline
        assert "temperature_c" not in gateway.get_state()
        assert "humidity_percent" not in gateway.get_state()
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

            environment.reading = None
            deadline = time.monotonic() + 2
            state = client.get_state()
            while state["temperature_c"] is not None and time.monotonic() < deadline:
                time.sleep(0.02)
                state = client.get_state()
            assert state["temperature_c"] is None
            assert state["humidity_percent"] is None
    finally:
        server.stop(grace=1).wait()
        gateway.close()
