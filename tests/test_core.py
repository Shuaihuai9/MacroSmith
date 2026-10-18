"""core 层单元测试：事件解析、脚本读写、回放两种模式、热键管理。

运行::

    python -m pytest tests/test_core.py -v
"""

from __future__ import annotations

import ctypes
import json
import tempfile
import time
from pathlib import Path

import pytest

from core import event_parser as ep
from core.config import Config
from core.hotkey_hook import (HC_ACTION, KBDLLHOOKSTRUCT, LLKHF_INJECTED,
                              WM_KEYDOWN, WM_KEYUP, KeyboardHook, hook_supported)
from core.hotkey_manager import HotkeyManager, display_name, normalize_token, parse_key
from core.player import MODE_STEP, MODE_TIMED, MacroPlayer
from core.recorder import Recorder, key_to_token, token_to_key
from core.poll_recorder import PollRecorder, _is_blacklisted_process


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
class SpyController:
    """记录注入动作的假控制器。

    ``log`` 记录全部调用（含停止时的兜底 ``release_all``）；
    ``mouse_actions`` 只记录鼠标按钮的按下/释放，便于精确断言点击步骤。
    """

    def __init__(self, kind: str = "mouse") -> None:
        self.kind = kind
        self._position = (0, 0)
        self.log: list[tuple] = []
        self.mouse_actions: list[tuple[str, str]] = []

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, value) -> None:
        self._position = value
        self.log.append(("move", tuple(value)))

    def press(self, key) -> None:
        self.log.append(("press", _token(key)))
        if self.kind == "mouse":
            self.mouse_actions.append(("press", _mouse_button(key)))

    def release(self, key) -> None:
        self.log.append(("release", _token(key)))
        if self.kind == "mouse":
            self.mouse_actions.append(("release", _mouse_button(key)))

    def scroll(self, dx, dy) -> None:
        self.log.append(("scroll", dx, dy))

    def release_all(self) -> None:
        self.log.append(("release_all",))

    def actions(self) -> list[tuple]:
        return [entry for entry in self.log if entry[0] != "release_all"]

    def moves(self) -> list[tuple[int, int]]:
        return [entry[1] for entry in self.log if entry[0] == "move"]


def _mouse_button(key) -> str:
    """把 pynput 的 Button 枚举转成 left/right/middle。"""
    from pynput import mouse as ms

    for button, name in ((ms.Button.left, "left"), (ms.Button.right, "right"),
                         (ms.Button.middle, "middle")):
        if key == button:
            return name
    return "unknown"


def _token(key) -> str:
    vk = getattr(key, "vk", None)
    if isinstance(vk, int):
        return f"vk:{vk}"
    char = getattr(key, "char", None)
    return f"char:{char}" if char else str(key)


@pytest.fixture()
def sample_events() -> list[dict]:
    """含各种事件类型、时间戳已知的样例序列。"""
    return [
        ep.make_move_event(0, 100, 100),
        ep.make_move_event(10, 105, 104),
        ep.make_move_event(200, 300, 400),
        ep.make_click_event(260, "press", "left", 300, 400),
        ep.make_click_event(300, "release", "left", 300, 400),
        ep.make_move_event(400, 320, 420),
        ep.make_scroll_event(600, 0, -2, 320, 420),
        ep.make_key_event(700, "press", "vk:65", 65),
        ep.make_key_event(740, "release", "vk:65", 65),
        ep.make_key_event(900, "press", "vk:16", 16),
        ep.make_key_event(950, "press", "vk:65", 65),
        ep.make_key_event(980, "release", "vk:65", 65),
        ep.make_key_event(1050, "release", "vk:16", 16),
    ]


# ---------------------------------------------------------------------------
# 事件解析
# ---------------------------------------------------------------------------
class TestEventParser:
    def test_describe_each_type(self):
        assert ep.describe(ep.make_move_event(0, 1, 2)) == "移动到 (1, 2)"
        assert "左键按下" in ep.describe(ep.make_click_event(0, "press", "left", 1, 2))
        assert "下滚" in ep.describe(ep.make_scroll_event(0, 0, -2, 1, 2))
        assert "键盘 A 按下" in ep.describe(ep.make_key_event(0, "press", "vk:65", 65))

    def test_key_display_special_keys(self):
        assert ep.key_display(ep.make_key_event(0, "press", "vk:112", 112)) == "F1"
        assert ep.key_display(ep.make_key_event(0, "press", "vk:34", 34)) == "PageDown"

    def test_grouping_merges_consecutive_moves(self, sample_events):
        steps = ep.build_steps(sample_events)
        assert steps[0].kind == "move"
        assert steps[0].event_count == 3
        assert steps[0].description == "移动到 (300, 400)"

    def test_grouping_merges_click_press_release(self, sample_events):
        steps = ep.build_steps(sample_events)
        click = [s for s in steps if s.kind == "click"][0]
        assert click.event_count == 2
        assert click.description.startswith("左键单击")

    def test_grouping_scroll_and_keys(self, sample_events):
        """键盘按下+释放合并；Shift+A 这类组合键合并成一步「组合键」。"""
        steps = ep.build_steps(sample_events)
        kinds = [s.kind for s in steps]
        # move, click, move, scroll 之后：
        #   键盘 A 按下并释放 / 组合键 Shift+A（Shift 按下 + A 按下/释放 + Shift 释放）
        assert kinds == ["move", "click", "move", "scroll", "key", "combo"]
        assert len(steps) == 6
        assert steps[4].description == "键盘 A 按下并释放"
        assert steps[4].event_count == 2
        assert steps[5].kind == "combo"
        assert steps[5].description == "组合键 Shift+A"
        assert steps[5].modifiers == ["Shift"]
        assert steps[5].target_key == "A"
        assert steps[5].event_count == 4

    def test_combo_disabled_keeps_separate_steps(self, sample_events):
        """关闭自动合并组合键后，回到「按下/普通键/释放」三步的拆分记录方式。"""
        steps = ep.build_steps(sample_events, ep.MergeOptions(combo=False))
        kinds = [s.kind for s in steps]
        assert kinds == ["move", "click", "move", "scroll", "key", "key", "key", "key"]
        assert steps[5].description == "键盘 Shift 按下"
        assert steps[6].description == "键盘 A 按下并释放"
        assert steps[7].description == "键盘 Shift 释放"

    def test_key_pair_merged_into_one_step(self):
        """同一按键的按下 + 释放必须合并为一步，且保留两个时间戳。"""
        events = [ep.make_key_event(100, "press", "vk:65", 65),
                  ep.make_key_event(180, "release", "vk:65", 65)]
        steps = ep.build_steps(events)
        assert len(steps) == 1
        step = steps[0]
        assert step.kind == "key"
        assert step.event_count == 2
        assert step.t_start == 100 and step.t_end == 180
        assert step.hold_ms == 80
        assert [e["action"] for e in step.events] == ["press", "release"]

    def test_step_ranges_cover_all_events(self, sample_events):
        steps = ep.build_steps(sample_events)
        assert steps[0].raw_start == 0
        assert steps[-1].raw_end == len(sample_events)
        # 区间必须首尾相接，回退时才能精确定位
        for prev, nxt in zip(steps, steps[1:]):
            assert prev.raw_end == nxt.raw_start

    def test_lone_press_without_release_is_own_step(self):
        events = [ep.make_click_event(0, "press", "right", 5, 6)]
        steps = ep.build_steps(events)
        assert len(steps) == 1
        assert "右键按下" in steps[0].description

    def test_right_click_wording(self):
        events = [
            ep.make_click_event(0, "press", "right", 5, 6),
            ep.make_click_event(10, "release", "right", 5, 6),
        ]
        assert "右击" in ep.build_steps(events)[0].description

    def test_sanitize_fills_missing_release(self):
        events = [ep.make_key_event(0, "press", "vk:65", 65)]
        cleaned = ep.sanitize_events(events)
        assert len(cleaned) == 2
        assert cleaned[-1]["action"] == "release"
        assert cleaned[-1]["key"] == "vk:65"

    def test_sanitize_fills_missing_mouse_release(self):
        cleaned = ep.sanitize_events([ep.make_click_event(0, "press", "left", 7, 8)])
        assert cleaned[-1]["type"] == "mouse_click"
        assert cleaned[-1]["action"] == "release"
        assert (cleaned[-1]["x"], cleaned[-1]["y"]) == (7, 8)

    def test_sanitize_enforces_monotonic_time(self):
        events = [ep.make_move_event(100, 0, 0), ep.make_move_event(50, 1, 1)]
        cleaned = ep.sanitize_events(events)
        assert [e["t"] for e in cleaned] == [100, 100]

    def test_sanitize_drops_junk(self):
        cleaned = ep.sanitize_events([None, {}, {"type": ""}, ep.make_move_event(0, 1, 1)])
        assert len(cleaned) == 1


