"""全局热键注册与管理。

实现方式
--------
使用 ``pynput.keyboard.Listener`` 做**被动监听**（全局钩子），在回调里把按键
与已注册的热键比对，命中就调用对应动作的回调。相比
``pynput.keyboard.GlobalHotKeys``，这种方式的好处是：

* 单个按键热键（F6/F7/F8/F9/PageUp/PageDown/Home）同样有效；
* 可以随时增删绑定（用户在设置页改热键后立刻生效）；
* 能对每个动作单独返回注册结果，便于把失败原因提示给用户。

线程说明
--------
回调在 pynput 的监听线程中执行。本模块不做任何 UI 操作，只把动作名抛给
上层；上层再用 Qt 的信号槽把执行切回主线程。

关于“被其他程序占用”
--------------------
被动监听不独占按键，所以正常情况下不会注册失败。若监听器启动失败
（例如被杀软拦截、权限不足），:meth:`HotkeyManager.register_all` 会返回失败
动作列表，界面据此提示用户改键或换用其它按键。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from pynput import keyboard

from core.hotkey_hook import KeyboardHook, hook_supported

log = logging.getLogger(__name__)

#: 键名别称 -> pynput Key 名称
KEY_ALIASES: dict[str, str] = {
    "pgup": "page_up", "pgdn": "page_down", "pagedown": "page_down",
    "pageup": "page_up", "esc": "escape", "return": "enter", "del": "delete",
    "ins": "insert", "win": "cmd", "super": "cmd", "meta": "cmd",
    "ctrl": "ctrl_l", "control": "ctrl_l", "shift": "shift", "alt": "alt_l",
    "space": "space", "tab": "tab", "backspace": "backspace",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "caps": "caps_lock", "capslock": "caps_lock",
}

#: 按键显示名（设置页 / 状态栏用）
DISPLAY_NAMES: dict[str, str] = {
    "page_up": "PageUp", "page_down": "PageDown", "home": "Home", "end": "End",
    "escape": "Esc", "enter": "Enter", "space": "Space", "tab": "Tab",
    "backspace": "Backspace", "delete": "Delete", "insert": "Insert",
    "up": "↑", "down": "↓", "left": "←", "right": "→",
    "ctrl_l": "Ctrl", "ctrl_r": "右Ctrl", "shift": "Shift",
    "alt_l": "Alt", "alt_r": "右Alt", "cmd": "Win", "caps_lock": "CapsLock",
    **{f"f{i}": f"F{i}" for i in range(1, 25)},
}


def normalize_token(token: str) -> str:
    """把用户输入的键名归一化：``"F9"`` -> ``"f9"``，``"PgDn"`` -> ``"page_down"``。"""
    token = (token or "").strip().lower()
    return KEY_ALIASES.get(token, token)


def display_name(token: str) -> str:
    """热键的显示文本。"""
    token = normalize_token(token)
    if token in DISPLAY_NAMES:
        return DISPLAY_NAMES[token]
    if len(token) == 1:
        return token.upper()
    return token.replace("_", " ").title()


def parse_key(token: str) -> Any:
    """把热键字符串解析成 pynput 可比较的按键对象。

    支持 ``"f9"``、``"page_down"``、``"a"``、``"1"`` 等形式；
    无法识别时返回 None（该绑定会被跳过并记录日志）。
    """
    token = normalize_token(token)
    if not token:
        return None
    if len(token) == 1:
        return keyboard.KeyCode.from_char(token)
    named = getattr(keyboard.Key, token, None)
    if named is not None:
        return named
    # 数字型 vk 写法：vk:114
    if token.startswith("vk:"):
        try:
            return keyboard.KeyCode.from_vk(int(token[3:]))
        except ValueError:
            return None
    log.warning("无法识别的热键：%s", token)
    return None


@dataclass
class HotkeyBinding:
    """一条热键绑定。"""

    action: str          # 动作名（record_stop / play_toggle / ...）
    token: str           # 归一化后的键名
    key: Any             # 解析后的 pynput 按键对象
    vk: Optional[int] = None   # 虚拟键码（独占模式要按 vk 匹配）

    @property
    def display(self) -> str:
        return display_name(self.token)


class HotkeyManager:
    """全局热键管理器（支持「独占模式」）。

    两种工作方式
    ------------
    * **普通模式**（``exclusive=False``）：用 pynput 的 ``Listener`` 旁观按键，
      热键生效但**不会**阻止前台程序也收到该键。
    * **独占模式**（``exclusive=True``）：改用 Win32 低级键盘钩子
      （:mod:`core.hotkey_hook`），命中热键时直接吞掉事件，
      前台程序收不到；同时由钩子回调负责触发动作，避免与 pynput 重复触发。

    使用方法::

        mgr = HotkeyManager(is_executing=player_flag, exclusive=True)
        mgr.set_callback("play_toggle", start_replay)
        mgr.register_all({"play_toggle": "f6", ...})
        mgr.start()
    """

    def __init__(self, is_executing: Optional[Callable[[], bool]] = None,
                 exclusive: bool = False) -> None:
        #: 返回 True 表示程序正在注入模拟事件，此时忽略热键，避免自我触发
        self._is_executing = is_executing or (lambda: False)
        self._lock = threading.RLock()
        self._bindings: dict[str, HotkeyBinding] = {}
        self._callbacks: dict[str, Callable[[], None]] = {}
        self._keymap: dict[Any, str] = {}       # 解析后的按键 -> 动作名
        self._vkmap: dict[int, str] = {}        # 虚拟键码 -> 动作名（独占模式用）
        self._listener: Optional[keyboard.Listener] = None
        self._hook: Optional[KeyboardHook] = None
        self._running = False
        self._last_error: str = ""
        self._exclusive = bool(exclusive)

    # ------------------------------------------------------------------
    # 独占模式
    # ------------------------------------------------------------------
    @property
    def exclusive(self) -> bool:
        return self._exclusive

    @property
    def hook_alive(self) -> bool:
        """独占钩子当前是否仍被系统认可（未因 UIPI 被摘掉）。"""
        return bool(self._hook is not None and self._hook.hook_alive())

    @property
    def blocked_count(self) -> int:
        """被独占吞掉的按键次数。"""
        return int(self._hook.blocked_count) if self._hook is not None else 0

    def set_exclusive(self, exclusive: bool) -> bool:
        """切换独占模式；已在运行时自动重挂载。返回最终是否生效。"""
        exclusive = bool(exclusive)
        if exclusive == self._exclusive:
            return self._exclusive
        was_running = self._running
        if was_running:
            self.stop()
        self._exclusive = exclusive
        if was_running:
            self.start()
        return self._exclusive

    # ------------------------------------------------------------------
    # 绑定管理
    # ------------------------------------------------------------------
    def set_callback(self, action: str, callback: Optional[Callable[[], None]]) -> None:
        """为动作注册回调（回调在监听/钩子线程执行，务必只做转发）。"""
        with self._lock:
            if callback is None:
                self._callbacks.pop(action, None)
            else:
                self._callbacks[action] = callback

    def set_binding(self, action: str, token: str) -> bool:
        """新增或修改一条热键绑定。返回是否解析成功。"""
        key = parse_key(token)
        with self._lock:
            if key is None:
                log.warning("热键 %s=%s 无法解析", action, token)
                return False
            norm = normalize_token(token)
            self._bindings[action] = HotkeyBinding(
                action=action, token=norm, key=key, vk=key_to_vk(norm)
            )
            self._rebuild_keymap()
            return True

    def register_all(self, hotkeys: dict[str, str]) -> list[str]:
        """批量注册热键，返回**注册失败**的动作名列表（空列表表示全部成功）。

        被动监听模式下不存在“组合键被占用”导致的失败；这里返回的是
        键名无法解析、或监听器/钩子启动失败的动作，供界面提示用户改键。
        """
        failed: list[str] = []
        with self._lock:
            self._bindings.clear()
            self._keymap.clear()
            self._vkmap.clear()
            for action, token in hotkeys.items():
                key = parse_key(token)
                if key is None:
                    failed.append(action)
                    log.warning("热键 %s=%s 无法解析，已跳过", action, token)
                    continue
                norm = normalize_token(token)
                self._bindings[action] = HotkeyBinding(
                    action=action, token=norm, key=key, vk=key_to_vk(norm)
                )
            self._rebuild_keymap()
        # 已启动时重新挂载，让新绑定立刻生效
        if self._running:
            self.stop()
            if not self.restart():
                failed.extend(self._bindings.keys())
        return failed

    def restart(self) -> bool:
        """重新挂载热键；独占失败时自动退化到被动监听。

        返回最终是否成功启动（可能是「独占失败但被动监听成功」）。
        """
        if self.start():
            return True
        if not self._exclusive:
            return False
        log.warning("独占模式启动失败，退化为普通监听")
        self._exclusive = False
        return self.start()

    def _rebuild_keymap(self) -> None:
        """重建“按键 -> 动作”查找表（调用方需持有锁）。"""
        self._keymap = {b.key: a for a, b in self._bindings.items()}
        self._vkmap = {b.vk: a for a, b in self._bindings.items() if b.vk is not None}

    def binding_display(self, action: str) -> str:
        """返回动作对应的热键显示文本（未绑定时为空串）。"""
        with self._lock:
            binding = self._bindings.get(action)
            return binding.display if binding else ""

    def display_map(self) -> dict[str, str]:
        """返回 ``{动作名: 显示文本}``，供状态栏拼接提示。"""
        with self._lock:
            return {a: b.display for a, b in self._bindings.items()}

    def duplicate_actions(self, action: str, token: str) -> list[str]:
        """检查是否有其它动作已经占用了同一个键，返回冲突的动作名列表。"""
        key = parse_key(token)
        if key is None:
            return []
        with self._lock:
            return [a for a, b in self._bindings.items() if a != action and b.key == key]

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        """启动全局热键。返回是否成功。

        独占模式下改用 Win32 低级键盘钩子（既能触发动作也能吞掉按键），
        此时**不再**启动 pynput 监听，避免同一个键触发两次。
        """
        if self._running:
            return True
        if self._exclusive:
            return self._start_exclusive()
        return self._start_passive()

    def _start_passive(self) -> bool:
        """普通模式：pynput 被动监听（不阻止按键传给其它程序）。"""
        try:
            self._listener = keyboard.Listener(on_press=self._on_press)
            self._listener.daemon = True
            self._listener.start()
            self._running = True
            return True
        except Exception as exc:  # pragma: no cover - 依赖系统钩子
            log.exception("全局热键监听启动失败")
            self._listener = None
            self._running = False
            self._last_error = str(exc)
            return False

    def _start_exclusive(self) -> bool:
        """独占模式：Win32 低级键盘钩子，命中热键时吞掉事件。"""
        if not hook_supported():
            self._last_error = "当前系统不支持热键独占（仅 Windows）"
            log.warning(self._last_error)
            return False
        hook = KeyboardHook(on_hotkey=self._on_hook_hotkey,
                            should_block=self._should_block_vk)
        if not hook.start():
            self._last_error = hook.last_error or "键盘钩子安装失败"
            log.error("热键独占模式启动失败：%s", self._last_error)
            hook.stop()
            return False
        self._hook = hook
        self._running = True
        log.info("热键独占模式已启用（命中热键时不再传给其它程序）")
        return True

    def stop(self, join_timeout: float = 0.0) -> None:
        """停止全局热键，并卸载独占钩子。

        默认**不等待**监听线程退出：pynput 在 Windows 上的 ``Listener.stop()``
        内部会 join 监听线程，若在 Qt 关闭流程里调用，可能与监听线程互相等待
        造成程序无法退出（实测表现为关闭窗口后进程一直不结束）。
        这里把 join 超时压到 0，只发停止信号，线程本身是 daemon，
        进程退出时会被回收。
        """
        listener = self._listener
        hook = self._hook
        self._listener = None
        self._hook = None
        self._running = False

        if hook is not None:
            try:
                hook.stop()
            except Exception:  # pragma: no cover
                log.debug("键盘钩子卸载异常", exc_info=True)
        if listener is None:
            return
        try:
            listener.stop()
        except Exception:  # pragma: no cover
            log.debug("热键监听 stop 异常", exc_info=True)
        if join_timeout > 0:
            try:
                listener.join(timeout=join_timeout)
            except Exception:  # pragma: no cover
                log.debug("热键监听 join 异常", exc_info=True)

    # ------------------------------------------------------------------
    # 独占钩子回调（钩子线程）
    # ------------------------------------------------------------------
    def _should_block_vk(self, vk: int) -> bool:
        """该虚拟键是否需要被独占吞掉（钩子线程调用）。

        返回 True 表示命中已注册热键：钩子会吃掉事件，前台程序收不到。
        """
        if self._is_executing():
            # 程序自己注入的按键不应被拦（钩子已按 LLKHF_INJECTED 过滤，
            # 这里是第二道保险）
            return False
        with self._lock:
            return vk in self._vkmap

    def _on_hook_hotkey(self, vk: int) -> None:
        """独占钩子命中热键（钩子线程）：触发对应动作。"""
        try:
            with self._lock:
                action = self._vkmap.get(vk)
                callback = self._callbacks.get(action) if action else None
            if callback is None:
                return
            callback()
        except Exception:  # pragma: no cover - 不得中断钩子链
            log.exception("独占热键回调执行失败")

    # ------------------------------------------------------------------
    # 被动监听回调（监听线程）
    # ------------------------------------------------------------------
    def _on_press(self, key: Any) -> None:
        """按键回调（监听线程）：命中绑定则调用动作回调。"""
        try:
            # 程序自己注入的按键忽略掉，避免“回放 -> 触发热键 -> 再回放”的自我循环。
            # （pynput 在 Windows 上已用 LLKHF_INJECTED 过滤注入事件，这里是第二道保险。）
            if self._is_executing():
                return
            with self._lock:
                action = self._keymap.get(key)
                if action is None:
                    return
                callback = self._callbacks.get(action)
            if callback is None:
                return
            callback()
        except Exception:  # pragma: no cover - 回调异常不得中断监听线程
            log.exception("热键回调执行失败")


def probe_hotkey(token: str) -> bool:
    """校验单个热键键名是否可用（供设置页即时反馈）。"""
    return parse_key(token) is not None


def key_to_vk(token: str) -> Optional[int]:
    """把键名转换成虚拟键码（独占钩子按 vk 匹配按键）。

    pynput 在 Windows 上的 ``Key.f6.vk`` 是 ``None``，真实键码藏在
    ``Key.f6.value``（一个 ``KeyCode``）里，字符为空的特殊键
    （F1–F24 / PageUp / Home…）只有 ``KeyCode.vk`` 可用；
    像 ``space`` 这种 ``KeyCode.char == ' '`` 的键则用一张小表兜底。
    """
    token = normalize_token(token)
    if not token:
        return None
    if token.startswith("vk:"):
        try:
            return int(token[3:])
        except ValueError:
            return None
    if len(token) == 1:
        char = token.upper()
        if ("A" <= char <= "Z") or ("0" <= char <= "9"):
            return ord(char)
        return None

    key = parse_key(token)
    # KeyCode 形态：直接取 vk
    vk = getattr(key, "vk", None)
    if isinstance(vk, int):
        return vk
    # Key 枚举形态：value 是 KeyCode
    value = getattr(key, "value", None)
    value_vk = getattr(value, "vk", None)
    if isinstance(value_vk, int):
        return value_vk
    if isinstance(value, int):
        return value
    char = getattr(value, "char", None) or getattr(key, "char", None)
    if isinstance(char, str) and char:
        return _CHAR_TO_VK.get(char.lower()) or _CHAR_TO_VK.get(char.upper())
    return None


#: 部分字符键 -> 虚拟键码（用于 space / enter 等别名字符）
_CHAR_TO_VK: dict[str, int] = {
    " ": 0x20, "\t": 0x09, "\r": 0x0D, "\n": 0x0D,
    "\x1b": 0x1B, "\x08": 0x08,
    **{chr(c): c for c in range(0x30, 0x3A)},          # 0-9
    **{chr(c): c for c in range(0x41, 0x5B)},          # A-Z
    **{chr(c).lower(): c for c in range(0x41, 0x5B)},  # a-z
}
