"""Durable, deterministic robot simulator with no hardware dependencies."""

from __future__ import annotations

import base64
import json
import math
import sqlite3
import struct
import threading
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from h2_gateway.teleop import TICK_SECONDS, SimulatedTeleop, teleop_reply


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def simulated_png(index: int) -> bytes:
    """Draw a synthetic cabinet and a visible SIM mark using only stdlib."""
    width, height = 320, 200
    pixels = [bytearray([18, 28, 45] * width) for _ in range(height)]

    def rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        for y in range(y0, y1):
            for x in range(x0, x1):
                pixels[y][x * 3 : x * 3 + 3] = bytes(color)

    rect(65, 25, 255, 180, (67, 86, 105))
    for slot in range(5):
        y = 33 + slot * 28
        rect(75, y, 245, y + 20, (21, 34, 48))
        rect(85, y + 5, 205, y + 8, (105, 127, 144))
        rect(85, y + 12, 195, y + 15, (105, 127, 144))
        rect(225, y + 6, 235, y + 15, (52, 211, 153))
    # This source is deliberately synthetic and cannot be mistaken for evidence.
    rect(0, 0, 58, 21, (177, 52, 54))
    glyphs = [
        ["11111", "10000", "10000", "11111", "00001", "00001", "11111"],
        ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
        ["10001", "11011", "10101", "10001", "10001", "10001", "10001"],
    ]
    for number, glyph in enumerate(glyphs):
        for y, row in enumerate(glyph):
            for x, bit in enumerate(row):
                if bit == "1":
                    rect(4 + number * 18 + x * 2, 3 + y * 2,
                         6 + number * 18 + x * 2, 5 + y * 2, (255, 255, 255))
    rect(8, 190, 8 + min(index + 1, 20) * 12, 194, (56, 189, 248))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + bytes(row) for row in pixels)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"tEXt", b"Source\x00SIMULATED technical validation image")
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class SimulatorEngine:
    """One simulated robot; its SQLite file is a gateway-local journal."""

    def __init__(self, db_path: str | Path, step_delay: float = 0.8):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.step_delay = max(0.01, step_delay)
        self._lock = threading.RLock()
        self._closed = False
        self._events: dict[str, threading.Event] = {}
        self._threads: list[threading.Thread] = []
        self._teleop = SimulatedTeleop()
        self._motion_updated_at: str | None = None
        self._watchdog_stop = threading.Event()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                progress REAL NOT NULL,
                points_json TEXT NOT NULL,
                samples_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY,
                signature TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        with self._db:
            for key, value in [("sequence", "0"), ("start_count", "0"), ("current_task_id", "")]:
                self._db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES (?,?)", (key, value))
            pending = self._db.execute(
                "SELECT count(*) FROM tasks WHERE status IN ('accepted','running')"
            ).fetchone()[0]
            if pending:
                self._db.execute(
                    "UPDATE tasks SET status='interrupted',reason=?,updated_at=? "
                    "WHERE status IN ('accepted','running')",
                    ("gateway_restarted: simulation was not automatically resumed", utc_now()),
                )
                self._increment("sequence")
        latest = self._db.execute(
            "SELECT samples_json FROM tasks WHERE samples_json != '[]' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if latest:
            samples = json.loads(latest["samples_json"])
            self._teleop.x = samples[-1]["x"]
            self._teleop.y = samples[-1]["y"]
        self._watchdog = threading.Thread(target=self._watch_teleop, name="sim-teleop-watchdog", daemon=True)
        self._watchdog.start()

    def _advance_teleop(self, now: float) -> None:
        if self._teleop.advance(now):
            self._motion_updated_at = utc_now()

    def _watch_teleop(self) -> None:
        while not self._watchdog_stop.wait(TICK_SECONDS):
            with self._lock:
                if self._closed:
                    return
                self._advance_teleop(time.monotonic())

    def teleop_acquire(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            self._advance_teleop(now)
            if self._teleop.token:
                return teleop_reply(False, "teleop_busy", "another simulated teleop session holds control")
            if self._db.execute(
                "SELECT 1 FROM tasks WHERE status IN ('accepted','running') LIMIT 1"
            ).fetchone():
                return teleop_reply(False, "robot_busy", "a simulated inspection holds control")
            self._motion_updated_at = utc_now()
            return self._teleop.acquire(now)

    def teleop_command(self, token: str, sequence: int, action: str, expires_at_ms: int) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            self._advance_teleop(now)
            reply = self._teleop.command(token, sequence, action, expires_at_ms - now_ms(), now)
            if reply["accepted"] and action != "heartbeat":
                self._motion_updated_at = utc_now()
            return reply

    def teleop_release(self, token: str) -> dict[str, Any]:
        with self._lock:
            self._advance_teleop(time.monotonic())
            reply = self._teleop.release(token)
            if reply["accepted"]:
                self._motion_updated_at = utc_now()
            return reply

    def _get_meta(self, key: str) -> str:
        return self._db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0]

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute("UPDATE metadata SET value=? WHERE key=?", (value, key))

    def _increment(self, key: str) -> int:
        number = int(self._get_meta(key)) + 1
        self._set_meta(key, str(number))
        return number

    def _reply(self, accepted: bool, code: str, message: str,
               task_id: str, command_id: str) -> dict[str, Any]:
        return {"accepted": accepted, "code": code, "message": message,
                "task_id": task_id, "command_id": command_id, "simulated": True}

    def _cached(self, command_id: str, signature: str, task_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT signature,result_json FROM commands WHERE command_id=?", (command_id,)
        ).fetchone()
        if row is None:
            return None
        if row["signature"] != signature:
            return self._reply(False, "idempotency_conflict",
                               "command_id already belongs to another request", task_id, command_id)
        return json.loads(row["result_json"])

    def _record(self, command_id: str, signature: str, reply: dict[str, Any]) -> dict[str, Any]:
        self._db.execute(
            "INSERT INTO commands(command_id,signature,result_json,created_at) VALUES (?,?,?,?)",
            (command_id, signature, json.dumps(reply), utc_now()),
        )
        return reply

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            self._advance_teleop(time.monotonic())
            current = self._get_meta("current_task_id")
            row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (current,)).fetchone()
            status = "idle"
            task_status = ""
            progress = 0.0
            updated_at = utc_now()
            if row:
                task_status = row["status"]
                progress = row["progress"]
                status = task_status if task_status in {"running", "stopped", "interrupted"} else "idle"
                updated_at = row["updated_at"]
            if self._motion_updated_at and self._motion_updated_at > updated_at:
                updated_at = self._motion_updated_at
            return {
                "robot_id": "H2-SIM-001", "status": "teleop" if self._teleop.token else status,
                "task_id": current, "task_status": task_status, "progress": progress,
                "sequence": int(self._get_meta("sequence")),
                "battery_percent": round(96.0 - min(50, int(self._get_meta("start_count"))) * 0.25, 2),
                "pose": self._teleop.pose(), "simulated": True,
                "start_count": int(self._get_meta("start_count")), "updated_at": updated_at,
                "teleop_active": bool(self._teleop.token), "motion": self._teleop.motion,
            }

    def start_task(self, task_id: str, command_id: str, expires_at_ms: int,
                   points: list[dict[str, Any]]) -> dict[str, Any]:
        signature = json.dumps({"kind": "start", "task_id": task_id, "points": points},
                               sort_keys=True, separators=(",", ":"))
        with self._lock, self._db:
            self._advance_teleop(time.monotonic())
            if not command_id:
                return self._reply(False, "invalid_command", "command_id is required", task_id, command_id)
            cached = self._cached(command_id, signature, task_id)
            if cached is not None:
                return cached

            def reject(code: str, message: str) -> dict[str, Any]:
                return self._record(command_id, signature,
                                    self._reply(False, code, message, task_id, command_id))

            if expires_at_ms <= now_ms():
                return reject("expired", "expired start command was not executed")
            if not task_id:
                return reject("invalid_task", "task_id is required")
            if not 1 <= len(points) <= 1000:
                return reject("invalid_points", "between 1 and 1000 points are required")
            try:
                normalized = [{"id": str(p["id"]), "label": str(p.get("label", p["id"])),
                               "x": float(p.get("x", 0)), "y": float(p.get("y", 0))} for p in points]
                if any(not p["id"] or not math.isfinite(p["x"]) or not math.isfinite(p["y"])
                       for p in normalized):
                    raise ValueError("invalid point")
                if len({p["id"] for p in normalized}) != len(normalized):
                    raise ValueError("duplicate point")
            except (TypeError, ValueError, KeyError):
                return reject("invalid_points", "point ids must be unique and coordinates finite")
            if self._db.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
                return reject("task_exists", "task_id already exists; query its state instead")
            if self._teleop.token:
                return reject("teleop_busy", "simulated teleop session holds control")
            if self._db.execute(
                "SELECT 1 FROM tasks WHERE status IN ('accepted','running') LIMIT 1"
            ).fetchone():
                return reject("robot_busy", "another simulated task is running")
            timestamp = utc_now()
            self._db.execute(
                "INSERT INTO tasks VALUES (?, 'running', 0, ?, '[]', '', ?, ?)",
                (task_id, json.dumps(normalized), timestamp, timestamp),
            )
            self._set_meta("current_task_id", task_id)
            self._increment("start_count")
            self._increment("sequence")
            reply = self._record(command_id, signature,
                                 self._reply(True, "accepted", "simulated task accepted", task_id, command_id))
            stop_event = threading.Event()
            self._events[task_id] = stop_event
            thread = threading.Thread(target=self._run, args=(task_id, normalized, stop_event),
                                      name=f"sim-{task_id}", daemon=True)
            self._threads.append(thread)
        thread.start()
        return reply

    def _run(self, task_id: str, points: list[dict[str, Any]], stop_event: threading.Event) -> None:
        for index, point in enumerate(points):
            if stop_event.wait(self.step_delay):
                return
            png = simulated_png(index)
            with self._lock:
                if self._closed or stop_event.is_set():
                    return
                with self._db:
                    row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                    if row is None or row["status"] != "running":
                        return
                    samples = json.loads(row["samples_json"])
                    samples.append({
                        "point_id": point["id"], "label": point["label"],
                        "device_id": f"SIM-{point['id']}",
                        "temperature_c": round(23.0 + index * 0.65, 2),
                        "humidity_percent": round(42.0 + index * 0.8, 2),
                        "image_png": base64.b64encode(png).decode("ascii"),
                        "captured_at": utc_now(), "simulated": True,
                        "x": point["x"], "y": point["y"],
                    })
                    status = "completed" if index == len(points) - 1 else "running"
                    self._db.execute(
                        "UPDATE tasks SET status=?,progress=?,samples_json=?,updated_at=? WHERE task_id=?",
                        (status, round((index + 1) / len(points) * 100, 2),
                         json.dumps(samples), utc_now(), task_id),
                    )
                    self._increment("sequence")
                self._teleop.inspection_position(point["x"], point["y"])

    def stop_task(self, task_id: str, command_id: str, expires_at_ms: int) -> dict[str, Any]:
        signature = json.dumps({"kind": "stop", "task_id": task_id}, sort_keys=True, separators=(",", ":"))
        with self._lock, self._db:
            if not command_id:
                return self._reply(False, "invalid_command", "command_id is required", task_id, command_id)
            cached = self._cached(command_id, signature, task_id)
            if cached is not None:
                return cached
            if expires_at_ms <= now_ms():
                reply = self._reply(False, "expired", "expired stop command was not executed", task_id, command_id)
            else:
                row = self._db.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if row is None:
                    reply = self._reply(False, "not_found", "task does not exist", task_id, command_id)
                elif row["status"] in {"completed", "stopped", "interrupted"}:
                    reply = self._reply(True, "already_terminal", "task is already terminal", task_id, command_id)
                else:
                    event = self._events.get(task_id)
                    if event:
                        event.set()
                    self._db.execute(
                        "UPDATE tasks SET status='stopped',reason='operator_stop',updated_at=? WHERE task_id=?",
                        (utc_now(), task_id),
                    )
                    self._increment("sequence")
                    reply = self._reply(True, "stopped", "simulated task stopped", task_id, command_id)
            return self._record(command_id, signature, reply)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            sequence = int(self._get_meta("sequence"))
            if row is None:
                return {"task_id": task_id, "status": "not_found", "progress": 0.0,
                        "samples": [], "reason": "task does not exist", "simulated": True,
                        "sequence": sequence, "updated_at": utc_now()}
            samples = json.loads(row["samples_json"])
            for sample in samples:
                sample["image_png"] = base64.b64decode(sample["image_png"])
            return {"task_id": task_id, "status": row["status"], "progress": row["progress"],
                    "samples": samples, "reason": row["reason"], "simulated": True,
                    "sequence": sequence, "updated_at": row["updated_at"]}

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._teleop.reset()
            self._watchdog_stop.set()
            for event in self._events.values():
                event.set()
        self._watchdog.join(timeout=2)
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=2)
        with self._lock:
            self._db.close()

    def __enter__(self) -> SimulatorEngine:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
