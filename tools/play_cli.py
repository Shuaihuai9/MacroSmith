"""命令行回放工具（开发阶段验证两种回放模式，不依赖 Qt）。

用法::

    # 模式一：原速回放（可用 --speed 加速，例如 3 倍速）
    python tools/play_cli.py data/scripts/demo.json --mode timed --speed 3

    # 模式一：从第 N 条事件开始回放
    python tools/play_cli.py demo.json --mode timed --from 20

    # 模式二：交互式单步（回车 = 下一步，b = 上一步，r = 重置，q = 退出）
    python tools/play_cli.py demo.json --mode step

    # 模式二：自动逐步执行，每步之间停顿 0.5 秒
    python tools/play_cli.py demo.json --mode step --auto --interval 0.5

默认有 3 秒倒计时，方便把鼠标焦点切到目标窗口。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

try:  # 兼容 `python tools/play_cli.py` 与 `python -m tools.play_cli`
    from tools._common import PROJECT_ROOT, fmt_ms, setup_console_logging  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    from _common import PROJECT_ROOT, fmt_ms, setup_console_logging  # type: ignore  # noqa: F401

from core import event_parser as ep
from core.player import MODE_STEP, MODE_TIMED, MacroPlayer


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MacroSmith 命令行回放工具")
    parser.add_argument("script", help="脚本 JSON 路径（可用相对项目根目录的路径）")
    parser.add_argument("--mode", choices=[MODE_TIMED, MODE_STEP], default=MODE_TIMED,
                        help="timed=模式一原速回放（默认），step=模式二单步执行")
    parser.add_argument("--speed", type=float, default=1.0, help="模式一时间倍率（默认 1.0）")
    parser.add_argument("--from", dest="start_index", type=int, default=0,
                        help="模式一从第几条事件开始（默认 0）")
    parser.add_argument("--countdown", type=float, default=3.0, help="开始前倒计时秒数")
    parser.add_argument("--auto", action="store_true", help="模式二自动逐步执行（无需手工回车）")
    parser.add_argument("--interval", type=float, default=0.5, help="模式二自动执行的步骤间隔秒数")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的内容，不真正注入事件")
    parser.add_argument("--verbose", action="store_true", help="打印每条被执行的原始事件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_console_logging()
    args = parse_args(argv)

    script_path = Path(args.script)
    if not script_path.is_absolute():
        script_path = (PROJECT_ROOT / script_path) if not script_path.exists() else script_path
    try:
        payload = ep.load_script(script_path)
    except ep.ScriptError as exc:
        print(f"载入脚本失败：{exc}")
        return 2

    events = payload["events"]
    steps = ep.build_steps(events)
    print("=" * 66)
    print(f"脚本      : {script_path}")
    print(f"名称      : {payload.get('name')}")
    print(f"事件      : {len(events)} 条，时长 {int(payload.get('duration_ms') or 0) / 1000.0:.3f} 秒")
    print(f"操作单元  : {len(steps)} 步")
    print(f"模式      : {'模式一 原速回放' if args.mode == MODE_TIMED else '模式二 单步执行'}")
    if args.dry_run:
        print("模式      : DRY-RUN（只打印，不注入事件）")
    print("=" * 66)

    player = MacroPlayer(
        on_event=(lambda i, e: print(f"  [{fmt_ms(int(e.get('t', 0)))}] #{i + 1} {ep.describe(e)}"))
        if args.verbose else None,
        on_finished=lambda reason: print(f"回放结束（{reason}）。"),
        on_error=lambda msg: print(f"  [错误] {msg}"),
    )

    if args.dry_run:
        player.set_controllers(mouse_ctl=_DryRunController("mouse"),
                               keyboard_ctl=_DryRunController("keyboard"))

    player.load(events, str(payload.get("name") or ""))

    if args.mode == MODE_TIMED:
        return _run_timed(player, events, args)
    return _run_step(player, steps, args)


# ---------------------------------------------------------------------------
# 模式一
# ---------------------------------------------------------------------------
def _run_timed(player: MacroPlayer, events: list[dict], args: argparse.Namespace) -> int:
    _countdown(args.countdown)
    player.speed = max(0.1, args.speed)
    print(f"▶ 开始回放（{player.speed:g}× 速度，{len(events) - args.start_index} 条事件）…")
    t0 = time.monotonic()
    if not player.start_timed(from_index=args.start_index, speed=args.speed):
        print("启动回放失败。")
        return 1

    # 监听 Ctrl+C：第一次停止，第二次强制退出
    try:
        while player.is_replaying:
            time.sleep(0.05)
            if player.is_paused:
                print("  … 已暂停")
                while player.is_paused and player.is_replaying:
                    time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，停止回放。")
        player.stop()
    elapsed = time.monotonic() - t0
    print(f"回放耗时 {elapsed:.3f} 秒（时间轴 / 速度 {player.speed:g}）。")
    return 0


# ---------------------------------------------------------------------------
# 模式二
# ---------------------------------------------------------------------------
def _run_step(player: MacroPlayer, steps: list, args: argparse.Namespace) -> int:
    _countdown(args.countdown)
    print("单步模式：回车=下一步   b=上一步   r=重置   q=退出")
    executed = 0
    try:
        if args.auto:
            while True:
                result = player.step_next()
                if result is None:
                    print("已到最后一步。")
                    break
                executed += 1
                _print_step(player, result)
                time.sleep(max(0.0, args.interval))
        else:
            while True:
                command = input("步骤> ").strip().lower()
                if command in ("q", "quit", "exit"):
                    break
                if command in ("b", "back", "p"):
                    result = player.step_prev()
                    if result is None or result <= 0:
                        print("  已回到开头，无法再回退。")
                    else:
                        _print_step(player, result, prefix="回退到")
                    continue
                if command in ("r", "reset"):
                    player.step_reset()
                    print("  步骤指针已重置到开头（步骤 0）。")
                    continue
                result = player.step_next()
                if result is None:
                    print("  已到最后一步，按 r 重置指针。")
                    continue
                executed += 1
                _print_step(player, result)
    except (KeyboardInterrupt, EOFError):
        print("\n退出单步模式。")
    print(f"共执行 {executed} 步。")
    return 0


def _print_step(player: MacroPlayer, index: int, prefix: str = "执行步骤") -> None:
    step = player.steps[index - 1]
    print(f"  {prefix} {index}/{player.step_total}：{step.description}"
          f"（原始事件 {step.raw_start + 1}–{step.raw_end}）")


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _countdown(seconds: float) -> None:
    if seconds <= 0:
        return
    print(f"{seconds:g} 秒后开始，请把鼠标焦点切到目标窗口…")
    remain = seconds
    while remain > 0:
        time.sleep(min(1.0, remain))
        remain -= 1.0
        if remain > 0:
            print(f"  {int(remain)}…")


class _DryRunController:
    """dry-run 用的假控制器：只打印调用，不真的动鼠标键盘。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._position = (0, 0)

    def __setattr__(self, name, value) -> None:
        if name == "position":
            object.__setattr__(self, "_position", value)
            print(f"  [dry-run] {self.kind}.position = {value}")
            return
        object.__setattr__(self, name, value)

    @property
    def position(self):
        return self._position

    def press(self, key) -> None:
        print(f"  [dry-run] {self.kind}.press({key})")

    def release(self, key) -> None:
        print(f"  [dry-run] {self.kind}.release({key})")

    def scroll(self, dx, dy) -> None:
        print(f"  [dry-run] {self.kind}.scroll({dx}, {dy})")

    def release_all(self) -> None:
        print(f"  [dry-run] {self.kind}.release_all()")


if __name__ == "__main__":
    sys.exit(main())
