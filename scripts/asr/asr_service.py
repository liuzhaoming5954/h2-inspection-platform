#!/usr/bin/env python3
"""H2 语音识别服务 v3（PC3 本地闭环）：reSpeaker 采集 + Qwen3-ASR 本地转写。

按 2026-11-20《H2-PC3现场AI优化任务书》重构，相对 v2 的关键变化：

- 切段：改用 asr_vad.Segmenter（待命只留前卷、4 秒硬上限含前卷尾静、
  起声确认窗计入语音样本），修复「长时间静音后第一段为巨段」缺陷；
- 口令会话化：演示待命/解除/命令事件独立于普通转写（/asr/session、
  /asr/commands）。整句「开始」才算口令；武装前的声音、播报期间、
  迟到结果、重复投递与服务重启均不触发；一次有效触发即消费会话；
- 播报自听：/asr/playback 标记播报窗口，播报期间丢弃采音，两路播报
  （/say 与关键词应答）共用该状态；
- 推理监督：transcribe 隔离在常驻子进程，主进程按超时回收、丢弃积压、
  作废待命会话；连续恢复超限进入故障态等待人工；
- 诊断：采音结束/入队/出队/推理起止全程打点，普通转写与口令事件分别
  报告排队耗时与推理耗时；发布时再校验样本新鲜度与会话有效性。

依赖说明：qwen_asr 延迟导入——本文件可在未安装模型的工作站上被导入
做逻辑测试；模型仅在 PC3 的 ~/qwen3-asr-env 内加载。

HTTP（host 网络，即 PC3:8098）：
  GET  /health                    模型/采音/会话/推理监督全量状态
  POST /asr/start | /asr/stop     采音开关（页面阀门语义不变）
  GET  /asr/text?since=N          普通转写增量（页面显示，不变）
  POST /asr/session {"action":"arm"|"disarm"}   演示待命会话
  GET  /asr/commands?since=N      口令命令事件（上位机消费）
  POST /asr/playback {"active":bool}            播报自听窗口标记
  GET  /asr/debug                 切段/口令/推理诊断事件
"""
from __future__ import annotations

import contextlib
import multiprocessing
import os
import queue
import subprocess
import threading
import time
import uuid

import numpy as np
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

import asr_vad

# XVF3800 USB 控制（wiki 2026-11-21 确认）：硬件 VAD + 固件 REBOOT。
# pyusb 延迟导入：libusb 初始化在 USB 设备异常时可能阻塞模块加载，
# 绝不在顶层 import（实测顶层导入导致服务启动卡死、端口不监听）。
_HAS_PYUSB = None    # None=未检测，True/False=已检测
_XVF_PARAMS = {
    "REBOOT":    (48, 7, 1, "wo", "uint8"),   # 固件级重启（最轻量恢复）
    "DOA_VALUE": (20, 18, 4, "ro", "uint16"), # [0]=方向 0-359°，[1]=VAD（1=语音）
}
_xvf_dev = None            # USB 设备句柄（懒初始化）
_hw_speech = False         # 芯片 VAD 最近一次读数
_hw_vad_ok = False         # 轮询是否正常工作


def _xvf_find():
    """懒导入 pyusb 并查找设备（安全：USB 异常时不阻塞模块加载）。"""
    global _xvf_dev, _HAS_PYUSB
    if _HAS_PYUSB is None:
        try:
            import usb.core
            _xvf_dev = usb.core.find(idVendor=0x2886, idProduct=0x001A)
            _HAS_PYUSB = _xvf_dev is not None
        except ImportError:
            _HAS_PYUSB = False
    if not _HAS_PYUSB:
        return None
    if _xvf_dev is None:
        import usb.core
        _xvf_dev = usb.core.find(idVendor=0x2886, idProduct=0x001A)
    return _xvf_dev


def _xvf_read_doa():
    """读 XVF3800 DOA_VALUE：返回 (方向度数, VAD 标志) 或 None。"""
    import struct
    dev = _xvf_find()
    if dev is None:
        return None
    response = dev.ctrl_transfer(0xC0, 0, 0x80 | 18, 20, 5, 10000)
    data = response.tobytes()[1:]
    return struct.unpack("<HH", data)   # (DOA 0-359°, VAD 1=speech)