# ---------------------------------------------------------------------------
# 步骤编辑：增 / 删 / 移 + 时间戳重排
# ---------------------------------------------------------------------------
class TestStepEditing:
    def test_normalize_is_idempotent(self, sample_events):
        once = ep.normalize_timestamps(sample_events)
        twice = ep.normalize_timestamps(once)
        assert twice == once

    def test_normalize_preserves_intra_step_offsets_with_negative_base(self):
        """时间戳为负（异常数据）时以最小值为基准，段内相对偏移不变。"""
        events = [ep.make_move_event(-50, 1, 1), ep.make_move_event(-20, 2, 2)]
        normalized = ep.normalize_timestamps(events)
        # 连续移动属于同一步骤，段内偏移 30ms 保持不变
        assert [e["t"] for e in normalized] == [-50, -20]

        # 换成两个独立步骤：基准取 -50，第二步按固定步间隔排在其后
        mixed = [ep.make_scroll_event(-50, 0, -1, 1, 1),
                 ep.make_key_event(-49, "press", "vk:65", 65)]
        times = [e["t"] for e in ep.normalize_timestamps(mixed)]
        assert times[0] == -50
        assert times[1] - times[0] == ep.MIN_STEP_GAP_MS

    def test_normalize_keeps_intra_step_offsets(self):
        events = [ep.make_click_event(0, "press", "left", 1, 1),
                  ep.make_click_event(40, "release", "left", 1, 1)]
        assert [e["t"] for e in ep.normalize_timestamps(events)] == [0, 40]

    def test_normalize_uses_fixed_step_gap(self):
        """段间间隔统一为 MIN_STEP_GAP_MS，段落自身的时长保留。"""
        events = [ep.make_scroll_event(0, 0, -1, 1, 1),
                  ep.make_key_event(0, "press", "vk:65", 65)]
        times = [e["t"] for e in ep.normalize_timestamps(events)]
        assert times == [0, ep.MIN_STEP_GAP_MS]

    def test_normalize_noop_on_already_normalized(self):
        """对已规整的数据调用不会产生漂移（幂等）。"""
        events = [ep.make_move_event(0, 1, 1),
                  ep.make_scroll_event(20, 0, -1, 1, 1),
                  ep.make_key_event(40, "press", "vk:65", 65)]
        assert ep.normalize_timestamps(events) == events

    def test_delete_steps_compresses_timeline(self):
        """删掉步骤后时间轴会收缩（段间间隔固定为 MIN_STEP_GAP_MS）。"""
        events = [
            ep.make_move_event(0, 1, 1),
            ep.make_click_event(400, "press", "left", 1, 1),
            ep.make_click_event(440, "release", "left", 1, 1),
            ep.make_scroll_event(800, 0, -1, 1, 1),
            ep.make_key_event(1500, "press", "vk:65", 65),
            ep.make_key_event(1540, "release", "vk:65", 65),
        ]
        steps = ep.build_steps(events)
        # 4 步：移动 / 左键单击 / 滚轮 / 键盘 A 按下并释放
        assert len(steps) == 4
        before = ep.events_duration_ms(events)

        deleted = ep.strip_internal_fields(ep.delete_steps(steps, [1]))
        assert [s.kind for s in ep.build_steps(deleted)] == ["move", "scroll", "key"]
        after = ep.events_duration_ms(deleted)
        assert after < before, "删除步骤应压缩总时长"
        # 删掉点击后只剩 3 步：2 个固定步间隔 + 按键自身的 40ms
        assert after == ep.MIN_STEP_GAP_MS * 2 + 40
        # 时间戳仍然单调不减
        times = [int(e["t"]) for e in deleted]
        assert times == sorted(times)

    def test_normalize_preserves_click_hold_duration(self):
        """段间被规整，但点击自身的“按住时长”必须保留。"""
        events = [ep.make_move_event(0, 1, 1),
                  ep.make_click_event(900, "press", "left", 1, 1),
                  ep.make_click_event(980, "release", "left", 1, 1),
                  ep.make_scroll_event(2000, 0, -1, 1, 1)]
        normalized = ep.normalize_timestamps(events)
        times = [e["t"] for e in normalized]
        assert times[1] - times[0] == ep.MIN_STEP_GAP_MS   # 步间隔被规整
        assert times[2] - times[1] == 80                   # 按住时长保留
        assert times[3] - times[2] == ep.MIN_STEP_GAP_MS
    def test_delete_then_insert_restores_duration(self):
        """删除 + 插回同样数量的步骤，时长回到同一量级（行为可预期）。"""
        events = [
            ep.make_move_event(0, 1, 1),
            ep.make_click_event(400, "press", "left", 1, 1),
            ep.make_click_event(440, "release", "left", 1, 1),
            ep.make_scroll_event(800, 0, -1, 1, 1),
        ]
        steps = ep.build_steps(events)
        deleted = ep.strip_internal_fields(ep.delete_steps(steps, [1]))
        short = ep.events_duration_ms(deleted)

        new_events, desc = ep.make_step_events("click", x=5, y=5)
        restored, index = ep.insert_step(ep.build_steps(deleted), 1, new_events, desc)
        long = ep.events_duration_ms(ep.strip_internal_fields(restored))
        assert long > short
        assert index == 1

    def test_delete_multiple_steps(self, sample_events):
        steps = ep.build_steps(sample_events)
        events = ep.delete_steps(steps, [0, 2, 4])
        assert len(ep.build_steps(events)) == len(steps) - 3

    def test_delete_ignores_out_of_range(self, sample_events):
        """越界下标被忽略，合法下标正常删除。"""
        steps = ep.build_steps(sample_events)
        events = ep.delete_steps(steps, [1, 999])
        assert len(ep.build_steps(events)) == len(steps) - 1

    def test_delete_all_returns_empty(self, sample_events):
        steps = ep.build_steps(sample_events)
        assert ep.delete_steps(steps, list(range(len(steps)))) == []

    def test_delete_without_valid_index_raises(self, sample_events):
        with pytest.raises(ep.StepEditError):
            ep.delete_steps(ep.build_steps(sample_events), [])

    def test_move_step_down_swaps(self, sample_events):
        steps = ep.build_steps(sample_events)
        original = [s.description for s in steps]
        events, mapping = ep.move_steps(steps, [0], 1)
        after = [s.description for s in ep.build_steps(events)]
        assert after[0] == original[1]
        assert after[1] == original[0]
        assert after[2:] == original[2:]
        assert mapping == {0: 1}

    def test_move_step_up_swaps(self, sample_events):
        steps = ep.build_steps(sample_events)
        original = [s.description for s in steps]
        events, mapping = ep.move_steps(steps, [1], -1)
        after = [s.description for s in ep.build_steps(events)]
        assert after[0] == original[1]
        assert after[1] == original[0]
        assert after[2:] == original[2:]
        assert mapping == {1: 0}

    def test_move_multiple_as_block(self, sample_events):
        """多选上移时整体挪一格，步骤之间相对顺序不变。"""
        steps = ep.build_steps(sample_events)
        original = [s.description for s in steps]
        events, mapping = ep.move_steps(steps, [1, 2], 1)
        after = [s.description for s in ep.build_steps(events)]
        assert after[0] == original[0]
        assert after[1] == original[3]
        assert after[2] == original[1]
        assert after[3] == original[2]
        assert mapping == {1: 2, 2: 3}

    def test_move_at_boundary_raises(self, sample_events):
        steps = ep.build_steps(sample_events)
        with pytest.raises(ep.StepEditError):
            ep.move_steps(steps, [0], -1)
        with pytest.raises(ep.StepEditError):
            ep.move_steps(steps, [len(steps) - 1], 1)

    def test_move_without_selection_raises(self, sample_events):
        with pytest.raises(ep.StepEditError):
            ep.move_steps(ep.build_steps(sample_events), [], 1)

    def test_move_preserves_total_duration(self, sample_events):
        """重排步骤不会把时间轴拉长（统一成固定步间隔后只会更短或持平）。"""
        steps = ep.build_steps(sample_events)
        before = ep.events_duration_ms(sample_events)
        events, _ = ep.move_steps(steps, [1], -1)
        assert ep.events_duration_ms(events) <= before + ep.MIN_STEP_GAP_MS

    def test_make_step_events_click(self):
        events, description = ep.make_step_events("click", x=10, y=20, button="right")
        assert len(events) == 2
        assert events[0]["action"] == "press" and events[0]["button"] == "right"
        assert events[1]["t"] > events[0]["t"]
        assert "右击" in description

    def test_make_step_events_click_repeat(self):
        """repeat>=2 现在直接产出「双击/三击」描述（第五阶段智能合并）。"""
        events, description = ep.make_step_events("click", x=1, y=2, repeat=3)
        assert len(events) == 6
        assert description == "鼠标三击 (1, 2)"
        # 三次点击之间的间隔必须小于连击阈值，才会被识别成三击
        presses = [e["t"] for e in events if e["action"] == "press"]
        gaps = [b - a for a, b in zip(presses, presses[1:])]
        assert all(g < ep.MULTI_CLICK_MAX_GAP_MS for g in gaps), gaps
        assert [e["t"] for e in events] == sorted(e["t"] for e in events)
        # 三击步骤应被 build_steps 识别为 multi
        built = ep.build_steps(events)
        assert len(built) == 1 and built[0].kind == "multi"
        assert built[0].click_count == 3

    def test_make_step_events_key(self):
        events, description = ep.make_step_events("key", key="vk:65", vk=65)
        assert len(events) == 2
        assert events[0]["key"] == "vk:65" and events[0]["vk"] == 65
        assert "键盘" in description

    def test_make_step_events_move_and_scroll(self):
        move_events, move_desc = ep.make_step_events("move", x=5, y=6)
        assert move_events[0]["type"] == "mouse_move"
        assert "移动到 (5, 6)" == move_desc

        scroll_events, scroll_desc = ep.make_step_events("scroll", dy=-3)
        assert scroll_events[0]["dy"] == -3
        assert "下滚" in scroll_desc

    def test_make_step_events_rejects_unknown_kind(self):
        with pytest.raises(ep.StepEditError):
            ep.make_step_events("unknown")

    def test_insert_step_at_beginning(self, sample_events):
        steps = ep.build_steps(sample_events)
        new_events, desc = ep.make_step_events("click", x=7, y=8)
        events, index = ep.insert_step(steps, 0, new_events, desc)
        after = ep.build_steps(events)
        assert index == 0
        assert len(after) == len(steps) + 1
        assert after[0].kind == "click"
        assert "7" in after[0].description

    def test_insert_step_in_middle(self, sample_events):
        steps = ep.build_steps(sample_events)
        new_events, desc = ep.make_step_events("key", key="vk:66", vk=66)
        events, index = ep.insert_step(steps, 2, new_events, desc)
        after = ep.build_steps(events)
        assert index == 2
        assert after[2].kind == "key"
        times = [int(e["t"]) for e in events]
        assert times == sorted(times)

    def test_insert_step_at_end(self, sample_events):
        steps = ep.build_steps(sample_events)
        new_events, desc = ep.make_step_events("move", x=1, y=1)
        events, index = ep.insert_step(steps, len(steps), new_events, desc)
        after = ep.build_steps(events)
        assert index == len(steps)
        assert len(after) == len(steps) + 1
        assert after[-1].kind == "move"

    def test_insert_empty_events_raises(self, sample_events):
        with pytest.raises(ep.StepEditError):
            ep.insert_step(ep.build_steps(sample_events), 0, [])

    def test_insert_into_empty_list(self):
        new_events, desc = ep.make_step_events("click", x=1, y=1)
        events, index = ep.insert_step([], 0, new_events, desc)
        assert index == 0
        assert len(ep.build_steps(events)) == 1

    def test_step_signature_identifies_content(self, sample_events):
        steps = ep.build_steps(sample_events)
        assert ep.step_signature(steps[0]) == ep.step_signature(steps[0])
        assert ep.step_signature(steps[0]) != ep.step_signature(steps[1])

    def test_steps_to_events_roundtrip(self, sample_events):
        steps = ep.build_steps(sample_events)
        flat = ep.steps_to_events(steps)
        # 摊平时会打上步骤边界标记，去掉内部字段后应与原始事件一致
        assert ep.strip_internal_fields(flat) == sample_events
        # 再分组得到的步骤与原来一致（边界标记保证粒度不变）
        rebuilt = ep.build_steps(flat)
        assert [s.description for s in rebuilt] == [s.description for s in steps]
        assert [s.kind for s in rebuilt] == [s.kind for s in steps]

    def test_step_boundary_marker_keeps_steps_apart(self):
        """两步移动被挪到一起后不应合并（这正是边界标记要解决的问题）。

        录制时连续的移动本来就该合并为一步；但编辑器把两个原本分开的移动步骤
        挪到相邻位置后，它们是两个独立步骤，不能再次被合并。
        """
        original = [
            ep.make_move_event(0, 10, 10),
            ep.make_click_event(200, "press", "left", 10, 10),
            ep.make_click_event(240, "release", "left", 10, 10),
            ep.make_move_event(400, 90, 90),
        ]
        steps = ep.build_steps(original)
        assert [s.kind for s in steps] == ["move", "click", "move"]

        # 把第 1 步下移一格 -> [click, move, move]
        events, _ = ep.move_steps(steps, [0], 1)
        after = ep.build_steps(events)
        assert [s.kind for s in after] == ["click", "move", "move"]
        assert len(after) == 3

        # 抹掉边界标记后，相邻的两个移动会被合并成一步（证明标记是关键）
        merged = ep.strip_internal_fields(events)
        assert len(ep.build_steps(merged)) == 2

    def test_boundary_marker_not_written_to_file(self, tmp_path: Path):
        steps = ep.build_steps([ep.make_move_event(0, 10, 10),
                                ep.make_move_event(500, 20, 20)])
        path = ep.save_script(tmp_path / "b.json", "b", ep.steps_to_events(steps))
        payload = ep.load_script(path)
        assert all(ep.BOUNDARY_KEY not in e for e in payload["events"])

    def test_events_duration(self):
        assert ep.events_duration_ms([]) == 0
        assert ep.events_duration_ms([ep.make_move_event(0, 1, 1),
                                      ep.make_key_event(500, "press", "vk:65", 65)]) == 500


# ---------------------------------------------------------------------------
# 录制器：始终记录移动与点击（第九阶段删除「仅录制点击坐标」）
# ---------------------------------------------------------------------------
class TestAlwaysRecordMoves:
    def _recorder(self):
        rec = Recorder(is_executing=lambda: False)
        rec.start()
        rec._recording = True          # 不依赖真实钩子
        rec._t0 = time.monotonic()
        return rec

    def test_start_has_no_clicks_only_param(self):
        import inspect

        params = inspect.signature(Recorder.start).parameters
        assert "clicks_only" not in params
        assert "mouse_move" not in params

    def test_no_clicks_only_attributes(self):
        rec = Recorder(is_executing=lambda: False)
        assert not hasattr(rec, "record_clicks_only")
        assert not hasattr(rec, "record_mouse_move")

    def test_moves_and_clicks_recorded(self):
        from pynput import mouse as ms

        rec = self._recorder()
        rec._handle_move(100, 100)
        rec._handle_move(400, 400)
        rec._handle_click(321, 654, ms.Button.left, True)
        rec._handle_click(321, 654, ms.Button.left, False)
        events = rec.get_events()
        assert [e["type"] for e in events] == [
            "mouse_move", "mouse_move", "mouse_click", "mouse_click"]
        assert (events[2]["x"], events[2]["y"]) == (321, 654)


# ---------------------------------------------------------------------------
# 第三阶段：坐标钳制 / 步骤时间编辑 / 总时长重设
# ---------------------------------------------------------------------------
class TestCoordinateClamping:
    def test_clamp_inside_bounds_is_unchanged(self):
        from utils.win_utils import clamp_to_virtual_screen, virtual_screen_rect

        left, top, width, height = virtual_screen_rect()
        x, y, changed = clamp_to_virtual_screen(left + 10, top + 10)
        assert (x, y) == (left + 10, top + 10)
        assert changed is False

    def test_clamp_out_of_bounds(self):
        """越界坐标（例如脚本在别的显示器配置下录制）会被压回范围内。"""
        from utils.win_utils import clamp_to_virtual_screen, virtual_screen_rect

        left, top, width, height = virtual_screen_rect()
        x, y, changed = clamp_to_virtual_screen(left + width + 5000, top + height + 5000)
        assert changed is True
        assert left <= x < left + width
        assert top <= y < top + height

    def test_clamp_handles_bad_values(self):
        from utils.win_utils import clamp_to_virtual_screen

        _x, _y, changed = clamp_to_virtual_screen("abc", None)
        assert changed is True

    def test_player_clamps_move_and_warns_once(self, caplog):
        """回放时越界坐标必须被钳制，且只告警一次。"""
        from utils.win_utils import virtual_screen_rect

        left, top, width, height = virtual_screen_rect()
        player = MacroPlayer()
        mouse = SpyController()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        far_x, far_y = left + width + 999, top + height + 999
        # 用滚动事件把两次移动隔开，避免它们被合并成同一个移动步骤
        player.load([ep.make_move_event(0, far_x, far_y),
                     ep.make_scroll_event(10, 0, -1, far_x, far_y),
                     ep.make_move_event(20, far_x + 10, far_y + 10)], "x")
        assert len(player.steps) == 3

        with caplog.at_level("WARNING"):
            for _ in range(len(player.steps)):
                player.step_next()

        moves = mouse.moves()
        # 两次鼠标移动 + 滚轮前的一次落点（回放点击/滚轮前会确保指针到位）
        assert len(moves) == 3, moves
        for mx, my in moves:
            assert left <= mx < left + width and top <= my < top + height
        warnings = [r for r in caplog.records if "钳制" in r.message]
        assert len(warnings) == 1, f"越界告警应只提示一次：{len(warnings)}"


