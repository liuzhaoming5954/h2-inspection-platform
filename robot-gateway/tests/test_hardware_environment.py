"""Environment telemetry: freshness window, degradation, and wire mapping."""

import io
import json
import runpy
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import time
from types import ModuleType, SimpleNamespace

import pytest

from h2_gateway.client import GatewayClient
from h2_gateway.hardware import AgentEnvironment, H2HardwareGateway
from h2_gateway.server import create_server

from test_hardware import RecordingLoco, send, wait_for


PC2_INSTANCE = 'a' * 32


def test_pc2_uses_monotonic_sample_age_and_unique_identity(monkeypatch):
    # Only exercise the PC2 cache. pyserial is installed on PC2, not required here.
    monkeypatch.setitem(sys.modules, "serial", ModuleType("serial"))
    agent = Path(__file__).resolve().parents[2] / "scripts" / "sensor_agent_pc2.py"
    latest = runpy.run_path(str(agent))["Latest"]()
    wall_time, monotonic_time = 1000.0, 50.0
    monkeypatch.setattr(time, "time", lambda: wall_time)
    monkeypatch.setattr(time, "monotonic", lambda: monotonic_time)

    assert latest.snapshot()["available"] is False
    latest.update(19.9, 6.6)
    first = latest.snapshot()["sample"]
    assert first["captured_at"] == wall_time and first["age_seconds"] == 0

    wall_time, monotonic_time = 100.0, 57.5
    unchanged = latest.snapshot()["sample"]
    assert unchanged["sample_id"] == first["sample_id"]
    assert unchanged["captured_at"] == first["captured_at"]
    assert unchanged["age_seconds"] == 7.5
    monotonic_time = 58.0
    assert latest.snapshot()["available"] is False

    wall_time = 1000.0
    latest.update(19.9, 6.6)
    second = latest.snapshot()["sample"]
    assert second["captured_at"] == first["captured_at"]
    assert second["sample_id"] != first["sample_id"]
    assert second["age_seconds"] == 0


class FakeEnvironment:
    def __init__(self):
        self.reading = {"temperature_c": 21.3, "humidity_percent": 42.5, "captured_at": 1000.0,
                        "sample_id": f"{PC2_INSTANCE}:1", "remaining_ms": 8000}
        self.closed = False

    def sample(self):
        return self.reading

    def close(self):
        self.closed = True


def test_agent_environment_uses_monotonic_age_and_conservative_transit(monkeypatch):
    wall_time, monotonic_time = 1002.0, 50.0
    monkeypatch.setattr(time, "time", lambda: wall_time)
    monkeypatch.setattr(time, "monotonic", lambda: monotonic_time)
    reading = {"temperature_c": 19.9, "humidity_percent": 6.6, "captured_at": 5000.0,
               "sample_id": f"{PC2_INSTANCE}:1"}
    payload = {"available": True, "sample": {**reading, "age_seconds": 2.0}}

    def response(*args, **kwargs):
        nonlocal monotonic_time
        monotonic_time += 0.25
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", response)
    env = AgentEnvironment("unused://")

    env._poll_once()
    assert env.sample() == {**reading, "remaining_ms": 5750}

    # Replayed age and independently stepped wall clocks cannot extend the lifetime.
    wall_time, monotonic_time = 6000, 55.0
    payload["sample"]["age_seconds"] = 0.0
    env._poll_once()
    assert env.sample() == {**reading, "remaining_ms": 750}
    payload["available"] = False
    env._poll_once()
    monotonic_time = 56.0
    assert env.sample() is None

    payload["available"] = True
    env._poll_once()
    assert env.sample() is None

    # A genuinely newer sample can carry an earlier PC2 wall timestamp.
    newer = {**reading, "sample_id": f"{PC2_INSTANCE}:2", "captured_at": 4000.0}
    payload["sample"] = {**newer, "age_seconds": 0.5}
    env._poll_once()
    assert env.sample() == {**newer, "remaining_ms": 7250}
    payload["sample"] = {**reading, "age_seconds": 0.0}
    env._poll_once()
    assert env.sample() == {**newer, "remaining_ms": 7000}


def test_agent_environment_new_sample_already_stale_on_arrival(monkeypatch):
    monotonic_time = 50.0
    monkeypatch.setattr(time, "monotonic", lambda: monotonic_time)

    def delayed_fetch():
        nonlocal monotonic_time
        monotonic_time += 0.5
        return {"temperature_c": 19.9, "humidity_percent": 6.6, "captured_at": 5000.0,
                "sample_id": f"{PC2_INSTANCE}:1", "age_seconds": 7.75}

    env = AgentEnvironment("unused://", fetch=delayed_fetch)
    env._poll_once()
    assert env.sample() is None


