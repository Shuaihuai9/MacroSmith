"""抓真实键盘事件流：看「谁在注入、注入了什么、系统收不收」。

为什么需要它
------------
「某个能用的工具敲得进去、宏匠敲不进去」这种对比，靠猜是猜不出来的。
本脚本同时开两路观测：

1. **低级键盘钩子**（``WH_KEYBOARD_LL``）——打印每个按键事件的虚拟键码、
   扫描码、``LLKHF_INJECTED``（这条是不是软件模拟）、
   ``LLKHF_LOWER_IL_INJECTED``（由更低权限的进程注入）、``dwExtraInfo``
   （有些驱动在这里写魔数），以及当时的前台窗口类名；
2. **输入标记轮询**（``GetLastInputInfo``）——这个计数只要有一丁点输入进了
   Windows 输入栈就会跳，**与权限无关、与钩子无关**。

两路必须一起看，因为钩子有权限盲区：**低完整性进程装的钩子收不到
高完整性进程注入的事件**（2026-11-11 就踩到了：抓包工具没提权，参考工具是
管理员运行，钩子 0 条事件，``GetLastInputInfo`` 却在动）。所以：

* 输入标记动了、钩子没事件 → 事件进了系统，只是被权限挡在钩子外面；
* 输入标记不动、钩子也没事件 → 那个工具**根本没走输入栈**（多半是窗口消息
  或无无障碍接口），这时候再怎么改注入方式都没用。

运行方式
--------
双击 ``tools\\watch_input.bat``。**提权由本脚本自己做**（``ShellExecuteW runas``），
.bat 只负责挑 Python 解释器：把提权写在批处理里出过一次事故——``Start-Process
-Verb RunAs`` 拉起新窗口后父窗口立刻 ``exit /b``，UAC 弹窗若没被注意到，看到的
就是「窗口闪一下就没了」，没有任何提示（2026-11-12 用户报「这脚本闪退」）。
提权放在 Python 里能拿到 ``ShellExecuteW`` 的返回值，区分「已发起」和「用户点了否」，
并且能明说下一步该看哪里。

也可以直接在管理员命令行跑::

    python tools/watch_input.py --seconds 90

流程：倒计时内切到目标窗口、把光标点进密码输入框 → 看到「开始记录」后
按要对比的那个工具的键 → 结束。**输出同时写进 ``tools/captures/`` 下的日志
文件，路径会在结尾打印出来**，直接发那个文件就行。

判读
----
* 参考工具有事件、宏匠没事件 → 宏匠的注入根本没发出去（入口被拦）。
* 两边都有事件、都是 ``INJECTED``，但只有参考工具的文字上屏
  → 目标程序在**应用层**按别的条件挑食，看事件差异（键码、扫描码、时序）。
* 参考工具完全没有事件、输入标记也不动，文字却上屏了
  → 它走的是窗口消息或无障碍接口，那我们该往那个方向做。

本脚本只读：不改任何东西，也不会自己发输入。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Win32
# ---------------------------------------------------------------------------
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
PM_REMOVE = 0x0001

LLKHF_EXTENDED = 0x01
LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_INJECTED = 0x10

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)

user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = (wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = (wt.HHOOK,)
user32.GetForegroundWindow.restype = wt.HWND
user32.GetClassNameW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)

#: 特殊键名（其余按字符显示）
_NAMES = {
    0x08: "Back", 0x09: "Tab", 0x0D: "Enter", 0x10: "Shift", 0x11: "Ctrl",
    0x12: "Alt", 0x1B: "Esc", 0x20: "Space", 0xA0: "LShift", 0xA1: "RShift",
    0xA2: "LCtrl", 0xA3: "RCtrl", 0xA4: "LAlt", 0xA5: "RAlt",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x24: "Home", 0x23: "End", 0x21: "PgUp", 0x22: "PgDn",
}


def key_name(vk: int) -> str:
    if vk in _NAMES:
        return _NAMES[vk]
    if 0x70 <= vk <= 0x87:                 # F1..F24
        return f"F{vk - 0x6F}"
    if 0x30 <= vk <= 0x5A:
        return chr(vk)
    if 0x60 <= vk <= 0x6F:
        return f"Num{vk - 0x60}"
    return f"vk{vk:#04x}"


def foreground_class() -> str:
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd:
        return "?"
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value or "?"


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def last_input_tick() -> int:
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    try:
        if user32.GetLastInputInfo(ctypes.byref(info)):
            return int(info.dwTime)
    except Exception:
        pass
    return 0


def is_elevated() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


#: 提权重启后由子进程继承的环境变量标记，两件事：① 不再递归提权；
#: ② 跑完 ``input()`` 停一下，否则新控制台窗口一闪就没了。
HOLD_ENV = "MACROSMITH_TOOL_HOLD"


def relaunch_elevated() -> int:
    """用 ``runas`` 重新拉起自身；返回 ``ShellExecuteW`` 的原始结果。

    ``> 32`` 表示成功发起（UAC 已接受）；``5``（``ERROR_ACCESS_DENIED``）表示
    用户在 UAC 上点了「否」或系统策略拒绝——**这两种情况必须区分开**，否则
    用户看到的就是「窗口闪一下就没了」。
    """
    script = str(Path(__file__).resolve())
    root = str(Path(__file__).resolve().parent.parent)
    os.environ[HOLD_ENV] = "1"     # 子进程继承：进到新窗口后跑完要停一下
    try:
        return int(shell32.ShellExecuteW(
            None, "runas", sys.executable,
            subprocess.list2cmdline([script, *sys.argv[1:]]), root, 1))
    except Exception:
        return 0


def ensure_elevated() -> bool:
    """已是管理员就返回 ``True``；否则发起提权并返回 ``False``（调用方应退出）。"""
    if is_elevated():
        return True
    print("=" * 66)
    print("需要管理员权限：低权限的键盘钩子收不到高权限进程注入的按键，")
    print("不提权抓到的永远是「0 条事件」——那不是结论，是假数据。")
    print("=" * 66)
    code = relaunch_elevated()
    if code > 32:
        print()
        print(">>> 已请求管理员权限。")
        print(">>> 请在弹出的 UAC 窗口点「是」，然后看**新出现的那个窗口**继续操作；")
        print(">>> 本窗口可以关掉了。")
        time.sleep(5)              # 留时间让用户看清 / 找到新窗口
        return False
    print()
    if code == 5:
        print(">>> UAC 被拒绝（点了「否」，或被组策略拦下），这次不抓了。")
        print(">>> 想重来：右键 tools\\watch_input.bat → 以管理员身份运行。")
    else:
        print(f">>> 提权没发起成功（ShellExecuteW 返回 {code}）。")
        print(">>> 请右键 tools\\watch_input.bat → 以管理员身份运行。")
    pause()
    return False


def pause() -> None:
    """停一下，别让双击出来的窗口一闪就没。只在交互式控制台里停。"""
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\n按回车关闭窗口 …")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 输出（控制台 + 日志文件双写）
# ---------------------------------------------------------------------------
class Echo:
    """同时写屏幕和文件；用户只要发那个文件就行，不用复制几十行。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "w", encoding="utf-8")
        self._lock = threading.Lock()

    def __call__(self, text: str = "") -> None:
        with self._lock:
            try:
                print(text, flush=True)
            except Exception:            # 控制台被关掉也别丢日志
                pass
            self._fh.write(text + "\n")
            self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------
