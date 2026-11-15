import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from h2_gateway.client import GatewayClient
from h2_gateway.engine import SimulatorEngine, now_ms
from h2_gateway.server import create_server
from h2_gateway.teleop import LINEAR_SPEED, MAX_MOTION_SECONDS, SimulatedTeleop


def wait_until(condition, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for simulated state")


def test_heartbeat_renews_lease_but_cannot_extend_direction():
    controller = SimulatedTeleop()
    token = controller.acquire(100)["token"]
    assert controller.command(token, 1, "forward", 500, 100)["accepted"]
    controller.advance(100.2)
    assert controller.command(token, 2, "heartbeat", 350, 100.2)["code"] == "renewed"
    assert controller.lease_deadline == pytest.approx(102.2)
    controller.advance(101)
    assert controller.pose() == pytest.approx({"x": LINEAR_SPEED * MAX_MOTION_SECONDS, "y": 0, "yaw": 0})
    assert controller.motion == "stop"
    assert not controller.token


def test_watchdog_stops_without_reads_and_old_token_cannot_take_over(tmp_path):
    with SimulatorEngine(tmp_path / "gateway.sqlite") as engine:
        token = engine.teleop_acquire()["token"]
        assert engine.teleop_command(token, 1, "forward", now_ms() + 240)["accepted"]

        def released_by_worker():
            # 这里只观察状态，调用 get_state 本身也会按已过去的时间结算运动。
            with engine._lock:
                return not engine._teleop.token

        wait_until(released_by_worker)
        state = engine.get_state()
        assert not state["teleop_active"] and state["motion"] == "stop"
        assert 0 < state["pose"]["x"] <= LINEAR_SPEED * 0.24 + 1e-6
        assert "token" not in state
        new_token = engine.teleop_acquire()["token"]
        assert new_token and new_token != token
        assert engine.teleop_command(token, 2, "forward", now_ms() + 350)["code"] == "invalid_token"
        assert engine.teleop_release(token)["code"] == "invalid_token"
        assert engine.get_state()["teleop_active"]
        assert engine.get_state()["pose"] == state["pose"]


def test_expired_idle_lease_rejects_late_heartbeats():
    controller = SimulatedTeleop()
    token = controller.acquire(100)["token"]
    controller.advance(102.1)
    assert controller.command(token, 1, "heartbeat", 350, 102.1)["code"] == "invalid_token"
    assert controller.motion == "stop"


def test_direction_commands_validate_age_and_order(tmp_path):
    with SimulatorEngine(tmp_path / "gateway.sqlite") as engine:
        token = engine.teleop_acquire()["token"]
        assert engine.teleop_acquire()["code"] == "teleop_busy"
        assert engine.teleop_command(token, 1, "forward", now_ms() - 1)["code"] == "expired"
        assert engine.teleop_command(token, 1, "forward", now_ms() + 1000)["code"] == "invalid_expiry"
        assert engine.teleop_command(token, 1, "jump", now_ms() + 350)["code"] == "invalid_action"
        assert engine.teleop_command(token, 2, "turn_left", now_ms() + 350)["code"] == "applied"
        assert engine.teleop_command(token, 2, "backward", now_ms() + 350)["code"] == "stale_sequence"
        assert engine.teleop_command(token, 1, "stop", now_ms() + 350)["code"] == "stale_sequence"
        assert engine.teleop_command(token, 3, "stop", now_ms() + 350)["code"] == "stopped"
        assert engine.get_state()["motion"] == "stop"
        assert engine.get_state()["teleop_active"]


def test_lease_and_inspection_start_are_atomically_exclusive(tmp_path):
    point = {"id": "cabinet", "label": "A01", "x": 1.2, "y": 0.5}
    with SimulatorEngine(tmp_path / "gateway.sqlite", step_delay=5) as engine:
        gate = threading.Barrier(2)

        def acquire():
            gate.wait(timeout=2)
            return engine.teleop_acquire()

        def start():
            gate.wait(timeout=2)
            return engine.start_task("inspection", "start", now_ms() + 1000, [point])

        with ThreadPoolExecutor(max_workers=2) as pool:
            acquire_future = pool.submit(acquire)
            start_future = pool.submit(start)
            manual, inspection = acquire_future.result(3), start_future.result(3)
        assert manual["accepted"] != inspection["accepted"]
        if manual["accepted"]:
            assert inspection["code"] == "teleop_busy"
        else:
            assert manual["code"] == "robot_busy"


def test_released_pose_persists_until_inspection_reaches_a_point(tmp_path):
    point = {"id": "cabinet", "label": "A01", "x": 1.2, "y": 0.5}
    with SimulatorEngine(tmp_path / "gateway.sqlite", step_delay=0.03) as engine:
        token = engine.teleop_acquire()["token"]
        assert engine.teleop_command(token, 1, "forward", now_ms() + 350)["accepted"]
        wait_until(lambda: engine.get_state()["pose"]["x"] > 0.005)
        assert engine.teleop_release(token)["accepted"]
        pose = engine.get_state()["pose"]
        time.sleep(0.04)
        assert engine.get_state()["pose"] == pose
        assert engine.start_task("inspection", "start", now_ms() + 1000, [point])["accepted"]
        wait_until(lambda: engine.get_task("inspection")["status"] == "completed")
        state = engine.get_state()
        assert state["pose"]["x"] == point["x"]
        assert state["pose"]["y"] == point["y"]
        assert not state["teleop_active"]


def test_restart_does_not_restore_manual_lease_or_motion(tmp_path):
    db = tmp_path / "gateway.sqlite"
    with SimulatorEngine(db) as engine:
        token = engine.teleop_acquire()["token"]
        assert engine.teleop_command(token, 1, "forward", now_ms() + 350)["accepted"]
    with SimulatorEngine(db) as restarted:
        assert not restarted.get_state()["teleop_active"]
        assert restarted.get_state()["motion"] == "stop"
        assert restarted.teleop_command(token, 2, "forward", now_ms() + 350)["code"] == "invalid_token"


def test_real_grpc_teleop_lifecycle_and_pose(tmp_path):
    with SimulatorEngine(tmp_path / "gateway.sqlite") as engine:
        server, port = create_server(engine, port=0)
        server.start()
        try:
            with GatewayClient(f"127.0.0.1:{port}") as gateway:
                token = gateway.teleop_acquire()["token"]
                assert token
                initial = gateway.get_state()
                assert initial["status"] == "teleop" and initial["motion"] == "stop"
                assert gateway.teleop_command(token, 1, "turn_left", now_ms() + 350)["accepted"]
                wait_until(lambda: gateway.get_state()["pose"]["yaw"] > 0.005)
                assert gateway.teleop_command(token, 2, "stop", now_ms() + 350)["code"] == "stopped"
                assert gateway.get_state()["motion"] == "stop"
                assert gateway.teleop_release(token)["code"] == "released"
                state = gateway.get_state()
                assert state["status"] == "idle" and not state["teleop_active"]
                assert state["pose"]["yaw"] > 0
                assert "token" not in state
        finally:
            server.stop(grace=1).wait()
