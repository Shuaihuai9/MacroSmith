"""受保护窗口（游戏/安全控件）的输入通道诊断。

用法::

    # 先切窗口、再测试（默认 5 秒倒计时，你可以慢慢切过去）
    python tools/probe_target.py --test

    # 只看结构，不动任何东西
    python tools/probe_target.py

    # 诊断当前前台窗口，不等倒计时
    python tools/probe_target.py --test --now

★ 为什么必须有倒计时：你是在终端里敲命令的，命令一执行，**前台窗口就是终端
自己**。此时读「焦点控件」读到的是 PowerShell 的控制台，不是 QQ 的输入框——
上一版没有倒计时，用户跑出来的结果全是终端的，白折腾。现在会先倒数几秒，
让你切过去、把光标点进目标输入框，再开始采样。

诊断输出回答一个问题：这个窗口到底吃哪条通道？

1. ``unicode``  —— ``SendInput`` + ``KEYEVENTF_UNICODE``（字符注入，最稳）；
2. ``physical`` —— ``SendInput`` 同时填 ``wVk`` 与 ``wScan``（最像真键盘）；
3. ``simulate`` —— ``SendInput`` + ``KEYEVENTF_SCANCODE``（模拟物理按键）；
4. ``message``  —— 窗口消息 ``WM_CHAR`` / ``WM_SETTEXT``（不经输入栈）。

★ **必须提权运行**（双击 ``tools\\run_probe.bat``，它会自己请求管理员权限）。
低权限进程的每条通道都会被 UIPI 静默丢掉，测出来的「全被拦」是测法问题——
2026-11-12 就因此得出过一次错误结论。

「系统级探针」（注入 F24 后读键盘状态）判定的是**事件有没有进系统**，与目标
程序配不配合无关；「写入测试」判定的是**字符有没有出现在输入框里**，非标准
控件读不回来，只能看屏幕。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import direct_input as di  # noqa: E402
from core import quick_input as qi  # noqa: E402
from core import win_input  # noqa: E402

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

EnumChildProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

#: 倒计时秒数：够从一个窗口切到另一个再把光标点进输入框
COUNTDOWN = 8

#: 提权重启后由子进程继承：① 不再递归提权；② 跑完停一下别让窗口闪没。
HOLD_ENV = "MACROSMITH_TOOL_HOLD"


# ---------------------------------------------------------------------------
# 提权（必须在任何采集之前）
# ---------------------------------------------------------------------------
def _pause() -> None:
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\n按回车关闭窗口 …")
    except Exception:
        pass


def ensure_elevated() -> bool:
    """已是管理员返回 ``True``；否则发起提权并返回 ``False``（调用方应退出）。

    提权写在 .bat 里出过事故：``Start-Process -Verb RunAs`` 拉起新窗口后父窗口
    立刻退出，UAC 弹窗没被注意到的样子就是「闪一下就没了」。放在这里能拿到
    ``ShellExecuteW`` 的返回值，把「已发起」和「用户点了否」分开说清楚。
    """
    if win_input.is_elevated():
        return True

    print("=" * 70)
    print("需要管理员权限：低权限进程的注入会被 UIPI 静默丢掉，")
    print("不提权测出来的「四条通道全被拦」是测法问题，不是目标的真实行为。")
    print("=" * 70)

    script = str(Path(__file__).resolve())
    root = str(Path(__file__).resolve().parent.parent)
    os.environ[HOLD_ENV] = "1"          # 子进程继承
    try:
        code = int(shell32.ShellExecuteW(
            None, "runas", sys.executable,
            subprocess.list2cmdline([script, *sys.argv[1:]]), root, 1))
    except Exception:
        code = 0

    print()
    if code > 32:
        print(">>> 已请求管理员权限。")
        print(">>> 请在 UAC 窗口点「是」，然后看**新出现的那个窗口**继续；本窗口可以关掉。")
        time.sleep(5)
    elif code == 5:
        print(">>> UAC 被拒绝（点了「否」，或被组策略拦下），这次不测了。")
        print(">>> 想重来：右键 tools\\run_probe.bat → 以管理员身份运行。")
    else:
        print(f">>> 提权没发起成功（ShellExecuteW 返回 {code}）。")
        print(">>> 请右键 tools\\run_probe.bat → 以管理员身份运行。")
    _pause()
    return False


# ---------------------------------------------------------------------------
# 基础信息
# ---------------------------------------------------------------------------
def exe_of(pid: int) -> str:
    handle = k32.OpenProcess(0x1000, False, pid)
    if not handle:
        return "?"
    buf = ctypes.create_unicode_buffer(1024)
    size = wt.DWORD(1024)
    k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
    k32.CloseHandle(handle)
    return buf.value


def pid_of(hwnd: int) -> int:
    pid = wt.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_text(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def style_of(hwnd: int) -> int:
    return int(u32.GetWindowLongW(hwnd, -16))


def top_level(hwnd: int) -> int:
    """一路向上找到顶层窗口。"""
    top = hwnd
    while True:
        parent = int(u32.GetParent(top) or 0)
        if not parent:
            return top
        top = parent


def last_input_tick() -> int:
    """``GetLastInputInfo`` 的时间戳（系统级「最近一次输入」标记）。

    这是判断「注入到底进没进系统」最硬的证据：只要有一丁点输入事件进了
    Windows 输入栈，这个计数就会跳。注入成功但目标不认，它会跳；注入被
    拦，它一动不动。
    """

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    try:
        if u32.GetLastInputInfo(ctypes.byref(info)):
            return int(info.dwTime)
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# 控件树
# ---------------------------------------------------------------------------
def describe(hwnd: int, depth: int = 0) -> None:
    cls = di.class_name(hwnd)
    style = style_of(hwnd)
    flags = []
    if style & 0x0020 and cls.startswith(("Edit", "RichEdit")):
        flags.append("密码框")
    if di.is_edit_control(hwnd):
        flags.append("标准Edit")
    if not u32.IsWindowVisible(hwnd):
        flags.append("不可见")
    if not u32.IsWindowEnabled(hwnd):
        flags.append("已禁用")
    text = window_text(hwnd)
    extra = f" 文本={text[:20]!r}" if text else ""
    print(f"{'  ' * depth}- hwnd={hwnd:<10} class={cls!r} style={style:#010x}"
          f" {'/'.join(flags)}{extra}")


def dump_tree(hwnd: int, depth: int = 0, seen: set[int] | None = None) -> None:
    """打印控件树。

    ``EnumChildWindows`` 会把**所有后代**都枚举出来（不只是直接子窗口），
    递归下去会同一批句柄被打印好几遍，日志看着像树坏了。用一个 ``seen``
    集合去重，每个窗口只出现一次。
    """
    if seen is None:
        seen = set()
    if hwnd in seen:
        return
    seen.add(hwnd)

    describe(hwnd, depth)
    if depth >= 4:
        return

    children: list[int] = []

    def each(child, _lp):
        children.append(int(child))
        return True

    u32.EnumChildWindows(hwnd, EnumChildProc(each), 0)
    for child in children[:40]:
        dump_tree(child, depth + 1, seen)


# ---------------------------------------------------------------------------
# 诊断
# ---------------------------------------------------------------------------
def report_target(hwnd: int) -> None:
    pid = pid_of(hwnd)
    mine_name, mine_rid = win_input.process_integrity(
        k32.GetCurrentProcessId())
    theirs_name, theirs_rid = win_input.process_integrity(pid)

    print("=" * 70)
    print("目标诊断：请确认下面就是你要输入的那个窗口")
    print("=" * 70)
    print(f"焦点控件 hwnd = {hwnd}")
    print(f"顶层窗口类名  = {di.class_name(top_level(hwnd))!r}")
    print(f"进程         = {exe_of(pid)} (pid {pid})")
    print(f"控件类名     = {di.class_name(hwnd)!r}")
    print(f"标准Edit     = {di.is_edit_control(hwnd)}")
    print(f"密码框       = {di.is_password_control(hwnd)}")
    if di.is_edit_control(hwnd):
        print(f"读回长度     = {len(di.read_text(hwnd))}")
    else:
        # 自绘窗口（CEF / FairyGUI / Qt）对 WM_GETTEXTLENGTH 返回的是窗口标题
        # 长度之类的东西，不是输入框内容，别当成"能读回"。
        print("读回长度     = -（非标准控件，读不回来，只能看屏幕）")

    print("\n权限（这一栏往往直接定死结论）：")
    print(f"· 本程序     = {mine_name or '?'}（{'管理员' if win_input.is_elevated() else '普通用户'}）")
    print(f"· 目标程序   = {theirs_name or '?'}")
    if theirs_rid and mine_rid and theirs_rid > mine_rid:
        print("  >>> 目标权限比本程序高：SendInput 与窗口消息都会被系统静默丢掉，")
        print("      请用「以管理员身份运行」启动宏匠（或本脚本）后重试。")
    elif theirs_rid and mine_rid and theirs_rid == mine_rid:
        print("  >>> 两边同级，权限不是障碍；投不进去就是目标自身在拦。")


def privilege_gap(hwnd: int) -> tuple[bool, str]:
    """本进程权限是否低于目标；返回 ``(是否不足, 说明)``。

    这一栏比任何注入测试都优先：权限不够时，``SendInput`` 与窗口消息都会被
    UIPI 静默丢掉，测出来的「被丢弃」是**测法的问题**，不是程序的行为。
    2026-11-12 就吃过一次：探针没提权，得出「注入入口被整体封了」的错误结论，
    而同一台机器上以管理员运行的宏匠其实能正常注入。
    """
    mine_name, mine = win_input.process_integrity(os.getpid())
    theirs_name, theirs = win_input.process_integrity(di.window_pid(hwnd))
    if theirs and mine and theirs > mine:
        return True, (f"本程序 {mine_name or hex(mine)} < 目标 {theirs_name or hex(theirs)}")
    return False, f"本程序 {mine_name or '-'} / 目标 {theirs_name or '-'}"


def probe_system(hwnd: int) -> tuple[bool, bool]:
    """系统级探针：注入 F24，看事件有没有进系统（与目标配不配合无关）。"""
    print("\n" + "=" * 70)
    print("第一步：系统级注入探针（F24 是一个没人用的空键，不会打出字符）")
    print("=" * 70)

    tick_before = last_input_tick()
    char_ok = win_input.injection_delivered()
    tick_after_char = last_input_tick()
    char_tick = (tick_after_char - tick_before) & 0xFFFFFFFF

    scan_ok = win_input.injection_delivered(scancode=True)
    tick_after_scan = last_input_tick()
    scan_tick = (tick_after_scan - tick_after_char) & 0xFFFFFFFF

    print(f"1. 字符注入通道（KEYEVENTF_UNICODE）: "
          f"{'[OK] 事件进了系统' if char_ok else '[--] 被丢弃'}"
          f"　GetLastInputInfo 增量 {char_tick}")
    print(f"2. 模拟按键通道（KEYEVENTF_SCANCODE）: "
          f"{'[OK] 事件进了系统' if scan_ok else '[--] 被丢弃'}"
          f"　GetLastInputInfo 增量 {scan_tick}")
    print(f"3. 原生入口 win32u!NtUserSendInput  : "
          f"{'可用' if win_input.native_send_input() else '本机没有这个导出'}")

    under, detail = privilege_gap(hwnd)
    if not char_ok and not scan_ok:
        if under:
            print("\n  >>> ★ 本次探针无效：权限不够（" + detail + "）。")
            print("      低权限进程的注入会被 UIPI 直接丢掉，测出来的「被丢弃」是测法问题，")
            print("      不能当成目标的真实行为。请用 tools\\run_probe.bat（会自动提权）重跑。")
        else:
            print("\n  >>> 两条注入通道的事件都进不了系统（两边权限同级，排除 UIPI）：")
            print("      注入入口被整体封了 —— 保护驱动、BlockInput，或本进程被特意针对。")
            print("      这种情况只能靠窗口消息，或者换一台/换一种方式提权。")
    return char_ok, scan_ok


def probe_channels(hwnd: int, text: str = "Test123",
                   char_ok: bool = True, scan_ok: bool = True,
                   under: bool = False) -> None:
    """依次试每条通道，用 ``WM_GETTEXT`` 读回来判断有没有进去。

    ``under`` 表示本进程权限低于目标：此时**每一条**通道都会被 UIPI 丢掉，
    整段输出只能证明「权限不够」，不能用来判断目标的行為。
    """
    print("\n" + "=" * 70)
    print(f"第二步：实际写入测试（写入 {text!r}，每条通道前会清空，末尾还原）")
    print("=" * 70)

    if under:
        print("★ 本进程权限低于目标程序：下面每条通道都会失败，结果不能当证据。")
        print("  请关掉本窗口，用 tools\\run_probe.bat（会自动提权）重跑。\n")

    original = di.read_text(hwnd)
    is_edit = di.is_edit_control(hwnd)
    if is_edit:
        print(f"· 原内容长度 {len(original)}（内容不打印）")
    else:
        print("· 这个控件读不回来（非标准 Edit），下面的判定只能靠肉眼")

    allowed, error = di.can_post(hwnd)
    if allowed:
        note = "（已比对完整性级别：不高于本程序）"
    elif error == di.ERROR_ACCESS_DENIED:
        note = "　<<< 目标权限高于本程序，UIPI 会拦掉窗口消息"
    else:
        note = ""
    print(f"· 消息投递：{'可以' if allowed else f'被拒绝（错误码 {error}）'}{note}")

    def reset() -> None:
        if is_edit:
            di.set_text(hwnd, "")

    def verdict(expected: str) -> str:
        if not is_edit:
            return "[??] 读不回，看屏幕"
        got = di.read_text(hwnd)
        return "[OK] 进得去" if got == expected else \
               f"[--] 没进去（读回 {len(got)} 字符）"

    # --- 1) SendInput 字符注入（unicode：wVk=0，字符进 wScan）---
    reset()
    tick = last_input_tick()
    typed = qi.type_text(text, delay_ms=8, method="unicode") if char_ok else 0
    moved = (last_input_tick() - tick) & 0xFFFFFFFF
    head = "已跳过（系统级探针就说进不去）" if not char_ok else f"送出 {typed} 字符"
    print(f"1. 字符注入    : {head}　输入标记 +{moved} -> {verdict(text)}")

    # --- 2) 物理按键（physical：wVk 与 wScan 都填准，最像真键盘）---
    reset()
    tick = last_input_tick()
    typed = qi.type_text(text, delay_ms=8, method="physical") if char_ok else 0
    moved = (last_input_tick() - tick) & 0xFFFFFFFF
    head = "已跳过（系统级探针就说进不去）" if not char_ok else f"送出 {typed} 字符"
    print(f"2. 物理按键    : {head}　输入标记 +{moved} -> {verdict(text)}")

    # --- 3) 仅扫描码（KEYEVENTF_SCANCODE）---
    reset()
    tick = last_input_tick()
    typed = qi.type_text(text, delay_ms=8, method="simulate") if scan_ok else 0
    moved = (last_input_tick() - tick) & 0xFFFFFFFF
    head = "已跳过（系统级探针就说进不去）" if not scan_ok else f"送出 {typed} 字符"
    print(f"3. 仅扫描码    : {head}　输入标记 +{moved} -> {verdict(text)}")

    # --- 4) 窗口消息逐字符 ---
    reset()
    n = di.type_text(text, hwnd=hwnd, delay_ms=8, verify=False)
    if under:
        head = f"发出 {n} 字符，但权限不够，必被 UIPI 丢掉"
    else:
        head = f"送出 {n} 字符"
    print(f"4. 窗口消息    : {head} -> {verdict(text)}")

    # --- 5) WM_SETTEXT（标准 Edit 专用）---
    reset()
    ok = di.set_text(hwnd, text) if is_edit else False
    print(f"5. WM_SETTEXT  : {'已发出' if ok else '不适用（非标准 Edit）'}"
          f" -> {verdict(text) if is_edit else '-'}")

    if is_edit:
        di.set_text(hwnd, original)
        print(f"\n已把原有内容写回（长度 {len(original)}）")
    else:
        print("\n注意：这个控件读不回来，没法自动还原。如果屏幕上真的出现了"
              " Test123，请手动删掉。")


def conclusion(char_ok: bool, scan_ok: bool, can_post: bool,
               under: bool = False) -> None:
    print("\n" + "=" * 70)
    print("建议")
    print("=" * 70)

    if under:
        # 权限不够时任何分支都是错的：先修测法，再谈方案。
        print("· ★ 这份结果无效，先别看结论。")
        print("  本进程的完整性级别低于目标程序，UIPI 会把 SendInput 和窗口消息")
        print("  一起静默丢掉 —— 也就是说上面每一条「被丢弃」都是必然的，")
        print("  它证明的是「探针没提权」，不是「目标拦得住」。")
        print("  重跑方式：双击 tools\\run_probe.bat（会自己请求管理员权限）。")
        return

    if char_ok or scan_ok:
        print("· 注入通道可用。若屏幕上仍然没出现字符，说明目标程序认得这些事件"
              "但主动忽略了（多半只认硬件设备）。")
        print("  接下来唯一现实的方案是硬件级模拟（USB HID 小键盘 / 树莓派 Pico），"
              "纯软件层面已经没有更强的路。")
    elif can_post:
        print("· 注入被整体封锁，但窗口消息投得进去：宏匠的自动模式会走"
              "「窗口消息通道」，直接用它试。")
        print("  若屏幕上仍无字符，说明这个界面不吃 WM_CHAR（自绘且不处理消息）——")
        print("  那就只剩硬件级模拟（USB HID 设备）或复制粘贴。")
    else:
        print("· 两条注入通道都进不了系统，窗口消息也被拒：目标程序既不吃输入栈")
        print("  也不吃消息，纯软件层面已经没有路。")
        print("  剩下的方向只有硬件级模拟（USB HID 设备），或者先去抓一次参考工具")
        print("  的按键事件（tools\\watch_input.bat），确认它是不是走了别的机制。")

    print("\n把上面全部输出贴给我，我据此决定下一步做哪套方案。")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def wait_for_target(seconds: int = COUNTDOWN) -> int:
    """倒计时，等用户切到目标窗口后再采焦点。"""
    print(f"请在 {seconds} 秒内切到目标窗口，并把光标点进要输入的那个框……")
    for left in range(seconds, 0, -1):
        print(f"  {left} 秒", end="\r", flush=True)
        time.sleep(1)
    print(" " * 12, end="\r")
    return di.focus_window()


def main() -> int:
    # 提权必须先于一切：没权限时跑出来的每条「被丢弃」都是假的。
    if not ensure_elevated():
        return 0

    # 输出同时写进 tools/captures/ 下的日志文件：几十行诊断让用户从控制台
    # 复制很痛苦，也容易漏行。直接把文件路径告诉对方（或让 AI 自己读）更可靠。
    out = Path(__file__).resolve().parent / "captures" / (
        time.strftime("probe_%Y%m%d-%H%M%S.log"))
    out.parent.mkdir(parents=True, exist_ok=True)
    handle = open(out, "w", encoding="utf-8")

    class _Tee:
        """把 stdout 同时写到文件和屏幕（不改任何 print 调用）。"""

        def write(self, text: str) -> int:
            handle.write(text)
            handle.flush()
            return sys.__stdout__.write(text)

        def flush(self) -> None:
            handle.flush()
            sys.__stdout__.flush()

    saved = sys.stdout
    sys.stdout = _Tee()
    try:
        code = _main()
        print(f"\n日志已保存：{out}")
        if os.environ.get(HOLD_ENV) == "1":
            _pause()
        return code
    finally:
        sys.stdout = saved
        handle.close()


def _main() -> int:
    wait = "--now" not in sys.argv
    if wait:
        hwnd = wait_for_target()
    else:
        hwnd = di.focus_window()

    if not hwnd:
        print("取不到前台窗口，请先切到目标程序再运行")
        return 1

    report_target(hwnd)
    print("\n控件树（从顶层窗口开始，最多 4 层）：")
    dump_tree(top_level(hwnd))

    if "--test" not in sys.argv:
        print("\n（未写入任何内容。想验证通道请加 --test）")
        return 0

    char_ok, scan_ok = probe_system(hwnd)
    # 探针只反映「系统收不收」，真正写哪个窗口还得重新取一次焦点
    # （上面几张打印不会改变焦点，但用户可能动了鼠标，重取一次更稳）
    target = di.focus_window() or hwnd
    under, detail = privilege_gap(target)
    if under:
        print(f"\n★ 权限差距：{detail} —— 下一步的写入测试不作数，仅作参考。")
    allowed, _err = di.can_post(target)
    probe_channels(target, char_ok=char_ok, scan_ok=scan_ok, under=under)
    conclusion(char_ok, scan_ok, allowed, under=under)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
