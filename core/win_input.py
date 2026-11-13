"""Windows 底层输入注入（SendInput）：鼠标移动 + 键盘敲击。

为什么不能直接用 ``pynput.mouse.Controller().position = (x, y)``
------------------------------------------------------------------
该 setter 内部调用的是 ``SetCursorPos``：它只把光标挪到目标像素，
**绕过了 Windows 输入栈**——不产生 pointer 输入（``WM_POINTER*``），
也不经过低级钩子与原始输入。

需要说明的是：实测 ``SetCursorPos`` **确实会附带若干 ``WM_MOUSEMOVE``**，
所以普通 Win32 窗口看上去"能用"；但 WinUI/XAML 应用（Windows 11 的记事本、
资源管理器）的菜单悬停走 pointer 通道，瞬移对它们等于什么都没发生——
指针到了、菜单不亮、子菜单不展开，后续点击自然落空。而 Edge 这类
Chromium 应用吃 ``WM_MOUSEMOVE``，于是一直看起来正常。

本模块统一改走 ``SendInput``，带 ``MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
| MOUSEEVENTF_VIRTUALDESK``，让目标窗口收到与真实硬件移动完全一致的
输入事件流。``MOUSEEVENTF_VIRTUALDESK`` 是多屏必需项：只有它才能让绝对
坐标覆盖整个虚拟桌面（含左侧/上方的负坐标副屏）。

坐标说明
--------
``SendInput`` 的绝对坐标是 0..65535 的**归一化值**，需要按虚拟桌面矩形
换算；换算公式与 Windows 的反向映射存在 1px 级取整差异，因此
:func:`send_mouse_move` 发送后会读回 ``GetCursorPos`` 校验，必要时用
残差再补发一次（最多 2 次），保证落点精确。这对点击小目标很关键。
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes
from typing import Optional

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------------------
# Win32 常量
# ---------------------------------------------------------------------------
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

#: 归一化坐标的满量程（0..65535）
_ABS_MAX = 65535
#: 注入失败只在第一次告警（脚本有几千帧移动，逐帧打日志会淹没日志文件）
_warned_failure = False

#: 本程序注入的每一条事件都写进 ``dwExtraInfo`` 的标记（"MS"）。
#:
#: 为什么必须有它：键盘钩子不能再用 ``LLKHF_INJECTED`` 一刀切地放行注入事件。
#: 2026-11-12 实测——QQ 游戏大厅登录框在前台时，物理按键会被安全输入组件
#: **重新注入**（带 ``extra=0x21`` 且置了 ``INJECTED``），钩子若见注入就放行，
#: 热键就彻底哑了。改成认这个标记，既能只放行「我们自己敲的」（回放与快捷输入，
#: 否则会再次触发热键甚至递归），又能照常响应被别的软件中转过的真人按键。
INJECT_TAG = 0x4D53


# ---------------------------------------------------------------------------
# 结构体（64 位下 INPUT 共 40 字节）
# ---------------------------------------------------------------------------
_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


# ---------------------------------------------------------------------------
# 坐标换算
# ---------------------------------------------------------------------------
def virtual_screen_rect() -> tuple[int, int, int, int]:
    """返回虚拟桌面矩形 ``(left, top, width, height)``（物理像素）。

    失败时退化为主屏 ``(0, 0, 主屏宽, 主屏高)``，保证注入不会因为
    取不到指标而完全失效。
    """
    if not IS_WINDOWS:
        return 0, 0, 1920, 1080
    try:
        user32 = ctypes.windll.user32
        left = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
        top = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
        width = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        height = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    except Exception:  # pragma: no cover - 依赖系统 API
        left, top, width, height = 0, 0, 1920, 1080
    if width <= 0 or height <= 0:
        width, height = 1920, 1080
    return left, top, width, height


def to_absolute_xy(x: int, y: int) -> tuple[int, int]:
    """把屏幕像素坐标换算成 ``SendInput`` 需要的 0..65535 归一化坐标。

    Windows 的反向映射是 ``像素 = floor(归一化 * 宽 / 65536)``，
    因此正向要取 **向上取整** 才能落在同一个像素上；用 ``round`` 在
    约 4% 的坐标上会差 1px（实测 5120 宽屏上 45 点错 2 点），
    对小图标、菜单项的点击是能感觉到的偏移。
    """
    left, top, width, height = virtual_screen_rect()
    width = max(1, width)
    height = max(1, height)
    # ceil((p - left) * 65536 / span)，纯整数运算避免浮点误差
    nx = ((int(x) - left) * 65536 + width - 1) // width
    ny = ((int(y) - top) * 65536 + height - 1) // height
    return max(0, min(_ABS_MAX, nx)), max(0, min(_ABS_MAX, ny))


# ---------------------------------------------------------------------------
# 注入
# ---------------------------------------------------------------------------
def send_mouse_move(x: int, y: int) -> bool:
    """用 ``SendInput`` 把光标移动到屏幕坐标 ``(x, y)``。

    与 ``SetCursorPos`` 的本质差别：这是一条真实的输入事件，会进入
    Windows 输入栈（包括 pointer 通道），因此 WinUI/XAML 应用的
    悬停跟踪、菜单高亮、子菜单展开才会生效。

    返回 True 表示事件已被系统接收。

    注意：**不要**在发送后立刻读回 ``GetCursorPos`` 做校验——输入是
    异步处理的，读回往往还是上一帧的位置，据此重发只会让光标
    落后更多（实测反而放大误差）。落点精度由 :func:`to_absolute_xy`
    的整数换算保证。
    """
    if not IS_WINDOWS:  # pragma: no cover - 仅为可移植性
        return False
    nx, ny = to_absolute_xy(int(x), int(y))
    return _send_norm(nx, ny)


def _send_norm(nx: int, ny: int) -> bool:
    """按归一化坐标发送一条绝对移动事件（内部使用）。"""
    inp = INPUT(
        type=INPUT_MOUSE,
        u=_INPUTUNION(mi=MOUSEINPUT(
            dx=nx,
            dy=ny,
            mouseData=0,
            dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            time=0,
            dwExtraInfo=INJECT_TAG,
        )),
    )
    try:
        sent = _call_send_input(inp)
    except Exception:  # pragma: no cover - 依赖系统 API
        _warn_once("SendInput 调用失败")
        log.debug("SendInput 调用异常", exc_info=True)
        return False
    if sent != 1:
        _warn_once("SendInput 未注入鼠标移动事件", detail=ctypes.GetLastError())
        return False
    return True


def window_rect(hwnd: int):
    """窗口矩形 ``(left, top, right, bottom)``（屏幕坐标）；取不到返回 ``None``。

    只用于日志与 :func:`cursor_in_window`——出问题时能一眼看出「指针在哪、
    窗口在哪」，比只写「不在窗口内」有用得多。
    """
    if not IS_WINDOWS or not hwnd:  # pragma: no cover - 仅为可移植性
        return None
    rect = wintypes.RECT()
    try:
        ok = ctypes.windll.user32.GetWindowRect(
            wintypes.HWND(int(hwnd)), ctypes.byref(rect))
    except Exception:  # pragma: no cover - 依赖系统 API
        return None
    if not ok:
        return None
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def cursor_in_window(hwnd: int) -> bool:
    """指针是否落在某个窗口的矩形内。

    补点击之前必须先问这一句：指针若停在别的程序上，那一下左键会把焦点
    **抢走**，字符就敲到别处去了（甚至敲进别人的输入框）。宁可跳过点击、
    老实写日志，也不能乱点。
    """
    pos = get_cursor_pos()
    rect = window_rect(hwnd)
    if pos is None or rect is None:  # pragma: no cover - 依赖系统 API
        return False
    x, y = pos
    left, top, right, bottom = rect
    return left <= x < right and top <= y < bottom


def click_left(hold_ms: int = 10) -> bool:
    """在**当前指针位置**补一次左键单击（不移动指针）。

    为什么需要这一步：QQ、腾讯微云、WeGame 的登录框都是页面内的输入框
    （CEF/Electron 自绘），只有收到**真实的鼠标按下**才把键盘焦点交给页面里
    那个 ``input``；只发键盘事件时它们认为「页面还没聚焦」，按键直接被丢掉
    —— 而且 ``SendInput`` 照样返回成功，日志上看不出任何异常。

    参考工具（非粘贴型密码框辅助输入工具 v3.1.2）就是「按热键 → 先点一下
    左键 → 再敲字」，用户用它时必须先把指针放到输入框上。2026-11-12 实测：
    QQ 登录、腾讯微云只有它进得去，宏匠补上这一步之后才对齐。
    """
    if not IS_WINDOWS:  # pragma: no cover - 仅为可移植性
        return False
    ok = True
    for flag in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        inp = INPUT(
            type=INPUT_MOUSE,
            u=_INPUTUNION(mi=MOUSEINPUT(
                dx=0, dy=0, mouseData=0, dwFlags=flag, time=0,
                dwExtraInfo=INJECT_TAG,
            )),
        )
        try:
            sent = _call_send_input(inp)
        except Exception:  # pragma: no cover - 依赖系统 API
            log.debug("SendInput 点击异常", exc_info=True)
            return False
        ok = ok and sent == 1
        if flag == MOUSEEVENTF_LEFTDOWN:
            time.sleep(max(0, int(hold_ms)) / 1000.0)
    return ok


def _warn_once(message: str, detail: object = None) -> None:
    """注入失败告警只打一次：几千帧移动逐帧打日志会把日志文件淹掉。"""
    global _warned_failure
    if _warned_failure:
        return
    _warned_failure = True
    if detail is None:
        log.warning("%s（后续同类告警已省略）", message)
    else:
        log.warning("%s（返回/错误码 %s，后续同类告警已省略）", message, detail)


#: 原生注入入口缓存：``None`` = 还没探过，``False`` = 本机没有这个导出
_native_entry: object = None


def native_send_input():
    """取 ``win32u!NtUserSendInput``；本机没有则返回 ``None``。

    为什么值得绕这一层：``user32!SendInput`` 是个**用户态**导出，任何进程都
    能在自己的注入 DLL 里给它下 inline hook / IAT hook。带保护模块的程序
    （QQ、QQ 游戏大厅、部分安全控件）正是这么干的——被 hook 之后调用会**立刻
    返回成功**，事件一个都不进系统，``GetLastError`` 还给你 0。这正好对上
    实测现象（返回 True/True、错误码 0、``GetLastInputInfo`` 一动不动）。

    ``win32u.dll`` 是 user32 的下一层（Windows 10 1809 起导出
    ``NtUserSendInput``），参数与返回值跟 ``SendInput`` 完全一致，hook 它要
    难得多。取不到就老实退回标准入口，不影响任何现有行为。
    """
    global _native_entry
    if _native_entry is not None:
        return _native_entry or None
    if not IS_WINDOWS:  # pragma: no cover - 仅为可移植性
        _native_entry = False
        return None
    for module in ("win32u.dll", "user32.dll"):
        try:
            func = getattr(ctypes.WinDLL(module), "NtUserSendInput")
        except (OSError, AttributeError):  # pragma: no cover - 取决于系统版本
            continue
        try:
            func.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
            func.restype = wintypes.UINT
        except Exception:  # pragma: no cover
            continue
        _native_entry = func
        log.debug("输入注入入口：%s!NtUserSendInput", module)
        return func
    _native_entry = False
    log.debug("本机未导出 NtUserSendInput，改用 user32!SendInput")
    return None


def _call_send_input(inp: INPUT) -> int:
    """真正下发一条输入事件（单独抽出，便于测试替换）。

    先走 ``win32u!NtUserSendInput`` 绕开 user32 上的用户态 hook，成功不了
    再退回标准 ``SendInput``——两条路都失败时返回值也是 0，调用方据此告警。
    """
    native = native_send_input()
    if native is not None:
        try:
            sent = int(native(1, ctypes.byref(inp), ctypes.sizeof(INPUT)))
            if sent >= 1:
                return sent
        except Exception:  # pragma: no cover - 依赖系统 API
            log.debug("NtUserSendInput 调用异常，退回 SendInput", exc_info=True)
    return int(ctypes.windll.user32.SendInput(
        1, ctypes.byref(inp), ctypes.sizeof(INPUT)))


def get_cursor_pos() -> Optional[tuple[int, int]]:
    """读取当前光标坐标，失败返回 None。"""
    if not IS_WINDOWS:  # pragma: no cover
        return None
    point = wintypes.POINT()
    try:
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return int(point.x), int(point.y)
    except Exception:  # pragma: no cover
        log.debug("GetCursorPos 调用失败", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# 键盘注入（供「快捷输入」逐字符敲击使用）
# ---------------------------------------------------------------------------
def send_key(vk: int, up: bool = False) -> bool:
    """发送一次键盘按下（``up=False``）或抬起（``up=True``）事件。

    只给 ``wVk``，不给 ``wScan``、也不带 ``KEYEVENTF_SCANCODE``：
    系统会用当前键盘布局把虚拟键码翻译成扫描码并补上字符消息
    （``WM_CHAR``），这才是目标输入框能收到文字的原因。
    如果自己填 ``wScan``，在非美式布局下会打出别的字符。

    按下与抬起分两次调用，中间由调用方插入延时——一次性打包多条
    ``INPUT`` 虽然更快，但某些输入法/远程桌面会把整批当成粘包丢掉。
    """
    if not IS_WINDOWS:  # pragma: no cover - 仅为可移植性
        return False
    inp = INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(ki=KEYBDINPUT(
            wVk=int(vk),
            wScan=0,
            dwFlags=KEYEVENTF_KEYUP if up else 0,
            time=0,
            dwExtraInfo=INJECT_TAG,
        )),
    )
    try:
        sent = _call_send_input(inp)
    except Exception:  # pragma: no cover - 依赖系统 API
        _warn_once("SendInput 调用失败（键盘）")
        log.debug("SendInput 键盘注入异常", exc_info=True)
        return False
    if sent != 1:
        _warn_once("SendInput 未注入键盘事件", detail=ctypes.GetLastError())
        return False
    return True


def send_key_scancode(vk: int, up: bool = False) -> bool:
    """按「物理按键」下发：只填**扫描码**并带 ``KEYEVENTF_SCANCODE``。

    与 :func:`send_key` 的差别在键码字段：这条走 ``wScan``、``wVk`` 留 0，
    效果最接近真人按下一个物理键（系统仍会照常做布局翻译、补 ``WM_CHAR``）。
    存在两个通道是有意义的——部分反注入模块只按标志位过滤，例如只丢掉
    ``KEYEVENTF_UNICODE`` 的注入，而对带扫描码的按键放行；反过来也有只拦
    扫描码、不与字符通道较劲的。哪条能过只能试，所以两条都留。

    ``vk`` 取不到扫描码时返回 False（不会退化成 ``send_key``，免得调用方
    以为走的是扫描码通道却在日志里看到另一种行为）。
    """
    if not IS_WINDOWS:  # pragma: no cover - 仅为可移植性
        return False
    scan = _vk_to_scan(int(vk))
    if not scan:
        return False
    inp = INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(ki=KEYBDINPUT(
            wVk=0,
            wScan=scan,
            dwFlags=KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0),
            time=0,
            dwExtraInfo=INJECT_TAG,
        )),
    )
    try:
        sent = _call_send_input(inp)
    except Exception:  # pragma: no cover - 依赖系统 API
        _warn_once("SendInput 调用失败（扫描码）")
        log.debug("SendInput 扫描码注入异常", exc_info=True)
        return False
    if sent != 1:
        _warn_once("SendInput 未注入扫描码事件", detail=ctypes.GetLastError())
        return False
    return True


#: ``MapVirtualKeyW`` 的 ``uMapType``：虚拟键码 -> 扫描码
_MAPVK_VK_TO_VSC = 0


def _vk_to_scan(vk: int) -> int:
    """虚拟键码 -> 扫描码（0 表示该键码没有物理键）。"""
    try:
        return int(ctypes.windll.user32.MapVirtualKeyW(int(vk), _MAPVK_VK_TO_VSC)) & 0xFF
    except Exception:  # pragma: no cover - 依赖系统 API
        return 0


def send_physical_key(vk: int, up: bool = False) -> bool:
    """按「一把真键盘」的样子下发：**同时**填 ``wVk`` 与 ``wScan``，不带标志位。

    这是三条注入通道里最像硬件的形态，也是 ``keybd_event``（AutoIt 的
    ``Send()``）内部的行为。为什么要专门做这一条：

    * :func:`send_char` 是 ``wVk=0`` + 字符塞进 ``wScan``——低级钩子读到的
      ``scanCode`` 是个 ASCII 码，跟任何真实键位都对不上；
    * :func:`send_key_scancode` 是 ``wVk=0`` + 真扫描码——钩子读到的
      ``vkCode`` 是 **0**；
    * 这条两个字段都填准确，钩子看到的 ``vkCode`` / ``scanCode`` 与真人敲键
      完全一致，只有 ``LLKHF_INJECTED`` 位不同。

    所以当目标程序在**应用层**校验「键码与扫描码是否匹配」时，前两条会被
    当成假按键丢掉，只有这条能过。代价是受输入法与键盘布局影响（与 ``vk``
    通道相同），因此不作为默认通道，只在 ``method="physical"`` 时使用。
    """
    if not IS_WINDOWS:  # pragma: no cover - 仅为可移植性
        return False
    inp = INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(ki=KEYBDINPUT(
            wVk=int(vk),
            wScan=_vk_to_scan(int(vk)),
            dwFlags=KEYEVENTF_KEYUP if up else 0,
            time=0,
            dwExtraInfo=INJECT_TAG,
        )),
    )
    try:
        sent = _call_send_input(inp)
    except Exception:  # pragma: no cover - 依赖系统 API
        _warn_once("SendInput 调用失败（物理按键）")
        log.debug("SendInput 物理按键注入异常", exc_info=True)
        return False
    if sent != 1:
        _warn_once("SendInput 未注入物理按键事件", detail=ctypes.GetLastError())
        return False
    return True


def send_char(ch: str, up: bool = False) -> bool:
    """发送一次「字符」按下（``up=False``）或抬起（``up=True``）事件。

    用 ``KEYEVENTF_UNICODE``：``wVk`` 留 0，字符本身放进 ``wScan``，系统直接
    把字符塞进消息队列，**完全不经过键盘布局与输入法**。这是实测换来的：

    * 中文输入法处于中文态时，按 vk 发的 ``b`` 被拼成「杯」、``;`` 变成「；」——
      整段密码报废（tkinter/记事本都复现）；
    * 键盘布局不是美式时（qwertz/dvorak），同一个 vk 打出的是别的字符；
    * ``CapsLock`` 亮着时，vk + Shift 的大小写会翻过来，而字符注入不受影响。

    代价：只认键码、不处理 ``WM_CHAR`` 的程序（部分游戏引擎）收不到字符。
    这类程序本来也常常直接屏蔽 ``SendInput``，因此默认仍用本函数；
    需要时可用 :func:`send_key` 的键码通道（见 ``quick_input`` 的 ``method``）。
    """
    if not IS_WINDOWS:  # pragma: no cover - 仅为可移植性
        return False
    if len(ch) != 1:
        return False
    inp = INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(ki=KEYBDINPUT(
            wVk=0,
            wScan=ord(ch),
            dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0),
            time=0,
            dwExtraInfo=INJECT_TAG,
        )),
    )
    try:
        sent = _call_send_input(inp)
    except Exception:  # pragma: no cover - 依赖系统 API
        _warn_once("SendInput 调用失败（字符）")
        log.debug("SendInput 字符注入异常", exc_info=True)
        return False
    if sent != 1:
        _warn_once("SendInput 未注入字符事件", detail=ctypes.GetLastError())
        return False
    return True


# ---------------------------------------------------------------------------
# 注入可用性探测
# ---------------------------------------------------------------------------
#: 探测键：**左 Shift**。
#:
#: 原来用的是 F24（0x87），踩过一个大坑：F24 是自动化工具的招牌按键，
#: 带反注入驱动的程序会**专门过滤它**。2026-11-12 实测——QQ / 腾讯微云 /
#: WeGame 的登录框上，两条通道的探针都报「注入被系统丢弃」，于是自动通道
#: 直接跳到窗口消息，一个字符都没敢走注入；而同一台机器上参考工具（从不探针）
#: 明明能敲进去。Shift 单按不产生字符，对界面无副作用，也不会被针对性过滤。
_PROBE_VK = 0x10
#: 探针按下后等待系统处理的时间（秒）
_PROBE_WAIT = 0.012
#: 探测失败重试次数：一次失败可能是系统繁忙，两次都读不到才算被拦
_PROBE_TRIES = 2


def last_input_tick() -> int:
    """系统「最近一次输入」的时间戳（毫秒）；取不到返回 0。

    这是判断「注入到底进没进系统」最硬的证据，而且**与按键无关**：
    只要有一丁点输入进了输入栈它就跳。``tools/watch_input.py`` 用的也是它。
    """
    if not IS_WINDOWS:  # pragma: no cover - 仅为可移植性
        return 0

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    try:
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return int(info.dwTime)
    except Exception:  # pragma: no cover - 依赖系统 API
        pass
    return 0


def injection_delivered(*, scancode: bool = False) -> bool:
    """注入一次探针键，确认事件**真的进了系统**。

    为什么不能只看 ``SendInput`` 的返回值：``SendInput`` 在反注入驱动下会
    **返回成功却什么都不到**，只看返回值等于白打。

    判据有两条，任一条成立就算「进去了」：

    1. 按住探针键时 ``GetAsyncKeyState`` 的高位为 1；
    2. 注入前后 ``GetLastInputInfo`` 的时间戳变了。

    ★ 第二条是 2026-11-12 补的。原来只有第一条，而探针键用的是 F24——
    被驱动针对性过滤时，两条通道都报「被丢弃」，自动通道直接跳窗口消息。
    换成 Shift 之后再加时间戳判据，才能区分「这个键被过滤」和「注入不通」。

    ``scancode=True`` 时用 :func:`send_key_scancode` 探——两条通道的过滤
    规则可能不同（有的程序只拦字符注入，有的只拦扫描码），所以必须分开问。
    """
    if not IS_WINDOWS:  # pragma: no cover - 仅为可移植性
        return True
    user32 = ctypes.windll.user32
    send = send_key_scancode if scancode else send_key
    try:
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
    except Exception:  # pragma: no cover
        pass
    for _ in range(_PROBE_TRIES):
        tick_before = last_input_tick()
        try:
            send(_PROBE_VK, False)
            time.sleep(_PROBE_WAIT)
            down = int(user32.GetAsyncKeyState(_PROBE_VK))
        except Exception:  # pragma: no cover - 依赖系统 API
            log.debug("注入探测异常", exc_info=True)
            return True
        finally:
            try:
                send(_PROBE_VK, True)
            except Exception:  # pragma: no cover
                pass
        if down & 0x8000:
            return True
        # 第二条判据：键状态读不到，但输入时间戳在动，说明事件确实进了输入栈
        # （只是这个键被目标程序/驱动忽略了，跟通道通不通是两件事）。
        if last_input_tick() != tick_before:
            log.info("%s通道的探针键被忽略，但事件已进系统，仍可用注入",
                     "扫描码" if scancode else "字符")
            return True
    log.info("注入事件被系统丢弃（%s通道）",
             "扫描码" if scancode else "字符")
    return False


# ---------------------------------------------------------------------------
# 权限诊断（探针工具与设置页提示用）
# ---------------------------------------------------------------------------
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_INTEGRITY_LEVEL = 25

#: 完整性级别 RID -> 中文名
_INTEGRITY_NAMES = {
    0x0000: "不可信", 0x1000: "低", 0x2000: "中等",
    0x3000: "高", 0x4000: "系统", 0x5000: "受保护",
}


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


def is_elevated() -> bool:
    """本进程是否以管理员身份运行。"""
    if not IS_WINDOWS:  # pragma: no cover
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover
        return False


def process_integrity(pid: int) -> tuple[str, int]:
    """查询进程完整性级别，返回 ``(中文名, RID)``；查不到返回 ``("", 0)``。

    这条信息能一句话定死「投不进去」的原因：本程序是「中等」而目标程序是
    「高」，那 ``SendInput`` 和窗口消息都会被 UIPI 静默丢掉，唯一解是把
    宏匠也以管理员身份启动——换多少种注入方式都没用。
    """
    if not IS_WINDOWS or not pid:  # pragma: no cover
        return "", 0
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD))
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetSidSubAuthorityCount.argtypes = (ctypes.c_void_p,)
        advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi32.GetSidSubAuthority.argtypes = (ctypes.c_void_p, wintypes.DWORD)
        advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

        process = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not process:
            return "", 0
        token = wintypes.HANDLE()
        try:
            if not advapi32.OpenProcessToken(process, _TOKEN_QUERY,
                                            ctypes.byref(token)):
                return "", 0
            size = wintypes.DWORD(0)
            advapi32.GetTokenInformation(
                token, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
            if size.value <= 0:
                return "", 0
            buf = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                    token, _TOKEN_INTEGRITY_LEVEL, buf, size.value,
                    ctypes.byref(size)):
                return "", 0
            label = ctypes.cast(buf, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents
            sid = label.Label.Sid
            count = advapi32.GetSidSubAuthorityCount(sid)
            if not count or count.contents.value == 0:
                return "", 0
            rid = int(advapi32.GetSidSubAuthority(
                sid, count.contents.value - 1).contents.value)
            return _INTEGRITY_NAMES.get(rid, f"0x{rid:x}"), rid
        finally:
            if token:
                kernel32.CloseHandle(token)
    except Exception:  # pragma: no cover - 依赖系统 API
        log.debug("查询进程完整性级别失败", exc_info=True)
        return "", 0
    finally:
        try:
            kernel32.CloseHandle(process)
        except Exception:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# 供 player 使用的鼠标控制器
# ---------------------------------------------------------------------------
def make_sendinput_mouse():
    """构造一个「移动走 SendInput」的鼠标控制器。

    ``pynput`` 的 ``Controller`` 除移动以外的能力（按键、滚轮）原样保留，
    只把 ``_position_set`` 换成 SendInput 版本 —— 这样 player 里
    ``ctl.position = (x, y)`` 的写法不用改，底层却换成了真实事件流。
    延迟导入 pynput，避免本模块在没有 Qt/pynput 的纯 CLI 场景下被强依赖。
    """
    from pynput import mouse as _mouse

    class SendInputMouse(_mouse.Controller):
        """移动通过 SendInput 注入的鼠标控制器。"""

        def _position_set(self, pos) -> None:
            x, y = int(pos[0]), int(pos[1])
            if not send_mouse_move(x, y):
                # 兜底：极端情况下（例如被安全软件拦截 SendInput）至少
                # 还要让指针到达目标，否则整个脚本会全盘落空。
                try:
                    ctypes.windll.user32.SetCursorPos(x, y)
                except Exception:  # pragma: no cover
                    log.debug("SetCursorPos 兜底失败", exc_info=True)
            try:
                self._emit("on_move", x, y, True)
            except Exception:  # pragma: no cover - 无监听器时
                pass

    return SendInputMouse()
