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
    monkeypatch.setattr(demo_module, "STALL_SECONDS", 2.0)   # 测试收紧停滞窗口
    monkeypatch.setattr(demo_module, "FRAME_INTERVAL", 0.02)
    # 时间主控模型速度/角速度与替身网关积分参数对齐（0.2 m/s、0.3 rad/s），
    # 主段即精确到位；停留与唤醒的等待压缩到测试节奏。
    monkeypatch.setattr(demo_module, "SIM_WALK_SPEED_MPS", 0.2)
    monkeypatch.setattr(demo_module, "CUT_EARLY_RAD", math.radians(2))
    monkeypatch.setattr(demo_module, "ACCEPT_TOLERANCE_RAD", math.radians(6))
    monkeypatch.setattr(demo_module, "SETTLE_AFTER_STEP", 0.02)
    monkeypatch.setattr(demo_module, "SETTLE_AFTER_BURST", 0.02)
    monkeypatch.setattr(demo_module, "HEARTBEAT_INTERVAL", 0.02)
    monkeypatch.setattr(demo_module, "PHRASE_GAP_S", 0.02)


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
        self.drift_yaw_rate = 0.0  # 前进时的航向漂移率（2026-11-23 回正测试注入）

    def _advance(self, now: float) -> None:
        if self.action in self.MOTION_ACTIONS:
            elapsed = max(0.0, min(now, self.motion_until) - self.action_since)
            if self.action in ("forward", "forward_slow"):
                self.x += self.speed * elapsed
                self.yaw += self.drift_yaw_rate * elapsed   # 模拟真机前进步漂航向
            elif self.action in ("backward", "backward_slow"):
                self.x -= self.speed * elapsed
                self.yaw -= self.drift_yaw_rate * elapsed
            elif self.action in ("turn_left", "turn_left_slow"):
                self.yaw += self.turn_speed * elapsed
            else:
                self.yaw -= self.turn_speed * elapsed
            if now >= self.motion_until:
                self.action = "stop"       # 帧过期自动零速（真实网关的 _expire 行为）
        self.action_since = now

    def get_state(self):
        self._advance(time.monotonic())
        if not self.pose_available:
            pose = None
        else:
            # speed 与真机 Pose.speed 对齐：帧窗内动作活跃即视为在走
            moving = self.action in self.MOTION_ACTIONS
            pose = {"x": self.x, "y": 0.0, "yaw": self.yaw,
                    "speed": self.speed if moving else 0.0}
        # 状态自报（2026-11-24）所需字段与真机 GetState 对齐
        return {"teleop_ready": True, "pose": pose, "simulated": True,
                "fsm_id": 601, "battery_percent": 88.0,
                "temperature_c": 22.5, "humidity_percent": 41.0}

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
    # 时间主控：模型速度与替身一致 → 0.3m 精确到位（0.3/0.2=1.5s 帧流）
    assert motion_gateway.x == pytest.approx(0.3, abs=0.05)
    assert "forward" in motion_gateway.actions
    assert "stop" in motion_gateway.actions


def test_move_degrades_to_time_only_without_fastlio(settings, gateway, monkeypatch,
                                                    motion_gateway, fast_timing, field_stub):
    """真机模式 FastLIO 全程缺失（速度可用）：时间主控照常走完，不中止。

    替身网关以 0.2m/s 积分而硬件模型按 0.5m/s 预算——精修也被跳过
    （FastLIO 缺失），验证的就是「开环走完不中止」而非停点精度。"""
    application = hardware_client(settings, gateway)
    application.state.demo._make_client = lambda target, timeout: motion_gateway
    monkeypatch.setattr(field, "fastlio_pose", lambda timeout=1.0: None)

    with TestClient(application) as client:
        body = {"name": "降级", "steps": [{"type": "forward", "value": 0.4}]}
        route_id = client.post("/api/demo/routes", json=body).json()["id"]
        client.post(f"/api/demo/routes/{route_id}/run")
        state = wait_phase(client, "done")
        assert state["step_index"] == 1
        assert 0.1 < motion_gateway.x < 0.4   # 走了但未按模型走满（降级语义）
        assert motion_gateway.released


def test_move_trim_corrects_model_error(client, motion_gateway, fast_timing, monkeypatch):
    """模型速度与替身失配（欠走）时，精修段按 FastLIO 停点误差补走。"""
    monkeypatch.setattr(demo_module, "SIM_WALK_SPEED_MPS", 0.4)  # 模型快于替身 0.2 → 欠走
    body = {"name": "精修", "steps": [{"type": "forward", "value": 0.4}]}
    route_id = client.post("/api/demo/routes", json=body).json()["id"]
    client.post(f"/api/demo/routes/{route_id}/run")
    state = wait_phase(client, "done")
    assert state["step_index"] == 1
    # 主段 0.4/0.4=1.0s 只走 0.2m；精修按停点误差补走，收敛到精修容差内
    # （2 倍模型误差下两轮精修后残差 0.1m ≤ TRIM_TOLERANCE_M=0.15）
    assert motion_gateway.x == pytest.approx(0.4, abs=demo_module.TRIM_TOLERANCE_M)
    assert "forward_slow" in motion_gateway.actions


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


