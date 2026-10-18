"""兼容轮询录制器：用于腾讯系（WeGame / QQ游戏大厅等）界面下的输入采集。

背景
----
WeGame / QQ游戏大厅等腾讯系界面的反作弊模块（ACE/SGuard、TenProtect 等）
运行于内核层，会在受保护界面为前台时**过滤掉发往用户态的低层钩子通知**
（``WH_MOUSE_LL`` / ``WH_KEYBOARD_LL``）。因此基于 pynput（本质是低层钩子）
的 :class:`core.recorder.Recorder` 在这些界面为前台时收不到任何事件。

本模块提供用户态的替代采集通道：**轮询**。

* ``GetAsyncKeyState`` / ``GetCursorPos`` 是查询式 API，不经过钩子分发链，
  反作弊驱动通常不做拦截；
* 本线程以约 4ms 的周期扫描按键状态，按「状态沿」判定按下/释放，
  输出事件结构与 :mod:`core.event_parser` 完全一致，回放器无需任何修改。

与钩子通道的协作方式（互补，互不重复）
--------------------------------------
轮询线程每轮先取前台窗口的进程名：

* **命中腾讯系黑名单** -> 本通道产出事件（此时钩子通道被反作弊过滤，
  天然静默）；
* **未命中** -> 只刷新按键快照、不产出任何事件（此时钩子通道正常工作）。

因此两条通道可以同时运行，事件直接汇入同一条 ``on_event`` 管线，
无需模式切换与去重。

已知局限
--------
* 滚轮没有可轮询的状态 API，兼容模式下无法录制滚轮；
* 轮询间隔 4ms，极短促的点击理论上可能丢失（实际点击持续 50~150ms，
  漏检概率可忽略）；
* 「录制游戏本体」仍受反作弊注入防护限制，本模块只解决**大厅/登录器界面**
  的录制问题。
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ctypes import wintypes

from core import event_parser as ep

log = logging.getLogger(__name__)

#: 轮询周期（秒）。约 4ms（250Hz），足够捕捉正常点击且 CPU 占用极低。
_POLL_INTERVAL_S = 0.004

#: 前台进程名（去扩展名、小写）包含这些子串即视为「钩子被过滤的腾讯系界面」。
#: 命中时轮询通道才产出事件；可根据后续遇到的进程名扩充。
BLACKLIST_KEYWORDS: tuple[str, ...] = (
    "wegame",        # WeGame 主程序及助手
    "tgp",           # WeGame 前身 TGP 的守护进程
    "qqgame",        # QQ游戏大厅（QQGame / QQGameHall 等）
    "sguard",        # ACE 反作弊 SGuard64 / SGuard
    "anticheatexpert",  # ACE AntiCheatExpert
    "tenprotect",    # TenProtect
    "tp3helper",     # TP 系辅助进程
)

#: 需要轮询的鼠标按键 VK -> 按钮名（X1/X2 回放暂不支持，不录）。
_MOUSE_BUTTON_VKS: tuple[tuple[int, str], ...] = (
    (0x01, "left"),
    (0x02, "right"),
    (0x04, "middle"),
)

#: 键盘扫描范围：VK_BACK(0x08) .. 0xFE。
_KEY_SCAN_RANGE = range(0x08, 0xFF)

_PRESSED_MASK = 0x8000  # GetAsyncKeyState 返回值的「当前按下」位


def _is_blacklisted_process(stem: str) -> bool:
    """前台进程名是否命中腾讯系黑名单（纯函数，便于测试）。"""
    if not stem:
        return False
    name = stem.lower()
    return any(keyword in name for keyword in BLACKLIST_KEYWORDS)


def _load_winapi() -> Any:
    """加载 user32 / kernel32 并固定 64 位安全的参数签名。

    封装成函数便于在无窗口环境（测试）中延迟加载。
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short

    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return user32, kernel32


