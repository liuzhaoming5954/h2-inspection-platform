"""Exercise the H2 mode through WebSocket and gRPC with a recording SDK substitute."""

import time

from fastapi.testclient import TestClient
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
def hardware_stack(tmp_path):
    loco = RecordingLoco()
    engine = H2HardwareGateway(loco)
    try:
        server, port = create_server(engine, port=0)
        server.start()
        try:
            address = f"127.0.0.1:{port}"
            settings = Settings(f"sqlite:///{tmp_path / 'platform.sqlite'}", address, tmp_path / "data",
                                hardware_teleop=True)
            with GatewayClient(address) as gateway, TestClient(create_app(settings), client=("127.0.0.1", 50000)) as api:
                wait_state(gateway, lambda state: state["teleop_ready"])
                yield api, gateway, loco
        finally:
            server.stop(grace=0).wait()
    finally:
        engine.close()


def command(socket, action, sequence):
    socket.send_json({"action": action, "sequence": sequence, "sent_at_ms": time.time_ns() // 1_000_000})
    response = socket.receive_json()
    assert response["accepted"] and response["sequence"] == sequence
    assert "token" not in response
    return response


def test_hardware_motion_stop_and_disconnect_through_local_api(hardware_stack):
    api, gateway, loco = hardware_stack
    health = api.get("/api/health").json()
    assert health["hardware_teleop"] and health["simulated"] is False
    state = api.get("/api/robot").json()
    assert state["simulated"] is False and state["status"] == "ready"
    assert state["fsm_id"] == 601 and state["fsm_mode"] == 0
    assert state.get("pose") is None and state.get("battery_percent") is None
    assert loco.requests == []

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
            (0.15, 0.0, 0.0), (-0.15, 0.0, 0.0), (0.0, 0.0, 0.25), (0.0, 0.0, -0.25),
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
