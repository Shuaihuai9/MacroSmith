"""悬停注入自检：证明移动走的是真实输入事件流，而不是瞬移。

背景
----
``pynput`` 的 ``mouse.Controller().position = (x, y)`` 底层是
``SetCursorPos``：像素位置变了，但它**绕过整个 Windows 输入栈**，
既不产生指针（pointer）输入，也不经过低级钩子与原始输入。

Windows 11 的记事本、资源管理器是 WinUI/XAML 应用，它们的菜单栏
悬停走 pointer 输入（``WM_POINTERUPDATE``）；而 Edge 这类 Chromium
应用走 ``WM_MOUSEMOVE``。这就是「Edge 收藏栏悬停正常、记事本菜单
悬停不动」的由来。

本脚本创建一个探针窗口，把它切换到「鼠标输入提升为 pointer」
（``EnableMouseInPointer``，与 XAML 应用一致），然后分三种方式把指针
扫过窗口，用消息计数直接对比：

=====  =========================  ===============
方式    说明                       预期
=====  =========================  ===============
1      ``SetCursorPos`` 瞬移       pointer 0 条（旧实现的病根）
2      ``win_input`` SendInput     pointer 多条
3      ``MacroPlayer`` 原速回放     pointer 多条 + 点击落地
=====  =========================  ===============

用法::

    python tools/check_hover.py           # 约 2 秒，结束后自动恢复光标
    python tools/check_hover.py --keep    # 保留探针窗口（排查用）

退出码 0 表示全部符合预期。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.win_utils import enable_dpi_awareness  # noqa: E402

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSELEAVE = 0x02A3
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_POINTERUPDATE = 0x0245
WM_POINTERDOWN = 0x0246
WM_APP_PROMOTE = 0x8001

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
IDC_ARROW = 32512

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _declare_apis() -> None:
    """显式声明签名。

    不写 argtypes 时 ctypes 会把 64 位 lParam 按 C int 转换，
    指针值稍大就抛 ``OverflowError: int too long to convert``。
    """
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                   wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.EnableMouseInPointer.argtypes = [ctypes.c_bool]
    user32.EnableMouseInPointer.restype = ctypes.c_bool


class TrackerWindow:
    """只统计消息的小窗口（置顶、不抢焦点、无标题栏）。"""

    def __init__(self, x: int, y: int, width: int = 320, height: int = 200) -> None:
        _declare_apis()
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.moves = 0
        self.pointer_updates = 0
        self.downs = 0
        self.ups = 0
        self.pointer_down = 0
        self.hwnd: int | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._promoted = threading.Event()
        self._promote_ok = False
        self._proc = WNDPROC(self._wnd_proc)     # 必须持有引用，否则会被回收

    # -- 生命周期 ------------------------------------------------------
    def start(self, timeout: float = 5.0) -> None:
        self._thread = threading.Thread(target=self._run, name="HoverProbe",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("探针窗口创建超时")

    def stop(self) -> None:
        self._stop.set()
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(timeout=2.0)

    def enable_pointer_promotion(self, wait: float = 1.0) -> bool:
        """把窗口所在线程切到 pointer 输入模式（XAML 应用的行为）。

        ``EnableMouseInPointer`` 是**线程级**设置，必须在窗口线程里调用，
        所以这里通过自定义消息转交过去。
        """
        if not self.hwnd:
            return False
        self._promoted.clear()
        user32.PostMessageW(self.hwnd, WM_APP_PROMOTE, 0, 0)
        return self._promoted.wait(wait) and self._promote_ok

    def snapshot(self) -> dict[str, int]:
        return {
            "moves": self.moves,
            "pointer": self.pointer_updates,
            "downs": self.downs,
            "ups": self.ups,
            "pointer_down": self.pointer_down,
        }

    def delta(self, base: dict[str, int]) -> dict[str, int]:
        now = self.snapshot()
        return {key: now[key] - base[key] for key in now}

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    # -- 窗口线程 ------------------------------------------------------
    def _run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        cls_name = f"MacroSmithHoverProbe{id(self)}"
        wc = WNDCLASSW()
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.hCursor = user32.LoadCursorW(None, IDC_ARROW)
        wc.hbrBackground = ctypes.cast(5, wintypes.HBRUSH)   # COLOR_WINDOW + 1
        wc.lpszClassName = cls_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise RuntimeError(f"RegisterClassW 失败：{ctypes.GetLastError()}")

        self.hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            cls_name, "MacroSmith 悬停探针", WS_POPUP | WS_VISIBLE,
            self.x, self.y, self.width, self.height,
            None, None, hinst, None,
        )
        if not self.hwnd:
            raise RuntimeError(f"CreateWindowExW 失败：{ctypes.GetLastError()}")
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        self._ready.set()

        msg = wintypes.MSG()
        while not self._stop.is_set():
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        user32.DestroyWindow(self.hwnd)
        user32.UnregisterClassW(cls_name, hinst)
        self.hwnd = None

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_APP_PROMOTE:
            self._promote_ok = bool(user32.EnableMouseInPointer(True))
            self._promoted.set()
            return 0
        if msg == WM_MOUSEMOVE:
            self.moves += 1
        elif msg == WM_POINTERUPDATE:
            self.pointer_updates += 1
            return 0
        elif msg == WM_POINTERDOWN:
            self.pointer_down += 1
            return 0
        elif msg == WM_LBUTTONDOWN:
            self.downs += 1
        elif msg == WM_LBUTTONUP:
            self.ups += 1
        elif msg == WM_CLOSE:
            self._stop.set()
            user32.DestroyWindow(hwnd)
            return 0
        elif msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ---------------------------------------------------------------------------
# 三种注入方式
# ---------------------------------------------------------------------------
def _sweep(win: TrackerWindow, frames: int = 24) -> list[tuple[int, int]]:
    """在窗口中部横向扫一条线。"""
    span = win.width - 20
    left = win.x + 10
    y = win.y + win.height // 2
    return [(left + round(span * i / max(1, frames - 1)), y) for i in range(frames)]


def sweep_setcursorpos(pts: list[tuple[int, int]], gap: float = 0.012) -> None:
    """旧实现：每个点一次 SetCursorPos。"""
    for (x, y) in pts:
        user32.SetCursorPos(int(x), int(y))
        time.sleep(gap)


def sweep_sendinput(pts: list[tuple[int, int]], gap: float = 0.012) -> None:
    """新实现：每个点一次 SendInput 绝对移动。"""
    from core.win_input import send_mouse_move
    for (x, y) in pts:
        send_mouse_move(int(x), int(y))
        time.sleep(gap)


def replay(pts: list[tuple[int, int]], gap_ms: int = 12) -> float:
    """真回放：MacroPlayer 原速跑一段「轨迹 + 点击」。"""
    from core.player import MacroPlayer

    events: list[dict] = []
    for index, (x, y) in enumerate(pts):
        events.append({"t": index * gap_ms, "type": "mouse_move", "x": x, "y": y})
    click_t = len(pts) * gap_ms
    end_x, end_y = pts[-1]
    events.append({"t": click_t, "type": "mouse_click", "action": "press",
                   "button": "left", "x": end_x, "y": end_y})
    events.append({"t": click_t + 60, "type": "mouse_click", "action": "release",
                   "button": "left", "x": end_x, "y": end_y})

    player = MacroPlayer()
    player.load(events, "hover-probe")
    start = time.monotonic()
    player.start_timed()
    while player.is_replaying:
        time.sleep(0.01)
    return time.monotonic() - start


def main() -> int:
    parser = argparse.ArgumentParser(description="MacroSmith 悬停注入自检")
    parser.add_argument("--keep", action="store_true", help="结束后保留探针窗口")
    args = parser.parse_args()

    enable_dpi_awareness()
    from core.win_input import get_cursor_pos, send_mouse_move, virtual_screen_rect

    left, top, width, height = virtual_screen_rect()
    restore = get_cursor_pos()

    win = TrackerWindow(left + width - 480, top + height - 320)
    print(f"虚拟桌面 {(left, top, width, height)}；探针窗口 "
          f"({win.x}, {win.y}) {win.width}x{win.height}")
    win.start()
    failures: list[str] = []
    # 每次扫描前先把指针停在窗口**内部**：跨越窗口边界本身会产生
    # 一两条 pointer enter/update，会污染计数
    inside = (win.x + 4, win.y + 4)

    try:
        if not win.enable_pointer_promotion():
            failures.append("EnableMouseInPointer 失败，无法模拟 XAML 应用")
        else:
            print("已开启 pointer 输入模式（与 Win11 记事本 / 资源管理器一致）\n")

        send_mouse_move(*inside)
        time.sleep(0.2)

        print("[1] SetCursorPos 瞬移（旧实现）")
        base = win.snapshot()
        sweep_setcursorpos(_sweep(win))
        time.sleep(0.2)
        got = win.delta(base)
        print(f"    WM_POINTERUPDATE = {got['pointer']}   "
              f"WM_MOUSEMOVE = {got['moves']}")
        if got["pointer"] >= got["moves"]:
            failures.append(
                f"SetCursorPos 竟产生 {got['pointer']} 条 pointer 消息"
                "（与移动数相当），判别条件失效")

        send_mouse_move(*inside)
        time.sleep(0.2)

        print("\n[2] SendInput 绝对移动（新实现）")
        base = win.snapshot()
        sweep_sendinput(_sweep(win))
        time.sleep(0.2)
        got = win.delta(base)
        print(f"    WM_POINTERUPDATE = {got['pointer']}   "
              f"WM_MOUSEMOVE = {got['moves']}")
        if got["pointer"] < 20:
            failures.append(f"SendInput 只产生 {got['pointer']} 条 pointer 消息")

        send_mouse_move(*inside)
        time.sleep(0.2)

        print("\n[3] MacroPlayer 原速回放（轨迹 + 点击）")
        base = win.snapshot()
        elapsed = replay(_sweep(win))
        time.sleep(0.25)
        got = win.delta(base)
        clicks = got["downs"] + got["pointer_down"]
        print(f"    WM_POINTERUPDATE = {got['pointer']}   点击 = {clicks}   "
              f"耗时 {elapsed*1000:.0f}ms")
        if got["pointer"] < 5:
            failures.append(f"回放只产生 {got['pointer']} 条 pointer 消息")
        if clicks != 1:
            failures.append(f"点击没有落在窗口上（收到 {clicks} 次按下）")
    finally:
        if restore:
            send_mouse_move(*restore)
        if not args.keep:
            win.stop()

    print("\n" + "=" * 60)
    if failures:
        for item in failures:
            print("失败：" + item)
        return 1
    print("结论：移动已进入真实输入栈，XAML 应用也能收到 pointer 悬停事件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