class TestNoSpuriousClicks:
    """第四阶段 BUG 1：回放首尾不得凭空多出左键/右键。"""

    def test_no_right_or_middle_release_when_never_pressed(self):
        """脚本里只有左键，收尾时绝不能发出右/中键的 release。

        旧实现在 ``_release_all()`` 里无条件 release 左/右/中三个键；
        Windows 会把“本来没按下的右键抬起”当成一次右键操作，
        于是用户看到回放开始/结束、停止录制时凭空多出右键点击。
        """
        events = [
            ep.make_move_event(0, 200, 200),
            ep.make_click_event(50, "press", "left", 200, 200),
            ep.make_click_event(90, "release", "left", 200, 200),
        ]
        player, mouse, _ = self._play_fully(events)

        released = [name for action, name in mouse.mouse_actions if action == "release"]
        assert released == ["left"], f"只应释放左键，实际 {released}"
        assert not any("right" in name or "middle" in name for name in released)

    def test_script_with_no_clicks_releases_nothing(self):
        """纯移动脚本执行完后，不应有任何鼠标按钮动作。"""
        events = [ep.make_move_event(0, 10, 10), ep.make_scroll_event(40, 0, -1, 10, 10)]
        player, mouse, _ = self._play_fully(events)
        assert mouse.mouse_actions == [], mouse.mouse_actions

    def test_right_click_released_once_when_script_has_it(self):
        """脚本自己录了右键时，仍要正常按下+释放各一次。"""
        events = [
            ep.make_click_event(0, "press", "right", 300, 300),
            ep.make_click_event(60, "release", "right", 300, 300),
        ]
        player, mouse, _ = self._play_fully(events)
        press_actions = [n for a, n in mouse.mouse_actions if a == "press"]
        release_actions = [n for a, n in mouse.mouse_actions if a == "release"]
        assert press_actions == ["right"]
        assert release_actions == ["right"]

    def test_held_button_state_is_tracked(self):
        """按下后未释放的按钮要被记录下来（收尾时才能正确释放）。"""
        player = MacroPlayer()
        mouse = SpyController()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        player.load([ep.make_click_event(0, "press", "left", 1, 1)], "held")
        player.step_next()
        assert player._held_buttons == {"left"}
        player.reset_held_state()
        assert player._held_buttons == set()

    def test_held_key_state_is_tracked(self):
        """按下/释放被合并成一步时，执行完就该是干净的（不残留按下状态）。"""
        player = MacroPlayer()
        player.set_controllers(mouse_ctl=SpyController(),
                               keyboard_ctl=SpyController("kbd"))
        player.load([ep.make_key_event(0, "press", "vk:65", 65),
                     ep.make_key_event(50, "release", "vk:65", 65)], "held")
        assert len(player.steps) == 1, "按下+释放应合并为一步"
        player.step_next()
        assert player._held_keys == set(), "合并步骤执行完不应残留按下状态"

    def test_lone_press_records_held_key(self):
        """只有按下（无释放）的步骤，执行后必须记录为「仍按住」。"""
        player = MacroPlayer()
        keyboard = SpyController("kbd")
        player.set_controllers(mouse_ctl=SpyController(), keyboard_ctl=keyboard)
        player.load([ep.make_key_event(0, "press", "vk:16", 16)], "lone")
        player.step_next()
        assert player._held_keys == {"vk:16"}, player._held_keys
        player._release_all()
        assert player._held_keys == set()

    def _play_fully(self, events):
        player = MacroPlayer()
        mouse = SpyController()
        keyboard = SpyController("kbd")
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=keyboard)
        player.load(events, "t")
        for _ in range(player.step_total + 1):
            player.step_next()
        return player, mouse, keyboard


class TestClickOnlyCursorPlacement:
    """第四阶段 BUG 2/3：只录点击的脚本必须先把指针移到坐标再点。"""

    @staticmethod
    def _click_only() -> list[dict]:
        # 只录点击模式下不会有 mouse_move 事件
        return [
            ep.make_click_event(0, "press", "left", 138, 525),
            ep.make_click_event(40, "release", "left", 138, 525),
            ep.make_click_event(200, "press", "left", 900, 700),
            ep.make_click_event(240, "release", "left", 900, 700),
        ]

    def test_no_move_events_in_fixture(self):
        assert all(e["type"] == "mouse_click" for e in self._click_only())

    def test_step_mode_moves_before_click(self):
        """模式二：每个点击步骤都要先把指针移到该步骤坐标。"""
        player = MacroPlayer()
        mouse = SpyController()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        player.load(self._click_only(), "clickonly")

        for _ in range(player.step_total):
            player.step_next()

        assert mouse.moves() == [(138, 525), (900, 700)], mouse.moves()
        # 每次移动都紧跟在对应点击之前
        assert mouse.mouse_actions == [
            ("press", "left"), ("release", "left"),
            ("press", "left"), ("release", "left"),
        ]

    def test_timed_mode_moves_before_click(self):
        """模式一：时间轴回放同样不能跳过落点。"""
        player = MacroPlayer()
        mouse = SpyController()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        player.load(self._click_only(), "clickonly")
        player.speed = 20.0
        player.start_timed()
        while player.is_replaying:
            time.sleep(0.005)

        assert mouse.moves() == [(138, 525), (900, 700)], mouse.moves()
        # 每一步「按下/释放」都必须发生在指针到位之后
        order = [entry[0] for entry in mouse.log]
        assert order.count("move") == 2, order

    def test_no_duplicate_move_before_consecutive_clicks(self):
        """同一坐标的按下+释放不应重复移动指针。"""
        events = [
            ep.make_move_event(0, 500, 500),
            ep.make_click_event(50, "press", "left", 500, 500),
            ep.make_click_event(90, "release", "left", 500, 500),
        ]
        player = MacroPlayer()
        mouse = SpyController()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        player.load(events, "dedupe")
        for _ in range(player.step_total):
            player.step_next()
        assert mouse.moves() == [(500, 500)], mouse.moves()

    def test_scroll_step_moves_before_scrolling(self):
        events = [ep.make_scroll_event(0, 0, -3, 700, 640)]
        player = MacroPlayer()
        mouse = SpyController()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        player.load(events, "scroll")
        player.step_next()
        assert mouse.moves() == [(700, 640)], mouse.moves()
        assert ("scroll", 0, -3) in mouse.log, mouse.log

    def test_click_without_coordinates_is_safe(self):
        """老脚本可能没有 x/y 字段，此时不应崩、也不强制移动。"""
        events = [{"t": 0, "type": "mouse_click", "action": "press", "button": "left"},
                  {"t": 40, "type": "mouse_click", "action": "release", "button": "left"}]
        player = MacroPlayer()
        mouse = SpyController()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        player.load(events, "nocoord")
        player.step_next()
        assert mouse.moves() == []


class TestStepTimeEditing:
    def test_set_step_time_shifts_step_and_following(self):
        events = [ep.make_move_event(0, 1, 1),
                  ep.make_scroll_event(1000, 0, -1, 1, 1),
                  ep.make_key_event(2000, "press", "vk:65", 65),
                  ep.make_key_event(2040, "release", "vk:65", 2040 - 2000 + 2000)]
        events[3]["t"] = 2040
        steps = ep.build_steps(events)
        assert len(steps) == 3

        moved = ep.set_step_time(events, 1, 5000)
        after = ep.build_steps(moved)
        assert after[1].t_start == 5000
        # 其后的键盘步骤整体平移，间隔保持 1000ms
        assert after[2].t_start == 6000
        # 按键自身的按住时长不变
        assert after[2].hold_ms == 40

    def test_set_step_time_clamps_to_previous_end(self):
        """时间不能早于上一步结束，否则会被夹回（保证时间戳单调）。"""
        events = [ep.make_scroll_event(0, 0, -1, 1, 1),
                  ep.make_key_event(5000, "press", "vk:65", 65),
                  ep.make_key_event(5040, "release", "vk:65", 65)]
        moved = ep.set_step_time(events, 1, 0)
        after = ep.build_steps(moved)
        assert after[1].t_start >= after[0].t_end + ep.MIN_STEP_GAP_MS
        times = [int(e["t"]) for e in moved]
        assert times == sorted(times)

    def test_set_step_time_first_step(self):
        events = [ep.make_scroll_event(100, 0, -1, 1, 1),
                  ep.make_scroll_event(900, 0, -1, 1, 1)]
        moved = ep.set_step_time(events, 0, 0)
        after = ep.build_steps(moved)
        assert after[0].t_start == 0
        assert after[1].t_start == 800      # 相对间隔保持

    def test_set_step_time_out_of_range(self):
        events = [ep.make_scroll_event(0, 0, -1, 1, 1)]
        with pytest.raises(ep.StepEditError):
            ep.set_step_time(events, 5, 100)


class TestStepDurationEditing:
    """第九阶段：set_step_duration —— 内部等比缩放，后续整体平移。"""

    @staticmethod
    def _events():
        return [ep.make_move_event(0, 1, 1),
                ep.make_scroll_event(1000, 0, -1, 1, 1),
                ep.make_key_event(2000, "press", "vk:65", 65),
                ep.make_key_event(2040, "release", "vk:65", 65)]

    def test_scales_inner_events(self):
        """键盘步骤时长 40ms → 2000ms：press 不动，release 等比放大。"""
        events = self._events()
        moved = ep.set_step_duration(events, 2, 2000)
        after = ep.build_steps(moved)
        assert after[2].t_start == 2000, "起始不变"
        assert after[2].t_end == 4000, "结束 = 起始 + 新时长"
        assert after[2].hold_ms == 2000

    def test_shrink_scales_down(self):
        """缩短时长同样等比缩放（40 → 10ms）。"""
        events = self._events()
        moved = ep.set_step_duration(events, 2, 10)
        after = ep.build_steps(moved)
        assert after[2].t_start == 2000
        assert after[2].t_end == 2010

    def test_following_steps_shift(self):
        """中间步骤时长变化后，后续步骤按差值整体平移，间隔保持。"""
        events = self._events()
        moved = ep.set_step_duration(events, 1, 1000)   # 滚轮步骤 0 → 1000ms
        after = ep.build_steps(moved)
        assert after[2].t_start == 3000, f"后续 +1000ms：{after[2].t_start}"
        assert after[2].hold_ms == 40, "后续步骤内部节奏不变"
        times = [int(e["t"]) for e in moved]
        assert times == sorted(times), "时间戳必须单调不减"

    def test_zero_old_duration_keeps_inner(self):
        """旧时长为 0 的步骤没有可缩放空间：内部不动，后续照差值平移。"""
        events = self._events()
        moved = ep.set_step_duration(events, 1, 500)
        scroll = [e for e in moved if e["type"] == "mouse_scroll"]
        assert int(scroll[0]["t"]) == 1000, "单事件步骤内部不变"

    def test_multi_click_scales_rhythm(self):
        """连击步骤的内部击键节奏按比例缩放。"""
        events = []
        for i in range(4):
            events.append(ep.make_click_event(i * 100, "press", "left", 1, 1))
            events.append(ep.make_click_event(i * 100 + 50, "release", "left", 1, 1))
        events.append(ep.make_move_event(1000, 5, 5))
        steps = ep.build_steps(events)
        assert steps[0].kind == "multi" and steps[0].click_count == 4
        moved = ep.set_step_duration(events, 0, 700)   # 350 → 700，factor=2
        after = ep.build_steps(moved)
        assert after[0].t_end - after[0].t_start == 700
        # 后续步骤整体平移 delta=350
        assert after[1].t_start == 1350

    def test_no_change_returns_equal(self):
        events = self._events()
        moved = ep.set_step_duration(events, 2, 40)
        assert [int(e["t"]) for e in moved] == [0, 1000, 2000, 2040]

    def test_out_of_range(self):
        with pytest.raises(ep.StepEditError):
            ep.set_step_duration(self._events(), 9, 100)

    def test_redistribute_duration_hits_target(self):
        events = [ep.make_move_event(0, 1, 1),
                  ep.make_click_event(1000, "press", "left", 1, 1),
                  ep.make_click_event(1100, "release", "left", 1, 1),
                  ep.make_key_event(5000, "press", "vk:65", 65),
                  ep.make_key_event(5100, "release", "vk:65", 65)]
        spread = ep.redistribute_duration(events, 30_000)
        assert ep.events_duration_ms(spread) == 30_000
        after = ep.build_steps(spread)
        assert len(after) == 3
        # 步骤内的按住时长保留
        assert after[1].hold_ms == 100
        assert after[2].hold_ms == 100
        # 步骤均匀铺开
        starts = [s.t_start for s in after]
        assert starts == sorted(starts)
        assert starts[0] == 0

    def test_redistribute_shorter_than_spans(self):
        """目标时长比各步骤自身跨度之和还小时不会崩，间隔压到 0。"""
        events = [ep.make_click_event(0, "press", "left", 1, 1),
                  ep.make_click_event(500, "release", "left", 1, 1),
                  ep.make_click_event(1000, "press", "left", 1, 1),
                  ep.make_click_event(1500, "release", "left", 1, 1)]
        spread = ep.redistribute_duration(events, 100)
        times = [int(e["t"]) for e in spread]
        assert times == sorted(times)

    def test_redistribute_empty(self):
        assert ep.redistribute_duration([], 1000) == []

    def test_redistribute_single_step(self):
        events = [ep.make_scroll_event(0, 0, -1, 1, 1)]
        spread = ep.redistribute_duration(events, 5000)
        assert ep.build_steps(spread)[0].t_start == 0

    def test_format_and_parse_duration(self):
        assert ep.format_duration(0) == "0.000s"
        assert ep.format_duration(1500) == "1.500s"
        assert ep.format_duration(90_000) == "1:30.000"
        assert ep.parse_duration("30") == 30_000
        assert ep.parse_duration("30s") == 30_000
        assert ep.parse_duration("1500ms") == 1500
        assert ep.parse_duration("2:30") == 150_000
        assert ep.parse_duration("1.5") == 1500
        assert ep.parse_duration("0:05.5") == 5500

    def test_parse_duration_rejects_bad_input(self):
        for bad in ("", "abc", "1:2:3", "::"):
            with pytest.raises(ep.StepEditError):
                ep.parse_duration(bad)


class TestStepHoldInfo:
    def test_hold_ms_for_paired_events(self):
        steps = ep.build_steps([ep.make_key_event(100, "press", "vk:65", 65),
                                ep.make_key_event(180, "release", "vk:65", 65)])
        assert steps[0].hold_ms == 80
        assert steps[0].duration_ms == 80

    def test_hold_ms_zero_for_instant_events(self):
        steps = ep.build_steps([ep.make_scroll_event(500, 0, -1, 1, 1)])
        assert steps[0].hold_ms == 0

    def test_kind_label(self):
        steps = ep.build_steps([ep.make_key_event(0, "press", "vk:65", 65)])
        assert steps[0].kind_label == "键盘按键"

    def test_as_dict_includes_hold(self):
        steps = ep.build_steps([ep.make_key_event(0, "press", "vk:65", 65),
                                ep.make_key_event(50, "release", "vk:65", 65)])
        payload = steps[0].as_dict()
        assert payload["hold_ms"] == 50
        assert payload["event_count"] == 2