def _hw_vad_loop():
    """后台 100ms 轮询芯片 VAD，供切段器自适应门限（2026-11-21 验证有效）。"""
    global _hw_speech, _hw_vad_ok
    while True:
        try:
            result = _xvf_read_doa()
            if result is not None:
                _hw_speech = result[1] == 1
                _hw_vad_ok = True
            else:
                _hw_vad_ok = False
        except Exception:  # noqa: BLE001 - USB 偶发失败不终止线程
            _hw_vad_ok = False
        time.sleep(0.1)


def xvf_reboot() -> bool:
    """XVF3800 固件级 REBOOT（wiki 命令，麦克风挂死首选恢复）。"""
    dev = _xvf_find()
    if dev is None:
        return False
    try:
        dev.ctrl_transfer(0x40, 0, 7, 48, [1], 10000)
        return True
    except Exception:  # noqa: BLE001
        return False


MODEL_PATH = "/home/unitree/models/Qwen3-ASR-1.7B"
ARECORD = ["arecord", "-D", "hw:Array,0", "-f", "S16_LE", "-r", "16000", "-c", "2",
           "-t", "raw", "--quiet", "-"]
BYTES_PER_WIN = int(asr_vad.SAMPLE_RATE * asr_vad.WIN_MS / 1000) * 4   # 立体声 int16
MAX_RESULTS = 100             # 普通转写环形条数
STALE_SEGMENT_S = 15.0        # 出队/发布时的旧段丢弃（只用于清积压，不是响应目标）
COMMAND_MAX_AGE_S = 10.0      # 口令事件发布时的采样年龄上限（迟到丢弃）
COMMAND_TEXT = "开始"
COMMAND_STRIP = " \t。，！？!?,、;；"
INFER_TIMEOUT_S = 30.0        # 单次推理超时（2026-11-21 实测短段 2~3s、4s 段约 15s；
                              # 越长聋窗越大，挂起段拖满 60s 曾作废整个待命会话）
RECOVERY_FAULT_LIMIT = 3      # 连续恢复次数上限，超过进入故障态等待人工

app = FastAPI()

_lock = threading.Lock()
_event_lock = threading.Lock()   # 调试事件独立锁：允许在持有 _lock 的路径里记录事件
_boot_id = uuid.uuid4().hex[:12]

# ---- 采音与播报 ----
_capturing = False
_capture_proc: subprocess.Popen | None = None
_capture_thread: threading.Thread | None = None
_mic_last_window: float | None = None     # 最近一次真正读到音频窗的单调时刻
_playback_skipped = 0                     # 播报期间丢弃的窗数（诊断自听抑制强度）
_playback_windows: list[tuple[float, float]] = []   # 最近播报窗口（单调秒）
_playback_active = False

# ---- 会话与事件 ----
_session: dict | None = None                        # {id, armed_at, consumed}
_results: list[dict] = []                           # 普通转写 [{id,ts,text,diag}]
_next_result_id = 1
_commands: list[dict] = []                          # 口令事件
_next_command_seq = 1
_debug_events: list[dict] = []

# ---- 推理模型（父进程加载，fork 给子进程继承）----
_MODEL = None
_model_error = ""


def _note_event(kind: str, **fields) -> None:
    with _event_lock:
        _debug_events.append({"t": round(time.time(), 2), "kind": kind, **fields})
        del _debug_events[:-80]


def _versions_line() -> str:
    """启动时记录推理环境版本，挂起取证时对照。"""
    parts = [f"cpu={os.cpu_count()}", f"boot={_boot_id}"]
    for name in ("torch", "transformers"):
        try:
            module = __import__(name)
            parts.append(f"{name}={module.__version__}")
        except Exception:  # noqa: BLE001 - 版本缺失不阻断启动
            parts.append(f"{name}=?")
    try:
        import qwen_asr
        parts.append(f"qwen_asr={getattr(qwen_asr, '__file__', '?')}")
    except Exception:  # noqa: BLE001
        parts.append("qwen_asr=?")
    return " ".join(parts)


def _load_model() -> None:
    global _MODEL, _model_error
    try:
        from qwen_asr import Qwen3ASRModel
        _MODEL = Qwen3ASRModel.from_pretrained(MODEL_PATH)
    except Exception as error:  # noqa: BLE001 - 启动失败要落到健康接口
        _model_error = str(error)[:200]


