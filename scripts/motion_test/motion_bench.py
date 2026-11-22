"""H2 机器人功能测试台（motion bench），部署在 PC3。

逐条实测 /home/sia/unitree_H2_docs 文档声明的能力，每个测试项都标注出处文档，
调用方式严格照搬文档例程（初始化顺序、话题名、IDL 类型、字段名、参数格式）：

- 高层运动 RPC（04/02_高层运动开发/01_RPC例程、02_高层运动服务接口）：
  LocoClient 全量白名单调用。例程顺序：ChannelFactory Init → client.Init()
  → client.SetTimeout(10.f)。
- 里程计双话题（04/02_高层运动开发/03_里程计服务接口）：
  rt/odommodestate（500Hz）与 rt/lf/odommodestate（20Hz）同时订阅，
  展示文档定义的位置（世界系）/速度（机体系）/欧拉角/yaw 角速度/四元数，
  附行走跟踪录制分析（验证 position 是否积分实际行走）。
- 底层状态（04/01_底层运动开发/02_底层服务接口）：
  订阅 rt/lowstate（unitree_hg::LowState_）只读展示 IMUState_/MotorState_[35]/
  wireless_remote。不发布 rt/lowcmd——底层控制需进入调试模式（内置运控退出），
  风险高，不纳入本测试台。
- 关节命名与限位（04/01_底层运动开发/01_关节电机顺序）。
- 遥控器解码（04/01_底层运动开发/03_遥控器数据结构）：
  40 字节 wireless_remote 按 xRockerBtnDataStruct 解码。
- 音频/灯光/内置 ASR（05/02_音频灯光服务接口）：
  AudioClient（TtsMaker/GetVolume/SetVolume/PlayStop/LedControl）、
  rt/audio_msg 订阅、麦克风组播 239.168.123.161:5555 接收。
  软件版本要求 vui_service >= 2.3.0.1。
- 多媒体拍照（05/04_多媒体服务接口）：VideoClient.GetImageSample 取 JPEG。
  h264 图传需 gst 命令行接收，不纳入页面。

未纳入项见页面「覆盖清单」标签页的说明。
安全约定：运动指令一律由操作员在页面亲手触发；持续速度指令由服务端看护
线程在时限到达时强制 StopMove（这是测试台自身的保险，不影响被测接口行为；
可在页面关闭以复现文档原生语义）。

启动方式（PC3，与语音测试台同款部署）：
  cd ~/h2-inspection-platform && \
  CYCLONEDDS_HOME=$PWD/.runtime/h2-sdk2/cyclonedds-install \
  .venv/bin/python ~/motion_bench/motion_bench.py
页面：http://<PC3-IP>:8110/（H2_BENCH_PORT 改端口，H2_BENCH_IFACE 改网卡，
默认 eth10——即 PC3 与运控通信的内部网口）。
"""

from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import threading
import time
import wave
from collections import deque
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

BIND = os.getenv("H2_BENCH_BIND", "0.0.0.0")
PORT = int(os.getenv("H2_BENCH_PORT", "8110"))
IFACE = os.getenv("H2_BENCH_IFACE", "eth10")
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html")

RPC_TIMEOUT_S = 10.0         # 01_RPC例程：client.SetTimeout(10.f)
FSM_POLL_S = 1.0             # FSM 徽章刷新（GetFsmId 轮询）
FSM_POLL_BACKOFF_S = 5.0     # 上次查询失败后的退避（服务不答时别占着 RPC 锁）
GUARD_TICK_S = 0.05          # 自动停车看护线程周期
SERIES_WINDOW_S = 60.0       # 时间序列图表窗口
SERIES_POINTS = 600          # 时间序列抽稀目标点数
TRAJ_POINTS = 1200           # 轨迹抽稀目标点数
TRAJ_WINDOW_S = 300.0        # 轨迹窗口（5 分钟）
RECORD_MAX_ROWS = 150_000    # 行走录制上限（500Hz × 5 分钟）
IMU_TEST_SECONDS = 10.0      # IMU 静态测试采样时长
MOVE_MARGIN_S = 1.5          # SetVelocity 到期后的看护余量（双保险）

# 应用层限幅：文档说运控会自动裁剪速度，这里先拦一道防止页面误输入
VXY_LIMIT = 1.0              # m/s
VYAW_LIMIT = 2.0             # rad/s
DURATION_LIMIT_S = 30.0      # SetVelocity duration 上限
AUTOSTOP_LIMIT_S = 60.0      # 持续模式自动停车上限

# FSM 编号 → 界面用语（02_高层运动服务接口「模式ID说明」表）
FSM_NAMES = {0: "零力矩", 1: "阻尼", 2: "蹲下", 3: "落座", 4: "锁定站立",
             601: "走跑运控", 701: "常规运控(≥2.5.4.11)", 703: "常规运控", 802: "起身",
             803: "躺下"}

# 关节命名与限位 rad（01_关节电机顺序 表，35 槽位中 31-34 预留无数据）
JOINTS: list[dict] = []
_JOINT_TABLE = [
    ("左腿俯仰", -2.4525975, 2.7754225), ("左腿横滚", -0.467441, 2.1688605),
    ("左腿偏航", -2.827, 2.827), ("左膝俯仰", -0.08725, 2.53025),
    ("左踝横滚", -0.3490658503, 0.296705972783), ("左踝俯仰", -1.13446401358, 0.61086523808),
    ("右腿俯仰", -2.4525975, 2.7754225), ("右腿横滚", -2.1688605, 0.467441),
    ("右腿偏航", -2.827, 2.827), ("右膝俯仰", -0.08725, 2.53025),
    ("右踝横滚", -0.296705972783, 0.3490658503), ("右踝俯仰", -1.13446401358, 0.61086523808),
    ("腰偏航", -1.7453, 1.7453), ("腰横滚A", -0.5236, 0.5236), ("腰俯仰B", -0.43633, 0.5236),
    ("左大臂俯仰", -2.6179939, 1.8325957), ("左大臂横滚", -0.517, 2.494),
    ("左大臂偏航", -2.6179939, 2.6179939), ("左肘俯仰", -0.986111, 3.0717795),
    ("左小臂横滚", -2.6179939, 2.6179939), ("左小臂俯仰", -0.576, 0.576),
    ("左小臂偏航", -1.2217305, 1.2217305),
    ("右大臂俯仰", -2.6179939, 1.8325957), ("右大臂横滚", -2.494, 0.517),
    ("右大臂偏航", -2.6179939, 2.6179939), ("右肘俯仰", -0.986111, 3.0717795),
    ("右小臂横滚", -2.6179939, 2.6179939), ("右小臂俯仰", -0.576, 0.576),
    ("右小臂偏航", -1.2217305, 1.2217305),
    ("脖子俯仰", -0.5236, 0.83775), ("脖子偏航", -1.7453, 1.7453),
]
JOINTS = [{"i": i, "name": name, "lo": lo, "hi": hi} for i, (name, lo, hi)
          in enumerate(_JOINT_TABLE)]
