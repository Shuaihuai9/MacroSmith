"""事件序列解析：原始事件描述、JSON 读写、模式二“操作单元”分组。

数据格式
--------
一个录制脚本就是一个 JSON 文件，其中 ``events`` 是原始事件数组
（模式一原速回放直接消费该数组）::

    {
      "app": "MacroSmith", "version": 1, "name": "脚本名",
      "created_at": "2025-01-01 12:00:00", "duration_ms": 12345,
      "events": [
        {"t": 0,    "type": "key",    "action": "press",   "key": "vk:65", "vk": 65},
        {"t": 120,  "type": "mouse_move", "x": 10, "y": 20},
        {"t": 200,  "type": "mouse_click", "action": "press", "button": "left", "x": 10, "y": 20},
        {"t": 260,  "type": "mouse_click", "action": "release", "button": "left", "x": 10, "y": 20},
        {"t": 300,  "type": "mouse_scroll", "dx": 0, "dy": -1, "x": 10, "y": 20}
      ]
    }

每个事件都带一个相对录制开始的毫秒时间戳 ``t``。

模式二把 ``events`` 归并成“操作单元”（step）：
连续的 ``mouse_move`` 合并为一步（只保留最终落点），一次完整的
按下+释放算一步，一次滚轮算一步。``raw_start``/``raw_end`` 记录该步骤
对应的原始事件区间，回退/前进时据此把执行指针精确复位。
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

APP_TAG = "MacroSmith"
FORMAT_VERSION = 1

#: 步骤边界标记（仅存在于内存，不写入 JSON）。
#: 录制时连续的 ``mouse_move`` 会被合并成一步；但用户在编辑器里把两步挪到一起后，
#: 它们是两个独立步骤，不应该再次被合并。:func:`steps_to_events` 会把这个标记
#: 打在每个步骤的首事件上，合并逻辑（移动分组 / 连击 / 组合键 / 按下+释放）
#: 遇到它就不再跨越合并。
#:
#: 保存到 JSON 时会被 :func:`strip_internal_fields` 去掉：文件里只存原始事件流，
#: 重新载入时按设置页的开关重新合并（这也是「旧脚本加载后自动合并」的实现方式）。
BOUNDARY_KEY = "_step_start"

MOUSE_BUTTON_CN = {"left": "左键", "right": "右键", "middle": "中键", "unknown": "其他键"}

#: 特殊键 vk 码 -> 中文显示名
VK_DISPLAY: dict[str, str] = {
    "vk:8": "Backspace", "vk:9": "Tab", "vk:13": "Enter", "vk:16": "Shift",
    "vk:17": "Ctrl", "vk:18": "Alt", "vk:19": "Pause", "vk:20": "CapsLock",
    "vk:27": "Esc", "vk:32": "Space", "vk:33": "PageUp", "vk:34": "PageDown",
    "vk:35": "End", "vk:36": "Home", "vk:37": "Left", "vk:38": "Up",
    "vk:39": "Right", "vk:40": "Down", "vk:45": "Insert", "vk:46": "Delete",
    "vk:91": "Win", "vk:92": "Win", "vk:93": "Menu",
    **{f"vk:{112 + i}": f"F{i + 1}" for i in range(24)},
}


# ---------------------------------------------------------------------------
# 事件构造与描述
# ---------------------------------------------------------------------------

def make_move_event(t: int, x: int, y: int) -> dict[str, Any]:
    return {"t": int(t), "type": "mouse_move", "x": int(x), "y": int(y)}


def make_click_event(t: int, action: str, button: str, x: int, y: int) -> dict[str, Any]:
    return {
        "t": int(t), "type": "mouse_click", "action": action,
        "button": button, "x": int(x), "y": int(y),
    }


def make_scroll_event(t: int, dx: int, dy: int, x: int, y: int) -> dict[str, Any]:
    return {
        "t": int(t), "type": "mouse_scroll", "dx": int(dx), "dy": int(dy),
        "x": int(x), "y": int(y),
    }


def make_key_event(t: int, action: str, key: str, vk: int | None = None) -> dict[str, Any]:
    return {"t": int(t), "type": "key", "action": action, "key": key, "vk": vk}


def key_display(event: dict[str, Any]) -> str:
    """把键盘事件里的键标识转成人类可读名称。"""
    raw = str(event.get("key") or "")
    if raw in VK_DISPLAY:
        return VK_DISPLAY[raw]
    vk = event.get("vk")
    if isinstance(vk, int):
        display = VK_DISPLAY.get(f"vk:{vk}")
        if display:
            return display
        if 32 <= vk < 127:
            return chr(vk)
        return f"VK{vk}"
    if raw.startswith("char:"):
        return raw[5:]
    return raw or "未知键"


def describe(event: dict[str, Any]) -> str:
    """把单个事件描述成中文短句，供界面列表显示。"""
    etype = event.get("type")
    if etype == "mouse_move":
        return f"移动到 ({event.get('x')}, {event.get('y')})"
    if etype == "mouse_click":
        button = MOUSE_BUTTON_CN.get(str(event.get("button")), str(event.get("button")))
        action = "按下" if event.get("action") == "press" else "释放"
        return f"{button}{action} ({event.get('x')}, {event.get('y')})"
    if etype == "mouse_scroll":
        dy = int(event.get("dy") or 0)
        direction = "上滚" if dy > 0 else ("下滚" if dy < 0 else "横向滚动")
        return f"滚轮{direction} {abs(dy)} 格 @({event.get('x')}, {event.get('y')})"
    if etype == "key":
        action = "按下" if event.get("action") == "press" else "释放"
        return f"键盘 {key_display(event)} {action}"
    return str(etype or "未知事件")


def type_label(event: dict[str, Any]) -> str:
    """事件类型的中文分类名（列表“类型”列）。"""
    return {
        "mouse_move": "鼠标移动",
        "mouse_click": "鼠标点击",
        "mouse_scroll": "鼠标滚轮",
        "key": "键盘按键",
    }.get(str(event.get("type")), "其他")


# ---------------------------------------------------------------------------
# 模式二：操作单元（step）分组
# ---------------------------------------------------------------------------

@dataclass
class Step:
    """一个“操作单元”，模式二单步执行的最小粒度。"""

    index: int                 # 步骤序号（从 1 开始，仅用于显示）
    kind: str                  # move | click | scroll | key | combo | multi
    description: str           # 中文描述
    events: list[dict[str, Any]] = field(default_factory=list)  # 该步骤的原始事件
    raw_start: int = 0         # 起始原始事件下标
    raw_end: int = 0           # 结束原始事件下标（不含）
    t_start: int = 0           # 起始时间戳（毫秒）
    t_end: int = 0             # 结束时间戳（毫秒）
    #: 组合键的修饰键列表（``["Ctrl", "Shift"]``），仅 ``kind == "combo"`` 时有值
    modifiers: list[str] = field(default_factory=list)
    #: 目标按键的可读名（``"V"``），仅 ``kind == "combo"`` 时有值
    target_key: str = ""
    #: 连击次数（2 = 双击，3 = 三击），仅 ``kind == "multi"`` 时 > 1
    click_count: int = 0
    #: 显式长按持续时间（毫秒），0 表示不启用，使用原始时间戳的自然按住时长
    explicit_hold_ms: int = 0
    #: 是否无限连击（模式一一直重复直到用户停止；模式二每按一次热键触发一轮）
    infinite_repeat: bool = False
    #: 无限连击的每轮间隔（毫秒）
    repeat_interval_ms: int = 50
    #: 是否为「插入时自动补帧」的插值移动步骤（模式二据此逐帧平滑执行）
    interpolated: bool = False

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def hold_ms(self) -> int:
        """步骤内部“按住”的时长（合并后的按下→释放间隔）。

        只有点击 / 按键这类成对事件才非 0；移动、滚轮为 0。
        """
        return max(0, int(self.t_end) - int(self.t_start))

    @property
    def duration_ms(self) -> int:
        """步骤占用的时长（等同 :attr:`hold_ms`，语义更直观）。"""
        return self.hold_ms

    @property
    def kind_label(self) -> str:
        """步骤类型的中文名。"""
        return {
            "move": "鼠标移动", "click": "鼠标点击", "scroll": "鼠标滚轮",
            "key": "键盘按键", "combo": "组合键", "multi": "鼠标连击",
        }.get(self.kind, self.kind)

    @property
    def is_compound(self) -> bool:
        """是否为「合并出来的复合步骤」（组合键 / 双击 / 三击）。

        模式二执行这类步骤时要**快速连续**发完内部事件，
        否则系统识别不出双击，组合键也可能因为间隔过大而失效。
        """
        return self.kind in ("combo", "multi")

    def as_dict(self) -> dict[str, Any]:
        out = {
            "index": self.index,
            "kind": self.kind,
            "description": self.description,
            "raw_start": self.raw_start,
            "raw_end": self.raw_end,
            "t_start": self.t_start,
            "t_end": self.t_end,
            "hold_ms": self.hold_ms,
            "event_count": self.event_count,
        }
        if self.modifiers:
            out["modifiers"] = list(self.modifiers)
        if self.target_key:
            out["target_key"] = self.target_key
        if self.click_count:
            out["click_count"] = self.click_count
        return out


# ---------------------------------------------------------------------------
# 智能合并参数（组合键 / 双击三击）
# ---------------------------------------------------------------------------
#: 合并后的组合键文本前缀
COMBO_PREFIX = "组合键"
#: 鼠标连击：坐标容差（像素）
MULTI_CLICK_TOLERANCE_PX = 5
#: 鼠标连击：相邻两次按下的最大间隔（毫秒）。
#: 500ms 与 Windows 默认双击速度一致，保证录下来的连击确实能被系统识别。
MULTI_CLICK_MAX_GAP_MS = 500
#: 连击次数的安全上限（仅防止手滑输入天文数字，正常使用不会碰到）
MAX_CLICK_COUNT = 50

#: 修饰键 vk -> 显示名（用于「组合键 Ctrl+V」）
MODIFIER_VKS: dict[int, str] = {
    0x10: "Shift", 0x11: "Ctrl", 0x12: "Alt",
    0x5B: "Win", 0x5C: "Win",
}
#: 修饰键显示名 -> 该修饰键可能出现的虚拟键码（左右各一个，用于匹配 release）
MODIFIER_VK_ALIASES: dict[str, tuple[int, ...]] = {
    "Shift": (0x10, 0xA0, 0xA1),
    "Ctrl": (0x11, 0xA2, 0xA3),
    "Alt": (0x12, 0xA4, 0xA5),
    "Win": (0x5B, 0x5C),
}
#: 所有“广义修饰键”的虚拟键码集合（左右键都算）
MODIFIER_VK_SET: frozenset[int] = frozenset(
    vk for aliases in MODIFIER_VK_ALIASES.values() for vk in aliases
)

#: 修饰键的**键名** -> 规范名。
#: 为什么需要它：pynput 在 Windows 上多数按键拿不到 ``vk``
#: （``Key.ctrl_l.vk is None``、``Key.ctrl_l.value`` 是 ``KeyCode(162)``），
#: 而录制器是按 ``key_to_token`` 存成 ``"ctrl_l"`` 这种名字的。
#: 只按 vk 判断会导致**真实录制的 Ctrl+V 无法识别成组合键**。
MODIFIER_TOKEN_NAMES: dict[str, str] = {
    "shift": "Shift", "shift_l": "Shift", "shift_r": "Shift",
    "ctrl": "Ctrl", "ctrl_l": "Ctrl", "ctrl_r": "Ctrl",
    "alt": "Alt", "alt_l": "Alt", "alt_r": "Alt", "alt_gr": "Alt",
    "cmd": "Win", "cmd_l": "Win", "cmd_r": "Win",
    "win": "Win", "win_l": "Win", "win_r": "Win",
    "super": "Win", "meta": "Win",
}


def modifier_name_from_token(token: str) -> Optional[str]:
    """键名 -> 规范修饰键名；不是修饰键时返回 ``None``。"""
    text = str(token or "").strip().lower()
    if not text:
        return None
    if text in MODIFIER_TOKEN_NAMES:
        return MODIFIER_TOKEN_NAMES[text]
    # vk:162 / vk:17 这类也认出来
    if text.startswith("vk:"):
        try:
            return modifier_name(int(text[3:]))
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class MergeOptions:
    """自动合并开关。默认全部开启（新建脚本按新结构走）。"""

    #: 把「修饰键按下 + 目标键按下/释放 + 修饰键释放」合并成一步「组合键」
    combo: bool = True
    #: 把连续快速的同点同键点击合并成「鼠标双击 / 三击」
    multi_click: bool = True
    #: 连击坐标容差（像素）
    click_tolerance_px: int = MULTI_CLICK_TOLERANCE_PX
    #: 连击相邻两次按下的最大间隔（毫秒）
    multi_click_gap_ms: int = MULTI_CLICK_MAX_GAP_MS

    @classmethod
    def none(cls) -> "MergeOptions":
        """全部关闭（用于「保持原有拆分记录方式」）。"""
        return cls(combo=False, multi_click=False)

    @classmethod
    def combo_off(cls) -> "MergeOptions":
        """只关掉组合键合并。"""
        return cls(combo=False, multi_click=True)

    @classmethod
    def multi_click_off(cls) -> "MergeOptions":
        """只关掉双击/三击合并。"""
        return cls(combo=True, multi_click=False)

    @classmethod
    def all_on(cls) -> "MergeOptions":
        return cls(combo=True, multi_click=True)


def _event_vk(evt: dict[str, Any]) -> Optional[int]:
    """取出事件的虚拟键码（优先 ``vk`` 字段，否则从 ``vk:NN`` 解析）。"""
    vk = evt.get("vk")
    if isinstance(vk, int):
        return vk
    token = str(evt.get("key") or "")
    if token.startswith("vk:"):
        try:
            return int(token[3:])
        except ValueError:
            return None
    return None


def is_modifier_event(evt: dict[str, Any]) -> bool:
    """该键盘事件是否为修饰键（Shift / Ctrl / Alt / Win，左右键都算）。

    先看虚拟键码；pynput 拿不到 vk 时退回看键名（``"ctrl_l"`` 等），
    否则真实录制的组合键永远匹配不上。
    """
    if evt.get("type") != "key":
        return False
    vk = _event_vk(evt)
    if vk is not None:
        return vk in MODIFIER_VK_SET
    return modifier_name_from_token(str(evt.get("key") or "")) is not None


def modifier_name_of(evt: dict[str, Any]) -> Optional[str]:
    """从键盘事件取出规范修饰键名（``"Ctrl"``），不是修饰键时返回 ``None``。"""
    if evt.get("type") != "key":
        return None
    vk = _event_vk(evt)
    if vk is not None and vk in MODIFIER_VK_SET:
        return modifier_name(vk)
    return modifier_name_from_token(str(evt.get("key") or ""))


def modifier_name(vk: Optional[int]) -> str:
    """修饰键虚拟键码 -> 显示名。"""
    if vk is None:
        return "?"
    name = MODIFIER_VKS.get(vk)
    if name:
        return name
    for display, aliases in MODIFIER_VK_ALIASES.items():
        if vk in aliases:
            return display
    return f"VK{vk}"


def _modifier_release_matches(press_evt: dict[str, Any],
                              release_evt: dict[str, Any]) -> bool:
    """判断 ``release_evt`` 是否为 ``press_evt`` 那个修饰键的释放。"""
    if release_evt.get("type") != "key" or release_evt.get("action") != "release":
        return False
    press_name = modifier_name_of(press_evt)
    release_name = modifier_name_of(release_evt)
    if press_name and release_name:
        # 左右 Ctrl 都归为同一个修饰键
        return press_name == release_name
    press_vk = _event_vk(press_evt)
    release_vk = _event_vk(release_evt)
    if press_vk is None or release_vk is None:
        return str(press_evt.get("key")) == str(release_evt.get("key"))
    return press_vk == release_vk


def _match_combo(events: Sequence[dict[str, Any]], start: int) -> Optional[int]:
    """从 ``start`` 起匹配「修饰键 + 普通键」组合，返回区间结束下标（不含）。

    要求形如::

        mod1 按下 [, mod2 按下] , 目标键 按下 , 目标键 释放 [, mod2 释放] , mod1 释放

    即按下顺序与释放顺序**严格相反**（真实的组合键就是这个形状），
    中间不能夹带其它事件（移动、滚轮等），否则不合并，避免打乱时序。
    返回 ``None`` 表示不构成组合键。

    注意：只检查**组合内部**的边界标记，不检查 ``start`` 自身的。
    ``steps_to_events`` 会给每个步骤的首事件打上边界标记（表示「这里是新的一步」），
    若把它也当作「不可合并」的信号，自动合并出来的组合键步骤就永远无法重新识别了。
    """
    evt = events[start]
    if not is_modifier_event(evt) or evt.get("action") != "press":
        return None

    # 1) 连续收集修饰键按下
    mods: list[dict[str, Any]] = []
    index = start
    while index < len(events):
        cur = events[index]
        if not is_modifier_event(cur) or cur.get("action") != "press":
            break
        if index > start and cur.get(BOUNDARY_KEY):
            break
        mods.append(cur)
        index += 1

    # 2) 必须紧跟一个目标键的「按下 + 释放」
    if index + 1 >= len(events):
        return None
    target_press = events[index]
    target_release = events[index + 1]
    if target_press.get("type") != "key" or target_press.get("action") != "press":
        return None
    # 目标键上带边界标记 => 用户把它单独拆成了一步，不能并回去
    if target_press.get(BOUNDARY_KEY):
        return None
    if target_release.get(BOUNDARY_KEY):
        return None
    if not _match_press_release(events, index):
        return None
    index += 2

    # 3) 修饰键按相反顺序释放（释放事件带边界标记同样视为用户拆过）
    for mod in reversed(mods):
        if index >= len(events):
            return None
        if events[index].get(BOUNDARY_KEY):
            return None
        if not _modifier_release_matches(mod, events[index]):
            return None
        index += 1

    return index


def _match_repeated_clicks(events: Sequence[dict[str, Any]], start: int,
                           options: MergeOptions) -> tuple[int, int]:
    """从 ``start`` 起匹配连续快速点击，返回 ``(结束下标, 连击次数)``。

    单次点击的判定复用 :func:`_match_press_release`（按下 + 同键释放），
    相邻两次点击必须满足：同一按键、坐标容差内、按下间隔小于阈值。
    匹配不到 2 连击时返回 ``(start + 1, 0)``。

    **不设连击次数上限**（N >= 2 全部合并）
    --------------------------------------
    原先上限是 3（四连击拆成「三击 + 单击」），但那样会毁掉合并的意义：
    模式二单步执行时，「三击」与「单击」之间要等用户按一次热键，
    延迟通常几百毫秒到几秒，第 4 次点击早已超出系统的连击判定窗口，
    永远不可能被识别成连击。所以只要有 N 次满足条件的连续点击，
    就整体合并成一步，模式二一次热键快速连发 N 次。
    """
    if not _match_press_release(events, start):
        return (start + 1, 0)
    first = events[start]
    if first.get("type") != "mouse_click" or first.get("action") != "press":
        return (start + 1, 0)
    button = str(first.get("button"))
    x, y = int(first.get("x") or 0), int(first.get("y") or 0)
    last_t = int(first.get("t") or 0)
    tolerance = max(0, int(options.click_tolerance_px))
    max_gap = max(1, int(options.multi_click_gap_ms))

    index = start + 2
    count = 1
    while index + 1 < len(events):
        nxt = events[index]
        if nxt.get(BOUNDARY_KEY):
            break
        if nxt.get("type") != "mouse_click" or nxt.get("action") != "press":
            break
        if str(nxt.get("button")) != button:
            break
        nx, ny = int(nxt.get("x") or 0), int(nxt.get("y") or 0)
        if abs(nx - x) > tolerance or abs(ny - y) > tolerance:
            break
        gap = int(nxt.get("t") or 0) - last_t
        if gap < 0 or gap >= max_gap:
            break
        if not _match_press_release(events, index):
            break
        last_t = int(nxt.get("t") or 0)
        count += 1
        index += 2

    if count < 2:
        return (start + 1, 0)
    return (index, count)


def build_segments(events: Sequence[dict[str, Any]],
                   options: Optional[MergeOptions] = None
                   ) -> list[tuple[int, int, str]]:
    """把事件序列切分成「操作单元」区间 ``[(start, end, tag), ...]``。

    ``tag`` 是单元类型提示（``""`` = 普通单元 / ``"combo"`` / ``"multi"``），
    :func:`build_steps` 据此生成合并后的步骤描述。

    分组规则（:func:`build_steps` 与 :func:`normalize_timestamps` 共用，
    保证「显示出来的步骤」与「时间戳重排的粒度」完全一致）：

    1. 连续的 ``mouse_move`` 合并成一步（取最后一个落点）；
    2. 修饰键 + 普通键的完整组合合并成一步（``options.combo`` 开启时）；
    3. 连续快速的同点同键点击合并成一步（``options.multi_click`` 开启时）；
    4. ``mouse_click`` / ``key`` 的「按下 + 同目标释放」合并成一步；
    5. 单独的释放、以及夹在其它事件之间的按下各自成步，保持真实时序；
    6. ``mouse_scroll`` 每条成一步；
    7. 带 :data:`BOUNDARY_KEY` 边界标记的事件永不与前一步合并
       （用户在编辑器里手工拆分过的步骤会被尊重）。
    """
    opts = options if options is not None else MergeOptions()
    evts = [e for e in events if isinstance(e, dict) and e.get("type")]
    segments: list[tuple[int, int, str]] = []
    index = 0
    total = len(evts)
    while index < total:
        evt = evts[index]
        etype = evt.get("type")
        end = index + 1
        tag = ""

        if etype == "mouse_move":
            end = _group_moves(evts, index)
        elif etype == "mouse_scroll":
            end = _group_scrolls(evts, index)
        elif etype == "key" and opts.combo:
            combo_end = _match_combo(evts, index)
            if combo_end is not None:
                end, tag = combo_end, "combo"
            elif _match_press_release(evts, index):
                end = index + 2
        elif etype == "mouse_click" and opts.multi_click:
            multi_end, count = _match_repeated_clicks(evts, index, opts)
            if count >= 2:
                end, tag = multi_end, "multi"
            elif _match_press_release(evts, index):
                end = index + 2
        elif etype in ("mouse_click", "key") and _match_press_release(evts, index):
            end = index + 2

        segments.append((index, end, tag))
        index = max(end, index + 1)
    return segments


def segment_bounds(events: Sequence[dict[str, Any]],
                   options: Optional[MergeOptions] = None) -> list[tuple[int, int]]:
    """只要区间的便捷封装（时间戳重排等场景使用）。"""
    return [(lo, hi) for lo, hi, _tag in build_segments(events, options)]


def _group_moves(events: Sequence[dict[str, Any]], start: int) -> int:
    """返回从 ``start`` 开始连续 mouse_move 事件区间的结束下标（不含）。

    遇到带步骤边界标记的事件立即停止，保证编辑后的独立步骤不会被误合并。
    """
    end = start + 1
    while (end < len(events) and events[end].get("type") == "mouse_move"
           and not events[end].get(BOUNDARY_KEY)):
        end += 1
    return end


#: 滚轮合并：指针位移阈值（像素），超过则不合并
SCROLL_MERGE_PIXEL = 20
#: 滚轮合并：最大时间间隔（毫秒），超过则不合并
SCROLL_MERGE_GAP_MS = 500


def _group_scrolls(events: Sequence[dict[str, Any]], start: int) -> int:
    """返回从 ``start`` 开始可合并的滚轮事件区间结束下标（不含）。

    合并条件：连续 ``mouse_scroll`` 事件、指针位移不超过
    :data:`SCROLL_MERGE_PIXEL` 像素、相邻间隔不超过
    :data:`SCROLL_MERGE_GAP_MS` 毫秒。
    """
    first = events[start]
    base_x = int(first.get("x") or 0)
    base_y = int(first.get("y") or 0)
    base_t = int(first.get("t") or 0)
    end = start + 1
    while end < len(events):
        evt = events[end]
        if evt.get("type") != "mouse_scroll":
            break
        if evt.get(BOUNDARY_KEY):
            break
        ex = int(evt.get("x") or 0)
        ey = int(evt.get("y") or 0)
        et = int(evt.get("t") or 0)
        if abs(ex - base_x) > SCROLL_MERGE_PIXEL or abs(ey - base_y) > SCROLL_MERGE_PIXEL:
            break
        if et - base_t > SCROLL_MERGE_GAP_MS:
            break
        end += 1
    return end


def _match_press_release(events: Sequence[dict[str, Any]], start: int) -> bool:
    """判断 ``start`` 处是否为「按下 + 紧随其后的同目标释放」。

    键盘看 ``key``，鼠标看 ``button``；中间不允许夹带其它事件，
    否则（例如 Shift 按住期间按 A）就各自成步，保持真实时序。
    """
    evt = events[start]
    nxt = start + 1
    if nxt >= len(events):
        return False
    if evt.get("action") != "press":
        return False
    nxt_evt = events[nxt]
    if nxt_evt.get(BOUNDARY_KEY) or nxt_evt.get("type") != evt.get("type"):
        return False
    if nxt_evt.get("action") != "release":
        return False
    if evt.get("type") == "key":
        return nxt_evt.get("key") == evt.get("key")
    if evt.get("type") == "mouse_click":
        return nxt_evt.get("button") == evt.get("button")
    return False


def describe_combo(events: Sequence[dict[str, Any]]) -> tuple[str, list[str], str]:
    """把组合键事件序列描述成 ``("组合键 Ctrl+V", ["Ctrl"], "V")``。"""
    pressed: list[str] = []
    target = ""
    for evt in events:
        if evt.get("type") != "key":
            continue
        if is_modifier_event(evt):
            if evt.get("action") == "press":
                name = modifier_name_of(evt) or modifier_name(_event_vk(evt))
                if name not in pressed:
                    pressed.append(name)
            continue
        if evt.get("action") == "press" and not target:
            target = key_display(evt)
    # 按规范顺序展示（Ctrl → Shift → Alt → Win），与编辑器勾选框一致
    mods = [name for name in MODIFIER_ORDER if name in pressed]
    mods += [name for name in pressed if name not in mods]
    return f"{COMBO_PREFIX} {'+'.join([*mods, target])}".rstrip("+ "), mods, target


def click_verb(count: int) -> str:
    """连击次数的中文说法：2=双击、3=三击、N=连击 N 次。"""
    return {1: "单击", 2: "双击", 3: "三击"}.get(int(count), f"连击{int(count)}次")


def describe_multi_click(events: Sequence[dict[str, Any]], count: int) -> str:
    """把连击事件序列描述成 ``"鼠标双击 (x, y)"`` / ``"鼠标连击5次 (x, y)"``。"""
    first = events[0] if events else {}
    button = str(first.get("button") or "left")
    x, y = first.get("x"), first.get("y")
    verb = click_verb(count)
    if button == "left":
        return f"鼠标{verb} ({x}, {y})"
    name = MOUSE_BUTTON_CN.get(button, button)
    return f"鼠标{name}{verb} ({x}, {y})"


def click_count_from_events(events: Sequence[dict[str, Any]]) -> int:
    """从点击事件序列里数出「按下」的次数（= 连击次数）。"""
    return sum(1 for e in events
               if isinstance(e, dict) and e.get("type") == "mouse_click"
               and e.get("action") == "press")


def build_steps(events: Iterable[dict[str, Any]],
                options: Optional[MergeOptions] = None) -> list[Step]:
    """把原始事件序列归并为模式二的步骤列表。

    一个步骤 = 一次完整的操作，模式二按一次热键就完整执行它：

    * ``鼠标移动``：连续移动合并为一步，取最后一个落点；
    * ``鼠标点击``：按下 + 释放合并为一步（``左键单击 / 右击 / 中键单击``）；
    * ``鼠标连击``：连续快速的同点同键点击合并为「鼠标双击 / 三击」一步；
    * ``键盘按键``：同一个键的按下 + 释放合并为一步（``键盘 A 按下并释放``）；
    * ``组合键``：修饰键 + 普通键合并为一步（``组合键 Ctrl+V``）；
    * ``鼠标滚轮``：一条成一步。

    合并后每个步骤内部仍保留原始事件与时间戳：
    模式一按原始时间戳逐条执行（原速），模式二在一步内按序快速执行完，
    所以既不会卡键，也不会因为间隔过大而让系统识别不出双击/组合键。

    通过 ``options`` 可以单独关闭组合键或连击合并
    （对应设置页的「自动合并组合键」「自动合并双击/三击」）。
    """
    opts = options if options is not None else MergeOptions()
    evts = [e for e in events if isinstance(e, dict) and e.get("type")]
    steps: list[Step] = []

    def add(kind: str, desc: str, lo: int, hi: int, **extra: Any) -> None:
        seg = evts[lo:hi]
        if not seg:
            return
        first = seg[0]
        steps.append(Step(
            index=len(steps) + 1,
            kind=kind,
            description=desc,
            events=list(seg),
            raw_start=lo,
            raw_end=hi,
            t_start=int(first.get("t") or 0),
            t_end=int(seg[-1].get("t") or 0),
            explicit_hold_ms=int(first.get("hold_ms") or 0),
            infinite_repeat=bool(first.get("infinite_repeat", False)),
            repeat_interval_ms=max(1, int(first.get("repeat_interval_ms") or 50)),
            interpolated=bool(first.get("interp", False)),
            **extra,
        ))

    for lo, hi, tag in build_segments(evts, opts):
        evt = evts[lo]
        etype = evt.get("type")
        paired = hi - lo == 2 and _match_press_release(evts, lo)

        if tag == "combo":
            desc, mods, target = describe_combo(evts[lo:hi])
            add("combo", desc, lo, hi, modifiers=mods, target_key=target)
            continue

        if tag == "multi":
            count = (hi - lo) // 2
            add("multi", describe_multi_click(evts[lo:hi], count), lo, hi,
                click_count=count)
            continue

        if etype == "mouse_move":
            last = evts[hi - 1]
            add("move", f"移动到 ({last.get('x')}, {last.get('y')})", lo, hi)
            continue

        if etype == "mouse_click":
            button = MOUSE_BUTTON_CN.get(str(evt.get("button")), str(evt.get("button")))
            if paired:
                verb = {"left": "单击", "right": "右击", "middle": "中键单击"}.get(
                    str(evt.get("button")), "点击")
                add("click", f"{button}{verb} ({evt.get('x')}, {evt.get('y')})", lo, hi)
            else:
                action = "按下" if evt.get("action") == "press" else "释放"
                add("click", f"{button}{action} ({evt.get('x')}, {evt.get('y')})", lo, hi)
            continue

        if etype == "mouse_scroll":
            # 合并区间内的所有滚轮事件，累计滚动量
            total_dy = 0
            total_dx = 0
            for se in evts[lo:hi]:
                total_dy += int(se.get("dy") or 0)
                total_dx += int(se.get("dx") or 0)
            direction = ("上滚" if total_dy > 0
                         else ("下滚" if total_dy < 0
                               else ("右滚" if total_dx > 0 else "左滚")))
            amount = abs(total_dy) if total_dy != 0 else abs(total_dx)
            last_scroll = evts[hi - 1]
            add("scroll",
                f"滚轮{direction} {amount} 格 @({last_scroll.get('x')}, {last_scroll.get('y')})",
                lo, hi)
            continue

        if etype == "key":
            name = key_display(evt)
            if paired:
                add("key", f"键盘 {name} 按下并释放", lo, hi)
            else:
                action = "按下" if evt.get("action") == "press" else "释放"
                add("key", f"键盘 {name} {action}", lo, hi)
            continue

        # 未知类型：按原始事件兜底成一步，保证不丢事件
        add(str(etype), describe(evt), lo, hi)

    return steps


# ---------------------------------------------------------------------------
# 步骤编辑：增 / 删 / 移 + 时间戳重排
# ---------------------------------------------------------------------------
#: 插入或移动步骤后，两步之间的最小间隔（毫秒）。避免出现 0ms 的“同一时刻”事件。
MIN_STEP_GAP_MS = 20


class StepEditError(Exception):
    """步骤编辑不合法（例如索引越界）。"""


def step_signature(step: Step) -> tuple:
    """步骤的“身份签名”。

    编辑（上移/下移/删除）只能改变步骤的位置，不会改变内容，
    因此用 ``(kind, 事件序列)`` 作为签名，可以在重排后重新定位原来的选择。
    """
    return (step.kind, tuple(
        (e.get("type"), e.get("action"), e.get("key"), e.get("vk"),
         e.get("button"), e.get("x"), e.get("y"), e.get("dx"), e.get("dy"))
        for e in step.events
    ))


def _auto_group(events: Sequence[dict[str, Any]], options: MergeOptions
                ) -> tuple[int, str]:
    """返回 ``events`` 若交给自动合并逻辑处理会得到的区间与类型。

    用于判断「这个步骤是否恰好就是自动合并的产物」。
    """
    if not events:
        return 0, ""
    segments = build_segments(events, options)
    return segments[0][1], segments[0][2]


def steps_to_events(steps: Sequence[Step],
                    options: Optional[MergeOptions] = None) -> list[dict[str, Any]]:
    """把步骤列表摊平回原始事件序列（深拷贝）并标注步骤边界。

    边界标记 :data:`BOUNDARY_KEY` 的规则
    ------------------------------------
    标记的目的是「挡住合并逻辑，保住当前粒度」，但标记写多了会把
    自动合并的结果锁死（关掉开关也拆不开）。因此：

    * **步骤本身就是自动合并的产物**（组合键 / 双击三击 / 按下+释放 /
      连续移动）：只在步骤首事件标一个边界，内部不标。
      这样关掉开关后重新分组即可还原成拆分形态；
    * **用户手工拆出来的步骤**（内部含多个自动分组）：内部每条事件都标边界，
      任何模式下都不会被合并回去，编辑结果被尊重。
    """
    opts = options if options is not None else MergeOptions()
    out: list[dict[str, Any]] = []
    for step in steps:
        events = [dict(e) for e in step.events]
        if not events:
            continue
        for item in events:
            item.pop(BOUNDARY_KEY, None)

        group_hi, _tag = _auto_group(events, opts)
        has_internal_boundary = group_hi < len(events)

        if has_internal_boundary:
            # 手工拆分：每条事件都挡住合并
            for item in events:
                item[BOUNDARY_KEY] = True
        else:
            events[0][BOUNDARY_KEY] = True
        out.extend(events)
    return out


def step_gaps(steps: Sequence[Step]) -> list[int]:
    """返回每一步「与前一步之间的空白间隔」（毫秒）。

    数据结构约定（本版本的时间模型）
    --------------------------------
    * **动作时长**：``step.t_end - step.t_start``，即步骤自身占用的时间；
    * **空白间隔**：``本步 t_start - 上一步 t_end``，不写进事件数据，
      由时间戳派生（间隔被完整保留时它才等于录制时的原始节奏）。

    返回列表与 ``steps`` 等长；第 0 项是该步骤相对时间轴起点（0）的偏移。
    """
    gaps: list[int] = []
    previous_end = 0
    for position, step in enumerate(steps):
        start = int(step.t_start)
        gaps.append(max(0, start - (previous_end if position else 0)))
        previous_end = int(step.t_end)
    return gaps


def layout_with_gaps(events: Sequence[dict[str, Any]],
                     gaps: Sequence[int]) -> list[dict[str, Any]]:
    """按给定的步间间隔把各步骤重新铺到时间轴上。

    步骤**内部**事件的相对偏移保持不变（按住时长、组合键节奏不破坏），
    只有步骤之间的空白被替换成 ``gaps`` 里的值。用于「编辑单步时保留
    前后间隔」——避免把录制节奏压成固定的 20ms。
    """
    result = [dict(e) for e in events]
    if not result:
        return result
    segments = segment_bounds(result)
    cursor = 0
    for position, (lo, hi) in enumerate(segments):
        gap = int(gaps[position]) if position < len(gaps) else int(MIN_STEP_GAP_MS)
        cursor += max(0, gap)
        origin = int(result[lo].get("t") or 0)
        for index in range(lo, hi):
            result[index]["t"] = cursor + (int(result[index].get("t") or 0) - origin)
        cursor = int(result[hi - 1].get("t") or cursor)
    return result


def normalize_timestamps(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """重排事件时间戳：段内保持相对偏移，段间统一为固定的步间隔。

    处理方案（编辑步骤后如何维护时间戳）
    ------------------------------------
    模式一依赖时间戳原速回放，而编辑会打乱时间轴，所以采用「按步骤重排」：

    * **基准**：整段序列从 0 开始（取全局最小值兜底，兼容异常数据）。
    * **步骤内部**：同一步骤内的事件保持原始相对偏移。例如合并的移动帧、
      点击的按下→释放间隔、连续按键的节奏，都不会被破坏。
    * **步骤之间**：统一使用 :data:`MIN_STEP_GAP_MS`（默认 20ms）。
      这样“删除步骤会压缩总时长、插入步骤会顶开后续步骤、纯重排不会
      把时间轴越拉越长”，行为可预期且与编辑操作一一对应。
    * **幂等**：对已规整的数据再次调用不会产生漂移。

    用户仍可在回放时用速度倍率调整整体快慢（见配置 ``player.speed``）。
    """
    events = [dict(e) for e in events]
    if not events:
        return events

    base = min(0, *(int(e.get("t") or 0) for e in events))

    # 与 build_steps 使用同一套切分规则，保证列表里看到的步骤
    # 就是这里时间戳重排的粒度单位。
    # 注意：这里用默认合并选项即可 —— 用户手工拆分过的步骤带
    # BOUNDARY_KEY 边界标记，任何模式下都不会被跨越合并。
    segments = segment_bounds(events)

    gap = max(1, int(MIN_STEP_GAP_MS))
    last_end = base
    for position, (lo, hi) in enumerate(segments):
        raw_start = int(events[lo].get("t") or 0)
        new_start = base if position == 0 else last_end + gap
        for offset_index in range(lo, hi):
            offset = int(events[offset_index].get("t") or 0) - raw_start
            events[offset_index]["t"] = new_start + offset
        last_end = int(events[hi - 1].get("t") or new_start)

    return events


def delete_steps(steps: Sequence[Step], indices: Sequence[int]) -> list[dict[str, Any]]:
    """删除指定步骤，返回重排时间戳后的事件序列。

    步骤边界（:data:`BOUNDARY_KEY`）会被保留：删掉中间某步之后，
    剩下的两个步骤仍然是两步，不会被合并逻辑重新并成一步。
    """
    doomed = {i for i in indices if 0 <= i < len(steps)}
    if not doomed:
        raise StepEditError("没有需要删除的步骤")
    kept = [step for i, step in enumerate(steps) if i not in doomed]
    return _renormalize(steps_to_events(kept))


def move_steps(steps: Sequence[Step], indices: Sequence[int], delta: int) -> tuple[list[dict], dict[int, int]]:
    """把选中的步骤整体上移/下移。

    返回 ``(重排后的事件序列, 新旧下标映射)``：映射的键是编辑前的步骤下标，
    值是该步骤编辑后的新下标，界面据此恢复复选框选中状态。
    """
    steps = list(steps)
    count = len(steps)
    selected = sorted({i for i in indices if 0 <= i < count})
    if not selected:
        raise StepEditError("请先勾选要移动的步骤")
    if delta == 0:
        raise StepEditError("移动方向无效")

    if delta < 0:
        if selected[0] == 0:
            raise StepEditError("已经是第一步，无法上移")
        # 自下而上逐个交换，保证多选整体上移一格
        order = selected
        for i in order:
            steps[i - 1], steps[i] = steps[i], steps[i - 1]
    else:
        if selected[-1] == count - 1:
            raise StepEditError("已经是最后一步，无法下移")
        # 自上而下逐个交换
        order = list(reversed(selected))
        for i in order:
            steps[i + 1], steps[i] = steps[i], steps[i + 1]

    mapping = {old: old + delta for old in selected}
    return _renormalize(steps_to_events(steps)), mapping


def make_step_events(kind: str, *, x: int = 0, y: int = 0, button: str = "left",
                     key: str = "vk:65", vk: Optional[int] = None,
                     dx: int = 0, dy: int = -1,
                     hold_ms: int = 40, repeat: int = 1,
                     modifiers: Optional[Sequence[str]] = None) -> tuple[list[dict[str, Any]], str]:
    """构造一个“新步骤”的原始事件（时间戳从 0 开始，稍后由重排统一分配）。

    返回 ``(事件列表, 中文描述)``。

    * ``repeat``：点击/按键重复次数。``kind="click"`` 且 ``repeat>=2`` 时
      会带节点击间隔（默认 60ms），这样 :func:`build_steps` 能识别成
      「鼠标双击 / 三击」。
    * ``modifiers``：``kind="combo"`` 时的修饰键名列表（``["Ctrl", "Shift"]``）。
    """
    kind = (kind or "").strip()
    hold_ms = max(1, int(hold_ms))
    repeat = max(1, min(50, int(repeat)))

    if kind == "move":
        return [make_move_event(0, x, y)], f"移动到 ({x}, {y})"

    if kind == "click":
        button_cn = MOUSE_BUTTON_CN.get(button, button)
        events: list[dict[str, Any]] = []
        for i in range(repeat):
            base = i * _CLICK_REPEAT_GAP_MS
            events.append(make_click_event(base, "press", button, x, y))
            events.append(make_click_event(base + hold_ms, "release", button, x, y))
        if repeat >= 2:
            verb = click_verb(repeat)
            desc = (f"鼠标{verb} ({x}, {y})" if button == "left"
                    else f"鼠标{button_cn}{verb} ({x}, {y})")
            return events, desc
        verb = {"left": "单击", "right": "右击", "middle": "中键单击"}.get(button, "点击")
        return events, f"{button_cn}{verb} ({x}, {y})"

    if kind == "scroll":
        direction = "上滚" if dy > 0 else ("下滚" if dy < 0 else "横向滚动")
        amount = abs(dy) or abs(dx)
        suffix = f" ×{repeat}" if repeat > 1 else ""
        return ([make_scroll_event(0, dx * repeat, dy * repeat, x, y)],
                f"滚轮{direction} {amount * repeat} 格 @({x}, {y}){suffix}")

    if kind == "key":
        events = []
        for i in range(repeat):
            base = i * (hold_ms * 2)
            events.append(make_key_event(base, "press", key, vk))
            events.append(make_key_event(base + hold_ms, "release", key, vk))
        name = key_display({"key": key, "vk": vk})
        suffix = f" ×{repeat}" if repeat > 1 else ""
        return events, f"键盘 {name} 按下并释放{suffix}"

    if kind == "combo":
        mods = _normalize_modifier_names(modifiers or ["Ctrl"])
        events = []
        t = 0
        for name in mods:
            mod_vk = _modifier_vk(name)
            events.append(make_key_event(t, "press", f"vk:{mod_vk}", mod_vk))
            t += combo_gap_ms()
        events.append(make_key_event(t, "press", key, vk))
        t += hold_ms
        events.append(make_key_event(t, "release", key, vk))
        t += combo_gap_ms()
        for name in reversed(mods):
            mod_vk = _modifier_vk(name)
            events.append(make_key_event(t, "release", f"vk:{mod_vk}", mod_vk))
            t += combo_gap_ms()
        target = key_display({"key": key, "vk": vk})
        return events, f"{COMBO_PREFIX} {'+'.join([*mods, target])}"

    raise StepEditError(f"不支持的步骤类型：{kind}")


def describe_click_repeat(button: str, count: int, x: int, y: int) -> str:
    """连击次数的中文描述（供插入/编辑对话框预览使用）。"""
    name = MOUSE_BUTTON_CN.get(button, button)
    if count >= 2:
        verb = click_verb(count)
        return (f"鼠标{verb} ({x}, {y})" if button == "left"
                else f"鼠标{name}{verb} ({x}, {y})")
    verb = {"left": "单击", "right": "右击", "middle": "中键单击"}.get(button, "点击")
    return f"{name}{verb} ({x}, {y})"


#: 连击（双击/三击）时两次按下之间的默认间隔（毫秒）。
#: 必须明显小于 :data:`MULTI_CLICK_MAX_GAP_MS`，系统才会识别为连击。
_CLICK_REPEAT_GAP_MS = 90


def combo_gap_ms() -> int:
    """组合键内部相邻事件之间的间隔（毫秒）。

    太小容易被某些程序当作同时按下而忽略修饰键，太大又可能被判定为
    「先按 Ctrl 再按 V」；10ms 在实测中最稳妥。
    """
    return 10


def _modifier_vk(name: str) -> int:
    """修饰键名 -> 左侧键的虚拟键码（Ctrl -> 0x11）。"""
    aliases = MODIFIER_VK_ALIASES.get(name)
    if aliases:
        return aliases[0]
    raise StepEditError(f"不支持的修饰键：{name}")


def _normalize_modifier_names(names: Sequence[str]) -> list[str]:
    """归一化修饰键名并去重（``ctrl`` -> ``Ctrl``），按规范顺序排列。

    顺序统一为 Ctrl → Shift → Alt → Win，这样「组合键 Shift+Ctrl+S」和
    「组合键 Ctrl+Shift+S」是同一种写法，界面与描述不会前后不一致。
    """
    seen: set[str] = set()
    for raw in names:
        text = str(raw or "").strip().lower()
        name = {
            "ctrl": "Ctrl", "control": "Ctrl", "ctl": "Ctrl",
            "shift": "Shift", "alt": "Alt", "win": "Win",
            "meta": "Win", "cmd": "Win", "super": "Win",
        }.get(text)
        if name is None:
            raise StepEditError(f"不支持的修饰键：{raw}")
        seen.add(name)
    if not seen:
        raise StepEditError("组合键至少要有一个修饰键")
    return [name for name in MODIFIER_ORDER if name in seen]


#: 修饰键的规范顺序（界面勾选框与「组合键 Ctrl+Shift+S」文本都按它排列）
MODIFIER_ORDER: tuple[str, ...] = ("Ctrl", "Shift", "Alt", "Win")


def all_modifier_names() -> list[str]:
    """全部可用的修饰键名，按 :data:`MODIFIER_ORDER` 的规范顺序返回。"""
    return [name for name in MODIFIER_ORDER if name in MODIFIER_VK_ALIASES]


def rebuild_step_events(step: Step, *, x: Optional[int] = None,
                        y: Optional[int] = None, button: Optional[str] = None,
                        click_count: Optional[int] = None,
                        key: Optional[str] = None, vk: Optional[int] = None,
                        modifiers: Optional[Sequence[str]] = None,
                        dx: Optional[int] = None, dy: Optional[int] = None,
                        explicit_hold_ms: Optional[int] = None,
                        infinite_repeat: Optional[bool] = None,
                        repeat_interval_ms: Optional[int] = None,
                        start_x: Optional[int] = None,
                        start_y: Optional[int] = None,
                        duration_ms: Optional[int] = None,
                        ) -> list[dict[str, Any]]:
    """按“只改指定字段”的语义重建一个步骤的事件序列。

    编辑器用它实现「动作与坐标分离编辑」：

    * 只改 ``x``/``y`` —— 动作与按键都不动，仅平移坐标；
    * 只改 ``button``/``click_count`` —— 重建点击序列（单击/双击/三击/左中右键）；
    * 只改 ``key`` —— 替换按键标识，**按下/释放的配对与时间偏移保持不变**；
    * 只改 ``modifiers`` —— 重建组合键的修饰键列表。

    时间偏移沿用原步骤内部节奏（原始 ``t`` 差值），因此不会破坏时间戳逻辑。

    ``duration_ms``：持续时长（毫秒）。提供且 >0 时，重建完成后把该步骤
    内部的时间跨度规整为该值（段内等比缩放；零跨度时把末事件挪到
    ``duration_ms``），持续时长随事件时间戳写入 JSON。
    """
    events = _rebuild_step_events(
        step, x=x, y=y, button=button, click_count=click_count,
        key=key, vk=vk, modifiers=modifiers, dx=dx, dy=dy,
        explicit_hold_ms=explicit_hold_ms,
        infinite_repeat=infinite_repeat,
        repeat_interval_ms=repeat_interval_ms,
        start_x=start_x, start_y=start_y)
    if duration_ms is not None and int(duration_ms) > 0:
        span = int(duration_ms)
        events = _apply_step_span(events, span)
        # 插入/单帧的移动步骤（只有起止两帧）：自动补中间帧，回放才有平滑轨迹；
        # 已有原始多帧的录制步骤不重复插值，保持原有事件。
        has_start = (step.kind == "move"
                     and (start_x is not None or start_y is not None))
        if step.kind == "move" and has_start and len(step.events) == 1:
            events = _interpolate_move_frames(events, span)
    return events


def _apply_step_span(events: list[dict[str, Any]], span_ms: int
                     ) -> list[dict[str, Any]]:
    """把一段事件的时间跨度规整为 ``span_ms``（插入新步骤时写入持续时长）。

    * 段内已有跨度：按 ``新跨度 / 旧跨度`` 等比缩放各事件的相对偏移，
      与 :func:`set_step_duration` 的段内缩放语义一致；
    * 零跨度（单事件或同刻事件）：把末事件的 ``t`` 挪到 ``首事件t + span_ms``，
      使该步骤在时间轴上占据设定时长（插入的单事件移动/滚轮即
      “经过设定时长后执行”，列表时间列也不再是 0.000s）。
    """
    if not events or span_ms <= 0:
        return events
    first_t = int(events[0].get("t") or 0)
    last_t = int(events[-1].get("t") or 0)
    old_span = max(0, last_t - first_t)
    if old_span == span_ms:
        return events
    if old_span > 0:
        factor = span_ms / old_span
        for evt in events:
            offset = int(evt.get("t") or 0) - first_t
            evt["t"] = first_t + int(round(offset * factor))
        return events
    out = [dict(e) for e in events]
    out[-1]["t"] = first_t + span_ms
    return out


#: 插值移动补帧：目标帧间隔（毫秒），10ms ≈ 100fps，肉眼平滑
_MOVE_INTERP_GAP_MS = 10
#: 插值移动补帧：中间帧数上限（超过自动拉大帧距，避免事件过多）
_MOVE_INTERP_MAX_FRAMES = 200


def _interpolate_move_frames(events: list[dict[str, Any]], span_ms: int
                             ) -> list[dict[str, Any]]:
    """给「起止两帧」的移动步骤线性插值补中间帧（平滑轨迹）。

    * 帧距目标 :data:`_MOVE_INTERP_GAP_MS`；帧数将达到
      :data:`_MOVE_INTERP_MAX_FRAMES` 时自动拉大帧距；
    * 起止坐标相同或只有起止两帧之外的形态时不补帧；
    * 生成的帧与录制产生的 ``mouse_move`` 事件结构完全一致，
      模式一按时间戳原速执行即呈现平滑移动；
    * 首事件带 ``interp`` 标记（随 JSON 持久化），模式二据此逐帧快速执行。
    """
    if len(events) != 2 or span_ms <= 0:
        return events
    first_t = int(events[0].get("t") or 0)
    sx, sy = int(events[0].get("x") or 0), int(events[0].get("y") or 0)
    ex, ey = int(events[1].get("x") or 0), int(events[1].get("y") or 0)
    if (sx, sy) == (ex, ey):
        return events                       # 起止相同：不补帧
    gap = max(_MOVE_INTERP_GAP_MS,
              -(-int(span_ms) // _MOVE_INTERP_MAX_FRAMES))   # ceil 除法
    out = [dict(events[0])]                 # 起始帧（t = first_t）
    t = gap
    while t < span_ms:
        ratio = t / span_ms
        out.append(make_move_event(
            first_t + t,
            int(round(sx + (ex - sx) * ratio)),
            int(round(sy + (ey - sy) * ratio)),
        ))
        t += gap
    out.append(dict(events[1]))             # 目标帧（t = first_t + span_ms）
    out[0]["interp"] = True
    return out


def _rebuild_move_path(events: list[dict[str, Any]],
                       start: tuple[int, int],
                       end: tuple[int, int]) -> list[dict[str, Any]]:
    """重建**多帧**移动步骤的坐标轨迹，帧数与时间戳原样保留。

    三条分支（顺序很重要，避免再次把轨迹压平）：

    * **起止点都没改** → 中间帧原样保留：用户只改时长/其他字段时，
      录制下来的真实轨迹（含曲线、抖动）必须完整保住；
    * **中间帧已被压成端点**（历史 Bug 留下的「无轨迹」数据：所有中间帧
      等于终点）→ 按时间比例在起止点之间线性插值补齐，恢复平滑移动；
    * **用户改了起止点** → 按原有相对位置做仿射映射，尽量保留轨迹形状。
    """
    n = len(events)
    out = [dict(e) for e in events]
    sx, sy = int(start[0]), int(start[1])
    ex, ey = int(end[0]), int(end[1])
    out[0]["x"], out[0]["y"] = sx, sy
    out[-1]["x"], out[-1]["y"] = ex, ey
    if n <= 2:
        return out

    orig_start = (int(events[0].get("x") or 0), int(events[0].get("y") or 0))
    orig_end = (int(events[-1].get("x") or 0), int(events[-1].get("y") or 0))
    t0 = int(events[0].get("t") or 0)
    span = int(events[-1].get("t") or 0) - t0
    middles = [(int(e.get("x") or 0), int(e.get("y") or 0)) for e in events[1:-1]]
    # 中间帧全部等于终点 = 没有轨迹（旧 Bug 写坏的数据）。必须放在
    # 「起止点未改动」判断之前，否则这种坏数据会被当成「轨迹没改」直接返回。
    collapsed = bool(middles) and all(pt == orig_end for pt in middles)
    if collapsed and span > 0:
        for index in range(1, n - 1):
            ratio = (int(events[index].get("t") or 0) - t0) / span
            out[index]["x"] = int(round(sx + (ex - sx) * ratio))
            out[index]["y"] = int(round(sy + (ey - sy) * ratio))
        return out

    # 起止点未改动：直接返回，中间帧（真实轨迹）一条都不动
    if orig_start == (sx, sy) and orig_end == (ex, ey):
        return out

    # 起止点被改动：按原相对位置做仿射映射，保留轨迹形状
    dx_old = orig_end[0] - orig_start[0]
    dy_old = orig_end[1] - orig_start[1]
    for index in range(1, n - 1):
        px = int(events[index].get("x") or 0)
        py = int(events[index].get("y") or 0)
        ux = (px - orig_start[0]) / dx_old if dx_old else 0.0
        uy = (py - orig_start[1]) / dy_old if dy_old else 0.0
        out[index]["x"] = int(round(sx + ux * (ex - sx)))
        out[index]["y"] = int(round(sy + uy * (ey - sy)))
    return out


def _rebuild_step_events(step: Step, *, x: Optional[int] = None,
                         y: Optional[int] = None, button: Optional[str] = None,
                         click_count: Optional[int] = None,
                         key: Optional[str] = None, vk: Optional[int] = None,
                         modifiers: Optional[Sequence[str]] = None,
                         dx: Optional[int] = None, dy: Optional[int] = None,
                         explicit_hold_ms: Optional[int] = None,
                         infinite_repeat: Optional[bool] = None,
                         repeat_interval_ms: Optional[int] = None,
                         start_x: Optional[int] = None,
                         start_y: Optional[int] = None,
                         ) -> list[dict[str, Any]]:
    """按“只改指定字段”的语义重建事件序列（主体实现，见 rebuild_step_events）。"""
    events = step.events
    if not events:
        raise StepEditError("该步骤没有可编辑的事件")

    kind = step.kind

    # ---------------- 鼠标移动 / 滚轮：只改坐标（滚轮还可改格数） ----------------
    if kind in ("move", "scroll"):
        n = len(events)
        has_start = (kind == "move"
                     and (start_x is not None or start_y is not None))
        if kind == "move" and n > 1:
            # 多帧移动：走轨迹重建，绝不能把中间帧坐标统一改成终点
            # （旧实现这么干，回放时指针会一步跳到目标点）
            start_xy = (int(start_x) if start_x is not None
                        else int(events[0].get("x") or 0),
                        int(start_y) if start_y is not None
                        else int(events[0].get("y") or 0))
            end_xy = (int(x) if x is not None else int(events[-1].get("x") or 0),
                      int(y) if y is not None else int(events[-1].get("y") or 0))
            out = _rebuild_move_path(events, start_xy, end_xy)
        else:
            # 滚轮 / 单帧移动：坐标只有一个点，直接写入目标值
            out = []
            for evt in events:
                item = dict(evt)
                if x is not None:
                    item["x"] = int(x)
                if y is not None:
                    item["y"] = int(y)
                if kind == "scroll":
                    if dx is not None:
                        item["dx"] = int(dx)
                    if dy is not None:
                        item["dy"] = int(dy)
                out.append(item)
        # move 且只有一条事件但用户指定了起始坐标 → 补一条起始事件
        if has_start and n == 1:
            first = dict(events[0])
            if start_x is not None:
                first["x"] = int(start_x)
            if start_y is not None:
                first["y"] = int(start_y)
            first["t"] = 0
            out.insert(0, first)
            out[1]["t"] = int(out[1].get("t") or 0) or int(events[0].get("t") or 0)
        return _write_step_meta(
            out,
            hold_ms=explicit_hold_ms,
            infinite_repeat=infinite_repeat,
            repeat_interval_ms=repeat_interval_ms,
        )

    # ---------------- 鼠标点击 / 连击：动作（键 + 点击次数）+ 坐标 ----------------
    if kind in ("click", "multi"):
        first = events[0]
        new_button = str(button or first.get("button") or "left")
        new_x = int(x) if x is not None else int(first.get("x") or 0)
        new_y = int(y) if y is not None else int(first.get("y") or 0)
        if click_count is not None:
            # 上限放宽到 50：连击次数不设实际限制，只防手滑输入天文数字
            count = max(1, min(MAX_CLICK_COUNT, int(click_count)))
        else:
            count = step.click_count or click_count_from_events(events) or 1

        # 保留原来的按下时长与连击间隔节奏
        pairs = _click_pairs(events)
        hold = 40
        gap = _CLICK_REPEAT_GAP_MS
        if pairs:
            hold = max(1, int(pairs[0][1].get("t") or 0) - int(pairs[0][0].get("t") or 0))
        if len(pairs) >= 2:
            gap = max(1, int(pairs[1][0].get("t") or 0) - int(pairs[0][0].get("t") or 0))
        gap = max(hold + 1, gap)

        out = []
        for i in range(count):
            base = i * gap
            out.append(make_click_event(base, "press", new_button, new_x, new_y))
            out.append(make_click_event(base + hold, "release", new_button, new_x, new_y))
        return _write_step_meta(
            out,
            hold_ms=explicit_hold_ms,
            infinite_repeat=infinite_repeat,
            repeat_interval_ms=repeat_interval_ms,
        )

    # ---------------- 键盘按键：只换按键标识，配对与节奏不变 ----------------
    if kind == "key":
        out = []
        for evt in events:
            item = dict(evt)
            if key is not None:
                item["key"] = str(key)
            if vk is not None:
                item["vk"] = int(vk)
            elif key is not None and not str(key).startswith("vk:"):
                item["vk"] = None
            out.append(item)
        return _write_step_meta(
            out,
            hold_ms=explicit_hold_ms,
            infinite_repeat=infinite_repeat,
            repeat_interval_ms=repeat_interval_ms,
        )

    # ---------------- 组合键：改修饰键列表与/或目标键 ----------------
    if kind == "combo":
        mods = _normalize_modifier_names(
            modifiers if modifiers is not None else (step.modifiers or ["Ctrl"])
        )
        target_evt = _combo_target_event(events)
        new_key = str(key) if key is not None else str(
            (target_evt or {}).get("key") or "vk:86"
        )
        new_vk = int(vk) if vk is not None else (target_evt or {}).get("vk")
        if vk is None and key is not None and not new_key.startswith("vk:"):
            new_vk = None

        # 沿用原来的时间节奏：修饰按下间隔、按住时长、释放间隔
        mod_gap = combo_gap_ms()
        hold = 40
        if len(events) >= 2:
            hold = max(1, int(events[-2].get("t") or 0) - int(events[-3].get("t") or 0)) \
                if len(events) >= 3 else 40

        out = []
        t = 0
        for name in mods:
            mod_vk = _modifier_vk(name)
            out.append(make_key_event(t, "press", f"vk:{mod_vk}", mod_vk))
            t += mod_gap
        out.append(make_key_event(t, "press", new_key, new_vk))
        t += hold
        out.append(make_key_event(t, "release", new_key, new_vk))
        t += mod_gap
        for name in reversed(mods):
            mod_vk = _modifier_vk(name)
            out.append(make_key_event(t, "release", f"vk:{mod_vk}", mod_vk))
            t += mod_gap
        return _write_step_meta(
            out,
            hold_ms=explicit_hold_ms,
            infinite_repeat=infinite_repeat,
            repeat_interval_ms=repeat_interval_ms,
        )

    raise StepEditError(f"暂不支持编辑「{step.kind_label}」类型的步骤")


def _write_step_meta(events: list[dict[str, Any]],
                     hold_ms: Optional[int] = None,
                     infinite_repeat: Optional[bool] = None,
                     repeat_interval_ms: Optional[int] = None,
                     ) -> list[dict[str, Any]]:
    """把步骤级元数据写入首事件（用于 JSON 持久化）。

    只写入非 ``None`` 的字段；值为 0 / False 的也写入（表示关闭该特性）。
    """
    if not events:
        return events
    first = events[0]
    if hold_ms is not None:
        first["hold_ms"] = max(0, int(hold_ms))
    if infinite_repeat is not None:
        first["infinite_repeat"] = bool(infinite_repeat)
    if repeat_interval_ms is not None:
        first["repeat_interval_ms"] = max(1, int(repeat_interval_ms))
    return events


def _click_pairs(events: Sequence[dict[str, Any]]) -> list[tuple[dict, dict]]:
    """把点击事件序列切成 ``[(按下事件, 释放事件), ...]``。"""
    pairs: list[tuple[dict, dict]] = []
    pending: Optional[dict] = None
    for evt in events:
        if evt.get("type") != "mouse_click":
            continue
        if evt.get("action") == "press":
            pending = evt
        elif pending is not None:
            pairs.append((pending, evt))
            pending = None
    return pairs


def _combo_target_event(events: Sequence[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """取出组合键里的目标键按下事件（跳过修饰键）。"""
    for evt in events:
        if evt.get("type") != "key" or evt.get("action") != "press":
            continue
        vk = _event_vk(evt)
        if vk in MODIFIER_VK_SET:
            continue
        return evt
    return None


def insert_step(steps: Sequence[Step], position: int, events: Sequence[dict[str, Any]],
                description: str = "") -> tuple[list[dict], int]:
    """在 ``position`` 处插入一个步骤。

    ``position`` 为 0..len(steps)：等于 len(steps) 时追加到末尾。
    返回 ``(重排后的事件序列, 新步骤下标)``。
    """
    steps = list(steps)
    count = len(steps)
    position = max(0, min(int(position), count))
    new_events = [dict(e) for e in events]
    if not new_events:
        raise StepEditError("新步骤没有任何事件")

    flat: list[dict[str, Any]] = []
    for i, step in enumerate(steps):
        if i == position:
            # 插到第 i 步之前：时间戳设为“上一步结束 + 最小间隔”，
            # 让规整逻辑算出一个合理的空档，同时顶开后续步骤。
            probe = _place_new_events(new_events, steps[i - 1].t_end if i > 0 else 0)
            flat.extend(probe)
        flat.extend(dict(e) for e in step.events)
    if position >= count:
        base = int(steps[-1].t_end) if steps else 0
        flat.extend(_place_new_events(new_events, base))

    normalized = normalize_timestamps(flat)
    # 新步骤在重排后的下标就是 position
    return normalized, position


def _place_new_events(events: Sequence[dict[str, Any]], base_t: int) -> list[dict[str, Any]]:
    """给新步骤的事件套上一个起始时间（保持它们之间的相对偏移）。"""
    out: list[dict[str, Any]] = []
    for event in events:
        item = dict(event)
        item["t"] = int(base_t) + int(event.get("t") or 0)
        out.append(item)
    return out


def replace_step(steps: Sequence[Step], index: int,
                 events: Sequence[dict[str, Any]],
                 kind: Optional[str] = None,
                 preserve_gaps: bool = True) -> list[dict[str, Any]]:
    """用新的事件替换第 ``index`` 步（编辑现有步骤参数）。

    ``kind`` 留空时沿用原步骤类型；显式传入时用于「单击 → 双击」这类
    类型变化的编辑。

    ``preserve_gaps=True``（默认）时**保留原有的步间空白间隔**，只替换
    这一步的内容 —— 这是修复「改一步时长把后续间隔全压成 20ms」的关键：
    其余步骤之间的录制节奏原封不动地保留在时间轴上。
    传 ``False`` 则退回旧的「全部重排成固定 20ms 间隔」行为。
    """
    steps = list(steps)
    if index < 0 or index >= len(steps):
        raise StepEditError("步骤下标越界")
    old_gaps = step_gaps(steps) if preserve_gaps else None
    old = steps[index]
    steps[index] = Step(
        index=index + 1,
        kind=kind or old.kind,
        description=old.description,
        events=[dict(e) for e in events],
        raw_start=0, raw_end=len(events),
    )
    return _renormalize(steps_to_events(steps), old_gaps)


def split_click_step(step: Step) -> list[list[dict[str, Any]]]:
    """把一个「连击 N 次」步骤拆成 N 个独立的「单击」步骤事件序列。

    返回 ``N`` 份事件列表（每份两条：按下 + 释放），供 :func:`replace_steps`
    写回并打上边界标记 —— 拆分后的步骤**不会**被自动合并重新粘回去，
    这正是设置页提到的「尊重用户编辑」。

    单击步骤（N == 1）原样返回一份。
    """
    pairs = _click_pairs(step.events)
    if len(pairs) <= 1:
        return [list(step.events)] if step.events else []

    out: list[list[dict[str, Any]]] = []
    hold_base = 0
    for press, release in pairs:
        hold = max(1, int(release.get("t") or 0) - int(press.get("t") or 0))
        out.append([
            make_click_event(hold_base, "press", str(press.get("button") or "left"),
                             int(press.get("x") or 0), int(press.get("y") or 0)),
            make_click_event(hold_base + hold, "release",
                             str(release.get("button") or "left"),
                             int(release.get("x") or 0), int(release.get("y") or 0)),
        ])
        hold_base += hold
    return out


def replace_step_with_many(steps: Sequence[Step], index: int,
                           groups: Sequence[Sequence[dict[str, Any]]],
                           kind: Optional[str] = None) -> list[dict[str, Any]]:
    """把第 ``index`` 步替换成**多个**步骤（用于「拆分为独立单击」）。

    每个分组会成为独立一步，并在首事件打上边界标记，
    重排时间戳后仍然保持拆分状态（不会被自动合并重新粘回去）。
    """
    steps = list(steps)
    if index < 0 or index >= len(steps):
        raise StepEditError("步骤下标越界")
    groups = [list(g) for g in groups if g]
    if not groups:
        raise StepEditError("拆分后没有任何事件")

    new_steps: list[Step] = []
    for position, step in enumerate(steps):
        if position != index:
            new_steps.append(step)
            continue
        for sub in groups:
            new_steps.append(Step(
                index=len(new_steps) + 1,
                kind=kind or _kind_for_events(sub),
                description="",
                events=[dict(e) for e in sub],
                raw_start=0,
                raw_end=len(sub),
            ))
    return _renormalize(steps_to_events(new_steps))


def _kind_for_events(events: Sequence[dict[str, Any]]) -> str:
    """猜一个事件分组该用什么步骤类型（用于拆分后的子步骤）。"""
    if not events:
        return "click"
    etype = events[0].get("type")
    if etype == "mouse_click":
        return "click"
    if etype == "mouse_move":
        return "move"
    if etype == "mouse_scroll":
        return "scroll"
    if etype == "key":
        return "key"
    return "click"


def _renormalize(events: Sequence[dict[str, Any]],
                 gaps: Optional[Sequence[int]] = None) -> list[dict[str, Any]]:
    """重排时间戳并**重新打上步骤边界**，保证编辑后的步骤粒度不被合并逻辑改变。

    ``gaps`` 为空时沿用旧行为（步间统一压到 :data:`MIN_STEP_GAP_MS`，
    用于删除/移动/拆分这类结构变更）；传入间隔列表时按原间隔铺设，
    用于「只改了某一步内容/时长」的编辑——保留录制节奏。
    """
    if gaps is None:
        return steps_to_events(build_steps(normalize_timestamps(events)))
    return steps_to_events(build_steps(layout_with_gaps(events, gaps)))

def events_duration_ms(events: Sequence[dict[str, Any]]) -> int:
    """事件序列的总时长（最后一条事件的时间戳）。"""
    if not events:
        return 0
    return int(max(int(e.get("t") or 0) for e in events))


def set_step_time(events: Sequence[dict[str, Any]], step_index: int,
                  new_start_ms: int) -> list[dict[str, Any]]:
    """把第 ``step_index`` 步的起始时间改成 ``new_start_ms``。

    处理方式：

    * 该步骤**内部**事件之间的相对偏移保持不变（按住时长、连键节奏不破坏）；
    * 该步骤**之后**的所有步骤整体平移，保持它们之间的间隔不变；
    * 若新时间早于前一步骤的结束时间，则夹到「前一步结束 + 最小间隔」，
      保证时间戳单调不减（否则模式一会把顺序执行错）。

    返回新的完整事件序列。
    """
    events = [dict(e) for e in events]
    steps = build_steps(events)
    total = len(steps)
    if not 0 <= step_index < total:
        raise StepEditError(f"步骤下标越界：{step_index}（共 {total} 步）")

    target = steps[step_index]
    if step_index == 0:
        floor = 0
    else:
        floor = steps[step_index - 1].t_end + max(1, int(MIN_STEP_GAP_MS))
    new_start = max(int(new_start_ms), floor)

    delta = new_start - target.t_start
    if delta == 0:
        return events

    # 该步骤及其后所有事件整体平移，步间间隔保持不变
    for event in events[target.raw_start:]:
        event["t"] = int(event.get("t") or 0) + delta

    # 若首步被推后，其余步骤也随之平移，因此基准仍是全局最小值 0；
    # 若首步被提前到 0，则后续保持相对间隔。
    return events


def set_step_duration(events: Sequence[dict[str, Any]], step_index: int,
                      new_duration_ms: int) -> list[dict[str, Any]]:
    """把第 ``step_index`` 步的持续时长改成 ``new_duration_ms``（第九阶段）。

    处理方式：

    * 该步骤**起始时间不变**，结束时间变为 ``起始 + 新时长``；
    * 步骤**内部**事件的时间偏移按 ``新时长 / 旧时长`` **等比缩放**
      （组合键 / 连击的内部节奏按比例保持；旧时长为 0 的步骤
      内部只有同一时刻的事件，没有可缩放的空间，保持原样）；
    * 该步骤**之后**的所有步骤整体平移 ``新时长 - 旧时长``，
      步间间隔保持不变 —— 因此后续步骤的起始时间结构性地
      不可能早于前一步的结束时间（间隔被完整保留）。

    返回新的完整事件序列。
    """
    events = [dict(e) for e in events]
    steps = build_steps(events)
    total = len(steps)
    if not 0 <= step_index < total:
        raise StepEditError(f"步骤下标越界：{step_index}（共 {total} 步）")

    target = steps[step_index]
    old_duration = target.t_end - target.t_start
    new_duration = max(0, int(new_duration_ms))
    delta = new_duration - old_duration
    if delta == 0:
        return events

    # 1) 步骤内部：相对偏移等比缩放（起始事件 offset=0 不动）
    if old_duration > 0:
        factor = new_duration / old_duration
        for event in events[target.raw_start:target.raw_end]:
            offset = int(event.get("t") or 0) - target.t_start
            event["t"] = target.t_start + int(round(offset * factor))

    # 2) 后续步骤：整体平移，间隔保持
    for event in events[target.raw_end:]:
        event["t"] = int(event.get("t") or 0) + delta
    return events


def set_step_gap(events: Sequence[dict[str, Any]], step_index: int,
                 gap_before_ms: Optional[int] = None,
                 gap_after_ms: Optional[int] = None) -> list[dict[str, Any]]:
    """修改某一步与上一步 / 下一步之间的**空白间隔**（第十一阶段）。

    时间模型（与 :func:`step_gaps` 一致）：

    * **动作时长**（步骤自身持续）由 :func:`set_step_duration` 负责；
    * **空白间隔**（两步之间的空档）由本函数负责，二者互不干扰。

    处理方式：

    * 改「与上一步的间隔」：本步及其后所有步骤整体平移
      ``新间隔 - 旧间隔``，前序步骤不动；
    * 改「与下一步的间隔」：下一步及其后所有步骤整体平移，
      本步不动；
    * 间隔一律夹到 >= 0，保证时间轴不出现负数或步骤重叠。

    返回新的完整事件序列（未改动时原样返回）。
    """
    events = [dict(e) for e in events]
    steps = build_steps(events)
    total = len(steps)
    if not 0 <= step_index < total:
        raise StepEditError(f"步骤下标越界：{step_index}（共 {total} 步）")

    # ---- 与上一步的间隔：平移本步及之后的所有事件 ----
    if gap_before_ms is not None:
        current = step_gaps(steps)[step_index]
        delta = max(0, int(gap_before_ms)) - current
        if delta:
            for event in events[steps[step_index].raw_start:]:
                event["t"] = int(event.get("t") or 0) + delta
            steps = build_steps(events)   # 重新计算区间，供下一步使用

    # ---- 与下一步的间隔：平移下一步及之后的所有事件 ----
    if gap_after_ms is not None and step_index + 1 < total:
        current = step_gaps(steps)[step_index + 1]
        delta = max(0, int(gap_after_ms)) - current
        if delta:
            for event in events[steps[step_index + 1].raw_start:]:
                event["t"] = int(event.get("t") or 0) + delta
    return events


def redistribute_duration(events: Sequence[dict[str, Any]],
                          target_ms: int,
                          avg_gap_ms: Optional[int] = None,
                          jitter_ms: int = 0,
                          rng: Optional[random.Random] = None
                          ) -> list[dict[str, Any]]:
    """把整段脚本的总时长重设为 ``target_ms``，按平均间隔 + 随机浮动铺开。

    分配算法（第十一阶段：新增平均间隔与随机浮动）
    ----------------------------------------------
    1. ``Action_Time`` = 所有步骤**动作时长**之和（各步 ``t_end - t_start``）；
    2. ``Gap_Time``    = ``目标总时长 - Action_Time``（不够时按 0 处理）；
    3. ``Base_Gap``    = ``Gap_Time / (步数 - 1)``（最后一步之后没有间隔）；
       若用户显式填了「平均步间间隔」（``avg_gap_ms`` > 0），则直接以它为准，
       此时总时长以间隔设置为准、不再强求等于目标值；
    4. 每个间隔 = ``Base_Gap + random(-jitter_ms, +jitter_ms)``，夹到 >= 0；
    5. 按生成的间隔重新计算每步起止时间戳（步骤内部相对偏移保持不变）。

    ``rng`` 可注入固定种子的随机源，便于测试复现。
    """
    events = [dict(e) for e in events]
    if not events:
        return events
    target_ms = max(0, int(target_ms))
    segments = segment_bounds(events)
    count = len(segments)
    if count == 0:
        return events

    # 每段自身的动作时长（段内最后一条 − 首条），不含步骤之间的空白
    spans = [int(events[hi - 1].get("t") or 0) - int(events[lo].get("t") or 0)
             for lo, hi in segments]
    action_total = sum(spans)
    gap_count = count - 1
    gap_total = max(0, target_ms - action_total)

    # 基础平均间隔：显式输入优先，否则由「剩余时间 / 间隔个数」推导
    explicit_gap = int(avg_gap_ms or 0)
    if explicit_gap > 0:
        base_gap = explicit_gap
    elif gap_count > 0:
        base_gap = int(gap_total / gap_count)
    else:
        base_gap = 0
    base_gap = max(0, base_gap)

    jitter = max(0, int(jitter_ms))
    source = rng if rng is not None else random
    gaps: list[int] = []
    for _ in range(gap_count):
        value = base_gap
        if jitter:
            value += source.randint(-jitter, jitter)
        gaps.append(max(0, value))
    # 没有随机浮动时，把除不尽的余数补给最后一个间隔，保证总时长精确命中目标
    if gaps and not jitter and explicit_gap <= 0:
        gaps[-1] += max(0, gap_total - base_gap * gap_count)

    cursor = 0
    for position, (lo, hi) in enumerate(segments):
        if position > 0:
            cursor += gaps[position - 1]
        raw_start = int(events[lo].get("t") or 0)
        for offset_index in range(lo, hi):
            offset = int(events[offset_index].get("t") or 0) - raw_start
            events[offset_index]["t"] = cursor + offset
        cursor += spans[position]
    return events


def format_duration(ms: int) -> str:
    """毫秒转「分:秒.毫秒」文本。"""
    ms = max(0, int(ms))
    minutes, rem = divmod(ms, 60_000)
    seconds, millis = divmod(rem, 1000)
    if minutes:
        return f"{minutes}:{seconds:02d}.{millis:03d}"
    return f"{seconds}.{millis:03d}s"


def parse_duration(text: str) -> int:
    """把用户输入的时长文本解析成毫秒。

    支持 ``"30"``（秒）、``"30s"``、``"1.5"``、``"2:30"``（分:秒）、
    ``"90ms"``。解析失败抛 :class:`StepEditError`。
    """
    raw = (text or "").strip().lower().replace("：", ":")
    if not raw:
        raise StepEditError("时长不能为空")
    try:
        if raw.endswith("ms"):
            value = float(raw[:-2])
            ms = int(value)
        elif raw.endswith("s"):
            ms = int(float(raw[:-1]) * 1000)
        elif ":" in raw:
            parts = raw.split(":")
            if len(parts) != 2:
                raise ValueError(raw)
            minutes = int(parts[0] or 0)
            seconds = float(parts[1] or 0)
            ms = int((minutes * 60 + seconds) * 1000)
        else:
            ms = int(float(raw) * 1000)
    except (TypeError, ValueError) as exc:
        raise StepEditError(
            f"无法识别的时长：{text}（可填 30 表示 30 秒，或 2:30、1500ms）"
        ) from exc
    if ms < 0:
        raise StepEditError("时长不能为负数")
    return ms




class ScriptError(Exception):
    """脚本文件读写或格式错误。"""


def serialize(name: str, events: Sequence[dict[str, Any]], duration_ms: int = 0) -> dict[str, Any]:
    """把事件序列包装成脚本字典（内部字段不会写入文件）。"""
    evts = strip_internal_fields(events)
    if duration_ms <= 0 and evts:
        duration_ms = int(evts[-1].get("t") or 0)
    return {
        "app": APP_TAG,
        "version": FORMAT_VERSION,
        "name": name or "未命名脚本",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_ms": int(duration_ms),
        "event_count": len(evts),
        "events": evts,
    }


def save_script(path: Path, name: str, events: Sequence[dict[str, Any]],
                duration_ms: int = 0) -> Path:
    """把事件序列写入 JSON 文件，返回实际写入路径。"""
    path = Path(path)
    if path.suffix.lower() != ".json":
        path = path.with_suffix(".json")
    payload = serialize(name, events, duration_ms)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise ScriptError(f"脚本保存失败：{exc}") from exc
    return path


def strip_internal_fields(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """去掉仅用于内存处理的内部字段（例如步骤边界标记）。"""
    out: list[dict[str, Any]] = []
    for event in events:
        item = dict(event)
        item.pop(BOUNDARY_KEY, None)
        out.append(item)
    return out


def load_script(path: Path) -> dict[str, Any]:
    """读取脚本 JSON，返回脚本字典（含 ``events``）。"""
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except FileNotFoundError as exc:
        raise ScriptError(f"脚本不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ScriptError(f"脚本格式错误（JSON 解析失败）：{path.name} —— {exc}") from exc
    except OSError as exc:
        raise ScriptError(f"脚本读取失败：{exc}") from exc

    if not isinstance(payload, dict):
        raise ScriptError(f"脚本格式错误：{path.name} 顶层不是对象")
    events = payload.get("events")
    if not isinstance(events, list):
        raise ScriptError(f"脚本格式错误：{path.name} 缺少 events 数组")

    payload["events"] = [strip_internal_fields([e])[0]
                         for e in events if isinstance(e, dict)]
    payload.setdefault("name", path.stem)
    payload.setdefault("duration_ms", int(payload["events"][-1].get("t") or 0) if payload["events"] else 0)
    return payload


def list_scripts(directory: Path) -> list[Path]:
    """列出脚本目录下的所有 JSON 脚本，按修改时间倒序。"""
    directory = Path(directory)
    if not directory.exists():
        return []
    files = [p for p in directory.glob("*.json") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _key_is_ignored(event: dict[str, Any], ignored: set[str]) -> bool:
    """键盘事件是否命中「永不录制」名单。

    同一个物理键有两种标识形式，两种都要能匹配上：
    ``Key.f9`` 序列化成 ``"f9"``，而从虚拟键码来的是 ``"vk:120"``；
    名单里存哪种写法都要能拦住另一种写法的事件。
    """
    token = str(event.get("key") or "")
    vk = event.get("vk")
    if token and token in ignored:
        return True
    if isinstance(vk, int) and f"vk:{vk}" in ignored:
        return True
    # token 是 vk:NN 而名单里存的是键名（或反过来）
    if token.startswith("vk:"):
        try:
            name = VK_TOKEN_ALIASES.get(int(token[3:]))
        except ValueError:
            name = None
        if name and name in ignored:
            return True
    if isinstance(vk, int):
        name = VK_TOKEN_ALIASES.get(vk)
        if name and name in ignored:
            return True
    return False


#: 少数常用键的 vk <-> 键名 对照（用于「永不录制」名单的两种写法互认）
VK_TOKEN_ALIASES: dict[int, str] = {
    0x78: "f9", 0x75: "f6", 0x76: "f7", 0x77: "f8",
    0x21: "page_up", 0x22: "page_down", 0x24: "home", 0x23: "end",
    0x1B: "esc", 0x0D: "enter", 0x20: "space", 0x09: "tab",
}


def sanitize_events(events: Sequence[dict[str, Any]],
                    ignore_keys: Iterable[str] = ()) -> list[dict[str, Any]]:
    """录制结束前的收尾清洗。

    - 去除非字典项与缺少 type 的项；
    - 丢弃 ``ignore_keys`` 里的按键（默认用于剔除停止录制热键 F9）；
    - 保证 ``t`` 单调不减（时基由 ``time.monotonic()`` 给出，正常不会倒退，
      这里主要防止手写/外部脚本乱序导致回放错乱）；
    - 对录音结束时仍处于“按下未释放”的键补一条释放事件，
      避免回放时按键卡住（stuck key）。
    """
    ignored = {str(k) for k in ignore_keys if k}
    out: list[dict[str, Any]] = []
    last_t = 0
    for evt in events:
        if not isinstance(evt, dict) or not evt.get("type"):
            continue
        item = dict(evt)
        if item.get("type") == "key" and ignored and _key_is_ignored(item, ignored):
            continue
        t = int(item.get("t") or 0)
        if t < last_t:
            t = last_t
        item["t"] = t
        last_t = t
        out.append(item)

    # 收集仍未释放的按键，补上 release，防止卡键
    held: dict[str, dict[str, Any]] = {}
    for item in out:
        if item.get("type") == "key":
            key = str(item.get("key"))
            if item.get("action") == "press":
                held[key] = item
            elif item.get("action") == "release":
                held.pop(key, None)
    for key, ref in held.items():
        out.append(make_key_event(last_t, "release", key, ref.get("vk")))

    # 鼠标按钮同理：按下的按钮若未释放，补一条释放
    held_btn: dict[str, dict[str, Any]] = {}
    for item in out:
        if item.get("type") == "mouse_click":
            button = str(item.get("button"))
            if item.get("action") == "press":
                held_btn[button] = item
            elif item.get("action") == "release":
                held_btn.pop(button, None)
    for button, ref in held_btn.items():
        out.append(make_click_event(last_t, "release", button,
                                    int(ref.get("x") or 0), int(ref.get("y") or 0)))

    return out
