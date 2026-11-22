#!/usr/bin/env python3
"""VAD 切段器：纯 NumPy、无服务依赖，供 asr_service 与离线回放测试共用。

2026-11-20 任务书指出的基线缺陷：采音循环在检测到人声之前就累计静音，
且 4 秒上限只在说话分支检查——长时间静音后的第一段实际是「静音+语音」
巨段（复现：12 秒静音＋0.75 秒语音＋1.05 秒静音 → 12.21 秒段），短口令
被拆进长而旧的段里。本模块的状态机修复该缺陷：

- 待命（未说话）只滚动保留 0.3 秒前卷，起声时把前卷并入段首；
- 4 秒硬上限作用于实际入队样本（含前卷与尾静音），超长语音的剩余样本
  继续进入下一段；
- 最短段长只统计语音样本（不含前卷），快速短口令不被过滤；
- 保留双通道按窗能量判定、按段择路的现场标定结论。

接口：feed(两路窗) -> list[Segment]；每窗最多产出 1 个完成段（超长语音
在 4 秒边界处产出后，后续窗继续累积新段）。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

WIN_MS = 30
SAMPLE_RATE = 16000
SPEECH_ENTER_RATIO = 4.0      # 进入语音：窗能量 ≥ 噪声底 × 此倍数
SPEECH_EXIT_RATIO = 2.0       # 退出语音：窗能量 < 噪声底 × 此倍数
ENTER_FRAMES = 6              # 连续 180ms 超门限才算说话开始
EXIT_MS = 700                 # 静音 0.7 秒断句
MAX_SEGMENT_S = 4.0           # 实际入队样本硬上限（含前卷与尾静音）
MIN_SPEECH_S = 0.25           # 段内语音样本下限（不含前卷，快速短口令可过）
PREROLL_S = 0.3               # 待命期滚动前卷长度
FLOOR_WINDOWS = 200           # 噪声底滑动窗（约 6 秒），取 20 百分位
FLOOR_CAP = 0.05
FLOOR_MIN = 0.0005


@dataclass
class Segment:
    """一个完成段：单声道音频与切分诊断量。"""

    audio: np.ndarray            # 已择路的单声道样本
    channel: int                 # 按段能量择路的结果（实测 ch1 为语音路）
    speech_samples: int          # 语音样本数（不含前卷，用于最短段长判定）
    total_samples: int           # 入队总样本（含前卷与尾静音，用于上限判定）
    capture_end_mono: float      # 采音结束时刻（单调时钟秒，供新鲜度判定）
    energy0: float               # 段内 ch0 累计能量（择路诊断）
    energy1: float               # 段内 ch1 累计能量


@dataclass
class WindowStat:
    """单窗统计，供服务层做噪声底学习与调试。"""

    rms0: float
    rms1: float
    speaking: bool


class Segmenter:
    """状态机切段器。窗由调用方读入后逐窗喂入，完成段即时返回。"""

    def __init__(self, *, sample_rate: int = SAMPLE_RATE, win_ms: int = WIN_MS,
                 enter_ratio: float = SPEECH_ENTER_RATIO, exit_ratio: float = SPEECH_EXIT_RATIO,
                 enter_frames: int = ENTER_FRAMES, exit_ms: int = EXIT_MS,
                 max_segment_s: float = MAX_SEGMENT_S, min_speech_s: float = MIN_SPEECH_S,
                 preroll_s: float = PREROLL_S,
                 now: "callable[[], float] | None" = None):
        import time
        self._now = now or time.monotonic
        self._enter_ratio = enter_ratio
        self._exit_ratio = exit_ratio
        self._enter_frames = enter_frames
        self._exit_windows = max(1, int(exit_ms / win_ms))
        self._max_samples = int(max_segment_s * sample_rate)
        self._min_speech = int(min_speech_s * sample_rate)
        self._win_samples = int(sample_rate * win_ms / 1000)
        preroll_windows = max(1, int(preroll_s * sample_rate / self._win_samples))
        self._preroll: deque[np.ndarray] = deque(maxlen=preroll_windows)
        self._noise_floor = 0.005
        self._rms_history: list[float] = []
        # 段内状态
        self._segment: list[np.ndarray] = []
        self._speech_samples = 0
        self._total_samples = 0
        self._energy0 = 0.0
        self._energy1 = 0.0
        self._exit_run = 0
        self._enter_run = 0
        self._speaking = False

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def noise_floor(self) -> float:
        return self._noise_floor

    def _learn_floor(self, rms: float) -> None:
        """百分位噪声底：语音窗不参与学习，防止门限追着说话声上涨。"""
        self._rms_history.append(rms)
        del self._rms_history[:-FLOOR_WINDOWS]
        if len(self._rms_history) >= 20:
            floor = float(np.percentile(self._rms_history, 20))
            self._noise_floor = min(FLOOR_CAP, max(FLOOR_MIN, floor))

    def _cut(self) -> Segment:
        """收尾切段：择路、复位状态，返回完成段。"""
        stereo = np.concatenate(self._segment, axis=1)
        segment = Segment(
            audio=stereo[1 if self._energy1 >= self._energy0 else 0].copy(),
            channel=1 if self._energy1 >= self._energy0 else 0,
            speech_samples=self._speech_samples,
            total_samples=self._total_samples,
            capture_end_mono=self._now(),
            energy0=self._energy0,
            energy1=self._energy1,
        )
        self._segment = []
        self._speech_samples = self._total_samples = 0
        self._energy0 = self._energy1 = 0.0
        self._exit_run = 0
        self._speaking = False
        return segment

    def _stash(self, stereo: np.ndarray, is_speech: bool) -> None:
        self._segment.append(stereo)
        self._total_samples += stereo.shape[1]
        if is_speech:
            self._speech_samples += stereo.shape[1]
        self._energy0 += float(np.mean(stereo[0] * stereo[0]))
        self._energy1 += float(np.mean(stereo[1] * stereo[1]))

    def feed(self, stereo: np.ndarray, speech_hint: bool | None = None) -> tuple[list[Segment], WindowStat]:
        """喂入一个 (2, win) 两路窗，返回 (完成段列表, 单窗统计)。

        speech_hint 来自 XVF3800 芯片级 VAD（asr_service 的 pyusb 轮询）：
        True=芯片检测到语音 → 用较低倍率进门（安静环境/远场也能起声）
        False/None=无芯片信号或检测为静音 → 用正常倍率（噪声免疫）
        窗样本始终进入当前段（一旦起声），静音窗在说话状态下也计入段内。
        """
        r0 = float(np.sqrt(np.mean(stereo[0] * stereo[0])))
        r1 = float(np.sqrt(np.mean(stereo[1] * stereo[1])))
        rms = max(r0, r1)
        done: list[Segment] = []

        if not self._speaking:
            # 待命：静音只进前卷环，不进段；噪声底持续学习
            # 硬件 VAD 提示有语音时用低倍率（1.5），否则用配置倍率
            effective_ratio = 1.5 if speech_hint else self._enter_ratio
            self._learn_floor(rms)
            self._enter_run = self._enter_run + 1 if rms >= self._noise_floor * effective_ratio else 0
            self._preroll.append(stereo)
            if self._enter_run >= self._enter_frames:
                # 起声：前卷并入段首（保证首字完整）；连续超门限的确认窗
                # 本身就是语音，计入语音样本，快速短口令不被最短段长过滤
                self._segment = list(self._preroll)
                self._total_samples = sum(w.shape[1] for w in self._segment)
                confirmed = list(self._preroll)[-self._enter_run:]
                self._speech_samples = sum(w.shape[1] for w in confirmed)
                self._energy0 = float(np.mean(np.concatenate(self._segment, axis=1)[0] ** 2))
                self._energy1 = float(np.mean(np.concatenate(self._segment, axis=1)[1] ** 2))
                self._preroll.clear()
                self._speaking = True
                self._exit_run = 0
        else:
            if rms < self._noise_floor * self._exit_ratio:
                self._exit_run += 1
            else:
                self._exit_run = 0
            self._stash(stereo, is_speech=self._exit_run == 0)
            # 断句：静音期满且语音样本足够；不足则整段丢弃（大概率是机器人噪音冲击）
            if self._exit_run >= self._exit_windows:
                segment = self._cut()
                done.append(segment) if segment.speech_samples >= self._min_speech else None
            elif self._total_samples >= self._max_samples:
                # 4 秒硬上限（含前卷与尾静音）：立即产出，超限窗已计入下一段起点
                done.append(self._cut())
                # 超长语音还在继续：当前窗（触发上限的这窗）保留进新段
                self._stash(stereo, is_speech=self._exit_run == 0)
                self._speaking = True

        return done, WindowStat(rms0=r0, rms1=r1, speaking=self._speaking)


INTERRUPT = "巡检中断，等待人工处理。"
def synth(seconds: float, *, level: float = 0.0, channels: int = 2,
          seed: int = 7) -> np.ndarray:
    """合成测试音频（静音=0，或定幅白噪声模拟语音窗），返回 (2, N) 两路。

    ch1 承载语音（现场标定结论），ch0 近静默。
    """
    generator = np.random.default_rng(seed)
    n = int(seconds * SAMPLE_RATE)
    voice = (generator.standard_normal(n) * level).astype(np.float32)
    return np.stack([voice * 0.02, voice], axis=0)
