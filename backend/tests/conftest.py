from copy import deepcopy
import os
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from h2_backend.app import create_app
from h2_backend.config import Settings
from h2_backend.models import Base


class FakeGateway:
    """接口边界处使用行为固定的测试替身，根目录的端到端测试使用真实的 gRPC。"""
    def __init__(self):
        self.tasks = {}
        self.commands = {}
        self.starts = 0
        self.available = True

    def _check(self):
        if not self.available:
            raise ConnectionError("gateway offline")

    def get_state(self):
        self._check()
        return {"robot_id": "sim-test", "status": "idle", "progress": 0, "start_count": self.starts,
                "simulated": True, "teleop_ready": True}

    def get_task(self, task_id):
        self._check()
        return deepcopy(self.tasks.get(task_id, {"task_id": task_id, "status": "not_found"}))

    def start_task(self, task_id, command_id, expires_at_ms, points):
        self._check()
        if command_id in self.commands:
            return self.commands[command_id]
        accepted = expires_at_ms >= time.time() * 1000
        reply = {"accepted": accepted, "code": "accepted" if accepted else "expired", "task_id": task_id, "command_id": command_id}
        if accepted:
            self.starts += 1
            self.tasks[task_id] = {"task_id": task_id, "status": "running", "progress": 0, "samples": [], "reason": ""}
        self.commands[command_id] = reply
        return reply

    def stop_task(self, task_id, command_id, expires_at_ms):
        self._check()
        task = self.tasks.get(task_id)
        if task:
            task["status"] = "stopped"
        return {"accepted": bool(task), "code": "stopped" if task else "not_found"}

    def complete(self, task_id, points):
        from io import BytesIO
        from PIL import Image
        stream = BytesIO()
        Image.new("RGB", (64, 40), "#3478bb").save(stream, "PNG")
        self.tasks[task_id] = {
            "task_id": task_id, "status": "completed", "progress": 100, "reason": "",
            "samples": [{"point_id": p["id"], "label": p["label"], "device_id": p["id"], "temperature_c": 24.25,
                         "humidity_percent": 45.5, "captured_at": f"2026-10-28T03:00:0{i}+00:00",
                         "image_png": stream.getvalue(), "simulated": True} for i, p in enumerate(points)],
        }


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def settings(tmp_path):
    return Settings(database_url=os.getenv("H2_TEST_DATABASE_URL", f"sqlite:///{tmp_path}/backend.db"),
                    gateway_address="unused", data_dir=tmp_path / "files")


@pytest.fixture
def app(settings, gateway):
    application = create_app(settings, gateway)
    Base.metadata.drop_all(application.state.service.engine)
    yield application
    Base.metadata.drop_all(application.state.service.engine)


@pytest.fixture
def client(app):
    with TestClient(app) as http:
        yield http


@pytest.fixture
def payload():
    return {"name": "机房模拟巡检", "request_id": "request-001", "points": [
        {"id": "rack-01", "label": "A01 机柜", "x": 1.0, "y": 2.0},
        {"id": "rack-02", "label": "A02 机柜", "x": 2.0, "y": 2.0},
    ]}
