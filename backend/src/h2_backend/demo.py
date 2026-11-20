"""巡检演示编排：路线执行器与语音触发。

演示开关是全流程唯一需要人工点亮的武装点。打开后依次拉起现场服务并
让机器人播报问候语，随后进入「待命听令」，由关键词「开始」触发路线执行；
执行期间与手动遥控共用网关的单一运动租约（互斥天然成立），
停步/释放/断链的既有优先级全部继承，任何中止都让机器人停在原地等待人工。

路线执行器直接调用 GatewayClient 的 teleop_* 接口（与浏览器遥控同一条
通道），用 FastLIO 位姿（硬件）或网关模拟位姿（干跑）闭环：
- 编排层只描述 距离/角度/时长，速度是实现细节；
- 远处巡航、近处脉冲逼近（单帧后等帧过期自停再测量），精度不依赖慢速档；
- 指令在发而位姿不涨视为低于行走阈值，立即中止并提示调整 H2_V_* 环境变量。
"""

from __future__ import annotations

import math
import threading
import time
from contextlib import suppress
from typing import Any
from uuid import uuid4

from . import field
from .models import DemoRoute
from .service import ServiceError
from .teleop import GatewayClock

# 演示话术。问候语与提示语刻意不含「开始」二字，避免机器人自听触发；
# 确认语包含「开始」但此刻状态已是 running，再次命中会被状态机忽略。
GREETING = "我是中核四零四研发的数据中心巡检机器人，现在由我演示数据中心巡检功能，各位领导，请您指示！"
CONFIRM = "收到，现在开始巡检。"
NOT_READY = "设备准备中，请稍候。"
DONE = "巡检演示完毕。"
INTERRUPT = "巡检中断，等待人工处理。"
TRIGGER_KEYWORD = "开始"

# 预置路线：首次部署即可调试与演示（步骤结构与 RouteStepInput 一致）。
SEED_ROUTES = [
    {"name": "单步测试",
     "steps": [{"type": "forward", "value": 0.3}]},
    {"name": "S形全流程",
     "steps": [{"type": "forward", "value": 2.0},
               {"type": "turn", "value": -90},
               {"type": "wait", "value": 10, "read_aloud": True},
               {"type": "turn", "value": 90},
               {"type": "forward", "value": 2.0},
               {"type": "turn", "value": -90},
               {"type": "forward", "value": 2.0},
               {"type": "turn", "value": -90},
               {"type": "forward", "value": 2.0}]},
]

TTL_MS = 350                    # 与浏览器遥控一致的单帧有效期
FRAME_INTERVAL = 0.1            # 巡航段指令节奏
HEARTBEAT_INTERVAL = 0.5        # 停留期间维持租约（租约期限 2 秒）
SETTLE_AFTER_BURST = 0.7        # 提前停流后等步态停稳与位姿收敛的时间
SETTLE_AFTER_STEP = 1.5         # 每步停稳时间，再进入下一步或抓取识别结果
DIST_TOLERANCE_M = 0.05         # 直线步容差
YAW_TOLERANCE_RAD = math.radians(3)
# 硬件逼近策略：HybridWalk 需要持续指令流才提交迈步（2026-11-20 实测 0.5m/s 单帧
# 150ms 只引起重心调整不抬脚，回执正常），因此不做短帧脉冲，而是连续发帧、
# 剩余量小于提前量时停流，停稳测量后以小段流纠偏；步态最小粒度可能大于精调
# 容差，纠偏次数用尽后放宽到现场容差接受，避免在目标附近来回摆动。
# 2026-11-20 单步实测：位姿反馈延迟约 0.2–0.3s，0.5m/s 下的停距约 0.25m
# （前进 0.3m 停在 0.43m），提前量按实测停距设置。
CUT_EARLY_M = 0.25              # 剩余距离小于此值停止指令流
# 转向阈值实测在 0.55 附近临界（手动按住偶尔只扭腰不迈步），取 0.7 留余量；
# 0.7 rad/s 下反应超冲约 12–19°，提前量 15°、接受窗 20°。
CUT_EARLY_RAD = math.radians(15)
APPROACH_PASSES = 3             # 停流-测量-反向闭环的往返次数上限
ACCEPT_TOLERANCE_M = 0.2        # 接受窗（步态反应超冲决定的最小可达精度）
ACCEPT_TOLERANCE_RAD = math.radians(20)
CRUISE_REMAINING_M = 0.3        # 剩余距离小于此值切换到慢速动作（当前与巡航同速）
CRUISE_REMAINING_RAD = math.radians(10)
STALL_SECONDS = 3.0             # 指令持续发送而位姿不涨的判定窗口（含步态起步时间）
STALL_DISTANCE_M = 0.02
STALL_YAW_RAD = math.radians(1)
POSE_FAIL_LIMIT = 10            # 连续位姿拉取失败（约 1 秒）即中止
ACQUIRE_READY_TIMEOUT = 5.0     # 等待遥控就绪的时间
ASR_DOWN_LIMIT = 20             # 武装期 ASR 连续不可达约 30 秒即中止演示
# FastLIO 平移可靠，但快速原地旋转时其 yaw 滞后/滑移（2026-11-20 三源实测：
# 物理右转 174° 时 FastLIO 首段只报约 4 成、停稳后回补超报），转向闭环改用
# Mid360 内置 IMU 的陀螺积分（桥 /pose 的 imu_yaw，原生日志号实测右转为正）。
# 本模块统一左转为正，故乘 -1；若现场换机器反号则改回 +1。
IMU_YAW_SIGN = -1.0
# 动作映射：turn_left 应使左正角度增大（2026-11-20 实测一致；换机反号则改 False）。
TURN_LEFT_POSITIVE = True