JOINTS += [{"i": i, "name": f"预留{i}", "lo": None, "hi": None} for i in range(31, 35)]

# 遥控器按键位序（03_遥控器数据结构 xKeySwitchUnion，LSB 起）
REMOTE_KEYS = ["R1", "L1", "start", "select", "R2", "L2", "F1", "F2",
               "A", "B", "X", "Y", "up", "right", "down", "left"]

BOOT_ID = uuid4().hex[:8]
START_MONO = time.monotonic()
IDL_ERRORS: list[str] = []   # 启动期订阅/导入错误，页面顶部展示


def clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def clamp_range(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ================= DDS 初始化（05/01_DDS通信接口 + 各例程） =================

def dds_init() -> None:
    """ChannelFactory::Instance()->Init(0, network_interface) 的 sdk2py 对应写法。

    额外摘除 SDK 默认 CycloneDDS 配置里的 Tracing 段——否则初始化慢且刷日志；
    这不改变文档规定的初始化参数（domainId=0，网卡名）。
    """
    from unitree_sdk2py.core import channel
    from xml.etree import ElementTree

    original = channel.ChannelConfigHasInterface
    config = ElementTree.fromstring(original)
    for entry in config.findall("./Domain"):
        tracing = entry.find("Tracing")
        if tracing is not None:
            entry.remove(tracing)
    channel.ChannelConfigHasInterface = ElementTree.tostring(config, encoding="unicode")
    try:
        channel.ChannelFactoryInitialize(0, IFACE)
    finally:
        channel.ChannelConfigHasInterface = original


class Watch:
    """通用话题观察器：最新样本 + 频率统计 + 环形历史（图表抽稀用）。

    对应文档 ChannelSubscriber 的用法：构造 (topic, 类型) → Init(回调, 1)。
    频率用回调计数法（近 2 秒计数 / 2），不依赖样本自带时间戳。
    history_fields 限定进历史的字段（图表所需）：完整样本里的重组件（如 35 个
    电机字典）只保留在最新快照，历史里只存瘦副本——否则 700Hz×35 字典的分配
    与 GC 会吃满一个核、内存涨到 GB 级（实测教训），并拖死本进程的 DDS 线程。
    传空列表则完全不记历史。
    """

    def __init__(self, topic: str, import_path: str, type_name: str, unpack,
                 history_fields: list[str] | None = None, decimate: int = 1):
        self.topic = topic
        self.ok = False
        self._unpack = unpack
        self._history_fields = history_fields
        self._decimate = max(1, decimate)   # 每 N 帧才解包 1 帧（重 IDL 解码抽稀）
        self._lock = threading.Lock()
        self._latest: tuple[float, dict] | None = None
        self._history: deque[tuple[float, dict]] = deque(maxlen=35_000)
        self._ticks: deque[float] = deque(maxlen=2500)
        self._count = 0
        try:
            from unitree_sdk2py.core.channel import ChannelSubscriber
            module = __import__(import_path, fromlist=[type_name])
            subscriber = ChannelSubscriber(topic, getattr(module, type_name))
            subscriber.Init(self._on_message, 1)
            self.ok = True
        except Exception as error:  # noqa: BLE001 - 启动错误展示到页面而不是崩溃
            IDL_ERRORS.append(f"{topic}: {type_name} 订阅失败 {error!r}")

    def _on_message(self, message) -> None:
        now = time.monotonic()
        with self._lock:
            self._ticks.append(now)
            self._count += 1
            skip = self._decimate > 1 and self._count % self._decimate != 0
        if skip:
            return   # 本帧只计数（频率统计仍准确），不解码省 CPU
        try:
            sample = self._unpack(message)
        except Exception:  # noqa: BLE001 - 单字段异常不该杀回调线程
            return
        slim = ({key: sample[key] for key in self._history_fields if key in sample}
                if self._history_fields else None)
        with self._lock:
            self._latest = (now, sample)
            if slim is not None:
                self._history.append((now, slim))

    def snapshot(self) -> dict | None:
        with self._lock:
            reading = self._latest
            ticks = len(self._ticks)
            span = (self._ticks[-1] - self._ticks[0]) if ticks >= 2 else 0.0
            count = self._count
        if reading is None:
            return None
        hz = (ticks - 1) / span if span > 0.5 else 0.0
        return {**reading[1], "age_ms": int((time.monotonic() - reading[0]) * 1000),
                "hz": round(hz, 1), "total": count}

    def series(self, fields: list[str], points: int = SERIES_POINTS) -> list:
        """窗口内抽稀的时间序列：[[t 相对启动, f1, f2...], ...]。"""
        with self._lock:
            cutoff = time.monotonic() - SERIES_WINDOW_S
            rows = [row for row in self._history if row[0] >= cutoff]
        step = max(1, len(rows) // points)
        return [[round(now - START_MONO, 2)] + [sample.get(f) for f in fields]
                for now, sample in rows[::step]]

    def history_since(self, seconds: float) -> list[tuple[float, dict]]:
        with self._lock:
            cutoff = time.monotonic() - seconds
            return [row for row in self._history if row[0] >= cutoff]


# ================= 消息解包（字段名逐一对应文档） =================

def unpack_odom(msg) -> dict:
    """SportModeState_（03_里程计服务接口）：位置/速度/欧拉角/yaw角速度/四元数。"""
    imu = getattr(msg, "imu_state", None)
    rpy = [float(v) for v in getattr(imu, "rpy", (0, 0, 0))] if imu is not None else [0.0] * 3
    gyro = [float(v) for v in (getattr(imu, "gyroscope", None)
                               or getattr(imu, "gyro", (0, 0, 0)))] if imu is not None else [0.0] * 3
    quat = [float(v) for v in getattr(imu, "quaternion", (1, 0, 0, 0))] \
        if imu is not None else [1.0, 0.0, 0.0, 0.0]
    vx, vy, vz = (float(v) for v in msg.velocity)
    return {
        "x": float(msg.position[0]), "y": float(msg.position[1]), "z": float(msg.position[2]),
        "vx": vx, "vy": vy, "vz": vz,
        "speed": round(math.hypot(vx, vy), 4),   # 机体系速度模长（图表用）
        "roll": rpy[0], "pitch": rpy[1], "yaw": rpy[2],
        "yaw_rate": gyro[2], "gyro": gyro, "quat": quat,
    }


def unpack_lowstate(msg) -> dict:
    """LowState_（02_底层服务接口）：IMUState + MotorState[35] + wireless_remote。"""
    imu = msg.imu_state
    motors = []
    for index, motor in enumerate(msg.motor_state):
        temp = list(getattr(motor, "temperature", [0, 0]))
        motors.append({
            "i": index,
            "name": JOINTS[index]["name"] if index < len(JOINTS) else str(index),
            "mode": int(getattr(motor, "mode", 0)),
            "q": round(float(motor.q), 4), "dq": round(float(motor.dq), 3),
            "ddq": round(float(getattr(motor, "ddq", 0.0)), 2),
            "tau": round(float(getattr(motor, "tau_est", 0.0)), 2),
            "t1": int(temp[0]) if len(temp) > 0 else 0,
            "t2": int(temp[1]) if len(temp) > 1 else 0,
            "vol": round(float(getattr(motor, "vol", 0.0)), 1),
            "motorstate": int(getattr(motor, "motorstate", 0)),
        })
    # 遥控器 40 字节 → xRockerBtnDataStruct：head[2] + 按键u16 + lx,rx,ry,L2,ly + idle[16]
    raw = bytes(msg.wireless_remote[:40])
    try:
        _, keys, lx, rx, ry, l2, ly, _ = struct.unpack("<2sH5f16s", raw)
        remote = {"lx": round(lx, 3), "ly": round(ly, 3), "rx": round(rx, 3),
                  "ry": round(ry, 3), "l2": round(l2, 3), "keys": keys,
                  "pressed": [name for bit, name in enumerate(REMOTE_KEYS) if keys >> bit & 1],
                  "raw": raw.hex()}
    except struct.error:
        remote = None
    gyro = [float(v) for v in imu.gyroscope]
    return {
        "quat": [float(v) for v in imu.quaternion],
        "gyro": gyro,
        # 标量副本：时间序列接口按列取值，数组列画不了图
        "gx": gyro[0], "gy": gyro[1], "gz": gyro[2],
        "accel": [float(v) for v in imu.accelerometer],
        "rpy": [float(v) for v in imu.rpy],
        "imu_temp": float(getattr(imu, "temperature", 0)),
        "motors": motors,
        "remote": remote,
    }


def unpack_string(msg) -> dict:
    """std_msgs String_（音频灯光文档 rt/audio_msg）：data 为 JSON 文本。"""
    text = str(msg.data)
    payload: dict | None = None
    with suppress(json.JSONDecodeError):
        payload = json.loads(text)
    return {"text": text, "json": payload}


# ================= 高层运动 RPC（01_RPC例程 + 02_高层运动服务接口） =================

class Loco:
    """LocoClient 封装：互斥调用 + 回执日志 + 持续速度安全看护。

    初始化顺序照搬 01_RPC例程：client.Init() 之后 client.SetTimeout(10.f)。
    2026-11-22 实测：网关进程同时运行时，第二个客户端的 SetVelocity 可能
    不被应答（返回 None），只读查询正常——页面顶栏常驻网关状态提示。
    """

    def __init__(self) -> None:
        self.client = None
        self.error: str | None = None
        self.lock = threading.RLock()
        self.log: deque[dict] = deque(maxlen=40)
        self.api_version: str | None = None
        self._guard_at: float | None = None
        self._guard_reason = ""
        self.guard_enabled = True          # 页面可关闭以复现文档原生持续语义
        self.fsm: dict | None = None
        self._fsm_last_fail = 0.0
        try:
            from unitree_sdk2py.h2.loco.h2_loco_client import LocoClient
            self.client = LocoClient()
            self.client.Init()
            self.client.SetTimeout(RPC_TIMEOUT_S)
            for probe in ("GetServerApiVersion", "ApiVersion"):
                if hasattr(self.client, probe):
                    with suppress(Exception):
                        self.api_version = str(getattr(self.client, probe)())
                    break
        except Exception as error:  # noqa: BLE001
            self.error = repr(error)

    # -- 安全看护：持续速度指令到期强制 StopMove（页面关闭也生效） --
    # 注意：_guard_at/_guard_reason/fsm 都是单赋值原子读写，刻意不加锁——
    # 曾因 FSM 轮询持锁 10s 超时把 /api/state 拖到 9s 才响应。
    def guard_loop(self) -> None:
        while True:
            time.sleep(GUARD_TICK_S)
            deadline = self._guard_at
            if deadline is None or time.monotonic() < deadline:
                continue
            self._guard_at = None
            self.call("StopMove", {}, source="看护停车：" + self._guard_reason)

    def arm_guard(self, seconds: float, reason: str) -> None:
        self._guard_at = time.monotonic() + seconds
        self._guard_reason = reason

    def guard_left_ms(self) -> int | None:
        deadline = self._guard_at
        if deadline is None:
            return None
        return max(0, int((deadline - time.monotonic()) * 1000))

    # -- FSM 轮询（GetFsmId/GetFsmMode，页面徽章） --
    def fsm_loop(self) -> None:
        fails = 0
        while True:
            if time.monotonic() - self._fsm_last_fail < FSM_POLL_BACKOFF_S:
                time.sleep(FSM_POLL_S)
                continue
            try:
                fsm_id = self.call("GetFsmId", {}, source="轮询", quiet=True).get("data")
                mode = self.call("GetFsmMode", {}, source="轮询", quiet=True).get("data")
                if fsm_id is None:
                    raise RuntimeError("GetFsmId 无应答")
                self.fsm = {"id": int(fsm_id),
                            "mode": int(mode) if mode is not None else None,
                            "ts": time.time()}
                fails = 0
            except Exception:  # noqa: BLE001
                self._fsm_last_fail = time.monotonic()
                self.fsm = None
                fails += 1
                # 运控服务切换/重启后，常驻客户端的 RPC 端点会失效（一次性脚本
                # 无此问题）；连续失败三次即重建客户端。
                if fails >= 3:
                    with self.lock:
                        self._reinit()
                    fails = 0
            time.sleep(FSM_POLL_S)

    def _reinit(self) -> None:
        """重建 LocoClient（服务端点失效自愈）。调用方需持有 self.lock。"""
        try:
            from unitree_sdk2py.h2.loco.h2_loco_client import LocoClient
            client = LocoClient()
            client.Init()
            client.SetTimeout(RPC_TIMEOUT_S)
            self.client = client
            self.error = None
        except Exception as err:  # noqa: BLE001
            self.error = repr(err)

    # -- 统一调用入口 --
    def call(self, method: str, args: dict, *, source: str = "页面",
             quiet: bool = False) -> dict:
        with self.lock:
            started = time.monotonic()
            error: str | None = None
            result: dict = {}
            if self.client is None:
                error = self.error or "LocoClient 未初始化"
            else:
                try:
                    result = self._dispatch(method, args)
                except Exception as err:  # noqa: BLE001 - 异常与回执都进日志
                    error = repr(err)
            entry = {"t": round(time.time() % 86_400, 1), "source": source,
                     "method": method, "args": args,
                     "effective": result.get("effective"),
                     "code": result.get("code"), "data": result.get("data"),
                     "elapsed_ms": int((time.monotonic() - started) * 1000),
                     "error": error}
            if not quiet:
                self.log.appendleft(entry)
            return {**entry, "ok": error is None and result.get("code") == 0}

    def _dispatch(self, method: str, args: dict) -> dict:
        c = self.client
        # 查询（01_RPC例程 --get_* 参数）
        if method == "GetFsmId":
            code, data = c.GetFsmId()
            return {"code": code, "data": data}
        if method == "GetFsmMode":
            code, data = c.GetFsmMode()
            return {"code": code, "data": data}
        if method == "GetAvailableFsmIds":
            code, ids, names = c.GetAvailableFsmIds()
            return {"code": code, "data": [[int(i), str(n)] for i, n in zip(ids, names)]}
        if method == "GetArmSdkStatus":
            code, data = c.GetArmSdkStatus()
            return {"code": code, "data": data}
        if method == "GetBalanceMode":
            code, data = c.GetBalanceMode()
            return {"code": code, "data": data}
        if method == "GetStandHeight":
            code, data = c.GetStandHeight()
            return {"code": code, "data": data}
        if method == "GetSwingHeight":
            code, data = c.GetSwingHeight()
            return {"code": code, "data": data}
        # 运动（02_高层运动服务接口：Move/SetVelocity/StopMove）
        if method == "Move":
            vx = clamp(float(args.get("vx", 0)), VXY_LIMIT)
            vy = clamp(float(args.get("vy", 0)), VXY_LIMIT)
            vyaw = clamp(float(args.get("vyaw", 0)), VYAW_LIMIT)
            continuous = bool(args.get("continous", False))
            code = c.Move(vx, vy, vyaw, continuous)
            if continuous and self.guard_enabled:
                self.arm_guard(clamp_range(float(args.get("auto_stop_s", 5)), 1.0,
                                           AUTOSTOP_LIMIT_S),
                               f"Move(continuous) ({vx},{vy},{vyaw})")
            return {"code": code,
                    "effective": {"vx": vx, "vy": vy, "vyaw": vyaw,
                                  "continous": continuous}}
        if method == "SetVelocity":
            vx = clamp(float(args.get("vx", 0)), VXY_LIMIT)
            vy = clamp(float(args.get("vy", 0)), VXY_LIMIT)
            omega = clamp(float(args.get("omega", 0)), VYAW_LIMIT)
            duration = clamp_range(float(args.get("duration", 1.0)), 0.1, DURATION_LIMIT_S)
            code = c.SetVelocity(vx, vy, omega, duration)
            # duration 自过期，看护只做双保险（可关闭时也不拦自过期指令）
            if self.guard_enabled:
                self.arm_guard(duration + MOVE_MARGIN_S, f"SetVelocity dur={duration}")
            return {"code": code,
                    "effective": {"vx": vx, "vy": vy, "omega": omega,
                                  "duration": duration}}
        if method == "StopMove":
            code = c.StopMove()
            self._guard_at = None
            return {"code": code}
        # 模式切换（01_RPC例程 --start/--damp/... 与 02 接口表）
        if method == "Start":
            return {"code": c.Start()}
        if method == "StandUp":
            return {"code": c.StandUp()}
        if method == "Squat":
            return {"code": c.Squat()}
        if method == "Sit":
            return {"code": c.Sit()}
        if method == "Damp":
            return {"code": c.Damp()}
        if method == "ZeroTorque":
            return {"code": c.ZeroTorque()}
        if method == "SetFsmId":
            return {"code": c.SetFsmId(int(args["id"]))}
        if method == "SwitchMoveMode":
            return {"code": c.SwitchMoveMode(bool(args.get("flag", False)))}
        if method == "BalanceStand":
            return {"code": c.BalanceStand()}
        if method == "ContinuousGait":
            return {"code": c.ContinuousGait(bool(args.get("flag", False)))}
        raise ValueError(f"未知方法 {method}")


# RPC 白名单（前端渲染按钮与确认级别；全部对应文档接口表/例程参数）
RPC_TABLE = {
    "GetFsmId": {"category": "查询", "desc": "当前状态机 ID"},
    "GetFsmMode": {"category": "查询", "desc": "主运控 0=站立 1=移动"},
    "GetAvailableFsmIds": {"category": "查询", "desc": "固件支持的 FSM 列表"},
    "GetArmSdkStatus": {"category": "查询", "desc": "手臂 SDK 开关状态"},
    "GetBalanceMode": {"category": "查询", "desc": "平衡/连续步态模式"},
    "GetStandHeight": {"category": "查询", "desc": "站立高度"},
    "GetSwingHeight": {"category": "查询", "desc": "摆腿高度"},
    "Move": {"category": "运动", "desc": "Move(vx,vy,vyaw,continous_move)"},
    "SetVelocity": {"category": "运动", "desc": "SetVelocity(vx,vy,ω,duration=1)"},
    "StopMove": {"category": "运动", "desc": "速度置零"},
    "Start": {"category": "模式", "desc": "进入主运控 601"},
    "StandUp": {"category": "模式", "desc": "锁定站立 4"},
    "Squat": {"category": "模式", "desc": "蹲下 2"},
    "Sit": {"category": "模式", "desc": "落座 3"},
    "SetFsmId": {"category": "模式", "desc": "SetFsmId(id)，含专家模式 701/703"},
    "SwitchMoveMode": {"category": "模式", "desc": "Move 持续响应开关（默认关）"},
    "BalanceStand": {"category": "模式", "desc": "平衡站立（必要时才踏步）"},
    "ContinuousGait": {"category": "模式", "desc": "连续步态（零速也保持踏步）"},
    "Damp": {"category": "危险", "desc": "阻尼模式：关节松弛，机器人会瘫倒"},
    "ZeroTorque": {"category": "危险", "desc": "零力矩：无平衡控制，会倒"},
}


# ================= 音频/灯光（05/02_音频灯光服务接口） =================

class Audio:
    """AudioClient 封装（TtsMaker/音量/PlayStop/LedControl）。

    文档要求 vui_service >= 2.3.0.1；sdk2py 是否含该客户端以现场探测为准，
    导入失败时面板显示原因——「SDK 未提供」本身也是一条实测结论。
    """

    IMPORT_PATHS = [
        "unitree_sdk2py.g1.audio.g1_audio_client:AudioClient",
        "unitree_sdk2py.audio.audio_client:AudioClient",
    ]

    def __init__(self) -> None:
        self.client = None
        self.error: str | None = None
        self.lock = threading.Lock()
        self.log: deque[dict] = deque(maxlen=20)
        for spec in self.IMPORT_PATHS:
            module_path, class_name = spec.split(":")
            try:
                module = __import__(module_path, fromlist=[class_name])
                client = getattr(module, class_name)()
                client.Init()
                client.SetTimeout(10.0)
                self.client = client
                self.error = None
                return
            except Exception as err:  # noqa: BLE001 - 逐个探测，记录最后错误
                self.error = f"{module_path}: {err!r}"
        if self.client is None and self.error is None:
            self.error = "未找到可用的 AudioClient 导入路径"

    def available(self) -> bool:
        return self.client is not None

    def call(self, method: str, args: dict) -> dict:
        with self.lock:
            started = time.monotonic()
            error: str | None = None
            code = None
            data = None
            if self.client is None:
                error = self.error or "AudioClient 不可用"
            else:
                try:
                    if method == "GetVolume":
                        code, data = self.client.GetVolume()
                    elif method == "SetVolume":
                        code = self.client.SetVolume(max(0, min(100, int(args.get("volume", 50)))))
                    elif method == "TtsMaker":
                        text = str(args.get("text", ""))[:200]
                        speaker = int(args.get("speaker_id", 0))
                        code = self.client.TtsMaker(text, speaker)
                    elif method == "PlayStop":
                        code = self.client.PlayStop(str(args.get("app_name", "bench")))
                    elif method == "LedControl":
                        r = max(0, min(255, int(args.get("r", 0))))
                        g = max(0, min(255, int(args.get("g", 0))))
                        b = max(0, min(255, int(args.get("b", 0))))
                        code = self.client.LedControl(r, g, b)
                    else:
                        raise ValueError(f"未知方法 {method}")
                except Exception as err:  # noqa: BLE001
                    error = repr(err)
            entry = {"t": round(time.time() % 86_400, 1), "method": method, "args": args,
                     "code": code, "data": data,
                     "elapsed_ms": int((time.monotonic() - started) * 1000),
                     "error": error}
            self.log.appendleft(entry)
            return {**entry, "ok": error is None and code == 0}


class MicMulticast:
    """麦克风组播接收（音频灯光文档：239.168.123.161:5555，16k/16bit/单通道）。

    照搬例程逻辑：加入组播组时绑定 192.168.123.* 本机地址（取不到则退回
    INADDR_ANY）；监听线程统计码率，录音模式攒够时长生成内存 WAV 供下载。
    """

    GROUP = "239.168.123.161"
    PORT = 5555

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.listening = False
        self.error: str | None = None
        self.bytes_per_s = 0.0
        self._bytes_window: deque[tuple[float, int]] = deque(maxlen=200)
        self._recording_until: float | None = None
        self._recording: list[bytes] = []
        self._record_len = 0
        self.last_wav: bytes | None = None
        self._sock = None

    @staticmethod
    def _local_ip() -> str:
        """例程的 get_local_ip_for_multicast：优先 192.168.123.* 地址。"""
        import socket
        hostname = socket.gethostname()
        try:
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                if ip.startswith("192.168.123."):
                    return ip
        except socket.gaierror:
            pass
        # getaddrinfo 拿不到时用 UDP 连接法取默认路由口地址
        with suppress(OSError):
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("239.255.255.250", 1))
            return probe.getsockname()[0]
        return "0.0.0.0"

    def start(self) -> dict:
        import socket
        with self._lock:
            if self.listening:
                return {"ok": True}
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", self.PORT))
                if hasattr(socket, "IP_ADD_MEMBERSHIP"):
                    group = socket.inet_aton(self.GROUP)
                    local = socket.inet_aton(self._local_ip())
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                    group + local)
                sock.settimeout(0.2)
                self._sock = sock
                self.listening = True
                self.error = None
            except OSError as err:
                self.error = repr(err)
                return {"ok": False, "error": self.error}
        threading.Thread(target=self._loop, daemon=True, name="h2-mic-mcast").start()
        return {"ok": True}

    def stop(self) -> dict:
        with self._lock:
            self.listening = False
            sock, self._sock = self._sock, None
        if sock is not None:
            with suppress(OSError):
                sock.close()
        return {"ok": True}

    def record(self, seconds: float) -> dict:
        with self._lock:
            if not self.listening:
                return {"error": "未在监听，先开始监听"}
            self._recording = []
            self._record_len = 0
            self._recording_until = time.monotonic() + max(1.0, min(30.0, seconds))
            self.last_wav = None
        return {"ok": True, "seconds": seconds}

    def _loop(self) -> None:
        import socket
        while True:
            with self._lock:
                sock = self._sock
                listening = self.listening
            if sock is None or not listening:
                time.sleep(0.2)
                continue
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                time.sleep(0.5)
                continue
            now = time.monotonic()
            with self._lock:
                self._bytes_window.append((now, len(data)))
                if self._recording_until is not None:
                    self._recording.append(data)
                    self._record_len += len(data)
                    if now >= self._recording_until:
                        self.last_wav = self._build_wav()
                        self._recording_until = None
                        self._recording = []
                cutoff = now - 3.0
                total = sum(size for ts, size in self._bytes_window if ts >= cutoff)
                span = min(3.0, max(0.5, now - self._bytes_window[0][0])) \
                    if self._bytes_window else 1.0
                self.bytes_per_s = round(total / span, 0)

    def _build_wav(self) -> bytes:
        import io
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"".join(self._recording))
        return buffer.getvalue()

    def status(self) -> dict:
        with self._lock:
            return {"listening": self.listening, "error": self.error,
                    "bytes_per_s": self.bytes_per_s,
                    "recording": self._recording_until is not None,
                    "wav_ready": self.last_wav is not None,
                    "wav_bytes": len(self.last_wav) if self.last_wav else 0}


