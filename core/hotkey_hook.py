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
* **只放行「本程序自己注入」的按键**（``dwExtraInfo == INJECT_TAG``），
  否则回放/快捷输入敲出的键会再次触发热键甚至递归。★ 不能按
  ``LLKHF_INJECTED`` 一刀切——有的安全软件会把**真人按键重新注入**再往下传，
  那样热键会被误当成「自己敲的」而彻底失效（见 :func:`should_passthrough`）；
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

#: 本程序注入事件时写进 ``dwExtraInfo`` 的标记（"MS"）。定义在
#: ``core.win_input``——那是唯一会调 ``SendInput`` 的地方，钩子在这里认领
#: 「自己敲的键」。win_input 不反向依赖本模块，不存在循环导入。
from core.win_input import INJECT_TAG

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


def should_passthrough(flags: int, extra_info: int = 0) -> bool:
    """这条键盘事件要不要原样放行（不参与热键判断）。

    ★ **只按 ``dwExtraInfo`` 认领自己注入的事件**，别的全部当真人按键。

    历史（这条规则被实测推翻过两次，别再改回去）：

    1. 最初「``LLKHF_INJECTED`` 一律放行」→ 2026-11-12 实测 QQ 游戏大厅
       把真人按下的 F2/F3 用 ``[INJECTED] extra=0x21`` 重发，热键永远等不到。
    2. 改成「自己注入的 + ``LLKHF_LOWER_IL_INJECTED`` 一律放行」→ 同一天
       19:24 在 QQ 登录框上又栽同样的跟头：**宏匠是管理员，QQ 是普通权限**，
       于是 QQ 重发的真人按键带上了 ``LOWER_IL_INJECTED``（``flags=0x12``），
       被这条规则当成「别人的自动化」放走，日志里连一行快捷输入都没有。
       而同一次按住 F3 偶尔以 ``flags=0x0000``（纯硬件）到达，就一切正常——
       这正好解释了「同一条热键时灵时不灵」。

    结论：``INJECTED`` / ``LOWER_IL_INJECTED`` 两个标志位都**不能**用来判断
    「是不是我自己敲的」，只有 ``dwExtraInfo`` 里那个标记能（见 ``INJECT_TAG``）。

    为什么可以安全地取消「更低权限注入一律放行」：那类事件本来就是**别的进程
    发出来的按键**，跟我们无关，按真人按键处理最接近事实；真正需要防的
    「回放/快捷输入自我触发」由 :meth:`HotkeyManager._should_block_vk` 的
    ``is_executing`` 守卫负责（回放期间一律不认热键），与这里无关。
    """
    return (int(extra_info) & 0xFFFFFFFF) == INJECT_TAG

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

#: 快捷输入的热键（F1–F4）。这四个键即使**没匹配上**也要记一条日志：
#: 「按了没反应」有两种完全不同的原因——①钩子根本没收到事件（被上游的
#: 安全输入组件/驱动吃掉）；②钩子收到了但动作表里没有它（绑定丢了）。
#: 不打这一行，两种情况在日志里长得一模一样（都是空白）。
_QUICK_VKS = frozenset({0x70, 0x71, 0x72, 0x73})

#: 日志里显示的热键名（只是给人看的，缺的显示 ``?``）
_KEY_NAMES = {
    0x70: "F1", 0x71: "F2", 0x72: "F3", 0x73: "F4", 0x74: "F5",
    0x75: "F6", 0x76: "F7", 0x77: "F8", 0x78: "F9", 0x79: "F10",
    0x7A: "F11", 0x7B: "F12", 0x21: "PageUp", 0x22: "PageDown",
    0x24: "Home", 0x1B: "Esc",
}


def _foreground_class() -> str:
    """前台窗口类名（只用于日志，取不到返回空串）。"""
    try:
        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return ""
        buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, buf, 128)
        return buf.value
    except Exception:  # pragma: no cover - 诊断信息不影响主流程
        return ""

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
        #: 是否已经在这台机器上见过「别人注入的按键」。见一次就够——这条日志
        #: 是「热键为什么没反应」的第一现场（QQ 登录框那类安全输入会把真人
        #: 按键重新注入）。只打一次，别把日志刷爆。
        self._seen_foreign_inject = False

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
            is_down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
            is_up = message in (WM_KEYUP, WM_SYSKEYUP)

            # ★ 逐键记录（只在 ``--debug`` 下可见）。这是排「按了热键没反应」的
            # 第一现场：日志里**一条都没有**，说明事件根本没到本进程的钩子
            # （被上游的安全输入组件或驱动吃掉了）；有这一行但下面没有
            # 「热键动作」，才是绑定/匹配的问题。不记这一行，两种情况都是空白。
            if is_down and log.isEnabledFor(logging.DEBUG):
                log.debug("钩子收到按键 vk=%#06x flags=%#06x extra=%#x 前台=%r",
                          vk, int(info.flags), int(info.dwExtraInfo),
                          _foreground_class())

            # 只放行「自己注入的」与「更低权限注入的」，其余注入事件照常参与
            # 热键判断——有的安全软件会把真人按键重新注入（见 should_passthrough）。
            passthrough = should_passthrough(info.flags, int(info.dwExtraInfo))
            if info.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED):
                # 只在 DEBUG 记：这一行是排「热键没反应」的唯一现场证据
                # （谁注入的、带什么 extra、有没有被当成热键）。
                log.debug("注入事件 vk=%#06x flags=%#06x extra=%#x 放行=%s",
                          vk, int(info.flags), int(info.dwExtraInfo), passthrough)
                if not passthrough and not self._seen_foreign_inject:
                    self._seen_foreign_inject = True
                    log.info("检测到别的程序注入的按键（vk=%#06x extra=%#x）："
                             "按真人按键处理——某些安全输入组件会重新注入按键",
                             vk, int(info.dwExtraInfo))
            if passthrough:
                return user32.CallNextHookEx(None, n_code, w_param, l_param)

            if not (is_down or is_up) or vk in PROTECTED_VKS:
                return user32.CallNextHookEx(None, n_code, w_param, l_param)

            should = False
            try:
                should = bool(self._should_block(vk))
            except Exception:  # pragma: no cover - 回调异常不能中断钩子链
                log.debug("should_block 回调异常", exc_info=True)
                should = False

            # 快捷输入的热键（F1–F4）不论命中与否都留一行现场证据：
            # 只有这一行能区分「钩子没收到」和「收到了但没匹配」。
            if is_down and (should or vk in _QUICK_VKS):
                log.info("钩子看到按键 vk=%#06x（%s）命中=%s 注入=%s extra=%#x 前台=%r",
                         vk, _KEY_NAMES.get(vk, "?"), should,
                         bool(info.flags & LLKHF_INJECTED),
                         int(info.dwExtraInfo), _foreground_class())

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
