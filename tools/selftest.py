"""一键自检脚本：用命令行验证 MacroSmith 的核心链路是否正常。

    python tools/selftest.py

自检内容
--------
1. 运行环境（Python 版本、PySide6 / pynput 是否就绪）
2. 配置文件读写（热键、窗口位置、回放选项）
3. 事件分组（模式二操作单元）与 JSON 保存 / 重新载入
4. 模式一原速回放的时间轴精度（用假控制器，不真的操作键鼠）
5. 模式二单步执行的指针推进与回退
6. 忽略自身事件标志位（回放中 ``is_executing`` 必须为 True）
7. 热键键名解析
8. 无边框窗口圆角遮罩计算

自检不模拟“真实键鼠钩子”（那需要人工操作），录制/回放的注入链路
请配合 ``tools/record_cli.py`` 与 ``tools/play_cli.py`` 人工验证。
"""

from __future__ import annotations

import ctypes
import inspect
import sys
import tempfile
import threading
import time
from pathlib import Path

try:  # 兼容 `python tools/selftest.py` 与 `python -m tools.selftest` 两种运行方式
    from tools._common import PROJECT_ROOT, setup_console_logging  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    from _common import PROJECT_ROOT, setup_console_logging  # type: ignore  # noqa: F401

from core import event_parser as ep

try:  # Qt 相关检查（弹窗样式 / 窗口缩放）需要 PySide6
    import PySide6  # noqa: F401

    QT_AVAILABLE = True
except ImportError:  # pragma: no cover
    QT_AVAILABLE = False

PASS = "  [通过]"
FAIL = "  [失败]"
WARN = "  [注意]"

_results: list[tuple[str, bool, str]] = []


def check(title: str, ok: bool, detail: str = "") -> bool:
    """记录一条检查结果并打印。"""
    _results.append((title, ok, detail))
    print(f"{PASS if ok else FAIL} {title}" + (f" —— {detail}" if detail else ""))
    return ok


# ---------------------------------------------------------------------------
def check_environment() -> None:
    print("\n[1] 运行环境")
    check(f"Python 版本 {sys.version.split()[0]}", sys.version_info >= (3, 11),
          "需要 3.11 及以上")
    try:
        import PySide6
        check(f"PySide6 {PySide6.__version__}", True)
    except ImportError as exc:
        check("PySide6 可用", False, f"{exc}（请执行 pip install -r requirements.txt）")
    try:
        import pynput
        version = getattr(pynput, "__version__", "未知")
        check(f"pynput {version}", True)
    except ImportError as exc:
        check("pynput 可用", False, str(exc))


def check_config() -> None:
    print("\n[2] 配置文件")
    from core.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg = Config(path)
        check("默认热键加载", cfg.hotkey("record_stop") == "f9",
              f"record_stop={cfg.hotkey('record_stop')}")
        cfg.set_hotkey("step_next", "Page_Down")
        cfg.set("window.x", 111)
        cfg.set("window.y", 222)
        check("配置保存", cfg.save(), str(path))

        cfg2 = Config(path)
        check("配置重新载入",
              cfg2.hotkey("step_next") == "page_down" and cfg2.get("window.x") == 111,
              f"step_next={cfg2.hotkey('step_next')}, x={cfg2.get('window.x')}")

        # 损坏文件容错
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        cfg3 = Config(path)
        check("损坏配置自动回退默认值", cfg3.hotkey("play_toggle") == "f6")

    real = Config()
    check("开发阶段配置文件存在", real.path.exists(), str(real.path))
    check("脚本目录可用", real.scripts_path().exists(), str(real.scripts_path()))


def _sample_events() -> list[dict]:
    """构造一段含各种事件类型的样例序列（时间戳已知，便于校验精度）。"""
    return [
        ep.make_move_event(0, 100, 100),
        ep.make_move_event(10, 105, 104),      # 抖动：与上一帧10ms/5px
        ep.make_move_event(20, 108, 110),      # 抖动
        ep.make_move_event(200, 300, 400),     # 正常移动
        ep.make_click_event(260, "press", "left", 300, 400),
        ep.make_click_event(300, "release", "left", 300, 400),
        ep.make_move_event(400, 320, 420),
        ep.make_move_event(410, 321, 420),     # 抖动
        ep.make_scroll_event(600, 0, -2, 320, 420),
        ep.make_key_event(700, "press", "vk:65", 65),
        ep.make_key_event(740, "release", "vk:65", 65),
        ep.make_key_event(900, "press", "vk:16", 16),
        ep.make_key_event(950, "press", "vk:65", 65),
        ep.make_key_event(980, "release", "vk:65", 65),
        ep.make_key_event(1050, "release", "vk:16", 16),
    ]


def check_parser() -> None:
    print("\n[3] 事件分组与 JSON 读写")
    events = _sample_events()
    steps = ep.build_steps(events)
    kinds = [s.kind for s in steps]
    print(f"    分组结果（{len(steps)} 步）：")
    for step in steps:
        print(f"      {step.index:>2}. [{step.kind:<6}] {step.description}"
              f"  （原始事件 {step.raw_start + 1}-{step.raw_end}）")

    check("原始事件总数正确", len(events) == 15, f"{len(events)} 条")
    check("步骤总数正确", len(steps) == 6, f"{len(steps)} 步")
    check("连续移动合并为一步", kinds[0] == "move" and steps[0].event_count == 4,
          f"首步合并 {steps[0].event_count} 帧")
    check("按下+释放合并为一步点击", kinds[1] == "click" and steps[1].event_count == 2,
          steps[1].description)
    check("滚轮单独成步", steps[3].kind == "scroll", steps[3].description)
    check("键盘按下+释放合并为一步",
          kinds[4] == "key" and steps[4].event_count == 2 and steps[4].hold_ms == 40,
          f"{steps[4].description}（按住 {steps[4].hold_ms}ms）")
    check("Shift+A 合并为一步组合键",
          steps[5].kind == "combo" and steps[5].description == "组合键 Shift+A",
          steps[5].description)
    check("组合键记录了修饰键与目标键",
          steps[5].modifiers == ["Shift"] and steps[5].target_key == "A"
          and steps[5].event_count == 4,
          f"mods={steps[5].modifiers} target={steps[5].target_key} "
          f"事件={steps[5].event_count}")
    check("步骤区间可覆盖全部事件",
          steps[-1].raw_end == len(events) and steps[0].raw_start == 0,
          f"raw {steps[0].raw_start}-{steps[-1].raw_end}")

    # 收尾清洗：未释放的键要补 release
    dirty = [ep.make_key_event(0, "press", "vk:65", 65)]
    clean = ep.sanitize_events(dirty)
    check("收尾清洗补齐未释放的按键", len(clean) == 2 and clean[-1]["action"] == "release")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "demo.json"
        saved = ep.save_script(path, "自检脚本", events)
        payload = ep.load_script(saved)
        check("JSON 保存并重新载入", payload["events"] == events,
              f"{saved.name}，{len(payload['events'])} 条事件")
        check("脚本元信息完整",
              payload.get("app") == "MacroSmith" and payload.get("duration_ms") == 1050,
              f"duration={payload.get('duration_ms')}ms")


