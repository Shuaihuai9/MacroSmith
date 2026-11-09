"""Windows 底层鼠标注入（SendInput）。

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
from ctypes import wintypes
from typing import Optional

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------------------
# Win32 常量
# ---------------------------------------------------------------------------
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

#: 归一化坐标的满量程（0..65535）
_ABS_MAX = 65535
#: 注入失败只在第一次告警（脚本有几千帧移动，逐帧打日志会淹没日志文件）
_warned_failure = False


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
            dwExtraInfo=0,
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


def _call_send_input(inp: INPUT) -> int:
    """真正的 ``SendInput`` 调用（单独抽出，便于测试替换）。"""
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