# ---------- 推理子进程与监督 ----------

def _worker_main(task_q, result_q) -> None:  # pragma: no cover - 子进程入口
    """常驻推理子进程：fork 继承已加载的模型，逐任务转写并回报时间戳。

    单调时钟在 fork 后跨进程一致（CLOCK_MONOTONIC 系统级），
    出队/推理起止可直接与父进程打点对齐。"""
    while True:
        try:
            task = task_q.get()
        except (EOFError, OSError, KeyboardInterrupt):
            return
        if task is None:
            return
        req_id, audio = task
        dequeued = time.monotonic()
        try:
            peak = float(np.abs(audio).max())
            if peak > 1e-4:
                audio = audio * (0.3 / peak)
            results = _MODEL.transcribe((audio, asr_vad.SAMPLE_RATE))
            text = " ".join(getattr(r, "text", "") for r in results).strip()
            result_q.put((req_id, text, None, dequeued, time.monotonic()))
        except Exception as error:  # noqa: BLE001 - 单段失败回报后继续
            result_q.put((req_id, None, str(error)[:200], dequeued, time.monotonic()))


class _InferenceSupervisor:
    """监督推理子进程：提交、结果泵、超时回收与有界重启。"""

    def __init__(self) -> None:
        self.proc: multiprocessing.process.BaseProcess | None = None
        self.task_q = None
        self.result_q = None
        self.pending: dict[int, dict] = {}              # req_id → 任务元数据
        self._next_req = 1
        self.current: tuple[int, float] | None = None   # (req_id, 入队时刻)
        self.recoveries = 0
        self.consecutive_failures = 0
        self.fault = False
        self.last_ok_mono: float | None = None
        self.recent_infer_ms: list[float] = []

    # -- 生命周期 --
    def start(self) -> None:
        """fork 出推理子进程（继承父进程已加载的模型，重启无需重载）。"""
        ctx = multiprocessing.get_context("fork")
        self.task_q = ctx.Queue(maxsize=2)
        self.result_q = ctx.Queue()
        self.proc = ctx.Process(target=_worker_main,
                                args=(self.task_q, self.result_q), daemon=True)
        self.proc.start()

    def _recycle(self, reason: str) -> None:
        """超时/退出时的回收：取证、杀进程、重建、丢弃积压、作废会话。

        禁止通过新建线程叠加已挂起的推理——旧任务全部作废，
        恢复后必须重新武装演示会话。"""
        global _session
        self.recoveries += 1
        self.consecutive_failures += 1
        req = self.current[0] if self.current else None
        self._dump_diagnostics(reason, req)
        if self.proc is not None and self.proc.is_alive():
            self.proc.kill()
            self.proc.join(timeout=2)
        # 旧队列必须显式弃管：对端已死，若留给 GC 走默认 close 会 join
        # feeder 线程，触发 GC 的线程（可能是任意请求线程）会永久阻塞，
        # 表现为服务进程存活但 HTTP 间歇无响应（2026-11-21 注入测试实测）
        for stale in (self.task_q, self.result_q):
            if stale is not None:
                with contextlib.suppress(Exception):
                    stale.close()
                with contextlib.suppress(Exception):
                    stale.cancel_join_thread()
        self.pending.clear()
        self.current = None
        with _lock:
            _session = None
        if self.consecutive_failures >= RECOVERY_FAULT_LIMIT:
            self.fault = True
            _note_event("infer-fault", recoveries=self.recoveries)
            return
        self.start()
        _submit_warmup()          # 重建的子进程同样先预热首转
        _note_event("infer-recovered", reason=reason, recoveries=self.recoveries)

    def _dump_diagnostics(self, reason: str, req: int | None) -> None:
        pid = self.proc.pid if self.proc else None
        status: dict[str, str] = {}
        if pid:
            try:
                with open(f"/proc/{pid}/status") as handle:
                    for line in handle:
                        if line.startswith(("State:", "VmRSS:", "Threads:")):
                            key, _, value = line.partition(":")
                            status[key.strip()] = value.strip()
            except OSError:
                pass
        _note_event("infer-timeout", reason=reason, req=req, pid=pid,
                    pending=len(self.pending), **status)

    # -- 提交与结果 --
    def submit(self, audio: np.ndarray, capture_end: float) -> None:
        if self.fault or self.proc is None:
            return
        req_id = self._next_req
        self._next_req += 1
        meta = {"req": req_id, "enqueued": time.monotonic(), "capture_end": capture_end}
        item = (req_id, audio)
        try:
            self.task_q.put_nowait(item)
            self.pending[req_id] = meta
        except ValueError:
            return     # 回收刚弃管的旧队列：丢弃本段，采集线程不得因此死掉
        except queue.Full:
            # 队满丢「最长」段：短而新的口令段优先存活（不可打断已进模型的推理）
            kept = []
            for _ in range(2):
                try:
                    kept.append(self.task_q.get_nowait())
                except queue.Empty:
                    break
            kept.sort(key=lambda entry: entry[1].size)
            if kept:
                self.task_q.put_nowait(kept[0])
                self.pending.pop(kept[-1][0], None)
            try:
                self.task_q.put_nowait(item)
                self.pending[req_id] = meta
            except queue.Full:
                pass

    def pump(self) -> None:
        """回收已完成结果并分发（无结果时立即返回）。"""
        if self.result_q is None:
            return
        while True:
            try:
                req_id, text, error, dequeued, infer_end = self.result_q.get_nowait()
            except queue.Empty:
                return
            except (EOFError, OSError):
                return
            self._handle_result(req_id, text, error, dequeued, infer_end)

    def _handle_result(self, req_id, text, error, dequeued: float, infer_end: float) -> None:
        meta = self.pending.pop(req_id, None)
        if meta is None:        # 回收时已作废
            return
        if self.current and self.current[0] == req_id:
            self.current = None
        infer_ms = (infer_end - dequeued) * 1000
        if error is not None:
            _note_event("infer-error", req=req_id, error=error)
            return
        self.consecutive_failures = 0
        self.last_ok_mono = infer_end
        self.recent_infer_ms.append(infer_ms)
        del self.recent_infer_ms[:-20]
        self._publish(meta, text, dequeued, infer_ms)

    def watchdog(self) -> None:
        """超时判定：仅在有任务超时或进程退出时触发；静默待命是正常态。"""
        if self.fault:
            return
        if self.proc is not None and not self.proc.is_alive():
            self._recycle("worker-exited")
            return
        if self.current is not None and time.monotonic() - self.current[1] > INFER_TIMEOUT_S:
            self._recycle("infer-timeout")

    def track_current(self) -> None:
        """以最早未完成任务作为当前任务计时（子进程串行消费）。"""
        if self.current is None and self.pending:
            first = min(self.pending.values(), key=lambda m: m["enqueued"])
            self.current = (first["req"], first["enqueued"])

    # -- 发布（普通转写 + 口令事件）--
    def _publish(self, meta: dict, text: str, dequeued: float, infer_ms: float) -> None:
        global _next_result_id
        now = time.monotonic()
        diag = {"age_ms": round((now - meta["capture_end"]) * 1000),
                "queue_ms": round((dequeued - meta["enqueued"]) * 1000),
                "infer_ms": round(infer_ms)}
        if diag["age_ms"] > STALE_SEGMENT_S * 1000:
            _note_event("stale-publish", **diag)    # 只清积压，不进结果
            return
        with _lock:
            # 播报窗口内的采样：闭合窗按区间判定；开窗中（end 未更新）时，
            # 晚于窗起点的采样一律视为播报自听
            playback = any(start <= meta["capture_end"] <= end
                           for start, end in _playback_windows)
            if not playback and _playback_active and _playback_windows:
                playback = meta["capture_end"] >= _playback_windows[-1][0]
        if not text:
            _note_event("infer", text="(空)", **diag)
            return
        if playback:
            # 播报期间/交叉段的自听结果：不进转写，更不产生口令
            _note_event("playback-drop", text=text[:40], **diag)
            return
        _note_event("infer", text=text[:60], **diag)
        with _lock:
            _results.append({"id": _next_result_id, "ts": time.time(),
                             "text": text, "diag": diag})
            _next_result_id += 1
            del _results[:-MAX_RESULTS]
        self._emit_command(text, meta, diag)

    def _emit_command(self, text: str, meta: dict, diag: dict) -> None:
        """整句「开始」才是口令；反例、迟到、重复、武装外全部拒绝。"""
        global _next_command_seq
        normalized = text.strip().strip(COMMAND_STRIP)
        if normalized != COMMAND_TEXT:
            if COMMAND_TEXT in text:
                _note_event("command-reject", text=text[:40], reason="not-exact")
            return
        with _lock:
            session = _session
            if session is None:
                _note_event("command-reject", reason="not-armed")
                return
            if session.get("consumed"):
                _note_event("command-reject", reason="consumed")
                return
            if meta["capture_end"] < session["armed_at"]:
                _note_event("command-reject", reason="pre-arm")
                return
            if diag["age_ms"] > COMMAND_MAX_AGE_S * 1000:
                _note_event("command-reject", reason="late")
                return
            session["consumed"] = True      # 一次有效触发即消费该会话
            event = {"seq": _next_command_seq, "boot_id": _boot_id,
                     "session_id": session["id"], "text": text,
                     "age_ms": diag["age_ms"], "queue_ms": diag["queue_ms"],
                     "infer_ms": diag["infer_ms"]}
            _next_command_seq += 1
            _commands.append(event)
            del _commands[:-20]
        _note_event("command", **{k: v for k, v in event.items() if k != "boot_id"})


