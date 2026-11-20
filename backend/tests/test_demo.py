"""演示编排与序列执行器的单元测试。

执行器用行为替身驱动：FakeMotionGateway 按时间把指令动作积分成位姿，
模拟「指令在发、机器人真的在走」的闭环；现场链路（语音/阀门/位姿 HTTP）
全部用 monkeypatch 替换。真实 gRPC 通道的端到端见模拟干跑与现场验收。
"""

import math
import threading
import time

from fastapi.testclient import TestClient
import pytest

from h2_backend import demo as demo_module
from h2_backend import field


def wait_phase(client, phase, timeout=20.0):
    """轮询演示状态直到进入期望相位（done/aborted 等终态立即返回）。"""
    deadline = time.monotonic() + timeout
    state = None
    while time.monotonic() < deadline:
        state = client.get("/api/demo/state").json()
        if state["phase"] == phase:
            return state
        if state["phase"] in {"done", "aborted"}:
            return state
        time.sleep(0.05)
    pytest.fail(f"演示状态超时：期望 {phase}，实际 {state}")


@pytest.fixture
def fast_timing(monkeypatch):
    """压缩执行器的真实等待，测试只验证行为不验证墙钟。"""
    monkeypatch.setattr(demo_module, "FRAME_INTERVAL", 0.02)
    monkeypatch.setattr(demo_module, "SETTLE_AFTER_BURST", 0.02)
    # 替身网关停步无惯性（不会像真机那样被停距带入接受窗），
    # 压缩提前量与接受窗让闭环分支在测试里同样收敛。
    monkeypatch.setattr(demo_module, "CUT_EARLY_M", 0.04)
    monkeypatch.setattr(demo_module, "CUT_EARLY_RAD", math.radians(2))
    monkeypatch.setattr(demo_module, "ACCEPT_TOLERANCE_M", 0.1)
    monkeypatch.setattr(demo_module, "ACCEPT_TOLERANCE_RAD", math.radians(6))
    monkeypatch.setattr(demo_module, "SETTLE_AFTER_STEP", 0.02)
    monkeypatch.setattr(demo_module, "HEARTBEAT_INTERVAL", 0.02)


class FakeMotionGateway:
    """teleop 替身：与真实网关同语义——运动帧只在 250ms 帧窗内生效，
    帧过期自动回到 stop，get_state 按经过时间把动作积分成位姿。"""

    FRAME_SECONDS = 0.25
    MOTION_ACTIONS = frozenset({"forward", "forward_slow", "backward", "backward_slow",
                                "turn_left", "turn_left_slow", "turn_right", "turn_right_slow"})

    def __init__(self):
        self.token = ""
        self.sequence = 0
        self.action = "stop"
        self.action_since = time.monotonic()
        self.motion_until = 0.0
        self.speed = 0.2           # 巡航与慢速都按此速度积分（测试只关心收敛）
        self.turn_speed = 0.3
        self.busy = False
        self.pose_available = True
        self.released = False
        self.actions: list[str] = []
        self.x = 0.0
        self.yaw = 0.0

    def _advance(self, now: float) -> None:
        if self.action in self.MOTION_ACTIONS:
            elapsed = max(0.0, min(now, self.motion_until) - self.action_since)
            if self.action in ("forward", "forward_slow"):
                self.x += self.speed * elapsed
            elif self.action in ("backward", "backward_slow"):
                self.x -= self.speed * elapsed
            elif self.action in ("turn_left", "turn_left_slow"):
                self.yaw += self.turn_speed * elapsed
            else:
                self.yaw -= self.turn_speed * elapsed
            if now >= self.motion_until:
                self.action = "stop"       # 帧过期自动零速（真实网关的 _expire 行为）
        self.action_since = now

    def get_state(self):
        self._advance(time.monotonic())
        pose = None if not self.pose_available else {"x": self.x, "y": 0.0, "yaw": self.yaw}
        return {"teleop_ready": True, "pose": pose, "simulated": True}

    def teleop_acquire(self):
        if self.busy:
            return {"accepted": False, "code": "teleop_busy"}
        self.token = "fake-token"
        return {"accepted": True, "code": "acquired", "token": self.token, **self._clock()}

    def teleop_command(self, token, sequence, action, expires_at_ms=0, **_):
        if not self.token or token != self.token:
            return {"accepted": False, "code": "invalid_token"}
        if sequence <= self.sequence:
            return {"accepted": False, "code": "stale_sequence"}
        self.sequence = sequence
        self.actions.append(action)
        now = time.monotonic()
        self._advance(now)
        if action == "heartbeat":
            return {"accepted": True, "code": "renewed", **self._clock()}
        if action == "stop":
            self.action, self.motion_until = "stop", 0.0
        else:
            self.action = action
            self.motion_until = now + self.FRAME_SECONDS
        return {"accepted": True, "code": "applied", **self._clock()}

    def teleop_release(self, token):
        if not self.token or token != self.token:
            return {"accepted": False, "code": "invalid_token"}
        self.action = "stop"
        self.token = ""
        self.released = True
        return {"accepted": True, "code": "released"}

    def close(self):
        self.action = "stop"

    @staticmethod
    def _clock() -> dict:
        # 与真实硬件网关一致：每条接受回执都带单调钟样本，供 GatewayClock 换算
        return {"gateway_monotonic_ms": time.monotonic_ns() // 1_000_000,
                "received_monotonic_ns": time.monotonic_ns()}

    def teleop_release(self, token):
        if not self.token or token != self.token:
            return {"accepted": False, "code": "invalid_token"}
        self.action = "stop"
        self.token = ""
        self.released = True
        return {"accepted": True, "code": "released"}

    def close(self):
        self.action = "stop"


