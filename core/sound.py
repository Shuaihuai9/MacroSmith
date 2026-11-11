"""状态提示音：开始 / 暂停 / 继续录制与回放各一个短音调。

为什么要提示音：任务栏徽标只有盯屏幕才看得见，而"到底开始录了没"这种事，
用户按下热键时往往正看着别的窗口。一声短促的「上行两声」比任何图标都直接。

为什么不用音频文件、也不放 Qt 多媒体模块：
提示音就是几百毫秒的正弦波，用标准库 ``wave`` 现算 PCM 塞进内存播放，
既不用管打包后的资源路径，也不用给 ``core/`` 引入 Qt 依赖（``core`` 必须零 Qt）。

为什么不阻塞：提示音是异步播放的（``SND_ASYNC``），不会让 170ms 的声音卡住界面线程。
但**不能在内存里异步播**——Windows 的 ``PlaySound`` 明确禁止
``SND_MEMORY | SND_ASYNC`` 组合（会抛 ``RuntimeError: Cannot play asynchronously
from memory``），而用同步播放又会把界面冻住小半秒。所以第一次用到某个音型时，
把生成的 WAV 落到临时目录，之后走 ``SND_FILENAME | SND_ASYNC`` 播放；
临时文件被清掉了会自动重建。

音调设计（同一旋律、两个音区，闭着眼也能分辨"录"和"放"）：

=================  ==========  ====================================
事件               音型        说明
=================  ==========  ====================================
开始录制           低区上行     C5 → G5，两声
暂停录制           低区下行     G5 → C5，两声
继续录制           低区单声     G5
开始回放           高区上行     C6 → E6，两声
暂停回放           高区下行     E6 → C6，两声
继续回放           高区单声     E6
回放结束           高区三连降   E6 → C6 → G5，收尾感
下一步（模式二）   高区短促单声 G6，45ms
上一步（模式二）   中区短促单声 D6，45ms
=================  ==========  ====================================

停止**录制**不发提示音：那是用户主动按停止键的确定动作，而且停止后界面
会立刻恢复可编辑状态，不需要额外确认。停止**回放**发（见 :data:`CUES`
里的 ``replay_stop``）：回放可能是自然播完的，用户此刻很可能在看别处，
"放完了"这件事值得说一声；手动停止与自然播完共用同一个音型。

单步的两个音刻意做得又短又轻（45ms）：模式二下用户会连着按十几次，
长音会吵人。这两个音型属于**可单独关闭**的一组（:data:`STEP_CUES`），
设置页有独立开关——调脚本时可能不想每次嗒一声。
"""

from __future__ import annotations

import math
import os
import struct
import tempfile
from pathlib import Path
from typing import Iterable

#: 采样率（Hz）。16 位单声道，足够提示音用，文件体积可忽略。
SAMPLE_RATE = 44100
#: 音量（0~1）。压得比较低，避免戴耳机被吓一跳。
AMPLITUDE = 0.26
#: 每段音头/音尾的淡入淡出（毫秒），不加会有"咔哒"爆音。
FADE_MS = 6
#: 两段音之间的静默（毫秒）。
GAP_MS = 12

#: 事件 -> 音型；每段是 (频率 Hz, 时长 ms)。
CUES: dict[str, tuple[tuple[int, int], ...]] = {
    "record_start": ((523, 70), (784, 90)),
    "record_pause": ((784, 70), (523, 90)),
    "record_resume": ((784, 90),),
    "replay_start": ((1047, 70), (1319, 90)),
    "replay_pause": ((1319, 70), (1047, 90)),
    "replay_resume": ((1047, 90),),
    # 三连降：和"暂停"的两声下行拉开距离，一听就是"结束了"
    "replay_stop": ((1319, 55), (1047, 55), (784, 85)),
    # 单步：极短单声，连按十几次也不烦
    "step_next": ((1568, 45),),
    "step_prev": ((1175, 45),),
}

#: 单步档：这组可以用独立开关关掉（调脚本时不想每步都响）
STEP_CUES: tuple[str, ...] = ("step_next", "step_prev")

#: 事件 -> 中文说明（设置页/文档/自检共用一处）
CUE_LABELS: dict[str, str] = {
    "record_start": "开始录制",
    "record_pause": "暂停录制",
    "record_resume": "继续录制",
    "replay_start": "开始回放",
    "replay_pause": "暂停回放",
    "replay_resume": "继续回放",
    "replay_stop": "回放结束",
    "step_next": "下一步（模式二）",
    "step_prev": "上一步（模式二）",
}

