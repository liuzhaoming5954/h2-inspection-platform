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
PREPARED_TEXT = "巡检准备已就绪。"   # 两段式：准备按钮全部就绪后的「可以开始」信号
# 演示内旁白式拉起（操作员方案 2026-11-21）：口令确认后机器人边播报边
# 逐项启动服务，等待时间成为表演内容；首步前服务全就位，位姿门槛天然通过。
CONFIRM = "收到，巡检演示现在开始。"
NARRATE_MAPPING = "正在启动雷达导航服务。"
MAPPING_READY = "雷达导航已就位。"
NARRATE_VISION = "正在启动图像识别服务。"
VISION_READY = "图像识别已就位。"
VISION_DEGRADED = "图像识别暂不可用，本次巡检不进行画面识别。"
DEMO_GO = "巡检开始！"
DONE = "巡检演示完毕。"
INTERRUPT = "巡检中断，等待人工处理。"

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
ODOM_SCALE = 3.31              # DDS 里程计尺度（2026-11-22 标定：4.06m/1.23m=3.31）：直线距离反馈源
TASK_SUCCESS_SETTLE_S = 5.0     # 停靠任务成功后（播报完）的额外停留
TASK_GIVE_UP_S = 15.0           # 停靠任务最长等待，超时放弃继续
STALL_GRACE_S = 5.0             # 停留步后免检期：步态重启的里程计冻结窗口
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
CUT_EARLY_RAD = math.radians(8)  # 2026-11-22 收紧：15° 时超冲过大，8° 配合纠偏更精细
APPROACH_PASSES = 3             # 停流-测量-反向闭环的往返次数上限
ACCEPT_TOLERANCE_M = 0.2        # 接受窗（步态反应超冲决定的最小可达精度）
ACCEPT_TOLERANCE_RAD = math.radians(20)
CRUISE_REMAINING_M = 0.3        # 剩余距离小于此值切换到慢速动作（当前与巡航同速）
CRUISE_REMAINING_RAD = math.radians(10)
STALL_SECONDS = 15.0            # 停滞判定窗口（步态切换期 DDS 里程计可冻结 3-5s）
STALL_DISTANCE_M = 0.02
STALL_YAW_RAD = math.radians(1)
POSE_FAIL_LIMIT = 10            # 连续位姿拉取失败（约 1 秒）即中止
ACQUIRE_READY_TIMEOUT = 5.0     # 等待遥控就绪的时间
PREPARE_TIMEOUT_S = 150.0       # 准备阶段：服务拉起+就绪检测的总时限
SERVICE_READY_TIMEOUT_S = 90.0  # 演示旁白期：单项服务就绪等待时限
PREPARE_POLL_S = 3.0            # 就绪复检间隔
ASR_DOWN_LIMIT = 20             # 武装期 ASR 连续不可达约 30 秒即中止演示
COMMAND_ACCEPT_AGE_MS = 12_000  # 口令事件接受年龄（服务端 10s 过滤 + 传输余量）
IMU_STALE_MS = 1000             # IMU 采样过期线（200Hz 源，转向闭环强制要求新鲜）
# FastLIO 平移可靠，但快速原地旋转时其 yaw 滞后/滑移（2026-11-20 三源实测：
# 物理右转 174° 时 FastLIO 首段只报约 4 成、停稳后回补超报），转向闭环改用
# Mid360 内置 IMU 的陀螺积分（桥 /pose 的 imu_yaw，原生日志号实测右转为正）。
# 本模块统一左转为正，故乘 -1；若现场换机器反号则改回 +1。
IMU_YAW_SIGN = -1.0
# 动作映射：turn_left 应使左正角度增大（2026-11-20 实测一致；换机反号则改 False）。
TURN_LEFT_POSITIVE = True

# 阀门名 → 界面用语（拉起阶段的状态展示）
VALVE_LABELS = {"vision": "视觉", "mapping": "建图", "asr": "语音识别"}

