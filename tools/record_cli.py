"""命令行录制工具（开发阶段验证录制链路，不依赖 Qt）。

用法::

    python tools/record_cli.py                       # 录制 5 秒后自动停止
    python tools/record_cli.py -d 10 -o data/scripts/demo.json
    python tools/record_cli.py --stop-key f9         # 也可以按 F9 提前停止
    python tools/record_cli.py --no-mouse-move       # 不记录鼠标移动
    python tools/record_cli.py --raw                 # 打印每一条事件

录制期间请手动操作鼠标键盘；倒计时结束后开始记录。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

try:  # 兼容 `python tools/record_cli.py` 与 `python -m tools.record_cli`
    from tools._common import PROJECT_ROOT, default_scripts_dir, fmt_ms, setup_console_logging  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    from _common import PROJECT_ROOT, default_scripts_dir, fmt_ms, setup_console_logging  # type: ignore  # noqa: F401

from core import event_parser as ep
from core.recorder import Recorder


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MacroSmith 命令行录制工具")
    parser.add_argument("-d", "--duration", type=float, default=5.0,
                        help="录制时长（秒），默认 5")
    parser.add_argument("--countdown", type=float, default=3.0,
                        help="开始前的倒计时秒数，默认 3")
    parser.add_argument("-o", "--output", type=str, default=None,
                        help="输出 JSON 路径；默认保存到脚本目录并自动命名")
    parser.add_argument("-n", "--name", type=str, default=None, help="脚本名称")
    parser.add_argument("--stop-key", type=str, default="f9",
                        help="提前停止录制的热键（默认 f9，留空则禁用）")
    parser.add_argument("--min-interval", type=int, default=50,
                        help="移动抖动过滤：最小时间间隔 ms（默认 50）")
    parser.add_argument("--min-distance", type=int, default=2,
                        help="移动抖动过滤：最小位移 px（默认 2）")
    parser.add_argument("--raw", action="store_true", help="实时打印每条事件")
    parser.add_argument("--no-save", action="store_true", help="只统计，不写文件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_console_logging()
    args = parse_args(argv)

    print("=" * 66)
    print("MacroSmith 命令行录制")
    print(f"  录制时长   : {args.duration} 秒")
    print(f"  录制内容   : 鼠标移动 + 点击 + 滚轮 + 键盘"
          f"（抖动过滤 {args.min_interval}ms / {args.min_distance}px）")
    print(f"  提前停止   : {args.stop_key or '未设置'}")
    print("=" * 66)

    stop_event = threading.Event()

    def on_event(event: dict) -> None:
        if args.raw:
            print(f"  [{fmt_ms(int(event.get('t', 0)))}] {ep.type_label(event):<6} "
                  f"{ep.describe(event)}")
        if not stop_event.is_set() and _is_stop_key(event, args.stop_key):
            print("\n检测到停止热键，提前结束录制…")
            stop_event.set()

    recorder = Recorder(
        is_executing=lambda: False,
        on_event=on_event,
        on_error=lambda msg: print(f"  [错误] {msg}"),
    )

    for remain in range(int(args.countdown), 0, -1):
        print(f"  {remain} 秒后开始录制，请把焦点切到要操作的窗口…")
        time.sleep(1.0)

    if not recorder.start(min_interval_ms=args.min_interval,
                          min_distance_px=args.min_distance):
        print("录制启动失败：监听器无法启动。")
        return 2

    print("● 正在录制… 请操作鼠标与键盘。")
    deadline = time.monotonic() + args.duration
    last_report = 0.0
    while time.monotonic() < deadline and not stop_event.is_set():
        time.sleep(0.05)
        now = time.monotonic()
        if not args.raw and now - last_report >= 1.0:
            last_report = now
            left = max(0.0, deadline - now)
            print(f"  已录制 {args.duration - left:4.1f}s / {recorder.event_count} 条事件")
    if stop_event.is_set():
        time.sleep(0.3)   # 让监听线程把最后几条事件写完

    events = recorder.stop()
    duration_ms = events[-1]["t"] if events else 0

    print("-" * 66)
    print(f"录制结束：共 {len(events)} 条事件，时长 {duration_ms / 1000.0:.3f} 秒")
    _print_summary(events, recorder)

    if args.no_save:
        print("已按 --no-save 跳过保存。")
        return 0
    if not events:
        print("没有录到任何事件，未保存。")
        return 1

    if args.output:
        from pathlib import Path
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = PROJECT_ROOT / out_path
    else:
        name = args.name or time.strftime("录制-%Y%m%d-%H%M%S")
        out_path = default_scripts_dir() / f"{name}.json"

    try:
        saved = ep.save_script(out_path, args.name or out_path.stem, events, duration_ms)
    except ep.ScriptError as exc:
        print(f"保存失败：{exc}")
        return 1
    print(f"已保存：{saved}")
    print(f"回放测试：python tools/play_cli.py \"{saved}\" --mode timed")
    return 0


def _is_stop_key(event: dict, token: str) -> bool:
    """判断事件是否为约定的停止热键（按下）。"""
    if not token or event.get("type") != "key" or event.get("action") != "press":
        return False
    from core.hotkey_manager import normalize_token, parse_key

    want = parse_key(token)
    if want is None:
        return False
    key = event.get("key")
    if isinstance(want, object) and getattr(want, "char", None):
        return key == f"char:{want.char}"
    vk = getattr(want, "vk", None)
    if vk is not None:
        return key == f"vk:{vk}"
    name = getattr(want, "name", None)
    return bool(name) and key == name


def _print_summary(events: list[dict], recorder: Recorder) -> None:
    """打印事件类型统计，便于快速判断录制是否正常。"""
    counts: dict[str, int] = {}
    for event in events:
        label = ep.type_label(event)
        counts[label] = counts.get(label, 0) + 1
    print("事件统计：")
    for label, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {label:<8} {count:>6} 条")
    steps = ep.build_steps(events)
    print(f"操作单元（模式二）：{len(steps)} 步")
    for step in steps[:8]:
        print(f"  {step.index:>3}. {step.description}")
    if len(steps) > 8:
        print(f"  … 其余 {len(steps) - 8} 步省略")


if __name__ == "__main__":
    sys.exit(main())