# ---------------------------------------------------------------------------
# 脚本读写
# ---------------------------------------------------------------------------
class TestScriptIO:
    def test_roundtrip(self, tmp_path: Path, sample_events):
        path = ep.save_script(tmp_path / "a.json", "测试脚本", sample_events)
        payload = ep.load_script(path)
        assert payload["events"] == sample_events
        assert payload["name"] == "测试脚本"
        assert payload["app"] == "MacroSmith"
        assert payload["duration_ms"] == 1050
        assert payload["event_count"] == len(sample_events)

    def test_extension_appended(self, tmp_path: Path):
        path = ep.save_script(tmp_path / "noext", "n", [ep.make_move_event(0, 1, 1)])
        assert path.suffix == ".json"
        assert path.exists()

    def test_unicode_name_preserved(self, tmp_path: Path):
        path = ep.save_script(tmp_path / "中文脚本.json", "中文脚本",
                              [ep.make_move_event(0, 1, 1)])
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["name"] == "中文脚本"

    def test_load_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(ep.ScriptError):
            ep.load_script(tmp_path / "nope.json")

    def test_load_broken_json_raises(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text("{ not json", encoding="utf-8")
        with pytest.raises(ep.ScriptError):
            ep.load_script(bad)

    def test_load_without_events_raises(self, tmp_path: Path):
        bad = tmp_path / "bad2.json"
        bad.write_text('{"name": "x"}', encoding="utf-8")
        with pytest.raises(ep.ScriptError):
            ep.load_script(bad)

    def test_list_scripts_sorted_by_mtime(self, tmp_path: Path):
        first = ep.save_script(tmp_path / "1.json", "1", [ep.make_move_event(0, 1, 1)])
        time.sleep(0.02)
        second = ep.save_script(tmp_path / "2.json", "2", [ep.make_move_event(0, 1, 1)])
        assert ep.list_scripts(tmp_path)[0] == second
        assert first in ep.list_scripts(tmp_path)

    def test_list_scripts_missing_dir(self, tmp_path: Path):
        assert ep.list_scripts(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# 模式一：原速回放
# ---------------------------------------------------------------------------
class TestTimedPlayback:
    def _make(self, events):
        mouse, keyboard = SpyController(), SpyController("kbd")
        player = MacroPlayer()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=keyboard)
        player.load(events, "t")
        return player, mouse, keyboard

    def test_timeline_accuracy(self):
        events = [ep.make_move_event(i * 100, i, i) for i in range(4)]
        player, mouse, _ = self._make(events)
        t0 = time.monotonic()
        assert player.start_timed()
        while player.is_replaying:
            time.sleep(0.005)
        elapsed = time.monotonic() - t0
        assert abs(elapsed - 0.3) < 0.08, f"实测 {elapsed:.3f}s"
        assert mouse.moves() == [(0, 0), (1, 1), (2, 2), (3, 3)]

    def test_speed_scales_timeline(self):
        events = [ep.make_move_event(i * 200, i, i) for i in range(3)]
        player, _, _ = self._make(events)
        t0 = time.monotonic()
        player.start_timed(speed=4.0)
        while player.is_replaying:
            time.sleep(0.005)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.25, f"4 倍速下应明显更快，实测 {elapsed:.3f}s"

    def test_pause_holds_position(self):
        events = [ep.make_move_event(i * 100, i, i) for i in range(8)]
        player, mouse, _ = self._make(events)
        player.start_timed()
        time.sleep(0.15)
        assert player.toggle_pause() is True
        time.sleep(0.08)
        frozen = len(mouse.moves())
        time.sleep(0.25)
        assert len(mouse.moves()) == frozen
        assert player.toggle_pause() is False
        while player.is_replaying:
            time.sleep(0.005)
        assert len(mouse.moves()) == 8

    def test_stop_resets_pointer(self):
        events = [ep.make_move_event(i * 100, i, i) for i in range(20)]
        player, _, _ = self._make(events)
        player.start_timed()
        time.sleep(0.1)
        player.stop()
        assert player.position == 0
        assert not player.is_replaying

    def test_start_from_middle_restores_held_keys(self, sample_events):
        """从中间开始时，之前按下的修饰键要恢复为按住状态。"""
        player, _, keyboard = self._make(sample_events)
        # sample_events 下标 9 是 Shift 按下，其后是 A 按下/释放
        player.start_timed(from_index=10)
        deadline = time.monotonic() + 1.0
        while player.is_replaying and time.monotonic() < deadline:
            time.sleep(0.005)
        actions = keyboard.actions()
        assert ("press", "vk:16") in actions, f"未恢复 Shift：{actions}"

    def test_empty_events_cannot_start(self):
        player = MacroPlayer()
        assert player.start_timed() is False

    def test_finished_callback_reason(self):
        reasons: list[str] = []
        player = MacroPlayer(on_finished=reasons.append)
        player.set_controllers(mouse_ctl=SpyController(), keyboard_ctl=SpyController("kbd"))
        player.load([ep.make_move_event(0, 1, 1)], "t")
        player.start_timed()
        while player.is_replaying:
            time.sleep(0.005)
        time.sleep(0.05)
        assert reasons == ["finished"]

    def test_stop_callback_reason(self):
        reasons: list[str] = []
        player = MacroPlayer(on_finished=reasons.append)
        player.set_controllers(mouse_ctl=SpyController(), keyboard_ctl=SpyController("kbd"))
        player.load([ep.make_move_event(i * 500, i, i) for i in range(10)], "t")
        player.start_timed()
        time.sleep(0.1)
        player.stop()
        time.sleep(0.05)
        assert reasons and reasons[-1] == "stopped"


# ---------------------------------------------------------------------------
# 模式二：单步执行
# ---------------------------------------------------------------------------
class TestStepPlayback:
    def _make(self, events):
        mouse, keyboard = SpyController(), SpyController("kbd")
        player = MacroPlayer()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=keyboard)
        player.load(events, "s")
        return player, mouse, keyboard

    def test_step_count_matches_parser(self, sample_events):
        player, _, _ = self._make(sample_events)
        assert player.step_total == len(ep.build_steps(sample_events))

    def test_step_next_advances_pointer(self, sample_events):
        player, _, _ = self._make(sample_events)
        assert player.step_index == 0
        assert player.step_next() == 1
        assert player.step_next() == 2
        assert player.step_index == 2

    def test_move_step_only_injects_final_point(self, sample_events):
        player, mouse, _ = self._make(sample_events)
        player.step_next()
        assert mouse.moves() == [(300, 400)]

    def test_click_step_injects_press_then_release(self, sample_events):
        player, mouse, _ = self._make(sample_events)
        player.step_next()
        mouse.mouse_actions.clear()
        player.step_next()
        assert mouse.mouse_actions == [("press", "left"), ("release", "left")]

    def test_key_step_injects_press_and_release(self, sample_events):
        """合并后的键盘步骤必须把「按下 + 释放」都发出去，否则会卡键。"""
        player, _, keyboard = self._make(sample_events)
        # 新分组下第 5 步是「键盘 A 按下并释放」（下标 4）
        assert player.steps[4].kind == "key"
        assert player.steps[4].event_count == 2
        player.run_step_at(4)
        assert keyboard.actions() == [("press", "vk:65"), ("release", "vk:65")]

    def test_step_prev_moves_back_and_reexecutes(self, sample_events):
        """回退 = 指针**真正后退一格**，并重新执行回退后所在的那一步。

        旧实现会把指针加回原值（净位移 0），于是「上一步」原地重放同一步、
        连按毫无变化、看起来像热键失效；本用例守住修复后的语义。
        """
        player, _, keyboard = self._make(sample_events)
        player.run_step_at(3)   # 步骤 4：滚轮
        player.run_step_at(4)   # 步骤 5：键盘 A 按下并释放
        assert player.step_index == 5
        keyboard.log.clear()
        assert player.step_prev() == 4      # 指针真正退回 4（不再原地不动）
        assert player.step_index == 4
        # 重新执行的是回退后所在的「步骤 4：滚轮」，不再重放键盘 A
        assert keyboard.actions() == []

    def test_step_prev_walks_back_to_start(self, sample_events):
        """连按回退可一路退到 0；到 0 后继续调用返回 None。"""
        player, _, _ = self._make(sample_events)
        for _ in range(3):
            player.step_next()
        assert player.step_index == 3
        assert player.step_prev() == 2
        assert player.step_prev() == 1
        assert player.step_prev() == 0
        assert player.step_prev() is None
        assert player.step_index == 0

    def test_step_prev_at_start_returns_none(self, sample_events):
        player, _, _ = self._make(sample_events)
        assert player.step_prev() is None

    def test_step_reset(self, sample_events):
        player, _, _ = self._make(sample_events)
        player.step_next()
        player.step_next()
        assert player.step_reset() == 0
        assert player.step_index == 0

    def test_end_of_sequence(self, sample_events):
        player, _, _ = self._make(sample_events)
        while player.step_next() is not None:
            pass
        assert player.step_index == player.step_total
        assert player.step_next() is None

    def test_run_step_at_out_of_range(self, sample_events):
        player, _, _ = self._make(sample_events)
        assert player.run_step_at(999) is None
        assert player.run_step_at(-1) is None

    def test_composite_key_order(self, sample_events):
        """组合键合并后：#6 一次触发 Shift+A，顺序必须是 按下修饰 → 按下目标 → 释放目标 → 释放修饰。"""
        player, _, keyboard = self._make(sample_events)
        player.step_reset()
        player.run_step_at(5)   # 步骤 6：组合键 Shift+A
        assert keyboard.actions() == [
            ("press", "vk:16"),                            # Shift 按下
            ("press", "vk:65"), ("release", "vk:65"),      # A 按下 + 释放
            ("release", "vk:16"),                          # Shift 释放
        ], keyboard.actions()

    def test_key_sequence_step_indices(self, sample_events):
        """键盘步骤序列：合并后仍严格保持 按下 -> 释放 的配对顺序。"""
        player, _, keyboard = self._make(sample_events)
        expected = [
            ("press", "vk:65"), ("release", "vk:65"),    # A 按下并释放（合并）
            ("press", "vk:16"),                           # 组合键 Shift+A：Shift 按下
            ("press", "vk:65"), ("release", "vk:65"),    # A 按下 + 释放
            ("release", "vk:16"),                         # Shift 释放
        ]
        player.step_reset()
        keyboard.log.clear()
        while player.step_next() is not None:
            pass
        assert [entry for entry in keyboard.actions()] == expected, keyboard.actions()

    def test_merged_step_has_no_stuck_key(self, sample_events):
        """合并步骤执行完毕后，每个按下都必须有对应的释放（不卡键）。"""
        player, _, keyboard = self._make(sample_events)
        player.step_reset()
        while player.step_next() is not None:
            pass
        held: set[str] = set()
        for action, token in keyboard.actions():
            if action == "press":
                held.add(token)
            else:
                held.discard(token)
        assert held == set(), f"以下按键没有释放：{held}"

    def test_empty_steps_error_callback(self):
        errors: list[str] = []
        player = MacroPlayer(on_error=errors.append)
        player.set_controllers(mouse_ctl=SpyController(), keyboard_ctl=SpyController("kbd"))
        assert player.step_next() is None
        assert errors


# ---------------------------------------------------------------------------
# 忽略自身事件
# ---------------------------------------------------------------------------
class TestExecutionFlag:
    def test_flag_true_during_injection(self):
        player = MacroPlayer()
        mouse = SpyController()
        samples: list[bool] = []
        original = mouse.press

        def probe(key):
            samples.append(player.is_executing)
            original(key)

        mouse.press = probe
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        player.load([ep.make_click_event(0, "press", "left", 1, 1)], "f")
        player.step_next()
        assert samples == [True]

    def test_flag_false_after_injection(self):
        player = MacroPlayer()
        player.set_controllers(mouse_ctl=SpyController(), keyboard_ctl=SpyController("kbd"))
        player.load([ep.make_move_event(0, 1, 1)], "f")
        player.step_next()
        assert player.is_executing is False

    def test_recorder_skips_while_executing(self):
        recorded: list[dict] = []
        recorder = Recorder(is_executing=lambda: True, on_event=recorded.append)
        recorder._recording = True
        recorder._t0 = time.monotonic()
        recorder._handle_press(type("K", (), {"vk": 65, "char": None})())
        recorder._handle_move(10, 10)
        recorder._handle_click(1, 1, None, True)
        recorder._handle_scroll(0, 0, 0, -1)
        assert recorded == []

    def test_nested_guard_returns_to_false(self):
        player = MacroPlayer()
        mouse = SpyController()
        depth: list[bool] = []
        original = mouse.press

        def probe(key):
            depth.append(player.is_executing)
            original(key)

        mouse.press = probe
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        player.load([ep.make_click_event(0, "press", "left", 1, 1)], "f")
        player.step_next()
        player.step_next()
        assert player.is_executing is False


