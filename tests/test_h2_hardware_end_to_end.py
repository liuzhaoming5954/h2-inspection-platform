"""Exercise the H2 mode through WebSocket and gRPC with a recording SDK substitute."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from fastapi.testclient import TestClient
import grpc
import pytest

from h2_backend.app import create_app
from h2_backend.config import Settings
from h2_gateway.client import GatewayClient
from h2_gateway.hardware import H2HardwareGateway
from h2_gateway.server import create_server


SOCKET = "ws://127.0.0.1:5173/api/teleop/ws"
ORIGIN = {"origin": "http://127.0.0.1:5173"}


class RecordingLoco:
    def __init__(self):
        self.requests = []

    def fsm(self):
        return 601, 0

    def velocity(self, vx, vy, omega, duration):
        self.requests.append((vx, vy, omega, duration))


def wait_state(gateway, predicate):
    deadline = time.monotonic() + 2
    while True:
        state = gateway.get_state()
        if predicate(state):
            return state
        if time.monotonic() >= deadline:
            pytest.fail(f"Timed out waiting for gateway state: {state}")
        time.sleep(0.01)


@pytest.fixture
def hardware_stack(tmp_path, request):
    loco = RecordingLoco()
    environment = BlockingEnvironment() if getattr(request, "param", False) else None
    engine = H2HardwareGateway(loco, environment)
    try:
        status, port = create_server(engine, port=0, lane="state")
        control, control_port = create_server(engine, port=0, lane="teleop")
        status.start()
        control.start()
        try:
            address = f"127.0.0.1:{port}"
            control_address = f"127.0.0.1:{control_port}"
            settings = Settings(f"sqlite:///{tmp_path / 'platform.sqlite'}", address, tmp_path / "data",
                                hardware_teleop=True, teleop_gateway_address=control_address)
            with GatewayClient(address) as gateway, TestClient(create_app(settings), client=("127.0.0.1", 50000)) as api:
                wait_state(gateway, lambda state: state["teleop_ready"])
                yield api, gateway, loco, environment
        finally:
            control.stop(grace=0).wait()
            status.stop(grace=0).wait()
    finally:
        engine.close()


def command(socket, action, sequence):
    socket.send_json({"action": action, "sequence": sequence, "sent_at_ms": time.time_ns() // 1_000_000})
    response = socket.receive_json()
    assert response["accepted"] and response["sequence"] == sequence
    assert "token" not in response
    return response


def test_hardware_motion_stop_and_disconnect_through_local_api(hardware_stack):
    api, gateway, loco, _ = hardware_stack
    health = api.get("/api/health").json()
    assert health["hardware_teleop"] and health["simulated"] is False
    state = api.get("/api/robot").json()
    assert state["simulated"] is False and state["status"] == "ready"
    assert state["fsm_id"] == 601 and state["fsm_mode"] == 0
    assert state.get("pose") is None and state.get("battery_percent") is None
    assert loco.requests == []
    with pytest.raises(grpc.RpcError) as rejected:
        gateway.teleop_acquire()
    assert rejected.value.code() == grpc.StatusCode.UNIMPLEMENTED

    task = {"name": "示例巡检", "request_id": "hardware-only", "points": [
        {"id": "point", "label": "点位", "x": 0, "y": 0},
    ]}
    assert api.post("/api/tasks", json=task).status_code == 409
    assert api.post("/api/tasks/hardware-only/stop").status_code == 409
    assert api.get("/api/tasks").json() == []

    with api.websocket_connect(SOCKET, headers=ORIGIN) as socket:
        lease = socket.receive_json()
        assert lease["accepted"] and lease["code"] == "acquired" and "token" not in lease
        directions = ["forward", "backward", "turn_left", "turn_right"]
        for sequence, direction in enumerate(directions, start=1):
            assert command(socket, direction, sequence)["code"] == "applied"
        assert [velocity[:3] for velocity in loco.requests] == [
            (0.5, 0.0, 0.0), (-0.5, 0.0, 0.0), (0.0, 0.0, 0.55), (0.0, 0.0, -0.55),
        ]
        assert all(0 < velocity[3] <= 0.25 for velocity in loco.requests)
        stopped = command(socket, "stop", 5)
        assert stopped["code"] == "stopped" and stopped["message"] == "H2 已接收停步请求。"
        assert loco.requests[-1][:3] == (0.0, 0.0, 0.0)
        assert gateway.get_state()["motion"] == "stop_requested"
        assert command(socket, "forward", 6)["code"] == "applied"

    state = wait_state(gateway, lambda state: not state["teleop_active"])
    assert state["motion"] == "stop_requested" and loco.requests[-1][:3] == (0.0, 0.0, 0.0)
    assert len(loco.requests) == 7
    with api.websocket_connect(SOCKET, headers=ORIGIN) as socket:
        assert socket.receive_json()["accepted"]
        assert command(socket, "release", 1)["code"] == "released"
    assert len(loco.requests) == 7


class BlockingEnvironment:
    def __init__(self):
        self.blocked = False
        self.entered = Event()
        self.resume = Event()
        self.lock = Lock()
        self.calls = 0

    def sample(self):
        if self.blocked:
            with self.lock:
                self.calls += 1
                if self.calls == 8:
                    self.entered.set()
            assert self.resume.wait(5)
        return None

    def close(self):
        self.resume.set()


@pytest.mark.parametrize("hardware_stack", [True], indirect=True)
def test_teleop_uses_separate_workers_from_saturated_telemetry(hardware_stack):
    api, telemetry, loco, environment = hardware_stack
    with ThreadPoolExecutor(max_workers=8) as callers:
        environment.blocked = True
        polls = [callers.submit(telemetry.get_state) for _ in range(8)]
        try:
            assert environment.entered.wait(2)
            with api.websocket_connect(SOCKET, headers=ORIGIN) as socket:
                assert socket.receive_json()["code"] == "acquired"
                assert command(socket, "forward", 1)["code"] == "applied"
                assert command(socket, "stop", 2)["code"] == "stopped"
                assert command(socket, "release", 3)["code"] == "released"
            assert len(loco.requests) == 2 and loco.requests[-1][:3] == (0.0, 0.0, 0.0)
            assert not any(poll.done() for poll in polls)
        finally:
            environment.resume.set()
        for poll in polls:
            poll.result(timeout=2)
