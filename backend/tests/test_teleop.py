import time
from threading import Event

from fastapi import WebSocketDisconnect
import pytest
from sqlalchemy import select

from h2_backend.models import Outbox


PATH = "ws://127.0.0.1:8000/api/teleop/ws"
HEADERS = {"origin": "http://127.0.0.1:5173", "host": "127.0.0.1:8000"}
PRIVATE_TOKEN = "private-simulator-token"


def now_ms():
    return time.time_ns() // 1_000_000


def send(socket, action, sequence, sent_at_ms=None):
    socket.send_json({"action": action, "sequence": sequence,
                      "sent_at_ms": now_ms() if sent_at_ms is None else sent_at_ms})
    return socket.receive_json()


class TeleopBoundary:
    def __init__(self, gateway):
        self.gateway = gateway
        self.state = gateway.get_state
        self.start = gateway.start_task
        self.simulated = True
        self.active = False
        self.motion = "stop"
        self.acquires = 0
        self.commands = []
        self.releases = []
        self.released = Event()
        self.timeout_at = None

    @staticmethod
    def response(accepted, code):
        return {"accepted": accepted, "code": code, "message": f"internal details {PRIVATE_TOKEN}",
                "token": PRIVATE_TOKEN}

    def get_state(self):
        state = self.state()
        return {**state, "status": "teleop" if self.active else state["status"],
                "simulated": self.simulated, "teleop_active": self.active, "motion": self.motion,
                "pose": {"x": 1, "y": 2, "yaw": 0.25}}

    def acquire(self):
        self.acquires += 1
        if self.timeout_at == "acquire":
            raise TimeoutError(f"internal details {PRIVATE_TOKEN}")
        if self.active:
            return self.response(False, "teleop_busy")
        if any(t["status"] == "running" for t in self.gateway.tasks.values()):
            return self.response(False, "robot_busy")
        self.active = True
        return self.response(True, "acquired")

    def command(self, token, sequence, action, expires_at_ms):
        self.commands.append((token, sequence, action, expires_at_ms))
        if self.timeout_at == "command":
            raise TimeoutError(f"internal details {PRIVATE_TOKEN}")
        if token != PRIVATE_TOKEN or not self.active:
            return self.response(False, "invalid_token")
        if action == "heartbeat":
            return self.response(True, "renewed")
        self.motion = action
        return self.response(True, "stopped" if action == "stop" else "applied")

    def release(self, token):
        self.releases.append(token)
        self.active = False
        self.motion = "stop"
        self.released.set()
        return self.response(True, "released")

    def start_task(self, *args):
        if self.active:
            return {"accepted": False, "code": "teleop_busy", "message": "Manual control is active"}
        return self.start(*args)


@pytest.fixture
def teleop(gateway, monkeypatch):
    boundary = TeleopBoundary(gateway)
    for name, method in {
        "get_state": boundary.get_state, "start_task": boundary.start_task,
        "teleop_acquire": boundary.acquire, "teleop_command": boundary.command,
        "teleop_release": boundary.release,
    }.items():
        monkeypatch.setattr(gateway, name, method, raising=False)
    return boundary


def test_control_contract_preserves_freshness_and_keeps_token_private(client, teleop):
    with client.websocket_connect(PATH, headers=HEADERS) as socket:
        lease = socket.receive_json()
        assert lease["type"] == "lease" and lease["accepted"] is True
        assert set(lease) == {"type", "accepted", "code", "message"}
        assert PRIVATE_TOKEN not in str(lease)

        sent_at = now_ms()
        motion = send(socket, "forward", 1, sent_at)
        assert motion["accepted"] is True and motion["sequence"] == 1
        assert teleop.commands == [(PRIVATE_TOKEN, 1, "forward", sent_at + 350)]
        state = client.get("/api/robot").json()
        assert state["status"] == "teleop" and state["teleop_active"] is True
        assert state["motion"] == "forward" and state["pose"]["yaw"] == 0.25
        assert PRIVATE_TOKEN not in str(state)

        heartbeat = send(socket, "heartbeat", 2)
        assert heartbeat["code"] == "renewed"
        stopped = send(socket, "stop", 3, 0)
        assert stopped["code"] == "stopped"
        assert teleop.commands[-1][3] > now_ms()
        released = send(socket, "release", 4, 0)
        assert released["code"] == "released" and released["sequence"] == 4
        assert teleop.releases == [PRIVATE_TOKEN]
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1000


@pytest.mark.parametrize("headers", [
    {**HEADERS, "origin": "http://untrusted.example:5173"},
    {**HEADERS, "host": "untrusted.example:5173"},
    {**HEADERS, "x-forwarded-host": "untrusted.example:5173"},
    {"host": HEADERS["host"]},
])
def test_untrusted_websocket_rejected_before_lease(client, teleop, headers):
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(PATH, headers=headers):
            pytest.fail("Untrusted websocket accepted")
    assert rejected.value.code == 1008
    assert teleop.acquires == 0