# ---------------------------------------------------------------------------
# 录制器内部逻辑（不依赖真实钩子）
# ---------------------------------------------------------------------------
class TestRecorderInternals:
    def _recorder(self, **kwargs):
        rec = Recorder(is_executing=kwargs.pop("is_executing", lambda: False), **kwargs)
        rec._recording = True
        rec._t0 = time.monotonic()
        rec.move_min_interval_ms = 50
        rec.move_min_distance_px = 2
        return rec

    def test_jitter_filtered(self):
        rec = self._recorder()
        rec._handle_move(100, 100)
        rec._handle_move(101, 100)   # 间隔极小、位移 1px -> 丢弃
        events = rec.get_events()
        assert len(events) == 1
        assert (events[0]["x"], events[0]["y"]) == (100, 100)

    def test_large_move_kept(self):
        rec = self._recorder()
        rec._handle_move(100, 100)
        rec._handle_move(140, 160)   # 位移足够大 -> 保留
        assert len(rec.get_events()) == 2

    def test_slow_small_move_kept(self):
        rec = self._recorder()
        rec._handle_move(100, 100)
        rec._last_move = (time.monotonic() - 0.5, 100, 100)   # 模拟 500ms 前的移动
        rec._handle_move(101, 100)
        assert len(rec.get_events()) == 2

    def test_same_coord_move_dropped(self):
        """同坐标的重复 move 事件（pynput 在按钮状态变化时发出）必须丢弃，
        否则会在点击的按下与释放之间插入一条独立的 move 步骤。"""
        from pynput import mouse as ms

        rec = self._recorder()
        rec._handle_move(57, 159)
        rec._last_move = (time.monotonic() - 0.4, 57, 159)   # 400ms 前到达 (57,159)
        rec._handle_click(57, 159, ms.Button.left, True)     # 按下
        rec._handle_move(57, 159)                             # 按钮状态变化触发的同坐标 move
        rec._handle_click(57, 159, ms.Button.left, False)    # 释放
        events = rec.get_events()
        moves = [e for e in events if e["type"] == "mouse_move"]
        assert len(moves) == 1, f"同坐标 move 应被过滤，实际 {len(moves)} 条"

    def test_click_recorded_as_press_release(self):
        from pynput import mouse as ms

        rec = self._recorder()
        rec._handle_click(10, 20, ms.Button.left, True)
        rec._handle_click(10, 20, ms.Button.left, False)
        events = rec.get_events()
        assert [e["action"] for e in events] == ["press", "release"]
        assert events[0]["button"] == "left"

    def test_all_buttons_mapped(self):
        from pynput import mouse as ms

        rec = self._recorder()
        for button, name in ((ms.Button.left, "left"), (ms.Button.right, "right"),
                             (ms.Button.middle, "middle")):
            rec._handle_click(0, 0, button, True)
        assert [e["button"] for e in rec.get_events()] == ["left", "right", "middle"]

    def test_scroll_recorded(self):
        rec = self._recorder()
        rec._handle_scroll(5, 6, 0, -3)
        event = rec.get_events()[0]
        assert event["type"] == "mouse_scroll"
        assert event["dy"] == -3

    def test_timestamps_are_relative_and_increasing(self):
        rec = self._recorder()
        rec._handle_move(1, 1)
        time.sleep(0.05)
        rec._handle_move(100, 100)
        times = [e["t"] for e in rec.get_events()]
        assert times[0] >= 0
        assert times[1] > times[0]

    def test_key_token_roundtrip(self):
        from pynput import keyboard as kb

        key = parse_key("f9")
        assert key is not None, "F9 应能被解析成 pynput 按键对象"
        token, vk = key_to_token(key)
        restored = token_to_key(token, vk)
        assert restored == key

    def test_special_key_token_roundtrip(self):
        from pynput import keyboard as kb

        # pynput 的 Key 枚举在 Windows 上没有 vk，使用枚举名作为标识
        token, vk = key_to_token(kb.Key.page_down)
        assert token == "page_down"
        assert vk is None
        assert token_to_key(token, vk) == kb.Key.page_down

    def test_special_key_token_with_vk(self):
        from pynput import keyboard as kb

        token, vk = key_to_token(kb.KeyCode.from_vk(34))
        assert token == "vk:34"
        assert token_to_key(token, vk) == kb.KeyCode.from_vk(34)

    def test_char_key_token(self):
        from pynput import keyboard as kb

        token, vk = key_to_token(kb.KeyCode.from_char("a"))
        restored = token_to_key(token, vk)
        assert getattr(restored, "char", None) == "a" or getattr(restored, "vk", None) is not None

    def test_executing_flag_error_is_treated_as_not_executing(self):
        """标志位读取失败必须按“未执行”处理，避免把用户操作整段吞掉。"""
        recorded: list[dict] = []
        rec = self._recorder(on_event=recorded.append)
        rec._is_executing = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        rec._handle_move(1, 1)
        assert len(recorded) == 1


# ---------------------------------------------------------------------------
# 热键管理
# ---------------------------------------------------------------------------
class TestHotkeyManager:
    def test_parse_named_keys(self):
        assert parse_key("f9") is not None
        assert parse_key("page_down") is not None
        assert parse_key("home") is not None
        assert parse_key("a") is not None
        assert parse_key("这不是键") is None
        assert parse_key("") is None

    def test_normalize_and_display(self):
        assert normalize_token("PgDn") == "page_down"
        assert normalize_token("F9") == "f9"
        assert display_name("page_up") == "PageUp"
        assert display_name("f9") == "F9"
        assert display_name("a") == "A"

    def test_register_all_reports_failures(self):
        mgr = HotkeyManager()
        failed = mgr.register_all({"play_toggle": "f6", "bad": "这不是键"})
        assert failed == ["bad"]
        assert mgr.binding_display("play_toggle") == "F6"

    def test_duplicate_detection(self):
        mgr = HotkeyManager()
        mgr.register_all({"play_toggle": "f6", "pause_toggle": "f7"})
        assert mgr.duplicate_actions("play_toggle", "f6") == []
        assert mgr.duplicate_actions("play_toggle", "f7") == ["pause_toggle"]

    def test_callback_dispatch_and_guard(self):
        fired: list[str] = []
        executing = {"value": False}
        mgr = HotkeyManager(is_executing=lambda: executing["value"])
        mgr.register_all({"play_toggle": "f6"})
        mgr.set_callback("play_toggle", lambda: fired.append("play"))

        key = parse_key("f6")
        mgr._on_press(key)
        assert fired == ["play"]

        executing["value"] = True     # 程序自己注入的按键必须被忽略
        mgr._on_press(key)
        assert fired == ["play"]

    def test_callback_exception_does_not_crash_listener(self):
        mgr = HotkeyManager()
        mgr.register_all({"play_toggle": "f6"})

        def boom():
            raise RuntimeError("boom")

        mgr.set_callback("play_toggle", boom)
        mgr._on_press(parse_key("f6"))   # 不应抛出

    def test_unknown_key_ignored(self):
        mgr = HotkeyManager()
        mgr.register_all({"play_toggle": "f6"})
        mgr._on_press(parse_key("a"))    # 未绑定，不应出错

    def test_set_binding_bad_token(self):
        mgr = HotkeyManager()
        assert mgr.set_binding("play_toggle", "这不是键") is False
        assert mgr.set_binding("play_toggle", "f6") is True


# ---------------------------------------------------------------------------
# 第四阶段：热键独占
# ---------------------------------------------------------------------------
class TestKeyToVk:
    def test_function_keys(self):
        from core.hotkey_manager import key_to_vk

        assert key_to_vk("f6") == 0x75      # VK_F6
        assert key_to_vk("f8") == 0x77      # VK_F8
        assert key_to_vk("f9") == 0x78      # VK_F9

    def test_navigation_keys(self):
        from core.hotkey_manager import key_to_vk

        assert key_to_vk("page_down") == 0x22
        assert key_to_vk("page_up") == 0x21
        assert key_to_vk("home") == 0x24
        assert key_to_vk("down") == 0x28

    def test_char_keys(self):
        from core.hotkey_manager import key_to_vk

        assert key_to_vk("a") == 0x41
        assert key_to_vk("A") == 0x41
        assert key_to_vk("1") == 0x31
        assert key_to_vk("space") == 0x20

    def test_explicit_vk_token(self):
        from core.hotkey_manager import key_to_vk

        assert key_to_vk("vk:114") == 114

    def test_unknown_token(self):
        from core.hotkey_manager import key_to_vk

        assert key_to_vk("这不是键") is None
        assert key_to_vk("") is None


class TestHotkeyExclusiveMode:
    """独占模式：命中热键要吞掉事件，且放行其它键与自身注入。"""

    def test_vkmap_built_from_bindings(self):
        mgr = HotkeyManager(exclusive=True)
        mgr.register_all({"play_toggle": "f6", "play_stop": "f8",
                          "step_next": "page_down"})
        assert mgr._vkmap == {0x75: "play_toggle", 0x77: "play_stop",
                              0x22: "step_next"}

    def test_should_block_registered_hotkey(self):
        mgr = HotkeyManager(exclusive=True)
        mgr.register_all({"play_toggle": "f6"})
        assert mgr._should_block_vk(0x75) is True
        assert mgr._should_block_vk(0x41) is False      # A 没绑定

    def test_should_not_block_while_executing(self):
        """程序自己注入的按键不能被吞，否则键盘会失效。"""
        executing = {"value": True}
        mgr = HotkeyManager(is_executing=lambda: executing["value"], exclusive=True)
        mgr.register_all({"play_toggle": "f6"})
        assert mgr._should_block_vk(0x75) is False
        executing["value"] = False
        assert mgr._should_block_vk(0x75) is True

    def test_exclusive_flag_reflects_config(self):
        assert HotkeyManager().exclusive is False
        assert HotkeyManager(exclusive=True).exclusive is True

    def test_set_exclusive_switches_mode(self):
        mgr = HotkeyManager()
        assert mgr.set_exclusive(True) is True
        assert mgr.exclusive is True
        assert mgr.set_exclusive(False) is False
        assert mgr.exclusive is False

    def test_on_hook_hotkey_dispatches_callback(self):
        fired: list[str] = []
        mgr = HotkeyManager(exclusive=True)
        mgr.register_all({"play_toggle": "f6"})
        mgr.set_callback("play_toggle", lambda: fired.append("play"))
        mgr._on_hook_hotkey(0x75)
        assert fired == ["play"]
        mgr._on_hook_hotkey(0x41)      # 未绑定
        assert fired == ["play"]

    def test_on_hook_hotkey_exception_is_contained(self):
        mgr = HotkeyManager(exclusive=True)
        mgr.register_all({"play_toggle": "f6"})
        mgr.set_callback("play_toggle", lambda: (_ for _ in ()).throw(RuntimeError()))
        mgr._on_hook_hotkey(0x75)      # 不应抛出，否则会中断钩子链


@pytest.mark.skipif(not hook_supported(), reason="仅 Windows 支持低级键盘钩子")
class TestKeyboardHook:
    """真实安装钩子，验证返回值语义（1 = 吞掉，0 = 放行）。"""

    @staticmethod
    def _call(hook, vk: int, flags: int = 0, msg: int = WM_KEYDOWN) -> int:
        """用构造的 KBDLLHOOKSTRUCT 直接调用钩子过程。"""
        struct = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=flags,
                                 time=0, dwExtraInfo=0)
        lp = ctypes.cast(ctypes.pointer(struct), ctypes.c_void_p).value
        return hook._hook_proc(HC_ACTION, msg, lp)

    def test_blocks_registered_key(self):
        hits: list[int] = []
        hook = KeyboardHook(on_hotkey=hits.append, should_block=lambda vk: vk == 0x75)
        assert hook.start(), hook.last_error
        try:
            assert self._call(hook, 0x75) == 1, "命中的热键必须被吞掉"
            assert hits == [0x75]
            assert self._call(hook, 0x75, msg=WM_KEYUP) == 1
            assert hits == [0x75], "抬起不应重复触发动作"
        finally:
            hook.stop()

    def test_passes_unregistered_key(self):
        hook = KeyboardHook(on_hotkey=lambda vk: None, should_block=lambda vk: vk == 0x75)
        assert hook.start(), hook.last_error
        try:
            assert self._call(hook, 0x41) == 0, "未绑定的键必须放行"
        finally:
            hook.stop()

    def test_passes_injected_key(self):
        """带 LLKHF_INJECTED 的按键必须放行，避免模拟输入被自己吞掉。"""
        hook = KeyboardHook(on_hotkey=lambda vk: None, should_block=lambda vk: vk == 0x75)
        assert hook.start(), hook.last_error
        try:
            assert self._call(hook, 0x75, flags=LLKHF_INJECTED) == 0
        finally:
            hook.stop()

    def test_protected_keys_never_blocked(self):
        """Win / Esc 永远放行，避免把系统锁死。"""
        hook = KeyboardHook(on_hotkey=lambda vk: None, should_block=lambda vk: True)
        assert hook.start(), hook.last_error
        try:
            for vk in (0x5B, 0x5C, 0x1B):
                assert self._call(hook, vk) == 0, f"vk={vk} 不应被独占"
        finally:
            hook.stop()

    def test_stop_uninstalls_hook(self):
        hook = KeyboardHook()
        assert hook.start(), hook.last_error
        assert hook.hook_alive()
        hook.stop()
        assert not hook.is_running
        assert not hook.hook_alive(), "卸载后钩子必须失效，系统键盘恢复正常"

    def test_blocked_count_tracks_hits(self):
        hook = KeyboardHook(on_hotkey=lambda vk: None, should_block=lambda vk: vk == 0x75)
        assert hook.start(), hook.last_error
        try:
            for _ in range(3):
                self._call(hook, 0x75)
            self._call(hook, 0x41)
            assert hook.blocked_count == 3
        finally:
            hook.stop()

    def test_start_reports_failure_without_leaking(self):
        """重复 start 不应重复安装；stop 之后可以再 start。"""
        hook = KeyboardHook()
        assert hook.start()
        assert hook.start() is True       # 幂等
        hook.stop()
        assert hook.start()               # 可以重新装
        hook.stop()


class TestExclusiveFallback:
    """独占启动失败时必须退化到普通监听，而不是让热键彻底失效。"""

    def test_manager_falls_back_when_hook_unavailable(self, monkeypatch):
        import core.hotkey_manager as hm

        monkeypatch.setattr(hm, "hook_supported", lambda: False)
        mgr = HotkeyManager(exclusive=True)
        mgr.register_all({"play_toggle": "f6"})
        assert mgr.start() is False
        assert "不支持" in mgr._last_error

        # 退化到普通模式后必须能正常启动
        mgr.set_exclusive(False)
        assert mgr.start() is True
        assert mgr.is_running
        mgr.stop()


# ---------------------------------------------------------------------------
# 第五阶段：组合键与双击/三击智能合并
# ---------------------------------------------------------------------------
def _key(t: int, action: str, vk: int) -> dict:
    return ep.make_key_event(t, action, f"vk:{vk}", vk)


def _click(t: int, action: str, button: str = "left",
           x: int = 100, y: int = 200) -> dict:
    return ep.make_click_event(t, action, button, x, y)