#: 生成的 WAV 字节缓存，避免每次播放都重算几万个采样点
_WAV_CACHE: dict[str, bytes] = {}
#: 音型 -> 临时 WAV 文件路径（异步播放只能从文件放，见模块说明）
_PATH_CACHE: dict[str, str] = {}

#: 总开关（设置页可关；测试里统一关掉，免得跑测试满屋子响）
_enabled = True
#: 播放后端是否可用（非 Windows 或无声卡时为 False）
_available = False
try:  # pragma: no cover - 平台相关的导入
    import winsound

    _available = True
except ImportError:  # pragma: no cover - 非 Windows
    winsound = None  # type: ignore[assignment]


def is_available() -> bool:
    """当前环境能不能发声（供设置页/自检显示）。"""
    return _available


def set_enabled(enabled: bool) -> None:
    """全局开关。测试里应当设为 False。"""
    global _enabled
    _enabled = bool(enabled)


def is_enabled() -> bool:
    return _enabled


def play(cue: str) -> bool:
    """播放一个提示音；返回是否真的播了（开关关闭/无后端/未知事件时为 False）。

    绝不抛异常：提示音失败不该影响录制或回放。
    """
    if not (_enabled and _available):
        return False
    if cue not in CUES:
        return False
    try:
        path = wav_path(cue)
    except Exception:  # pragma: no cover - 生成/落盘失败只可能是极端情况
        return False
    try:
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC
                           | winsound.SND_NODEFAULT)
        return True
    except Exception:  # pragma: no cover - 无声卡 / 设备被独占
        return False


def wav_bytes(cue: str) -> bytes:
    """取某个提示音的 WAV 字节（测试、试听页与排错用，带缓存）。"""
    return _wav_bytes(cue)


def wav_path(cue: str) -> str:
    """取某个提示音的临时 WAV 文件路径（文件不在时会重新生成）。

    异步播放必须走文件：Windows 不允许 ``SND_MEMORY | SND_ASYNC``。
    """
    cached = _PATH_CACHE.get(cue)
    if cached and os.path.exists(cached):
        return cached
    folder = Path(tempfile.gettempdir()) / "MacroSmith" / "cues"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{cue}.wav"
    path.write_bytes(_wav_bytes(cue))
    _PATH_CACHE[cue] = str(path)
    return str(path)


def duration_ms(cue: str) -> int:
    """某个提示音的总时长（含段间静默），用于测试与文档。"""
    segments = CUES[cue]
    return sum(ms for _freq, ms in segments) + GAP_MS * (len(segments) - 1)


def clear_cache() -> None:
    """清空 WAV 缓存（改了音型参数重算时用；正常路径不需要）。"""
    _WAV_CACHE.clear()
    _PATH_CACHE.clear()


# ======================================================================
# 合成
# ======================================================================
def _wav_bytes(cue: str) -> bytes:
    cached = _WAV_CACHE.get(cue)
    if cached is not None:
        return cached
    data = _render_wav(CUES[cue])
    _WAV_CACHE[cue] = data
    return data


def _render_wav(segments: Iterable[tuple[int, int]]) -> bytes:
    """把 (频率, 时长) 序列渲染成 16 位单声道 WAV。"""
    samples: list[int] = []
    for freq, ms in segments:
        samples.extend(_tone(freq, ms))
        samples.extend([0] * int(SAMPLE_RATE * GAP_MS / 1000))
    # 结尾那段静默去掉，省得按下去之后还拖着半个静音尾巴
    if segments:
        samples = samples[: len(samples) - int(SAMPLE_RATE * GAP_MS / 1000)]

    frames = b"".join(struct.pack("<h", s) for s in samples)
    return _wrap_wav(frames)


def _tone(freq: int, ms: int) -> list[int]:
    """单段正弦波，带线性淡入淡出，防止起落音爆。"""
    count = max(1, int(SAMPLE_RATE * ms / 1000))
    fade = max(1, int(SAMPLE_RATE * FADE_MS / 1000))
    peak = int(32767 * AMPLITUDE)
    step = 2.0 * math.pi * freq / SAMPLE_RATE
    out: list[int] = []
    for i in range(count):
        value = math.sin(step * i) * peak
        if i < fade:                      # 淡入
            value *= i / fade
        elif i > count - fade:            # 淡出
            value *= (count - i) / fade
        out.append(int(value))
    return out


def _wrap_wav(frames: bytes) -> bytes:
    """给 PCM 数据套上 44 字节的 RIFF/WAVE 头。"""
    channels = 1
    bits = 16
    byte_rate = SAMPLE_RATE * channels * bits // 8
    block_align = channels * bits // 8
    header = b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, SAMPLE_RATE,
                                    byte_rate, block_align, bits)
    header += b"data" + struct.pack("<I", len(frames))
    return header + frames