# ================= 多媒体拍照（05/04_多媒体服务接口） =================

class Video:
    """VideoClient.GetImageSample() 拍照（文档例程：SetTimeout(1.f) 后 Init）。

    h264 图传（UDP 组播 230.1.1.1:1720）需 gst-launch 接收，不纳入页面，
    覆盖清单里给出文档原命令。
    """

    def __init__(self) -> None:
        self.client = None
        self.error: str | None = None
        self.lock = threading.Lock()
        try:
            from unitree_sdk2py.go2.video.video_client import VideoClient
            client = VideoClient()
            client.SetTimeout(1.0)
            client.Init()
            self.client = client
        except Exception as err:  # noqa: BLE001
            self.error = repr(err)

    def available(self) -> bool:
        return self.client is not None

    def snapshot(self) -> tuple[int, bytes] | None:
        """返回 (http 状态, JPEG 字节)；失败时 None 或 (503, 错误 json)。"""
        with self.lock:
            if self.client is None:
                return None
            try:
                code, image = self.client.GetImageSample()
                if code == 0 and image:
                    return 200, bytes(image)
                return None
            except Exception:  # noqa: BLE001
                return None


# ================= 行走录制分析（03_里程计服务接口的实测判定） =================

class Recorder:
    """里程计行走录制：验证文档声明的能力——position 是否随行走积分。

    分析口径与 2026-11-22 手工实验一致：position 取运动窗口首尾各 1 秒均值，
    速度按文档为机体系量测，旋转到世界系后做梯形积分得路程。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.on = False
        self.rows: list[tuple] = []
        self.started_at: float | None = None
        self.summary: dict | None = None

    def start(self) -> None:
        with self._lock:
            self.rows = []
            self.on = True
            self.started_at = time.monotonic()
            self.summary = None

    def stop(self) -> None:
        with self._lock:
            self.on = False

    def feed(self, sample: dict) -> None:
        with self._lock:
            if self.on and len(self.rows) < RECORD_MAX_ROWS:
                self.rows.append((time.monotonic(), sample["x"], sample["y"], sample["z"],
                                  sample["vx"], sample["vy"], sample["vz"],
                                  sample["roll"], sample["pitch"], sample["yaw"]))

    def status(self) -> dict:
        with self._lock:
            duration = ((time.monotonic() - self.started_at) if self.on and self.started_at
                        else (len(self.rows) / 500.0 if self.rows else 0.0))
            return {"on": self.on, "rows": len(self.rows), "duration_s": round(duration, 1),
                    "summary": self.summary}

    def analyze(self) -> dict:
        with self._lock:
            rows = list(self.rows)
        if len(rows) < 500:
            return {"error": f"样本不足（{len(rows)} 帧，至少 1 秒）"}
        speeds = [math.hypot(r[4], r[5]) for r in rows]
        base = {"rows": len(rows), "duration_s": round(rows[-1][0] - rows[0][0], 1),
                "peak_speed": round(max(speeds), 3)}
        moving = [i for i, s in enumerate(speeds) if s > 0.05]
        if not moving:
            return {**base, "error": "未检测到运动段（速度始终 < 0.05 m/s）"}
        a, b = moving[0], moving[-1]
        b_end = min(len(rows) - 1, b + 500)

        def window_mean(center: int) -> tuple[float, float]:
            lo = max(0, center - 500)
            xs = [rows[i][1] for i in range(lo, center)]
            ys = [rows[i][2] for i in range(lo, center)]
            return sum(xs) / len(xs), sum(ys) / len(ys)

        x0, y0 = window_mean(a)
        x1, y1 = window_mean(b_end)
        pos_dist = math.hypot(x1 - x0, y1 - y0)
        path = net_x = net_y = 0.0
        for i in range(a, b_end):
            dt = rows[i + 1][0] - rows[i][0]
            for j in (i, i + 1):
                c, s = math.cos(rows[j][9]), math.sin(rows[j][9])
                net_x += 0.5 * dt * (rows[j][4] * c - rows[j][5] * s)
                net_y += 0.5 * dt * (rows[j][4] * s + rows[j][5] * c)
                path += 0.5 * dt * math.hypot(rows[j][4], rows[j][5])
        ratio = pos_dist / path if path > 0.01 else 0.0
        yaw_change = math.remainder(rows[b_end][9] - rows[a][9], 2 * math.pi)
        if path > 0.3 and ratio < 0.5:
            verdict = "❌ position 未积分实际行走（速度有读数但位置不累积）"
        elif path > 0.3 and ratio > 0.8:
            verdict = "✅ position 与速度积分一致，里程计正常跟踪行走"
        elif path <= 0.3:
            verdict = "⚠ 运动量太小（速度积分路程 < 0.3m），建议再走一段"
        else:
            verdict = f"⚠ position 约为速度积分路程的 {ratio:.0%}，疑似尺度偏差"
        summary = {**base,
                   "moving_window_s": [round(rows[a][0] - rows[0][0], 1),
                                       round(rows[b][0] - rows[0][0], 1)],
                   "pos_displacement_m": round(pos_dist, 3),
                   "vel_path_integral_m": round(path, 3),
                   "vel_net_integral_m": round(math.hypot(net_x, net_y), 3),
                   "pos_over_path": round(ratio, 3),
                   "yaw_change_rad": round(yaw_change, 3),
                   "verdict": verdict}
        with self._lock:
            self.summary = summary
        return summary

    def csv_bytes(self) -> bytes:
        with self._lock:
            rows = list(self.rows)
        lines = ["t,x,y,z,vx,vy,vz,roll,pitch,yaw"]
        lines += [",".join(f"{v:.4f}" if isinstance(v, float) else str(v) for v in r)
                  for r in rows]
        return ("\n".join(lines) + "\n").encode()


# ================= IMU 静态测试（02_底层服务接口 IMUState_ 实测） =================

class ImuTest:
    """10 秒静态采样：陀螺零偏 / 加计模长 / yaw 稳定度 / 温度。

    数据源 rt/lowstate 的 IMUState_（底层原始输出）；lowstate 不可用时
    退化用里程计话题的 imu_state 并标注来源。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.report: dict | None = None
        self._samples: list[tuple] = []

    def start(self, lowstate_watch: Watch, odom_watch: Watch) -> dict:
        with self._lock:
            if self.running:
                return {"error": "测试进行中"}
            self.running = True
            self._samples = []
        threading.Thread(target=self._run, args=(lowstate_watch, odom_watch),
                         daemon=True, name="h2-imu-test").start()
        return {"started": True}

    def _run(self, lowstate_watch: Watch, odom_watch: Watch) -> None:
        source = "lowstate"
        started = time.monotonic()
        deadline = started + IMU_TEST_SECONDS
        while time.monotonic() < deadline:
            sample = lowstate_watch.snapshot() if lowstate_watch.ok else None
            if sample is None:
                sample = odom_watch.snapshot()
                source = "odommodestate"
            if sample is not None and "gyro" in sample:
                accel = sample.get("accel")
                self._samples.append((
                    time.monotonic(),
                    sample["gyro"][0], sample["gyro"][1], sample["gyro"][2],
                    sample["rpy"][2] if "rpy" in sample else sample.get("yaw", 0.0),
                    math.hypot(*accel) if accel else None))
            elif time.monotonic() - started > 2.0 and not self._samples:
                # 两个数据源都 2 秒无样本：快速失败，别让页面干等 10 秒
                report = {"error": "2 秒内未采到 IMU 样本（rt/lowstate 与里程计均无数据）",
                          "source": "none"}
                with self._lock:
                    self.running = False
                    self.report = report
                return
            time.sleep(0.002)
        report = self._analyze(source)
        with self._lock:
            self.running = False
            self.report = report

    def _analyze(self, source: str) -> dict:
        rows = self._samples
        n = len(rows)
        if n < 200:
            return {"error": f"采样不足（{n} 帧），检查 IMU 数据源", "source": source}
        dur = rows[-1][0] - rows[0][0]
        dt = dur / (n - 1)
        means = [sum(r[i] for r in rows) / n for i in (1, 2, 3)]
        stds = [math.sqrt(sum((r[i] - means[k]) ** 2 for r in rows) / n)
                for k, i in enumerate((1, 2, 3))]
        drift = sum(r[3] for r in rows) * dt          # z 轴陀螺积分
        accels = [r[5] for r in rows if r[5] is not None]
        accel_mean = sum(accels) / len(accels) if accels else None
        yaw0 = rows[0][4]
        yaw_span = max(abs(math.remainder(r[4] - yaw0, 2 * math.pi)) for r in rows)
        notes = []
        if max(abs(m) for m in means) > math.radians(0.5):
            notes.append("陀螺零偏 > 0.5°/s：积分前必须先校零")
        else:
            notes.append(f"陀螺零偏 ≤ 0.5°/s：分钟级短时积分可用")
        if accel_mean is not None and abs(accel_mean - 9.81) > 0.15:
            notes.append(f"加计模长 {accel_mean:.2f} m/s² 偏离重力 9.81")
        elif accel_mean is not None:
            notes.append("加计模长与重力一致")
        return {"source": source, "frames": n, "duration_s": round(dur, 1),
                "gyro_mean_dps": [round(math.degrees(m), 3) for m in means],
                "gyro_std_dps": [round(math.degrees(s), 3) for s in stds],
                "gyro_z_drift_deg_min": round(math.degrees(drift) / dur * 60, 2),
                "accel_mean_ms2": round(accel_mean, 3) if accel_mean is not None else None,
                "yaw_span_deg": round(math.degrees(yaw_span), 3),
                "notes": notes, "ts": time.time()}

    def status(self) -> dict:
        with self._lock:
            return {"running": self.running, "report": self.report}