class TestComboMerging:
    def test_single_modifier_combo(self):
        events = [_key(0, "press", 17), _key(15, "press", 86),
                  _key(60, "release", 86), _key(75, "release", 17)]
        steps = ep.build_steps(events)
        assert len(steps) == 1
        step = steps[0]
        assert step.kind == "combo"
        assert step.description == "组合键 Ctrl+V"
        assert step.modifiers == ["Ctrl"]
        assert step.target_key == "V"
        assert step.event_count == 4
        assert [e["action"] for e in step.events] == ["press", "press", "release", "release"]

    def test_multi_modifier_combo(self):
        events = [_key(0, "press", 17), _key(10, "press", 16),
                  _key(25, "press", 83), _key(70, "release", 83),
                  _key(85, "release", 16), _key(95, "release", 17)]
        steps = ep.build_steps(events)
        assert len(steps) == 1
        assert steps[0].description == "组合键 Ctrl+Shift+S"
        assert steps[0].modifiers == ["Ctrl", "Shift"]
        assert steps[0].target_key == "S"

    def test_alt_combo(self):
        events = [_key(0, "press", 18), _key(10, "press", 115),
                  _key(50, "release", 115), _key(60, "release", 18)]
        steps = ep.build_steps(events)
        assert steps[0].description == "组合键 Alt+F4"

    def test_win_combo(self):
        events = [_key(0, "press", 0x5B), _key(10, "press", 69),
                  _key(50, "release", 69), _key(60, "release", 0x5B)]
        steps = ep.build_steps(events)
        assert steps[0].description == "组合键 Win+E"

    def test_combo_disabled(self):
        events = [_key(0, "press", 17), _key(15, "press", 86),
                  _key(60, "release", 86), _key(75, "release", 17)]
        steps = ep.build_steps(events, ep.MergeOptions(combo=False))
        assert [s.kind for s in steps] == ["key", "key", "key"]
        assert steps[0].description == "键盘 Ctrl 按下"

    def test_incomplete_combo_not_merged(self):
        """修饰键没有释放（或按下顺序不对）时不合并，保持真实时序。"""
        events = [_key(0, "press", 17), _key(15, "press", 86),
                  _key(60, "release", 86)]
        steps = ep.build_steps(events)
        assert all(s.kind != "combo" for s in steps), [s.description for s in steps]

    def test_interleaved_event_blocks_combo(self):
        """修饰键与目标键之间夹了鼠标事件 -> 不合并（避免打乱时序）。"""
        events = [_key(0, "press", 17), _click(8, "press"),
                  _click(20, "release"), _key(30, "press", 86),
                  _key(60, "release", 86), _key(75, "release", 17)]
        steps = ep.build_steps(events)
        assert all(s.kind != "combo" for s in steps)
        # 点击本身仍然正常合并
        assert any(s.kind == "click" for s in steps)

    def test_combo_timestamps_preserved(self):
        """组合键内部时间戳必须原样保留（模式一按原速回放）。"""
        events = [_key(100, "press", 17), _key(115, "press", 86),
                  _key(160, "release", 86), _key(175, "release", 17)]
        step = ep.build_steps(events)[0]
        assert [e["t"] for e in step.events] == [100, 115, 160, 175]


class TestMultiClickMerging:
    def test_double_click(self):
        events = [_click(0, "press"), _click(40, "release"),
                  _click(120, "press"), _click(160, "release")]
        steps = ep.build_steps(events)
        assert len(steps) == 1
        assert steps[0].kind == "multi"
        assert steps[0].description == "鼠标双击 (100, 200)"
        assert steps[0].click_count == 2
        assert steps[0].event_count == 4

    def test_triple_click(self):
        events = [_click(0, "press"), _click(40, "release"),
                  _click(120, "press"), _click(160, "release"),
                  _click(240, "press"), _click(280, "release")]
        steps = ep.build_steps(events)
        assert len(steps) == 1
        assert steps[0].description == "鼠标三击 (100, 200)"
        assert steps[0].click_count == 3

    @pytest.mark.parametrize("count", [4, 5, 6, 8, 12, 20])
    def test_unlimited_click_count_merges_into_one_step(self, count):
        """N >= 2 必须整体合并为一步（不设上限）。

        上限 3 会拆出「三击 + 单击」，模式二两次热键之间的延迟必然超过
        系统连击窗口，第 4 次点击永远不会被识别成连击 —— 正是要避免的情况。
        """
        events: list[dict] = []
        for i in range(count):
            events += [_click(i * 120, "press"), _click(i * 120 + 40, "release")]
        steps = ep.build_steps(events)
        assert len(steps) == 1, f"{count} 连击被拆成了 {len(steps)} 步"
        assert steps[0].kind == "multi"
        assert steps[0].click_count == count
        assert steps[0].event_count == count * 2
        assert sum(s.event_count for s in steps) == len(events)
        expected = ("鼠标双击" if count == 2 else
                    "鼠标三击" if count == 3 else f"鼠标连击{count}次")
        assert steps[0].description == f"{expected} (100, 200)"

    def test_many_clicks_no_event_loss(self):
        """大量连击也不能丢事件。"""
        events: list[dict] = []
        for i in range(40):
            events += [_click(i * 60, "press"), _click(i * 60 + 30, "release")]
        steps = ep.build_steps(events)
        assert len(steps) == 1
        assert steps[0].click_count == 40
        assert sum(s.event_count for s in steps) == 80

    def test_click_verb_wording(self):
        assert ep.click_verb(1) == "单击"
        assert ep.click_verb(2) == "双击"
        assert ep.click_verb(3) == "三击"
        assert ep.click_verb(4) == "连击4次"
        assert ep.click_verb(10) == "连击10次"

    def test_click_count_from_events(self):
        events = [_click(0, "press"), _click(40, "release"),
                  _click(120, "press"), _click(160, "release")]
        assert ep.click_count_from_events(events) == 2

    def test_gap_too_large_not_merged(self):
        events = [_click(0, "press"), _click(40, "release"),
                  _click(900, "press"), _click(940, "release")]
        steps = ep.build_steps(events)
        assert [s.kind for s in steps] == ["click", "click"]

    def test_long_click_chain_breaks_at_slow_gap(self):
        """6 次点击中第 3 次间隔过大 -> 断链成 2 连击 + 4 连击（不设上限）。"""
        events: list[dict] = []
        times = [0, 120, 1200, 1320, 1440, 1560]
        for t in times:
            events += [_click(t, "press"), _click(t + 40, "release")]
        steps = ep.build_steps(events)
        assert [s.kind for s in steps] == ["multi", "multi"]
        assert [s.click_count for s in steps] == [2, 4], \
            [s.description for s in steps]
        assert sum(s.event_count for s in steps) == len(events)

    def test_click_chain_never_split_by_count_limit(self):
        """慢速间隔之前的所有连续点击必须落在同一步里。"""
        events: list[dict] = []
        for t in [0, 100, 200, 300, 400, 500, 600]:
            events += [_click(t, "press"), _click(t + 40, "release")]
        steps = ep.build_steps(events)
        assert len(steps) == 1
        assert steps[0].click_count == 7

    def test_long_click_chain_with_one_break(self):
        """5 次快速点击 + 1 次慢速间隔 -> 前 5 次整体一步，最后 1 次单独成步。"""
        events: list[dict] = []
        times = [0, 100, 200, 300, 400, 2000]
        for t in times:
            events += [_click(t, "press"), _click(t + 40, "release")]
        steps = ep.build_steps(events)
        assert [s.kind for s in steps] == ["multi", "click"]
        assert steps[0].click_count == 5
        assert sum(s.event_count for s in steps) == len(events)

    def test_different_position_not_merged(self):
        events = [_click(0, "press", x=100, y=100), _click(40, "release", x=100, y=100),
                  _click(120, "press", x=200, y=100), _click(160, "release", x=200, y=100)]
        steps = ep.build_steps(events)
        assert [s.kind for s in steps] == ["click", "click"]

    def test_position_within_tolerance_merged(self):
        """容差 5 像素以内视为同一点击。"""
        events = [_click(0, "press", x=100, y=100), _click(40, "release", x=100, y=100),
                  _click(120, "press", x=103, y=102), _click(160, "release", x=103, y=102)]
        steps = ep.build_steps(events)
        assert len(steps) == 1 and steps[0].kind == "multi"

    def test_different_button_not_merged(self):
        events = [_click(0, "press", "left"), _click(40, "release", "left"),
                  _click(120, "press", "right"), _click(160, "release", "right")]
        steps = ep.build_steps(events)
        assert [s.kind for s in steps] == ["click", "click"]

    def test_right_double_click_wording(self):
        events = [_click(0, "press", "right"), _click(40, "release", "right"),
                  _click(120, "press", "right"), _click(160, "release", "right")]
        steps = ep.build_steps(events)
        assert steps[0].description == "鼠标右键双击 (100, 200)"

    def test_right_quadruple_click_wording(self):
        events: list[dict] = []
        for i in range(4):
            events += [_click(i * 120, "press", "right"),
                       _click(i * 120 + 40, "release", "right")]
        steps = ep.build_steps(events)
        assert steps[0].description == "鼠标右键连击4次 (100, 200)"

    def test_multi_click_disabled(self):
        events = [_click(0, "press"), _click(40, "release"),
                  _click(120, "press"), _click(160, "release")]
        steps = ep.build_steps(events, ep.MergeOptions(multi_click=False))
        assert [s.kind for s in steps] == ["click", "click"]

    def test_multi_click_timestamps_preserved(self):
        events = [_click(10, "press"), _click(50, "release"),
                  _click(130, "press"), _click(170, "release")]
        step = ep.build_steps(events)[0]
        assert [e["t"] for e in step.events] == [10, 50, 130, 170]

    def test_make_step_events_high_repeat(self):
        """插入步骤时 repeat 可以填大于 3，也要能识别成一步连击。"""
        events, description = ep.make_step_events("click", x=5, y=6, repeat=6)
        assert len(events) == 12
        assert description == "鼠标连击6次 (5, 6)"
        built = ep.build_steps(events)
        assert len(built) == 1 and built[0].click_count == 6


class TestSplitClickStep:
    """「拆分为独立单击」：连击步骤按需还原成 N 个单击步骤。"""

    @staticmethod
    def _multi(count: int) -> ep.Step:
        events: list[dict] = []
        for i in range(count):
            events += [_click(i * 120, "press"), _click(i * 120 + 40, "release")]
        steps = ep.build_steps(events)
        assert steps[0].kind == "multi"
        return steps[0]

    @pytest.mark.parametrize("count", [2, 3, 5, 9])
    def test_split_returns_n_groups(self, count):
        groups = ep.split_click_step(self._multi(count))
        assert len(groups) == count
        for group in groups:
            assert len(group) == 2
            assert [e["action"] for e in group] == ["press", "release"]

    def test_split_preserves_button_and_position(self):
        groups = ep.split_click_step(self._multi(4))
        for group in groups:
            assert all(e["button"] == "left" for e in group)
            assert all(e["x"] == 100 and e["y"] == 200 for e in group)

    def test_split_preserves_hold_duration(self):
        """每个子单击都要保留原来的按住时长。"""
        step = self._multi(3)
        groups = ep.split_click_step(step)
        for group in groups:
            hold = group[1]["t"] - group[0]["t"]
            assert hold == 40, hold

    def test_split_single_click_is_unchanged(self):
        step = ep.build_steps([_click(0, "press"), _click(40, "release")])[0]
        groups = ep.split_click_step(step)
        assert len(groups) == 1

    def test_replace_with_many_produces_independent_steps(self):
        step = self._multi(5)
        groups = ep.split_click_step(step)
        flat = ep.replace_step_with_many([step], 0, groups)
        after = ep.build_steps(flat)
        assert len(after) == 5
        assert all(s.kind == "click" for s in after)
        assert sum(s.event_count for s in after) == 10

    def test_split_survives_remerge_attempt(self):
        """拆分后的步骤不会因为「自动合并开着」被粘回去。"""
        step = self._multi(4)
        groups = ep.split_click_step(step)
        flat = ep.replace_step_with_many([step], 0, groups)
        # 默认选项（合并全开）下仍是 4 步
        assert len(ep.build_steps(flat)) == 4
        # 反复摊平/重建也保持 4 步
        again = ep.steps_to_events(ep.build_steps(flat))
        assert len(ep.build_steps(again)) == 4

    def test_split_keeps_surrounding_steps(self):
        """拆分中间某一步不应影响前后步骤。"""
        before = ep.build_steps([ep.make_move_event(0, 1, 1),
                                 ep.make_scroll_event(50, 0, -1, 1, 1)])
        multi = self._multi(3)
        steps = before + [multi]
        groups = ep.split_click_step(multi)
        flat = ep.replace_step_with_many(steps, 2, groups)
        after = ep.build_steps(flat)
        assert [s.kind for s in after] == ["move", "scroll", "click", "click", "click"]

    def test_replace_with_many_out_of_range(self):
        with pytest.raises(ep.StepEditError):
            ep.replace_step_with_many([], 0, [[_click(0, "press")]])

    def test_replace_with_many_empty_groups(self):
        step = self._multi(2)
        with pytest.raises(ep.StepEditError):
            ep.replace_step_with_many([step], 0, [])

    def test_rebuild_high_click_count(self):
        """编辑器把连击次数改成大于 3 也要能正确重建。"""
        step = ep.build_steps([_click(0, "press"), _click(40, "release")])[0]
        out = ep.rebuild_step_events(step, click_count=7)
        assert len(out) == 14
        built = ep.build_steps(out)
        assert len(built) == 1
        assert built[0].click_count == 7
        assert built[0].description == "鼠标连击7次 (100, 200)"

    def test_rebuild_click_count_clamped_to_max(self):
        step = ep.build_steps([_click(0, "press"), _click(40, "release")])[0]
        out = ep.rebuild_step_events(step, click_count=10_000)
        assert len(out) == ep.MAX_CLICK_COUNT * 2