class PollRecorder:
    """腾讯系界面兼容轮询录制器。

    接口与 :class:`core.recorder.Recorder` 保持一致，可与它并行运行：
    两者共用同一 ``on_event`` 回调与同一时间基 ``t0``，产出的事件
    可以直接合并排序。

    参数
    ----
    is_executing:
        返回 True 表示当前是程序在回放，此时只刷新按键快照、不产出事件
        （避免把程序自己注入的输入录进去）。
    on_event:
        ``fn(event)``，每记录一个事件回调一次（来自轮询线程）。
    on_error:
        ``fn(message)``，轮询异常时回调。
    on_stop_hotkey:
        ``fn()``，兼容模式下检测到「停止录制热键」按下沿时回调
        （低层钩子热键在腾讯系前台同样会失效，因此这里兜底）。
    """

    def __init__(
        self,
        is_executing: Optional[Callable[[], bool]] = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_stop_hotkey: Optional[Callable[[], None]] = None,
    ) -> None:
        self._is_executing = is_executing or (lambda: False)
        self._on_event = on_event
        self._on_error = on_error
        self._on_stop_hotkey = on_stop_hotkey

        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._recording = False
        self._t0 = 0.0

        self.move_min_interval_ms = 50
        self.move_min_distance_px = 2
        self._last_move: Optional[tuple[float, int, int]] = None

        #: 停止录制热键的 VK（默认 F9 = 0x78），可通过 start() 的参数覆盖。
        #: 该键按下沿触发 on_stop_hotkey，且本身不会被录进事件。
        self.stop_vk: int = 0x78

        # 上一轮输入状态快照（vk -> 是否按下），沿检测的基准
        self._prev_mouse: dict[int, bool] = {}
        self._prev_keys: dict[int, bool] = {}

        self._thread: Optional[threading.Thread] = None

        try:
            self._user32, self._kernel32 = _load_winapi()
        except Exception as exc:  # pragma: no cover - 非 Windows 环境才发生
            log.error("轮询录制器 WinAPI 加载失败：%s", exc)
            self._user32 = None
            self._kernel32 = None

    # ------------------------------------------------------------------
    # 状态（与 Recorder 同名同义）
    # ------------------------------------------------------------------
    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    def elapsed_ms(self) -> int:
        with self._lock:
            if not self._recording:
                return int(self._events[-1]["t"]) if self._events else 0
            return int((time.monotonic() - self._t0) * 1000)

    def get_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._events]

    # ------------------------------------------------------------------
    # 开始 / 停止
    # ------------------------------------------------------------------
    def start(self, t0: float, stop_vk: int = 0x78,
              min_interval_ms: int = 50,
              min_distance_px: int = 2) -> bool:
        """开始轮询录制。

        参数
        ----
        t0:
            与 :class:`Recorder` 一致的录制起点（``time.monotonic()`` 秒），
            保证两通道事件时间戳可直接合并排序。
        stop_vk:
            停止录制热键的 VK 码，按下沿触发 ``on_stop_hotkey``。
        """
        if self._user32 is None:  # pragma: no cover
            self._notify_error("轮询录制器不可用：WinAPI 加载失败。")
            return False
        with self._lock:
            if self._recording:
                return False
            self._events = []
            self._last_move = None
            self._prev_mouse = {}
            self._prev_keys = {}
            self._t0 = float(t0)
            self.stop_vk = int(stop_vk) & 0xFF
            self.move_min_interval_ms = int(min_interval_ms)
            self.move_min_distance_px = int(min_distance_px)
            self._recording = True

        self._thread = threading.Thread(
            target=self._poll_loop, name="PollRecorder", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> list[dict[str, Any]]:
        """停止轮询，返回清洗后的事件列表（与 Recorder.stop 语义一致）。"""
        with self._lock:
            if not self._recording:
                return self.get_events()
            self._recording = False

        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

        with self._lock:
            events = list(self._events)
        events = ep.sanitize_events(events)
        with self._lock:
            self._events = events
        return [dict(e) for e in events]

    # ------------------------------------------------------------------
    # 轮询线程
    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        user32 = self._user32
        while self._recording:
            try:
                self._tick(user32)
            except Exception as exc:  # pragma: no cover
                self._notify_error(f"轮询采集异常：{exc}")
            time.sleep(_POLL_INTERVAL_S)

    def _tick(self, user32: Any) -> None:
        """单轮采集：先判前台进程，再按需扫描输入。"""
        stem = self._foreground_stem()
        active = _is_blacklisted_process(stem)

        pt = wintypes.POINT()
        has_cursor = bool(active and user32.GetCursorPos(ctypes.byref(pt)))
        x, y = pt.x, pt.y

        mouse_now = {vk: bool(user32.GetAsyncKeyState(vk) & _PRESSED_MASK)
                     for vk, _name in _MOUSE_BUTTON_VKS}
        keys_now = ({vk: bool(user32.GetAsyncKeyState(vk) & _PRESSED_MASK)
                     for vk in _KEY_SCAN_RANGE} if active else {})

        skip = self._skip()
        pressed: list[dict[str, Any]] = []
        released: list[dict[str, Any]] = []

        # 鼠标按键沿（只在黑名单前台时产出）
        if active and not skip:
            for vk, button in _MOUSE_BUTTON_VKS:
                was = self._prev_mouse.get(vk, False)
                now = mouse_now[vk]
                if now and not was:
                    pressed.append(ep.make_click_event(
                        self._stamp(), "press", button, x, y))
                elif was and not now:
                    released.append(ep.make_click_event(
                        self._stamp(), "release", button, x, y))

        # 键盘沿（只在黑名单前台时产出；stop_vk 触发兜底热键）
        if active and not skip:
            for vk, now in keys_now.items():
                if vk == self.stop_vk:
                    was = self._prev_keys.get(vk, False)
                    if now and not was:
                        self._fire_stop_hotkey()
                    continue
                was = self._prev_keys.get(vk, False)
                if now and not was:
                    pressed.append(ep.make_key_event(
                        self._stamp(), "press", f"vk:{vk}", vk))
                elif was and not now:
                    released.append(ep.make_key_event(
                        self._stamp(), "release", f"vk:{vk}", vk))

        self._prev_mouse = mouse_now
        self._prev_keys = keys_now

        if not active:
            return

        # 鼠标移动（带抖动过滤，规则与 Recorder._handle_move 一致）
        if has_cursor and not skip:
            self._maybe_record_move(x, y)

        for event in pressed:
            self._append(event)
        for event in released:
            self._append(event)

    def _maybe_record_move(self, x: int, y: int) -> None:
        now = time.monotonic()
        if self._last_move is not None:
            last_t, last_x, last_y = self._last_move
            dt_ms = (now - last_t) * 1000.0
            dist = max(abs(x - last_x), abs(y - last_y))
            if dist == 0:
                return
            if dt_ms < self.move_min_interval_ms and dist < self.move_min_distance_px:
                return
        self._last_move = (now, int(x), int(y))
        self._append(ep.make_move_event(self._stamp(), x, y))

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _stamp(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def _skip(self) -> bool:
        """回放期间为 True：只刷新快照，不产出事件。"""
        try:
            return bool(self._is_executing())
        except Exception:  # pragma: no cover
            log.warning("轮询读取 is_executing 失败，按未执行处理", exc_info=True)
            return False

    def _fire_stop_hotkey(self) -> None:
        if self._on_stop_hotkey is None:
            return
        try:
            self._on_stop_hotkey()
        except Exception:  # pragma: no cover
            log.debug("on_stop_hotkey 回调异常", exc_info=True)

    def _append(self, event: dict[str, Any]) -> None:
        with self._lock:
            if not self._recording:
                return
            self._events.append(event)
        if self._on_event is not None:
            try:
                self._on_event(dict(event))
            except Exception:  # pragma: no cover
                log.debug("on_event 回调异常", exc_info=True)

    def _notify_error(self, message: str) -> None:
        log.error(message)
        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:  # pragma: no cover
                log.debug("on_error 回调异常", exc_info=True)

    def _foreground_stem(self) -> str:
        """前台窗口所属进程的文件名主干（小写），取不到返回空串。"""
        user32, kernel32 = self._user32, self._kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        # PROCESS_QUERY_LIMITED_INFORMATION：普通权限即可查询，无需管理员
        handle = kernel32.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(512)
            buf = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)):
                return ""
            return Path(buf.value).stem.lower()
        finally:
            kernel32.CloseHandle(handle)