# ================= 装配 =================

print(f"motion bench 启动：iface={IFACE} bind={BIND}:{PORT}", flush=True)
dds_init()
# 历史字段按图表需要瘦身：电机/遥控器等重组件只进最新快照（见 Watch 注释）
ODOM_HF = Watch("rt/odommodestate", "unitree_sdk2py.idl.unitree_go.msg.dds_",
                "SportModeState_", unpack_odom,
                history_fields=["x", "y", "speed", "yaw"])
ODOM_LF = Watch("rt/lf/odommodestate", "unitree_sdk2py.idl.unitree_go.msg.dds_",
                "SportModeState_", unpack_odom)
LOWSTATE = Watch("rt/lowstate", "unitree_sdk2py.idl.unitree_hg.msg.dds_",
                 "LowState_", unpack_lowstate, history_fields=["gx", "gy", "gz"],
                 decimate=5)   # ~800Hz 全解码会吃满一核，显示用 160Hz 足够
AUDIO_MSG = Watch("rt/audio_msg", "unitree_sdk2py.idl.std_msgs.msg.dds_",
                  "String_", unpack_string)
LOCO = Loco()
AUDIO = Audio()
VIDEO = Video()
MIC = MicMulticast()
RECORDER = Recorder()
IMU_TEST = ImuTest()