_supervisor = _InferenceSupervisor()


def _submit_warmup() -> None:
    """推理预热：0.5 秒静音段过一遍推理。

    2026-11-21 现场规律：fork 出的子进程首个真实任务有较大概率挂起
    （torch 状态继承所致），30 秒超时回收后一切正常。把首转安排在
    最不紧要的时刻（服务/采音刚启动、演示尚未武装），挂起被静默
    回收吸收，听众毫无感知。静音段转写结果为空，不产生任何输出。"""
    if _MODEL is not None and _supervisor.proc is not None:
        _supervisor.submit(np.zeros(int(0.5 * asr_vad.SAMPLE_RATE), dtype=np.float32),
                           time.monotonic() - 0.6)


def _pump_loop() -> None:
    """结果泵 + 看门狗：持续回收子进程结果并做超时判定。"""
    while True:
        _supervisor.pump()
        _supervisor.track_current()
        _supervisor.watchdog()
        time.sleep(0.2)


# ---------- 采音 ----------

GUARD_SPEECH_RMS = 0.035   # 播报后守卫：窗能量高于此视为机器人还在发声
GUARD_QUIET_MS = 300       # 连续静音达到此时长才确认播报真正结束
GUARD_CAP_MS = 6000        # 守卫上限：异常情况下不能永久吞麦


