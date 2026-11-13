"""窗口消息级文本注入：不经过输入栈的「第二套通道」。

为什么需要它
------------
``SendInput`` 是往**系统输入栈**里塞事件，再由系统派发给前台窗口。带内核
防护的程序（QQ 游戏大厅、各类游戏客户端、部分银行安全控件）会直接掐掉
注入输入。实测（2026-11-11，前台 ``QQMicroGameBox.exe``）：

* ``SendInput`` / ``keybd_event`` / ``mouse_event`` 全部无效；
* 自建 ``WH_KEYBOARD_LL`` 钩子收不到任何事件；
* ``GetAsyncKeyState`` 读回来是 0 —— 事件根本没进系统；
* 而 ``SetCursorPos`` 正常。

也就是说拦的是**入口**，不是**编码**：换键码、换扫描码、加 Shift 一律白费。
本模块换一层投递：``PostMessage(hwnd, WM_CHAR, ...)`` 直接把消息放进目标
窗口自己的消息队列。这条路不产生输入事件，``BlockInput`` 与反注入驱动都
拦不到；代价是只有「按消息处理文本」的界面才吃（Qt、DirectUI、
Electron/CEF、MFC 的绝大多数输入框都吃；纯 DirectInput 读键盘状态的
游戏引擎不吃，那种只能靠虚拟 HID 设备，得装驱动）。

三级阶梯
--------
:func:`type_text` 按目标控件类型选最稳的一级：

1. 标准 ``Edit`` / ``RichEdit`` → 读出原内容 + ``WM_SETTEXT`` 写回，
   再用 ``WM_GETTEXT`` **读回验证**（这两个消息由系统跨进程编组，安全）；
2. 其它控件（CEF / DirectUI / Qt）→ 逐字符 ``WM_KEYDOWN`` + ``WM_CHAR``
   + ``WM_KEYUP``；
3. 全失败 → 返回 0，由调用方决定是否回退 ``SendInput``。

目标窗口取**前台线程的焦点控件**（``GetGUIThreadInfo().hwndFocus``），
不是 ``GetForegroundWindow()``：Chromium/CEF 的焦点在子窗口
（``Chrome_RenderWidgetHostHWND``）上，往顶层窗投消息没人处理。
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes
from typing import Callable, Optional

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------------------
# Win32 常量
# ---------------------------------------------------------------------------
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_NULL = 0x0000
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E

ERROR_ACCESS_DENIED = 5

MAPVK_VK_TO_VSC = 0
GWL_STYLE = -16
ES_PASSWORD = 0x0020

#: 标准文本控件的窗口类名前缀（``RichEdit50W`` 之类都算）
_EDIT_CLASSES = ("Edit", "RichEdit", "RICHEDIT")

#: 单次读写控件文本的上限（字符）：登录框/密码框远小于这个值
_MAX_TEXT = 1024

if IS_WINDOWS:  # pragma: no branch - 非 Windows 下留空实现便于单测导入
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)
    _user32.PostMessageW.restype = wintypes.BOOL
    _user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)
    _user32.SendMessageW.restype = ctypes.c_ssize_t
    _user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
    _user32.MapVirtualKeyW.restype = wintypes.UINT
    _user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
    _user32.GetWindowLongW.restype = wintypes.LONG
    _user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND,
                                                 ctypes.POINTER(wintypes.DWORD))
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
else:  # pragma: no cover - 仅为可移植性
    _user32 = None


class GUITHREADINFO(ctypes.Structure):
    """``GetGUIThreadInfo`` 的入参结构（拿焦点控件用）。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


