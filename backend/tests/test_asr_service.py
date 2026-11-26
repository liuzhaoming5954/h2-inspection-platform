"""语音识别服务 v3 的逻辑测试（无模型/无麦克风/无 SDK 的工作站可跑）。

覆盖 2026-11-20 任务书的口令会话化与推理监督要求：整句「开始」判定、
否定句/武装前/迟到/重复/播报自听全部拒绝、一次触发即消费、推理子进程
超时回收后会话作废与有界重启、队列丢长保短。推理结果通过直接调用
发布路径注入，不依赖真实模型。
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import time

from fastapi.testclient import TestClient
import numpy as np
import pytest

ASR_DIR = Path(__file__).resolve().parents[2] / "scripts" / "asr"
sys.path.insert(0, str(ASR_DIR))

SPEC = spec_from_file_location("asr_service_under_test", ASR_DIR / "asr_service.py")
service = module_from_spec(SPEC)
sys.modules["asr_service_under_test"] = service
SPEC.loader.exec_module(service)


@pytest.fixture(autouse=True)
def reset_state():
    """模块级状态在用例间复位（会话/事件/播报窗口均为进程内状态）。"""
    service._commands.clear()
    service._results.clear()
    service._next_result_id = 1
    service._next_command_seq = 1
    service._session = None
    service._playback_windows.clear()
    service._playback_active = False
    yield


@pytest.fixture()
def client():
    """应用客户端：启动事件会尝试加载模型（本机失败属预期，不影响逻辑）。"""
    with TestClient(service.app) as http:
        yield http


def publish(text, *, capture_end=None, enqueued=None, age=None):
    """绕过子进程直接注入一条推理结果。age 秒控制采样年龄。"""
    now = time.monotonic()
    # 真实时序：操作员在武装之后开口，默认采样时刻取「当下」
    meta = {"req": publish._next,
            "enqueued": enqueued if enqueued is not None else now - 0.6,
            "capture_end": capture_end if capture_end is not None
            else now - (age if age is not None else 0.0)}
    publish._next += 1
    service._supervisor._publish(meta, text, meta["enqueued"] + 0.1, 4000.0)


publish._next = 1


def commands(http):
    return http.get("/asr/commands").json()["items"]


def arm(http):
    return http.post("/asr/session", json={"action": "arm"}).json()


# ---------- 会话与口令判定 ----------

def test_command_triggers_once_and_consumes(client):
    session = arm(client)
    assert session["ok"] and session["session_id"] and session["boot_id"]
    publish("开始。")
    items = commands(client)
    assert len(items) == 1
    event = items[0]
    assert event["text"] == "开始。"
    assert event["boot_id"] == session["boot_id"]
    assert event["session_id"] == session["session_id"]
    assert event["age_ms"] < 10_000 and event["infer_ms"] == 4000
    # 同一会话再来一次「开始」不得重复触发
    publish("开始")
    assert len(commands(client)) == 1


def test_command_phrase_variants_trigger(client):
    """句中含「开始」即算口令（2026-11-26 现场放宽）：口令常被说成短句。"""
    for phrase in ("开始巡检", "好的开始", "现在开始巡检", "开始了吗"):
        arm(client)                          # 一次触发消费一个会话，逐句重新武装
        publish(phrase)
    items = commands(client)
    assert [item["text"] for item in items] == ["开始巡检", "好的开始", "现在开始巡检", "开始了吗"]
    # 口令句同样作为普通转写进入页面显示
    texts = [item["text"] for item in client.get("/asr/text").json()["items"]]
    assert "开始巡检" in texts


def test_negated_command_also_triggers(client):
    """现场决定不做否定词排除：否定句、转述句同样触发（待命期间须避免谈论含口令的话）。"""
    for phrase in ("不要开始", "还没开始", "他说了开始"):
        arm(client)
        publish(phrase)
    assert [item["text"] for item in commands(client)] == ["不要开始", "还没开始", "他说了开始"]


def test_sentences_without_command_word_rejected(client):
    arm(client)
    for text in ("你好", "现在巡检吧", "开个会"):
        publish(text)
    assert commands(client) == []
    texts = [item["text"] for item in client.get("/asr/text").json()["items"]]
    assert "现在巡检吧" in texts


def test_not_armed_and_disarmed_rejected(client):
    publish("开始")                      # 未武装
    assert commands(client) == []
    arm(client)
    client.post("/asr/session", json={"action": "disarm"})
    publish("开始")                      # 已解除，迟到结果不触发
    assert commands(client) == []


def test_pre_arm_and_late_audio_rejected(client):
    now = time.monotonic()
    session = arm(client)
    publish("开始", capture_end=now - 5.0)     # 武装前采的音
    assert commands(client) == []
    publish("开始", age=12.0)                   # 发布时已超口令年龄上限
    assert commands(client) == []
    publish("开始")                             # 新鲜且武装后 → 触发
    assert len(commands(client)) == 1


def test_playback_window_suppresses_transcript_and_command(client):
    arm(client)
    active = client.post("/asr/playback", json={"active": True})
    assert active.json()["ok"] is True
    publish("开始")                             # 播报期间的自听
    client.post("/asr/playback", json={"active": False})
    assert commands(client) == []
    texts = [item["text"] for item in client.get("/asr/text").json()["items"]]
    assert "开始" not in texts                  # 不进普通转写


def test_stale_result_dropped_entirely(client):
    publish("你好", age=20.0)                   # 超过旧段丢弃线
    texts = [item["text"] for item in client.get("/asr/text").json()["items"]]
    assert "你好" not in texts


# ---------- 推理监督 ----------

class FakeProcess:
    def __init__(self):
        self.pid = 424242
        self.exitcode = None

    def is_alive(self):
        return False

    def kill(self):
        pass

    def join(self, timeout=None):
        pass


@pytest.fixture()
def isolated_supervisor(monkeypatch):
    """监督器隔离：不 fork 真子进程，回收路径可注入。"""
    supervisor = service._InferenceSupervisor()
    monkeypatch.setattr(supervisor, "start", lambda: None)
    supervisor.proc = FakeProcess()
    return supervisor


def test_worker_exit_triggers_recycle_and_invalidates_session(isolated_supervisor, monkeypatch):
    service._session = {"id": "s1", "armed_at": time.monotonic(), "consumed": False}
    isolated_supervisor.pending[1] = {"req": 1, "enqueued": time.monotonic(),
                                      "capture_end": time.monotonic()}
    isolated_supervisor.current = (1, time.monotonic())
    isolated_supervisor.watchdog()
    assert isolated_supervisor.recoveries == 1
    assert isolated_supervisor.pending == {}          # 旧任务作废
    assert service._session is None                   # 会话作废，恢复后须重新武装
    assert not isolated_supervisor.fault


def test_timeout_recycle_and_bounded_fault(isolated_supervisor, monkeypatch):
    isolated_supervisor.proc = type("P", (), {"pid": 1, "is_alive": lambda s: True,
                                              "kill": lambda s: None,
                                              "join": lambda s, timeout=None: None})()
    for _ in range(service.RECOVERY_FAULT_LIMIT):
        isolated_supervisor.current = (99, time.monotonic() - service.INFER_TIMEOUT_S - 1)
        isolated_supervisor.watchdog()
    assert isolated_supervisor.fault is True          # 连续失败进入故障态等待人工
    # 故障后 submit 直接拒绝，不再排任务
    isolated_supervisor.submit(np.zeros(16000, dtype=np.float32), time.monotonic())
    assert isolated_supervisor.pending == {}


def test_submit_drops_longest_keeps_short_fresh():
    supervisor = service._InferenceSupervisor()

    class StubQueue:
        def __init__(self):
            self.items = []

        def put_nowait(self, item):
            if len(self.items) >= 2:
                raise service.queue.Full
            self.items.append(item)

        def get_nowait(self):
            return self.items.pop(0) if self.items else (_ for _ in ()).throw(service.queue.Empty)

    supervisor.task_q = StubQueue()
    supervisor.proc = object()                        # 仅需非 None
    long_audio = np.zeros(16000 * 4, dtype=np.float32)
    short_audio = np.zeros(16000, dtype=np.float32)
    supervisor.submit(long_audio, time.monotonic())   # req 1 长
    supervisor.submit(long_audio, time.monotonic())   # req 2 长（队列满）
    supervisor.submit(short_audio, time.monotonic())  # req 3 短口令 → 挤掉一个长段
    queued = [audio for _, audio in supervisor.task_q.items]
    assert len(queued) == 2
    assert any(audio.size == short_audio.size for audio in queued)
    assert 1 in supervisor.pending and 3 in supervisor.pending