# 阀门名 → 界面用语（拉起阶段的状态展示）
VALVE_LABELS = {"vision": "视觉", "mapping": "建图", "asr": "语音识别"}

_PHASES_BUSY = frozenset({"starting", "armed", "running"})


class DemoAbort(RuntimeError):
    """执行器内部的中止信号：消息面向操作员展示。"""


class _PoseTracker:
    """由连续位姿样本累计路程与解缠偏航。

    目标一律用「步开始时的累计量 + 步幅」表达，与世界系原点无关；
    之前步骤的抖动计入基线而不是本步增量。
    """

    def __init__(self) -> None:
        self.distance = 0.0
        self.yaw = 0.0
        self.imu_yaw: float | None = None   # IMU 陀螺积分（原生符号），缺失保持 None
        self._last: tuple[float, float, float] | None = None
        self._last_imu: float | None = None

    def update(self, x: float, y: float, yaw: float, imu_yaw: float | None = None) -> None:
        if self._last is not None:
            self.distance += math.hypot(x - self._last[0], y - self._last[1])
            self.yaw += math.remainder(yaw - self._last[2], 2 * math.pi)
        else:
            self.yaw = yaw
        self._last = (x, y, yaw)
        if imu_yaw is not None:
            if self._last_imu is not None:
                self.imu_yaw = (self.imu_yaw or 0.0) + math.remainder(imu_yaw - self._last_imu, 2 * math.pi)
            else:
                self.imu_yaw = 0.0
            self._last_imu = imu_yaw


class _PoseFeed(threading.Thread):
    """硬件模式的后台位姿线程：10Hz 拉取并累计路程/偏航。

    建图桥的 /pose 偶发被点云累计阻塞数百毫秒（见 h2_map_bridge 拆锁注释），
    运动循环若同步拉取，指令流会断到帧过期，步态控制器随之重置不迈步。
    线程内单写 tracker，运动线程只读最新累计量。
    """

    def __init__(self, source):
        super().__init__(daemon=True, name="h2-demo-pose")
        self._source = source
        self.tracker = _PoseTracker()
        self.fail = 0
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            pose = self._source()
            if pose is None:
                self.fail += 1
            else:
                self.fail = 0
                self.tracker.update(*pose)
            self._halt.wait(0.1)

    def poll(self) -> None:
        pass  # 后台线程自更新，运动循环无需做任何事

    def close(self) -> None:
        self._halt.set()
        self.join(timeout=1.0)


class _SyncPoseFeed:
    """模拟模式的同步位姿源：测量即时且确定，直接在循环内拉取。"""

    def __init__(self, client, service):
        self._client = client
        self._service = service
        self.tracker = _PoseTracker()
        self.fail = 0

    def poll(self) -> None:
        pose = self._service._pose(self._client)
        if pose is None:
            self.fail += 1
        else:
            self.fail = 0
            self.tracker.update(*pose)

    def close(self) -> None:
        pass