# ---------------------------------------------------------------------------
# 目标窗口
# ---------------------------------------------------------------------------
def focus_window() -> int:
    """返回当前**焦点控件**的窗口句柄，取不到返回 0。

    ``GetGUIThreadInfo`` 只做只读查询，不需要 ``AttachThreadInput``
    （不往对方线程塞队列，不会把它卡住）。焦点控件为空时退回前台窗口。
    """
    if not IS_WINDOWS:  # pragma: no cover
        return 0
    try:
        hwnd = int(_user32.GetForegroundWindow() or 0)
        if not hwnd:
            return 0
        tid = int(_user32.GetWindowThreadProcessId(hwnd, None) or 0)
        if tid:
            info = GUITHREADINFO()
            info.cbSize = ctypes.sizeof(GUITHREADINFO)
            if _user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
                focus = int(info.hwndFocus or 0)
                if focus:
                    return focus
        return hwnd
    except Exception:  # pragma: no cover - 依赖系统 API
        log.debug("读取焦点窗口失败", exc_info=True)
        return 0


def window_pid(hwnd: int) -> int:
    """窗口所属进程 ID（取不到返回 0）。"""
    if not IS_WINDOWS or not hwnd:  # pragma: no cover
        return 0
    pid = wintypes.DWORD(0)
    try:
        _user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)),
                                         ctypes.byref(pid))
    except Exception:  # pragma: no cover - 依赖系统 API
        return 0
    return int(pid.value)


def class_name(hwnd: int) -> str:
    """窗口类名（取不到返回空串）。"""
    if not IS_WINDOWS or not hwnd:  # pragma: no cover
        return ""
    buf = ctypes.create_unicode_buffer(256)
    try:
        int(_user32.GetClassNameW(int(hwnd), buf, 256))
    except Exception:  # pragma: no cover
        return ""
    return buf.value


def is_edit_control(hwnd: int) -> bool:
    """是否标准文本控件（``Edit`` / ``RichEdit*``）。"""
    name = class_name(hwnd)
    return any(name.startswith(prefix) for prefix in _EDIT_CLASSES)


def is_password_control(hwnd: int) -> bool:
    """是否密码框（``ES_PASSWORD`` 样式位），仅诊断用。"""
    if not IS_WINDOWS or not hwnd:  # pragma: no cover
        return False
    try:
        return bool(int(_user32.GetWindowLongW(int(hwnd), GWL_STYLE)) & ES_PASSWORD)
    except Exception:  # pragma: no cover
        return False


# ---------------------------------------------------------------------------
# 底层消息
# ---------------------------------------------------------------------------
def _post(hwnd: int, msg: int, wparam: int, lparam: int) -> bool:
    """投递一条窗口消息（异步）。"""
    if not IS_WINDOWS:  # pragma: no cover
        return False
    try:
        return bool(_user32.PostMessageW(
            wintypes.HWND(int(hwnd)), wintypes.UINT(int(msg)),
            wintypes.WPARAM(int(wparam)), wintypes.LPARAM(int(lparam))))
    except Exception:  # pragma: no cover
        log.debug("PostMessage 失败：msg=%#x", msg, exc_info=True)
        return False