def check_player_timed() -> None:
    print("\n[4] 模式一 原速回放（时间轴精度）")
    from core.player import MacroPlayer

    events = [
        ep.make_move_event(0, 10, 10),
        ep.make_move_event(120, 20, 20),
        ep.make_move_event(240, 30, 30),
        ep.make_move_event(360, 40, 40),
    ]
    mouse = _SpyController("mouse")
    player = MacroPlayer()
    player.set_controllers(mouse_ctl=mouse, keyboard_ctl=_SpyController("kbd"))
    player.load(events, "timing")

    t0 = time.monotonic()
    player.start_timed()
    while player.is_replaying:
        time.sleep(0.005)
    elapsed = time.monotonic() - t0

    marks = _move_times(mouse.log)
    expected = 0.36
    check("回放线程正常结束", len(marks) == 4, f"执行 {len(marks)} 帧移动")
    check("总耗时接近时间轴", abs(elapsed - expected) < 0.08,
          f"实测 {elapsed:.3f}s / 期望 {expected:.3f}s")
    if len(marks) == 4:
        offsets = [m - marks[0] for m in marks]
        worst = max(abs(offsets[i] * 1000 - 120 * i) for i in range(4))
        check("逐帧落点误差 < 20ms", worst < 20.0, f"最大误差 {worst:.1f}ms")
        check("移动落点内容正确", mouse.moves() == [(10, 10), (20, 20), (30, 30), (40, 40)],
              str(mouse.moves()))
    check("回放结束后 is_executing 归位", player.is_executing is False)

    # ---- 注入期间标志位必须为 True：在控制器回调里精确采样，避免时序竞态 ----
    # 后台线程的注入窗口只有微秒级，用外部轮询采样会漏，因此在鼠标
    # press 回调里直接读取标志位（这正是录制器判断“是否自己发的”那一刻）。
    flag_samples: list[bool] = []
    original_press = mouse.press

    def press_probe(key):
        flag_samples.append(player.is_executing)
        original_press(key)

    mouse.press = press_probe
    mouse.clear()
    player.load([ep.make_click_event(0, "press", "left", 1, 1),
                 ep.make_click_event(10, "release", "left", 1, 1)], "flag-timed")
    player.start_timed()
    while player.is_replaying:
        time.sleep(0.005)
    mouse.press = original_press
    check("模式一注入瞬间标志位为 True", flag_samples == [True],
          f"采样 {len(flag_samples)} 次：{flag_samples}")

    # 暂停 / 继续
    player.load(events, "timing")
    mouse.clear()
    player.start_timed()
    time.sleep(0.12)
    check("暂停切换成功", player.toggle_pause() is True)
    time.sleep(0.05)   # 给回放线程一点时间落到“暂停”分支
    paused_count = len(_move_times(mouse.log))
    time.sleep(0.25)
    check("暂停期间不再执行事件", len(_move_times(mouse.log)) == paused_count,
          f"暂停前 {paused_count} 帧，暂停后仍为 {len(_move_times(mouse.log))} 帧")
    check("继续切换成功", player.toggle_pause() is False)
    time.sleep(0.5)
    check("继续后回放走完剩余事件", len(_move_times(mouse.log)) == 4,
          f"共 {len(_move_times(mouse.log))} 帧")
    player.stop()
    check("停止后指针归零", player.position == 0 and not player.is_replaying)


def _move_times(log: list[tuple]) -> list[float]:
    """取出 spy 控制器记录的“移动时刻”列表（模式一时间轴精度校验用）。"""
    return [entry[2] for entry in log if entry[0] == "move" and len(entry) > 2]


def check_player_step() -> None:
    print("\n[5] 模式二 单步执行")
    from core.player import MacroPlayer

    events = _sample_events()
    steps = ep.build_steps(events)
    mouse = _SpyController("mouse")
    keyboard = _SpyController("kbd")
    player = MacroPlayer()
    player.set_controllers(mouse_ctl=mouse, keyboard_ctl=keyboard)
    player.load(events, "steps")

    check("步骤总数与解析一致", player.step_total == len(steps), f"{player.step_total} 步")

    index = player.step_next()
    check("第一步推进到 1", index == 1, f"step_index={index}")
    check("合并移动只执行落点", mouse.moves() == [(300, 400)], f"moves={mouse.moves()}")

    mouse.clear()
    index = player.step_next()
    click_actions = mouse.actions()
    check("第二步为左键单击", index == 2 and click_actions == [
        ("mouse_press", "left"), ("mouse_release", "left")
    ], f"actions={click_actions}")

    for _ in range(3):
        player.step_next()
    check("继续推进到第 5 步", player.step_index == 5, f"step_index={player.step_index}")
    check("滚轮步骤被注入", ("scroll", 0, -2) in mouse.actions(), str(mouse.actions()))

    back = player.step_prev()
    check("回退会真正退一步并重执行上一步", back == 4 and player.step_index == 4,
          f"回退后 step_index={back}")

    player.step_reset()
    check("重置指针归零", player.step_index == 0)

    # 走到末尾
    while player.step_next() is not None:
        pass
    check("走到末尾指针等于总步数", player.step_index == player.step_total,
          f"{player.step_index}/{player.step_total}")
    check("末尾继续调用返回 None", player.step_next() is None)

    # 键盘步骤已合并：第 5 步是「键盘 A 按下并释放」，一次执行完两条事件
    keyboard.clear()
    player.step_reset()
    player.run_step_at(4)
    key_actions = keyboard.actions()
    check("合并的键盘步骤一次注入按下+释放",
          key_actions == [("press", "vk:65"), ("release", "vk:65")], str(key_actions))

    # 组合键已合并成一步：一次触发完 修饰按下 -> 目标按下/释放 -> 修饰释放
    player.step_reset()
    keyboard.clear()
    player.run_step_at(5)   # 组合键 Shift+A
    shift_first = keyboard.actions()
    check("组合键一次注入 修饰按下 -> 目标按下/释放 -> 修饰释放",
          shift_first == [("press", "vk:16"), ("press", "vk:65"),
                          ("release", "vk:65"), ("release", "vk:16")],
          str(shift_first))

    # 合并后不允许出现「只按下没释放」的卡键
    keyboard.clear()
    player.step_reset()
    while player.step_next() is not None:
        pass
    held: set[str] = set()
    for action, token in keyboard.actions():
        if action == "press":
            held.add(token)
        else:
            held.discard(token)
    check("所有按键都成对释放（无卡键）", held == set(), f"未释放：{held}")


def check_execution_flag() -> None:
    print("\n[6] 忽略自身事件（is_executing 标志位）")
    from core.player import MacroPlayer

    mouse = _SpyController("mouse")
    keyboard = _SpyController("kbd")
    player = MacroPlayer()
    player.set_controllers(mouse_ctl=mouse, keyboard_ctl=keyboard)

    # 直接验证“注入时控制器里能观察到的标志位”：
    # spy 控制器在 press 的瞬间采样 is_executing，避免时序竞态。
    sampled: list[bool] = []
    real_press = keyboard.press

    def spy_press(key):
        sampled.append(player.is_executing)
        real_press(key)

    keyboard.press = spy_press

    player.load([ep.make_key_event(0, "press", "vk:65", 65)], "flag")
    player.step_next()
    check("注入事件期间标志位为 True", sampled == [True], f"observed={sampled}")
    check("注入结束后标志位为 False", player.is_executing is False)

    # 模式一在后台线程注入，同样必须置位（在控制器回调里精确采样）
    timed_samples: list[bool] = []
    original_press2 = keyboard.press
    original_release2 = keyboard.release

    def press_probe2(key):
        timed_samples.append(player.is_executing)
        original_press2(key)

    def release_probe2(key):
        timed_samples.append(player.is_executing)
        original_release2(key)

    keyboard.press = press_probe2
    keyboard.release = release_probe2
    player.load([ep.make_key_event(0, "press", "vk:65", 65),
                 ep.make_key_event(20, "release", "vk:65", 65)], "flag-timed")
    player.start_timed()
    while player.is_replaying:
        time.sleep(0.005)
    keyboard.press = original_press2
    keyboard.release = original_release2
    check("模式一注入瞬间标志位为 True", timed_samples == [True, True],
          f"observed={timed_samples}")

    # 录制器读取同一个标志位：True 时必须跳过记录
    from core.recorder import Recorder

    recorded: list[dict] = []
    recorder = Recorder(is_executing=lambda: True, on_event=recorded.append)
    recorder._recording = True
    recorder._t0 = time.monotonic()
    recorder._handle_press(type("K", (), {"vk": 65, "char": None})())
    check("标志位为 True 时录制器跳过事件", recorded == [], f"recorded={len(recorded)} 条")


def check_hotkeys() -> None:
    print("\n[7] 热键解析")
    from core.hotkey_manager import HotkeyManager, display_name, normalize_token, parse_key

    check("F9 解析", parse_key("f9") is not None, display_name("f9"))
    check("PageDown 解析", parse_key("PageDown") is not None, display_name("page_down"))
    check("别名 PgDn 归一化", normalize_token("PgDn") == "page_down")
    check("字母键解析", parse_key("a") is not None)
    check("非法键名返回 None", parse_key("这不是键") is None)

    mgr = HotkeyManager()
    failed = mgr.register_all({"play_toggle": "f6", "step_next": "page_down",
                               "bad": "这不是键"})
    check("批量注册返回失败列表", failed == ["bad"], f"failed={failed}")
    check("热键显示文本", mgr.binding_display("step_next") == "PageDown",
          mgr.binding_display("step_next"))
    check("重复键检测", mgr.duplicate_actions("play_toggle", "f6") == []
          and mgr.duplicate_actions("play_toggle", "page_down") == ["step_next"],
          str(mgr.duplicate_actions("play_toggle", "page_down")))