class TestMergeBoundaries:
    """手工拆分过的步骤必须被尊重，不能被自动合并回去。"""

    def test_manual_split_double_click_stays_split(self):
        events = [_click(0, "press"), _click(40, "release"),
                  _click(120, "press"), _click(160, "release")]
        # 用户把它们拆成两个单击步骤
        manual = [
            ep.Step(index=1, kind="click", description="左键单击 (100, 200)",
                    events=events[:2]),
            ep.Step(index=2, kind="click", description="左键单击 (100, 200)",
                    events=events[2:]),
        ]
        flat = ep.steps_to_events(manual)
        steps = ep.build_steps(flat)
        assert [s.kind for s in steps] == ["click", "click"], \
            [s.description for s in steps]

    def test_manual_split_combo_stays_split(self):
        events = [_key(0, "press", 17), _key(15, "press", 86),
                  _key(60, "release", 86), _key(75, "release", 17)]
        manual = [
            ep.Step(index=1, kind="key", description="键盘 Ctrl 按下", events=events[:1]),
            ep.Step(index=2, kind="key", description="键盘 V 按下并释放", events=events[1:3]),
            ep.Step(index=3, kind="key", description="键盘 Ctrl 释放", events=events[3:]),
        ]
        flat = ep.steps_to_events(manual)
        steps = ep.build_steps(flat)
        assert all(s.kind == "key" for s in steps), [s.description for s in steps]
        assert len(steps) == 3

    def test_auto_merged_step_rebuilds_identically(self):
        """自动合并出来的步骤，摊平再分组必须得到同样的步骤（开关可逆）。"""
        cases = [
            [_key(0, "press", 17), _key(15, "press", 86),
             _key(60, "release", 86), _key(75, "release", 17)],
            [_click(0, "press"), _click(40, "release"),
             _click(120, "press"), _click(160, "release")],
            [_key(0, "press", 65), _key(40, "release", 65)],
            [ep.make_move_event(0, 1, 1), ep.make_move_event(10, 2, 2)],
        ]
        for events in cases:
            steps = ep.build_steps(events)
            flat = ep.steps_to_events(steps)
            rebuilt = ep.build_steps(flat)
            assert [s.description for s in rebuilt] == [s.description for s in steps], \
                f"{[s.description for s in steps]} != {[s.description for s in rebuilt]}"
            assert [s.kind for s in rebuilt] == [s.kind for s in steps]

    def test_auto_merged_step_can_be_unmerged_by_options(self):
        """摊平出来的事件在关闭开关后能还原成拆分形态（边界没有被锁死）。"""
        events = [_key(0, "press", 17), _key(15, "press", 86),
                  _key(60, "release", 86), _key(75, "release", 17)]
        flat = ep.steps_to_events(ep.build_steps(events))
        split = ep.build_steps(flat, ep.MergeOptions(combo=False))
        assert [s.kind for s in split] == ["key", "key", "key"]


class TestComboAndMultiPlayback:
    """模式二：复合步骤必须快速连续执行完内部所有事件。"""

    def test_combo_step_executes_all_four_events(self):
        events = [_key(0, "press", 17), _key(15, "press", 86),
                  _key(60, "release", 86), _key(75, "release", 17)]
        player = MacroPlayer()
        keyboard = SpyController("kbd")
        player.set_controllers(mouse_ctl=SpyController(), keyboard_ctl=keyboard)
        player.load(events, "combo")
        assert player.step_total == 1
        player.step_next()
        assert keyboard.actions() == [
            ("press", "vk:17"), ("press", "vk:86"),
            ("release", "vk:86"), ("release", "vk:17"),
        ]

    def test_combo_step_is_fast(self):
        """一次热键触发整个组合键，耗时必须远小于真实录制间隔。"""
        events = [_key(0, "press", 17), _key(15, "press", 86),
                  _key(1600, "release", 86), _key(1615, "release", 17)]
        player = MacroPlayer()
        player.set_controllers(mouse_ctl=SpyController(),
                               keyboard_ctl=SpyController("kbd"))
        player.load(events, "combo")
        t0 = time.monotonic()
        player.step_next()
        elapsed = time.monotonic() - t0
        # 真实间隔 1.6 秒，快速执行后应远小于它
        assert elapsed < 0.2, f"组合键执行耗时 {elapsed:.3f}s，太慢"

    def test_double_click_step_fires_two_clicks_quickly(self):
        events = [_click(0, "press"), _click(40, "release"),
                  _click(120, "press"), _click(160, "release")]
        player = MacroPlayer()
        mouse = SpyController()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        player.load(events, "multi")
        assert player.step_total == 1
        t0 = time.monotonic()
        player.step_next()
        elapsed = time.monotonic() - t0
        assert mouse.mouse_actions == [
            ("press", "left"), ("release", "left"),
            ("press", "left"), ("release", "left"),
        ]
        # 两次按下之间必须远小于系统双击判定窗口（500ms）
        assert elapsed < 0.3, f"双击执行耗时 {elapsed:.3f}s，系统可能识别不出"

    def test_double_click_interval_under_threshold(self):
        """实测两次按下之间的间隔必须在双击阈值内。"""
        events = [_click(0, "press"), _click(40, "release"),
                  _click(400, "press"), _click(440, "release")]
        player = MacroPlayer()
        mouse = SpyController()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        player.load(events, "multi")

        stamps: list[float] = []
        original = mouse.press

        def timed_press(key):
            stamps.append(time.monotonic())
            original(key)

        mouse.press = timed_press
        player.step_next()
        assert len(stamps) == 2
        gap_ms = (stamps[1] - stamps[0]) * 1000
        assert gap_ms < ep.MULTI_CLICK_MAX_GAP_MS, f"两次按下间隔 {gap_ms:.0f}ms"

    def test_combo_no_stuck_modifier(self):
        """组合键执行完，修饰键必须已经释放，不能卡住 Ctrl。"""
        events = [_key(0, "press", 17), _key(15, "press", 86),
                  _key(60, "release", 86), _key(75, "release", 17)]
        player = MacroPlayer()
        keyboard = SpyController("kbd")
        player.set_controllers(mouse_ctl=SpyController(), keyboard_ctl=keyboard)
        player.load(events, "combo")
        player.step_next()
        held: set[str] = set()
        for action, token in keyboard.actions():
            held.add(token) if action == "press" else held.discard(token)
        assert held == set(), f"未释放：{held}"

    def test_is_compound_flag(self):
        combo = ep.build_steps([_key(0, "press", 17), _key(15, "press", 86),
                                _key(60, "release", 86), _key(75, "release", 17)])[0]
        multi = ep.build_steps([_click(0, "press"), _click(40, "release"),
                                _click(120, "press"), _click(160, "release")])[0]
        single = ep.build_steps([_click(0, "press"), _click(40, "release")])[0]
        assert combo.is_compound and multi.is_compound
        assert not single.is_compound


class TestStepParamEditing:
    """第五阶段：鼠标「动作 / 位置」分离编辑、键盘改键、组合键编辑。"""

    def test_change_click_coordinates_only(self):
        step = ep.build_steps([_click(0, "press"), _click(40, "release")])[0]
        out = ep.rebuild_step_events(step, x=555, y=666)
        assert all(e["x"] == 555 and e["y"] == 666 for e in out)
        assert all(e["button"] == "left" for e in out)
        assert [e["t"] for e in out] == [0, 40]      # 节奏不变

    def test_change_click_action_only(self):
        step = ep.build_steps([_click(0, "press"), _click(40, "release")])[0]
        out = ep.rebuild_step_events(step, button="right")
        assert all(e["button"] == "right" for e in out)
        assert all(e["x"] == 100 and e["y"] == 200 for e in out)

    def test_single_to_double_click(self):
        step = ep.build_steps([_click(0, "press"), _click(40, "release")])[0]
        out = ep.rebuild_step_events(step, click_count=2)
        assert len(out) == 4
        built = ep.build_steps(out)
        assert len(built) == 1 and built[0].kind == "multi"
        assert built[0].click_count == 2

    def test_triple_to_single_click(self):
        events = [_click(0, "press"), _click(40, "release"),
                  _click(120, "press"), _click(160, "release"),
                  _click(240, "press"), _click(280, "release")]
        step = ep.build_steps(events)[0]
        out = ep.rebuild_step_events(step, click_count=1)
        assert len(out) == 2
        built = ep.build_steps(out)
        assert built[0].kind == "click"

    def test_change_key_keeps_pairing(self):
        step = ep.build_steps([_key(0, "press", 65), _key(40, "release", 65)])[0]
        out = ep.rebuild_step_events(step, key="vk:66", vk=66)
        assert [e["key"] for e in out] == ["vk:66", "vk:66"]
        assert [e["action"] for e in out] == ["press", "release"]
        assert [e["t"] for e in out] == [0, 40]

    def test_change_combo_modifiers(self):
        events = [_key(0, "press", 17), _key(15, "press", 86),
                  _key(60, "release", 86), _key(75, "release", 17)]
        step = ep.build_steps(events)[0]
        out = ep.rebuild_step_events(step, modifiers=["Ctrl", "Alt"])
        desc, mods, target = ep.describe_combo(out)
        assert desc == "组合键 Ctrl+Alt+V"
        assert mods == ["Ctrl", "Alt"] and target == "V"
        # 重新分组后仍应被识别为一个 combo 步骤
        rebuilt = ep.build_steps(ep.steps_to_events([ep.Step(
            index=1, kind="combo", description=desc, events=out)]))
        assert rebuilt[0].kind == "combo"

    def test_change_combo_target_key(self):
        events = [_key(0, "press", 17), _key(15, "press", 86),
                  _key(60, "release", 86), _key(75, "release", 17)]
        step = ep.build_steps(events)[0]
        out = ep.rebuild_step_events(step, key="vk:67", vk=67)
        desc, _mods, target = ep.describe_combo(out)
        assert desc == "组合键 Ctrl+C"
        assert target == "C"

    def test_change_move_coordinates(self):
        step = ep.build_steps([ep.make_move_event(0, 1, 1)])[0]
        out = ep.rebuild_step_events(step, x=800, y=600)
        assert out[0]["x"] == 800 and out[0]["y"] == 600

    def test_change_scroll_amount(self):
        step = ep.build_steps([ep.make_scroll_event(0, 0, -2, 10, 10)])[0]
        out = ep.rebuild_step_events(step, dx=0, dy=-5, x=20, y=30)
        assert out[0]["dy"] == -5 and out[0]["dx"] == 0
        assert out[0]["x"] == 20 and out[0]["y"] == 30

    def test_replace_step_type_change(self):
        """replace_step 支持把单击步替换成双击步（类型也跟着变）。"""
        events = [_click(0, "press"), _click(40, "release")]
        steps = ep.build_steps(events)
        new_events = ep.rebuild_step_events(steps[0], click_count=2)
        out = ep.replace_step(steps, 0, new_events, kind="multi")
        rebuilt = ep.build_steps(out)
        assert len(rebuilt) == 1
        assert rebuilt[0].kind == "multi"
        assert rebuilt[0].description == "鼠标双击 (100, 200)"

    def test_rebuild_unknown_kind_raises(self):
        step = ep.Step(index=1, kind="weird", description="?",
                       events=[{"t": 0, "type": "mystery"}])
        with pytest.raises(ep.StepEditError):
            ep.rebuild_step_events(step, x=1)

    def test_rebuild_empty_step_raises(self):
        step = ep.Step(index=1, kind="click", description="?", events=[])
        with pytest.raises(ep.StepEditError):
            ep.rebuild_step_events(step, x=1)

    def test_make_step_events_combo(self):
        events, description = ep.make_step_events("combo", modifiers=["Ctrl"],
                                                  key="vk:86", vk=86)
        assert description == "组合键 Ctrl+V"
        assert len(events) == 4
        assert [e["action"] for e in events] == ["press", "press", "release", "release"]
        built = ep.build_steps(events)
        assert built[0].kind == "combo"

    def test_all_modifier_names(self):
        assert set(ep.all_modifier_names()) == {"Shift", "Ctrl", "Alt", "Win"}


class TestRecorderIgnoreKeys:
    """第五阶段：停止录制热键 F9 不能录进脚本。"""

    def _recorder(self, events: list[dict]):
        rec = Recorder(is_executing=lambda: False, on_event=events.append)
        rec._recording = True
        rec._t0 = time.monotonic()
        return rec

    def test_f9_press_is_ignored(self):
        from pynput import keyboard as kb

        recorded: list[dict] = []
        rec = self._recorder(recorded)
        rec._handle_press(kb.Key.f9)
        rec._handle_release(kb.Key.f9)
        assert recorded == [], recorded

    def test_other_keys_still_recorded(self):
        from pynput import keyboard as kb

        recorded: list[dict] = []
        rec = self._recorder(recorded)
        rec._handle_press(kb.KeyCode.from_char("a"))
        rec._handle_release(kb.KeyCode.from_char("a"))
        assert len(recorded) == 2
        assert all(e["type"] == "key" for e in recorded)

    def test_sanitize_events_drops_f9(self):
        events = [_key(0, "press", 65), _key(10, "release", 65),
                  _key(20, "press", 120), _key(30, "release", 120)]
        cleaned = ep.sanitize_events(events, ignore_keys={"vk:120"})
        assert all(e["vk"] != 120 for e in cleaned)
        assert len(cleaned) == 2

    def test_recorder_stop_filters_f9(self):
        from pynput import keyboard as kb

        recorded: list[dict] = []
        rec = self._recorder(recorded)
        # 绕过回调过滤，直接塞进 F9 事件，验证 stop() 仍会清掉
        rec._events = [_key(0, "press", 65), _key(10, "release", 65),
                       _key(20, "press", 120), _key(30, "release", 120)]
        out = rec.stop()
        assert all(e.get("vk") != 120 for e in out), out
        assert len(out) == 2

    def test_ignore_keys_configurable(self):
        from pynput import keyboard as kb

        recorded: list[dict] = []
        rec = self._recorder(recorded)
        rec.ignored_keys = set()          # 清空忽略名单 -> F9 会被录下
        rec._handle_press(kb.Key.f9)
        assert len(recorded) == 1