@pytest.fixture
def motion_gateway(app):
    gateway = FakeMotionGateway()
    app.state.demo._make_client = lambda target, timeout: gateway
    return gateway


# ---------- 路线 CRUD ----------

def test_seeded_routes_available(client):
    routes = client.get("/api/demo/routes").json()
    names = {route["name"] for route in routes}
    assert {"单步测试", "S形全流程"} <= names
    single = next(route for route in routes if route["name"] == "单步测试")
    assert single["steps"] == [{"type": "forward", "value": 0.3}]


def test_route_crud_validation(client):
    body = {"name": "测试路线", "steps": [{"type": "turn", "value": 45}]}
    created = client.post("/api/demo/routes", json=body)
    assert created.status_code == 201
    route_id = created.json()["id"]

    # 距离/角度/时长越界与非法组合都要 422
    for steps in (
        [{"type": "forward", "value": 0.01}],
        [{"type": "forward", "value": 9}],
        [{"type": "turn", "value": 0}],
        [{"type": "turn", "value": 270}],
        [{"type": "wait", "value": 0.1}],
        [{"type": "wait", "value": 100}],
        [{"type": "forward", "value": 1.0, "read_aloud": True}],
    ):
        assert client.post("/api/demo/routes", json={"name": "x", "steps": steps}).status_code == 422

    assert client.post("/api/demo/routes", json=body).status_code == 409
    assert client.delete(f"/api/demo/routes/{route_id}").json()["ok"] is True
    assert client.delete(f"/api/demo/routes/{route_id}").status_code == 404


# ---------- 执行器（模拟模式干跑路径） ----------

def test_manual_run_completes_single_step(client, motion_gateway, fast_timing):
    routes = client.get("/api/demo/routes").json()
    single = next(route for route in routes if route["name"] == "单步测试")
    reply = client.post(f"/api/demo/routes/{single['id']}/run")
    assert reply.json()["ok"] is True
    state = wait_phase(client, "done")
    assert state["step_index"] == 1
    assert motion_gateway.released is True
    # 0.3m 目标、±5cm 容差
    assert motion_gateway.x == pytest.approx(0.3, abs=0.05)
    assert "forward_slow" in motion_gateway.actions
    assert "stop" in motion_gateway.actions


def test_full_route_turn_and_wait(client, motion_gateway, fast_timing):
    body = {"name": "转弯路线", "steps": [{"type": "forward", "value": 0.3},
                                          {"type": "turn", "value": 90},
                                          {"type": "wait", "value": 0.5}]}
    route_id = client.post("/api/demo/routes", json=body).json()["id"]
    client.post(f"/api/demo/routes/{route_id}/run")
    state = wait_phase(client, "done")
    assert state["steps_total"] == 3 and state["step_index"] == 3
    assert motion_gateway.x == pytest.approx(0.3, abs=0.05)
    assert motion_gateway.yaw == pytest.approx(math.pi / 2, abs=math.radians(5))
    assert "heartbeat" in motion_gateway.actions