def _capture_loop() -> None:  # pragma: no cover - 需要 arecord 设备
    """读 arecord 管道并经段切器切段，段提交推理子进程。

    播报自听抑制分两段（2026-11-21 终版）：播报窗口内整段丢弃；窗口
    到期后进入「能量守卫」——TTS 实际语速与估算必有偏差，估算收窗要么
    吞掉操作员紧跟的口令、要么漏出机器人自己的尾巴（实测漏出的
    「领导，请您指示」被切成段、吞掉了紧跟的口令）。守卫按麦克风能量
    判定：机器人还在出声就继续丢，真正静音 0.3 秒才恢复收音。

    单窗异常只记事件不退出；线程退出时把采音旗子放下（自愈可见）。"""
    global _capturing, _playback_skipped, _mic_last_window
    segmenter = asr_vad.Segmenter()
    stream = _capture_proc.stdout if _capture_proc else None
    if stream is None:
        _capturing = False
        return
    guarding = False
    guard_quiet_ms = 0
    guard_elapsed_ms = 0
    try:
        while _capturing:
            chunk = stream.read(BYTES_PER_WIN)
            if not chunk:
                break
            _mic_last_window = time.monotonic()   # 数据流心跳：就绪检测据此判断
            try:
                stereo = np.frombuffer(chunk, dtype="<i2").reshape(-1, 2).astype(np.float32) / 32768.0
            except Exception as error:  # noqa: BLE001 - 单窗失败不杀死采集
                _note_event("capture-error", error=str(error)[:120])
                continue
            if _playback_active:
                _playback_skipped += 1
                guarding = True            # 窗口结束后转入能量守卫
                guard_quiet_ms = guard_elapsed_ms = 0
                continue
            if guarding and guard_elapsed_ms < GUARD_CAP_MS:
                rms = max(float(np.sqrt(np.mean(stereo[:, 0] ** 2))),
                          float(np.sqrt(np.mean(stereo[:, 1] ** 2))))
                if rms >= GUARD_SPEECH_RMS:
                    _playback_skipped += 1   # 机器人尾音未落：继续丢弃
                    guard_quiet_ms = 0
                    guard_elapsed_ms += asr_vad.WIN_MS
                    continue
                guard_quiet_ms += asr_vad.WIN_MS
                guard_elapsed_ms += asr_vad.WIN_MS
                if guard_quiet_ms < GUARD_QUIET_MS:
                    continue
                guarding = False           # 确认静音 0.3s：恢复正常收音
            try:
                segments, _stat = segmenter.feed(stereo.T,
                                                  speech_hint=_hw_speech if _hw_vad_ok else None)
            except Exception as error:  # noqa: BLE001
                _note_event("capture-error", error=str(error)[:120])
                continue
            for segment in segments:
                _supervisor.submit(segment.audio, segment.capture_end_mono)
    finally:
        _capturing = False


