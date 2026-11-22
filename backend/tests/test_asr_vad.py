"""VAD 切段器的离线回放回归（任务书 2026-11-20 第二节）。

纯 NumPy、无模型/SDK 依赖：直接回放合成 PCM，覆盖「长时间静音后说
开始」的切段缺陷复现、短口令不被最短段长过滤、超长语音的 4 秒硬上限、
待命内存有界与首字完整。
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import numpy as np
import pytest

SPEC = spec_from_file_location(
    "asr_vad", Path(__file__).resolve().parents[2] / "scripts" / "asr" / "asr_vad.py")
asr_vad = module_from_spec(SPEC)
import sys
sys.modules["asr_vad"] = asr_vad   # dataclass 注解解析需要模块已注册
SPEC.loader.exec_module(asr_vad)

WIN = int(asr_vad.SAMPLE_RATE * asr_vad.WIN_MS / 1000)


def replay(pieces, *, level=0.12):
    """pieces: [(秒, 是否语音)] → 段切器逐窗回放，返回 (完成段, 段切器)。"""
    segmenter = asr_vad.Segmenter()
    audio = np.concatenate(
        [asr_vad.synth(seconds, level=level if speech else 0.0, seed=11 + i)
         for i, (seconds, speech) in enumerate(pieces)], axis=1)
    done = []
    for start in range(0, audio.shape[1] - WIN + 1, WIN):
        segments, _ = segmenter.feed(audio[:, start:start + WIN])
        done.extend(segments)
    return done, segmenter


def test_long_silence_then_short_command_is_one_fresh_segment():
    """任务书复现用例：12 秒静音＋0.75 秒语音＋1.05 秒静音。

    基线缺陷：静音累计进段，产出 12.21 秒巨段。修复后应为单个
    约 1.7 秒的新鲜段（前卷 0.3 + 语音 0.75 + 尾静音 0.7）。"""
    done, _ = replay([(12.0, False), (0.75, True), (1.05, False)])
    assert len(done) == 1
    segment = done[0]
    assert segment.total_samples <= int(2.5 * asr_vad.SAMPLE_RATE)
    # 语音样本 ≈ 0.75 秒（±两个窗的边界抖动），首字完整由前卷保证
    assert segment.speech_samples == pytest.approx(0.75 * asr_vad.SAMPLE_RATE, abs=WIN * 3)
    assert segment.total_samples - segment.speech_samples >= int(0.28 * asr_vad.SAMPLE_RATE)


def test_thirty_seconds_silence_memory_stays_bounded():
    """待命期音频不得累计：静音再长，段切器内部也只有前卷环。"""
    done, segmenter = replay([(30.0, False), (0.5, True), (1.2, False)])
    assert len(done) == 1
    assert len(segmenter._preroll) <= segmenter._preroll.maxlen
    assert not segmenter._segment


def test_fast_short_command_not_filtered_by_minimum():
    """快速短口令（约 0.4 秒语音）不被最短段长过滤。"""
    done, _ = replay([(2.0, False), (0.4, True), (1.0, False)])
    assert len(done) == 1


def test_pure_noise_burst_dropped():
    """机器人噪音冲击（起声后立即消失）语音样本不足，整段丢弃。"""
    done, _ = replay([(1.0, False), (0.1, True), (1.0, False)])
    assert done == []


def test_long_continuous_speech_hard_cap():
    """连续语音 10 秒：按 4 秒硬上限切分，每段入队样本不超过上限。"""
    done, _ = replay([(1.0, False), (10.0, True), (1.0, False)])
    assert len(done) >= 3
    for segment in done:
        # 上限含前卷与尾静音；允许一个窗的边界余量
        assert segment.total_samples <= int(4.0 * asr_vad.SAMPLE_RATE) + WIN
    # 三段语音样本合计 ≈ 10 秒
    total_speech = sum(s.speech_samples for s in done)
    assert total_speech == pytest.approx(10.0 * asr_vad.SAMPLE_RATE, rel=0.05)


def test_channel_pick_follows_energy():
    """双路不对称（ch1 语音、ch0 近静默）：择路应选 ch1。"""
    done, _ = replay([(1.0, False), (0.8, True), (1.0, False)])
    assert done[0].channel == 1


def test_capture_end_monotonic_increases():
    """段的采音结束时刻单调递增，供服务层做新鲜度判定。"""
    done, _ = replay([(1.0, False), (10.0, True), (1.0, False)])
    times = [s.capture_end_mono for s in done]
    assert times == sorted(times)
