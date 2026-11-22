#!/usr/bin/env python3
"""XVF3800 麦克风 + 语音模型独立测试台（PC3 上运行，脱离上位机链路）。

用法（qwen 环境，含 numpy；模型按需加载）：
    cd ~/h2-voice-test && nohup ~/qwen3-asr-env/bin/python mic_bench.py > bench.log 2>&1 &
    浏览器打开 http://<PC3>:8100/

功能（2026-11-21 现场排障需求）：
- 实时双通道电平表、噪声底、VAD 状态、切段日志（时长/择路/能量）
- 可选加载 Qwen3-ASR：逐段转写 + 推理耗时 + 切段到出字总延迟
- 麦克风复活三件套（XVF3800 收音停更的现场处置，按伤害从小到大）：
  1) amixer 通道重开（wiki 记载的采集通道被关问题，无USB 复位）
  2) USB 总线复位（unbind/bind，处置设备彻底挂死）
  3) 2 秒裸采探针（arecord 字节数，快速判设备死活）
- Mute 物理键状态无法软件读取：界面常驻提示（红 LED = 已静音，按一下恢复）
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

import asr_vad

try:
    import usb.core
    _HAS_PYUSB = True
except ImportError:
    _HAS_PYUSB = False

PORT = 8100
MODEL_PATH = "/home/unitree/models/Qwen3-ASR-1.7B"
ARECORD = ["arecord", "-D", "hw:Array,0", "-f", "S16_LE", "-r", "16000",
           "-c", "2", "-t", "raw", "--quiet", "-"]
WIN = int(asr_vad.SAMPLE_RATE * asr_vad.WIN_MS / 1000)

_lock = threading.Lock()
_capturing = False
_proc = None
_meter = {"rms0": 0.0, "rms1": 0.0, "floor": 0.005, "vad": False, "windows": 0,
          "peak0": 0.0, "peak1": 0.0, "enter_ratio": asr_vad.SPEECH_ENTER_RATIO}
_last_window = 0.0            # 数据流心跳（判"旗子绿但耳朵死"）
_segments = []                # [{t, seconds, channel, energy, text?, infer_ms?, latency_ms?}]
_segmenter = None             # 采集线程持有的段切器引用（灵敏度在线调整用）
_model = None
_model_state = "off"          # off | loading | ready | error
_model_error = ""
_usb_path = "1-2"             # XVF3800 在 USB 总线上的位置（现场实测）
_log = []                     # 操作日志（界面显示）

# ---- XVF3800 USB 控制（wiki 2026-11-21 确认的两个关键命令）----
_XVF_PARAMS = {
    "REBOOT":     (48, 7, 1, "wo", "uint8"),   # 固件级重启（轻量恢复，优于 USB 总线复位）
    "DOA_VALUE":  (20, 18, 4, "ro", "uint16"), # [0]=方向 0-359°，[1]=硬件 VAD（1=语音）
    "VERSION":    (48, 0, 3, "ro", "uint8"),
}
_xvf_dev = None                # USB 设备句柄（懒初始化）
_hw_vad = {"speech": False, "doa": -1, "poll_ms": 0}   # 硬件 VAD 状态（供界面/主链路参考）


def _xvf_find():
    """查找并缓存 XVF3800 USB 设备句柄。"""
    global _xvf_dev
    if not _HAS_PYUSB:
        return None
    if _xvf_dev is None:
        _xvf_dev = usb.core.find(idVendor=0x2886, idProduct=0x001A)
    return _xvf_dev


def _xvf_read(name):
    dev = _xvf_find()
    if dev is None:
        return None
    resid, cmdid, length, _, dtype = _XVF_PARAMS[name]
    import struct
    response = dev.ctrl_transfer(
        0xC0, 0, 0x80 | cmdid, resid, length + 1, 10000)   # CTRL_IN | VENDOR | DEVICE
    data = response.tobytes()[1:]                          # 首字节为状态
    if dtype == "uint16":
        return list(struct.unpack("<" + "H" * (length // 2), data))
    return list(data)


def _xvf_write(name, value):
    dev = _xvf_find()
    if dev is None:
        return False
    resid, cmdid, length, _, dtype = _XVF_PARAMS[name]
    dev.ctrl_transfer(
        0x40, 0, cmdid, resid, [value], 10000)             # CTRL_OUT | VENDOR | DEVICE
    return True


def _hw_vad_loop():
    """后台线程 100ms 轮询硬件 VAD 与声源方向（wiki DOA_VALUE 命令）。

    芯片波束级 VAD 比软件能量判定更可靠（不受增益/噪声底漂移影响），
    可替代或辅助 asr_vad 的能量门限。"""
    while True:
        try:
            result = _xvf_read("DOA_VALUE")
            if result and len(result) >= 2:
                with _lock:
                    _hw_vad.update(speech=result[1] == 1, doa=result[0],
                                   poll_ms=int(time.monotonic() * 1000))
        except Exception:  # noqa: BLE001 - USB 偶发失败不终止线程
            with _lock:
                _hw_vad["poll_ms"] = 0    # 标记轮询不可用
        time.sleep(0.1)


def recover_reboot() -> dict:
    """复活⓪：XVF3800 固件级 REBOOT（wiki 命令，最轻量恢复）。"""
    stop_capture()
    ok = _xvf_write("REBOOT", 1)
    time.sleep(3)                 # 固件重启约需 2-3 秒
    probe = probe_device()
    start_capture()
    _note(f"复活⓪ 固件 REBOOT → {'成功' if ok else '失败'}，探针 {probe['bytes']} 字节")
    return {"ok": probe["ok"], "detail": f"REBOOT {'已发送' if ok else '失败'}，探针 {probe['bytes']} 字节"}


def _note(message: str) -> None:
    with _lock:
        _log.append({"t": time.strftime("%H:%M:%S"), "message": message})
        del _log[:-40]


def _sudo_password() -> str:
    """USB 复位需要 root：密码放在本机 ~/.sudo_pass（不进任何仓库）。"""
    try:
        with open(os.path.expanduser("~/h2-voice-test/.sudo_pass")) as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _load_model() -> None:
    global _model, _model_state, _model_error
    _model_state = "loading"
    try:
        from qwen_asr import Qwen3ASRModel
        _model = Qwen3ASRModel.from_pretrained(MODEL_PATH)
        _model_state = "ready"
        _note("识别模型已加载")
    except Exception as error:  # noqa: BLE001 - 失败要显示在界面
        _model_state = "error"
        _model_error = str(error)[:200]
        _note(f"模型加载失败：{_model_error}")


def _infer(segment: np.ndarray, meta: dict) -> None:
    """模型推理（独立线程池逐段执行，测量纯推理与总延迟）。"""
    if _model is None:
        return
    started = time.monotonic()
    try:
        peak = float(np.abs(segment).max())
        if peak > 1e-4:
            segment = segment * (0.3 / peak)
        results = _model.transcribe((segment, asr_vad.SAMPLE_RATE))
        text = " ".join(getattr(r, "text", "") for r in results).strip()
    except Exception as error:  # noqa: BLE001
        text = f"<推理失败: {str(error)[:60]}>"
    infer_ms = (time.monotonic() - started) * 1000
    latency_ms = (time.monotonic() - meta["cut_at"]) * 1000
    with _lock:
        for item in _segments:
            if item["id"] == meta["id"]:
                item.update(text=text, infer_ms=round(infer_ms),
                            latency_ms=round(latency_ms))
                break


def _capture_loop() -> None:  # pragma: no cover - 需要麦克风设备
    global _capturing, _meter, _last_window
    from collections import deque
    global _segmenter
    segmenter = asr_vad.Segmenter()
    _segmenter = segmenter
    preroll_history: deque = deque(maxlen=100)   # 近 3 秒窗能量，界面画水位
    peak0_history: deque = deque(maxlen=333)     # 近 10 秒每窗两路电平（峰值保持）
    peak1_history: deque = deque(maxlen=333)
    stream = _proc.stdout if _proc else None
    if stream is None:
        _capturing = False
        return
    next_id = 1
    infer_pool: list[threading.Thread] = []
    try:
        while _capturing:
            chunk = stream.read(WIN * 4)
            if not chunk:
                _note("arecord 数据流中断（0 字节）——设备可能挂死，尝试下方复活按钮")
                break
            _last_window = time.monotonic()
            stereo = np.frombuffer(chunk, dtype="<i2").reshape(-1, 2).astype(np.float32) / 32768.0
            r0 = float(np.sqrt(np.mean(stereo[:, 0] ** 2)))
            r1 = float(np.sqrt(np.mean(stereo[:, 1] ** 2)))
            preroll_history.append(max(r0, r1))
            segments, _stat = segmenter.feed(stereo.T)
            peak0_history.append(r0)
            peak1_history.append(r1)
            with _lock:
                _meter = {"rms0": round(r0, 5), "rms1": round(r1, 5),
                          "floor": round(segmenter.noise_floor, 5),
                          "vad": segmenter.speaking,
                          "windows": _meter["windows"] + 1,
                          "peak0": round(max(peak0_history), 4),
                          "peak1": round(max(peak1_history), 4),
                          "enter_ratio": segmenter._enter_ratio,
                          "recent": [round(x, 4) for x in preroll_history]}
            for segment in segments:
                meta = {"id": next_id, "cut_at": time.monotonic()}
                next_id += 1
                entry = {"id": meta["id"], "t": time.strftime("%H:%M:%S"),
                         "seconds": round(segment.total_samples / asr_vad.SAMPLE_RATE, 2),
                         "channel": segment.channel,
                         "energy": round(max(segment.energy0, segment.energy1), 5)}
                with _lock:
                    _segments.append(entry)
                    del _segments[:-30]
                if _model is not None:
                    worker = threading.Thread(target=_infer, args=(segment.audio, meta), daemon=True)
                    worker.start()
                    infer_pool = [t for t in infer_pool if t.is_alive()][-4:]
    finally:
        _capturing = False
        if _proc is not None:
            with contextlib.suppress(Exception):
                _proc.wait(timeout=2)


def start_capture() -> dict:
    global _capturing, _proc
    if _capturing:
        return {"ok": True, "already": True}
    # 先清残留：旧 arecord 不退干净会让新进程报 Device busy（2026-11-21 实测）
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.5)
    _proc = subprocess.Popen(ARECORD, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, bufsize=1 << 16)

    def _drain_stderr() -> None:
        error = _proc.stderr.read().decode(errors="replace").strip()
        if error:
            _note(f"arecord 报错：{error[:120]}")

    threading.Thread(target=_drain_stderr, daemon=True).start()
    _capturing = True
    threading.Thread(target=_capture_loop, daemon=True, name="bench-capture").start()
    _note("采音已启动")
    return {"ok": True}


def stop_capture() -> dict:
    global _capturing, _proc
    _capturing = False
    if _proc is not None:
        _proc.terminate()
        try:
            _proc.wait(timeout=2)   # 回收，避免 defunct 僵尸占 ps 也占日志
        except subprocess.TimeoutExpired:
            _proc.kill()
        _proc = None
    _note("采音已停止")
    return {"ok": True}


def recover_amixer() -> dict:
    """复活①：重开采集通道与增益（wiki 记载，无需 USB 复位）。"""
    card = ""
    try:
        with open("/proc/asound/cards") as handle:
            for line in handle:
                if "Array" in line:
                    card = line.strip().split()[-1]
                    break
    except OSError:
        pass
    if not card:
        return {"ok": False, "error": "未在 /proc/asound/cards 找到 Array 设备"}
    outputs = []
    for args in (["cset", "numid=8", "on,on"], ["cset", "numid=10", "60,60"]):
        result = subprocess.run(["amixer", "-c", card] + args,
                                capture_output=True, text=True, timeout=5)
        outputs.append(f"amixer -c {card} {' '.join(args)} → "
                       f"{(result.stdout + result.stderr).strip()[:80]}")
    # 重启采音使通道设置生效
    stop_capture()
    time.sleep(0.5)
    start_capture()
    _note("复活① amixer 通道重开：" + " | ".join(outputs))
    return {"ok": True, "detail": " | ".join(outputs)}


def recover_usb() -> dict:
    """复活②：USB 总线复位（处置设备彻底挂死，现场验证有效）。"""
    password = _sudo_password()
    if not password:
        return {"ok": False, "error": "缺少 ~/h2-voice-test/.sudo_pass"}
    stop_capture()
    subprocess.run(["pkill", "arecord"], capture_output=True)
    command = (f"echo {password} | sudo -S -p '' sh -c "
               f"'echo {_usb_path} > /sys/bus/usb/drivers/usb/unbind; "
               f"sleep 2; echo {_usb_path} > /sys/bus/usb/drivers/usb/bind'")
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=15)
    time.sleep(2)
    probe = probe_device()
    start_capture()
    _note(f"复活② USB 复位完成，裸采探针 {probe['bytes']} 字节")
    return {"ok": probe["ok"], "detail": f"探针 {probe['bytes']} 字节 rms={probe['rms']}"}


def probe_device() -> dict:
    """2 秒裸采探针：绕开一切服务直接问设备要数据。"""
    try:
        result = subprocess.run(ARECORD + ["-d", "2"], capture_output=True, timeout=6)
        data = result.stdout
        if len(data) < 4:
            return {"ok": False, "bytes": len(data), "rms": 0.0}
        arr = np.frombuffer(data[:len(data) // 4 * 4], dtype="<i2").astype(np.float32) / 32768.0
        rms = round(float(np.sqrt(np.mean(arr[1::2] ** 2))), 5)
        return {"ok": len(data) > 100000, "bytes": len(data), "rms": rms}
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "bytes": 0, "rms": 0.0, "error": str(error)[:80]}


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/state":
            with _lock:
                payload = {"capturing": _capturing,
                           "mic_recent": _last_window > 0 and time.monotonic() - _last_window < 3,
                           "meter": _meter, "segments": list(_segments[-12:]),
                           "model": {"state": _model_state, "error": _model_error},
                           "hw_vad": dict(_hw_vad), "pyusb": _HAS_PYUSB,
                           "log": list(_log[-12:])}
            self._json(payload)
        elif self.path == "/":
            with open(os.path.join(os.path.dirname(__file__), "page.html"), "rb") as handle:
                body = handle.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        def set_vad_ratio() -> dict:
            """在线调整起声门限倍率（保留噪声底学习史）。"""
            length = int(self.headers.get("Content-Length") or 0)
            try:
                ratio = float(json.loads(self.rfile.read(length) or b"{}").get("enter", 4.0))
            except ValueError:
                ratio = 4.0
            ratio = min(8.0, max(1.5, ratio))
            with _lock:
                segmenter = _segmenter
            if segmenter is None:
                return {"ok": False, "error": "采音未启动"}
            history = list(segmenter._rms_history)
            floor = segmenter.noise_floor
            fresh = asr_vad.Segmenter(enter_ratio=ratio)
            fresh._rms_history = history
            fresh._noise_floor = floor
            with _lock:
                _segmenter = fresh
            _note(f"起声门限倍率调整为 {ratio}（当前噪声底 {floor:.4f}，"
                  f"门限≈{floor * ratio:.4f}）")
            return {"ok": True, "enter": ratio}
        actions = {"/start": start_capture, "/stop": stop_capture,
                   "/vad": set_vad_ratio, "/recover/reboot": recover_reboot,
                   "/model/on": lambda: (_note("开始加载识别模型（约 5 秒）"),
                                         threading.Thread(target=_load_model, daemon=True).start(),
                                         {"ok": True})[2],
                   "/recover/amixer": recover_amixer,
                   "/recover/usb": recover_usb,
                   "/probe": lambda: probe_device()}
        action = actions.get(self.path)
        if action is None:
            self.send_error(404)
            return
        try:
            self._json(action())
        except Exception as error:  # noqa: BLE001
            self._json({"ok": False, "error": str(error)[:120]}, 500)

    def log_message(self, *args):  # 静默访问日志
        return


if __name__ == "__main__":
    print(f"[bench] http://0.0.0.0:{PORT}", flush=True)
    if _HAS_PYUSB:
        version = _xvf_read("VERSION")
        print(f"[bench] XVF3800 firmware: {version}", flush=True)
        threading.Thread(target=_hw_vad_loop, daemon=True, name="hw-vad").start()
    else:
        print("[bench] pyusb 未安装，硬件 VAD/REBOOT 不可用", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
