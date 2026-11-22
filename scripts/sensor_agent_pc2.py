"""PC2 传感器代理，轮询 RS-485 传感器并通过本地 HTTP 接口返回 JSON 读数。

部署在 PC2 上，使用系统 python3 和 pyserial 从 /dev/ttyACM0 读取 TASI 变送器。
该设备对应 10 号接口的 RS-485，传感器 A/B 线与机器人总线约定交叉连接。
GET /environment 仅在机器人外部开发网段提供服务。
读取失败时仍返回上一次有效采样，直到该采样过期，此后报告不可用。
Modbus 逻辑移植自经过现场验证的 read_sensor.py。
使用 Modbus-RTU 9600 8N1，从站地址 0x01，湿度寄存器 0x0000，温度寄存器 0x0001，读数除以 10。

GET /vision 转发头部相机视觉程序（ob_callback，OrbbecSDK 示例改造）的识别结果。
该程序把每帧结果追加写入 detect_log.txt，本接口按需读取日志尾部并解析最新一帧，
返回人数、红绿灯、二维码和中心深度。available 只表示日志可读且解析出过帧，
数据是否新鲜由 age_ms 判断，读取端按采样年龄决定显示或提示视觉程序未运行。
日志路径可用 H2_VISION_LOG 覆盖。

GET /vision/frame 返回最新一帧带检测框的预览图（按扩展名给 Content-Type，
视觉程序用 PNG——该机 OpenCV 未编入 JPEG 编码器），
响应头 X-Frame-Age-Ms 给出该帧的采样年龄（毫秒），无帧时返回 503。
预览文件路径可用 H2_VISION_FRAME 覆盖。
"""

from __future__ import annotations

from datetime import datetime
import json
import os
import re
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import serial

PORT = "/dev/ttyACM0"
BIND = os.getenv("H2_PC2_BIND") or os.getenv("H2_PC2_HOST") or "192.168.124.162"
HTTP_PORT = 8090
POLL_SECONDS = 1.0
FRESH_SECONDS = 8.0
FUNC_READ_HOLDING = 0x03
REG_HUMIDITY = 0x0000
TIMEOUT = 1.0

# 视觉程序在 PC2 上的输出日志。路径含 SDK 版本号，升级 SDK 后用 H2_VISION_LOG 覆盖，
# 或直接修改此默认值并重新部署本代理。
VISION_LOG = os.getenv("H2_VISION_LOG") or (
    "/home/unitree/OrbbecSDK_v2.10.6_202609291004_575347c_linux_x86_64"
    "/examples/build/detect_log.txt")
# 程序约 3Hz 追加一帧，每次请求只读日志末尾一段即可拿到最新帧；
# 日志轮转（截断重写）也无影响，因为不做增量状态跟踪。
VISION_TAIL_BYTES = 65536
# 帧汇总行，例如：
# [2026-11-18 16:56:03.592] person=0 led=1 red=1 green=0 qr=none center=1967mm
# 检出二维码时 qr 为原文（可含空格/中文），且该帧不写 center 字段：
# [2026-11-19 16:44:34.506] person=1 led=0 red=0 green=0 qr=您好，我是中核 404 智能巡检机器人
FRAME_LINE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\] "
    r"person=(\d+) led=(\d+) red=(\d+) green=(\d+) qr=(.*?)(?: center=(\d+)mm)?\s*$")
# 帧内缩进的灯明细行，例如：    LED Red 734mm @(792,610)
LED_LINE = re.compile(r"^\s+LED (\w+) (\d+)mm @\((\d+),(\d+)\)\s*$")
# 预览帧文件：视觉程序每 500ms 原子写入（先写临时文件再 rename）的带检测框 PNG。
# /dev/shm 是内存盘，程序停止后文件会残留，是否在线由采样年龄判断。
VISION_FRAME = os.getenv("H2_VISION_FRAME") or "/dev/shm/latest_annot.png"


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_read_request(addr: int, func: int, start: int, count: int) -> bytes:
    body = struct.pack(">BBHH", addr, func, start, count)
    return body + struct.pack("<H", crc16(body))


