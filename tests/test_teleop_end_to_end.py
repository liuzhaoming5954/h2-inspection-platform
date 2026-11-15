"""使用实际运行的模拟器 gRPC 服务验证浏览器的 WebSocket 协议。"""
import time

from fastapi.testclient import TestClient
import pytest

from h2_backend.app import create_app
from h2_backend.config import Settings
from h2_gateway.client import GatewayClient
from h2_gateway.engine import SimulatorEngine
from h2_gateway.server import create_server


ORIGIN = {"origin": "http://127.0.0.1:5173"}
# 即使覆盖 HTTP base_url，Starlette 仍以 ws://testserver 解析相对 WebSocket 路径。
# 使用与本地浏览器一致的 Host。
SOCKET = "ws://127.0.0.1:5173/api/teleop/ws"


@pytest.fixture
def stack(tmp_path):
    engine = SimulatorEngine(tmp_path / "simulator.sqlite", step_delay=0.8)
    server, port = create_server(engine, port=0)
    server.start()
    address = f"127.0.0.1:{port}"
    settings = Settings(f"sqlite:///{tmp_path / 'platform.sqlite'}", address, tmp_path / "data")
    try:
        with GatewayClient(address) as observer:
            with TestClient(create_app(settings), base_url="http://127.0.0.1:8000",
                            client=("127.0.0.1", 50000)) as client:
                yield client, observer
    finally:
        server.stop(grace=0).wait()
        engine.close()


def command(ws, sequence, action):
    ws.send_json({"action": action, "sequence": sequence, "sent_at_ms": time.time_ns() // 1_000_000})
    reply = ws.receive_json()
    assert "token" not in reply
    return reply


def wait_state(observer, predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while True:
        state = observer.get_state()
        if predicate(state):
            return state
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for gateway state: {state}")
        time.sleep(0.02)


def test_press_move_stop_and_disconnect(stack):
    client, gateway = stack
    initial_x = gateway.get_state()["pose"]["x"]
    with client.websocket_connect(SOCKET, headers=ORIGIN) as ws:
        lease = ws.receive_json()
        assert lease["type"] == "lease" and lease["accepted"] and "token" not in lease
        assert command(ws, 1, "forward")["accepted"]
        wait_state(gateway, lambda s: s["motion"] == "forward" and s["pose"]["x"] > initial_x)
        assert command(ws, 2, "stop")["accepted"]
        stopped = gateway.get_state()
        assert stopped["motion"] == "stop" and stopped["teleop_active"]
        assert command(ws, 3, "turn_left")["accepted"]
        wait_state(gateway, lambda s: abs(s["pose"]["yaw"]) > 0.01)
    released = wait_state(gateway, lambda s: not s["teleop_active"] and s["motion"] == "stop")
    assert released["pose"]["x"] == stopped["pose"]["x"]
    with client.websocket_connect(SOCKET, headers=ORIGIN) as new_ws:
        assert new_ws.receive_json()["accepted"]
        assert gateway.get_state()["motion"] == "stop"


def test_movement_expiry_and_inspection_arbitration(stack):
    client, gateway = stack
    task = {"name": "互斥验证", "request_id": "teleop-conflict", "points": [
        {"id": "p1", "label": "点位一", "x": 1, "y": 0},
        {"id": "p2", "label": "点位二", "x": 2, "y": 0},
    ]}
    with client.websocket_connect(SOCKET, headers=ORIGIN) as ws:
        assert ws.receive_json()["accepted"]
        with client.websocket_connect(SOCKET, headers=ORIGIN) as other:
            assert other.receive_json()["accepted"] is False
        rejected = client.post("/api/tasks", json=task)
        assert rejected.status_code == 201 and rejected.json()["status"] == "rejected"
        assert command(ws, 1, "forward")["accepted"]
        assert command(ws, 2, "heartbeat")["accepted"]
        # WebSocket 仍保持连接，心跳不能延长运动指令的有效期。
        wait_state(gateway, lambda s: not s["teleop_active"] and s["motion"] == "stop")
        assert command(ws, 3, "forward")["accepted"] is False
    client.app.state.service.recover_outbox()
    assert gateway.get_state()["start_count"] == 0
    started = client.post("/api/tasks", json={**task, "request_id": "after-teleop"}).json()
    assert started["status"] == "running"
    with client.websocket_connect(SOCKET, headers=ORIGIN) as ws:
        assert ws.receive_json()["accepted"] is False
    assert client.post(f"/api/tasks/{started['id']}/stop").json()["status"] == "stopped"
