"""PC2 sensor agent: poll the RS-485 sensor and serve readings as local HTTP JSON.

Deployed on PC2 (system python3 + pyserial), reads the TASI transmitter on
/dev/ttyACM0 (interface-10 RS-485; sensor A/B are crossed against the robot
bus convention). Serves GET /environment on the robot's external dev subnet
only. Read failures serve the last good sample until it expires, then report
unavailable. Ported from the field-proven read_sensor.py Modbus logic
(Modbus-RTU 9600 8N1, slave 0x01, humidity 0x0000 / temperature 0x0001, /10).
"""

from __future__ import annotations

import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
    """Last good sample guarded by a lock; readers never block on the bus."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sample: dict | None = None
        self.failures = 0

    def update(self, temp: float, humi: float) -> None:
        with self.lock:
            self.sample = {"temperature_c": round(temp, 1), "humidity_percent": round(humi, 1),
                           "captured_at": time.time(), "source": "pc2-rs485", "ok": True}
            self.failures = 0

    def note_failure(self) -> None:
        with self.lock:
            self.failures += 1

    def snapshot(self) -> dict:
        with self.lock:
            sample = dict(self.sample) if self.sample else None
            failures = self.failures
        if sample and time.time() - sample["captured_at"] > FRESH_SECONDS:
            sample = None
        return {"available": sample is not None, "sample": sample, "consecutive_failures": failures}


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
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path != "/environment":
            self.send_error(404)
            return
        body = json.dumps(LATEST.snapshot()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass  # keep nohup logs quiet; failures surface via consecutive_failures


if __name__ == "__main__":
    threading.Thread(target=poll_loop, daemon=True, name="rs485-poll").start()
    print(f"serving http://{BIND}:{HTTP_PORT}/environment", flush=True)
    ThreadingHTTPServer((BIND, HTTP_PORT), Handler).serve_forever()
