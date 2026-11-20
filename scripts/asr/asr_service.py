#!/usr/bin/env python3
"""H2 语音识别服务（PC3 本地闭环）：reSpeaker 采集 + Qwen3-ASR 本地转写。

链路：arecord 常驻管道（hw:Array,0 固定 16k/2ch/S16）→ 单声道化 →
能量 VAD 切段 → Qwen3-ASR 推理 → 环形结果缓冲。采声与识别都在本机，
上位机只拉取文本。模型进程常驻；/asr/start 与 /asr/stop 只控制采集，
对应上位机的数据阀门（关闭=停采，重开=秒级恢复，不重启模型）。

HTTP（host 网络，即 PC3:8098）：
  GET  /health           模型与采集状态
  POST /asr/start        开始采集（幂等）
  POST /asr/stop         停止采集（幂等，模型与已识别文本保留）
  GET  /asr/text?since=N 增量取转写：{items:[{id,ts,text}], last_id}
"""
from __future__ import annotations

import queue
import subprocess
import threading
import time

import numpy as np
import uvicorn
from fastapi import FastAPI
from qwen_asr import Qwen3ASRModel

MODEL_PATH = "/home/unitree/models/Qwen3-ASR-1.7B"
ARECORD = ["arecord", "-D", "hw:Array,0", "-f", "S16_LE", "-r", "16000", "-c", "2",
           "-t", "raw", "--quiet", "-"]
SAMPLE_RATE = 16000
WIN_MS = 30                    # 能量判定窗；立体声 int16 每样本 4 字节
BYTES_PER_WIN = int(SAMPLE_RATE * WIN_MS / 1000) * 4
SPEECH_ENTER_RATIO = 4.0       # 进入语音：窗能量 ≥ 噪声底 × 此倍数
SPEECH_EXIT_RATIO = 2.0        # 退出语音：窗能量 < 噪声底 × 此倍数
ENTER_FRAMES = 6               # 连续 180ms 超门限才算说话开始
EXIT_MS = 700                  # 静音 0.7 秒断句
MAX_SEGMENT_S = 12.0           # 单段最长，超过强制切段
MIN_SEGMENT_S = 0.5            # 过短片段丢弃（大概率是机器人噪音冲击）
MAX_RESULTS = 100              # 环形结果条数
FLOOR_WINDOWS = 200            # 噪声底滑动窗（约 6 秒），取 20 百分位
FLOOR_CAP = 0.05               # 噪声底上限：远高于任何环境底噪，防追着语音涨

app = FastAPI()

_lock = threading.Lock()
_model: Qwen3ASRModel | None = None
_capturing = False
_capture_proc: subprocess.Popen | None = None
_results: list[dict] = []      # [{id, ts, text}]
_next_id = 1
_rms_history: list[float] = []  # 滑动窗内的窗能量，用于百分位噪声底
_noise_floor = 0.005
_debug_events: list[dict] = []  # 最近的状态跳变与段落事件，供现场标定
_segments: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=4)


def _note_event(kind: str, **fields) -> None:
    with _lock:
        _debug_events.append({"t": round(time.time(), 2), "kind": kind, **fields})
        del _debug_events[:-60]


def _update_noise_floor(rms: float, speaking: bool) -> None:
    """百分位噪声底：取滑动窗的低分位。语音窗只会抬高少数样本，
    低分位几乎不动，从根本上避免门限追着说话声上涨。"""
    global _rms_history, _noise_floor
    if speaking:
        return  # 说话期间不学噪声
    _rms_history.append(rms)
    del _rms_history[:-FLOOR_WINDOWS]
    if len(_rms_history) >= 20:
        floor = float(np.percentile(_rms_history, 20))
        _noise_floor = min(FLOOR_CAP, max(0.0005, floor))


def _load_model() -> None:
    global _model
    _model = Qwen3ASRModel.from_pretrained(MODEL_PATH)


@app.on_event("startup")
def _startup() -> None:
    threading.Thread(target=_load_model, daemon=True, name="model-load").start()
    threading.Thread(target=_inference_loop, daemon=True, name="asr-infer").start()


def _mono_from_stereo(raw: bytes) -> np.ndarray:
    """立体声原始块 → (2, N) 浮点两路。

    实测该阵列两路不对称：ch1 承载语音、ch0 近乎静默（疑似处理输出+
    参考路），直接平均会把语音稀释掉。因此保留两路，由上层按能量择路。
    """
    stereo = np.frombuffer(raw, dtype="<i2").reshape(-1, 2).astype(np.float32) / 32768.0
    return stereo.T


def _enqueue_segment(buf: list[np.ndarray], samples: int, channel: int) -> None:
    """拼接段并选目标声道：实测 ch1 为语音、ch0 近静默，由调用方按段能量择路。"""
    segment = np.concatenate(buf, axis=1)[:, :samples][channel].copy()
    try:
        _segments.put_nowait(segment)
    except queue.Full:
        try:
            _segments.get_nowait()  # 推理跟不上时丢最旧段，保实时性
        except queue.Empty:
            pass
        try:
            _segments.put_nowait(segment)
        except queue.Full:
            pass