class DemoService:
    """演示状态机与路线执行器。所有远程动作都在后台线程中执行。"""

    def __init__(self, settings: Any, service: Any):
        self._settings = settings
        self._service = service                # 复用其数据库 engine 与建表
        self._hardware = settings.hardware_teleop
        self._make_client = self._default_client_factory   # 测试注入替身用
        self._lock = threading.Lock()
        self._phase = "idle"
        self._detail = ""
        self._error: str | None = None
        self._route: dict | None = None
        self._step_index = 0
        self._motion_started = False           # 只在真正发出过运动帧后播报中断
        self._abort = threading.Event()
        self._worker: threading.Thread | None = None

    # ---------- 状态 ----------

    def state(self) -> dict:
        with self._lock:
            steps_total = len(self._route["steps"]) if self._route else 0
            return {"phase": self._phase, "detail": self._detail, "error": self._error,
                    "route": dict(self._route) if self._route else None,
                    "step_index": self._step_index, "steps_total": steps_total,
                    "hardware": self._hardware}

    def _set_phase(self, phase: str, detail: str = "", error: str | None = None) -> None:
        with self._lock:
            self._phase = phase
            self._detail = detail
            self._error = error

    def _set_step(self, index: int) -> None:
        with self._lock:
            self._step_index = index

    def _busy_phase(self) -> str:
        with self._lock:
            return self._phase

    def _spawn(self, target, name: str) -> None:
        self._worker = threading.Thread(target=target, name=name, daemon=True)
        self._worker.start()

    def shutdown(self) -> None:
        # 应用退出时请求中止；执行线程是守护线程，不阻塞停机。
        self._abort.set()

    # ---------- 路线存取 ----------

    def initialize(self) -> None:
        """建表后注入预置路线（仅在表为空时）。"""
        with self._service.sessions() as session:
            if session.query(DemoRoute).count() == 0:
                for seed in SEED_ROUTES:
                    session.add(DemoRoute(id=str(uuid4()), name=seed["name"], steps=seed["steps"]))
                session.commit()

    def list_routes(self) -> list[dict]:
        with self._service.sessions() as session:
            routes = session.query(DemoRoute).order_by(DemoRoute.created_at).all()
            return [self._route_dict(route) for route in routes]

    def _route_dict(self, route: DemoRoute) -> dict:
        return {"id": route.id, "name": route.name, "steps": list(route.steps),
                "created_at": route.created_at.isoformat() if route.created_at else None}

    def _load_route(self, route_id: str) -> dict:
        with self._service.sessions() as session:
            route = session.get(DemoRoute, route_id)
            if route is None:
                raise ServiceError(404, "路线不存在")
            return self._route_dict(route)

    def create_route(self, payload: dict) -> dict:
        name = payload["name"]
        with self._service.sessions() as session:
            if session.query(DemoRoute).filter(DemoRoute.name == name).count():
                raise ServiceError(409, f"路线「{name}」已存在")
            route = DemoRoute(id=str(uuid4()), name=name, steps=payload["steps"])
            session.add(route)
            session.commit()
            return self._route_dict(route)

    def delete_route(self, route_id: str) -> dict:
        with self._service.sessions() as session:
            route = session.get(DemoRoute, route_id)
            if route is None:
                raise ServiceError(404, "路线不存在")
            session.delete(route)
            session.commit()
        return {"ok": True}

    # ---------- 演示编排 ----------

    def start(self, route_id: str) -> dict:
        """打开演示开关：拉服务、播问候语、武装语音触发。"""
        if not self._hardware:
            raise ServiceError(409, "语音演示编排仅在真机模式可用，模拟模式请使用手动执行")
        route = self._load_route(route_id)
        if self._busy_phase() in _PHASES_BUSY:
            raise ServiceError(409, "演示或路线执行进行中，请先停止")
        self._begin(route)
        self._spawn(lambda: self._demo_worker(route), "h2-demo-start")
        return {"ok": True, "phase": "starting"}

    def stop(self) -> dict:
        """关闭演示/中止执行：请求后台线程停止，运动循环会在下一帧内停步。"""
        self._abort.set()
        return {"ok": True}

    def run_route(self, route_id: str) -> dict:
        """手动直跑路线（调试路径）：不武装语音、不拉服务，护栏相同。"""
        route = self._load_route(route_id)
        if self._busy_phase() in _PHASES_BUSY:
            raise ServiceError(409, "演示或路线执行进行中，请先停止")
        self._begin(route)
        self._spawn(lambda: self._execute_worker(route), "h2-demo-run")
        return {"ok": True, "phase": "running"}

    def _begin(self, route: dict) -> None:
        self._abort.clear()
        with self._lock:
            self._route = route
            self._step_index = 0
            self._error = None
            self._motion_started = False

    def _demo_worker(self, route: dict) -> None:
        try:
            self._set_phase("starting", "拉起现场服务…")
            self._pull_up_services()
            self._set_phase("armed", "等待「开始」口令")
            self._watch_keyword(route)
        except DemoAbort as abort:
            self._finish("aborted", str(abort))
        except Exception as error:  # noqa: BLE001 - 编排失败要落到界面上
            self._finish("aborted", f"演示启动失败：{error}")

    def _pull_up_services(self) -> None:
        """按对演示体验最友好的顺序拉起：机器人先说话，重服务后台热身。"""
        self._check_abort()
        voice = field.start_pc3_voice()
        self._check_abort()
        if voice.get("ok"):
            self._say_quiet(GREETING)
        for kind in ("vision", "mapping", "asr"):
            self._check_abort()
            self._set_phase("starting", f"拉起{VALVE_LABELS[kind]}…")
            result = field.set_data_valve(kind, True)
            # 建图失败=没有位姿来源，ASR 失败=没有触发来源，二者必须中止；
            # 视觉失败只影响朗读，保留演示。
            if not result.get("ok") and kind in ("mapping", "asr"):
                raise DemoAbort(f"{VALVE_LABELS[kind]}服务拉起失败："
                                f"{result.get('error') or '就绪探测未通过'}")

    def _watch_keyword(self, route: dict) -> None:
        """武装期监听转写。基线取服务就绪后的 last_id，历史文本不参与触发。"""
        payload = field.asr_text(0)
        since = payload.get("last_id") or 0 if payload else 0
        asr_down = 0
        while not self._abort.is_set():
            payload = field.asr_text(since)
            if payload is None:
                asr_down += 1
                if asr_down >= ASR_DOWN_LIMIT:
                    raise DemoAbort("语音识别服务持续不可用，演示解除武装")
                time.sleep(1.5)
                continue
            asr_down = 0
            last_id = payload.get("last_id") or 0
            if last_id < since:
                # ASR 服务重启后 id 从 1 重新计数：条目全是重启后的新文本，从头拉取
                since = 0
                payload = field.asr_text(0) or payload
            since = payload.get("last_id") or since
            for item in payload.get("items", []):
                if TRIGGER_KEYWORD in str(item.get("text", "")):
                    if self._try_trigger(route) != "armed":
                        return                 # 已进入执行并得出结果
                    break                      # 本次口令未就绪，回待命继续听
            time.sleep(0.8)
        self._finish("aborted", "操作员停止演示")

    def _try_trigger(self, route: dict) -> str:
        """口令到达：先确认播报，再校验位姿就绪；不就绪保持待命。"""
        self._set_phase("running", "收到口令，确认中…")
        self._say_quiet(CONFIRM)
        for _ in range(3):
            self._check_abort()
            if field.fastlio_pose() is not None:
                return self._execute_worker(route, in_demo=True)
            time.sleep(0.5)
        self._say_quiet(NOT_READY)
        self._set_phase("armed", "位姿未就绪，等待「开始」口令")
        return "armed"

    def _execute_worker(self, route: dict, in_demo: bool = False) -> str:
        try:
            self._run_sequence(route)
        except DemoAbort as abort:
            self._finish("aborted", str(abort))
            return "aborted"
        except Exception as error:  # noqa: BLE001 - 执行异常要落到界面上
            self._finish("aborted", f"执行异常：{error}")
            return "aborted"
        if in_demo:
            self._say_quiet(DONE)
        self._finish("done", "路线执行完成")
        return "done"

    def _say_quiet(self, text: str) -> None:
        """演示话术播报：停采音→播完→恢复采音。

        机器人自听会以连续语音占据 VAD 并挤占推理队列（2026-11-20 实测：
        问候语自听与操作员口令合并成段、转写延迟积压到分钟级），
        播报期间静默麦克风是 AEC 缺位下的现场补偿。"""
        if not self._hardware:
            field.say(text)
            return
        with suppress(Exception):
            field._asr_post("/asr/stop")
        field.say(text)
        with suppress(Exception):
            field._asr_post("/asr/start")

    def _finish(self, phase: str, detail: str) -> None:
        # 只有真正动起来之后的中止才向现场播报（例如占用控制权被拒不必出声）
        if (self._hardware and phase == "aborted" and self._motion_started):
            self._say_quiet(INTERRUPT)
        self._set_phase(phase, detail)

    # ---------- 序列执行 ----------

    @staticmethod
    def _default_client_factory(target: str, timeout: float):
        from h2_gateway.client import GatewayClient
        return GatewayClient(target=target, timeout=timeout)

    def _run_sequence(self, route: dict) -> None:
        target = (self._settings.teleop_gateway_address if self._hardware
                  else self._settings.gateway_address)
        client = self._make_client(target, 0.75)
        state: dict[str, Any] = {"seq": 0, "clock": None, "token": ""}
        try:
            self._set_phase("running", "申请运动控制权…")
            state["token"], state["clock"] = self._acquire(client)
            if self._hardware:
                feed = _PoseFeed(lambda: self._pose(client))
                feed.start()
            else:
                feed = _SyncPoseFeed(client, self)
            try:
                for index, step in enumerate(route["steps"]):
                    self._set_step(index)
                    kind = step["type"]
                    if kind == "forward":
                        self._move(client, state, feed, float(step["value"]))
                    elif kind == "turn":
                        self._turn(client, state, feed, float(step["value"]))
                    else:
                        self._dwell(client, state, step)
                self._set_step(len(route["steps"]))
            finally:
                feed.close()
            self._send_checked(client, state, "stop")
            reply = client.teleop_release(state["token"])
            if not reply.get("accepted"):
                raise DemoAbort(f"释放控制权失败：{reply.get('code')}")
        except Exception:
            # 任何异常路径都先尽力停步并归还租约；失败时网关租约到期也会自动零速
            with suppress(Exception):
                self._send(client, state, "stop")
            with suppress(Exception):
                client.teleop_release(state["token"])
            raise
        finally:
            client.close()

    def _acquire(self, client) -> tuple[str, GatewayClock | None]:
        # 就绪判定与浏览器遥控（teleop_socket）同语义：teleop_ready 仅硬件网关提供，
        # 模拟网关可达即可申请租约。
        deadline = time.monotonic() + ACQUIRE_READY_TIMEOUT
        ready = False
        while time.monotonic() < deadline:
            self._check_abort()
            try:
                if client.get_state().get("teleop_ready") or not self._hardware:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if not ready:
            raise DemoAbort("机器人遥控服务未就绪")
        reply = client.teleop_acquire()
        if not reply.get("accepted"):
            code = reply.get("code")
            if code == "teleop_busy":
                raise DemoAbort("手动遥控占用中，请先释放控制权")
            if code == "robot_busy":
                raise DemoAbort("巡检任务进行中，无法执行路线")
            raise DemoAbort(f"未获得运动控制权：{code}")
        return reply["token"], GatewayClock.from_reply(reply)

    def _pose(self, client):
        """硬件模式用 FastLIO 位姿（DDS 里程计存在 1/3 尺度问题，不用）。

        返回 (x, y, yaw, imu_yaw)；imu_yaw 来自桥的陀螺积分，桥旧版无此字段
        时为 None，转向闭环退回扫描匹配 yaw。
        """
        if self._hardware:
            payload = field.fastlio_pose(timeout=0.8)
            if payload is None:
                return None
            imu = payload.get("imu_yaw")
            return (float(payload["x"]), float(payload["y"]), float(payload["yaw"]),
                    float(imu) if isinstance(imu, (int, float)) else None)
        pose = client.get_state().get("pose")
        if not pose:
            return None
        return float(pose["x"]), float(pose["y"]), float(pose["yaw"]), None

    def _send(self, client, state: dict, action: str) -> dict:
        """发送一帧。硬件模式除 stop 外一律携带 PC3 单调钟截止时间。"""
        state["seq"] += 1
        if self._hardware:
            kwargs: dict[str, Any] = {"timeout": 0.5}
            if action != "stop":
                clock = state["clock"]
                if clock is None:
                    raise DemoAbort("缺少网关时钟样本，无法计算指令有效期")
                kwargs["gateway_deadline_ms"] = clock.translate(
                    time.monotonic_ns() + TTL_MS * 1_000_000)
            reply = client.teleop_command(state["token"], state["seq"], action, 0, **kwargs)
        else:
            # 模拟网关按宿主机 Unix 时间校验，剩余有效期上限 500ms
            reply = client.teleop_command(state["token"], state["seq"], action,
                                          int(time.time() * 1000) + 240, timeout=0.5)
        clock = GatewayClock.from_reply(reply)
        if clock is not None:
            state["clock"] = clock
        return reply

    _TRANSIENT_CODES = frozenset({"expired", "invalid_expiry"})

    def _send_checked(self, client, state: dict, action: str) -> dict:
        """发送一帧并归类回执：瞬时过期可容忍，会话性失败立即中止。"""
        reply = self._send(client, state, action)
        if reply.get("accepted"):
            if action not in ("stop", "heartbeat"):
                self._motion_started = True
            return reply
        code = reply.get("code")
        if code in self._TRANSIENT_CODES:
            return reply                     # 下一帧重新计算有效期即可
        if code == "invalid_token":
            raise DemoAbort("运动控制权已丢失（租约被撤销）")
        raise DemoAbort(f"指令 {action} 被网关拒绝：{code}")

    def _check_abort(self) -> None:
        if self._abort.is_set():
            raise DemoAbort("操作员停止演示")

    def _check_field(self) -> None:
        """硬件模式下的现场前置：映射阀门关闭即失去位姿来源。"""
        if self._hardware and not field.DATA_VALVES.get("mapping", True):
            raise DemoAbort("雷达数据接入被关闭，位姿来源中断")

    def _move(self, client, state: dict, feed, distance: float) -> None:
        sign = 1.0 if distance >= 0 else -1.0
        cruise, slow = (("forward", "forward_slow") if sign > 0
                        else ("backward", "backward_slow"))
        reverse_slow = "backward_slow" if sign > 0 else "forward_slow"
        target = feed.tracker.distance + abs(distance)
        deadline = time.monotonic() + 15 + abs(distance) / 0.1   # 步超时兜底
        stall_progress = feed.tracker.distance
        stall_at = time.monotonic()
        passes = 0                 # 停流-测量-反向的往返次数
        while True:
            self._check_abort()
            self._check_field()
            feed.poll()
            tracker = feed.tracker
            if feed.fail >= POSE_FAIL_LIMIT:
                raise DemoAbort("位姿丢失（建图数据中断）")
            remaining = target - tracker.distance
            if abs(remaining) <= DIST_TOLERANCE_M:
                break
            if time.monotonic() > deadline:
                raise DemoAbort(f"前进 {abs(distance):.2f}m 步超时")
            if tracker.distance - stall_progress >= STALL_DISTANCE_M:
                stall_progress, stall_at = tracker.distance, time.monotonic()
            elif time.monotonic() - stall_at > STALL_SECONDS:
                raise DemoAbort("指令持续发送而位姿不涨：速度低于行走阈值，"
                                "请上调网关 H2_V_*_SLOW 环境变量后重试")
            if not self._hardware:
                # 模拟：测量即时、无步态粒度，连续帧流直接收敛到容差
                action = cruise if remaining > CRUISE_REMAINING_M else (slow if remaining > 0 else reverse_slow)
                self._send_checked(client, state, action)
                time.sleep(FRAME_INTERVAL)
            elif abs(remaining) > CUT_EARLY_M:
                # 闭环行进（含反向）：连续帧流直到剩余量进入提前窗。
                # 提前窗按停距设置，任何方向都受它约束，不存在盲发段。
                self._send_checked(client, state, slow if remaining > 0 else reverse_slow)
                time.sleep(FRAME_INTERVAL)
            elif abs(remaining) <= ACCEPT_TOLERANCE_M:
                break            # 停稳后落在接受窗内（反应超冲决定的最小可达精度）
            elif passes < APPROACH_PASSES:
                passes += 1      # 停流等停稳再测；停距惯性通常会把剩余量带进接受窗
                self._send_checked(client, state, "stop")
                time.sleep(SETTLE_AFTER_BURST)
            else:
                raise DemoAbort(f"前进目标未收敛（剩余 {abs(remaining):.2f}m）")
        self._send_checked(client, state, "stop")
        time.sleep(SETTLE_AFTER_STEP)

    def _turn(self, client, state: dict, feed, degrees: float) -> None:
        positive = ("turn_left", "turn_left_slow") if TURN_LEFT_POSITIVE \
            else ("turn_right", "turn_right_slow")
        negative = ("turn_right", "turn_right_slow") if TURN_LEFT_POSITIVE \
            else ("turn_left", "turn_left_slow")
        # 转向反馈优先 IMU 陀螺积分（左正）；无 IMU 时退回扫描匹配 yaw
        start_yaw = IMU_YAW_SIGN * feed.tracker.imu_yaw if feed.tracker.imu_yaw is not None \
            else feed.tracker.yaw
        target = start_yaw + math.radians(degrees)
        deadline = time.monotonic() + 15 + abs(math.radians(degrees)) / 0.1
        stall_yaw = start_yaw
        stall_at = time.monotonic()
        passes = 0
        while True:
            self._check_abort()
            self._check_field()
            feed.poll()
            tracker = feed.tracker
            if feed.fail >= POSE_FAIL_LIMIT:
                raise DemoAbort("位姿丢失（建图数据中断）")
            turn_yaw = IMU_YAW_SIGN * tracker.imu_yaw if tracker.imu_yaw is not None else tracker.yaw
            remaining = target - turn_yaw
            if abs(remaining) <= YAW_TOLERANCE_RAD:
                break
            if time.monotonic() > deadline:
                raise DemoAbort(f"转向 {degrees:.0f}° 步超时")
            if abs(turn_yaw - stall_yaw) >= STALL_YAW_RAD:
                stall_yaw, stall_at = turn_yaw, time.monotonic()
            elif time.monotonic() - stall_at > STALL_SECONDS:
                raise DemoAbort("指令持续发送而偏航不涨：角速度低于行走阈值，"
                                "请上调网关 H2_V_TURN_SLOW 环境变量后重试")
            if not self._hardware:
                action = (positive[0] if remaining > 0 else negative[0]) if abs(remaining) > CRUISE_REMAINING_RAD \
                    else (positive[1] if remaining > 0 else negative[1])
                self._send_checked(client, state, action)
                time.sleep(FRAME_INTERVAL)
            elif abs(remaining) > CUT_EARLY_RAD:
                # 与直线步同款闭环（含反向），见 _move 注释
                self._send_checked(client, state, positive[1] if remaining > 0 else negative[1])
                time.sleep(FRAME_INTERVAL)
            elif abs(remaining) <= ACCEPT_TOLERANCE_RAD:
                break
            elif passes < APPROACH_PASSES:
                passes += 1
                self._send_checked(client, state, "stop")
                time.sleep(SETTLE_AFTER_BURST)
            else:
                raise DemoAbort(f"转向目标未收敛（剩余 {math.degrees(remaining):.0f}°）")
        self._send_checked(client, state, "stop")
        time.sleep(SETTLE_AFTER_STEP)

    def _dwell(self, client, state: dict, step: dict) -> None:
        """停留步：心跳维持租约，结束后按需朗读视觉识别结果。"""
        deadline = time.monotonic() + float(step["value"])
        while True:
            self._check_abort()
            self._check_field()
            if time.monotonic() >= deadline:
                break
            self._send(client, state, "heartbeat")
            time.sleep(HEARTBEAT_INTERVAL)
        if step.get("read_aloud") and self._hardware:
            self._read_aloud()

    def _read_aloud(self) -> None:
        digest = field.vision()
        qr = str(digest.get("qr") or "").strip()
        if qr:
            self._say_quiet(f"识别到二维码：{qr[:80]}")
