"""录制模块：封装 pynput 的全局鼠标/键盘监听。

设计要点
--------
1. pynput 的 Listener 在**独立线程**里触发回调，因此本模块用一个
   ``threading.Lock`` 保护事件列表，绝不在回调里碰 UI。事件通过回调
   ``on_event`` 抛给上层，由上层用 Qt 信号槽切回主线程。
2. 每个事件带一个相对录制开始的毫秒时间戳（``t``）。时间基准使用
   ``time.monotonic()``：单调时钟不受系统时间调整影响，间隔计算更可靠。
3. 抖动过滤：相邻 ``mouse_move`` 之间若“时间间隔 < 阈值 且 位移 < 阈值”
   则丢弃，阈值来自配置（默认 50ms / 2px）。
4. ``is_executing`` 标志位由回放模块置位，监听回调检测到它为 True 时
   直接跳过，从而避免把程序自己发出的模拟事件再录一遍。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from pynput import keyboard, mouse

from core import event_parser as ep

log = logging.getLogger(__name__)

#: 单个监听器停止时的等待上限（秒），避免界面卡死
_JOIN_TIMEOUT = 1.0

#: 默认永不录制的按键：停止录制热键 F9。
#:
#: 同一物理键在不同来源下标识不同，两种写法都要列上：
#: * pynput 回调看到 ``Key.f9`` -> 序列化成 token ``"f9"``；
#: * 从 vk 还原（或旧脚本）时是 ``"vk:120"``（VK_F9 = 0x78 = 120）。
DEFAULT_IGNORED_KEYS: frozenset[str] = frozenset({"f9", "vk:120"})

#: 默认忽略的按键对应的虚拟键码（用于扫描旧脚本里以 vk 形式存在的 F9）
DEFAULT_IGNORED_VKS: frozenset[int] = frozenset({0x78})


class Recorder:
    """键鼠录制器。

    参数
    ----
    is_executing:
        无参可调用对象。返回 True 表示“当前是程序在回放”，此时忽略事件。
    on_event:
        ``fn(event)``，每记录一个事件回调一次（可能来自监听线程）。
    on_error:
        ``fn(message)``，监听器异常时回调。
    """

    def __init__(
        self,
        is_executing: Optional[Callable[[], bool]] = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._is_executing = is_executing or (lambda: False)
        self._on_event = on_event
        self._on_error = on_error

        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._recording = False
        self._t0 = 0.0                      # 录制起点（monotonic 秒）
        self._mouse_listener: Optional[mouse.Listener] = None
        self._key_listener: Optional[keyboard.Listener] = None

        # 抖动过滤参数与上一帧鼠标状态
        # 第九阶段起：始终记录鼠标移动 + 点击（「仅录制点击坐标」
        # 与「记录鼠标移动」开关已删除，不再有只记点击的模式）
        self.move_min_interval_ms = 50
        self.move_min_distance_px = 2
        self._last_move: Optional[tuple[float, int, int]] = None

        #: 永不录制的按键（键标识）。默认屏蔽停止录制热键 F9：
        #: 用户按 F9 停录制时，这个按键本身不应该出现在脚本里，
        #: 否则回放到最后会「用脚本按 F9」，把下一段录制也停掉。
        self.ignored_keys: set[str] = set(DEFAULT_IGNORED_KEYS)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    def elapsed_ms(self) -> int:
        """当前已录制时长（毫秒）。"""
        with self._lock:
            if not self._recording:
                return int(self._events[-1]["t"]) if self._events else 0
            return int((time.monotonic() - self._t0) * 1000)

    def get_events(self) -> list[dict[str, Any]]:
        """返回事件列表的副本（线程安全）。"""
        with self._lock:
            return [dict(e) for e in self._events]

    # ------------------------------------------------------------------
    # 开始 / 停止
    # ------------------------------------------------------------------
    def start(self, min_interval_ms: int = 50,
              min_distance_px: int = 2) -> bool:
        """开始录制。已在录制中则返回 False。

        始终记录鼠标移动（带抖动过滤）、点击、滚轮与键盘。
        """
        with self._lock:
            if self._recording:
                return False
            self._events = []
            self._last_move = None
            self.move_min_interval_ms = int(min_interval_ms)
            self.move_min_distance_px = int(min_distance_px)
            # 时间基准：录制开始时取一次 monotonic，之后所有事件都用它做差
            self._t0 = time.monotonic()
            self._recording = True

        try:
            self._mouse_listener = mouse.Listener(
                on_move=self._handle_move,
                on_click=self._handle_click,
                on_scroll=self._handle_scroll,
            )
            self._key_listener = keyboard.Listener(
                on_press=self._handle_press,
                on_release=self._handle_release,
            )
            # daemon 线程：主程序退出时不阻塞进程结束
            self._mouse_listener.daemon = True
            self._key_listener.daemon = True
            self._mouse_listener.start()
            self._key_listener.start()
        except Exception as exc:  # pragma: no cover - 依赖系统钩子
            log.exception("录制监听器启动失败")
            with self._lock:
                self._recording = False
            self._notify_error(f"监听器启动失败：{exc}")
            return False
        return True

    def stop(self) -> list[dict[str, Any]]:
        """停止录制，返回清洗后的事件列表（已补全未释放的按键/按钮）。"""
        with self._lock:
            if not self._recording:
                return self.get_events()
            self._recording = False
            events = list(self._events)

        # 先停止监听线程，确保不会再有新事件写入
        self._stop_listeners()
        events = ep.sanitize_events(events, ignore_keys=self.ignored_keys)
        with self._lock:
            self._events = events
        return [dict(e) for e in events]

    def _stop_listeners(self) -> None:
        """逐个停掉监听器；pynput 的 stop 在自身回调线程里调用会阻塞，故带超时。"""
        for listener in (self._mouse_listener, self._key_listener):
            if listener is None:
                continue
            try:
                listener.stop()
            except Exception:  # pragma: no cover
                log.debug("监听器 stop() 异常", exc_info=True)
        for listener in (self._mouse_listener, self._key_listener):
            if listener is None:
                continue
            try:
                listener.join(timeout=_JOIN_TIMEOUT)
            except Exception:  # pragma: no cover
                pass
        self._mouse_listener = None
        self._key_listener = None

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _stamp(self) -> int:
        """生成事件时间戳：相对录制开始的毫秒数。"""
        return int((time.monotonic() - self._t0) * 1000)

    def _skip(self) -> bool:
        """是否需要忽略当前事件（正在回放自己发出的事件）。

        标志位读取异常时保守地按“不忽略”处理：宁可多录一条，
        也不要因为一次异常把整段录制吞掉；同时写日志便于排查。
        """
        try:
            return bool(self._is_executing())
        except Exception:  # pragma: no cover - 防御性
            log.warning("读取 is_executing 失败，按未执行处理", exc_info=True)
            return False

    def _append(self, event: dict[str, Any]) -> None:
        with self._lock:
            if not self._recording:
                return
            self._events.append(event)
        if self._on_event is not None:
            try:
                self._on_event(dict(event))
            except Exception:  # pragma: no cover - 回调异常不影响录制
                log.debug("on_event 回调异常", exc_info=True)

    def _notify_error(self, message: str) -> None:
        log.error(message)
        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:  # pragma: no cover
                log.debug("on_error 回调异常", exc_info=True)

    # ------------------------------------------------------------------
    # pynput 回调（运行在监听线程）
    # ------------------------------------------------------------------
    def _handle_move(self, x: int, y: int) -> None:
        """鼠标移动：带抖动过滤（时间太近且位移太小的帧丢弃）。"""
        try:
            if self._skip():
                return
            now = time.monotonic()
            if self._last_move is not None:
                last_t, last_x, last_y = self._last_move
                dt_ms = (now - last_t) * 1000.0
                dist = max(abs(x - last_x), abs(y - last_y))  # 切比雪夫距离，廉价且够用
                # 同坐标的重复 move 事件直接丢弃 —— pynput 在鼠标按钮状态变化
                # （按下/释放）时会发出同坐标的 WM_MOUSEMOVE，不滤掉就会在
                # 点击的按下与释放之间插一条独立 move 步骤
                if dist == 0:
                    return
                # 抖动判定：时间太近 **且** 位移太小 -> 丢弃
                if dt_ms < self.move_min_interval_ms and dist < self.move_min_distance_px:
                    return
            self._last_move = (now, int(x), int(y))
            self._append(ep.make_move_event(self._stamp(), x, y))
        except Exception as exc:  # pragma: no cover
            self._notify_error(f"处理鼠标移动事件出错：{exc}")

    def _handle_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        """鼠标点击：按下/释放各记一条事件，坐标取事件自带坐标。"""
        try:
            if self._skip():
                return
            self._append(ep.make_click_event(
                self._stamp(),
                "press" if pressed else "release",
                _button_name(button),
                x, y,
            ))
        except Exception as exc:  # pragma: no cover
            self._notify_error(f"处理鼠标点击事件出错：{exc}")

    def _handle_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        """鼠标滚轮：记录滚动方向与格数。"""
        try:
            if self._skip():
                return
            self._append(ep.make_scroll_event(self._stamp(), dx, dy, x, y))
        except Exception as exc:  # pragma: no cover
            self._notify_error(f"处理鼠标滚轮事件出错：{exc}")

    def _handle_press(self, key: Any) -> None:
        try:
            if self._skip():
                return
            token, vk = key_to_token(key)
            if self._is_ignored(token, vk):
                return
            self._append(ep.make_key_event(self._stamp(), "press", token, vk))
        except Exception as exc:  # pragma: no cover
            self._notify_error(f"处理键盘按下事件出错：{exc}")

    def _handle_release(self, key: Any) -> None:
        try:
            if self._skip():
                return
            token, vk = key_to_token(key)
            if self._is_ignored(token, vk):
                return
            self._append(ep.make_key_event(self._stamp(), "release", token, vk))
        except Exception as exc:  # pragma: no cover
            self._notify_error(f"处理键盘释放事件出错：{exc}")

    def _is_ignored(self, token: str, vk: Optional[int]) -> bool:
        """该按键是否在「永不录制」名单里（默认含停止录制热键 F9）。"""
        if not self.ignored_keys:
            return False
        if token and token in self.ignored_keys:
            return True
        if isinstance(vk, int) and f"vk:{vk}" in self.ignored_keys:
            return True
        # 名单里存的是键名（"f9"）而事件给的是 vk（120）时也要能匹配
        if isinstance(vk, int):
            name = ep.VK_TOKEN_ALIASES.get(vk)
            if name and name in self.ignored_keys:
                return True
        return False

# ---------------------------------------------------------------------------
# 按键 <-> 序列化标识 的相互转换
# ---------------------------------------------------------------------------

def _button_name(button: Any) -> str:
    """鼠标按钮归一化：left / right / middle / unknown。"""
    try:
        if button == mouse.Button.left:
            return "left"
        if button == mouse.Button.right:
            return "right"
        if button == mouse.Button.middle:
            return "middle"
    except Exception:  # pragma: no cover
        pass
    name = getattr(button, "name", None)
    return str(name) if name else "unknown"


def key_to_token(key: Any) -> tuple[str, Optional[int]]:
    """把 pynput 的按键对象转成可 JSON 化的 ``(key, vk)``。

    - 普通字符键：``key = "vk:65"``，字符另存于 ``char``（本函数只返回 vk 标识）
    - 特殊键（F1..F12、方向键等）：``key = "vk:112"``
    - 无 vk 的字符键：``key = "char:a"``
    """
    vk = getattr(key, "vk", None)
    char = getattr(key, "char", None)
    if isinstance(vk, int):
        return f"vk:{vk}", vk
    if isinstance(char, str) and char:
        return f"char:{char}", None
    name = getattr(key, "name", None) or str(key)
    return str(name), None


def token_to_key(token: str, vk: Optional[int] = None) -> Any:
    """把序列化标识还原成 pynput 可用的按键对象（回放时用）。"""
    # pynput 的 Controller 对 KeyCode(vk=...) 的兼容性最好，
    # 因为 vk 层不区分大小写与输入法状态；只有确实没有 vk 时才用 char。
    if token.startswith("char:"):
        char = token[5:]
        if vk is not None:
            return keyboard.KeyCode.from_vk(int(vk))
        return keyboard.KeyCode.from_char(char)
    if isinstance(vk, int):
        return keyboard.KeyCode.from_vk(int(vk))
    if token.startswith("vk:"):
        try:
            return keyboard.KeyCode.from_vk(int(token[3:]))
        except ValueError:
            pass
    # 特殊键名（"f9"、"page_down" 等）优先转成 keyboard.Key 枚举
    named = getattr(keyboard.Key, token, None)
    if named is not None:
        return named
    return keyboard.KeyCode.from_char(token)
