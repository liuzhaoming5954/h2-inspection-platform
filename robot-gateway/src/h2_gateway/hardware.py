"""通过 SDK2 对本地操作的宇树 H2 进行高层控制。"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Protocol
from xml.etree import ElementTree

from h2_gateway.teleop import ACTIONS, DIRECTIONS, LEASE_SECONDS, TICK_SECONDS, teleop_reply


LOG = logging.getLogger(__name__)


def emit_event(fields: dict) -> None:
    # 现场诊断事件每行记录一个 JSON 对象，时间戳使用本机单调时钟。
    # 因此可以离线分析指令帧连续性、SDK 回执延迟和每次零速度请求，
    # 无需同步各设备的时钟。
    # 服务端为 h2_gateway 包开启 INFO 级别日志，
    # 单元测试读取相同的记录。
    fields["mono_ms"] = time.monotonic_ns() // 1_000_000
    LOG.info("h2_event %s", json.dumps(fields, sort_keys=True))
MAIN_FSM = 601
STANDING = 0
MOVING = 1
SDK_TIMEOUT = 0.10
STATE_POLL_INTERVAL = 0.35
STATE_MAX_AGE = 0.8
MAX_VELOCITY_DURATION = 0.25
MIN_VELOCITY_DURATION = 0.04
MAX_HARDWARE_COMMAND_MS = 350
ENVIRONMENT_SAMPLE_ID = re.compile(r"([0-9a-f]{32}):([1-9][0-9]{0,15})")

# 2026-11-15 经 H2 真机运动测试验证的试验限值。FSM 601 下，
# 内置 HybridWalk 控制器只在速度超过阈值后抬脚。
# 在 0.15 m/s 和 0.25 rad/s 下，已接受的指令只会引起
# 倾斜或原地偏航等平衡调整。起步速度介于 0.3 和 0.5 m/s 之间。
# 下列数值经持续指令流和单条指令测试。
# 步态和平衡仍由 H2 的内置控制器负责。
VELOCITIES = {
    "forward": (0.5, 0.0, 0.0),
    "backward": (-0.5, 0.0, 0.0),
    "turn_left": (0.0, 0.0, 0.55),
    "turn_right": (0.0, 0.0, -0.55),
}


class Loco(Protocol):
    def fsm(self) -> tuple[int, int | None]: ...
    def velocity(self, vx: float, vy: float, omega: float, duration: float) -> None: ...


class UnitreeLoco:
    def __init__(self, interface: str, *, domain: int = 0):
        try:
            socket.if_nametoindex(interface)
        except OSError as error:
            raise ValueError(f"Unknown H2 network interface: {interface}") from error
        try:
            from unitree_sdk2py.core import channel
            from unitree_sdk2py.h2.loco.h2_loco_client import LocoClient
        except ImportError as error:
            raise RuntimeError("Install the Unitree H2 Python SDK and CycloneDDS first; see README.md") from error
        try:
            # 固定版本的 SDK 会将配置跟踪信息写入 /tmp/cdds.LOG。
            # Ubuntu 24.04 上的 CycloneDDS 0.10.2 构建会在写入跟踪信息时异常终止。
            original_config = channel.ChannelConfigHasInterface
            config = ElementTree.fromstring(original_config)
            for entry in config.findall("./Domain"):
                tracing = entry.find("Tracing")
                if tracing is not None:
                    entry.remove(tracing)
            channel.ChannelConfigHasInterface = ElementTree.tostring(config, encoding="unicode")
            try:
                channel.ChannelFactoryInitialize(domain, interface)
            finally:
                channel.ChannelConfigHasInterface = original_config
            self._client = LocoClient()
            self._client.SetTimeout(SDK_TIMEOUT)
            self._client.Init()
        except Exception as error:
            raise RuntimeError(f"Could not initialize Unitree DDS on {interface}: {error}") from error

    def fsm(self) -> tuple[int, int | None]:
        code, fsm_id = self._client.GetFsmId()
        if code != 0 or type(fsm_id) is not int:
            raise RuntimeError(f"H2 GetFsmId failed (SDK code {code})")
        if fsm_id != MAIN_FSM:
            return fsm_id, None
        code, fsm_mode = self._client.GetFsmMode()
        if code != 0 or type(fsm_mode) is not int:
            raise RuntimeError(f"H2 GetFsmMode failed (SDK code {code})")
        return fsm_id, fsm_mode

    def velocity(self, vx: float, vy: float, omega: float, duration: float) -> None:
        # Python SDK 的 Move 和 StopMove 便捷方法会丢弃 RPC 返回结果。
        code = self._client.SetVelocity(vx, vy, omega, duration)
        if code != 0:
            raise RuntimeError(f"H2 SetVelocity failed (SDK code {code})")


class EnvironmentSource(Protocol):
    def sample(self) -> dict | None: ...


class BatterySource(Protocol):
    def battery(self) -> float | None: ...


class BmsWatcher:
    """通过已启用的通道工厂订阅机器人的低频 BMS 话题。

    在 UnitreeLoco 调用 ChannelFactoryInitialize 后构造实例。
    读数过期或缺失时返回 None，状态中省略该字段。
    """

    def __init__(self, topic: str = "rt/lf/bmsstate", *, fresh_seconds: float = 5.0):
        from unitree_sdk2py.core.channel import ChannelSubscriber
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import BmsState_

        self._lock = threading.Lock()
        self._fresh_seconds = fresh_seconds
        self._reading: tuple[float, float] | None = None  # （单调时钟读数，soc 百分比）
        self._subscriber = ChannelSubscriber(topic, BmsState_)
        self._subscriber.Init(self._on_message, 1)

    def _on_message(self, message) -> None:
        with self._lock:
            self._reading = (time.monotonic(), float(message.soc))

    def battery(self) -> float | None:
        with self._lock:
            reading = self._reading
        if reading is None or time.monotonic() - reading[0] > self._fresh_seconds:
            return None
        return reading[1]


class OdomWatcher:
    """订阅 H2 里程计话题（rt/odommodestate，约 500 Hz），
    保存最新位姿供状态轮询读取。

    早期固件没有文档所述的低频话题（rt/lf/odommodestate），
    因此订阅高频话题并用锁保护最新样本。状态轮询约为 1 Hz。
    世界坐标系以开机位置为原点，x 轴向前，y 轴向左，
    重启会重置原点。"""

    def __init__(self, topic: str = "rt/odommodestate", *, fresh_seconds: float = 2.0):
        from unitree_sdk2py.core.channel import ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_

        self._lock = threading.Lock()
        self._fresh_seconds = fresh_seconds
        self._reading: tuple[float, dict] | None = None  # （单调时钟读数，位姿）
        self._subscriber = ChannelSubscriber(topic, SportModeState_)
        self._subscriber.Init(self._on_message, 1)

    def _on_message(self, message) -> None:
        with self._lock:
            self._reading = (time.monotonic(), {
                "x": float(message.position[0]),
                "y": float(message.position[1]),
                "yaw": float(message.imu_state.rpy[2]),
            })

    def pose(self) -> dict | None:
        with self._lock:
            reading = self._reading
        if reading is None or time.monotonic() - reading[0] > self._fresh_seconds:
            return None
        return reading[1]


class AgentEnvironment:
    """轮询 PC2 的 RS-485 传感器代理，提供最新的有效读数。

    读取失败或读数过期时返回 None，状态中省略相应字段。
    遥测独立于运动控制路径。
    """

    def __init__(self, url: str, *, interval: float = 2.0, fresh_seconds: float = 8.0,
                 fetch=None):
        self._url = url
        self._interval = max(0.5, interval)
        self._fresh_seconds = fresh_seconds
        self._fetch = fetch or self._http_fetch
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reading: tuple[float, dict] | None = None  # （单调时钟截止时间，PC2 样本）
        self._latest_samples: dict[str, int] = {}
        self._current_instance: str | None = None

    def _http_fetch(self) -> dict | None:
        import urllib.request
        try:
            with urllib.request.urlopen(self._url, timeout=1.5) as response:
                payload = json.loads(response.read().decode())
        except Exception:
            return None
        sample = payload.get("sample") if isinstance(payload, dict) else None
        if not isinstance(sample, dict) or payload.get("available") is not True:
            return None
        return sample

    def _poll_once(self) -> None:
        started = time.monotonic()
        sample = self._fetch()
        if not isinstance(sample, dict):
            return
        reading = {}
        for field in ("temperature_c", "humidity_percent", "captured_at", "age_seconds"):
            value = sample.get(field)
            if type(value) not in (int, float):
                return
            try:
                reading[field] = float(value)
            except OverflowError:
                return
            if not math.isfinite(reading[field]):
                return
        sample_id = sample.get("sample_id")
        match = ENVIRONMENT_SAMPLE_ID.fullmatch(sample_id) if isinstance(sample_id, str) else None
        if match is None or not 0 <= reading["age_seconds"] or reading["captured_at"] <= 0:
            return
        instance, sequence_text = match.groups()
        sequence = int(sequence_text)
        if sequence > 2**53 - 1:
            return
        expires_at = started + self._fresh_seconds - reading.pop("age_seconds")
        reading["sample_id"] = sample_id
        with self._lock:
            if instance != self._current_instance and instance in self._latest_samples:
                return
            latest = self._latest_samples.get(instance, 0)
            if sequence < latest:
                return
            if sequence == latest:
                if self._reading is not None and self._reading[1]["sample_id"] == sample_id:
                    self._reading = (min(self._reading[0], expires_at), self._reading[1])
                return
            self._latest_samples[instance] = sequence
            self._current_instance = instance
            if expires_at > time.monotonic():
                self._reading = (expires_at, reading)
            else:
                self._reading = None

    def _poll_loop(self) -> None:
        while not self._stop.wait(self._interval):
            self._poll_once()

    def start(self) -> "AgentEnvironment":
        threading.Thread(target=self._poll_loop, name="h2-environment-poll", daemon=True).start()
        return self

    def sample(self) -> dict | None:
        with self._lock:
            reading = self._reading
        if reading is None:
            return None
        remaining_ms = math.floor((reading[0] - time.monotonic()) * 1000)
        return {**reading[1], "remaining_ms": remaining_ms} if remaining_ms > 0 else None

    def close(self) -> None:
        self._stop.set()


class _SDKLock:
    """串行访问 SDK，同一优先级内先处理较早入队的请求。"""

    SAFETY = 0
    CONTROL = 1
    STATUS = 2

    def __init__(self):
        self._changed = threading.Condition()
        self._locked = False
        self._issued = [0, 0, 0]
        self._next = [0, 0, 0]

    def _waiting(self, before: int) -> bool:
        return self._issued[:before] != self._next[:before]

    @contextmanager
    def hold(self, priority: int, *, blocking: bool = True):
        with self._changed:
            acquire = blocking or (not self._locked and not self._waiting(priority + 1))
            if acquire:
                ticket = self._issued[priority]
                self._issued[priority] += 1
                self._changed.wait_for(lambda: not self._locked
                                       and self._next[priority] == ticket
                                       and not self._waiting(priority))
                self._next[priority] += 1
                self._locked = True
        try:
            yield acquire
        finally:
            if acquire:
                with self._changed:
                    self._locked = False
                    self._changed.notify_all()


class H2HardwareGateway:
    """串行执行 SDK 写入，控制租约仅保存在内存中。"""

    def __init__(self, loco: Loco, environment: EnvironmentSource | None = None,
                 battery: BatterySource | None = None, odom: "OdomWatcher | None" = None):
        self._loco = loco
        self._environment = environment
        self._battery = battery
        self._odom = odom
        self._lock = _SDKLock()
        self._shutdown = threading.Event()
        self._fsm: tuple[int, int | None] | None = None
        self._state_at = 0.0
        self._updated_at = ""
        self._token = ""
        self._sequence = 0
        self._lease_until = self._motion_until = 0.0
        self._motion = "unknown"
        self._stop_needed = False
        self._stop_fault = False
        self._worker = threading.Thread(target=self._watch, name="h2-hardware-watchdog", daemon=True)
        self._worker.start()

    def _ready(self, now: float) -> bool:
        modes = (STANDING, MOVING) if self._token else (STANDING,)
        return (not self._shutdown.is_set() and not self._stop_fault
                and self._fsm is not None and now - self._state_at <= STATE_MAX_AGE
                and self._fsm[0] == MAIN_FSM and self._fsm[1] in modes)

    def _stop(self, reason: str) -> bool:
        self._motion_until = 0.0
        sent_ns = time.monotonic_ns()
        try:
            self._loco.velocity(0.0, 0.0, 0.0, MAX_VELOCITY_DURATION)
        except Exception as error:
            emit_event({"event": "zero_velocity", "reason": reason, "ack": False,
                        "error": str(error)[:120]})
            LOG.error("H2 stop request was not acknowledged: %s", error)
            self._motion = "unknown"
            self._stop_needed = True
            self._stop_fault = True
            return False
        emit_event({"event": "zero_velocity", "reason": reason, "ack": True,
                    "sdk_ms": round((time.monotonic_ns() - sent_ns) / 1e6, 1)})
        self._stop_needed = False
        self._motion = "stop_requested"
        return True

    def _revoke(self, reason: str) -> bool:
        emit_event({"event": "lease", "change": "revoked", "reason": reason})
        self._token = ""
        self._lease_until = 0.0
        self._sequence = 0
        return not self._stop_needed or self._stop(reason)

    def _expire(self, now: float) -> None:
        if self._token and now >= self._lease_until:
            self._revoke("lease_expired")
        elif self._token and not self._ready(now):
            self._revoke("not_ready")
        elif self._motion_until and now >= self._motion_until:
            # 运动指令帧已过期，后续帧尚未到达。
            # 按帧超时立即停步，租约由 2 秒心跳期限独立管理。
            # 把两种期限联动会使正常会话被提前撤销。
            # 例如，前端的下一帧经隧道传输，
            # 超过 250 ms 才到达时便会触发这种情况。
            self._motion_until = 0.0
            if self._motion in DIRECTIONS:
                self._stop("frame_expired")  # 失败时锁存 stop_fault，随后由 _ready 判定撤销租约。

    def _watch(self) -> None:
        next_poll = 0.0
        while not self._shutdown.wait(TICK_SECONDS):
            with self._lock.hold(_SDKLock.SAFETY):
                if self._shutdown.is_set():
                    return
                now = time.monotonic()
                self._expire(now)
                if now < next_poll:
                    continue
                # 普通状态轮询给队列中的控制指令让行。
                # 状态接近过期时，与方向指令按先进先出顺序排队，
                # 防止持续运动导致轮询一直得不到执行。
                refresh_due = now - self._state_at >= STATE_MAX_AGE / 2
                if (not refresh_due and self._motion_until
                        and self._motion_until - now <= 2 * SDK_TIMEOUT):
                    continue
            priority = _SDKLock.CONTROL if refresh_due else _SDKLock.STATUS
            with self._lock.hold(priority, blocking=refresh_due) as acquired:
                if not acquired:
                    continue
                if self._shutdown.is_set():
                    return
                self._expire(time.monotonic())
                try:
                    self._fsm = self._loco.fsm()
                    self._state_at = time.monotonic()
                    self._updated_at = datetime.now(timezone.utc).isoformat()
                except Exception as error:
                    if self._fsm is not None:
                        LOG.warning("Lost H2 high-level state: %s", error)
                    self._fsm = None
                    self._motion = "unknown"
                self._expire(time.monotonic())
                next_poll = time.monotonic() + STATE_POLL_INTERVAL

    def get_state(self, *, include_telemetry: bool = True) -> dict:
        priority = _SDKLock.STATUS if include_telemetry else _SDKLock.CONTROL
        with self._lock.hold(priority):
            now = time.monotonic()
            self._expire(now)
            ready = self._ready(now)
            fresh = self._fsm is not None and now - self._state_at <= STATE_MAX_AGE
            if self._stop_fault:
                status = "fault"
            elif not fresh:
                status = "offline"
            elif self._token:
                status = "teleop"
            else:
                status = "ready" if ready else "not_ready"
            state = {
                "robot_id": "H2", "simulated": False, "status": status,
                "teleop_ready": ready, "teleop_active": bool(self._token),
                "updated_at": self._updated_at, "motion": self._motion if fresh else "unknown",
            }
            if fresh:
                fsm_id, fsm_mode = self._fsm
                state["fsm_id"] = fsm_id
                if fsm_mode is not None:
                    state["fsm_mode"] = fsm_mode

        if not include_telemetry:
            return state
        # 这些缓存各自加锁。读取前先释放运动锁，
        # 避免状态请求拖延停步或方向指令。
        if self._environment is not None:
            reading = self._environment.sample()
            if reading is not None:
                state["temperature_c"] = reading["temperature_c"]
                state["humidity_percent"] = reading["humidity_percent"]
                if "captured_at" in reading:
                    state["environment_captured_at"] = reading["captured_at"]
                if "remaining_ms" in reading and "sample_id" in reading:
                    state["environment_remaining_ms"] = reading["remaining_ms"]
                    state["environment_sample_id"] = reading["sample_id"]
        if self._battery is not None:
            percent = self._battery.battery()
            if percent is not None:
                state["battery_percent"] = percent
        if self._odom is not None:
            pose = self._odom.pose()
            if pose is not None:
                state["pose"] = pose
        return state

    def teleop_acquire(self) -> dict:
        with self._lock.hold(_SDKLock.CONTROL):
            now = time.monotonic()
            self._expire(now)
            if self._stop_fault:
                return teleop_reply(False, "stop_unconfirmed", "previous H2 stop was not acknowledged")
            if self._token:
                return teleop_reply(False, "teleop_busy", "another operator holds H2 control")
            if self._fsm is None or now - self._state_at > STATE_MAX_AGE or self._shutdown.is_set():
                return teleop_reply(False, "hardware_unavailable", "no fresh H2 motion service state")
            if not self._ready(now):
                return teleop_reply(False, "robot_not_ready", "H2 must be standing in main locomotion mode")
            self._token = secrets.token_urlsafe(24)
            self._lease_until = now + LEASE_SECONDS
            emit_event({"event": "lease", "change": "acquired"})
            return teleop_reply(True, "acquired", "H2 teleop control acquired", self._token)

    def _reject(self, sequence: int, action: str, code: str, message: str) -> dict:
        emit_event({"event": "teleop_reject", "seq": sequence, "action": action[:40], "code": code})
        return teleop_reply(False, code, message)

    def teleop_command(self, token: str, sequence: int, action: str, gateway_deadline_ms: int | None,
                       *, is_active: Callable[[], bool] | None = None) -> dict:
        priority = _SDKLock.SAFETY if action == "stop" else _SDKLock.CONTROL
        with self._lock.hold(priority):
            now = time.monotonic()
            self._expire(now)
            if self._stop_fault:
                return self._reject(sequence, action, "stop_unconfirmed", "H2 stop was not acknowledged")
            if not self._token or token != self._token:
                return self._reject(sequence, action, "invalid_token", "H2 teleop control is no longer held")
            if action not in ACTIONS:
                return self._reject(sequence, action, "invalid_action", "unsupported H2 teleop action")
            if sequence <= self._sequence:
                return self._reject(sequence, action, "stale_sequence", "teleop sequence must strictly increase")
            # 当前租约内的停步指令即使延迟到达仍然有效。
            if action != "stop":
                if is_active is not None and not is_active():
                    return self._reject(sequence, action, "expired",
                                        "teleop request was cancelled before SDK admission")
                if gateway_deadline_ms is None:
                    return self._reject(sequence, action, "invalid_expiry", "PC3 monotonic deadline is required")
                remaining_ms = gateway_deadline_ms - (time.monotonic_ns() + 999_999) // 1_000_000
                if remaining_ms > MAX_HARDWARE_COMMAND_MS:
                    return self._reject(sequence, action, "invalid_expiry",
                                        "teleop deadline is too far in the future")
                if remaining_ms <= 0 or (action != "heartbeat" and remaining_ms < MIN_VELOCITY_DURATION * 1000):
                    return self._reject(sequence, action, "expired", "teleop command has expired")
            self._sequence = sequence
            self._lease_until = time.monotonic() + LEASE_SECONDS
            if action == "heartbeat":
                return teleop_reply(True, "renewed", "H2 teleop lease renewed", token)
            if action == "stop":
                if self._stop("stop"):
                    return teleop_reply(True, "stopped", "H2 acknowledged zero velocity", token)
                self._token = ""
                return teleop_reply(False, "stop_unconfirmed", "H2 stop was not acknowledged")

            duration = min(remaining_ms / 1000, MAX_VELOCITY_DURATION)
            # RPC 发出后若超时，无法区分请求未送达和回执丢失。
            self._stop_needed = True
            self._motion_until = min(time.monotonic() + duration, gateway_deadline_ms / 1000)
            frame = {"event": "teleop_frame", "seq": sequence, "action": action,
                     "remaining_ms": remaining_ms, "duration_ms": round(duration * 1000, 1)}
            sent_ns = time.monotonic_ns()
            try:
                self._loco.velocity(*VELOCITIES[action], duration)
            except Exception as error:
                LOG.warning("H2 velocity request was not acknowledged: %s", error)
                emit_event({**frame, "ack": False, "error": str(error)[:120]})
                if not self._revoke("sdk_rejected"):
                    return teleop_reply(False, "stop_unconfirmed", "H2 stop was not acknowledged")
                return teleop_reply(False, "sdk_rejected", "H2 velocity request was not acknowledged")
            frame["sdk_ms"] = round((time.monotonic_ns() - sent_ns) / 1e6, 1)
            if time.monotonic() >= self._motion_until:
                # SDK 回执在该帧有效期结束后到达。
                # 趁当前指令仍持有锁时发送零速度指令，并保留租约。
                if not self._stop("late_frame"):
                    return teleop_reply(False, "stop_unconfirmed", "H2 stop was not acknowledged")
                emit_event({**frame, "result": "late_ack_zeroed"})
                return teleop_reply(True, "stopped", "H2 acknowledged a late frame; motion halted", token)
            self._motion = action
            emit_event({**frame, "result": "applied"})
            return teleop_reply(True, "applied", "H2 acknowledged velocity request", token)

    def teleop_release(self, token: str) -> dict:
        with self._lock.hold(_SDKLock.SAFETY):
            if self._stop_fault:
                return teleop_reply(False, "stop_unconfirmed", "H2 stop was not acknowledged")
            if not self._token or token != self._token:
                return teleop_reply(False, "invalid_token", "H2 teleop control is no longer held")
            if not self._revoke("release"):
                return teleop_reply(False, "stop_unconfirmed", "H2 stop was not acknowledged")
            return teleop_reply(True, "released", "H2 teleop control released")

    def close(self) -> None:
        self._shutdown.set()
        with self._lock.hold(_SDKLock.SAFETY):
            if self._token:
                self._revoke("shutdown")
        self._worker.join()
        if self._environment is not None:
            self._environment.close()


def probe() -> None:
    parser = argparse.ArgumentParser(description="Read H2 high-level FSM status without sending motion")
    parser.add_argument("--interface", required=True, help="Network interface connected to the H2")
    args = parser.parse_args()
    try:
        fsm_id, fsm_mode = UnitreeLoco(args.interface).fsm()
    except (RuntimeError, ValueError) as error:
        raise SystemExit(f"H2 status query failed: {error}") from error
    print(json.dumps({"fsm_id": fsm_id, "fsm_mode": fsm_mode,
                      "teleop_ready": (fsm_id, fsm_mode) == (MAIN_FSM, STANDING)}))


if __name__ == "__main__":
    probe()