def test_stop_mid_run_halts_and_releases(client, motion_gateway, fast_timing):
    body = {"name": "长途", "steps": [{"type": "forward", "value": 5.0}]}
    route_id = client.post("/api/demo/routes", json=body).json()["id"]
    client.post(f"/api/demo/routes/{route_id}/run")
    wait_phase(client, "running")
    client.post("/api/demo/stop")
    state = wait_phase(client, "aborted")
    assert "停止" in state["detail"]
    assert motion_gateway.action == "stop" or motion_gateway.released
    assert motion_gateway.x < 1.0            # 没有走完全程


def test_stall_below_walk_threshold_aborts(client, motion_gateway, fast_timing):
    motion_gateway.speed = 0.0               # 指令在发，位姿不动（低于阈值）
    routes = client.get("/api/demo/routes").json()
    single = next(route for route in routes if route["name"] == "单步测试")
    client.post(f"/api/demo/routes/{single['id']}/run")
    state = wait_phase(client, "aborted")
    assert "行走阈值" in state["detail"]


def test_pose_loss_aborts(client, motion_gateway, fast_timing):
    motion_gateway.pose_available = False
    routes = client.get("/api/demo/routes").json()
    single = next(route for route in routes if route["name"] == "单步测试")
    client.post(f"/api/demo/routes/{single['id']}/run")
    state = wait_phase(client, "aborted")
    assert "位姿丢失" in state["detail"]


def test_manual_teleop_busy_rejected(client, motion_gateway, fast_timing):
    motion_gateway.busy = True
    routes = client.get("/api/demo/routes").json()
    single = next(route for route in routes if route["name"] == "单步测试")
    client.post(f"/api/demo/routes/{single['id']}/run")
    state = wait_phase(client, "aborted")
    assert "手动遥控占用" in state["detail"]


# ---------- 演示编排（真机模式话术链） ----------

class FieldStub:
    def __init__(self):
        self.says: list[str] = []
        self.poses: list[dict] = []


def hardware_client(settings, gateway):
    """构造真机模式应用（演示编排仅在真机模式注册语音路径）。"""
    from dataclasses import replace
    from h2_backend.app import create_app
    return create_app(
        replace(settings, hardware_teleop=True,
                teleop_gateway_address="127.0.0.1:50052"), gateway)


@pytest.fixture
def field_stub(monkeypatch, motion_gateway):
    stub = FieldStub()

    monkeypatch.setattr(field, "start_pc3_voice", lambda: {"ok": True, "already": False})
    monkeypatch.setattr(field, "say", lambda text: stub.says.append(text) or True)
    # _say_quiet 播报前后的采音开关走桩，不触真机
    monkeypatch.setattr(field, "_asr_post", lambda path: {"ok": True})
    monkeypatch.setattr(field, "set_data_valve",
                        lambda name, enabled: {"ok": True, "enabled": True})
    # 位姿跟随替身网关的积分结果（硬件模式执行器从 fastlio_pose 读反馈）
    # imu_yaw 模拟桥的陀螺积分：原生日志号右转为正（与 gw.yaw 左正相反）
    monkeypatch.setattr(field, "fastlio_pose",
                        lambda timeout=1.0: {"x": motion_gateway.x, "y": 0.0, "z": 0.0,
                                             "yaw": motion_gateway.yaw, "age_ms": 10,
                                             "imu_yaw": -motion_gateway.yaw})
    monkeypatch.setattr(field, "asr_text", lambda since=0: {"items": [], "last_id": 0})
    monkeypatch.setattr(field, "DATA_VALVES", {"vision": True, "mapping": True, "asr": True})
    return stub