def parse_read_response(resp: bytes, addr: int, func: int, count: int) -> bytes:
    if not resp:
        raise ValueError("sensor timeout")
    expected_len = 5 + 2 * count
    if len(resp) != expected_len:
        raise ValueError(f"bad response length: {resp.hex()}")
    if crc16(resp[:-2]) != int.from_bytes(resp[-2:], "little"):
        raise ValueError(f"crc mismatch: {resp.hex()}")
    if resp[0] != addr or resp[1] != func or resp[2] != 2 * count:
        raise ValueError(f"frame mismatch: {resp.hex()}")
    return resp[3:-2]


def read_temp_humi(ser: serial.Serial, addr: int = 1) -> tuple[float, float]:
    ser.reset_input_buffer()
    ser.write(build_read_request(addr, FUNC_READ_HOLDING, REG_HUMIDITY, 2))
    resp = ser.read(9)
    humi_raw, temp_raw = struct.unpack(">Hh", parse_read_response(resp, addr, FUNC_READ_HOLDING, 2))
    return temp_raw / 10, humi_raw / 10


class Latest:
    """用锁保护最近一次有效采样，使读取方无需等待总线通信。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sample: dict | None = None
        self.captured_monotonic = 0.0
        self.instance_id = uuid4().hex
        self.sequence = 0
        self.failures = 0

    def update(self, temp: float, humi: float) -> None:
        with self.lock:
            self.sequence += 1
            self.captured_monotonic = time.monotonic()
            self.sample = {"temperature_c": round(temp, 1), "humidity_percent": round(humi, 1),
                           "captured_at": time.time(), "sample_id": f"{self.instance_id}:{self.sequence}",
                           "source": "pc2-rs485", "ok": True}
            self.failures = 0

    def note_failure(self) -> None:
        with self.lock:
            self.failures += 1

    def snapshot(self) -> dict:
        with self.lock:
            age = time.monotonic() - self.captured_monotonic
            sample = {**self.sample, "age_seconds": age} if self.sample is not None and age < FRESH_SECONDS else None
            return {"available": sample is not None, "sample": sample, "consecutive_failures": self.failures}


LATEST = Latest()


def poll_loop() -> None:
    while True:
        try:
            ser = serial.Serial(PORT, 9600, bytesize=8, parity="N", stopbits=1, timeout=TIMEOUT)
            break
        except serial.SerialException:
            time.sleep(2)
    with ser:
        while True:
            try:
                temp, humi = read_temp_humi(ser)
                LATEST.update(temp, humi)
            except (ValueError, serial.SerialException):
                LATEST.note_failure()
            time.sleep(POLL_SECONDS)


def parse_vision_tail(text: str) -> dict | None:
    """解析日志尾部文本，返回最新一帧的识别结果，无帧时返回 None。

    帧汇总行自带 person/led/red/green/qr/center 全部字段，遇到下一帧汇总行
    即说明上一帧完整；缩进的 LED 明细行归属其前方最近的帧。
    其余行（程序启停标记、参数回显等）全部忽略。
    """
    latest: dict | None = None
    qr_continues = False   # 上一行是帧汇总行且检出二维码（续行归属它）
    for line in text.splitlines():
        frame = FRAME_LINE.match(line)
        if frame:
            ts, person, led, red, green, qr, center = frame.groups()
            latest = {
                "frame_ts": ts,
                "person": int(person),
                "led": int(led),
                "red": int(red),
                "green": int(green),
                # 日志用字面量 none 表示本帧没有二维码，空串同样按无处理。
                "qr": None if qr in ("none", "") else qr,
                # 检出二维码的帧不写 center 字段，与日志的 center=0 语义一致按 0 处理。
                "center_mm": int(center) if center is not None else 0,
                "leds": [],
            }
            qr_continues = latest["qr"] is not None
            continue
        detail = LED_LINE.match(line)
        if detail and latest is not None:
            color, distance, x, y = detail.groups()
            latest["leds"].append({"color": color.lower(), "distance_mm": int(distance),
                                   "x": int(x), "y": int(y)})
            qr_continues = False
            continue
        # 多行二维码：视觉程序把含换行的原文分行写入日志，帧汇总行的
        # 正则只捕获第一行；后续非帧、非缩进行是原文的续行，追加回 qr。
        # 缩进行（"  PERSON ..."/"  QR ..."/"  LED ..."）是检测元数据，不是内容
        if qr_continues and latest is not None:
            if line.startswith((" ", "\t")):
                qr_continues = False   # 缩进行 = 检测明细，二维码原文已结束
            else:
                stripped = line.strip()
                if stripped:
                    latest["qr"] = (latest["qr"] or "") + "\n" + stripped
                else:
                    qr_continues = False   # 空行视为原文结束
    return latest


def vision_snapshot() -> dict:
    """读取视觉日志末尾并返回 /vision 响应。

    帧时间戳是视觉程序在 PC2 本机写的墙上时钟，与本接口在同一台机器
    读取，直接相减得到采样年龄，不引入跨机时钟同步。
    """
    try:
        with open(VISION_LOG, "rb") as log:
            log.seek(0, os.SEEK_END)
            size = log.tell()
            log.seek(max(0, size - VISION_TAIL_BYTES))
            text = log.read().decode("utf-8", errors="replace")
    except OSError as error:
        return {"available": False, "error": f"视觉日志不可读：{error}"[:160]}
    latest = parse_vision_tail(text)
    if latest is None:
        return {"available": False, "error": "视觉日志中没有可解析的帧"}
    try:
        frame_epoch = datetime.strptime(latest["frame_ts"], "%Y-%m-%d %H:%M:%S.%f").timestamp()
    except ValueError as error:
        return {"available": False, "error": f"帧时间戳无法解析：{error}"[:160]}
    return {"available": True, **latest, "age_ms": int(max(0.0, time.time() - frame_epoch) * 1000)}


def vision_frame() -> tuple[bytes, int] | None:
    """读取最新预览帧，返回 (JPEG 字节, 采样年龄毫秒)，无帧时返回 None。

    写方先写临时文件再 rename，读到的一定是完整一帧；程序停止后
    内存盘上的残留文件由采样年龄暴露，读取端据此提示画面停滞。
    """
    try:
        stat = os.stat(VISION_FRAME)
        with open(VISION_FRAME, "rb") as frame:
            data = frame.read()
    except OSError:
        return None
    return data, int(max(0.0, time.time() - stat.st_mtime) * 1000)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - 沿用标准库的方法命名
        if self.path == "/environment":
            self._respond_json(LATEST.snapshot())
        elif self.path == "/vision":
            self._respond_json(vision_snapshot())
        elif self.path == "/vision/frame":
            frame = vision_frame()
            if frame is None:
                self._respond_json({"available": False, "error": "预览帧尚未生成"}, status=503)
                return
            data, age_ms = frame
            # 视觉程序按 imwrite 可用编码器输出 PNG；按扩展名声明类型即可。
            content_type = "image/png" if VISION_FRAME.endswith(".png") else "image/jpeg"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            # 采样年龄放响应头，读取端无需再对时。
            self.send_header("X-Frame-Age-Ms", str(age_ms))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def _respond_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass  # 省略 nohup 中的常规请求日志，通过 consecutive_failures 反映读取失败。


if __name__ == "__main__":
    threading.Thread(target=poll_loop, daemon=True, name="rs485-poll").start()
    print(f"serving http://{BIND}:{HTTP_PORT}/environment", flush=True)
    ThreadingHTTPServer((BIND, HTTP_PORT), Handler).serve_forever()