# ---------- HTTP ----------

class SessionRequest(BaseModel):
    action: str       # "arm" | "disarm"


class PlaybackRequest(BaseModel):
    active: bool


@app.on_event("startup")
def _startup() -> None:
    """启动：记录环境版本；模型加载完成后 fork 出推理子进程；起结果泵。"""
    print(f"[asr] {_versions_line()}", flush=True)

    def load_then_fork() -> None:
        _load_model()
        if _MODEL is not None:
            _supervisor.start()
            _submit_warmup()

    threading.Thread(target=load_then_fork, daemon=True, name="model-load").start()
    threading.Thread(target=_pump_loop, daemon=True, name="infer-pump").start()
    # 硬件 VAD 的 USB 连接全部放后台线程（libusb 初始化可能阻塞）
    def _hw_vad_starter():
        connected = _xvf_find() is not None
        print(f"[asr] XVF3800 hardware VAD: {'connected' if connected else 'not found (non-fatal)'}",
              flush=True)
        if connected:
            _hw_vad_loop()
    threading.Thread(target=_hw_vad_starter, daemon=True, name="hw-vad").start()
    # 开机自动起采音（2026-11-25 根因修复）：此前开机不采集，须手动
    # /asr/start 才有数据流——两天内两次把演示准备阶段卡到超时。
    asr_start()
    threading.Thread(target=_capture_watchdog, daemon=True, name="capture-watchdog").start()


def _capture_watchdog() -> None:  # pragma: no cover - 需要 arecord 设备
    """采集看门狗（2026-11-25）：线程死亡或数据流停更 >15s 时自动重启。

    现场规律：XVF3800 偶发零数据把 arecord 拖成僵尸、采集线程退出，
    服务自身无自愈（健康页只如实报告 capturing=False）。停更判定用
    _mic_last_window 心跳——播放抑制只丢弃内容不停止读数，窗心跳
    仍更新，不受影响。"""
    while True:
        time.sleep(5)
        try:
            thread = _capture_thread
            dead = thread is None or not thread.is_alive()
            stale = (_capturing and (_mic_last_window is None
                                     or time.monotonic() - _mic_last_window > 15.0))
            if dead or stale:
                print(f"[asr] watchdog: capture {'dead' if dead else 'stale'} → restart",
                      flush=True)
                asr_stop()
                time.sleep(0.5)
                asr_start()
        except Exception as error:  # noqa: BLE001 - 看门狗自身绝不退出
            print(f"[asr] watchdog error: {error!r}", flush=True)