TRAJ_ORIGIN: dict[str, float] | None = None   # 轨迹图清零点（None=首个样本自动定）

# 行走录制挂在 odom 解包之后（不改 Watch 通用逻辑）
_orig_unpack = ODOM_HF._unpack
ODOM_HF._unpack = lambda msg: (lambda s: (RECORDER.feed(s), s)[1])(_orig_unpack(msg))

threading.Thread(target=LOCO.guard_loop, daemon=True, name="h2-guard").start()
threading.Thread(target=LOCO.fsm_loop, daemon=True, name="h2-fsm").start()

import atexit  # noqa: E402

atexit.register(lambda: LOCO.client and LOCO.call("StopMove", {}, source="退出保护"))

_GW_CHECK_AT, _GW_RUNNING = 0.0, False


def gateway_running() -> bool:
    """网关在跑时运控服务可能只应答网关会话（2026-11-22 实测），页面提示。
    结果缓存 3 秒，避免每次轮询都 fork pgrep。"""
    global _GW_CHECK_AT, _GW_RUNNING
    now = time.monotonic()
    if now - _GW_CHECK_AT > 3.0:
        try:
            _GW_RUNNING = subprocess.run(
                ["pgrep", "-f", "h2_gateway.server"], capture_output=True,
                timeout=1.0).returncode == 0
        except Exception:  # noqa: BLE001
            _GW_RUNNING = False
        _GW_CHECK_AT = now
    return _GW_RUNNING