# ---------------------------------------------------------------------------
# 第六阶段：循环回放
# ---------------------------------------------------------------------------
class TestLoopSettings:
    def test_defaults(self):
        from core.player import LOOP_NONE

        player = MacroPlayer()
        assert player.loop_mode == LOOP_NONE
        assert player.loop_count == 1
        assert player.loop_rounds_total == 1
        assert player.loop_progress_text() == ""

    def test_set_loop_fixed(self):
        from core.player import LOOP_FIXED

        player = MacroPlayer()
        player.set_loop("fixed", 5)
        assert player.loop_mode == LOOP_FIXED
        assert player.loop_count == 5
        assert player.loop_rounds_total == 5
        assert player.loop_progress_text() == "第 1 / 5 轮"

    def test_set_loop_infinite(self):
        from core.player import LOOP_INFINITE

        player = MacroPlayer()
        player.set_loop("infinite")
        assert player.loop_mode == LOOP_INFINITE
        assert player.loop_rounds_total == 0
        assert "无限循环" in player.loop_progress_text()

    def test_normalize_invalid_mode(self):
        from core.player import LOOP_NONE, normalize_loop_mode

        assert normalize_loop_mode("bogus") == LOOP_NONE
        assert normalize_loop_mode(None) == LOOP_NONE
        assert normalize_loop_mode("FIXED") == "fixed"

    def test_normalize_count_clamped(self):
        from core.player import (MAX_LOOP_COUNT, MIN_LOOP_COUNT,
                                 normalize_loop_count)

        assert normalize_loop_count(0) == MIN_LOOP_COUNT
        assert normalize_loop_count(-5) == MIN_LOOP_COUNT
        assert normalize_loop_count(10 ** 9) == MAX_LOOP_COUNT
        assert normalize_loop_count("abc") == 1

    def test_reset_keeps_loop_settings(self):
        """重置指针不能把用户的循环设置清掉（它是偏好而非运行状态）。"""
        player = MacroPlayer()
        player.set_loop("fixed", 4)
        player.reset()
        assert player.loop_mode == "fixed"
        assert player.loop_count == 4
        assert player.loop_round == 0


class TestLoopPlayback:
    """模式一循环执行。"""

    @staticmethod
    def _events():
        return [ep.make_move_event(0, 10, 10),
                ep.make_move_event(60, 20, 20),
                ep.make_move_event(120, 30, 30)]

    def _run(self, mode, count, timeout=8.0):
        player = MacroPlayer()
        mouse = SpyController()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        player.load(self._events(), "loop")
        player.set_loop(mode, count)
        assert player.start_timed()
        deadline = time.monotonic() + timeout
        while player.is_replaying and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not player.is_replaying, "循环回放未在超时前结束"
        return player, mouse

    def test_no_loop_runs_once(self):
        player, mouse = self._run("none", 1)
        assert mouse.moves() == [(10, 10), (20, 20), (30, 30)]
        assert player.loop_rounds_done == 1

    def test_fixed_rounds_repeats(self):
        player, mouse = self._run("fixed", 3)
        assert mouse.moves() == [(10, 10), (20, 20), (30, 30)] * 3
        assert player.loop_rounds_done == 3

    def test_fixed_one_round_is_same_as_no_loop(self):
        player, mouse = self._run("fixed", 1)
        assert mouse.moves() == [(10, 10), (20, 20), (30, 30)]
        assert player.loop_rounds_done == 1

    def test_infinite_loops_until_stopped(self):
        player = MacroPlayer()
        mouse = SpyController()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        player.load(self._events(), "loop")
        player.set_loop("infinite")
        assert player.start_timed()
        time.sleep(0.75)
        player.stop()
        deadline = time.monotonic() + 3
        while player.is_replaying and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not player.is_replaying
        # 单轮约 120ms + 轮间隔 120ms，0.75s 内至少 2 轮
        assert player.loop_rounds_done >= 2, player.loop_rounds_done
        assert len(mouse.moves()) >= 6

    def test_loop_round_increments(self):
        player = MacroPlayer()
        seen: list[int] = []
        player.set_controllers(mouse_ctl=SpyController(),
                               keyboard_ctl=SpyController("kbd"))
        player.load(self._events(), "loop")
        player.set_loop("fixed", 3)
        original = player._execute_event

        def probe(evt, raw_index):
            if player.loop_round not in seen:
                seen.append(player.loop_round)
            return original(evt, raw_index)

        player._execute_event = probe
        player.start_timed()
        deadline = time.monotonic() + 5
        while player.is_replaying and time.monotonic() < deadline:
            time.sleep(0.005)
        assert seen == [1, 2, 3], seen

    def test_pause_resume_during_loop(self):
        """循环过程中 F7 暂停/继续必须有效。"""
        player = MacroPlayer()
        mouse = SpyController()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        player.load(self._events(), "loop")
        player.set_loop("infinite")
        assert player.start_timed()
        time.sleep(0.2)
        assert player.pause() is True
        assert player.is_paused is True
        settled = len(mouse.moves())
        time.sleep(0.3)
        # 暂停期间不应再有动作
        assert len(mouse.moves()) == settled, "暂停期间仍在注入事件"
        assert player.resume() is True
        time.sleep(0.3)
        assert len(mouse.moves()) > settled, "继续后应恢复注入"
        player.stop()
        deadline = time.monotonic() + 3
        while player.is_replaying and time.monotonic() < deadline:
            time.sleep(0.005)

    def test_single_event_loop_does_not_spin(self):
        """只有一个事件的脚本也能循环，且不会因为跨度 0 而失控。"""
        player = MacroPlayer()
        player.set_controllers(mouse_ctl=SpyController(),
                               keyboard_ctl=SpyController("kbd"))
        player.load([ep.make_move_event(0, 1, 1)], "single")
        player.set_loop("fixed", 2)
        assert player.start_timed()
        deadline = time.monotonic() + 5
        while player.is_replaying and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not player.is_replaying
        assert player.loop_rounds_done == 2

    def test_from_middle_disables_loop(self):
        """从中间开始回放时不做循环（时间轴起点不对会重现错位）。"""
        player = MacroPlayer()
        player.set_controllers(mouse_ctl=SpyController(),
                               keyboard_ctl=SpyController("kbd"))
        player.load(self._events(), "loop")
        player.set_loop("fixed", 3)
        assert player.start_timed(from_index=1)
        assert player.loop_mode == "none"
        deadline = time.monotonic() + 5
        while player.is_replaying and time.monotonic() < deadline:
            time.sleep(0.005)
        assert player.loop_rounds_done == 1

    def test_loop_setting_change_takes_effect_next_round(self):
        """回放中改循环模式：下一轮生效（不崩、不丢事件）。"""
        player = MacroPlayer()
        mouse = SpyController()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=SpyController("kbd"))
        player.load(self._events(), "loop")
        player.set_loop("infinite")
        player.start_timed()
        time.sleep(0.2)
        player.set_loop("fixed", 2)
        deadline = time.monotonic() + 5
        while player.is_replaying and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not player.is_replaying
        assert player.loop_rounds_done >= 2


class TestLoopConfig:
    def test_defaults_are_no_loop(self, tmp_path: Path):
        cfg = Config(tmp_path / "c.json")
        assert cfg.loop_mode == "none"
        assert cfg.loop_count == 1

    def test_set_and_reload(self, tmp_path: Path):
        path = tmp_path / "c.json"
        cfg = Config(path)
        cfg.set_loop("fixed", 7)
        cfg.save()
        reloaded = Config(path)
        assert reloaded.loop_mode == "fixed"
        assert reloaded.loop_count == 7

    def test_invalid_values_sanitized(self, tmp_path: Path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"loop": {"mode": "nonsense", "count": -3}}),
                        encoding="utf-8")
        cfg = Config(path)
        assert cfg.loop_mode == "none"
        assert cfg.loop_count == 1

    def test_config_drives_player(self, tmp_path: Path):
        cfg = Config(tmp_path / "c.json")
        cfg.set_loop("infinite")
        player = MacroPlayer()
        player.set_loop(cfg.loop_mode, cfg.loop_count)
        assert player.loop_mode == "infinite"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
class TestConfig:
    def test_defaults(self, tmp_path: Path):
        from core.config import DEFAULT_WINDOW_HEIGHT, DEFAULT_WINDOW_WIDTH

        cfg = Config(tmp_path / "config.json")
        assert cfg.hotkey("record_stop") == "f9"
        assert cfg.hotkey("step_next") == "page_down"
        # 第五阶段：默认窗口尺寸提升到 1000x700
        assert DEFAULT_WINDOW_WIDTH == 1000 and DEFAULT_WINDOW_HEIGHT == 700
        assert cfg.get("window.width") == DEFAULT_WINDOW_WIDTH
        assert cfg.get("window.height") == DEFAULT_WINDOW_HEIGHT
        assert cfg.get("player.speed") == 1.0

    def test_merge_defaults_on(self, tmp_path: Path):
        cfg = Config(tmp_path / "config.json")
        assert cfg.merge_combo is True
        assert cfg.merge_multi_click is True
        opts = cfg.merge_options()
        assert opts.combo is True and opts.multi_click is True

    def test_merge_toggle_roundtrip(self, tmp_path: Path):
        path = tmp_path / "config.json"
        cfg = Config(path)
        cfg.set_merge(combo=False, multi_click=True)
        cfg.save()
        reloaded = Config(path)
        assert reloaded.merge_combo is False
        assert reloaded.merge_multi_click is True

    def test_legacy_default_window_size_migrates(self, tmp_path: Path):
        """老配置里正好是历史默认 900x600 -> 视为没调过，升级到 1000x700。"""
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"window": {"x": 12, "y": 34,
                                               "width": 900, "height": 600}}),
                        encoding="utf-8")
        cfg = Config(path)
        assert cfg.get("window.width") == 1000
        assert cfg.get("window.height") == 700
        # 位置必须保留
        assert cfg.get("window.x") == 12 and cfg.get("window.y") == 34

    def test_user_resized_window_is_kept(self, tmp_path: Path):
        """用户手动调过的尺寸不能被迁移覆盖。"""
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"window": {"x": 5, "y": 6,
                                               "width": 1234, "height": 812}}),
                        encoding="utf-8")
        cfg = Config(path)
        assert cfg.get("window.width") == 1234
        assert cfg.get("window.height") == 812

    def test_window_floor_clamped(self, tmp_path: Path):
        """过小的尺寸会被夹到可用的下限。"""
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"window": {"width": 100, "height": 100}}),
                        encoding="utf-8")
        cfg = Config(path)
        assert cfg.get("window.width") >= 820
        assert cfg.get("window.height") >= 560

    def test_save_and_reload(self, tmp_path: Path):
        path = tmp_path / "config.json"
        cfg = Config(path)
        cfg.set("window.x", 123)
        cfg.set_hotkey("play_toggle", "F8")
        assert cfg.save()
        again = Config(path)
        assert again.get("window.x") == 123
        assert again.hotkey("play_toggle") == "f8"    # 统一小写

    def test_corrupt_file_falls_back(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text("{ broken", encoding="utf-8")
        cfg = Config(path)
        assert cfg.hotkey("record_stop") == "f9"
        assert path.with_suffix(".json.bak").exists()

    def test_blank_hotkey_restored_to_default(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text('{"hotkeys": {"record_stop": "  "}}', encoding="utf-8")
        assert Config(path).hotkey("record_stop") == "f9"

    def test_legacy_flat_keys_migrated(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text('{"hotkey_play": "f5"}', encoding="utf-8")
        assert Config(path).hotkey("play_toggle") == "f5"

    def test_invalid_mode_sanitized(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text('{"player": {"default_mode": "乱七八糟", "speed": "x"}}',
                        encoding="utf-8")
        cfg = Config(path)
        assert cfg.get("player.default_mode") == MODE_TIMED
        assert isinstance(cfg.get("player.speed"), float)

    def test_scripts_path_created(self, tmp_path: Path):
        cfg = Config(tmp_path / "config.json")
        cfg.set("scripts_dir", "data/scripts")
        path = cfg.scripts_path()
        assert path.exists()
        assert path.name == "scripts"


# ---------------------------------------------------------------------------
# 兼容轮询录制器（PollRecorder）
# ---------------------------------------------------------------------------

class TestPollRecorderBlacklist:
    """前台进程黑名单匹配（纯函数）。"""

    def test_tencent_processes_match(self):
        for stem in ("wegame", "QQGame", "SGuard64", "wegame_helper",
                     "tgp_daemon", "AntiCheatExpert"):
            assert _is_blacklisted_process(stem), stem

    def test_normal_processes_no_match(self):
        for stem in ("", "explorer", "chrome", "notepad", "code"):
            assert not _is_blacklisted_process(stem), stem


class TestPollRecorderLifecycle:
    """轮询通道的启停与时间基（真实 WinAPI，周期短、无 UI 依赖）。"""

    def test_start_stop_empty_without_blacklist_foreground(self):
        rec = PollRecorder()
        assert rec.is_recording is False
        assert rec.start(t0=time.monotonic()) is True
        assert rec.is_recording is True
        assert rec.start(t0=time.monotonic()) is False  # 重复启动被拒
        assert rec.stop() == []
        assert rec.is_recording is False

    def test_stop_is_idempotent(self):
        rec = PollRecorder()
        assert rec.stop() == []
        rec.start(t0=time.monotonic())
        rec.stop()
        assert rec.stop() == []

    def test_elapsed_uses_shared_t0(self):
        rec = PollRecorder()
        t0 = time.monotonic() - 1.0
        rec.start(t0=t0)
        try:
            # 时基对齐：elapsed 应接近 1000ms（允许轮询周期误差）
            assert 900 <= rec.elapsed_ms() <= 1300
        finally:
            rec.stop()

    def test_recorder_t0_property_before_start(self):
        assert Recorder().t0 == 0.0