_PHASES_BUSY = frozenset({"preparing", "prepared", "starting", "armed", "running"})
# 就绪检查项 → 界面用语
CHECK_LABELS = {"mic": "麦克风数据流", "asr": "语音识别", "pose": "建图位姿",
                "imu": "IMU 转角", "vision": "相机画面", "teleop": "运控就绪"}


class DemoAbort(RuntimeError):
    """执行器内部的中止信号：消息面向操作员展示。"""


class _PoseTracker:
    """由连续位姿样本累计路程与解缠偏航。

    目标一律用「步开始时的累计量 + 步幅」表达，与世界系原点无关；
    之前步骤的抖动计入基线而不是本步增量。
    odom_distance：机器人自身里程计的位移累计——尺度未校（实测约 1/3），
    但相对变化可靠，用作 FastLIO 扫描匹配丢失时的运动旁证（2026-11-21：
    行走中 FastLIO 位姿停更但机器人实际在走，停滞检测误杀）。
    """

    def __init__(self) -> None:
        self.distance = 0.0
        self.yaw = 0.0
        self.imu_yaw: float | None = None   # IMU 陀螺积分（原生符号），缺失保持 None
        self.odom_distance = 0.0            # 里程计位移累计（尺度未校，看趋势不看绝对值）
        self._last: tuple[float, float, float] | None = None
        self._last_imu: float | None = None
        self._last_odom: tuple[float, float] | None = None

    def update(self, x: float, y: float, yaw: float, imu_yaw: float | None = None,
               odom: tuple[float, float] | None = None) -> None:
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
        if odom is not None:
            if self._last_odom is not None:
                self.odom_distance += math.hypot(odom[0] - self._last_odom[0],
                                                 odom[1] - self._last_odom[1])
            self._last_odom = odom


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
        self.imu_ok = False      # 最近一次样本是否携带有效 IMU（转向步强制要求）
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            pose = self._source()
            if pose is None:
                self.fail += 1
            else:
                self.fail = 0
                self.imu_ok = pose[3] is not None
                self.tracker.update(*pose[:4], odom=pose[4] if len(pose) > 4 else None)
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
        self.imu_ok = False

    def poll(self) -> None:
        pose = self._service._pose(self._client)
        if pose is None:
            self.fail += 1
        else:
            self.fail = 0
            self.imu_ok = pose[3] is not None
            self.tracker.update(*pose[:4], odom=pose[4] if len(pose) > 4 else None)

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
        self._checks: dict[str, bool] = {}    # 就绪检查项（准备阶段实时更新）
        self._motion_started = False           # 只在真正发出过运动帧后播报中断
        self._last_step_was_dwell = False       # 上一步是否为停留步（免检期判定）
        self._abort = threading.Event()
        self._worker: threading.Thread | None = None

    # ---------- 状态 ----------

    def state(self) -> dict:
        with self._lock:
            steps_total = len(self._route["steps"]) if self._route else 0
            return {"phase": self._phase, "detail": self._detail, "error": self._error,
                    "route": dict(self._route) if self._route else None,
                    "step_index": self._step_index, "steps_total": steps_total,
                    "checks": dict(self._checks), "hardware": self._hardware}

    def _set_checks(self, checks: dict[str, bool]) -> None:
        with self._lock:
            self._checks = checks

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

    def prepare(self) -> dict:
        """「巡检准备」按钮：一键拉起全部服务并做功能性就绪检测。

        全部就绪后播报「巡检准备已就绪」，这是操作员可点「巡检演示」的
        信号。检查项都是功能性判据（麦克风真实数据流、IMU 有效等），
        不以 HTTP 可达充数——2026-11-21 现场教训：旗子全绿但耳朵已死。
        """
        if not self._hardware:
            raise ServiceError(409, "巡检准备仅在真机模式可用")
        if self._busy_phase() in _PHASES_BUSY and self._busy_phase() != "prepared":
            raise ServiceError(409, "演示或准备进行中，请先停止")
        self._abort.clear()
        with self._lock:
            self._phase = "preparing"
            self._detail = "拉起服务…"
            self._error = None
            self._checks = {}
            self._route = None
        self._spawn(self._prepare_worker, "h2-demo-prepare")
        return {"ok": True, "phase": "preparing"}

    def _prepare_worker(self) -> None:
        try:
            self._pull_up_services()
            deadline = time.monotonic() + PREPARE_TIMEOUT_S
            mic_remediated = False
            while time.monotonic() < deadline:
                self._check_abort()
                checks = self._readiness_checks()
                self._set_checks(checks)
                if all(checks.values()):
                    self._set_phase("prepared", "全部服务就绪")
                    self._say_quiet(PREPARED_TEXT)
                    return
                failed = [CHECK_LABELS[key] for key, ok in checks.items() if not ok]
                # 麦克风无数据：自动做一次 USB 复位（现场验证过的修复动作）
                if not checks.get("mic") and not mic_remediated:
                    mic_remediated = True
                    self._set_phase("preparing", "麦克风无数据，执行 USB 复位…")
                    field.reset_pc3_mic()
                    with suppress(Exception):
                        field._asr_post("/asr/start")   # 复位后重挂采音
                    time.sleep(4)
                    continue
                self._set_phase("preparing", "等待就绪：" + "、".join(failed))
                time.sleep(PREPARE_POLL_S)
            self._set_phase("prep_failed", "准备超时："
                            + "、".join(k for k, v in self._checks.items() if not v))
        except DemoAbort:
            self._set_phase("idle", "准备已取消")
        except Exception as error:  # noqa: BLE001 - 准备失败要落到界面
            self._set_phase("prep_failed", f"准备失败：{error}")

    def _readiness_checks(self) -> dict[str, bool]:
        """准备阶段的功能性就绪检查（语音两项）；位姿/IMU/视觉/运控在
        演示的旁白式拉起中逐项点亮（_perform_narrated 内更新）。"""
        checks: dict[str, bool] = {}
        health = field.asr_health()
        checks["asr"] = bool(health and health.get("model_ready") is True
                             and (health.get("inference") or {}).get("fault") is not True)
        checks["mic"] = bool(health and health.get("capturing") is True
                             and health.get("mic_recent") is True)
        return checks

    def start(self, route_id: str) -> dict:
        """「巡检演示」按钮：要求先完成巡检准备，再问候、武装、听令。"""
        if not self._hardware:
            raise ServiceError(409, "语音演示编排仅在真机模式可用，模拟模式请使用手动执行")
        if self._busy_phase() == "preparing":
            raise ServiceError(409, "巡检准备进行中，请等待就绪播报")
        if self._busy_phase() != "prepared":
            raise ServiceError(409, "请先点击「巡检准备」并等待就绪播报")
        route = self._load_route(route_id)
        self._begin(route)
        self._spawn(lambda: self._demo_worker(route), "h2-demo-start")
        return {"ok": True, "phase": "starting"}

    def stop(self) -> dict:
        """停止/复位：执行中则请求中止（下一帧内停步）；就绪/失败态直接回空闲。"""
        self._abort.set()
        phase = self._busy_phase()
        if phase in {"prepared", "prep_failed"}:
            self._set_phase("idle", "已复位")
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
            self._last_step_was_dwell = False

    def _demo_worker(self, route: dict) -> None:
        try:
            self._set_phase("starting", "巡检演示开始…")
            if field.voice_ready():
                self._say_quiet(GREETING)
            session = field.asr_arm_session()
            if not session or not session.get("session_id"):
                raise DemoAbort("语音识别服务不可用，无法武装待命")
            self._set_phase("armed", "等待「开始」口令")
            self._watch_keyword(route, session)
        except DemoAbort as abort:
            self._finish("aborted", str(abort))
        except Exception as error:  # noqa: BLE001 - 编排失败要落到界面上
            self._finish("aborted", f"演示启动失败：{error}")

    def _pull_up_services(self) -> None:
        """「巡检准备」只拉语音专项（播报+识别）：准备最快、语音链独占
        资源最稳；首转推理预热在识别服务内自动完成。雷达/图像属于演示
        内的旁白式拉起（见 _perform_narrated），问候语与武装属于
        「巡检演示」按钮。"""
        self._check_abort()
        field.ensure_usb_audio_power()   # USB 节电修复（2026-11-21 根因，幂等）
        self._check_abort()
        if not field.start_pc3_voice().get("ok"):
            raise DemoAbort("语音播报服务拉起失败")
        self._check_abort()
        self._set_phase("preparing", "拉起语音识别…")
        if not field.set_data_valve("asr", True).get("ok"):
            raise DemoAbort("语音识别服务拉起失败：没有口令触发来源")

    def _watch_keyword(self, route: dict, session: dict) -> None:
        """待命听令：只消费当前武装会话的口令命令事件（任务书第三节）。

        会话在问候语结束后立即开启（见 _pull_up_services）。整句「开始」的
        判定、否定句/自听/迟到/重复的拒绝都在识别服务完成，这里校验
        boot_id 与会话 id；服务重启会使旧会话失效，重新武装后旧事件不会
        重放；一次有效触发即消费——位姿未就绪的口令也消费会话，随后
        重新武装让操作员再说一次。"""
        since = 0
        asr_down = 0
        try:
            while not self._abort.is_set():
                payload = field.asr_commands(since)
                if payload is None:
                    asr_down += 1
                    if asr_down >= ASR_DOWN_LIMIT:
                        raise DemoAbort("语音识别服务持续不可用，演示解除武装")
                    time.sleep(1.5)
                    continue
                asr_down = 0
                since = payload.get("last_seq") or since
                live = payload.get("session") or {}
                session_lost = (payload.get("boot_id") != session.get("boot_id")
                                or live.get("id") != session.get("session_id"))
                if session_lost:
                    # 会话失效的两条路径：服务重启（boot 变）或推理监督回收作废
                    # 会话（2026-11-21 现场：挂起段 60s 超时回收后待命聋死）。
                    # 重新武装即恢复听令，事件序号从零、旧口令不重放。
                    fresh = field.asr_arm_session()
                    if fresh and fresh.get("session_id"):
                        session = fresh
                        since = 0
                    time.sleep(0.8)
                    continue
                for item in payload.get("items", []):
                    if item.get("session_id") != session.get("session_id"):
                        continue
                    if item.get("age_ms", 99_999) > COMMAND_ACCEPT_AGE_MS:
                        continue         # 迟到事件（服务端已滤，双保险）
                    if self._perform_narrated(route) != "armed":
                        return           # 已进入执行并得出结果
                    # 位姿未就绪：口令已消费当前会话，重新武装以便再说一次
                    fresh = field.asr_arm_session()
                    if fresh and fresh.get("session_id"):
                        session = fresh
                        since = 0
                    break
                time.sleep(0.8)
        finally:
            field.asr_disarm_session()
        self._finish("aborted", "操作员停止演示")

    def _perform_narrated(self, route: dict) -> str:
        """口令到达后的旁白式启动（操作员方案 2026-11-21）。

        确认播报 → 「正在启动雷达导航服务」→ 拉建图并等位姿/IMU 有效 →
        「雷达导航已就位」→「正在启动图像识别服务」→ 拉视觉等画面 →
        「图像识别已就位」→「巡检开始！」→ 执行路线。等待成为表演
        内容，首步前服务全就位。返回执行结果状态。"""
        self._set_phase("running", "收到口令")
        self._say_quiet(CONFIRM)

        # 雷达导航：拉建图，等位姿与 IMU 都有效（执行器的行进门槛）
        self._set_phase("running", "正在启动雷达导航服务…")
        self._say_quiet(NARRATE_MAPPING)
        self._check_abort()
        result = field.set_data_valve("mapping", True)
        if not result.get("ok"):
            raise DemoAbort("雷达导航服务拉起失败："
                            f"{result.get('error') or '就绪探测未通过'}")
        deadline = time.monotonic() + SERVICE_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            self._check_abort()
            pose = field.fastlio_pose()
            imu_ok = bool(pose and pose.get("imu_valid") is True
                          and isinstance(pose.get("imu_age_ms"), (int, float))
                          and pose["imu_age_ms"] <= IMU_STALE_MS)
            if pose is not None and imu_ok:
                break
            time.sleep(PREPARE_POLL_S)
        else:
            raise DemoAbort("雷达导航位姿未就绪")
        self._set_checks({**self._checks, "pose": True, "imu": True})
        self._say_quiet(MAPPING_READY)

        # 图像识别：拉视觉，画面不新鲜不阻断（停靠朗读降级）
        self._set_phase("running", "正在启动图像识别服务…")
        self._say_quiet(NARRATE_VISION)
        self._check_abort()
        vision_ok = field.set_data_valve("vision", True).get("ok") is True
        deadline = time.monotonic() + 20.0
        while vision_ok and time.monotonic() < deadline:
            self._check_abort()
            if field.vision_frame_ready():
                break
            time.sleep(PREPARE_POLL_S)
        else:
            if vision_ok and not field.vision_frame_ready():
                vision_ok = False
        self._set_checks({**self._checks, "vision": vision_ok})
        self._say_quiet(VISION_READY if vision_ok else VISION_DEGRADED)

        # 运控就绪由执行器申请租约时核验；全部旁白完成，进入执行
        self._set_phase("running", "巡检开始")
        self._say_quiet(DEMO_GO)
        return self._execute_worker(route, in_demo=True)

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
        """演示话术播报。自听抑制已由 voice_service 服务端处理：
        播报起止标记给识别服务（/asr/playback），期间丢弃采音、
        交叉段不进转写也不触发口令，无需上位机停启采音。"""
        field.say(text)

    def _say_holding_lease(self, client, state: dict, text: str) -> None:
        """持租约期间的长播报：保持零运动的同时并行心跳续租（任务书第五节）。

        播报阻塞数秒以上，若不续租租约会过期触发自动零速与撤销。"""
        halt = threading.Event()

        def beat() -> None:
            while not halt.wait(HEARTBEAT_INTERVAL):
                with suppress(Exception):
                    self._send(client, state, "heartbeat")

        worker = threading.Thread(target=beat, daemon=True, name="h2-demo-say-beat")
        worker.start()
        try:
            self._say_quiet(text)
        finally:
            halt.set()
            worker.join(timeout=1.0)

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
            # 全程心跳（2026-11-21）：贯穿所有步骤的不间断续租线程，
            # 消除步骤切换、任务采样、播报阻塞期间的心跳断档
            halt_hb = threading.Event()
            def _hb() -> None:
                while not halt_hb.wait(HEARTBEAT_INTERVAL):
                    with suppress(Exception):
                        self._send(client, state, "heartbeat")
            hb_thread = threading.Thread(target=_hb, daemon=True, name="h2-demo-hb")
            hb_thread.start()
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
                halt_hb.set()
                hb_thread.join(timeout=1.0)
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
            # odom 独立获取（不依赖 FastLIO）：2026-11-22 实测 FastLIO 抖动
            # 导致 _pose 返回 None → odom 停更 → 2m 目标走 5m 仍不停
            try:
                odom_pose = client.get_state().get("pose")
                odom = (float(odom_pose["x"]), float(odom_pose["y"])) if odom_pose else None
            except Exception:  # noqa: BLE001
                odom = None
            if payload is None:
                if odom is None:
                    return None    # 两者都不可用才计失败
                # FastLIO 不可用但 odom 正常：返回 odom 数据（FastLIO 置零），
                # _move 用 odom 做距离不受影响；建图阀门断流由 _check_field 兜底
                return (0.0, 0.0, 0.0, None, odom)
            # IMU 只在「有样本且未停更」时提供：初始 0 或停更的积分不能当转角
            imu = payload.get("imu_yaw")
            imu_fresh = (payload.get("imu_valid") is True
                         and isinstance(payload.get("imu_age_ms"), (int, float))
                         and payload["imu_age_ms"] <= IMU_STALE_MS)
            return (float(payload["x"]), float(payload["y"]), float(payload["yaw"]),
                    float(imu) if imu_fresh and isinstance(imu, (int, float)) else None,
                    odom)   # odom 来自 GetState（DDS），作运动旁证
        state = client.get_state()
        pose = state.get("pose")
        if not pose:
            return None
        return (float(pose["x"]), float(pose["y"]), float(pose["yaw"]),
                None, (float(pose["x"]), float(pose["y"])))   # 模拟模式 odom=自身位姿

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
        """直线步（2026-11-22 里程计版）：距离反馈改用机器人自身 DDS 里程计
        （×ODOM_SCALE 校正），FastLIO 行走中的跳变/丢失不再影响行进判定。
        转向步不受影响（原本用 IMU）。"""
        sign = 1.0 if distance >= 0 else -1.0
        cruise, slow = (("forward", "forward_slow") if sign > 0
                        else ("backward", "backward_slow"))
        reverse_slow = "backward_slow" if sign > 0 else "forward_slow"
        # 里程计基线与目标（里程计增量 = 实际距离 / 尺度）
        # 模拟模式的里程计=网关位姿（无尺度），硬件模式=DDS 里程计（约 1/3）
        scale = ODOM_SCALE if self._hardware else 1.0
        # 停留步后免检期：长时间站立→重新起步的里程计冻结窗口（2026-11-22）
        grace_until = time.monotonic() + STALL_GRACE_S if self._last_step_was_dwell else 0
        self._last_step_was_dwell = False
        odom_base = feed.tracker.odom_distance
        odom_target = odom_base + abs(distance) / scale
        odom_tol = DIST_TOLERANCE_M / scale
        odom_cut = CUT_EARLY_M / scale
        odom_accept = ACCEPT_TOLERANCE_M / scale
        deadline = time.monotonic() + 15 + abs(distance) / 0.05
        stall_odom_base = feed.tracker.odom_distance
        stall_at = time.monotonic()
        passes = 0
        while True:
            self._check_abort()
            self._check_field()
            feed.poll()
            tracker = feed.tracker
            if feed.fail >= POSE_FAIL_LIMIT:
                raise DemoAbort("位姿丢失（建图数据中断）")
            remaining = odom_target - tracker.odom_distance
            if abs(remaining) <= odom_tol:
                break
            if time.monotonic() > deadline:
                raise DemoAbort(f"前进 {abs(distance):.2f}m 步超时")
            # 停滞检测（纯里程计，不再依赖 FastLIO）
            if tracker.odom_distance - stall_odom_base >= STALL_DISTANCE_M / scale:
                stall_odom_base = tracker.odom_distance
                stall_at = time.monotonic()
            elif time.monotonic() < grace_until:
                stall_at = time.monotonic()   # 免检期内不计时（步态重启窗口）
            elif time.monotonic() - stall_at > STALL_SECONDS:
                raise DemoAbort("指令持续发送而里程计不涨：速度低于行走阈值，"
                                "请上调网关 H2_V_*_SLOW 环境变量后重试")
            # 纠偏保护：后退不超过 0.5m 等效里程，超限接受当前位置
            if (remaining > 0 and tracker.odom_distance < odom_base - 0.5 / scale):
                break
            if not self._hardware:
                # 模拟：连续帧流直接收敛
                action = cruise if remaining > CRUISE_REMAINING_M / scale else (slow if remaining > 0 else reverse_slow)
                self._send_checked(client, state, action)
                time.sleep(FRAME_INTERVAL)
            elif abs(remaining) > odom_cut:
                # 闭环行进（含反向纠偏）
                self._send_checked(client, state, slow if remaining > 0 else reverse_slow)
                time.sleep(FRAME_INTERVAL)
            elif abs(remaining) <= odom_accept:
                break
            elif passes < APPROACH_PASSES:
                passes += 1
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
        # 停留步后免检期（同 _move：步态重启的里程计/IMU 冻结窗口）
        grace_until = time.monotonic() + STALL_GRACE_S if self._last_step_was_dwell else 0
        self._last_step_was_dwell = False
        # 转向前校零：消除温漂累计（采样 1 秒陀螺取均值从积分中扣除）
        if self._hardware:
            with suppress(Exception):
                field.calibrate_imu_bias()
            time.sleep(0.2)   # 等桥的积分稳定

        # 转向反馈优先 IMU 陀螺积分（左正）；无 IMU 时退回扫描匹配 yaw
        start_yaw = IMU_YAW_SIGN * feed.tracker.imu_yaw if feed.tracker.imu_yaw is not None \
            else feed.tracker.yaw
        target = start_yaw + math.radians(degrees)
        deadline = time.monotonic() + 15 + abs(math.radians(degrees)) / 0.1
        stall_yaw = start_yaw
        stall_at = time.monotonic()
        passes = 0
        stall_odom_base = feed.tracker.odom_distance
        while True:
            self._check_abort()
            self._check_field()
            feed.poll()
            tracker = feed.tracker
            if feed.fail >= POSE_FAIL_LIMIT:
                raise DemoAbort("位姿丢失（建图数据中断）")
            if self._hardware and not feed.imu_ok:
                # 转向闭环强制要求新鲜 IMU：初始无效或运行中停更都立即中止，
                # 不回退到扫描匹配 yaw（快转时不可信，2026-11-20 三源实测）
                raise DemoAbort("IMU 数据无效（未收到或已停更），转向中止等待人工")
            turn_yaw = IMU_YAW_SIGN * tracker.imu_yaw if tracker.imu_yaw is not None else tracker.yaw
            remaining = target - turn_yaw
            if abs(remaining) <= YAW_TOLERANCE_RAD:
                break
            if time.monotonic() > deadline:
                raise DemoAbort(f"转向 {degrees:.0f}° 步超时")
            if abs(turn_yaw - stall_yaw) >= STALL_YAW_RAD:
                stall_yaw, stall_at = turn_yaw, time.monotonic()
            elif time.monotonic() < grace_until:
                stall_at = time.monotonic()   # 免检期
            elif abs(tracker.odom_distance - stall_odom_base) >= STALL_DISTANCE_M / 3:
                # IMU 偏航不涨但里程计位置在变：机器人在动（重心转移），继续等
                stall_odom_base = tracker.odom_distance
                stall_at = time.monotonic()
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
        """停留步（2026-11-21 任务驱动终版）。

        无任务：按指定时长停留。
        有任务：开始即尝试识别；识别成功→播报→停 5 秒→继续；
              15 秒内未识别到→放弃→继续。全程心跳续租。
        """
        task = step.get("task")
        if not task and step.get("read_aloud"):
            task = "qr"   # 兼容旧路线数据
        if not task or not self._hardware:
            # 无任务/模拟模式：按指定时长
            deadline = time.monotonic() + float(step["value"])
            while time.monotonic() < deadline:
                self._check_abort()
                self._check_field()
                self._send(client, state, "heartbeat")
                time.sleep(HEARTBEAT_INTERVAL)
            self._last_step_was_dwell = True
            return

        # 有任务：任务驱动
        halt = threading.Event()

        def beat() -> None:
            while not halt.wait(HEARTBEAT_INTERVAL):
                with suppress(Exception):
                    self._send(client, state, "heartbeat")

        worker = threading.Thread(target=beat, daemon=True, name="h2-demo-dwell-beat")
        worker.start()
        try:
            started = time.monotonic()
            succeeded_at: float | None = None
            while True:
                self._check_abort()
                self._check_field()
                now = time.monotonic()
                if succeeded_at is not None:
                    if now - succeeded_at >= TASK_SUCCESS_SETTLE_S:
                        break           # 播报完停 5 秒，继续
                elif now - started >= TASK_GIVE_UP_S:
                    break               # 15 秒未识别到，放弃继续
                else:
                    result = self._try_recognize(task)
                    if result is not None:
                        self._say_quiet(result)
                        succeeded_at = time.monotonic()
                        continue
                time.sleep(0.5)
        finally:
            halt.set()
            worker.join(timeout=1.0)
        self._last_step_was_dwell = True

    def _try_recognize(self, task: str) -> str | None:
        """单轮识别尝试（3 帧快速采样多数表决），成功返回播报文本。"""
        samples = self._sample_vision(rounds=2, interval=0.3)
        if not samples:
            return None
        if task == "qr":
            texts = [str(s.get("qr") or "").strip() for s in samples]
            non_empty = [t for t in texts if t]
            if not non_empty:
                return None
            best = max(set(non_empty), key=non_empty.count)
            return f"识别到二维码：{self._digits_for_tts(best[:200])}"
        if task == "led":
            leds = [int(s.get("led") or 0) for s in samples]
            led = max(set(leds), key=leds.count)
            if led == 0:
                return None
            reds = [int(s.get("red") or 0) for s in samples]
            greens = [int(s.get("green") or 0) for s in samples]
            red = max(set(reds), key=reds.count)
            green = max(set(greens), key=greens.count)
            return f"识别到信号灯{led}盏，其中红色{red}盏、绿色{green}盏"
        if task == "person":
            people = [int(s.get("person") or 0) for s in samples]
            person = max(set(people), key=people.count)
            if person == 0:
                return None
            return f"视野内共{person}人"
        return None

    def _sample_vision(self, rounds: int = 3, interval: float = 0.5) -> list[dict]:
        """连续采样若干帧视觉摘要（间隔约 0.5s，覆盖视觉程序 3Hz 输出）。"""
        samples = []
        for _ in range(rounds):
            digest = field.vision()
            if isinstance(digest, dict) and digest.get("available"):
                samples.append(digest)
            time.sleep(interval)
        return samples

    def _perform_task(self, client, state: dict, task: str) -> None:
        """停靠点识别任务：三帧多数表决后播报（防单帧闪烁误报）。

        采样（约 1.5 秒）+ 播报全程由心跳线程续租——2026-11-21 实测：
        采样期间不续租导致 2 秒租约过期，网关撤销后执行中止。"""
        halt = threading.Event()

        def beat() -> None:
            while not halt.wait(HEARTBEAT_INTERVAL):
                with suppress(Exception):
                    self._send(client, state, "heartbeat")

        worker = threading.Thread(target=beat, daemon=True, name="h2-demo-task-beat")
        worker.start()
        try:
            self._perform_task_inner(client, state, task)
        finally:
            halt.set()
            worker.join(timeout=1.0)

    @staticmethod
    def _digits_for_tts(text: str) -> str:
        """数字逐字播报：'100' → '一零零'（2026-11-22 现场要求：
        资产编码/序列号不按数值读，逐位读出避免歧义）。"""
        mapping = {"0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
                   "5": "五", "6": "六", "7": "七", "8": "八", "9": "九"}
        return "".join(mapping.get(ch, ch) if ch.isdigit() else ch for ch in text)

    def _perform_task_inner(self, client, state: dict, task: str) -> None:
        samples = self._sample_vision()
        if not samples:
            self._say_holding_lease(client, state, "视觉数据不可用")
            return
        if task == "qr":
            texts = [str(s.get("qr") or "").strip() for s in samples]
            non_empty = [t for t in texts if t]
            if not non_empty:
                return    # 无二维码，安静跳过
            # 取出现最多的文本
            best = max(set(non_empty), key=non_empty.count)
            self._say_holding_lease(client, state, f"识别到二维码：{best[:80]}")
        elif task == "led":
            leds = [int(s.get("led") or 0) for s in samples]
            reds = [int(s.get("red") or 0) for s in samples]
            greens = [int(s.get("green") or 0) for s in samples]
            led = max(set(leds), key=leds.count)
            red = max(set(reds), key=reds.count)
            green = max(set(greens), key=greens.count)
            if led == 0:
                self._say_holding_lease(client, state, "未识别到信号灯")
            else:
                self._say_holding_lease(client, state,
                                        f"识别到信号灯{led}盏，其中红色{red}盏、绿色{green}盏")
        elif task == "person":
            people = [int(s.get("person") or 0) for s in samples]
            person = max(set(people), key=people.count)
            if person == 0:
                self._say_holding_lease(client, state, "视野内无人")
            else:
                self._say_holding_lease(client, state, f"视野内共{person}人")