def _send(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
    """同步发送一条窗口消息（``WM_GETTEXT`` / ``WM_SETTEXT`` 必须同步）。"""
    if not IS_WINDOWS:  # pragma: no cover
        return 0
    try:
        return int(_user32.SendMessageW(
            wintypes.HWND(int(hwnd)), wintypes.UINT(int(msg)),
            wintypes.WPARAM(int(wparam)), wintypes.LPARAM(int(lparam))))
    except Exception:  # pragma: no cover
        log.debug("SendMessage 失败：msg=%#x", msg, exc_info=True)
        return 0


def _scan_code(vk: int) -> int:
    """虚拟键码 -> 扫描码（0 表示该键码没有对应扫描码）。"""
    if not IS_WINDOWS:  # pragma: no cover
        return 0
    try:
        return int(_user32.MapVirtualKeyW(int(vk), MAPVK_VK_TO_VSC))
    except Exception:  # pragma: no cover
        return 0


def can_post(hwnd: int) -> tuple[bool, int]:
    """目标是否吃我们投递的消息；返回 ``(能否投递, 错误码)``。

    ★ **不要用 ``WM_NULL`` 探测**（本函数第一版就是这么写的，错了）：``WM_NULL``
    是标准的「这个窗口还活着吗」探针，**UIPI 明确放行它**，所以「低权限进程 → 高
    权限窗口」时它照样返回成功。2026-11-12 实测打脸：

    * ``PostMessage(hwnd, WM_NULL)`` → 成功（判定「可以投递」）
    * 紧接着 ``PostMessage(hwnd, WM_CHAR, 'T')`` → 全部失败，一个字都没进去

    真正可靠的判据是**完整性级别**：目标比自己高时，消息一定会被 UIPI 丢掉，
    这跟消息种类无关。所以这里先比级别，再补一次 ``WM_NULL`` 确认窗口还活着
    （不能响应消息的窗口也发不进去）。

    返回的两种失败要分开报：``ERROR_ACCESS_DENIED`` 是权限不够（换管理员身份即可），
    其它错误码是目标不接受消息（只能改用注入或复制粘贴）。
    """
    if not IS_WINDOWS:  # pragma: no cover
        return False, 0
    if not hwnd:
        return False, 0
    # 1) 权限：目标完整性级别更高 → 消息必被 UIPI 拦，不必再试
    try:
        from core.win_input import process_integrity

        theirs_name, theirs = process_integrity(window_pid(hwnd))
        _mine_name, mine = process_integrity(os.getpid())
        if theirs and mine and theirs > mine:
            log.warning("目标进程完整性级别（%s）高于本程序，窗口消息会被 UIPI 丢弃",
                        theirs_name or hex(theirs))
            return False, ERROR_ACCESS_DENIED
    except Exception:  # pragma: no cover - 诊断失败不该让功能停摆
        log.debug("比较完整性级别失败，退回消息探测", exc_info=True)
    # 2) 还活着吗
    if _post(hwnd, WM_NULL, 0, 0):
        return True, 0
    try:
        return False, int(ctypes.get_last_error())
    except Exception:  # pragma: no cover
        return False, 0


def key_lparam(vk: int, *, up: bool = False) -> int:
    """构造 ``WM_KEYDOWN`` / ``WM_KEYUP`` 的 lParam。

    位布局：0–15 重复次数、16–23 扫描码、24 扩展键、29 上下文码、
    30 上一状态、31 转换状态。扫描码必须真填 —— 不少 DirectUI 框架会读它，
    填 0 会被当成无效按键直接丢掉。
    """
    lp = 1 | ((_scan_code(vk) & 0xFF) << 16)
    if up:
        lp |= (1 << 30) | (1 << 31)
    return lp


# ---------------------------------------------------------------------------
# 读写控件文本（WM_GETTEXT / WM_SETTEXT 由系统跨进程编组，可安全使用）
# ---------------------------------------------------------------------------
def read_text(hwnd: int, *, send: Optional[Callable] = None) -> str:
    """读控件当前文本；读不到返回空串。

    密码框（``ES_PASSWORD``）读回来是**真实内容**——本工具本来就持有这个
    密码，读它是为了「写进去有没有生效」的验证，**绝不写日志**。
    """
    if not IS_WINDOWS or not hwnd:  # pragma: no cover
        return ""
    call = send or _send
    try:
        length = int(call(hwnd, WM_GETTEXTLENGTH, 0, 0))
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(_MAX_TEXT)
        n = min(length, _MAX_TEXT - 1)
        copied = int(call(hwnd, WM_GETTEXT, n + 1, ctypes.addressof(buf)))
        if copied <= 0:
            return ""
        return buf.value[:copied]
    except Exception:  # pragma: no cover
        log.debug("读取控件文本失败", exc_info=True)
        return ""


def set_text(hwnd: int, text: str, *, send: Optional[Callable] = None) -> bool:
    """把控件文本整体设为 ``text``（``WM_SETTEXT``）。"""
    if not IS_WINDOWS or not hwnd:  # pragma: no cover
        return False
    call = send or _send
    buf = ctypes.create_unicode_buffer(text, len(text) + 1)
    try:
        return bool(call(hwnd, WM_SETTEXT, 0, ctypes.addressof(buf)))
    except Exception:  # pragma: no cover
        return False


# ---------------------------------------------------------------------------
# 注入
# ---------------------------------------------------------------------------
def type_char(hwnd: int, ch: str, *, post: Optional[Callable] = None) -> bool:
    """把一个字符送进目标窗口：**只发** ``WM_CHAR``。

    ★ 不要顺手补 ``WM_KEYDOWN`` / ``WM_KEYUP``。实测（tkinter Entry）：

    * 只发 ``WM_CHAR`` → ``Ab3!@-;:Q`` 原样上屏；
    * 只发 ``WM_KEYDOWN`` → ``ab312-;;q``（没有 Shift，界面自己按 vk 翻译）；
    * 两条都发 → 先上屏 ``Ab3!@-;:Q``，紧跟一份 ``ab312-;;q``，**一个字打两遍**。

    也就是说「消息驱动」的界面会自己把按键消息再翻译成字符，补按键消息
    只会让文本重复。``WM_CHAR`` 才是「这里有一个字符」的正式通道。
    """
    from core.quick_input import char_to_stroke       # 延迟导入，避免循环依赖

    stroke = char_to_stroke(ch)
    if stroke is None:
        return False
    vk, _need_shift = stroke
    send = post or _post
    return bool(send(hwnd, WM_CHAR, ord(ch), key_lparam(vk)))


def type_text(text: object, *, hwnd: Optional[int] = None,
              delay_ms: int = 8,
              sleep: Optional[Callable[[float], None]] = None,
              should_stop: Optional[Callable[[], bool]] = None,
              post: Optional[Callable] = None,
              send: Optional[Callable] = None,
              verify: bool = True) -> int:
    """用窗口消息把文本送进焦点控件，返回真正送出的字符数。

    阶梯顺序见模块文档。``post`` / ``send`` 可注入替身，测试据此不碰真窗口。
    """
    from core.quick_input import clamp_delay, sanitize

    payload = sanitize(text)
    if not payload:
        return 0

    target = int(hwnd or 0) or focus_window()
    if not target:
        log.warning("窗口消息通道：找不到焦点控件，放弃注入")
        return 0

    allowed, error = (True, 0) if post is not None else can_post(target)
    if not allowed:
        if error == ERROR_ACCESS_DENIED:
            log.warning("窗口消息被拒绝（权限不足）：目标程序以更高权限运行，"
                        "请用「以管理员身份运行」启动本程序后重试")
        else:
            log.warning("窗口消息被拒绝（错误码 %s），目标可能不接受文本消息", error)
        return 0

    wait = sleep or time.sleep
    gap = max(0.0, clamp_delay(delay_ms) / 1000.0)

    # ---- 阶梯 1：标准文本控件 → 读原文 + WM_SETTEXT + 读回验证 ----
    if is_edit_control(target):
        before = read_text(target, send=send) if verify else ""
        if set_text(target, before + payload, send=send):
            if not verify:
                return len(payload)
            after = read_text(target, send=send)
            if after and after != before:
                return len(payload)
            log.debug("WM_SETTEXT 未被接受（可能自绘控件伪装成 Edit），改走逐字符")

    # ---- 阶梯 2：逐字符 WM_CHAR ----
    typed = 0
    for index, ch in enumerate(payload):
        if should_stop is not None and should_stop():
            return index
        try:
            if not type_char(target, ch, post=post):
                continue
        except Exception:  # pragma: no cover - 注入被拦时不让线程炸掉
            log.warning("窗口消息注入在第 %d 个字符处中断", index + 1, exc_info=True)
            return typed
        typed += 1
        if gap > 0 and index + 1 < len(payload):
            wait(gap)
    return typed