def _capture_loop() -> None:
    """读 arecord 管道并按 VAD 切段，段入队供推理线程消费。

    能量按「两路中较高者」判定（实测 ch1 为波束语音、ch0 近乎静默，
    直接平均会稀释语音）；切段时选段内能量更高的声道送推理。
    """
    global _noise_floor
    buf: list[np.ndarray] = []      # 每项 (2, win) 的两路窗
    buf_samples = 0
    energy0 = 0.0
    energy1 = 0.0
    speaking = False
    enter_run = 0
    exit_run_ms = 0.0
    proc = _capture_proc
    if proc is None or proc.stdout is None:
        return
    stream = proc.stdout
    while _capturing:
        chunk = stream.read(BYTES_PER_WIN)
        if not chunk:
            break
        stereo = _mono_from_stereo(chunk)
        buf.append(stereo)
        buf_samples += stereo.shape[1]
        r0 = float(np.sqrt(np.mean(stereo[0] * stereo[0])))
        r1 = float(np.sqrt(np.mean(stereo[1] * stereo[1])))
        energy0 += r0 * r0
        energy1 += r1 * r1
        rms = max(r0, r1)
        if not speaking:
            _update_noise_floor(rms, speaking=False)
            enter_run = enter_run + 1 if rms >= _noise_floor * SPEECH_ENTER_RATIO else 0
            if enter_run >= ENTER_FRAMES:
                speaking = True
                exit_run_ms = 0.0
                _note_event("enter", floor=round(_noise_floor, 5),
                            rms=round(rms, 5), threshold=round(_noise_floor * SPEECH_ENTER_RATIO, 5))
        else:
            if rms < _noise_floor * SPEECH_EXIT_RATIO:
                exit_run_ms += WIN_MS
                if exit_run_ms >= EXIT_MS and buf_samples >= int(MIN_SEGMENT_S * SAMPLE_RATE):
                    _note_event("cut", seconds=round(buf_samples / SAMPLE_RATE, 2),
                                ch=int(energy1 >= energy0))
                    _enqueue_segment(buf, buf_samples,
                                     channel=1 if energy1 >= energy0 else 0)
                    buf, buf_samples, speaking = [], 0, False
                    energy0 = energy1 = 0.0
            else:
                exit_run_ms = 0.0
            if buf_samples >= int(MAX_SEGMENT_S * SAMPLE_RATE):
                _note_event("cut-max", seconds=round(buf_samples / SAMPLE_RATE, 2),
                            ch=int(energy1 >= energy0))
                _enqueue_segment(buf, buf_samples,
                                 channel=1 if energy1 >= energy0 else 0)
                buf, buf_samples, speaking = [], 0, False
                energy0 = energy1 = 0.0


def _inference_loop() -> None:
    """单线程顺序推理：模型串行调用，结果进环形缓冲。"""
    global _next_id
    while True:
        segment = _segments.get()
        if _model is None:
            time.sleep(1.0)      # 模型尚未加载完；该段放弃但避免忙转
            continue
        try:
            # 麦克风电平偏低（实测说话峰值仅 ~0.05），推理前做峰值归一化
            peak = float(np.abs(segment).max())
            if peak > 1e-4:
                segment = segment * (0.3 / peak)
            results = _model.transcribe((segment, SAMPLE_RATE))
            text = " ".join(getattr(r, "text", "") for r in results).strip()
        except Exception as error:  # noqa: BLE001 - 单段失败不拖垮服务
            print(f"[asr] 推理失败：{error}", flush=True)
            continue
        _note_event("infer", text=text[:80] or "(空)")
        if not text:
            continue             # 纯噪音段返回空文本，不入结果
        with _lock:
            _results.append({"id": _next_id, "ts": time.time(), "text": text})
            _next_id += 1
            del _results[:-MAX_RESULTS]


@app.get("/health")
def health() -> dict:
    return {"model_ready": _model is not None, "capturing": _capturing,
            "noise_floor": round(_noise_floor, 5)}


@app.post("/asr/start")
def asr_start() -> dict:
    global _capturing, _capture_proc
    if _capturing:
        return {"ok": True, "already": True}
    proc = subprocess.Popen(ARECORD, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=1 << 16)
    _capture_proc = proc
    _capturing = True
    threading.Thread(target=_capture_loop, daemon=True, name="asr-capture").start()
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


@app.get("/asr/debug")
def asr_debug() -> dict:
    """现场标定用：最近窗能量分布与 VAD 事件。"""
    with _lock:
        history = list(_rms_history)
        events = list(_debug_events)
    return {"noise_floor": round(_noise_floor, 5),
            "rms_p50": round(float(np.percentile(history, 50)), 5) if len(history) >= 20 else None,
            "rms_p95": round(float(np.percentile(history, 95)), 5) if len(history) >= 20 else None,
            "rms_max": round(max(history), 5) if history else None,
            "events": events[-20:]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8098, log_level="warning")
