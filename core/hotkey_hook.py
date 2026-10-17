"""Windows 低级键盘钩子（``WH_KEYBOARD_LL``）：热键独占。

为什么需要它
------------
pynput 的 ``Listener`` 只是**旁观**按键（挂一个低级钩子读取事件），
无法阻止事件继续传给前台程序。于是 F6 既会触发 MacroSmith 的回放，
又会被浏览器等程序当成自己的快捷键。

本模块用 ctypes 直接调用 Win32 的 ``SetWindowsHookExW(WH_KEYBOARD_LL, ...)``，
在钩子回调里对已注册的热键直接返回 1（非 0）**吃掉事件**，
系统就不会再把它派发给其它窗口。

要点
----
* 钩子回调运行在自己的消息循环线程里，不占用 Qt 主线程；
* 只拦截「已注册 + 本次没被吃掉」的虚拟键；其它按键原样放行；
* **程序自己注入**的按键（``LLKHF_INJECTED``）一律放行，
  否则会让模拟输入失效甚至递归；
* 卸载时先 ``UnhookWindowsHookEx`` 再 ``PostThreadMessageW(WM_QUIT)``
  结束消息循环，保证退出后系统键盘恢复正常。

权限说明
--------
Windows 会静默忽略「完整性级别低于前台窗口」的进程所装的钩子
（UIPI）。:func:`KeyboardHook.hook_alive` 通过
``SendMessageTimeout(HM_NCOM)`` 探测当前钩子是否已被系统摘掉，
界面据此提示用户「以管理员身份运行」或更换热键。
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

IS_WINDOWS = hasattr(ctypes, "windll")

# ---------------------------------------------------------------------------
# Win32 常量与结构
# ---------------------------------------------------------------------------
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

LLKHF_INJECTED = 0x00000010
LLKHF_LOWER_IL_INJECTED = 0x00000002

#: 判断钩子是否仍然有效（HM_NCOM，Chrome 与多个安全软件也用它做探活）
_HM_NCOM = 0x001D
_SMTO_ABORTIFHUNG = 0x0002
_SMTO_BLOCK = 0x0001

#: 不允许被独占拦截的“系统必留键”，避免把用户锁死在无法操作的状态
PROTECTED_VKS = frozenset({
    0x5B,  # LWin
    0x5C,  # RWin
    0x1B,  # Esc
})

if IS_WINDOWS:
    ULONG_PTR = wintypes.WPARAM

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        """低级键盘钩子回传的按键信息。"""

        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    _HOOKPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    )

    def _declare_win32(user32) -> None:
        """显式声明用到的 Win32 函数签名。

        ctypes 默认把整数参数当 32 位 ``c_int``，在 x64 上传递 64 位的
        ``LPARAM``/``HHOOK`` 会抛 ``OverflowError: int too long to convert``。
        低级钩子回调里每按一个键都会走到这里，必须声明准确。
        """
        user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        ]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD
        ]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
        ]
        user32.GetMessageW.restype = ctypes.c_int
        user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        ]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ulong),
        ]
        user32.SendMessageTimeoutW.restype = wintypes.LPARAM

    _WIN32_DECLARED = False
else:  # pragma: no cover - 非 Windows 平台占位
    KBDLLHOOKSTRUCT = None  # type: ignore[assignment]
    _HOOKPROC = None  # type: ignore[assignment]

    def _declare_win32(user32) -> None:  # noqa: ARG001
        return

    _WIN32_DECLARED = True


def _user32():
    """返回已声明好签名的 ``user32`` 句柄（幂等）。"""
    global _WIN32_DECLARED
    user32 = ctypes.windll.user32
    if not _WIN32_DECLARED:
        _declare_win32(user32)
        _WIN32_DECLARED = True
    return user32


class KeyboardHook:
    """低级键盘钩子：命中指定虚拟键时吞掉事件。

    参数
    ----
    on_hotkey:
        命中并**决定拦截**时回调 ``fn(vk)``（在钩子线程执行，
        调用方务必只做转发，不要直接碰界面）。
    should_block:
        ``fn(vk) -> bool``，返回是否需要独占拦截该键。
        每次按键都会问一次，便于运行时动态改键。
    """

    def __init__(self, on_hotkey: Optional[Callable[[int], None]] = None,
                 should_block: Optional[Callable[[int], bool]] = None) -> None:
        self._on_hotkey = on_hotkey
        self._should_block = should_block or (lambda vk: False)

        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._hook_handle: Optional[int] = None
        self._proc = None                 # 必须持引用，否则回调会被 GC 回收
        self._ready = threading.Event()
        self._running = False
        self._last_error = ""
        self._blocked_count = 0

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._running and self._hook_handle is not None

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def blocked_count(self) -> int:
        """已被吞掉的按键次数（用于自检与界面反馈）。"""
        return self._blocked_count

    # ------------------------------------------------------------------
    # 安装 / 卸载
    # ------------------------------------------------------------------
    def start(self, timeout: float = 2.0) -> bool:
        """在独立线程里装钩子并跑消息循环；返回是否装好。"""
        if not IS_WINDOWS:
            self._last_error = "仅支持 Windows"
            return False
        if self.is_running:
            return True

        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="MacroSmith-KeyHook",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self._last_error = "钩子安装超时"
            return False
        if self._hook_handle is None:
            self._last_error = self._last_error or "钩子安装失败"
            return False
        return True

    def stop(self, timeout: float = 1.0) -> None:
        """卸载钩子并结束消息循环，保证系统键盘恢复正常。"""
        handle = self._hook_handle
        thread_id = self._thread_id
        self._hook_handle = None
        self._running = False

        if handle and IS_WINDOWS:
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(handle)
            except Exception:  # pragma: no cover
                log.debug("UnhookWindowsHookEx 失败", exc_info=True)
        if thread_id and IS_WINDOWS:
            try:
                # 让消息循环退出；不 join，避免与关闭流程互相等待
                ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            except Exception:  # pragma: no cover
                log.debug("PostThreadMessageW 失败", exc_info=True)

        thread = self._thread
        if thread is not None and thread.is_alive() and timeout > 0:
            thread.join(timeout=timeout)
        self._thread = None
        self._thread_id = None
        self._proc = None

    # ------------------------------------------------------------------
    # 探活
    # ------------------------------------------------------------------
    def hook_alive(self) -> bool:
        """探测钩子是否仍被系统认可（未被 UIPI 摘掉）。"""
        if not IS_WINDOWS or self._thread_id is None:
            return False
        try:
            result = ctypes.c_ulong(0)
            _user32().SendMessageTimeoutW(
                wintypes.HWND(_HM_NCOM), 0, 0, 0,
                _SMTO_ABORTIFHUNG | _SMTO_BLOCK, 200, ctypes.byref(result),
            )
            return True
        except Exception:  # pragma: no cover
            return False

    def probe_missing_hotkey(self, vk: int, timeout_ms: int = 20) -> bool:
        """粗略探测某个键是否被更高权限的程序独占。

        通过 ``SendMessageTimeout`` 触发一次钩子链调用并计时：
        若钩子被 UIPI 禁用，调用会超时。这只作为**提示**依据，
        不作为硬性判断（游戏/输入法反作弊钩子也会导致类似现象）。
        """
        if not IS_WINDOWS:
            return False
        try:
            result = ctypes.c_ulong(0)
            winmm = ctypes.windll.winmm
            vk_state = ctypes.windll.user32.GetAsyncKeyState(vk)
            del vk_state          # 仅为触发一次查询，结果不参与判断
            winmm.timeGetTime.restype = wintypes.DWORD
            start = winmm.timeGetTime()
            ctypes.windll.user32.SendMessageTimeoutW(
                wintypes.HWND(_HM_NCOM), 0, 0, 0,
                _SMTO_ABORTIFHUNG | _SMTO_BLOCK, timeout_ms, ctypes.byref(result),
            )
            elapsed = winmm.timeGetTime() - start
            return elapsed >= timeout_ms
        except Exception:  # pragma: no cover
            return False

    # ------------------------------------------------------------------
    # 内部：线程体 / 回调
    # ------------------------------------------------------------------
    def _run(self) -> None:
        """钩子线程：装钩子 + 消息循环。"""
        try:
            user32 = _user32()
            kernel32 = ctypes.windll.kernel32

            self._thread_id = int(kernel32.GetCurrentThreadId())
            self._proc = _HOOKPROC(self._hook_proc)
            handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
            if not handle:
                self._last_error = f"SetWindowsHookExW 失败（错误码 {kernel32.GetLastError()}）"
                log.error(self._last_error)
                self._ready.set()
                return

            self._hook_handle = int(handle)
            self._running = True
            log.info("低级键盘钩子已安装（线程 %s）", self._thread_id)
            self._ready.set()

            # 消息循环：低级钩子必须靠消息泵才能收到回调
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:  # pragma: no cover - 依赖系统 API
            self._last_error = f"键盘钩子线程异常：{exc}"
            log.exception("键盘钩子线程异常")
            self._ready.set()
        finally:
            self._running = False

    def _hook_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        """钩子回调（钩子线程）：命中热键就吞掉事件。"""
        user32 = _user32()
        if n_code < 0 or n_code != HC_ACTION:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        try:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(info.vkCode)
            message = int(w_param)

            # 程序自己注入的按键一律放行，否则模拟输入会失效甚至递归
            if info.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED):
                return user32.CallNextHookEx(None, n_code, w_param, l_param)

            is_down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
            is_up = message in (WM_KEYUP, WM_SYSKEYUP)
            if not (is_down or is_up) or vk in PROTECTED_VKS:
                return user32.CallNextHookEx(None, n_code, w_param, l_param)

            should = False
            try:
                should = bool(self._should_block(vk))
            except Exception:  # pragma: no cover - 回调异常不能中断钩子链
                log.debug("should_block 回调异常", exc_info=True)
                should = False

            if not should:
                return user32.CallNextHookEx(None, n_code, w_param, l_param)

            # 独占：吞掉事件，不再传给其它程序。
            # 只在“按下”时触发动作，避免抬起时重复执行。
            self._blocked_count += 1
            if is_down and self._on_hotkey is not None:
                try:
                    self._on_hotkey(vk)
                except Exception:  # pragma: no cover
                    log.debug("on_hotkey 回调异常", exc_info=True)
            return 1
        except Exception:  # pragma: no cover - 防御性
            log.debug("键盘钩子回调异常", exc_info=True)
        return user32.CallNextHookEx(None, n_code, w_param, l_param)


def hook_supported() -> bool:
    """当前环境是否支持独占钩子。"""
    return IS_WINDOWS


def protected_vks() -> Iterable[int]:
    """返回不允许被独占的虚拟键（Win / Esc）。"""
    return PROTECTED_VKS
