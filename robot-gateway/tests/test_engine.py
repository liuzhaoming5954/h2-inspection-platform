"""Checks focus on command replay, stale motion, stop, and crash recovery."""

import threading
import time

from h2_gateway.engine import SimulatorEngine, now_ms


POINTS = [{"id": "rack-a", "label": "A01", "x": 1.5, "y": 2.0},
          {"id": "rack-b", "label": "B01", "x": 2.5, "y": 2.0}]


def wait_terminal(engine: SimulatorEngine, task_id: str, timeout: float = 3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = engine.get_task(task_id)
        if result["status"] != "running":
            return result
        time.sleep(0.01)
    raise AssertionError("simulated task failed to terminate")


def test_replayed_command_is_persistent_and_never_restarts(tmp_path):
    db = tmp_path / "gateway.sqlite"
    with SimulatorEngine(db, step_delay=0.01) as engine:
        reply = engine.start_task("t1", "start-1", now_ms() + 10_000, POINTS)
        assert reply["accepted"]
        result = wait_terminal(engine, "t1")
        assert result["status"] == "completed"
        assert result["progress"] == 100
        assert len(result["samples"]) == 2
        assert result["samples"][0]["image_png"].startswith(b"\x89PNG\r\n\x1a\n")
        assert result["samples"][0]["simulated"] is True
    with SimulatorEngine(db) as restarted:
        # An old command replay returns the original reply, even after its deadline.
        assert restarted.start_task("t1", "start-1", now_ms() - 1, POINTS) == reply
        assert restarted.get_state()["start_count"] == 1
        assert restarted.get_task("t1")["status"] == "completed"


def test_expired_start_is_rejected_and_cannot_be_revived(tmp_path):
    with SimulatorEngine(tmp_path / "gateway.sqlite") as engine:
        expired = engine.start_task("t1", "old-start", now_ms() - 1, POINTS)
        assert expired["code"] == "expired"
        assert not expired["accepted"]
        assert engine.start_task("t1", "old-start", now_ms() + 10_000, POINTS) == expired
        assert engine.get_task("t1")["status"] == "not_found"
        assert engine.get_state()["start_count"] == 0


def test_stop_does_not_wait_for_point_or_generate_further_samples(tmp_path):
    with SimulatorEngine(tmp_path / "gateway.sqlite", step_delay=5) as engine:
        engine.start_task("t1", "start", now_ms() + 10_000, POINTS)
        started = time.monotonic()
        reply = engine.stop_task("t1", "stop", now_ms() + 10_000)
        assert reply["code"] == "stopped"
        assert time.monotonic() - started < 0.5
        time.sleep(0.03)
        result = engine.get_task("t1")
        assert result["status"] == "stopped"
        assert result["samples"] == []
        assert engine.get_state()["start_count"] == 1


def test_restart_marks_running_task_interrupted_and_does_not_resume(tmp_path):
    db = tmp_path / "gateway.sqlite"
    engine = SimulatorEngine(db, step_delay=5)
    original = engine.start_task("t1", "start", now_ms() + 10_000, POINTS)
    engine.close()
    with SimulatorEngine(db, step_delay=0.01) as restarted:
        assert restarted.get_task("t1")["status"] == "interrupted"
        assert "gateway_restarted" in restarted.get_task("t1")["reason"]
        assert restarted.start_task("t1", "start", now_ms() + 10_000, POINTS) == original
        time.sleep(0.03)
        assert restarted.get_task("t1")["samples"] == []
        assert restarted.get_state()["start_count"] == 1


def test_task_identity_and_control_exclusivity(tmp_path):
    with SimulatorEngine(tmp_path / "gateway.sqlite", step_delay=5) as engine:
        assert engine.start_task("t1", "start", now_ms() + 10_000, POINTS)["accepted"]
        assert engine.start_task("t2", "other", now_ms() + 10_000, POINTS)["code"] == "robot_busy"
        assert engine.start_task("t1", "different", now_ms() + 10_000, POINTS)["code"] == "task_exists"
        assert engine.stop_task("t1", "start", now_ms() + 10_000)["code"] == "idempotency_conflict"
        assert engine.get_state()["start_count"] == 1


def test_stale_stop_and_old_stop_replay_do_not_stop_a_different_task(tmp_path):
    with SimulatorEngine(tmp_path / "gateway.sqlite", step_delay=5) as engine:
        assert engine.start_task("t1", "start-1", now_ms() + 10_000, POINTS)["accepted"]
        assert engine.stop_task("t1", "stale-stop", now_ms() - 1)["code"] == "expired"
        assert engine.get_task("t1")["status"] == "running"
        reply = engine.stop_task("t1", "stop-1", now_ms() + 10_000)
        assert reply["accepted"]
        assert engine.start_task("t2", "start-2", now_ms() + 10_000, POINTS)["accepted"]
        assert engine.stop_task("t1", "stop-1", now_ms() - 1) == reply
        assert engine.get_task("t2")["status"] == "running"
        assert engine.get_state()["start_count"] == 2


def test_concurrent_retries_start_one_execution(tmp_path):
    with SimulatorEngine(tmp_path / "gateway.sqlite", step_delay=5) as engine:
        replies = []
        threads = [threading.Thread(target=lambda: replies.append(
            engine.start_task("t1", "same-command", now_ms() + 10_000, POINTS)
        )) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert len(replies) == 20
        assert all(reply == replies[0] for reply in replies)
        assert replies[0]["accepted"]
        assert engine.get_state()["start_count"] == 1
