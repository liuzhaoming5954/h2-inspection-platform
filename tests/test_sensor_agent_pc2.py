"""PC2 传感器代理视觉日志解析的单元验证。

样例行取自 2026-11-18 现场 detect_log.txt 的真实输出，保证解析器
与 ob_callback（OrbbecSDK 示例改造）的日志格式一致。该脚本部署在 PC2 上，
这里直接按路径加载，不依赖 pyserial 之外的运行环境。
"""

from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import time

import pytest


@pytest.fixture
def agent():
    script = Path(__file__).resolve().parents[1] / "scripts" / "sensor_agent_pc2.py"
    spec = spec_from_file_location("sensor_agent_pc2_under_test", script)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 依次包含：无法归类的残行、两帧汇总行、灯明细行和程序退出标记。
LOG_SAMPLE = """someone center=1963mm
[2026-11-18 16:56:01.195] person=0 led=1 red=1 green=0 qr=none center=1985mm
    LED Red 735mm @(792,610)
[2026-11-18 16:56:03.592] person=2 led=1 red=0 green=1 qr=机房 A 巡检点 3 center=1967mm
    LED Green 512mm @(80,90)
===== 程序退出 2026-11-18 16:56:12.930 =====
"""


def test_parse_vision_tail_returns_latest_frame(agent):
    latest = agent.parse_vision_tail(LOG_SAMPLE)
    assert latest == {
        "frame_ts": "2026-11-18 16:56:03.592",
        "person": 2, "led": 1, "red": 0, "green": 1,
        "qr": "机房 A 巡检点 3", "center_mm": 1967,
        "leds": [{"color": "green", "distance_mm": 512, "x": 80, "y": 90}],
    }


def test_parse_vision_tail_keeps_led_details_with_their_own_frame(agent):
    sample = LOG_SAMPLE.splitlines()[:3]
    latest = agent.parse_vision_tail("\n".join(sample))
    # 前一帧的红色灯明细不得串入后一帧。
    assert latest["leds"] == [{"color": "red", "distance_mm": 735, "x": 792, "y": 610}]


def test_parse_vision_tail_without_frames(agent):
    assert agent.parse_vision_tail("===== 程序退出 2026-11-18 16:56:12.930 =====\n") is None
    assert agent.parse_vision_tail("") is None


def test_vision_snapshot_reports_fresh_frame(tmp_path, agent, monkeypatch):
    log = tmp_path / "detect_log.txt"
    frame_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
    log.write_text(f"[{frame_ts}] person=1 led=0 red=0 green=0 qr=none center=0mm\n", encoding="utf-8")
    monkeypatch.setattr(agent, "VISION_LOG", str(log))

    snapshot = agent.vision_snapshot()
    assert snapshot["available"] is True
    assert snapshot["frame_ts"] == frame_ts
    assert 0 <= snapshot["age_ms"] < 3000


def test_vision_snapshot_keeps_stale_frame_for_liveness_judgement(tmp_path, agent, monkeypatch):
    # 视觉程序停止后日志保留最后一帧，采样年龄由读取端判断是否仍在线。
    log = tmp_path / "detect_log.txt"
    log.write_text("[2026-11-01 00:00:00.000] person=0 led=0 red=0 green=0 qr=none center=0mm\n",
                   encoding="utf-8")
    monkeypatch.setattr(agent, "VISION_LOG", str(log))

    snapshot = agent.vision_snapshot()
    assert snapshot["available"] is True
    assert snapshot["age_ms"] > 8 * 24 * 3600 * 1000


def test_vision_snapshot_reports_unreadable_log(tmp_path, agent, monkeypatch):
    monkeypatch.setattr(agent, "VISION_LOG", str(tmp_path / "missing.txt"))
    snapshot = agent.vision_snapshot()
    assert snapshot["available"] is False
    assert "视觉日志不可读" in snapshot["error"]

    log = tmp_path / "empty.txt"
    log.write_text("no frame here\n", encoding="utf-8")
    monkeypatch.setattr(agent, "VISION_LOG", str(log))
    snapshot = agent.vision_snapshot()
    assert snapshot["available"] is False
    assert "没有可解析的帧" in snapshot["error"]


def test_vision_frame_returns_latest_jpeg_with_age(tmp_path, agent, monkeypatch):
    frame_file = tmp_path / "latest_annot.jpg"
    frame_file.write_bytes(b"\xff\xd8fake-jpeg")
    monkeypatch.setattr(agent, "VISION_FRAME", str(frame_file))

    result = agent.vision_frame()
    assert result is not None
    data, age_ms = result
    assert data == b"\xff\xd8fake-jpeg"
    assert 0 <= age_ms < 3000


def test_vision_frame_exposes_stale_residual_file(tmp_path, agent, monkeypatch):
    # 视觉程序停止后内存盘会残留最后一帧，采样年龄负责暴露画面已停滞。
    frame_file = tmp_path / "latest_annot.jpg"
    frame_file.write_bytes(b"old-frame")
    stale = time.time() - 3600
    os.utime(frame_file, (stale, stale))
    monkeypatch.setattr(agent, "VISION_FRAME", str(frame_file))

    result = agent.vision_frame()
    assert result is not None
    assert result[1] > 3500 * 1000


def test_vision_frame_missing_file_returns_none(tmp_path, agent, monkeypatch):
    monkeypatch.setattr(agent, "VISION_FRAME", str(tmp_path / "none.jpg"))
    assert agent.vision_frame() is None
