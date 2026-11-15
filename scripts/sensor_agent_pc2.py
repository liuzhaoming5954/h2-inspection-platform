"""PC2 传感器代理，轮询 RS-485 传感器并通过本地 HTTP 接口返回 JSON 读数。

部署在 PC2 上，使用系统 python3 和 pyserial 从 /dev/ttyACM0 读取 TASI 变送器。
该设备对应 10 号接口的 RS-485，传感器 A/B 线与机器人总线约定交叉连接。
GET /environment 仅在机器人外部开发网段提供服务。
读取失败时仍返回上一次有效采样，直到该采样过期，此后报告不可用。
Modbus 逻辑移植自经过现场验证的 read_sensor.py。
使用 Modbus-RTU 9600 8N1，从站地址 0x01，湿度寄存器 0x0000，温度寄存器 0x0001，读数除以 10。
"""

from __future__ import annotations

import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import serial

PORT = "/dev/ttyACM0"
BIND = "192.168.124.162"
HTTP_PORT = 8090
POLL_SECONDS = 1.0
FRESH_SECONDS = 8.0
FUNC_READ_HOLDING = 0x03
REG_HUMIDITY = 0x0000
TIMEOUT = 1.0


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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - 沿用标准库的方法命名
        if self.path != "/environment":
            self.send_error(404)
            return
        body = json.dumps(LATEST.snapshot()).encode()
        self.send_response(200)
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