def trajectory() -> list:
    """轨迹图数据：相对清零原点的 (x,y) 序列。"""
    global TRAJ_ORIGIN
    latest = ODOM_HF.snapshot()
    if latest is None:
        return []
    if TRAJ_ORIGIN is None:
        TRAJ_ORIGIN = {"x": latest["x"], "y": latest["y"]}
    rows = ODOM_HF.history_since(TRAJ_WINDOW_S)
    step = max(1, len(rows) // TRAJ_POINTS)
    ox, oy = TRAJ_ORIGIN["x"], TRAJ_ORIGIN["y"]
    return [[round(sample["x"] - ox, 3), round(sample["y"] - oy, 3)]
            for _, sample in rows[::step]]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            try:
                with open(PAGE, "rb") as page:
                    self._respond(page.read(), "text/html; charset=utf-8")
            except OSError as error:
                self._respond(f"page.html 缺失：{error}".encode(), "text/plain", 500)
        elif self.path == "/api/state":
            self._respond_json(self.build_state())
        elif self.path == "/api/record/summary":
            self._respond_json(RECORDER.analyze())
        elif self.path == "/api/record/download":
            self._respond(RECORDER.csv_bytes(), "text/csv", headers={
                "Content-Disposition": "attachment; filename=odom_record.csv"})
        elif self.path == "/api/mic/wav":
            with MIC._lock:
                wav = MIC.last_wav
            if wav is None:
                self._respond_json({"error": "尚无录音"}, status=503)
            else:
                self._respond(wav, "audio/wav", headers={
                    "Content-Disposition": "attachment; filename=mic_record.wav"})
        elif self.path == "/api/rpc/methods":
            self._respond_json(RPC_TABLE)
        elif self.path == "/api/video/snapshot":
            result = VIDEO.snapshot()
            if result is None or result[0] != 200:
                self._respond_json({"error": VIDEO.error or "GetImageSample 无应答"},
                                   status=503)
            else:
                self._respond(result[1], "image/jpeg",
                              headers={"Cache-Control": "no-store"})
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode()) if length else {}
        except json.JSONDecodeError:
            self._respond_json({"error": "请求体不是合法 JSON"}, status=400)
            return
        if self.path == "/api/rpc":
            method = str(payload.get("method") or "")
            if method not in RPC_TABLE:
                self._respond_json({"error": f"方法不在白名单：{method}"}, status=400)
                return
            self._respond_json(LOCO.call(method, payload.get("args") or {}))
        elif self.path == "/api/guard":
            LOCO.guard_enabled = bool(payload.get("enabled", True))
            if not LOCO.guard_enabled:
                LOCO._guard_at = None
            self._respond_json({"guard_enabled": LOCO.guard_enabled})
        elif self.path == "/api/panic":
            first = LOCO.call("StopMove", {}, source="急停")
            second = LOCO.call("StopMove", {}, source="急停(重复)")
            self._respond_json({"first": first, "second": second})
        elif self.path == "/api/audio":
            self._respond_json(AUDIO.call(str(payload.get("method")), payload.get("args") or {}))
        elif self.path == "/api/mic/listen":
            self._respond_json(MIC.start() if payload.get("on") else MIC.stop())
        elif self.path == "/api/mic/record":
            self._respond_json(MIC.record(float(payload.get("seconds", 5))))
        elif self.path == "/api/record/start":
            RECORDER.start()
            self._respond_json(RECORDER.status())
        elif self.path == "/api/record/stop":
            RECORDER.stop()
            self._respond_json(RECORDER.analyze())
        elif self.path == "/api/imu_test/start":
            self._respond_json(IMU_TEST.start(LOWSTATE, ODOM_HF))
        elif self.path == "/api/trajectory/reset":
            global TRAJ_ORIGIN
            latest = ODOM_HF.snapshot()
            TRAJ_ORIGIN = ({"x": latest["x"], "y": latest["y"]} if latest else None)
            self._respond_json({"ok": True, "origin": TRAJ_ORIGIN})
        else:
            self.send_error(404)

    def build_state(self) -> dict:
        hf = ODOM_HF.snapshot()
        lf = ODOM_LF.snapshot()
        low = LOWSTATE.snapshot()
        audio_latest = AUDIO_MSG.snapshot()
        return {
            "boot_id": BOOT_ID, "iface": IFACE, "uptime_s": round(time.monotonic() - START_MONO, 0),
            "gateway_running": gateway_running(),
            "idl_errors": IDL_ERRORS,
            "rpc_error": LOCO.error, "api_version": LOCO.api_version,
            "fsm": LOCO.fsm, "fsm_name": FSM_NAMES.get(LOCO.fsm["id"]) if LOCO.fsm else None,
            "guard_left_ms": LOCO.guard_left_ms(), "guard_enabled": LOCO.guard_enabled,
            "record": RECORDER.status(), "imu_test": IMU_TEST.status(),
            "audio": {"available": AUDIO.available(), "error": AUDIO.error,
                      "log": list(AUDIO.log)},
            "video": {"available": VIDEO.available(), "error": VIDEO.error},
            "mic": MIC.status(),
            "audio_msg": (None if audio_latest is None else {
                "age_ms": audio_latest["age_ms"], "hz": audio_latest["hz"],
                "text": audio_latest["text"], "json": audio_latest["json"]}),
            "audio_msg_ok": AUDIO_MSG.ok,
            "rpc_log": list(LOCO.log),
            "odom_hf": hf, "odom_lf": lf, "lowstate": low,
            "series": {
                "speed": ODOM_HF.series(["speed"]),
                "yaw": ODOM_HF.series(["yaw"]),
                "gyro": LOWSTATE.series(["gx", "gy", "gz"]),
            },
            "traj": trajectory(),
            "joints": JOINTS,
            "fsm_names": FSM_NAMES,
        }

    def _respond(self, body: bytes, content_type: str, status: int = 200,
                 headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, payload: dict, status: int = 200) -> None:
        self._respond(json.dumps(payload, ensure_ascii=False, default=str).encode(),
                      "application/json; charset=utf-8", status)

    def log_message(self, *args: object) -> None:
        pass  # 5Hz 轮询的常规访问日志略去


if __name__ == "__main__":
    print(f"serving http://{BIND}:{PORT}/  (从工作站访问 http://<PC3-IP>:{PORT}/)",
          flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