def test_demo_voice_flow_greeting_trigger_completion(settings, gateway, monkeypatch,
                                                     motion_gateway, fast_timing, field_stub):
    """完整语音链：问候语 → 口令 → 确认 → 执行 → 完成播报。"""
    application = hardware_client(settings, gateway)
    # 执行器仍用替身（运动通道不在本测试范围）
    application.state.demo._make_client = lambda target, timeout: motion_gateway
    # 口令在武装后才出现；基线调用（_watch_keyword 的第一次拉取）不返回文本，
    # 与真实语义一致：基线只取 last_id，历史一律作废
    command = threading.Event()
    seen_baseline = {"done": False}

    def asr_text(since=0):
        if not seen_baseline["done"]:
            seen_baseline["done"] = True
            return {"items": [], "last_id": 0}
        items = [{"id": 2, "ts": time.time(), "text": "现在开始巡检"}] if command.is_set() else []
        return {"items": [item for item in items if item["id"] > since],
                "last_id": items[-1]["id"] if items else 0}
    monkeypatch.setattr(field, "asr_text", asr_text)

    with TestClient(application) as client:
        routes = client.get("/api/demo/routes").json()
        single = next(route for route in routes if route["name"] == "单步测试")
        assert client.post("/api/demo/start", json={"route_id": single["id"]}).json()["ok"]
        wait_phase(client, "armed")
        command.set()
        state = wait_phase(client, "done")
        assert state["step_index"] == 1
        # 话术顺序：问候 → 确认 → 完成；执行中的中断播报不应出现
        assert field_stub.says == [demo_module.GREETING, demo_module.CONFIRM, demo_module.DONE]
        assert motion_gateway.released


def test_demo_history_text_does_not_trigger(settings, gateway, monkeypatch,
                                            motion_gateway, fast_timing, field_stub):
    """武装前的历史转写（含「开始」）不触发运动：基线之后才算口令。"""
    application = hardware_client(settings, gateway)
    application.state.demo._make_client = lambda target, timeout: motion_gateway

    def asr_text(since=0):
        # 转写桩按 since 过滤，与真实服务一致；恒定只有一条历史记录
        items = [{"id": 1, "ts": time.time(), "text": "开始吃饭了"}]
        return {"items": [item for item in items if item["id"] > since], "last_id": 1}
    monkeypatch.setattr(field, "asr_text", asr_text)

    with TestClient(application) as client:
        routes = client.get("/api/demo/routes").json()
        single = next(route for route in routes if route["name"] == "单步测试")
        client.post("/api/demo/start", json={"route_id": single["id"]})
        wait_phase(client, "armed")
        time.sleep(1.0)                      # 历史文本在场，但不应触发
        state = client.get("/api/demo/state").json()
        assert state["phase"] == "armed"
        assert motion_gateway.actions == []
        client.post("/api/demo/stop")
        wait_phase(client, "aborted")


def test_demo_mapping_valve_closed_aborts(settings, gateway, monkeypatch,
                                          motion_gateway, fast_timing, field_stub):
    application = hardware_client(settings, gateway)
    application.state.demo._make_client = lambda target, timeout: motion_gateway
    monkeypatch.setattr(field, "DATA_VALVES", {"vision": True, "mapping": False, "asr": True})

    with TestClient(application) as client:
        routes = client.get("/api/demo/routes").json()
        single = next(route for route in routes if route["name"] == "单步测试")
        client.post(f"/api/demo/routes/{single['id']}/run")
        state = wait_phase(client, "aborted")
        assert "雷达数据接入被关闭" in state["detail"]


def test_demo_hardware_turn_uses_imu_feedback(settings, gateway, monkeypatch,
                                              motion_gateway, fast_timing, field_stub):
    """转向闭环走 IMU 陀螺积分（桥提供 imu_yaw 时），扫描 yaw 不参与判定。"""
    application = hardware_client(settings, gateway)
    application.state.demo._make_client = lambda target, timeout: motion_gateway
    with TestClient(application) as client:
        body = {"name": "IMU转向", "steps": [{"type": "turn", "value": 90}]}
        route_id = client.post("/api/demo/routes", json=body).json()["id"]
        client.post(f"/api/demo/routes/{route_id}/run")
        state = wait_phase(client, "done")
        assert state["step_index"] == 1
        assert motion_gateway.yaw == pytest.approx(math.pi / 2, abs=math.radians(6))
        assert "turn_left_slow" in motion_gateway.actions


def test_demo_start_rejected_in_simulation(client):
    """模拟模式无语音链路：演示开关明确拒绝，引导使用手动执行。"""
    routes = client.get("/api/demo/routes").json()
    single = next(route for route in routes if route["name"] == "单步测试")
    reply = client.post("/api/demo/start", json={"route_id": single["id"]})
    assert reply.status_code == 409
    assert "手动执行" in reply.json()["detail"]
