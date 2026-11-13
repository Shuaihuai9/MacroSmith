"""把「参考工具为什么敲得进去」拆成可判读的对照实验。

背景
----
2026-11-12 抓到了参考工具的真实事件流（``tools/captures/watch_ref_*.log``）：

* 每个字符都是 ``vk`` + ``scan`` 都填好的 ``SendInput``（= 我们的 ``physical``）；
* 全部带 ``LLKHF_INJECTED``，``GetLastInputInfo`` 每次都跳 —— 它**走的是输入栈**；
* 关键差异：**字符间隔约 1000ms**（我们的默认间隔是 24ms 甚至 8ms）。

本脚本就验证这一条：同样的通道、不同的间隔，挨个试一遍，让用户**盯着密码框**看
哪一段真的出现了。密码框是掩码显示的，所以每段用**不同长度**的数字串：

    1) 物理按键 24ms    敲  "1"       → 1 个点
    2) 物理按键 1000ms  敲  "22"      → 2 个点
    3) 字符注入 24ms    敲  "333"     → 3 个点
    4) 字符注入 1000ms  敲  "4444"    → 4 个点
    5) 仅扫描码 1000ms  敲  "55555"   → 5 个点

五段全成功 = 15 个点；只 2、4 成功 = 6 个点。用户只要报总数（或报哪几段出来了），
就能判断「间隔」是不是决定性因素。

用法
----
双击 ``tools\\try_type.bat``，或在任意命令行跑::

    python tools/try_type.py            # 8 秒倒计时后开始
    python tools/try_type.py --now      # 立刻开始（先切好窗口）

脚本只读配置、不改任何文件；提权由脚本自己做（道理见 ``watch_input.py``）。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import direct_input as di  # noqa: E402
from core import quick_input as qi  # noqa: E402
from core import win_input  # noqa: E402

shell32 = ctypes.windll.shell32

#: 提权重启后由子进程继承：① 不再递归提权；② 跑完停一下别让窗口闪没。
HOLD_ENV = "MACROSMITH_TOOL_HOLD"

COUNTDOWN = 8

#: (标签, 通道, 间隔 ms, 要敲的字符)。长度递增，方便用「点」的个数反推哪段成功。
CASES: tuple[tuple[str, str, int, str], ...] = (
    ("物理按键（vk+扫描码）", "physical", 24, "1"),
    ("物理按键（vk+扫描码）", "physical", 1000, "22"),
    ("字符注入（Unicode）", "unicode", 24, "333"),
    ("字符注入（Unicode）", "unicode", 1000, "4444"),
    ("仅扫描码（SCANCODE）", "simulate", 1000, "55555"),
)

_SENDERS = {
    "physical": win_input.send_physical_key,
    "unicode": win_input.send_char,
    "simulate": win_input.send_key_scancode,
}


# ---------------------------------------------------------------------------
# 提权（与 watch_input.py / probe_target.py 同一套）
# ---------------------------------------------------------------------------
def _pause() -> None:
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\n按回车关闭窗口 …")
    except Exception:
        pass


def ensure_elevated() -> bool:
    """已是管理员返回 ``True``；否则发起提权并返回 ``False``。"""
    if win_input.is_elevated():
        return True
    print("=" * 70)
    print("需要管理员权限：低权限进程的注入会被 UIPI 静默丢掉，测不出真东西。")
    print("=" * 70)
    script = str(Path(__file__).resolve())
    root = str(Path(__file__).resolve().parent.parent)
    os.environ[HOLD_ENV] = "1"
    try:
        code = int(shell32.ShellExecuteW(
            None, "runas", sys.executable,
            subprocess.list2cmdline([script, *sys.argv[1:]]), root, 1))
    except Exception:
        code = 0
    print()
    if code > 32:
        print(">>> 已请求管理员权限，请在 UAC 窗口点「是」，看**新出现的窗口**。")
        print(">>> 本窗口可以关掉了。")
        time.sleep(5)
    elif code == 5:
        print(">>> UAC 被拒绝，这次不测了。右键 tools\\try_type.bat → 以管理员身份运行。")
    else:
        print(f">>> 提权没发起成功（ShellExecuteW 返回 {code}）。")
    _pause()
    return False


# ---------------------------------------------------------------------------
# 打字
# ---------------------------------------------------------------------------
def type_chars(text: str, kind: str, gap_ms: int) -> int:
    """按给定通道和间隔敲一段文本，返回敲了几个字符。

    这里**不走** :func:`core.quick_input.type_text`：它有 ``MAX_DELAY_MS=50``
    的夹取，做不了 1000ms 的对照。分组规则仍复用它的，免得两边不一致。
    """
    send = _SENDERS[kind]
    groups = qi._unicode_groups(text) if kind == "unicode" else qi._vk_groups(text)
    gap = gap_ms / 1000.0
    for index, group in enumerate(groups):
        for payload, up in group:
            try:
                send(payload, up)
            except Exception:
                return index
        if index + 1 < len(groups):
            time.sleep(gap)
    return len(groups)


def banner(step: int, label: str, gap_ms: int, text: str) -> None:
    dots = "·" * len(text)
    print()
    print("=" * 66)
    print(f" 第 {step} / {len(CASES)} 段：{label}　间隔 {gap_ms}ms")
    print(f"   现在敲 {text!r}（屏幕上应该多出 {len(text)} 个点：{dots}）")
    print("=" * 66)
    print(">>> 看着密码框，别碰键鼠 ……", end="", flush=True)
    for _ in range(3):
        time.sleep(1)
        print(".", end="", flush=True)
    print(" 开始")


def wait_for_target(seconds: int = COUNTDOWN) -> int:
    print(f"请在 {seconds} 秒内切到 QQ 登录框，把光标点进**密码框**……")
    for left in range(seconds, 0, -1):
        print(f"  {left} 秒", end="\r", flush=True)
        time.sleep(1)
    print(" " * 12, end="\r")
    return di.focus_window()


def main() -> int:
    if not ensure_elevated():
        return 0

    if "--now" not in sys.argv:
        hwnd = wait_for_target()
    else:
        hwnd = di.focus_window()

    print("=" * 66)
    print("目标窗口")
    print("=" * 66)
    if hwnd:
        print(f"焦点控件 = {hwnd}　类名 = {di.class_name(hwnd)!r}")
    else:
        print("取不到前台窗口 —— 请先切到目标程序再运行")
    print(f"本进程权限 = {'管理员' if win_input.is_elevated() else '普通用户'}")

    for step, (label, kind, gap_ms, text) in enumerate(CASES, start=1):
        banner(step, label, gap_ms, text)
        n = type_chars(text, kind, gap_ms)
        print(f"  → 已送出 {n}/{len(text)} 个字符")
        time.sleep(2.0)          # 留时间让用户看清这一段有没有出现

    total = sum(len(c[3]) for c in CASES)
    print()
    print("=" * 66)
    print("对照表（密码框里应该出现多少「点」）")
    print("=" * 66)
    expect = 0
    for step, (label, kind, gap_ms, text) in enumerate(CASES, start=1):
        expect += len(text)
        print(f"  {step}. {label:<24} {gap_ms:>5}ms  {text!r:<10} → {len(text)} 个点")
    print(f"  全成功 = {total} 个点；完全没反应 = 0 个点。")
    print()
    print("请报给我：密码框里一共几个点／哪几段出来了。")
    print("（这一段是掩码显示，看不到原文，只能数点或看出现的位置。）")

    if os.environ.get(HOLD_ENV) == "1":
        _pause()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