class Watcher:
    def __init__(self, say: Echo) -> None:
        self.say = say
        self.t0 = time.perf_counter()
        self.count = 0
        self.injected = 0
        self.hardware = 0
        self.ticks = 0
        #: 键事件与输入标记变化的时刻（perf_counter），用于事后配对
        self.key_times: list[float] = []
        self.tick_times: list[float] = []
        self.last_class = ""
        self._proc = HOOKPROC(self._callback)      # 必须持有引用，否则会被回收
        self.hook = None
        self._running = False

    def _stamp(self) -> str:
        return f"{(time.perf_counter() - self.t0) * 1000:8.1f}ms"

    # -- 钩子 --
    def _callback(self, code, wparam, lparam):
        if code >= 0:
            try:
                info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                self._emit(int(wparam), info)
            except Exception:                   # 钩子里绝不抛异常，否则会被摘钩
                pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _emit(self, wparam: int, info: KBDLLHOOKSTRUCT) -> None:
        down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
        flags = int(info.flags)
        injected = bool(flags & LLKHF_INJECTED)
        self.count += 1
        self.key_times.append(time.perf_counter())
        if injected:
            self.injected += 1
        else:
            self.hardware += 1

        marks = []
        if injected:
            marks.append("INJECTED")
        if flags & LLKHF_LOWER_IL_INJECTED:
            marks.append("LOWER_IL")
        if flags & LLKHF_EXTENDED:
            marks.append("EXT")
        extra = ""
        if info.dwExtraInfo:
            extra = f" extra={int(info.dwExtraInfo):#x}"
        cls = foreground_class()
        target = ""
        if cls != self.last_class:
            self.last_class = cls
            target = f"  <- 前台={cls}"

        self.say(f"{self._stamp()} {'DOWN' if down else 'UP  '} "
                 f"{key_name(int(info.vkCode)):<8} vk={int(info.vkCode):#04x} "
                 f"scan={int(info.scanCode):#04x} "
                 f"[{'|'.join(marks) if marks else '硬件'}]"
                 f"{extra}{target}")

    # -- 输入标记轮询（与权限无关的旁证）--
    def _tick_loop(self, seconds: float) -> None:
        previous = last_input_tick()
        deadline = time.time() + seconds
        while self._running and time.time() < deadline:
            time.sleep(0.1)
            now = last_input_tick()
            if now != previous:
                self.ticks += 1
                self.tick_times.append(time.perf_counter())
                self.say(f"{self._stamp()} · 输入标记变化 dwTime={now} "
                         f"(+{(now - previous) & 0xFFFFFFFF}ms) "
                         f"<- 有输入进入了系统输入栈")
                previous = now

    def _pair_ticks(self) -> tuple[int, int]:
        """把输入标记变化与键事件配对，分出「键盘」和「鼠标/其它」。

        为什么要配对：``GetLastInputInfo`` 对鼠标移动也会跳，而测试过程中
        切窗口、点密码框必然要动鼠标。不配对的话，一次鼠标移动就会被误读成
        「键盘事件进了系统」，结论正好反过来。判据是 200ms 内是否有键事件。
        """
        paired = 0
        for when in self.tick_times:
            if any(abs(when - key) <= 0.2 for key in self.key_times):
                paired += 1
        return paired, len(self.tick_times) - paired

    def run(self, seconds: float) -> None:
        self.hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not self.hook:
            self.say(f"装钩子失败（错误码 {ctypes.GetLastError()}）。"
                     f"请以管理员身份运行（tools\\watch_input.bat 会自己提权）。")
            return
        self._running = True
        ticker = threading.Thread(target=self._tick_loop, args=(seconds,),
                                  name="tick", daemon=True)
        ticker.start()
        self.say("●●● 开始记录 / START RECORDING（钩子已装好）—— 现在按键，别再做别的动作 ●●●")
        deadline = time.time() + seconds
        try:
            while time.time() < deadline:
                msg = wt.MSG()
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.005)
        finally:
            self._running = False
            user32.UnhookWindowsHookEx(self.hook)
            ticker.join(timeout=0.5)

        with_keys, mouse_only = self._pair_ticks()
        self.say("● 记录结束")
        self.say()
        self.say(f"合计 {self.count} 条按键事件：软件模拟 {self.injected} 条，"
                 f"硬件（真人按键）{self.hardware} 条")
        self.say(f"输入标记共变化 {self.ticks} 次：伴随键盘事件 {with_keys} 次，"
                 f"疑似鼠标/其它 {mouse_only} 次")
        self.say()
        if self.count and self.injected:
            self.say("→ 抓到了软件模拟的按键，这就是那个工具用的通道。")
            self.say("  照这组 vk / scan / flags 对齐我们自己的注入即可。")
        elif self.count:
            self.say("→ 只看到真人按键，没有软件模拟：那个工具的输入没经过输入栈"
                     "（钩子能看见真人按键，说明钩子本身是好的）。")
        elif with_keys:
            self.say("→ 输入标记跟着键盘事件在跳，但钩子没拿到键事件："
                     "钩子权限比注入方低（务必用 tools\\watch_input.bat 提权后再跑）。")
        elif mouse_only:
            self.say("→ 只有鼠标动作，没有任何键盘输入进入系统。")
            self.say("  如果这次确实按了那个工具的热键、文字也上屏了，那它就是"
                     "**走窗口消息**，不是注入——这正是我们要找的答案。")
        else:
            self.say("→ 既没按键也没输入标记变化：这段时间系统里没有任何输入。"
                     "确认热键真的按下了、且发生在记录窗口内。")