def test_forward_drift_realigned_to_nominal(client, motion_gateway, fast_timing):
    """前进步漂航向时：转向步闭环到名义航向、前进步前自动回正（2026-11-23）。

    注入漂移率 -0.2rad/s（0.3m/0.2m/s=1.5s 前进 → 每步漂约 -0.3rad）。
    两次右/左转各 90°（绝对目标）把第一次前进的漂移吃掉；回正在第二次
    前进前把残留清零。终态只剩最后一步前进的新鲜漂移（-0.3）——
    关键断言：漂移没有累积（无簿记时终态应为两步之和 -0.6）。"""
    motion_gateway.drift_yaw_rate = -0.2
    body = {"name": "回正路线", "steps": [{"type": "forward", "value": 0.3},
                                          {"type": "turn", "value": -90},
                                          {"type": "wait", "value": 0.5},
                                          {"type": "turn", "value": 90},
                                          {"type": "forward", "value": 0.3}]}
    route_id = client.post("/api/demo/routes", json=body).json()["id"]
    client.post(f"/api/demo/routes/{route_id}/run")
    state = wait_phase(client, "done")
    assert state["step_index"] == 5
    # 终态航向 = 仅最后一步的新鲜漂移（-0.2×1.5s），漂移未累积
    assert motion_gateway.yaw == pytest.approx(-0.3, abs=math.radians(5))
    assert motion_gateway.x == pytest.approx(0.6, abs=0.1)


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
    monkeypatch.setattr(field, "ensure_usb_audio_power", lambda: None)
    monkeypatch.setattr(field, "voice_ready", lambda: True)
    monkeypatch.setattr(field, "say", lambda text: stub.says.append(text) or True)
    # 会话化口令接口（v3）：默认无事件、会话固定
    monkeypatch.setattr(field, "asr_arm_session",
                        lambda: {"ok": True, "session_id": "sess-1", "boot_id": "boot-1"})
    monkeypatch.setattr(field, "asr_disarm_session", lambda: None)
    monkeypatch.setattr(field, "asr_commands",
                        lambda since=0: {"items": [], "last_seq": 0, "boot_id": "boot-1",
                                         "session": {"id": "sess-1", "consumed": False}})
    monkeypatch.setattr(field, "set_data_valve",
                        lambda name, enabled: {"ok": True, "enabled": True})
    # 演示旁白期的建图栈自动重启（2026-11-24）：测试里桩掉 SSH
    monkeypatch.setattr(field, "restart_mapping_stack",
                        lambda: {"ok": True})
    # 就绪检查项（两段式准备）：全绿
    monkeypatch.setattr(field, "asr_health",
                        lambda: {"model_ready": True, "capturing": True, "mic_recent": True,
                                 "inference": {"fault": False}})
    monkeypatch.setattr(field, "vision_frame_ready", lambda: True)
    # 位姿跟随替身网关的积分结果（硬件模式执行器从 fastlio_pose 读反馈）
    # imu_yaw 模拟桥的陀螺积分：原生日志号右转为正（与 gw.yaw 左正相反）
    monkeypatch.setattr(field, "fastlio_pose",
                        lambda timeout=1.0: {"x": motion_gateway.x, "y": 0.0, "z": 0.0,
                                             "yaw": motion_gateway.yaw, "age_ms": 10,
                                             "imu_yaw": -motion_gateway.yaw,
                                             "imu_valid": True, "imu_age_ms": 40})
    monkeypatch.setattr(field, "DATA_VALVES", {"vision": True, "mapping": True, "asr": True})
    return stub


