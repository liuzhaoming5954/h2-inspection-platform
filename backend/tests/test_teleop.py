from dataclasses import replace
import time
from threading import Event

from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from h2_backend.app import create_app
from h2_backend.models import Base, Outbox, Task
from h2_backend import teleop as teleop_module


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
        self.ready = None
        self.active = False
        self.motion = "stop"
        self.acquires = 0
        self.commands = []
        self.releases = []
        self.released = Event()
        self.timeout_at = None
        self.command_failure = None
        self.clock_offset_ms = 0

    def response(self, accepted, code):
        reply = {"accepted": accepted, "code": code, "message": f"internal details {PRIVATE_TOKEN}",
                 "token": PRIVATE_TOKEN}
        if not self.simulated:
            stamp = time.monotonic_ns()
            reply.update(gateway_monotonic_ms=stamp // 1_000_000 + self.clock_offset_ms,
                         received_monotonic_ns=stamp)
        return reply

    def get_state(self):
        state = self.state()
        status = state["status"] if self.simulated else "ready"
        return {**state, "status": "teleop" if self.active else status,
                "simulated": self.simulated, "teleop_ready": self.ready, "teleop_active": self.active, "motion": self.motion,
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

    def command(self, token, sequence, action, expires_at_ms, **options):
        if not self.simulated and action not in ("stop", "release"):
            assert not expires_at_ms and options["gateway_deadline_ms"] > 0
            assert 0 < options["timeout"] <= 0.35
        self.commands.append((token, sequence, action, expires_at_ms))
        if self.timeout_at == "command":
            raise TimeoutError(f"internal details {PRIVATE_TOKEN}")
        if token != PRIVATE_TOKEN or not self.active:
            return self.response(False, "invalid_token")
        if self.command_failure:
            return self.response(False, self.command_failure)
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


@pytest.fixture
def hardware_client(settings, gateway, teleop):
    teleop.simulated = False
    teleop.ready = True
    app = create_app(replace(settings, hardware_teleop=True), gateway)
    Base.metadata.drop_all(app.state.service.engine)
    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            yield client
    finally:
        Base.metadata.drop_all(app.state.service.engine)


@pytest.mark.parametrize("simulated,ready,code", [
    (True, True, "hardware_required"),
    (False, None, "robot_not_ready"),
    (False, False, "robot_not_ready"),
])
def test_hardware_lease_requires_ready_hardware(hardware_client, teleop, simulated, ready, code):
    teleop.simulated = simulated
    teleop.ready = ready
    with hardware_client.websocket_connect(PATH, headers=HEADERS) as socket:
        response = socket.receive_json()
        assert response["code"] == code and not response["accepted"]
    assert teleop.acquires == 0


def test_hardware_rejects_non_loopback_peer_even_with_local_headers(hardware_client, teleop):
    client = TestClient(hardware_client.app, client=("192.0.2.10", 50000))
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(PATH, headers=HEADERS):
            pytest.fail("Hardware teleop accepted a non-loopback peer")
    assert rejected.value.code == 1008
    assert teleop.acquires == 0


def test_hardware_ack_distinguishes_request_from_measured_motion(hardware_client, teleop):
    assert hardware_client.get("/api/health").json()["hardware_teleop"] is True
    assert hardware_client.get("/api/robot").json()["simulated"] is False
    with hardware_client.websocket_connect(PATH, headers=HEADERS) as socket:
        assert socket.receive_json()["code"] == "acquired"
        assert send(socket, "forward", 1)["message"] == "H2 已接收运动指令。"
        stop = send(socket, "stop", 2)
        assert stop["code"] == "stopped" and stop["message"] == "H2 已接收停步请求。"
        assert send(socket, "release", 3)["accepted"]
    assert teleop.releases == [PRIVATE_TOKEN]


@pytest.mark.parametrize("priority,final_code", [("stop", "stopped"), ("release", "released")])
def test_hardware_priority_discards_queued_motion(hardware_client, teleop, monkeypatch, priority, final_code):
    first_started = Event()
    finish_first = Event()

    def command(*args, **kwargs):
        if args[1] == 1:
            first_started.set()
            assert finish_first.wait(2)
        return teleop.command(*args, **kwargs)

    monkeypatch.setattr(teleop.gateway, "teleop_command", command)
    with hardware_client.websocket_connect(PATH, headers=HEADERS) as socket:
        try:
            assert socket.receive_json()["code"] == "acquired"
            socket.send_json({"action": "forward", "sequence": 1, "sent_at_ms": now_ms()})
            assert first_started.wait(1)

            socket.send_json({"action": "forward", "sequence": 2, "sent_at_ms": now_ms()})
            socket.send_json({"action": "turn_right", "sequence": 3, "sent_at_ms": now_ms()})
            dropped = socket.receive_json()
            assert (dropped["code"], dropped["sequence"]) == ("superseded", 2)

            socket.send_json({"action": priority, "sequence": 4, "sent_at_ms": 0})
            dropped = socket.receive_json()
            assert (dropped["code"], dropped["sequence"]) == ("superseded", 3)

            if priority == "stop":
                socket.send_json({"action": "forward", "sequence": 5, "sent_at_ms": now_ms()})
                dropped = socket.receive_json()
                assert (dropped["code"], dropped["sequence"]) == ("superseded", 5)

            finish_first.set()
            assert socket.receive_json()["code"] == "applied"
            priority_reply = socket.receive_json()
            assert (priority_reply["code"], priority_reply["sequence"]) == (final_code, 4)
            expected = [(1, "forward"), (4, "stop")] if priority == "stop" else [(1, "forward")]
            assert [(item[1], item[2]) for item in teleop.commands] == expected

            if priority == "stop":
                assert send(socket, "turn_left", 6)["code"] == "applied"
                assert send(socket, "release", 7)["code"] == "released"
        finally:
            finish_first.set()
    assert teleop.releases == [PRIVATE_TOKEN]


def test_hardware_late_direction_stop_ack_keeps_session(hardware_client, teleop, monkeypatch):
    def command(*args, **kwargs):
        if args[2] == "forward":
            return teleop.response(True, "stopped")
        return teleop.command(*args, **kwargs)

    monkeypatch.setattr(teleop.gateway, "teleop_command", command)
    with hardware_client.websocket_connect(PATH, headers=HEADERS) as socket:
        assert socket.receive_json()["code"] == "acquired"
        late = send(socket, "forward", 1)
        assert late["accepted"] and late["code"] == "stopped"
        assert send(socket, "heartbeat", 2)["code"] == "renewed"
        assert send(socket, "release", 3)["code"] == "released"


def test_hardware_translates_to_pc3_monotonic_epoch(hardware_client, teleop, monkeypatch):
    teleop.clock_offset_ms = 52_000

    def command(token, sequence, action, expires_at_ms, **options):
        remaining = options["gateway_deadline_ms"] - teleop.clock_offset_ms - time.monotonic_ns() // 1_000_000
        assert 0 < remaining <= teleop_module.COMMAND_TTL_MS
        return teleop.command(token, sequence, action, expires_at_ms, **options)

    monkeypatch.setattr(teleop.gateway, "teleop_command", command)
    with hardware_client.websocket_connect(PATH, headers=HEADERS) as socket:
        lease = socket.receive_json()
        assert lease["code"] == "acquired" and "gateway_monotonic_ms" not in lease
        assert send(socket, "forward", 1)["code"] == "applied"
        assert send(socket, "heartbeat", 2)["code"] == "renewed"
        assert send(socket, "release", 3)["code"] == "released"


def test_hardware_releases_incompatible_legacy_gateway_lease(hardware_client, teleop, monkeypatch):
    def acquire():
        reply = teleop.acquire()
        del reply["gateway_monotonic_ms"], reply["received_monotonic_ns"]
        return reply

    monkeypatch.setattr(teleop.gateway, "teleop_acquire", acquire)
    with hardware_client.websocket_connect(PATH, headers=HEADERS) as socket:
        assert socket.receive_json()["code"] == "gateway_protocol_error"
        assert teleop.released.wait(1)
    assert not teleop.commands


@pytest.mark.parametrize("disconnected", [False, True])
def test_hardware_does_not_dispatch_queued_motion_after_expiry_or_disconnect(
        hardware_client, teleop, monkeypatch, disconnected):
    first_started = Event()
    finish_first = Event()
    input_closed = Event()
    receive_command = teleop_module.receive_command

    def command(*args, **kwargs):
        if args[1] == 1:
            first_started.set()
            assert finish_first.wait(2)
        return teleop.command(*args, **kwargs)

    async def receive(*args):
        result = await receive_command(*args)
        if result is None:
            input_closed.set()
        return result

    monkeypatch.setattr(teleop.gateway, "teleop_command", command)
    monkeypatch.setattr(teleop_module, "receive_command", receive)
    with hardware_client.websocket_connect(PATH, headers=HEADERS) as socket:
        try:
            assert socket.receive_json()["code"] == "acquired"
            socket.send_json({"action": "forward", "sequence": 1, "sent_at_ms": now_ms()})
            assert first_started.wait(1)
            socket.send_json({"action": "forward", "sequence": 2, "sent_at_ms": now_ms()})
            socket.send_json({"action": "turn_left", "sequence": 3, "sent_at_ms": now_ms()})
            assert socket.receive_json()["code"] == "superseded"

            if disconnected:
                socket.close()
                assert input_closed.wait(1)
                finish_first.set()
                assert teleop.released.wait(1)
            else:
                time.sleep((teleop_module.COMMAND_TTL_MS + 10) / 1000)
                finish_first.set()
                assert socket.receive_json()["code"] == "applied"
                expired = socket.receive_json()
                assert (expired["code"], expired["sequence"]) == ("stale_command", 3)
                assert send(socket, "release", 4)["code"] == "released"
        finally:
            finish_first.set()
    assert [item[1] for item in teleop.commands] == [1]
    assert teleop.releases == [PRIVATE_TOKEN]


@pytest.mark.parametrize("code", ["hardware_unavailable", "robot_not_ready", "sdk_rejected", "stop_unconfirmed"])
def test_hardware_failure_closes_session_and_releases_control(hardware_client, teleop, code):
    teleop.command_failure = code
    with hardware_client.websocket_connect(PATH, headers=HEADERS) as socket:
        assert socket.receive_json()["accepted"]
        failure = send(socket, "stop" if code == "stop_unconfirmed" else "forward", 1)
        assert failure["code"] == code and not failure["accepted"]
        assert PRIVATE_TOKEN not in str(failure)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1008
        assert teleop.released.wait(1)
    assert teleop.releases == [PRIVATE_TOKEN]


def test_hardware_disables_inspection_and_pending_replay(hardware_client, teleop, payload):
    service = hardware_client.app.state.service
    with service.sessions.begin() as session:
        session.add(Task(id="existing-task", request_id="original", request_hash="hash", name="Original task", points=[]))
        session.flush()
        session.add(Outbox(id="existing-command", task_id="existing-task", expires_at_ms=now_ms() + 30000))
    # Even if a hardware-configured API is pointed at a simulator, it must
    # neither accept new inspection actions nor replay its old outbox.
    teleop.simulated = True
    assert hardware_client.get("/api/robot").status_code == 503
    assert hardware_client.get("/api/health").json()["gateway"] == "unavailable"
    assert hardware_client.post("/api/tasks", json=payload).status_code == 409
    assert hardware_client.post("/api/tasks/existing-task/stop").status_code == 409
    service.recover_outbox()
    service.dispatch("existing-task")
    assert hardware_client.get("/api/tasks/existing-task").json()["status"] == "pending_dispatch"
    assert hardware_client.get("/api/tasks").json()[0]["id"] == "existing-task"
    with service.sessions() as session:
        command = session.get(Outbox, "existing-command")
        assert command.status == "pending" and command.attempts == 0
    assert teleop.gateway.starts == 0


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
