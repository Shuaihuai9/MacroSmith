"""回放模块：模式一（原速时间轴回放）与模式二（单步热键执行）。

两种模式共用同一份原始事件序列，但驱动方式不同：

* **模式一 原速回放**：在后台线程按录制时的毫秒时间戳推进，
  时间轴用 ``time.monotonic()`` 计算“目标时刻 - 起点”，
  而不是逐条 ``sleep(间隔)``，这样累积误差不会被放大。
* **模式二 单步执行**：完全不看时间戳，只按“操作单元”执行；
  每次由外部热键调用 :meth:`MacroPlayer.step_next` / :meth:`step_prev`，
  执行完立即返回（非阻塞），用户可以继续操作其他程序。

忽略自身事件
------------
所有模拟事件都通过 :meth:`MacroPlayer._dispatch` 发出。该方法在执行期间
把 ``_exec_depth`` 计数加一，:meth:`is_executing` 返回 True；录制器读取同一个
标志位后就会跳过这些事件，因此程序自己发出的键鼠事件不会再次被录制。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional, Sequence

from pynput import keyboard, mouse

from core import event_parser as ep
from core.event_parser import Step
from utils.win_utils import clamp_to_virtual_screen

log = logging.getLogger(__name__)

#: 模式枚称常量
MODE_TIMED = "timed"   # 模式一：原速回放
MODE_STEP = "step"     # 模式二：单步热键执行

#: 时间轴等待的片长上限（毫秒）。分片等待才能及时响应暂停/停止。
_WAIT_SLICE_MS = 20
#: 距离目标时刻小于该值（毫秒）时改用微睡眠，提高落点精度。
_BUSY_SLICE_MS = 2
#: 模式二单步执行时，步骤内两条事件之间的等待上限（毫秒）。
#: 「按下 → 释放」保留真实间隔，但不超过该值，避免 UI 长时间阻塞。
_STEP_MAX_GAP_MS = 60
#: 复合步骤（组合键 / 双击三击）内部的等待上限（毫秒）。
#: 这类步骤必须**快速连续**发完：间隔一大，系统就识别不出双击，
#: 组合键也可能被前台程序当成「先按 Ctrl、再按 V」两次独立操作。
#: 5ms 足以让系统按顺序处理，同时远小于双击判定窗口。
_MERGED_STEP_MAX_GAP_MS = 5

# ---------------------------------------------------------------------------
# 循环回放（模式一）
# ---------------------------------------------------------------------------
#: 不循环：跑完一遍就结束
LOOP_NONE = "none"
#: 固定轮数：跑够 N 轮自动停止
LOOP_FIXED = "fixed"
#: 无限循环：一直跑到用户停止
LOOP_INFINITE = "infinite"

#: 全部循环模式（供界面下拉框使用）
LOOP_MODES: tuple[str, ...] = (LOOP_NONE, LOOP_FIXED, LOOP_INFINITE)

#: 轮与轮之间的小间隔（毫秒），避免上一轮末尾与下一轮开头的动作粘连
_LOOP_GAP_MS = 120
#: 事件时间跨度接近 0 时的兜底跨度，防止无限循环把 CPU 占满
_MIN_LOOP_SPAN_MS = 50
#: 固定轮数的取值范围
MIN_LOOP_COUNT = 1
MAX_LOOP_COUNT = 9999


def normalize_loop_mode(mode: Any) -> str:
    """把任意输入归一化成合法的循环模式。"""
    text = str(mode or "").strip().lower()
    return text if text in LOOP_MODES else LOOP_NONE


def normalize_loop_count(count: Any) -> int:
    """把轮数夹到合法区间。"""
    try:
        value = int(count)
    except (TypeError, ValueError):
        value = 1
    return max(MIN_LOOP_COUNT, min(MAX_LOOP_COUNT, value))


class MacroPlayer:
    """键鼠回放器（两种模式）。"""

    def __init__(
        self,
        on_event: Optional[Callable[[int, dict[str, Any]], None]] = None,
        on_finished: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        #: fn(raw_index, event)：每执行一条原始事件后回调（用于列表高亮）
        self._on_event = on_event
        #: fn(reason)：回放结束回调，reason ∈ {"finished", "stopped"}
        self._on_finished = on_finished
        self._on_error = on_error

        self._events: list[dict[str, Any]] = []
        self._steps: list[Step] = []
        self._name = ""

        self.speed = 1.0
        self.mode = MODE_TIMED

        # 忽略自身事件用的可重入深度计数
        self._guard_lock = threading.Lock()
        self._exec_depth = 0

        self._mouse: Optional[mouse.Controller] = None
        self._keyboard: Optional[keyboard.Controller] = None

        # 模式一状态
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()          # 用于唤醒等待中的回放线程
        self._stop_requested = False
        self._paused = False
        self._replaying = False
        self._position = 0                       # 已执行到的原始事件下标

        # 循环回放（只对模式一有效）
        self._loop_mode = LOOP_NONE
        self._loop_count = 1                     # 固定轮数时的目标轮数
        self._loop_round = 0                     # 当前/最近一轮的轮次（1 起）

        # 模式二状态
        self._step_index = 0                     # 已完成/已执行的步骤数（0 = 未开始）
        self._raw_cursor = 0                     # 下一步骤对应的原始事件下标
        self._coord_warned = False               # 越界坐标告警只提示一次

        # 回放期间“真的按下过”的键与鼠标按钮。
        # 收尾时只释放这些，避免发出多余的右键/中键抬起消息（见 _release_all）。
        self._held_keys: set[str] = set()
        self._held_buttons: set[str] = set()
        self._last_move: Optional[tuple[int, int]] = None   # 指针已知位置（去重移动）

    # ==================================================================
    # 基础属性与查询
    # ==================================================================
    @property
    def is_executing(self) -> bool:
        """当前是否正在发送模拟事件（录制器据此忽略自身事件）。"""
        with self._guard_lock:
            return self._exec_depth > 0

    # ------------------------------------------------------------------
    # 循环回放设置
    # ------------------------------------------------------------------
    @property
    def loop_mode(self) -> str:
        """循环模式：``none`` / ``fixed`` / ``infinite``。"""
        return self._loop_mode

    @property
    def loop_count(self) -> int:
        """固定轮数模式下的目标轮数。"""
        return self._loop_count

    @property
    def loop_round(self) -> int:
        """当前正在执行的轮次（从 1 开始；未开始回放时为 0）。"""
        return self._loop_round

    @property
    def loop_rounds_total(self) -> int:
        """总轮数：固定轮数返回目标值，无限循环返回 0（表示未知）。"""
        if self._loop_mode == LOOP_FIXED:
            return self._loop_count
        if self._loop_mode == LOOP_INFINITE:
            return 0
        return 1

    @property
    def loop_rounds_done(self) -> int:
        """已经完整跑完的轮数（用于回放结束后的统计）。"""
        if self._replaying:
            # 当前这一轮还没跑完
            return max(0, self._loop_round - 1)
        return max(0, self._loop_round)

    def set_loop(self, mode: Any = None, count: Any = None) -> None:
        """设置循环模式与轮数（回放中也能改，下一轮生效）。"""
        if mode is not None:
            self._loop_mode = normalize_loop_mode(mode)
        if count is not None:
            self._loop_count = normalize_loop_count(count)

    def loop_progress_text(self) -> str:
        """状态栏用的轮数文案，例如 ``第 2 / 5 轮`` / ``无限循环 · 第 3 轮``。"""
        if self._loop_mode == LOOP_NONE:
            return ""
        current = max(1, self._loop_round)
        if self._loop_mode == LOOP_FIXED:
            return f"第 {current} / {self._loop_count} 轮"
        return f"无限循环 · 第 {current} 轮"

    @property
    def is_replaying(self) -> bool:
        return self._replaying

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def position(self) -> int:
        """模式一：已执行到的原始事件下标。"""
        return self._position

    @property
    def step_index(self) -> int:
        """模式二：当前步骤指针（0 表示尚未执行任何步骤）。"""
        return self._step_index

    @property
    def step_total(self) -> int:
        return len(self._steps)

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    @property
    def steps(self) -> list[Step]:
        return list(self._steps)

    def has_content(self) -> bool:
        return bool(self._events)

    # ==================================================================
    # 载入 / 清空
    # ==================================================================
    def load(self, events: Sequence[dict[str, Any]], name: str = "",
             rebuild_steps: bool = True) -> None:
        """载入事件序列（自动重新分组步骤）。"""
        self._events = [dict(e) for e in events if isinstance(e, dict)]
        self._name = name
        if rebuild_steps:
            self._steps = ep.build_steps(self._events)
        self.reset()

    def set_steps(self, steps: Sequence[Step]) -> None:
        """外部直接注入步骤列表（测试或高级用法）。"""
        self._steps = list(steps)
        self.reset()

    def clear(self) -> None:
        self.stop()
        self._events = []
        self._steps = []
        self._name = ""
        self.reset()

    def reset(self) -> None:
        """把执行指针归零（模式一位置 + 模式二步骤指针）。

        注意：**不重置循环设置** —— 循环是用户偏好，不是运行状态。
        """
        self.stop()
        self._position = 0
        self._step_index = 0
        self._raw_cursor = 0
        self._loop_round = 0
        self.reset_held_state()

    # ==================================================================
    # 控制器（延迟创建，便于测试时替换）
    # ==================================================================
    def _mouse_ctl(self) -> mouse.Controller:
        if self._mouse is None:
            self._mouse = mouse.Controller()
        return self._mouse

    def _keyboard_ctl(self) -> keyboard.Controller:
        if self._keyboard is None:
            self._keyboard = keyboard.Controller()
        return self._keyboard

    def set_controllers(self, mouse_ctl: Any = None, keyboard_ctl: Any = None) -> None:
        """注入自定义控制器（单元测试用，正常流程不需要调用）。"""
        if mouse_ctl is not None:
            self._mouse = mouse_ctl
        if keyboard_ctl is not None:
            self._keyboard = keyboard_ctl

    # ==================================================================
    # 模式一：原速回放
    # ==================================================================
    def start_timed(self, from_index: int = 0, speed: Optional[float] = None) -> bool:
        """开始模式一原速回放。已在回放中则返回 False。"""
        if self._replaying:
            return False
        if not self._events:
            self._notify_error("没有可回放的事件，请先录制或加载脚本")
            return False

        if speed is not None:
            self.speed = max(0.1, min(10.0, float(speed)))
        self.mode = MODE_TIMED
        self._position = max(0, min(int(from_index), len(self._events) - 1))
        self._stop_requested = False
        self._paused = False
        self._replaying = True
        self._wake.clear()
        # 每次开始回放都从第 1 轮算起
        self._loop_round = 0

        # 关键：从当前位置之前的事件里“恢复按键状态”，
        # 这样从中间开始回放时，之前按下的修饰键仍然是按下的。
        # 从中间开始时不做循环（时间轴起点不对，循环会重现错位）。
        if self._position > 0:
            self._restore_state(self._position)
            if self._loop_mode != LOOP_NONE:
                log.info("从中间位置开始回放，本次不再循环")
                self._loop_mode = LOOP_NONE

        self._thread = threading.Thread(
            target=self._timed_loop, name="MacroSmith-TimedPlayback", daemon=True
        )
        self._thread.start()
        return True

    def _restore_state(self, index: int) -> None:
        """分析 [0, index) 区间，重建当前应处于按下状态的键与鼠标按钮。"""
        held_keys: dict[str, dict[str, Any]] = {}
        held_buttons: set[str] = set()
        for evt in self._events[:index]:
            if evt.get("type") == "key":
                key = str(evt.get("key"))
                if evt.get("action") == "press":
                    held_keys[key] = evt
                else:
                    held_keys.pop(key, None)
            elif evt.get("type") == "mouse_click":
                button = str(evt.get("button"))
                if evt.get("action") == "press":
                    held_buttons.add(button)
                else:
                    held_buttons.discard(button)
        for evt in held_keys.values():
            self._dispatch_key(evt, "press")
        for button in held_buttons:
            self._dispatch_button(button, True)

    def _timed_loop(self) -> None:
        """模式一回放主循环（运行在独立线程）。

        支持循环回放：每一「轮」按原始时间戳跑完整段脚本后，
        根据 :attr:`loop_mode` 决定是再来一轮还是结束。
        F7 暂停 / F8 停止在轮与轮之间同样生效。
        """
        try:
            total = len(self._events)
            if total == 0:
                self._replaying = False
                self._paused = False
                self._notify_finished("stopped")
                return

            # 事件时间跨度为 0 时给一个最小轮间隔，避免无限循环把 CPU 打满
            span_ms = ep.events_duration_ms(self._events)
            if span_ms <= 0:
                span_ms = _MIN_LOOP_SPAN_MS

            completed_all = True
            while True:
                self._loop_round += 1
                self._position = 0
                self._restore_state(0)
                base_t = int(self._events[0].get("t") or 0)
                t0 = time.monotonic()
                paused_total = 0.0
                finished_pass = False

                while True:
                    if self._stop_requested:
                        break

                    # 暂停检查必须放在“取事件”之前：否则等待被暂停中断时，
                    # 会立刻把当前事件执行掉，导致暂停期间仍在操作键鼠。
                    if self._paused:
                        # 暂停：记录暂停起点，等待唤醒；唤醒后把暂停时长补进偏移
                        p_start = time.monotonic()
                        self._wake.clear()
                        self._wake.wait(0.2)
                        paused_total += time.monotonic() - p_start
                        continue

                    if self._position >= total:
                        finished_pass = True
                        break

                    evt = self._events[self._position]
                    # 目标时刻 = 起点 + (事件时间戳 - 首事件时间戳) / 速度 + 累计暂停
                    rel_ms = (int(evt.get("t") or 0) - base_t) / max(self.speed, 0.1)
                    target = t0 + paused_total + rel_ms / 1000.0

                    # ---- 长按覆盖（explicit_hold_ms）----
                    # 当前事件是 release，且上一条是同目标 press，且所在步骤设了长按
                    # 则把释放时间推后到 press + hold_ms，并把额外时间计入 paused_total
                    # （后续事件整体平移，与 set_step_duration 行为一致）
                    if (evt.get("action") == "release"
                            and self._position > 0):
                        prev_evt = self._events[self._position - 1]
                        if (prev_evt.get("action") == "press"
                                and self._event_matches(prev_evt, evt)):
                            st = self._step_containing(self._position)
                            if st and st.explicit_hold_ms > 0:
                                orig_hold = max(0, int(evt.get("t") or 0)
                                                - int(prev_evt.get("t") or 0))
                                extra_ms = max(0, st.explicit_hold_ms - orig_hold)
                                if extra_ms > 0:
                                    extra_s = extra_ms / 1000.0 / max(self.speed, 0.1)
                                    target += extra_s
                                    paused_total += extra_s

                    if not self._wait_until(target):
                        break  # 收到停止请求

                    self._execute_event(evt, self._position)
                    self._position += 1

                    # ---- 无限连击（infinite_repeat）----
                    # 刚跑完一步，且该步启用了无限连击 → 回到步首，等间隔后再跑
                    # 模式二单步策略与此不同：模式二每按一次热键只跑一轮，
                    # 无限连击只在模式一（自动回放）中真正"无限"。
                    rep_step = self._step_ending_at(self._position)
                    if (rep_step is not None
                            and getattr(rep_step, "infinite_repeat", False)
                            and not self._stop_requested):
                        interval = max(1, int(getattr(
                            rep_step, "repeat_interval_ms", 50)))
                        if not self._sleep_interruptible(interval / 1000.0):
                            break  # 停止请求
                        self._position = rep_step.raw_start
                        # 重置 base_t / t0 引用点：把当前时刻作为新一轮的起点，
                        # 避免时间轴漂移累积
                        base_t = int(self._events[rep_step.raw_start].get("t") or 0)
                        t0 = time.monotonic()

                if self._stop_requested or not finished_pass:
                    completed_all = False
                    break

                # 一轮跑完：判断是否继续
                if not self._should_loop_again():
                    break

                # 轮间有个小间隔，避免上一轮最后一个动作与下一轮第一个动作
                # 撞在同一时刻（某些程序会把它们当成一次连续双击/连击）。
                if not self._sleep_interruptible(_LOOP_GAP_MS / 1000.0):
                    completed_all = False
                    break

            self._release_all()
            self._replaying = False
            self._paused = False
            self._notify_finished("finished" if completed_all else "stopped")
        except Exception as exc:  # pragma: no cover - 依赖系统注入
            log.exception("模式一回放异常")
            self._replaying = False
            self._paused = False
            self._release_all()
            self._notify_error(f"回放出错：{exc}")
            self._notify_finished("stopped")

    def _should_loop_again(self) -> bool:
        """当前轮跑完后是否还要再来一轮。"""
        if self._loop_mode == LOOP_NONE:
            return False
        if self._loop_mode == LOOP_INFINITE:
            return not self._stop_requested
        # 固定轮数
        return (self._loop_round < max(1, self._loop_count)
                and not self._stop_requested)

    def _sleep_interruptible(self, seconds: float) -> bool:
        """可被停止/暂停打断的等待。返回 False 表示收到停止请求。"""
        if seconds <= 0:
            return not self._stop_requested
        end = time.monotonic() + seconds
        while True:
            if self._stop_requested:
                return False
            if self._paused:
                # 暂停时挂起，等继续；暂停时长不再计入这一小段间隔
                self._wake.clear()
                self._wake.wait(0.2)
                continue
            remain = end - time.monotonic()
            if remain <= 0:
                return True
            self._wake.wait(min(remain, 0.05))

    def _wait_until(self, target: float) -> bool:
        """分片等待到目标时刻。返回 False 表示收到停止请求。

        精度说明：Windows 上 ``threading.Event.wait`` 的量级在毫秒到十几毫秒，
        这里按 20ms 分片等待，最后 2ms 用微睡眠收尾，实测落点误差可控制在
        数毫秒内，同时保证暂停/停止能在 20ms 内响应。
        """
        while True:
            if self._stop_requested:
                return False
            if self._paused:
                # 暂停时立即返回，让主循环去累计暂停时长。
                # 主循环收到返回值后仍会先检查暂停状态，因此不会提前执行事件。
                return True
            remain = target - time.monotonic()
            if remain <= 0:
                return True
            remain_ms = remain * 1000.0
            if remain_ms > _BUSY_SLICE_MS:
                # 分片等待：既能被唤醒（暂停/停止），又不丢精度
                self._wake.wait(min(remain, _WAIT_SLICE_MS / 1000.0))
            else:
                time.sleep(min(remain, _BUSY_SLICE_MS / 1000.0))

    def pause(self) -> bool:
        """暂停回放（模式一）。返回是否成功切换状态。"""
        if not self._replaying or self._paused:
            return False
        self._paused = True
        self._wake.set()
        return True

    def resume(self) -> bool:
        """继续回放。"""
        if not self._replaying or not self._paused:
            return False
        self._paused = False
        self._wake.set()
        return True

    def toggle_pause(self) -> Optional[bool]:
        """切换暂停/继续；返回切换后的暂停状态，未在回放时返回 None。"""
        if not self._replaying:
            return None
        if self._paused:
            self.resume()
            return False
        self.pause()
        return True

    def stop(self, join_timeout: float = 1.0) -> None:
        """停止回放并把指针归零。"""
        self._stop_requested = True
        self._paused = False
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)
        self._thread = None
        self._replaying = False
        self._paused = False
        self._position = 0
        self._release_all()

    # ==================================================================
    # 模式二：单步热键执行
    # ==================================================================
    def step_next(self) -> Optional[int]:
        """执行下一步，返回执行后的步骤指针；已到末尾返回 None。"""
        self.mode = MODE_STEP
        if not self._steps:
            self._notify_error("没有可执行的步骤，请先录制或加载脚本")
            return None
        if self._step_index >= len(self._steps):
            return None
        self._run_step(self._steps[self._step_index])
        self._step_index += 1
        return self._step_index

    def step_prev(self) -> Optional[int]:
        """回退一步：指针**真正后退一格**，并重新执行回退后所在的那一步。

        返回回退后的步骤指针；已在开头（指针为 0）返回 None。
        回退到 0 时没有「所在的那一步」可重放，因此只移动指针
        （UI 侧据此提示「已回到开头」并清掉列表高亮）。

        注：旧实现是 ``target = _step_index - 1`` 重放后
        ``_step_index = target + 1``，把指针又加回原值 —— 净位移恒为 0，
        于是「上一步」退化成「原地重放同一步」，连按毫无变化、
        看起来像热键失效（第十阶段修复）。
        """
        self.mode = MODE_STEP
        if not self._steps or self._step_index <= 0:
            return None
        # 回退时需要先把可能残留的按下状态释放掉，避免修饰键卡住
        self._release_all()
        new_index = self._step_index - 1
        self._step_index = new_index
        if new_index > 0:
            # new_index 表示「已执行步数」，其最后一步下标为 new_index - 1
            self._run_step(self._steps[new_index - 1])
        return new_index

    def step_reset(self) -> int:
        """重置步骤指针到开头（不执行任何操作）。"""
        self._release_all()
        self._step_index = 0
        self._raw_cursor = 0
        return 0

    def run_step_at(self, index: int) -> Optional[int]:
        """直接跳到并执行第 ``index`` 步（0 基）。用于界面点击列表跳步。"""
        if not self._steps or index < 0 or index >= len(self._steps):
            return None
        self._run_step(self._steps[index])
        self._step_index = index + 1
        return self._step_index

    def _run_step(self, step: Step) -> None:
        """执行一个操作单元（模式二）。

        关键点
        ------
        * **严格按序执行步骤内的每一条事件**：合并后的按键/点击步骤包含
          「按下 + 释放」，必须两条都发出去，否则会出现「只按下没释放」的卡键。
        * **瞬时操作（点击、按键）保留段内时序但封顶**：按下与释放之间的
          真实间隔按比例保留，但单次等待不超过 :data:`_STEP_MAX_GAP_MS`。
        * **连续操作（滚轮）一次执行完毕**：执行步骤内所有事件，
          间隔压缩到 :data:`_MERGED_STEP_MAX_GAP_MS`，快速完成滚动。
        * **合并后的移动只执行最后一个落点**，一步直达（单步语义）；
          插值补帧的移动步骤（``Step.interpolated``）则逐帧快速执行，
          呈现平滑轨迹。
        * **复合步骤（组合键 / 双击 / 三击）快速连续执行**：间隔压到
          :data:`_MERGED_STEP_MAX_GAP_MS`，一次热键就完整触发一次
          Ctrl+V 或一次双击，稳定不失效。
        """
        events = step.events
        if not events:
            return
        is_scroll = step.kind == "scroll"
        cap_ms = (_MERGED_STEP_MAX_GAP_MS
                  if (step.is_compound or is_scroll)
                  else _STEP_MAX_GAP_MS)
        hold_ms = max(0, int(getattr(step, "explicit_hold_ms", 0)))
        # 插值移动步骤：逐帧快速执行呈现平滑轨迹；录制的多帧移动仍一步直达
        is_interp_move = (step.kind == "move"
                          and bool(getattr(step, "interpolated", False)))
        try:
            last_t = int(events[0].get("t") or 0)
            for offset, evt in enumerate(events):
                if step.kind == "move":
                    if is_interp_move:
                        # 自动补帧的移动：快速连续执行所有帧（间隔压缩到
                        # _MERGED_STEP_MAX_GAP_MS），一步内平滑走到目标点
                        now_t = int(evt.get("t") or 0)
                        wait_ms = min(_MERGED_STEP_MAX_GAP_MS,
                                      max(0, now_t - last_t))
                        if wait_ms:
                            time.sleep(wait_ms / 1000.0 / max(self.speed, 0.1))
                        last_t = now_t
                    elif offset != len(events) - 1:
                        # 连续移动已合并，只发最后一帧，避免拖动式缓慢移动
                        continue
                else:
                    now_t = int(evt.get("t") or 0)
                    wait_ms = min(cap_ms, max(0, now_t - last_t))
                    # 长按覆盖：如果上一条是 press 且当前是同目标 release，
                    # 用 explicit_hold_ms 替换按住时长（不封顶，因为用户明确要这个时长）
                    if hold_ms > 0 and offset > 0 and evt.get("action") == "release":
                        prev = events[offset - 1]
                        if (prev.get("action") == "press"
                                and prev.get("type") == evt.get("type")
                                and self._event_matches(prev, evt)):
                            wait_ms = hold_ms
                    if wait_ms:
                        time.sleep(wait_ms / 1000.0 / max(self.speed, 0.1))
                    last_t = now_t
                self._execute_event(evt, step.raw_start + offset)
            self._raw_cursor = step.raw_end
        except Exception as exc:  # pragma: no cover - 依赖系统注入
            log.exception("单步执行异常")
            self._release_all()
            self._notify_error(f"单步执行出错：{exc}")

    # ==================================================================
    # 事件注入
    # ==================================================================
    def _execute_event(self, evt: dict[str, Any], raw_index: int) -> None:
        """执行一条原始事件，并通知界面当前执行位置。

        **点击/滚轮前必须先落点**：脚本可能是「只录点击」模式录制的，
        里面只有 ``mouse_click`` 事件、没有前置的 ``mouse_move``，
        此时若不先把指针移到事件自带的 ``(x, y)``，点击就会落在
        指针当时所在的位置（表现为「指针完全不动」）。
        """
        etype = evt.get("type")
        if etype == "mouse_move":
            self._dispatch_move(int(evt.get("x") or 0), int(evt.get("y") or 0))
        elif etype == "mouse_click":
            self._ensure_cursor_at(evt)
            self._dispatch_button(str(evt.get("button") or "left"),
                                  evt.get("action") == "press")
        elif etype == "mouse_scroll":
            self._ensure_cursor_at(evt)
            self._dispatch_scroll(int(evt.get("dx") or 0), int(evt.get("dy") or 0))
        elif etype == "key":
            self._dispatch_key(evt, str(evt.get("action") or "press"))
        else:
            log.debug("跳过未知事件类型：%s", etype)
        if self._on_event is not None:
            try:
                self._on_event(raw_index, dict(evt))
            except Exception:  # pragma: no cover
                log.debug("on_event 回调异常", exc_info=True)

    def _ensure_cursor_at(self, evt: dict[str, Any]) -> None:
        """确保指针位于事件自带的坐标上（坐标缺失或已在目标位置时不动）。"""
        if "x" not in evt or "y" not in evt:
            return
        try:
            x, y = int(evt.get("x")), int(evt.get("y"))
        except (TypeError, ValueError):
            return
        if self._last_move == (x, y):
            return        # 上一条事件已经移动到这里，无需重复移动
        self._dispatch_move(x, y)

    def _guard(self):
        """上下文管理器：注入期间把执行标志位置位，供录制器忽略这些事件。"""
        return _ExecGuard(self)

    def _dispatch_move(self, x: int, y: int) -> None:
        """移动光标。越界坐标会被钳制到虚拟桌面范围内（避免“看起来没动作”）。"""
        safe_x, safe_y, changed = clamp_to_virtual_screen(x, y)
        if changed and not self._coord_warned:
            self._coord_warned = True
            log.warning("坐标 (%s, %s) 超出当前屏幕范围，已钳制到 (%s, %s)；"
                        "脚本可能是在其它显示器配置下录制的", x, y, safe_x, safe_y)
        with self._guard():
            self._mouse_ctl().position = (safe_x, safe_y)
        self._last_move = (safe_x, safe_y)

    def _dispatch_button(self, button: str, pressed: bool) -> None:
        btn = {
            "left": mouse.Button.left,
            "right": mouse.Button.right,
            "middle": mouse.Button.middle,
        }.get(button, mouse.Button.left)
        with self._guard():
            ctl = self._mouse_ctl()
            if pressed:
                ctl.press(btn)
                self._held_buttons.add(button)
            else:
                ctl.release(btn)
                self._held_buttons.discard(button)

    def _dispatch_scroll(self, dx: int, dy: int) -> None:
        with self._guard():
            ctl = self._mouse_ctl()
            # 先滚垂直再滚水平，pynput 支持 (dx, dy) 元组
            ctl.scroll(dx, dy)

    def _dispatch_key(self, evt: dict[str, Any], action: str) -> None:
        from core.recorder import token_to_key

        token = str(evt.get("key") or "")
        key = token_to_key(token, evt.get("vk"))
        with self._guard():
            ctl = self._keyboard_ctl()
            if action == "press":
                ctl.press(key)
                self._held_keys.add(token)
            else:
                ctl.release(key)
                self._held_keys.discard(token)

    @staticmethod
    def _event_matches(prev: dict[str, Any], cur: dict[str, Any]) -> bool:
        """判断两个事件是否为「同目标」的 press / release 对（用于长按覆盖）。

        鼠标：``button`` 相同；键盘：``key`` 相同。
        """
        if prev.get("type") != cur.get("type"):
            return False
        etype = prev.get("type")
        if etype == "mouse_click":
            return prev.get("button") == cur.get("button")
        if etype == "key":
            return prev.get("key") == cur.get("key")
        return False

    def _step_ending_at(self, raw_end: int) -> Optional["ep.Step"]:
        """返回 ``raw_end`` 正好是结束下标的那个步骤（没有则返回 None）。

        用于模式一检测「刚跑完的这一步是否需要无限连击」。
        """
        for step in self._steps:
            if step.raw_end == raw_end:
                return step
        return None

    def _step_containing(self, raw_index: int) -> Optional["ep.Step"]:
        """返回包含给定原始事件下标的步骤（没有则返回 None）。"""
        for step in self._steps:
            if step.raw_start <= raw_index < step.raw_end:
                return step
        return None

    def _release_all(self) -> None:
        """兜底：只释放**本次回放真的按下过**的键与鼠标按钮。

        为什么不能无脑释放全部
        ----------------------
        ``mouse.release(Button.right)`` 即使右键本来没按下，也会向系统发出一次
        右键抬起消息；不少程序会把它当成一次右键操作（弹出上下文菜单等）。
        旧实现无条件释放 左/右/中 三个键，于是用户看到「回放开始/结束、停止录制时
        凭空多出一次右键点击」。键盘同理（无脑 ``release_all`` 会打扰用户
        原本按住不放的键）。这里改成按需释放。
        """
        # ---- 键盘：只放开我们按下去的键 ----
        if self._held_keys:
            tokens = list(self._held_keys)
            for token in tokens:
                try:
                    with self._guard():
                        ctl = self._keyboard_ctl()
                        ctl.release(token_to_key(token))
                except Exception:  # pragma: no cover
                    log.debug("释放按键 %s 失败", token, exc_info=True)
            self._held_keys.clear()

        # ---- 鼠标：只放开我们按下去过的按钮 ----
        if self._held_buttons:
            buttons = list(self._held_buttons)
            for name in buttons:
                button = {
                    "left": mouse.Button.left,
                    "right": mouse.Button.right,
                    "middle": mouse.Button.middle,
                }.get(name)
                if button is None:
                    continue
                try:
                    with self._guard():
                        self._mouse_ctl().release(button)
                except Exception:  # pragma: no cover
                    log.debug("释放鼠标按钮 %s 失败", name, exc_info=True)
            self._held_buttons.clear()

    def reset_held_state(self) -> None:
        """清空“当前按下的键/按钮”记录（不产生任何注入动作）。

        载入新脚本或重置指针时调用，避免上一段回放的状态泄漏到下一段。
        """
        self._held_keys.clear()
        self._held_buttons.clear()
        self._last_move = None

    # ==================================================================
    # 回调
    # ==================================================================
    def _notify_finished(self, reason: str) -> None:
        if self._on_finished is not None:
            try:
                self._on_finished(reason)
            except Exception:  # pragma: no cover
                log.debug("on_finished 回调异常", exc_info=True)

    def _notify_error(self, message: str) -> None:
        log.error(message)
        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:  # pragma: no cover
                log.debug("on_error 回调异常", exc_info=True)


class _ExecGuard:
    """把 ``MacroPlayer._exec_depth`` 加一/减一的上下文管理器。"""

    __slots__ = ("_player",)

    def __init__(self, player: MacroPlayer) -> None:
        self._player = player

    def __enter__(self) -> "_ExecGuard":
        with self._player._guard_lock:
            self._player._exec_depth += 1
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        with self._player._guard_lock:
            self._player._exec_depth = max(0, self._player._exec_depth - 1)
        return False