#: 测哪个工具。一次只测一个：中间"关掉宏匠再开另一个"必须动鼠标，
#: 两段混在一次记录里只会让输入标记的判读变成一锅粥（用户 2026-11-12 指出）。
_TOOLS: dict[str, tuple[str, str]] = {
    "ref": ("参考工具", "F1"),
    "mc": ("宏匠 MacroSmith", "F2"),
}


def ask_tool() -> str:
    """没给 ``--tool`` 时在控制台问一句（原来这段在 .bat 里）。"""
    print("=" * 66)
    print(" 这次测哪个工具？（一次只测一个：另一个请先关掉，否则热键会抢）")
    print("   1 = 参考工具      （热键 F1）")
    print("   2 = 宏匠 MacroSmith（热键 F2）")
    print("=" * 66)
    try:
        choice = input("输入 1 或 2 后回车 [默认 1]：").strip()
    except Exception:
        choice = ""
    return "mc" if choice == "2" else "ref"


def main() -> int:
    countdown = 8
    seconds = 45.0
    tool = ""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--seconds" and i + 1 < len(args):
            seconds = float(args[i + 1])
        if a == "--now":
            countdown = 0
        if a == "--tool" and i + 1 < len(args):
            tool = args[i + 1].strip().lower()
        # 也认裸参数：watch_input.bat 里写的是 `ref` / `mc`，提权重启时
        # 整个命令行被原样传给子进程，到了这里就成了不带 --tool 的位置参数。
        # 不认它的话子进程会去问「测哪个工具」，而它的 stdin 是个新控制台，
        # 于是卡在那里等回车、连日志都不建（2026-11-12 踩到）。
        if a.strip().lower() in _TOOLS:
            tool = a.strip().lower()

    # 先问测哪个工具、再提权：这样 UAC 只弹一次，提权后的新窗口直接开始计数，
    # 不会出现「新窗口又问一遍」的割裂感。
    if not tool:
        tool = ask_tool()
        # ★ 交互选的结果必须写回命令行：提权重启是把 sys.argv[1:] 原样转给子
        #   进程的，而子进程的 stdin 是刚开的新控制台——不带上 --tool，它会
        #   再问一遍并卡在等回车，连日志都不建。
        sys.argv += ["--tool", tool]

    # 提权必须在任何采集之前：低权限钩子收不到高权限注入，不提权跑出来的
    # 「0 条事件」是假数据（2026-11-11 踩过一次）。
    if not ensure_elevated():
        return 0

    name, hotkey = _TOOLS.get(tool, ("未知工具", "?"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(__file__).resolve().parent / "captures" / (
        f"watch_{tool}_{stamp}.log")
    say = Echo(out)
    try:
        say("=" * 66)
        say("键盘事件抓包（只读，不会自己发任何输入）")
        say("=" * 66)
        say(f"本次测试对象：{name}（热键 {hotkey}）")
        say(f"本进程权限：{'管理员' if is_elevated() else '普通用户（会漏掉高权限进程的注入）'}")
        say(f"Python：{sys.version.split()[0]}")
        say()
        say("★ 这一次只测一个工具。另一个工具请先关掉，避免两个热键抢同一个键。")
        say()
        say("步骤：")
        say("  1. 倒计时内切到 QQ 登录框，把光标点进密码框（该动的鼠标现在动完）；")
        say(f"  2. 看到「开始记录 / START RECORDING」后，手离开鼠标，只按一下 {hotkey}；")
        say(f"  3. 等 {int(seconds)} 秒自动结束。记录期间的鼠标动作会污染判读，所以别碰。")
        say()
        say(f"GetLastInputInfo 起始值 = {last_input_tick()}")

        for left in range(countdown, 0, -1):
            say(f"  还有 {left} 秒……切到目标窗口并把光标点进输入框")
            time.sleep(1)
        say()

        Watcher(say).run(seconds)

        say()
        say(f"GetLastInputInfo 结束值 = {last_input_tick()}")
        say()
        say(f"日志已保存：{out}")
    finally:
        say.close()
    if os.environ.get(HOLD_ENV) == "1":
        pause()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