def check_window_geometry() -> None:
    print("\n[8] 原生窗口与配置路径")
    from ui.frameless import WindowShell

    check("外壳默认尺寸常量",
          (WindowShell.DEFAULT_WIDTH, WindowShell.DEFAULT_HEIGHT) == (1000, 700),
          f"{WindowShell.DEFAULT_WIDTH}x{WindowShell.DEFAULT_HEIGHT}")
    check("外壳最小尺寸常量",
          (WindowShell.MIN_WIDTH, WindowShell.MIN_HEIGHT) == (820, 560),
          f"{WindowShell.MIN_WIDTH}x{WindowShell.MIN_HEIGHT}")
    check("无边框残留 API 已清理",
          not hasattr(WindowShell, "_RESIZE_MARGIN")
          and not hasattr(WindowShell, "apply_mask")
          and not hasattr(WindowShell, "_edges_at")
          and not hasattr(WindowShell, "_update_cursor"),
          "热区/遮罩/自定义光标逻辑已随原生窗口一并移除")

    from utils.win_utils import WINDOW_TITLE, app_data_dir, is_frozen, project_root
    check("窗口标题符合规范", WINDOW_TITLE == "宏匠", WINDOW_TITLE)
    check("开发阶段判定", is_frozen() is False, f"项目根目录 {project_root()}")
    print(f"    正式发布时的数据目录：{app_data_dir()}")