@app.get("/health")
def health() -> dict:
    with _lock:
        session = dict(_session) if _session else None
    infer = {"queue_depth": len(_supervisor.pending),
             "recoveries": _supervisor.recoveries,
             "fault": _supervisor.fault,
             "last_ok_age_ms": round((time.monotonic() - _supervisor.last_ok_mono) * 1000)
             if _supervisor.last_ok_mono else None,
             "recent_infer_ms": sorted(_supervisor.recent_infer_ms)}
    if _supervisor.current:
        infer["current_req"] = _supervisor.current[0]
        infer["current_ms"] = round((time.monotonic() - _supervisor.current[1]) * 1000)
    mic_recent = (_capturing and _mic_last_window is not None
                  and time.monotonic() - _mic_last_window < 3.0)
    return {"boot_id": _boot_id, "model_ready": _MODEL is not None,
            "model_error": _model_error or None,
            "capturing": _capturing, "playback_active": _playback_active,
            "mic_recent": mic_recent, "playback_skipped_windows": _playback_skipped,
            "hw_vad": {"speech": _hw_speech, "ok": _hw_vad_ok},
            "session": session, "inference": infer}


@app.post("/asr/start")
def asr_start() -> dict:
    """开始采音（幂等）。恢复采音不会自动开启新的演示待命会话。

    自愈：旗子亮但采集线程已死（历史异常）时重启线程而非直接返回。"""
    global _capturing, _capture_proc, _capture_thread
    if _capturing and _capture_thread is not None and _capture_thread.is_alive():
        return {"ok": True, "already": True}
    if _capture_proc is None or _capture_proc.poll() is not None:
        _capture_proc = subprocess.Popen(ARECORD, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, bufsize=1 << 16)
    _capturing = True
    _capture_thread = threading.Thread(target=_capture_loop, daemon=True, name="asr-capture")
    _capture_thread.start()
    _submit_warmup()              # 采音启动即预热：首转挂起不留给待命期
    return {"ok": True, "already": False}


@app.post("/asr/stop")
def asr_stop() -> dict:
    global _capturing, _capture_proc
    if not _capturing:
        return {"ok": True, "already": True}
    _capturing = False
    proc = _capture_proc
    _capture_proc = None
    if proc is not None:
        proc.terminate()   # 关管道让采集线程的阻塞读返回 EOF
    return {"ok": True, "already": False}


@app.get("/asr/text")
def asr_text(since: int = 0) -> dict:
    with _lock:
        items = [dict(item) for item in _results if item["id"] > since]
        last_id = _results[-1]["id"] if _results else since
    return {"items": items, "last_id": last_id}


@app.post("/asr/session")
def asr_session(payload: SessionRequest) -> dict:
    """演示待命会话：arm 建会话（采样自此之后才算），disarm 立即作废。

    会话作废或服务重启（boot_id 变化）后，仍在排队的旧段即使
    转写出「开始」也不会触发。"""
    global _session
    if payload.action == "arm":
        with _lock:
            _session = {"id": uuid.uuid4().hex[:12],
                        "armed_at": time.monotonic(), "consumed": False}
            session_id = _session["id"]
        return {"ok": True, "session_id": session_id, "boot_id": _boot_id}
    if payload.action == "disarm":
        with _lock:
            _session = None
        return {"ok": True, "disarmed": True}
    return {"ok": False, "error": "action 必须是 arm 或 disarm"}


@app.get("/asr/commands")
def asr_commands(since: int = 0) -> dict:
    """口令命令事件（上位机消费）。boot_id 与会话 id 由上位机校验。

    响应附当前会话概要：监督回收会作废会话（任务书第四节），上位机据此
    发现失效并重新武装，避免待命聋死。"""
    with _lock:
        items = [dict(item) for item in _commands if item["seq"] > since]
        last_seq = _commands[-1]["seq"] if _commands else since
        session = ({"id": _session["id"], "consumed": _session["consumed"]}
                   if _session else None)
    return {"items": items, "last_seq": last_seq, "boot_id": _boot_id,
            "session": session}


@app.post("/asr/playback")
def asr_playback(payload: PlaybackRequest) -> dict:
    """标记机器人播报窗口：期间丢弃采音，交叉段不进转写也不触发口令。"""
    global _playback_active
    now = time.monotonic()
    with _lock:
        if payload.active:
            _playback_windows.append((now, now))
            _playback_windows[:] = _playback_windows[-5:]
            _playback_active = True
        else:
            if _playback_windows:
                start, _ = _playback_windows[-1]
                _playback_windows[-1] = (start, now)
            _playback_active = False
    return {"ok": True}


@app.get("/asr/debug")
def asr_debug() -> dict:
    with _lock:
        events = list(_debug_events)
    return {"boot_id": _boot_id, "events": events[-40:]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8098, log_level="warning")