def test_non_simulator_rejected_before_acquiring_lease(client, teleop):
    teleop.simulated = False
    with client.websocket_connect(PATH, headers=HEADERS) as socket:
        lease = socket.receive_json()
        assert lease["type"] == "lease" and lease["accepted"] is False
        assert lease["code"] == "simulation_required"
    assert teleop.acquires == 0
    assert client.get("/api/robot").json()["simulated"] is False


def test_invalid_and_stale_moves_are_not_sent_to_gateway(client, teleop):
    with client.websocket_connect(PATH, headers=HEADERS) as socket:
        assert socket.receive_json()["accepted"]
        socket.send_json({"action": "forward", "sequence": "1", "sent_at_ms": now_ms()})
        assert socket.receive_json()["code"] == "invalid_message"
        socket.send_json({"action": "forward", "sequence": 1, "sent_at_ms": now_ms(), "token": "injected"})
        assert socket.receive_json()["code"] == "invalid_message"
        socket.send_bytes(b"invalid binary frame")
        assert socket.receive_json()["code"] == "invalid_message"
        stale = send(socket, "forward", 2, now_ms() - 351)
        future = send(socket, "turn_right", 3, now_ms() + 1000)
        assert stale["code"] == "stale_command" and stale["sequence"] == 2
        assert future["code"] == "future_command" and future["sequence"] == 3
        assert not stale["accepted"] and not future["accepted"]
        assert teleop.commands == []


def test_contending_socket_cannot_steal_lease_and_disconnect_releases(client, teleop):
    with client.websocket_connect(PATH, headers=HEADERS) as owner:
        assert owner.receive_json()["accepted"]
        with client.websocket_connect(PATH, headers=HEADERS) as contender:
            denied = contender.receive_json()
            assert denied["type"] == "lease" and not denied["accepted"]
            assert denied["code"] == "teleop_busy"
        assert teleop.releases == []
        assert send(owner, "forward", 1)["accepted"]
        owner.close()
        assert teleop.released.wait(1)
        assert not teleop.active and teleop.motion == "stop"
    assert teleop.releases == [PRIVATE_TOKEN]


def test_expired_lease_closes_socket_without_automatic_reacquisition(client, teleop):
    with client.websocket_connect(PATH, headers=HEADERS) as socket:
        assert socket.receive_json()["accepted"]
        teleop.active = False
        rejected = send(socket, "heartbeat", 1)
        assert rejected["code"] == "invalid_token" and not rejected["accepted"]
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1008
    assert teleop.acquires == 1


def test_idle_socket_closes_and_releases_lease(client, teleop, monkeypatch):
    monkeypatch.setattr("h2_backend.teleop.IDLE_TIMEOUT_SECONDS", 0.03)
    with client.websocket_connect(PATH, headers=HEADERS) as socket:
        assert socket.receive_json()["accepted"]
        expired = socket.receive_json()
        assert expired["type"] == "error" and expired["code"] == "idle_timeout"
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
        assert teleop.released.wait(1)
    assert teleop.releases == [PRIVATE_TOKEN]


@pytest.mark.parametrize("stage", ["acquire", "command"])
def test_rpc_timeout_reports_safe_error_and_closes(client, teleop, stage):
    teleop.timeout_at = stage
    with client.websocket_connect(PATH, headers=HEADERS) as socket:
        initial = socket.receive_json()
        error = initial if stage == "acquire" else send(socket, "forward", 1)
        assert error["type"] == ("lease" if stage == "acquire" else "error")
        assert error["code"] == "gateway_timeout" and not error["accepted"]
        assert PRIVATE_TOKEN not in str(error)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1011
        if stage == "command":
            assert teleop.released.wait(1)


def test_inspection_rejected_during_teleop_does_not_replay_after_release(client, app, teleop, payload):
    with client.websocket_connect(PATH, headers=HEADERS) as socket:
        assert socket.receive_json()["accepted"]
        task = client.post("/api/tasks", json=payload).json()
        assert task["status"] == "rejected"
        with app.state.service.sessions() as db:
            outbox = db.scalar(select(Outbox).where(Outbox.task_id == task["id"]))
            assert outbox.status == "rejected" and outbox.result["code"] == "teleop_busy"
        assert send(socket, "release", 1)["accepted"]
    app.state.service.recover_outbox()
    retried = client.post("/api/tasks", json=payload).json()
    assert retried["id"] == task["id"] and retried["status"] == "rejected"
    assert teleop.gateway.starts == 0


def test_inspection_running_keeps_teleop_from_getting_lease(client, teleop, payload):
    assert client.post("/api/tasks", json=payload).json()["status"] == "running"
    with client.websocket_connect(PATH, headers=HEADERS) as socket:
        denied = socket.receive_json()
        assert denied["code"] == "robot_busy" and not denied["accepted"]
    assert teleop.releases == []