def test_demo_voice_flow_greeting_trigger_completion(settings, gateway, monkeypatch,
                                                     motion_gateway, fast_timing, field_stub):
    """完整语音链：问候语 → 口令 → 确认 → 执行 → 完成播报。"""
    application = hardware_client(settings, gateway)
    # 执行器仍用替身（运动通道不在本测试范围）
    application.state.demo._make_client = lambda target, timeout: motion_gateway
    # 口令在武装后出现：会话事件由识别服务完成整句判定后下发
    command = threading.Event()

    def asr_commands(since=0):
        items = [{"seq": 1, "boot_id": "boot-1", "session_id": "sess-1",
                  "text": "开始", "age_ms": 800}] if command.is_set() else []
        return {"items": [item for item in items if item["seq"] > since],
                "last_seq": items[-1]["seq"] if items else 0, "boot_id": "boot-1",
                "session": {"id": "sess-1", "consumed": False}}
    monkeypatch.setattr(field, "asr_commands", asr_commands)

    with TestClient(application) as client:
        routes = client.get("/api/demo/routes").json()
        single = next(route for route in routes if route["name"] == "单步测试")
        client.post("/api/demo/prepare")
        wait_phase(client, "prepared")
        assert client.post("/api/demo/start", json={"route_id": single["id"]}).json()["ok"]
        wait_phase(client, "armed")
        command.set()
        state = wait_phase(client, "done")
        assert state["step_index"] == 1
        # 话术顺序（2026-11-24）：准备就绪 → 问候 → 状态自报（通信/调度/
        # 电量/运动/温湿度/安全）→ 雷达旁白×2 → 图像旁白×2 → 巡检开始 →
        # 完成（单步路线无任务，终态即 DONE 本体）
        assert field_stub.says == [demo_module.PREPARED_TEXT, demo_module.GREETING,
                                   demo_module.VOICE_OK_TEXT, demo_module.CONSOLE_OK_TEXT,
                                   "机器人当前电量88%。",
                                   demo_module.MOTION_OK_TEXT,
                                   "当前环境温度22度，湿度41%。",
                                   demo_module.SAFE_TEXT,
                                   demo_module.NARRATE_MAPPING, demo_module.MAPPING_READY,
                                   demo_module.NARRATE_VISION, demo_module.VISION_READY,
                                   demo_module.DEMO_GO, demo_module.DONE]
        assert motion_gateway.released


def test_demo_armed_timeout_auto_continues(settings, gateway, monkeypatch,
                                           motion_gateway, fast_timing, field_stub):
    """武装 5 秒未闻「开始」自动继续（2026-11-24）：口令从必需变可选。"""
    application = hardware_client(settings, gateway)
    application.state.demo._make_client = lambda target, timeout: motion_gateway
    monkeypatch.setattr(field, "asr_commands",
                        lambda since=0: {"items": [], "last_seq": 0, "boot_id": "boot-1",
                                         "session": {"id": "sess-1", "consumed": False}})
    monkeypatch.setattr(demo_module, "ARMED_AUTO_CONTINUE_S", 0.3)

    with TestClient(application) as client:
        routes = client.get("/api/demo/routes").json()
        single = next(route for route in routes if route["name"] == "单步测试")
        client.post("/api/demo/prepare")
        wait_phase(client, "prepared")
        client.post("/api/demo/start", json={"route_id": single["id"]})
        # 不设口令事件：超时后自动走完
        state = wait_phase(client, "done", timeout=30)
        assert state["step_index"] == 1
        assert demo_module.SAFE_TEXT in field_stub.says
        assert demo_module.DEMO_GO in field_stub.says
        assert motion_gateway.released


def test_demo_history_text_does_not_trigger(settings, gateway, monkeypatch,
                                            motion_gateway, fast_timing, field_stub):
    """武装前的历史转写（含「开始」）不触发运动：基线之后才算口令。"""
    application = hardware_client(settings, gateway)
    application.state.demo._make_client = lambda target, timeout: motion_gateway

    # 历史文本不再参与触发（整句判定在识别服务，上位机只收会话事件）
    monkeypatch.setattr(field, "asr_commands",
                        lambda since=0: {"items": [], "last_seq": 0, "boot_id": "boot-1",
                                         "session": {"id": "sess-1", "consumed": False}})

    with TestClient(application) as client:
        routes = client.get("/api/demo/routes").json()
        single = next(route for route in routes if route["name"] == "单步测试")
        client.post("/api/demo/prepare")
        wait_phase(client, "prepared")
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


def test_demo_hardware_turn_uses_gateway_yaw_feedback(settings, gateway, monkeypatch,
                                                       motion_gateway, fast_timing, field_stub):
    """转向闭环首选网关姿态角（gRPC Pose.yaw，2026-11-23 三源实测结论）。

    桥的 IMU 字段即使存在也不参与（本测试的 fastlio_pose 桩无 imu 字段），
    网关替身的 yaw 积分即反馈源，闭环收敛到 ±6°。"""
    application = hardware_client(settings, gateway)
    application.state.demo._make_client = lambda target, timeout: motion_gateway
    monkeypatch.setattr(field, "fastlio_pose",
                        lambda timeout=1.0: {"x": motion_gateway.x, "y": 0.0, "z": 0.0,
                                             "yaw": motion_gateway.yaw, "age_ms": 10})
    with TestClient(application) as client:
        body = {"name": "网关姿态转向", "steps": [{"type": "turn", "value": 90}]}
        route_id = client.post("/api/demo/routes", json=body).json()["id"]
        client.post(f"/api/demo/routes/{route_id}/run")
        state = wait_phase(client, "done")
        assert state["step_index"] == 1
        assert motion_gateway.yaw == pytest.approx(math.pi / 2, abs=math.radians(6))
        assert "turn_left_slow" in motion_gateway.actions