def test_agent_environment_accepts_new_pc2_instance_but_rejects_retired_instance(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 50.0)
    payload = {"temperature_c": 19.9, "humidity_percent": 6.6, "captured_at": 5000.0,
               "sample_id": f"{PC2_INSTANCE}:1", "age_seconds": 0.0}
    env = AgentEnvironment("unused://", fetch=lambda: payload)
    env._poll_once()
    assert env.sample()["sample_id"] == f"{PC2_INSTANCE}:1"

    restarted_id = f"{'b' * 32}:1"
    payload["sample_id"] = restarted_id
    payload["captured_at"] = 100.0
    env._poll_once()
    assert env.sample()["sample_id"] == restarted_id

    payload["sample_id"] = f"{PC2_INSTANCE}:2"
    env._poll_once()
    assert env.sample()["sample_id"] == restarted_id


@pytest.mark.parametrize("field,value", [
    ("captured_at", None), ("captured_at", "1000.0"), ("captured_at", True),
    ("captured_at", float("nan")), ("captured_at", float("inf")), ("captured_at", 10**1000),
    ("captured_at", 0), ("age_seconds", None), ("age_seconds", -0.1), ("age_seconds", 8.0),
    ("sample_id", None), ("sample_id", f"{PC2_INSTANCE}:01"),
    ("sample_id", f"{PC2_INSTANCE}:{2**53}"),
])
def test_agent_environment_rejects_invalid_freshness_metadata(field, value):
    sample = {"temperature_c": 19.9, "humidity_percent": 6.6, "captured_at": 1000.0,
              "sample_id": f"{PC2_INSTANCE}:1", "age_seconds": 0.0}
    sample[field] = value
    env = AgentEnvironment("unused://", fetch=lambda: sample)
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
        assert state["environment_sample_id"] == f"{PC2_INSTANCE}:1"
        assert state["environment_remaining_ms"] == 8000

        environment.reading = None  # sensor agent offline
        state = gateway.get_state()
        assert "temperature_c" not in state
        assert "humidity_percent" not in state
        assert "environment_captured_at" not in state
        assert "environment_sample_id" not in state
        assert "environment_remaining_ms" not in state
    finally:
        gateway.close()
    assert environment.closed


@pytest.mark.parametrize("blocked_cache", ["environment", "battery"])
def test_slow_telemetry_read_does_not_block_stop(monkeypatch, blocked_cache):
    environment = FakeEnvironment()
    battery = SimpleNamespace(battery=lambda: 78.0)
    loco = RecordingLoco()
    gateway = H2HardwareGateway(loco, environment, battery)
    read_started, unblock_read = Event(), Event()
    source, method = (environment, "sample") if blocked_cache == "environment" else (battery, "battery")
    read_cache = getattr(source, method)

    def read():
        read_started.set()
        assert unblock_read.wait(5)
        return read_cache()

    try:
        wait_for(gateway, lambda state: state["teleop_ready"])
        token = gateway.teleop_acquire()["token"]
        monkeypatch.setattr(source, method, read)
        with ThreadPoolExecutor(max_workers=2) as executor:
            state_request = executor.submit(gateway.get_state)
            try:
                assert read_started.wait(1)
                stop = executor.submit(send, gateway, token, 1, "stop")
                assert stop.result(timeout=1)["code"] == "stopped"
                assert loco.commands.get_nowait()[0] == (0, 0, 0)
                assert not state_request.done()
            finally:
                unblock_read.set()
            state = state_request.result(timeout=1)
        assert state["temperature_c"] == 21.3
        assert state["battery_percent"] == 78.0
    finally:
        gateway.close()


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
            assert state["environment_sample_id"] == f"{PC2_INSTANCE}:1"
            assert 0 < state["environment_remaining_ms"] < 8000

            environment.reading = None
            deadline = time.monotonic() + 2
            state = client.get_state()
            while state["temperature_c"] is not None and time.monotonic() < deadline:
                time.sleep(0.02)
                state = client.get_state()
            assert state["temperature_c"] is None
            assert state["humidity_percent"] is None
            assert state["environment_captured_at"] is None
            assert state["environment_sample_id"] is None
            assert state["environment_remaining_ms"] is None
    finally:
        server.stop(grace=1).wait()
        gateway.close()