class _SpyController:
    """假控制器：记录所有调用，用于在不触碰真实键鼠的情况下验证回放。

    ``log`` 里每条记录形如 ``("move", (x, y))`` / ``("press", "vk:65")`` /
    ``("mouse_press", "left")``，便于逐项断言“到底注入了什么”。
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._position = (0, 0)
        self.log: list[tuple] = []

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, value) -> None:
        self._position = value
        # 同时记录动作时刻，用于校验模式一的时间轴精度
        self.log.append(("move", tuple(value), time.monotonic()))

    def press(self, key) -> None:
        if self.kind == "mouse":
            self.log.append(("mouse_press", str(key).split(".")[-1].rstrip(":)>")))
        else:
            self.log.append(("press", _key_token(key)))

    def release(self, key) -> None:
        if self.kind == "mouse":
            self.log.append(("mouse_release", str(key).split(".")[-1].rstrip(":)>")))
        else:
            self.log.append(("release", _key_token(key)))

    def scroll(self, dx, dy) -> None:
        self.log.append(("scroll", dx, dy))

    def release_all(self) -> None:
        self.log.append(("release_all",))

    # -- 便于断言的便捷视图 ------------------------------------------
    def moves(self) -> list[tuple[int, int]]:
        return [entry[1] for entry in self.log if entry[0] == "move"]

    def actions(self) -> list[tuple]:
        """过滤掉兜底 release_all，只看真正的注入动作。"""
        return [entry for entry in self.log if entry[0] != "release_all"]

    def clear(self) -> None:
        self.log.clear()


def _key_token(key) -> str:
    """把 pynput 按键对象转成 ``vk:xx`` / ``char:x`` 便于断言。"""
    vk = getattr(key, "vk", None)
    if isinstance(vk, int):
        return f"vk:{vk}"
    char = getattr(key, "char", None)
    if char:
        return f"char:{char}"
    return str(key)


def check_step_editing() -> None:
    """第二阶段新增：步骤编辑（增删改移）与时间戳重排。"""
    print("\n[9] 步骤编辑与时间戳重排")
    events = _sample_events()
    steps = ep.build_steps(events)
    before = len(steps)

    deleted = ep.delete_steps(steps, [1])
    check("删除步骤", len(ep.build_steps(deleted)) == before - 1,
          f"{before} 步 -> {len(ep.build_steps(deleted))} 步")
    times = [int(e["t"]) for e in ep.strip_internal_fields(deleted)]
    check("删除后时间戳单调不减", times == sorted(times), f"{times}")

    moved, mapping = ep.move_steps(steps, [0], 1)
    original = [s.description for s in steps]
    after_move = [s.description for s in ep.build_steps(moved)]
    check("下移步骤交换顺序",
          after_move[0] == original[1] and after_move[1] == original[0],
          f"{after_move[0]} / {after_move[1]}")
    check("重排后不再合并相邻步骤",
          len(ep.build_steps(moved)) == before,
          f"{len(ep.build_steps(moved))} 步（原 {before} 步）")

    new_events, desc = ep.make_step_events("click", x=123, y=456, button="right")
    inserted, index = ep.insert_step(steps, 2, new_events, desc)
    inserted_steps = ep.build_steps(inserted)
    check("插入新步骤", index == 2 and len(inserted_steps) == before + 1,
          f"在第 {index + 1} 步插入「{desc}」")
    check("插入后时间戳单调不减",
          [int(e["t"]) for e in ep.strip_internal_fields(inserted)]
          == sorted(int(e["t"]) for e in ep.strip_internal_fields(inserted)))

    check("段内按住时长保留",
          _click_hold_preserved(), "规整后 按下→释放 间隔保持 80ms")

    # 编辑后仍能正常回放（用假控制器验证注入链路没被破坏）
    from core.player import MacroPlayer

    keyboard = _SpyController("kbd")
    player = MacroPlayer()
    player.set_controllers(mouse_ctl=_SpyController("mouse"), keyboard_ctl=keyboard)
    player.load(moved, "edited")
    player.step_reset()
    player.step_next()
    check("编辑后的序列可回放", player.step_index == 1 and player.step_total == before,
          f"步骤 {player.step_index}/{player.step_total}")


def _click_hold_preserved() -> bool:
    """点击自身的按住时长不应被时间戳重排抹掉。"""
    events = [ep.make_move_event(0, 1, 1),
              ep.make_click_event(900, "press", "left", 1, 1),
              ep.make_click_event(980, "release", "left", 1, 1),
              ep.make_scroll_event(2000, 0, -1, 1, 1)]
    times = [int(e["t"]) for e in ep.normalize_timestamps(events)]
    return (times[1] - times[0] == ep.MIN_STEP_GAP_MS
            and times[2] - times[1] == 80
            and times[3] - times[2] == ep.MIN_STEP_GAP_MS)


def check_always_records_moves() -> None:
    """第九阶段：「仅录制点击坐标」已删除，录制始终记录移动 + 点击。"""
    print("\n[10] 始终记录鼠标移动与点击")
    from pynput import mouse as ms

    from core.recorder import Recorder

    recorded: list[dict] = []
    recorder = Recorder(is_executing=lambda: False, on_event=recorded.append)
    recorder.start()
    recorder._recording = True          # 不依赖真实钩子
    recorder._t0 = time.monotonic()
    check("clicks_only 参数已移除",
          "clicks_only" not in inspect.signature(recorder.start).parameters)

    recorder._handle_move(100, 100)
    recorder._handle_move(400, 400)
    check("移动轨迹被记录", recorder.event_count == 2,
          f"{recorder.event_count} 条事件")

    recorder._handle_click(321, 654, ms.Button.left, True)
    recorder._handle_click(321, 654, ms.Button.left, False)
    recorder._handle_scroll(321, 654, 0, -2)
    recorder._handle_press(type("K", (), {"vk": 65, "char": None})())
    events = recorder.get_events()
    kinds = [e["type"] for e in events]
    check("移动/点击/滚轮/键盘全部记录",
          kinds == ["mouse_move", "mouse_move", "mouse_click", "mouse_click",
                    "mouse_scroll", "key"], str(kinds))
    check("点击坐标被记录",
          (events[2]["x"], events[2]["y"]) == (321, 654),
          f"({events[2]['x']}, {events[2]['y']})")


def check_step_timing() -> None:
    """第三阶段新增：步骤时间编辑与总时长重设。"""
    print("\n[11] 步骤时间编辑与总时长重设")
    events = [
        ep.make_move_event(0, 1, 1),
        ep.make_scroll_event(1000, 0, -1, 1, 1),
        ep.make_key_event(2000, "press", "vk:65", 65),
        ep.make_key_event(2040, "release", "vk:65", 65),
    ]
    steps = ep.build_steps(events)
    check("步骤已合并（键盘按下+释放）",
          [s.kind for s in steps] == ["move", "scroll", "key"], str([s.kind for s in steps]))
    check("键盘步骤带按住时长", steps[2].hold_ms == 40, f"{steps[2].hold_ms}ms")

    moved = ep.set_step_time(events, 1, 5000)
    after = ep.build_steps(moved)
    check("单步时间可修改", after[1].t_start == 5000,
          f"第 2 步 {after[1].t_start}ms")
    check("后续步骤整体平移", after[2].t_start == 6000,
          f"第 3 步 {after[2].t_start}ms（间隔 1000ms 保持）")
    check("步骤内按住时长不受影响", after[2].hold_ms == 40)

    # 第九阶段：持续时长编辑（内部等比缩放 + 后续平移）
    dur = ep.set_step_duration(events, 2, 2000)
    dafter = ep.build_steps(dur)
    check("持续时长可修改（内部等比缩放）",
          dafter[2].t_start == 2000 and dafter[2].t_end == 4000,
          f"{dafter[2].t_start} → {dafter[2].t_end}ms")
    shifted = ep.set_step_duration(events, 1, 1000)
    safter = ep.build_steps(shifted)
    check("改时长后后续步骤整体平移", safter[2].t_start == 3000,
          f"第 3 步 {safter[2].t_start}ms")

    spread = ep.redistribute_duration(events, 30_000)
    check("总时长可重设为 30 秒", ep.events_duration_ms(spread) == 30_000,
          f"实际 {ep.events_duration_ms(spread)}ms")
    spread_steps = ep.build_steps(spread)
    check("重设后步骤均匀分布",
          [s.t_start for s in spread_steps] == sorted(s.t_start for s in spread_steps)
          and spread_steps[0].t_start == 0,
          f"起点 {[s.t_start for s in spread_steps]}")

    check("时长文本往返解析",
          ep.parse_duration("2:30") == 150_000 and ep.parse_duration("1500ms") == 1500
          and ep.format_duration(1500) == "1.500s",
          "2:30 / 1500ms / 1.500s")


def check_coordinate_clamp() -> None:
    """第三阶段新增：越界坐标钳制（避免回放时“看起来没有动作”）。"""
    print("\n[12] 越界坐标钳制")
    from utils.win_utils import clamp_to_virtual_screen, virtual_screen_rect

    left, top, width, height = virtual_screen_rect()
    print(f"    当前虚拟桌面：({left}, {top}) {width}x{height}")
    _x, _y, changed = clamp_to_virtual_screen(left + 10, top + 10)
    check("范围内的坐标不变", changed is False)

    cx, cy, changed = clamp_to_virtual_screen(left + width + 5000, top + height + 5000)
    check("越界坐标被钳制回范围", changed is True and left <= cx < left + width
          and top <= cy < top + height, f"({cx}, {cy})")

    from core.player import MacroPlayer

    mouse = _SpyController("mouse")
    player = MacroPlayer()
    player.set_controllers(mouse_ctl=mouse, keyboard_ctl=_SpyController("kbd"))
    player.load([ep.make_move_event(0, left + width + 999, top + height + 999),
                 ep.make_scroll_event(10, 0, -1, 0, 0)], "clamp")
    player.step_next()
    moved = mouse.moves()[0]
    check("回放时越界移动被钳制", left <= moved[0] < left + width
          and top <= moved[1] < top + height, str(moved))


def check_app_icon() -> None:
    """第三阶段新增：应用图标（蓝底白字「宏」）。"""
    print("\n[13] 应用图标")
    import struct

    icon_path = PROJECT_ROOT / "assets" / "icon.ico"
    check("assets/icon.ico 存在", icon_path.exists(), str(icon_path))
    if not icon_path.exists():
        return
    data = icon_path.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    check("ICO 文件头合法", reserved == 0 and kind == 1, f"count={count}")
    sizes = []
    for i in range(count):
        entry = data[6 + 16 * i:6 + 16 * (i + 1)]
        sizes.append(entry[0] or 256)
    check("包含任务栏/Alt+Tab 所需尺寸",
          256 in sizes and 32 in sizes and 48 in sizes, f"尺寸 {sizes}")
    check("图标文件大小合理", icon_path.stat().st_size > 2000,
          f"{icon_path.stat().st_size} 字节")


def check_no_spurious_clicks() -> None:
    """第四阶段 BUG 1：回放首尾不得凭空多出左键/右键。"""
    print("\n[14] 回放不得凭空产生鼠标点击")
    from core.player import MacroPlayer

    class _Spy:
        def __init__(self, kind: str = "mouse") -> None:
            self.kind = kind
            self._pos = (0, 0)
            self.log: list[tuple] = []
            self.buttons: list[tuple[str, str]] = []

        @property
        def position(self):
            return self._pos

        @position.setter
        def position(self, value):
            self._pos = tuple(value)
            self.log.append(("move", self._pos))

        @staticmethod
        def _name(key) -> str:
            return getattr(key, "name", str(key)).replace("Button.", "")

        def press(self, key):
            name = self._name(key)
            self.log.append(("press", name))
            if self.kind == "mouse":
                self.buttons.append(("press", name))

        def release(self, key):
            name = self._name(key)
            self.log.append(("release", name))
            if self.kind == "mouse":
                self.buttons.append(("release", name))

        def scroll(self, dx, dy):
            self.log.append(("scroll", dx, dy))

        def release_all(self):
            self.log.append(("release_all",))

    def play(events):
        player = MacroPlayer()
        mouse, keyboard = _Spy("mouse"), _Spy("kbd")
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=keyboard)
        player.load(events, "selftest")
        for _ in range(player.step_total + 1):
            player.step_next()
        return mouse

    # 只含左键的脚本：收尾不能冒出右/中键的 release
    mouse = play([
        ep.make_move_event(0, 200, 200),
        ep.make_click_event(50, "press", "left", 200, 200),
        ep.make_click_event(90, "release", "left", 200, 200),
    ])
    releases = [n for a, n in mouse.buttons if a == "release"]
    check("只含左键的脚本不会多出右键/中键",
          releases == ["left"], f"实际释放 {releases}")

    # 纯移动脚本：不应有任何按钮动作
    mouse = play([ep.make_move_event(0, 10, 10),
                  ep.make_scroll_event(40, 0, -1, 10, 10)])
    check("纯移动脚本不产生任何按钮动作",
          mouse.buttons == [], f"实际 {mouse.buttons}")

    # 脚本自己有右键时仍要正常执行
    mouse = play([ep.make_click_event(0, "press", "right", 300, 300),
                  ep.make_click_event(60, "release", "right", 300, 300)])
    check("脚本录制的右键仍正常按下+释放",
          mouse.buttons == [("press", "right"), ("release", "right")],
          f"实际 {mouse.buttons}")


def check_click_only_cursor() -> None:
    """第四阶段 BUG 2/3：只录点击的脚本必须先把指针移到坐标。"""
    print("\n[15] 仅录点击脚本的指针落点")
    from core.player import MacroPlayer

    class _SpyMouse:
        def __init__(self) -> None:
            self._pos = (0, 0)
            self.log: list[tuple] = []

        @property
        def position(self):
            return self._pos

        @position.setter
        def position(self, value):
            self._pos = tuple(value)
            self.log.append(("move", self._pos))

        def press(self, key):
            self.log.append(("press", getattr(key, "name", str(key))))

        def release(self, key):
            self.log.append(("release", getattr(key, "name", str(key))))

        def scroll(self, dx, dy):
            self.log.append(("scroll", dx, dy))

        def release_all(self):
            self.log.append(("release_all",))

    # 不含移动轨迹的脚本（只有带坐标的 mouse_click）也要能正常回放
    click_only = [
        ep.make_click_event(0, "press", "left", 138, 525),
        ep.make_click_event(40, "release", "left", 138, 525),
        ep.make_click_event(200, "press", "left", 900, 700),
        ep.make_click_event(240, "release", "left", 900, 700),
    ]
    check("仅录点击的事件全部是 mouse_click 且带坐标",
          all(e["type"] == "mouse_click" and "x" in e and "y" in e
              for e in click_only))

    player = MacroPlayer()
    mouse = _SpyMouse()
    player.set_controllers(mouse_ctl=mouse, keyboard_ctl=_SpyMouse())
    player.load(click_only, "clickonly")
    for _ in range(player.step_total):
        player.step_next()
    moves = [entry[1] for entry in mouse.log if entry[0] == "move"]
    check("模式二：点击前把指针移到录制坐标",
          moves == [(138, 525), (900, 700)], f"实际 {moves}")

    player2 = MacroPlayer()
    mouse2 = _SpyMouse()
    player2.set_controllers(mouse_ctl=mouse2, keyboard_ctl=_SpyMouse())
    player2.load(click_only, "clickonly")
    player2.speed = 20.0
    player2.start_timed()
    while player2.is_replaying:
        time.sleep(0.005)
    moves2 = [entry[1] for entry in mouse2.log if entry[0] == "move"]
    check("模式一：点击前把指针移到录制坐标",
          moves2 == [(138, 525), (900, 700)], f"实际 {moves2}")


def check_hotkey_exclusive() -> None:
    """第四阶段优化：热键独占（Win32 低级键盘钩子）。"""
    print("\n[16] 热键独占模式")
    from core.hotkey_hook import (HC_ACTION, KBDLLHOOKSTRUCT, LLKHF_INJECTED,
                                  WM_KEYDOWN, WM_KEYUP, KeyboardHook,
                                  hook_supported)
    from core.hotkey_manager import HotkeyManager, key_to_vk

    check("虚拟键码映射正确",
          key_to_vk("f6") == 0x75 and key_to_vk("f9") == 0x78
          and key_to_vk("page_down") == 0x22 and key_to_vk("home") == 0x24,
          f"F6={key_to_vk('f6')} F9={key_to_vk('f9')} "
          f"PageDown={key_to_vk('page_down')}")

    if not hook_supported():
        check("当前系统支持独占钩子", False, "仅 Windows 支持")
        return
    check("当前系统支持独占钩子", True)

    hits: list[int] = []
    hook = KeyboardHook(on_hotkey=hits.append, should_block=lambda vk: vk == 0x75)
    if not hook.start():
        check("独占钩子安装成功", False, hook.last_error)
        return
    check("独占钩子安装成功", True, f"线程 {hook._thread_id}")

    def call(vk: int, flags: int = 0, msg: int = WM_KEYDOWN) -> int:
        struct = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=flags,
                                 time=0, dwExtraInfo=0)
        lp = ctypes.cast(ctypes.pointer(struct), ctypes.c_void_p).value
        return hook._hook_proc(HC_ACTION, msg, lp)

    down = call(0x75)
    check("命中热键时吞掉按键（前台收不到）", down == 1, f"返回 {down}")
    check("吞键时触发对应动作", hits == [0x75], f"动作 {hits}")
    up = call(0x75, msg=WM_KEYUP)
    check("抬起不重复触发动作", up == 1 and hits == [0x75], f"动作 {hits}")

    passthrough = call(0x41)
    check("未绑定的键原样放行", passthrough == 0, f"返回 {passthrough}")

    injected = call(0x75, flags=LLKHF_INJECTED)
    check("程序自身注入的按键放行（避免自锁）", injected == 0, f"返回 {injected}")

    check("Win 键与 Esc 永不被独占",
          all(call(vk) == 0 for vk in (0x5B, 0x5C, 0x1B)))

    check("拦截计数在累计", hook.blocked_count >= 2,
          f"blocked_count={hook.blocked_count}")

    hook.stop()
    check("停止后钩子已卸载（系统键盘恢复正常）",
          not hook.is_running and not hook.hook_alive())

    # 管理器层面的接线
    mgr = HotkeyManager(exclusive=True)
    mgr.register_all({"play_toggle": "f6", "play_stop": "f8"})
    check("管理器按 vk 建立独占表",
          mgr._vkmap == {0x75: "play_toggle", 0x77: "play_stop"},
          str(mgr._vkmap))
    check("管理器判定需拦截的键", mgr._should_block_vk(0x75) is True
          and mgr._should_block_vk(0x41) is False)
    mgr.set_exclusive(False)
    check("独占开关可切换", mgr.exclusive is False)


def check_smart_merge() -> None:
    """第五阶段新增：组合键与双击/三击智能合并。"""
    print("\n[17] 组合键 / 双击三击智能合并")

    def _k(t, a, vk):
        return ep.make_key_event(t, a, f"vk:{vk}", vk)

    def _c(t, a, b="left", x=100, y=200):
        return ep.make_click_event(t, a, b, x, y)

    # ---- 组合键 ----
    combo = [_k(0, "press", 17), _k(15, "press", 86),
             _k(60, "release", 86), _k(75, "release", 17)]
    steps = ep.build_steps(combo)
    check("Ctrl+V 合并为一步组合键",
          len(steps) == 1 and steps[0].kind == "combo"
          and steps[0].description == "组合键 Ctrl+V",
          f"{len(steps)} 步 / {steps[0].description if steps else '?'}")
    check("组合键记录了修饰键与目标键",
          steps[0].modifiers == ["Ctrl"] and steps[0].target_key == "V",
          f"mods={steps[0].modifiers} target={steps[0].target_key}")
    check("组合键内部时间戳原样保留",
          [e["t"] for e in steps[0].events] == [0, 15, 60, 75],
          str([e["t"] for e in steps[0].events]))

    multi_mod = [_k(0, "press", 17), _k(10, "press", 16), _k(25, "press", 83),
                 _k(70, "release", 83), _k(85, "release", 16), _k(95, "release", 17)]
    steps2 = ep.build_steps(multi_mod)
    check("多修饰键 Ctrl+Shift+S 合并为一步",
          len(steps2) == 1 and steps2[0].description == "组合键 Ctrl+Shift+S",
          steps2[0].description if steps2 else "?")

    # 真实录制里 pynput 给不出 vk（Key.ctrl_l.vk is None），此时按键名识别
    token_combo = [ep.make_key_event(0, "press", "ctrl_l", None),
                   ep.make_key_event(15, "press", "vk:86", 86),
                   ep.make_key_event(60, "release", "vk:86", 86),
                   ep.make_key_event(75, "release", "ctrl_l", None)]
    steps3 = ep.build_steps(token_combo)
    check("按键名识别修饰键（真实录制形态）",
          len(steps3) == 1 and steps3[0].kind == "combo",
          f"{len(steps3)} 步：{[s.description for s in steps3]}")

    # ---- 关闭开关 ----
    off = ep.build_steps(combo, ep.MergeOptions.combo_off())
    check("关闭开关后保持拆分记录",
          [s.kind for s in off] == ["key", "key", "key"],
          str([s.description for s in off]))

    # ---- 双击 / 三击 ----
    double = [_c(0, "press"), _c(40, "release"), _c(120, "press"), _c(160, "release")]
    d_steps = ep.build_steps(double)
    check("两次快速点击合并为双击",
          len(d_steps) == 1 and d_steps[0].kind == "multi"
          and d_steps[0].description == "鼠标双击 (100, 200)",
          d_steps[0].description if d_steps else "?")
    check("双击记录了连击次数", d_steps[0].click_count == 2, str(d_steps[0].click_count))

    triple = double + [_c(240, "press"), _c(280, "release")]
    t_steps = ep.build_steps(triple)
    check("三次快速点击合并为三击",
          len(t_steps) == 1 and t_steps[0].click_count == 3
          and t_steps[0].description == "鼠标三击 (100, 200)",
          t_steps[0].description if t_steps else "?")

    # 连击次数不设上限：上限 3 会拆出「三击 + 单击」，模式二两次热键之间的
    # 延迟必然超过系统连击窗口，第 4 次点击永远识别不了 —— 必须整体合并。
    for n in (4, 6, 10):
        chain = []
        for i in range(n):
            chain += [_c(i * 120, "press"), _c(i * 120 + 40, "release")]
        built = ep.build_steps(chain)
        check(f"{n} 连击整体合并为一步（不设上限）",
              len(built) == 1 and built[0].kind == "multi"
              and built[0].click_count == n
              and sum(s.event_count for s in built) == len(chain),
              f"{len(built)} 步 / {built[0].description if built else '?'}")

    check("连击文案：2/3 用双击三击，其它用「连击N次」",
          ep.click_verb(2) == "双击" and ep.click_verb(3) == "三击"
          and ep.click_verb(5) == "连击5次",
          f"2={ep.click_verb(2)} 3={ep.click_verb(3)} 5={ep.click_verb(5)}")

    far = [_c(0, "press"), _c(40, "release"), _c(900, "press"), _c(940, "release")]
    check("间隔过大不合并", len(ep.build_steps(far)) == 2,
          f"{len(ep.build_steps(far))} 步")
    apart = [_c(0, "press", x=100, y=100), _c(40, "release", x=100, y=100),
             _c(120, "press", x=300, y=100), _c(160, "release", x=300, y=100)]
    check("坐标相差过大不合并", len(ep.build_steps(apart)) == 2,
          f"{len(ep.build_steps(apart))} 步")

    # ---- 手工拆分不被吞掉 ----
    manual = [
        ep.Step(index=1, kind="click", description="左键单击 (100, 200)",
                events=double[:2]),
        ep.Step(index=2, kind="click", description="左键单击 (100, 200)",
                events=double[2:]),
    ]
    flat = ep.steps_to_events(manual)
    check("手工拆分的步骤不会被再次合并",
          len(ep.build_steps(flat)) == 2,
          str([s.description for s in ep.build_steps(flat)]))

    # ---- 「拆分为独立单击」功能 ----
    chain5 = []
    for i in range(5):
        chain5 += [_c(i * 120, "press"), _c(i * 120 + 40, "release")]
    merged = ep.build_steps(chain5)
    groups = ep.split_click_step(merged[0])
    check("连击 5 次可拆成 5 组独立单击", len(groups) == 5, f"{len(groups)} 组")
    split_flat = ep.replace_step_with_many(merged, 0, groups)
    split_steps = ep.build_steps(split_flat)
    check("拆分后成为 5 个单击步骤",
          len(split_steps) == 5 and all(s.kind == "click" for s in split_steps),
          str([s.description for s in split_steps]))
    check("拆分不丢事件",
          len(split_flat) == len(chain5)
          and sum(s.event_count for s in split_steps) == len(chain5),
          f"{len(split_flat)}/{len(chain5)} 条")
    check("拆分结果不会被自动合并粘回去",
          len(ep.build_steps(ep.steps_to_events(split_steps))) == 5)

    # ---- 自动合并结果可逆 ----
    auto = ep.steps_to_events(ep.build_steps(combo))
    check("自动合并结果摊平后可重新识别",
          ep.build_steps(auto)[0].kind == "combo",
          ep.build_steps(auto)[0].description)
    check("关掉开关即可还原拆分形态",
          len(ep.build_steps(auto, ep.MergeOptions.combo_off())) == 3)


def check_merged_playback() -> None:
    """第五阶段新增：模式二对合并步骤的快速连续执行。"""
    print("\n[18] 合并步骤的快速连续执行")
    from core.player import MacroPlayer

    class _Spy:
        def __init__(self, kind="mouse"):
            self.kind = kind
            self._pos = (0, 0)
            self.log: list[tuple] = []
            self.actions: list[tuple] = []

        @property
        def position(self):
            return self._pos

        @position.setter
        def position(self, value):
            self._pos = tuple(value)
            self.log.append(("move", self._pos))

        @staticmethod
        def _name(key):
            return getattr(key, "name", str(key)).replace("Button.", "")

        def press(self, key):
            self.log.append(("press", self._name(key)))
            if self.kind == "mouse":
                self.actions.append(("press", self._name(key)))

        def release(self, key):
            self.log.append(("release", self._name(key)))
            if self.kind == "mouse":
                self.actions.append(("release", self._name(key)))

        def scroll(self, dx, dy):
            self.log.append(("scroll", dx, dy))

        def release_all(self):
            self.log.append(("release_all",))

    def play(events):
        player = MacroPlayer()
        mouse, keyboard = _Spy("mouse"), _Spy("kbd")
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=keyboard)
        player.load(events, "selftest")
        assert player.step_total == 1, f"应只有一步，实际 {player.step_total}"
        t0 = time.monotonic()
        player.step_next()
        return (time.monotonic() - t0) * 1000, keyboard, mouse

    combo = [
        ep.make_key_event(0, "press", "vk:17", 17),
        ep.make_key_event(15, "press", "vk:86", 86),
        ep.make_key_event(1600, "release", "vk:86", 86),
        ep.make_key_event(1615, "release", "vk:17", 17),
    ]
    elapsed, keyboard, _ = play(combo)
    check("组合键一次触发全部 4 条事件",
          [a for a, _ in keyboard.log if a != "release_all"]
          == ["press", "press", "release", "release"],
          str(keyboard.log))
    check("组合键快速执行（不受真实间隔影响）", elapsed < 300,
          f"耗时 {elapsed:.0f}ms（录制间隔 1615ms）")
    held = []
    for action, token in keyboard.log:
        if action == "press":
            held.append(token)
        elif action == "release" and token in held:
            held.remove(token)
    check("修饰键已释放（不卡 Ctrl）", not held, f"未释放 {held}")

    double = [
        ep.make_click_event(0, "press", "left", 10, 20),
        ep.make_click_event(40, "release", "left", 10, 20),
        ep.make_click_event(480, "press", "left", 10, 20),
        ep.make_click_event(520, "release", "left", 10, 20),
    ]
    elapsed, _, mouse = play(double)
    check("双击一次触发 4 次按钮动作",
          mouse.actions == [("press", "left"), ("release", "left"),
                            ("press", "left"), ("release", "left")],
          str(mouse.actions))
    check("双击两次按下间隔远小于系统阈值", elapsed < 300,
          f"耗时 {elapsed:.0f}ms（阈值 {ep.MULTI_CLICK_MAX_GAP_MS}ms）")

    # N 连击一次性发完：这是「放宽上限」的核心价值
    for n in (4, 8):
        chain = []
        for i in range(n):
            chain += [
                ep.make_click_event(i * 120, "press", "left", 10, 20),
                ep.make_click_event(i * 120 + 40, "release", "left", 10, 20),
            ]
        elapsed, _, mouse = play(chain)
        presses = [a for a, _ in mouse.actions if a == "press"]
        releases = [a for a, _ in mouse.actions if a == "release"]
        check(f"{n} 连击一次热键发完全部点击",
              len(presses) == n and len(releases) == n,
              f"按下 {len(presses)} / 释放 {len(releases)} 次")
        check(f"{n} 连击总耗时落在连击窗口内",
              elapsed < ep.MULTI_CLICK_MAX_GAP_MS,
              f"耗时 {elapsed:.0f}ms（窗口 {ep.MULTI_CLICK_MAX_GAP_MS}ms）")


def check_step_param_editing() -> None:
    """第五阶段新增：鼠标「动作/位置」分离编辑与键盘改键。"""
    print("\n[19] 步骤参数编辑（动作/位置分离、改键、组合键）")

    def _c(t, a, b="left", x=100, y=200):
        return ep.make_click_event(t, a, b, x, y)

    def _k(t, a, vk):
        return ep.make_key_event(t, a, f"vk:{vk}", vk)

    # 只改位置
    step = ep.build_steps([_c(0, "press"), _c(40, "release")])[0]
    moved = ep.rebuild_step_events(step, x=555, y=666)
    check("只改坐标：动作与节奏不变",
          all(e["x"] == 555 and e["y"] == 666 and e["button"] == "left" for e in moved)
          and [e["t"] for e in moved] == [0, 40],
          str([(e["x"], e["y"], e["t"]) for e in moved]))

    # 只改动作
    right = ep.rebuild_step_events(step, button="right")
    check("只改动作：坐标不变",
          all(e["button"] == "right" and e["x"] == 100 for e in right),
          str([(e["button"], e["x"]) for e in right]))

    # 单击 -> 双击
    doubled = ep.rebuild_step_events(step, click_count=2)
    built = ep.build_steps(doubled)
    check("单击可改成双击",
          len(built) == 1 and built[0].kind == "multi"
          and built[0].description == "鼠标双击 (100, 200)",
          built[0].description if built else "?")

    # 三击 -> 单击
    triple = [_c(0, "press"), _c(40, "release"), _c(120, "press"), _c(160, "release"),
              _c(240, "press"), _c(280, "release")]
    t_step = ep.build_steps(triple)[0]
    single = ep.build_steps(ep.rebuild_step_events(t_step, click_count=1))
    check("三击可改回单击",
          len(single) == 1 and single[0].kind == "click", str(single[0].kind))

    # 键盘改键
    key_step = ep.build_steps([_k(0, "press", 65), _k(40, "release", 65)])[0]
    changed = ep.rebuild_step_events(key_step, key="vk:66", vk=66)
    check("键盘改键后按下/释放配对不变",
          [e["key"] for e in changed] == ["vk:66", "vk:66"]
          and [e["action"] for e in changed] == ["press", "release"],
          str([(e["key"], e["action"]) for e in changed]))

    # 组合键改修饰键 / 目标键
    combo = [_k(0, "press", 17), _k(15, "press", 86),
             _k(60, "release", 86), _k(75, "release", 17)]
    c_step = ep.build_steps(combo)[0]
    check("组合键可改修饰键",
          ep.describe_combo(ep.rebuild_step_events(c_step, modifiers=["Ctrl", "Alt"]))[0]
          == "组合键 Ctrl+Alt+V")
    check("组合键可改目标键",
          ep.describe_combo(ep.rebuild_step_events(c_step, key="vk:67", vk=67))[0]
          == "组合键 Ctrl+C")

    # 修饰键顺序规范化
    check("修饰键顺序统一为 Ctrl→Shift→Alt→Win",
          ep._normalize_modifier_names(["shift", "ctrl"]) == ["Ctrl", "Shift"]
          and ep.all_modifier_names() == ["Ctrl", "Shift", "Alt", "Win"],
          str(ep.all_modifier_names()))

    check("不支持的修饰键被拒绝", _raises(ep.StepEditError,
                                          lambda: ep._normalize_modifier_names(["hyper"])))


def _raises(exc_type, func) -> bool:
    """func 抛出指定异常时返回 True。"""
    try:
        func()
    except exc_type:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def check_hotkey_recording_ignore() -> None:
    """第五阶段新增：按 F9 停止录制时，F9 本身不能进脚本。"""
    print("\n[20] 停止录制热键 F9 不被录制")
    from core.player import MacroPlayer
    from core.recorder import DEFAULT_IGNORED_KEYS, Recorder

    print(f"    默认忽略名单：{sorted(DEFAULT_IGNORED_KEYS)}")
    check("默认忽略名单包含 F9",
          "f9" in DEFAULT_IGNORED_KEYS and "vk:120" in DEFAULT_IGNORED_KEYS)

    player = MacroPlayer()
    recorded: list[dict] = []
    rec = Recorder(is_executing=lambda: player.is_executing, on_event=recorded.append)
    rec._recording = True
    rec._t0 = time.monotonic()

    from pynput import keyboard as kb

    rec._handle_press(kb.Key.f9)
    rec._handle_release(kb.Key.f9)
    check("F9 按下/释放都不进事件列表", recorded == [], str(recorded))

    rec._handle_press(kb.KeyCode.from_char("a"))
    rec._handle_release(kb.KeyCode.from_char("a"))
    check("其它按键照常录制", len(recorded) == 2, f"{len(recorded)} 条")

    # 旧脚本里以 vk 形式存在的 F9 也要能被清洗掉
    dirty = [ep.make_key_event(0, "press", "vk:65", 65),
             ep.make_key_event(10, "release", "vk:65", 65),
             ep.make_key_event(20, "press", "vk:120", 120),
             ep.make_key_event(30, "release", "vk:120", 120)]
    cleaned = ep.sanitize_events(dirty, ignore_keys=DEFAULT_IGNORED_KEYS)
    check("清洗阶段也能剔除 F9",
          all(e.get("vk") != 120 and e.get("key") != "f9" for e in cleaned)
          and len(cleaned) == 2, f"{len(cleaned)} 条")


def check_loop_playback() -> None:
    """第六阶段新增：模式一循环回放。"""
    print("\n[21] 循环回放（模式一）")
    from core.player import (LOOP_FIXED, LOOP_INFINITE, LOOP_NONE, MacroPlayer,
                             normalize_loop_count, normalize_loop_mode)

    class _Spy:
        def __init__(self, kind: str = "mouse") -> None:
            self.kind = kind
            self._pos = (0, 0)
            self.log: list[tuple] = []

        @property
        def position(self):
            return self._pos

        @position.setter
        def position(self, value):
            self._pos = tuple(value)
            self.log.append(("move", self._pos))

        def press(self, key):
            self.log.append(("press", getattr(key, "name", str(key))))

        def release(self, key):
            self.log.append(("release", getattr(key, "name", str(key))))

        def scroll(self, dx, dy):
            self.log.append(("scroll", dx, dy))

        def release_all(self):
            self.log.append(("release_all",))

    events = [ep.make_move_event(0, 10, 10),
              ep.make_move_event(60, 20, 20),
              ep.make_move_event(120, 30, 30)]

    def run(mode: str, count: int, timeout: float = 8.0):
        player = MacroPlayer()
        mouse = _Spy()
        player.set_controllers(mouse_ctl=mouse, keyboard_ctl=_Spy("kbd"))
        player.load(events, "loop")
        player.set_loop(mode, count)
        player.start_timed()
        deadline = time.monotonic() + timeout
        while player.is_replaying and time.monotonic() < deadline:
            time.sleep(0.005)
        moves = [e[1] for e in mouse.log if e[0] == "move"]
        return (not player.is_replaying), moves, player.loop_rounds_done

    check("循环模式归一化",
          normalize_loop_mode("fixed") == LOOP_FIXED
          and normalize_loop_mode("x") == LOOP_NONE
          and normalize_loop_count(0) >= 1,
          f"fixed={normalize_loop_mode('fixed')} 非法={normalize_loop_mode('x')}")

    ok, moves, done = run(LOOP_NONE, 1)
    check("不循环：跑一遍就结束",
          ok and moves == [(10, 10), (20, 20), (30, 30)] and done == 1,
          f"移动 {len(moves)} 次 / {done} 轮")

    ok, moves, done = run(LOOP_FIXED, 3)
    check("固定 3 轮：重复执行 3 遍",
          ok and moves == [(10, 10), (20, 20), (30, 30)] * 3 and done == 3,
          f"移动 {len(moves)} 次 / {done} 轮")

    ok, _moves, done = run(LOOP_FIXED, 1)
    check("固定 1 轮等同不循环", ok and done == 1, f"{done} 轮")

    # 无限循环 + 手动停止
    player = MacroPlayer()
    mouse = _Spy()
    player.set_controllers(mouse_ctl=mouse, keyboard_ctl=_Spy("kbd"))
    player.load(events, "loop")
    player.set_loop(LOOP_INFINITE)
    player.start_timed()
    time.sleep(0.75)
    player.stop()
    deadline = time.monotonic() + 3
    while player.is_replaying and time.monotonic() < deadline:
        time.sleep(0.005)
    done = player.loop_rounds_done
    check("无限循环：一直跑到手动停止", not player.is_replaying and done >= 2,
          f"停止后已完成 {done} 轮")

    fixed_text = _loop_text(LOOP_FIXED, 5)
    check("轮次文案：固定轮数显示「第 x / N 轮」",
          "第 1 / 5 轮" in fixed_text, fixed_text)
    infinite_text = _loop_text(LOOP_INFINITE, 1)
    check("轮次文案：无限循环显示「无限循环 · 第 x 轮」",
          "无限循环" in infinite_text, infinite_text)

    probe = MacroPlayer()
    probe.set_loop(LOOP_FIXED, 4)
    probe.reset()
    check("重置指针不清空循环设置",
          probe.loop_mode == LOOP_FIXED and probe.loop_count == 4,
          f"{probe.loop_mode} / {probe.loop_count}")


def _loop_text(mode: str, count: int) -> str:
    """取某循环模式下的轮次文案。"""
    from core.player import MacroPlayer

    player = MacroPlayer()
    player.set_loop(mode, count)
    return player.loop_progress_text()


def check_loop_config() -> None:
    """第六阶段新增：循环设置持久化。"""
    print("\n[22] 循环设置持久化")
    from core.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg = Config(path)
        check("默认不循环", cfg.loop_mode == "none" and cfg.loop_count == 1,
              f"{cfg.loop_mode} / {cfg.loop_count}")

        cfg.set_loop("fixed", 7)
        cfg.save()
        reloaded = Config(path)
        check("固定轮数写入并读回",
              reloaded.loop_mode == "fixed" and reloaded.loop_count == 7,
              f"{reloaded.loop_mode} / {reloaded.loop_count}")

        cfg.set_loop("infinite")
        cfg.save()
        check("无限循环写入并读回",
              Config(path).loop_mode == "infinite", Config(path).loop_mode)

        path.write_text('{"loop": {"mode": "nonsense", "count": -9}}',
                        encoding="utf-8")
        cleaned = Config(path)
        check("非法循环配置被清洗",
              cleaned.loop_mode == "none" and cleaned.loop_count == 1,
              f"{cleaned.loop_mode} / {cleaned.loop_count}")


def check_dialog_styles() -> None:
    """第六阶段新增：所有步骤编辑弹窗都套用主界面 QSS。"""
    print("\n[23] 弹窗样式统一")
    if not QT_AVAILABLE:
        check("Qt 可用（离屏）", False, "未安装 PySide6")
        return

    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QPushButton

    app = QApplication.instance() or QApplication([])
    from ui import styles
    from ui.name_dialog import NameInputDialog
    from ui.step_dialog import InsertStepDialog
    from ui.step_editor import (ComboStepDialog, KeyStepDialog, MouseStepDialog,
                                MoveStepDialog)
    from ui.time_dialog import DurationDialog, StepTimeDialog

    click = ep.build_steps([ep.make_click_event(0, "press", "left", 1, 2),
                            ep.make_click_event(40, "release", "left", 1, 2)])[0]
    key = ep.build_steps([ep.make_key_event(0, "press", "vk:65", 65),
                          ep.make_key_event(40, "release", "vk:65", 65)])[0]
    combo = ep.build_steps([ep.make_key_event(0, "press", "vk:17", 17),
                            ep.make_key_event(10, "press", "vk:86", 86),
                            ep.make_key_event(50, "release", "vk:86", 86),
                            ep.make_key_event(60, "release", "vk:17", 17)])[0]
    move = ep.build_steps([ep.make_move_event(0, 1, 1)])[0]
    scroll = ep.build_steps([ep.make_scroll_event(0, 0, -1, 1, 1)])[0]

    dialogs = [
        ("鼠标编辑", MouseStepDialog(click)),
        ("移动编辑", MoveStepDialog(move)),
        ("滚轮编辑", MoveStepDialog(scroll)),
        ("键盘编辑", KeyStepDialog(key)),
        ("组合键编辑", ComboStepDialog(combo)),
        ("插入步骤", InsertStepDialog()),
        ("单步时间", StepTimeDialog("左键单击", 1, 5, 1000, 1040, 0)),
        ("总时长", DurationDialog(30000, 5)),
        ("脚本命名", NameInputDialog("新建脚本", "输入名称：")),
    ]
    no_style = [name for name, d in dialogs if not d.styleSheet()]
    check("全部弹窗自带全局样式表", not no_style, f"缺失：{no_style}")

    bad: list[str] = []
    for name, dialog in dialogs:
        names = {b.objectName() for b in dialog.findChildren(QPushButton)}
        if "PrimaryButton" not in names or "GhostButton" not in names:
            bad.append(name)
    check("确认/取消按钮使用设计样式（非原生灰按钮）", not bad, f"异常：{bad}")

    stripped = [name for name, dialog in dialogs
                if any(not b.objectName()
                       for b in dialog.findChildren(QPushButton))]
    check("弹窗里没有未设样式的裸按钮", not stripped, f"异常：{stripped}")

    qss = styles.build_stylesheet()
    missing = [sel for sel in ("QDialog", "QComboBox", "QSpinBox", "QLineEdit",
                               "QDialogButtonBox", "DialogHeading",
                               "DialogPreview", "DialogWarn")
               if sel not in qss]
    check("样式表覆盖弹窗控件", not missing, f"缺少：{missing}")

    for _name, dialog in dialogs:
        dialog.deleteLater()
    app.processEvents()


def check_window_resize() -> None:
    """第六阶段新增：窗口可自由缩放（边缘热区归属外壳）。"""
    print("\n[24] 窗口可缩放")
    if not QT_AVAILABLE:
        check("Qt 可用（离屏）", False, "未安装 PySide6")
        return

    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from core.config import Config
    from ui.main_window import MainWindow

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(Path(tmp) / "config.json")
        cfg.set("scripts_dir", str(Path(tmp) / "scripts"))
        cfg.save()
        window = MainWindow(cfg)
        window.show()
        for _ in range(60):
            app.processEvents()

        check("没有固定尺寸限制",
              window.minimumSize() != window.maximumSize()
              and window.maximumWidth() > 10000
              and not (window.windowFlags() & Qt.MSWindowsFixedSizeDialogHint),
              f"min={window.minimumSize().toTuple()} "
              f"max={window.maximumSize().toTuple()}")
        check("默认尺寸 1000x700",
              (window.DEFAULT_WIDTH, window.DEFAULT_HEIGHT) == (1000, 700),
              f"{window.DEFAULT_WIDTH}x{window.DEFAULT_HEIGHT}")

        check("原生窗口边框（缩放由系统提供）",
              not (window.windowFlags() & Qt.FramelessWindowHint)
              and window.mask().isEmpty(),
              f"flags={int(window.windowFlags())}，边缘拖拽/双箭头光标/"
              "双击最大化/Win+方向键分屏均交给 Windows")
        check("无自定义缩放残留",
              not hasattr(window, "_RESIZE_MARGIN")
              and not hasattr(window, "_resizing"),
              "无边框热区与手动缩放逻辑已清理")

        window.resize(1180, 760)
        window.move(140, 110)
        for _ in range(40):
            app.processEvents()
        window._save_window_geometry()
        saved = Config(cfg.path)
        check("尺寸与位置写入配置",
              (saved.get("window.width"), saved.get("window.height")) == (1180, 760)
              and (saved.get("window.x"), saved.get("window.y")) == (140, 110),
              f"{saved.get('window.width')}x{saved.get('window.height')} "
              f"@({saved.get('window.x')},{saved.get('window.y')})")
        window.close()
        for _ in range(30):
            app.processEvents()


def check_os_hooks() -> None:
    """真实安装一次系统钩子，验证“录制链路能跑通、且忽略自身注入”。

    这一步会真的启动 pynput 的全局钩子（并立刻停止），
    但**不会**真的移动鼠标或按键，因此可以安全地自动运行。
    """
    print("\n[25] 系统钩子与忽略自身事件（真实监听器）")
    from core.player import MacroPlayer
    from core.recorder import Recorder

    player = MacroPlayer()
    raw: list[dict] = []
    errors: list[str] = []
    recorder = Recorder(
        is_executing=lambda: player.is_executing,
        on_event=raw.append,
        on_error=errors.append,
    )

    if not recorder.start(min_interval_ms=50, min_distance_px=2):
        check("录制监听器启动", False, "监听器无法启动（可能被安全软件拦截）")
        return
    check("录制监听器启动", recorder.is_recording)

    time.sleep(0.15)

    # ---- 1) 真实鼠标移动 + 键盘按键：应被录到 ----
    from pynput import keyboard as kb
    from pynput import mouse as ms

    mouse_ctl = ms.Controller()
    key_ctl = kb.Controller()
    start_pos = mouse_ctl.position
    mouse_ctl.position = (int(start_pos[0]) + 60, int(start_pos[1]) + 40)
    time.sleep(0.05)
    key_ctl.press("z")
    time.sleep(0.05)
    key_ctl.release("z")
    time.sleep(0.2)

    seen_moves = [e for e in raw if e["type"] == "mouse_move"]
    seen_keys = [e for e in raw if e["type"] == "key"]
    check("真实鼠标移动被录制", len(seen_moves) >= 1, f"{len(seen_moves)} 条移动事件")
    check("真实键盘输入被录制", len(seen_keys) >= 2,
          f"{len(seen_keys)} 条键盘事件：{[ep.describe(e) for e in seen_keys[:2]]}")

    # ---- 2) 回放注入的点击：必须被忽略，不能被录进来 ----
    before_clicks = len([e for e in raw if e["type"] == "mouse_click"])
    player.set_controllers(mouse_ctl=mouse_ctl, keyboard_ctl=key_ctl)
    player.load([ep.make_click_event(0, "press", "left", 200, 200),
                 ep.make_click_event(20, "release", "left", 200, 200)], "ignore-check")
    player.step_next()
    player.step_next()
    time.sleep(0.2)
    after_clicks = len([e for e in raw if e["type"] == "mouse_click"])
    check("回放注入的点击没有被再次录制", after_clicks == before_clicks,
          f"注入前 {before_clicks} 条，注入后 {after_clicks} 条")

    # ---- 3) 抖动过滤：一次性小位移应被丢弃 ----
    jitter_before = len(raw)
    recorder._handle_move(2000, 2000)
    recorder._handle_move(2001, 2000)   # 间隔 < 50ms 且位移 < 2px -> 丢弃
    jitter_after = len(raw)
    check("抖动过滤丢弃微小移动", jitter_after - jitter_before == 1,
          f"两次调用只记录 {jitter_after - jitter_before} 条")

    events = recorder.stop()
    check("停止录制后监听器已关闭", not recorder.is_recording)
    check("录制过程无错误", not errors, f"errors={errors}")
    key_after_stop = [e for e in events if e["type"] == "key"]
    check("停止录制后返回清洗过的事件序列", len(key_after_stop) >= 2,
          f"{len(events)} 条事件 / {len(key_after_stop)} 条键盘事件")

    # 收尾清理：把鼠标放回原位，避免影响用户
    try:
        mouse_ctl.position = start_pos
    except Exception:  # pragma: no cover
        pass


def main() -> int:
    setup_console_logging()
    print("=" * 66)
    print("MacroSmith 宏匠 —— 核心功能自检（命令行）")
    print("=" * 66)

    check_environment()
    check_config()
    check_parser()
    check_player_timed()
    check_player_step()
    check_execution_flag()
    check_hotkeys()
    check_window_geometry()
    check_step_editing()
    check_always_records_moves()
    check_step_timing()
    check_coordinate_clamp()
    check_app_icon()
    check_no_spurious_clicks()
    check_click_only_cursor()
    check_hotkey_exclusive()
    check_smart_merge()
    check_merged_playback()
    check_step_param_editing()
    check_hotkey_recording_ignore()
    check_loop_playback()
    check_loop_config()
    check_dialog_styles()
    check_window_resize()
    check_os_hooks()

    total = len(_results)
    failed = [r for r in _results if not r[1]]
    print("\n" + "=" * 66)
    print(f"自检结果：{total - len(failed)}/{total} 项通过")
    if failed:
        print("未通过项：")
        for title, _ok, detail in failed:
            print(f"  × {title} {detail}")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