def test_demo_hardware_turn_requires_yaw_feedback(settings, gateway, monkeypatch,
                                                  motion_gateway, fast_timing, field_stub):
    """网关姿态与桥 IMU 均缺失时中止（不回退到快转不可信的扫描 yaw）。"""
    application = hardware_client(settings, gateway)
    application.state.demo._make_client = lambda target, timeout: motion_gateway
    motion_gateway.pose_available = False    # 网关姿态缺失（速度一并缺失）
    monkeypatch.setattr(field, "fastlio_pose",
                        lambda timeout=1.0: {"x": motion_gateway.x, "y": 0.0, "z": 0.0,
                                             "yaw": motion_gateway.yaw, "age_ms": 10})
    with TestClient(application) as client:
        body = {"name": "无反馈转向", "steps": [{"type": "turn", "value": 90}]}
        route_id = client.post("/api/demo/routes", json=body).json()["id"]
        client.post(f"/api/demo/routes/{route_id}/run")
        state = wait_phase(client, "aborted")
        assert "转向反馈源不可用" in state["detail"]


def test_turn_burst_cap_aborts_on_frozen_yaw(settings, gateway, monkeypatch,
                                             motion_gateway, fast_timing, field_stub):
    """盲转保险②（728d7ef 行为恢复版）：反馈数值冻结（样本新鲜但不更新）
    时，单段时长上限逐段刹车，passes 耗尽后带诊断中止——绝不无限盲转。"""
    application = hardware_client(settings, gateway)
    application.state.demo._make_client = lambda target, timeout: motion_gateway
    # livox 源冻结：桥桩的 imu_yaw 恒为 0（样本新鲜、数值不动）
    monkeypatch.setattr(field, "fastlio_pose", lambda timeout=1.0: {
        "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0, "age_ms": 10,
        "imu_yaw": 0.0, "imu_valid": True, "imu_age_ms": 40})
    monkeypatch.setattr(demo_module, "TURN_RATE_RADPS", 50.0)   # 单段上限≈瞬时

    with TestClient(application) as client:
        body = {"name": "冻结反馈", "steps": [{"type": "turn", "value": 90}]}
        route_id = client.post("/api/demo/routes", json=body).json()["id"]
        client.post(f"/api/demo/routes/{route_id}/run")
        state = wait_phase(client, "aborted", timeout=30)
        assert "单段超时" in state["detail"]


def test_demo_two_stage_prepare_then_start(settings, gateway, monkeypatch,
                                            motion_gateway, fast_timing, field_stub):
    """两段式：未准备点演示被拒；准备全绿→播报就绪；六项检查可见。"""
    application = hardware_client(settings, gateway)
    application.state.demo._make_client = lambda target, timeout: motion_gateway
    with TestClient(application) as client:
        routes = client.get("/api/demo/routes").json()
        single = next(route for route in routes if route["name"] == "单步测试")
        denied = client.post("/api/demo/start", json={"route_id": single["id"]})
        assert denied.status_code == 409
        assert "巡检准备" in denied.json()["detail"]
        assert client.post("/api/demo/prepare").json()["ok"]
        state = wait_phase(client, "prepared")
        # 准备阶段只查语音两项；其余项在演示旁白中逐个点亮
        assert all(state["checks"].values()) and len(state["checks"]) == 2
        assert demo_module.PREPARED_TEXT in field_stub.says


def test_demo_prepare_reports_failure_item(settings, gateway, monkeypatch,
                                            motion_gateway, fast_timing, field_stub):
    """准备阶段检查不绿：麦克风无数据 → 超时进入准备失败并指明检查项。"""
    monkeypatch.setattr(demo_module, "PREPARE_TIMEOUT_S", 6.0)
    monkeypatch.setattr(field, "asr_health",
                        lambda: {"model_ready": True, "capturing": True,
                                 "mic_recent": False, "inference": {"fault": False}})
    monkeypatch.setattr(field, "reset_pc3_mic", lambda: {"ok": True})
    application = hardware_client(settings, gateway)
    with TestClient(application) as client:
        assert client.post("/api/demo/prepare").json()["ok"]
        state = wait_phase(client, "prep_failed", timeout=30)
        assert "准备超时" in state["detail"]


def test_demo_start_rejected_in_simulation(client):
    """模拟模式无语音链路：演示开关明确拒绝，引导使用手动执行。"""
    routes = client.get("/api/demo/routes").json()
    single = next(route for route in routes if route["name"] == "单步测试")
    reply = client.post("/api/demo/start", json={"route_id": single["id"]})
    assert reply.status_code == 409
    assert "手动执行" in reply.json()["detail"]
