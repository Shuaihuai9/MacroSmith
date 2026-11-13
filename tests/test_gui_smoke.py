"""GUI 冒烟测试（离屏渲染，不需要真实显示器）。

使用 Qt 的 ``offscreen`` 平台运行，覆盖：

* 现代极简浅色卡片风格（配色、侧边栏宽度、圆角遮罩）
* 录制流程（始终记录移动与点击）与步骤列表刷新
* 模式一原速回放、模式二单步热键
* **步骤编辑器**：勾选、批量删除、上移 / 下移、插入、保存回写
* **脚本库**：列表、预览、加载、重命名、删除、新建
* 热键保存校验、窗口几何持久化

运行::

    python -m pytest tests/test_gui_smoke.py -v
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# 离屏平台必须在导入 QtWidgets 之前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QElapsedTimer, QPoint, QTimer, Qt    # noqa: E402
from PySide6.QtTest import QTest                                  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from core import event_parser as ep                              # noqa: E402
from core import quick_input as qi                               # noqa: E402
from core.config import DEFAULT_HOTKEYS, Config                   # noqa: E402
from core.hotkey_hook import hook_supported                       # noqa: E402
from core.player import MODE_STEP, MODE_TIMED                    # noqa: E402
from ui import styles                                            # noqa: E402
from ui.main_window import (PAGE_ABOUT, PAGE_LIBRARY, PAGE_RECORD,   # noqa: E402
                            PAGE_SCRIPTS, PAGE_SETTINGS, MainWindow, State)
from utils.win_utils import WINDOW_TITLE                         # noqa: E402


# ===========================================================================
# 夹具与辅助函数
# ===========================================================================
@pytest.fixture(scope="session")
def qapp():
    """整个会话共用一个 QApplication。"""
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    """每个用例一份独立配置，避免污染项目根目录的 config.json。

    同时把回放模式与热键显式重置为默认值，避免上一个用例写入的值
    （例如把 F9 改成 F10）影响后续用例。
    """
    cfg = Config(tmp_path / "config.json")
    for action, token in DEFAULT_HOTKEYS.items():
        cfg.set_hotkey(action, token)
    cfg.set("scripts_dir", str(tmp_path / "scripts"))
    cfg.set("player.default_mode", MODE_TIMED)
    cfg.save()
    return cfg


@pytest.fixture()
def window(qapp, config: Config, monkeypatch):
    """创建主窗口；弹窗一律打桩，避免离屏环境下阻塞测试。"""
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: None))
    # 中文确认框统一走 ask_question，这里打桩默认「确定」，避免离屏环境阻塞
    monkeypatch.setattr("ui.main_window.ask_question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))

    win = MainWindow(config)
    win.show()
    _pump(qapp, 60)
    yield win
    win.close()
    _pump(qapp, 30)


def _pump(app: QApplication, ms: int = 120) -> None:
    """跑一段事件循环，让定时器与排队信号都得到处理。"""
    timer = QElapsedTimer()
    timer.start()
    while timer.elapsed() < ms:
        app.processEvents()


def _wait_until(app: QApplication, predicate, timeout_ms: int = 3000) -> bool:
    """等待条件成立（同时继续处理 Qt 事件）。"""
    timer = QElapsedTimer()
    timer.start()
    while timer.elapsed() < timeout_ms:
        app.processEvents()
        if predicate():
            return True
        QTimer.singleShot(5, lambda: None)
        app.processEvents()
    return predicate()


def _sample_events() -> list[dict]:
    """一段短小的事件序列：8 条事件 / 5 个操作单元。"""
    return [
        ep.make_move_event(0, 100, 100),
        ep.make_move_event(10, 102, 101),
        ep.make_move_event(60, 200, 200),
        ep.make_click_event(100, "press", "left", 200, 200),
        ep.make_click_event(140, "release", "left", 200, 200),
        ep.make_scroll_event(200, 0, -1, 200, 200),
        ep.make_key_event(260, "press", "vk:65", 65),
        ep.make_key_event(300, "release", "vk:65", 65),
    ]


def _long_events(count: int = 12, step_ms: int = 200) -> list[dict]:
    """较长的时间轴，用于观察回放过程中的状态与高亮。"""
    return [ep.make_move_event(i * step_ms, i, i) for i in range(count)]


def _install_null_controllers(window: MainWindow) -> None:
    """把回放器的控制器换成空实现，避免测试真的去操作鼠标键盘。"""
    window.player.set_controllers(mouse_ctl=_NullMouse(), keyboard_ctl=_NullKeyboard())


def _load_into_window(window: MainWindow, events: list[dict]) -> None:
    """把事件交给主窗口（走公开的 apply_steps，等价于载入脚本后的状态）。"""
    window.apply_steps(events, mark_unsaved=False)


# ===========================================================================
# 窗口外观（现代极简浅色卡片风格）
# ===========================================================================
class TestWindowAppearance:
    def test_window_title(self, window):
        assert window.windowTitle().startswith(WINDOW_TITLE)
        assert WINDOW_TITLE == "宏匠"

    def test_native_window_frame(self, window):
        """第七阶段起回归原生窗口：系统标题栏 + 系统边框。

        无边框方案已被用户否决（startSystemResize 被 Windows 拒绝，
        窗口拖不动），这里断言没有任何无边框残留。
        """
        assert not (window.windowFlags() & Qt.FramelessWindowHint)
        assert window.mask().isEmpty(), "原生窗口不应再设置圆角遮罩"
        assert not window.testAttribute(Qt.WA_TranslucentBackground)

    def test_default_size(self, window, qapp):
        _pump(qapp, 120)   # 等 QTimer.singleShot(0) 里的目标尺寸生效
        # 第五阶段：默认初始尺寸 1000x700（用户拖拽调整后会写入 config.json）
        assert (window.width(), window.height()) == (
            window.DEFAULT_WIDTH, window.DEFAULT_HEIGHT)
        assert (window.DEFAULT_WIDTH, window.DEFAULT_HEIGHT) == (1000, 700)

    def test_resizable_and_minimum_size(self, window, qapp):
        """必须允许拖拽调整大小，且有合理下限。"""
        assert window.minimumWidth() >= 800
        assert window.minimumHeight() >= 540
        assert window.maximumWidth() > 10000     # 没有被固定住

    def test_resize_then_restore_from_config(self, qapp, tmp_path):
        """拖拽调整后的尺寸/位置写入配置，重启后能恢复。"""
        cfg = Config(tmp_path / "cfg.json")
        cfg.set("scripts_dir", str(tmp_path / "scripts"))
        cfg.save()

        first = MainWindow(cfg)
        first.show()
        _pump(qapp, 80)
        first.resize(1234, 812)
        first.move(120, 90)
        _pump(qapp, 60)
        first._save_window_geometry()

        reloaded = Config(cfg.path)
        assert reloaded.get("window.width") == 1234
        assert reloaded.get("window.height") == 812
        assert reloaded.get("window.x") == 120
        assert reloaded.get("window.y") == 90
        first.close()
        _pump(qapp, 40)

        second = MainWindow(reloaded)
        second.show()
        _pump(qapp, 120)
        assert (second.width(), second.height()) == (1234, 812)
        assert (second.x(), second.y()) == (120, 90)
        second.close()
        _pump(qapp, 40)

    def test_sidebar_width_and_buttons(self, window):
        from ui.sidebar import SIDEBAR_WIDTH

        assert SIDEBAR_WIDTH == 64
        assert window.sidebar.width() == 64
        # 导航项：录制与步骤编辑 / 脚本库 / 设置 / 关于（「回放」入口已按需求移除）
        assert all(btn is not None for btn in (
            window.sidebar.btn_record, window.sidebar.btn_library,
            window.sidebar.btn_settings, window.sidebar.btn_about))
        assert not hasattr(window.sidebar, "btn_play"), "「回放」导航项应已删除"

    def test_modern_palette_in_stylesheet(self, window):
        """新的浅色极简配色必须出现在样式表里，且不再使用老版微信绿。"""
        qss = window.styleSheet().lower()
        for color in (styles.ACCENT, styles.ROOT_BG, styles.CARD, styles.SIDEBAR_BG,
                      styles.TEXT, styles.TEXT_SECONDARY, styles.BORDER):
            assert color.lower() in qss, f"样式表缺少配色 {color}"
        assert styles.ACCENT == "#4A6CF7"
        assert styles.ROOT_BG in ("#F7F7F8", "#F9F9F9")
        assert styles.CARD == "#FFFFFF"
        assert styles.TEXT == "#1A1A1A"
        assert "#07c160" not in qss, "老版微信绿不应再出现"
        assert "border-radius: 6px" in qss, "按钮应为 6px 圆角"

    def test_recording_pulse_indicator(self, window):
        window.sidebar.set_recording(True)
        assert window.sidebar.btn_record._anim.state() != 0, "录制时应启动脉冲动画"
        window.sidebar.set_recording(False)
        assert window.sidebar.btn_record._pulse == 0.0

    def test_page_switching(self, window):
        window.sidebar.library_clicked.emit()
        assert window.pages.currentIndex() == PAGE_LIBRARY
        window.sidebar.settings_clicked.emit()
        assert window.pages.currentIndex() == PAGE_SETTINGS
        window.sidebar.about_clicked.emit()
        assert window.pages.currentIndex() == PAGE_ABOUT
        window.sidebar.record_clicked.emit()
        assert window.pages.currentIndex() == PAGE_RECORD


# ===========================================================================
# 录制 → 步骤列表 → 保存
# ===========================================================================
class TestRecordingFlow:
    def test_start_stop_recording_updates_list(self, window, qapp, monkeypatch):
        """走 UI 的 start_recording → 信号回调 → stop_recording 全流程。

        这里把真实监听器的 start/stop 换成可控实现：
        测试环境里可能真的录到宿主机的鼠标移动，导致断言不稳定。
        """
        monkeypatch.setattr(window.recorder, "start", lambda **kwargs: True)
        monkeypatch.setattr(window.recorder, "stop", lambda: [])

        window.start_recording()
        assert window.state is State.RECORDING
        assert window.btn_stop_record.isEnabled()
        assert window.btn_start_record.isEnabled() is False
        assert window.sidebar.btn_record._anim.state() != 0   # 红色脉冲动画已启动
        assert window.event_list.table.rowCount() == 0        # 开始录制时清空旧列表

        # 模拟监听线程回调 → 通过 Qt 信号排队到主线程
        for event in _sample_events():
            window.sig_recorded.emit(event)
        _pump(qapp, 250)   # 等 120ms 批量刷新定时器跑一轮

        expected_steps = len(ep.build_steps(_sample_events()))
        assert len(window.event_list.events) == 8
        assert window.event_list.table.rowCount() == expected_steps

        window.stop_recording()
        _pump(qapp, 150)
        assert window.state is State.IDLE
        assert window.stop_button_disabled()
        assert window.btn_start_play.isEnabled()
        assert window.sidebar.btn_record._pulse == 0.0
        assert len(window.event_list.events) == 8
        assert len(window.player.steps) == expected_steps
        assert len(window._script_steps) == expected_steps

    def test_recording_tick_shows_elapsed(self, window, qapp, monkeypatch):
        monkeypatch.setattr(window.recorder, "start", lambda **kwargs: True)
        monkeypatch.setattr(window.recorder, "stop", lambda: [])
        window.start_recording()
        window._on_record_tick()
        assert "正在录制" in window.message_label.text()
        window.stop_recording()
        _pump(qapp, 80)

    def test_event_rows_show_type_and_description(self, window, qapp):
        events = _sample_events()
        steps = ep.build_steps(events)
        window.event_list.set_steps(steps)
        _pump(qapp, 60)
        table = window.event_list.table
        # 列顺序：复选框 / 序号 / 类型 / 内容 / 时间
        assert table.item(0, 1).text() == "1"
        assert table.item(0, 2).text() == "鼠标移动"
        assert table.item(0, 3).text().startswith("移动到 (200, 200)")
        assert table.item(1, 2).text() == "鼠标点击"
        assert table.item(1, 3).text().startswith("左键单击")
        # 步骤顺序：移动 / 点击 / 滚轮 / 键盘按下 / 键盘释放
        assert table.item(2, 2).text() == "鼠标滚轮"
        assert table.item(3, 2).text() == "键盘按键"
        assert table.item(0, 4).text().endswith("s")

    def test_step_list_merges_moves(self, window, qapp):
        events = _sample_events()
        steps = ep.build_steps(events)
        window.event_list.set_steps(steps)
        _pump(qapp, 60)
        table = window.event_list.table
        assert table.rowCount() == len(steps)
        assert "合并" in table.item(0, 3).text()   # 连续移动被合并，描述里注明帧数
        assert window.event_list.table.columnWidth(4) >= 120

    def test_jitter_filter_applied_from_config(self, window, qapp):
        """开始录制时会把配置里的抖动阈值下发给录制器。"""
        window._save_options({"move_min_interval_ms": 80,
                              "move_min_distance_px": 5, "speed": 1.0})
        _pump(qapp, 60)
        window.start_recording()
        assert window.recorder.move_min_interval_ms == 80
        assert window.recorder.move_min_distance_px == 5
        window.stop_recording()
        _pump(qapp, 80)

    def test_record_option_checkboxes_removed(self, window, qapp, monkeypatch):
        """第九阶段：「仅录制点击坐标」「记录鼠标移动」开关已删除，
        录制始终记录移动 + 点击（不再向录制器传 clicks_only/mouse_move）。"""
        assert not hasattr(window, "chk_clicks_only")
        assert not hasattr(window, "chk_record_move")
        assert not hasattr(window.page_settings, "chk_clicks_only")
        assert not hasattr(window.page_settings, "chk_mouse_move")
        # 开始录制时不再传已删除的参数
        captured: dict = {}
        monkeypatch.setattr(window.recorder, "start",
                            lambda **kwargs: captured.update(kwargs) or True)
        monkeypatch.setattr(window.recorder, "stop", lambda: [])
        window.start_recording()
        assert "clicks_only" not in captured
        assert "mouse_move" not in captured
        assert captured.get("min_interval_ms") is not None
        window.stop_recording()
        _pump(qapp, 80)

    def test_save_and_reload_script(self, window, qapp):
        events = _sample_events()
        _load_into_window(window, events)
        window.set_script_name("冒烟脚本")
        path = window.save_script()
        assert path is not None and path.exists()
        assert path.parent == window.config.scripts_path()
        assert window._current_script == path

        window.clear_events()
        _pump(qapp, 60)
        assert window.event_list.events == []

        window.load_script_file(path)
        _pump(qapp, 60)
        assert len(window.event_list.events) == len(events)
        assert window.script_name() == "冒烟脚本"
        assert window.config.get("last_script") == str(path)

    def test_save_without_content_is_noop(self, window):
        assert window.save_script() is None
        assert "没有可保存" in window.message_label.text()

    def test_clear_events_resets_everything(self, window, qapp):
        _load_into_window(window, _sample_events())
        _pump(qapp, 60)
        assert window.event_list.events
        window._unsaved = False
        window.clear_events()
        _pump(qapp, 60)
        assert window.event_list.events == []
        assert window.event_list.table.rowCount() == 0
        assert window.player.step_total == 0
        assert window.btn_start_play.isEnabled() is False


# ===========================================================================
# 录制暂停 / 继续（v1.2.0）
# ===========================================================================
class TestRecordingPause:
    def _start(self, window, monkeypatch):
        monkeypatch.setattr(window.recorder, "start", lambda **kwargs: True)
        monkeypatch.setattr(window.recorder, "stop", lambda: [])
        window.start_recording()
        assert window.state is State.RECORDING

    def test_pause_button_disabled_when_idle(self, window):
        assert window.btn_pause_record.isEnabled() is False
        assert window.btn_pause_record.text() == "暂停录制"

    def test_pause_button_enabled_while_recording(self, window, qapp, monkeypatch):
        self._start(window, monkeypatch)
        assert window.btn_pause_record.isEnabled() is True
        assert window.btn_stop_record.isEnabled() is True
        window.stop_recording()
        _pump(qapp, 80)

    def test_pause_resume_toggles_state_and_text(self, window, qapp, monkeypatch):
        self._start(window, monkeypatch)
        window.toggle_recording_pause()
        assert window.state is State.RECORD_PAUSED
        assert window.btn_pause_record.text() == "继续录制"
        assert window.btn_pause_record.isEnabled() is True
        assert window.btn_stop_record.isEnabled() is True
        assert "暂停" in window.message_label.text()
        window.toggle_recording_pause()
        assert window.state is State.RECORDING
        assert window.btn_pause_record.text() == "暂停录制"
        window.stop_recording()
        _pump(qapp, 80)

    def test_pause_hotkey_toggles(self, window, qapp, monkeypatch):
        self._start(window, monkeypatch)
        window._on_hotkey_action("record_pause")
        assert window.state is State.RECORD_PAUSED
        window._on_hotkey_action("record_pause")
        assert window.state is State.RECORDING
        window.stop_recording()
        _pump(qapp, 80)

    def test_pause_hotkey_ignored_when_idle(self, window, qapp):
        window._on_hotkey_action("record_pause")
        assert window.state is State.IDLE

    def test_stop_while_paused(self, window, qapp, monkeypatch):
        self._start(window, monkeypatch)
        window.toggle_recording_pause()
        assert window.state is State.RECORD_PAUSED
        window.stop_recording()
        _pump(qapp, 120)
        assert window.state is State.IDLE
        assert window.btn_pause_record.isEnabled() is False
        assert window.btn_pause_record.text() == "暂停录制"

    def test_editing_blocked_while_paused(self, window, qapp, monkeypatch):
        self._start(window, monkeypatch)
        window.toggle_recording_pause()
        assert window.state is State.RECORD_PAUSED
        assert window.btn_start_record.isEnabled() is False
        assert window.btn_new_script.isEnabled() is False
        assert window.btn_clear.isEnabled() is False
        window.stop_recording()
        _pump(qapp, 80)

    def test_pulse_stops_while_paused(self, window, qapp, monkeypatch):
        self._start(window, monkeypatch)
        assert window.sidebar.btn_record._anim.state() != 0
        window.toggle_recording_pause()
        assert window.sidebar.btn_record._pulse == 0.0
        window.stop_recording()
        _pump(qapp, 80)


# ===========================================================================
# 模式一：原速回放
# ===========================================================================
class TestTimedReplay:
    def _prepare(self, window, qapp, events=None):
        events = events or _sample_events()
        _load_into_window(window, events)
        _install_null_controllers(window)
        window.mode_combo.setCurrentIndex(0)     # 模式一
        _pump(qapp, 60)
        return events

    def test_replay_finishes_and_updates_status(self, window, qapp):
        self._prepare(window, qapp)
        window.start_replay()
        assert window.state in (State.REPLAYING, State.IDLE)
        assert _wait_until(qapp, lambda: window.state is State.IDLE, 3000)
        assert "回放完成" in window.message_label.text()
        assert window.step_label.text().startswith("步骤")

    def test_replay_highlights_current_event(self, window, qapp):
        self._prepare(window, qapp, _long_events(12, 120))
        window.start_replay()
        _pump(qapp, 250)
        assert window.state is State.REPLAYING
        assert window.event_list.highlighted >= 0, "回放时应高亮当前步骤行"
        window.stop_replay()
        _pump(qapp, 60)
        assert window.event_list.highlighted == -1

    def test_pause_and_resume(self, window, qapp):
        self._prepare(window, qapp, _long_events(12, 200))
        window.start_replay()
        _pump(qapp, 150)
        window._on_hotkey_action("pause_toggle")
        assert window.state is State.PAUSED
        assert window.btn_start_play.text() == "继续回放"
        # 暂停后不再推进（允许请求与执行之间存在一次极短的边界事件）
        _pump(qapp, 80)
        frozen = window.player.position
        _pump(qapp, 250)
        assert window.player.position == frozen
        window._on_hotkey_action("pause_toggle")
        assert window.state is State.REPLAYING
        window._on_hotkey_action("play_stop")
        assert window.state is State.IDLE
        assert window.player.position == 0

    def test_stop_button_disabled_when_idle(self, window):
        assert window.btn_stop_play.isEnabled() is False

    def test_replay_without_content_shows_hint(self, window):
        window.start_replay()
        assert "没有可回放" in window.message_label.text()

    def test_f6_toggles_play_and_pause(self, window, qapp):
        """F6 在空闲时开始回放、在回放中切换暂停/继续。"""
        self._prepare(window, qapp, _long_events(12, 200))
        window._on_hotkey_action("play_toggle")
        assert window.state is State.REPLAYING
        _pump(qapp, 100)
        window._on_hotkey_action("play_toggle")
        assert window.state is State.PAUSED
        window._on_hotkey_action("play_toggle")
        assert window.state is State.REPLAYING
        window._on_hotkey_action("play_stop")
        assert window.state is State.IDLE


# ===========================================================================
# 模式二：单步热键执行
# ===========================================================================
class TestStepMode:
    def _prepare(self, window, qapp):
        """把事件同时交给列表和回放器，并切到模式二。"""
        events = _sample_events()
        _load_into_window(window, events)
        _install_null_controllers(window)
        window.mode_combo.setCurrentIndex(1)     # 模式二
        _pump(qapp, 60)
        return events

    def test_step_count_and_view(self, window, qapp):
        events = self._prepare(window, qapp)
        assert window.player.step_total == len(ep.build_steps(events))
        assert len(window._script_steps) == len(ep.build_steps(events))
        assert window.event_list.table.rowCount() == window.player.step_total

    def test_page_down_steps_forward(self, window, qapp):
        self._prepare(window, qapp)
        total = window.player.step_total
        window._on_hotkey_action("step_next")
        _pump(qapp, 60)
        assert window.player.step_index == 1
        assert window.step_label.text() == f"步骤 1 / {total}"
        assert window.event_list.highlighted == 0

        window._on_hotkey_action("step_next")
        _pump(qapp, 60)
        assert window.player.step_index == 2
        assert window.step_label.text() == f"步骤 2 / {total}"
        assert window.event_list.highlighted == 1

    def test_page_up_steps_backward(self, window, qapp):
        self._prepare(window, qapp)
        for _ in range(3):
            window._on_hotkey_action("step_next")
        _pump(qapp, 60)
        assert window.player.step_index == 3
        assert window.event_list.highlighted == 2
        window._on_hotkey_action("step_prev")
        _pump(qapp, 60)
        # 回退真正退一步：指针 3 → 2，并重新执行回退后所在的第 2 步
        assert window.player.step_index == 2
        assert window.event_list.highlighted == 1
        window._on_hotkey_action("step_prev")
        _pump(qapp, 60)
        assert window.player.step_index == 1
        assert window.event_list.highlighted == 0
        window._on_hotkey_action("step_reset")
        _pump(qapp, 60)
        assert window.player.step_index == 0
        assert "步骤 0 /" in window.step_label.text()

    def test_step_pointer_synced_to_list_highlight(self, window, qapp):
        self._prepare(window, qapp)
        for _ in range(4):
            window._on_hotkey_action("step_next")
        _pump(qapp, 60)
        assert window.player.step_index == 4
        assert window.event_list.highlighted == 3

    def test_step_hotkey_at_start_and_end(self, window, qapp):
        self._prepare(window, qapp)
        window._on_hotkey_action("step_prev")
        assert "已经回到开头" in window.message_label.text()

        # 一直推进到末尾（UI 的 step_next 是槽函数，没有返回值，按指针判断）
        guard = 0
        while window.player.step_index < window.player.step_total and guard < 100:
            window.step_next()
            guard += 1
        _pump(qapp, 60)
        assert window.player.step_index == window.player.step_total
        window._on_hotkey_action("step_next")
        assert "已经到最后一步" in window.message_label.text()

    def test_reset_button_works(self, window, qapp):
        self._prepare(window, qapp)
        window._on_hotkey_action("step_next")
        _pump(qapp, 60)
        window.btn_reset_step.click()
        _pump(qapp, 60)
        assert window.player.step_index == 0
        assert "已重置到开头" in window.message_label.text()

    def test_double_click_row_jumps_to_step(self, window, qapp):
        self._prepare(window, qapp)
        window.event_list.row_activated.emit(3)   # 直接执行第 4 步
        _pump(qapp, 60)
        assert window.player.step_index == 4

    def test_step_next_without_content(self, window):
        window.mode_combo.setCurrentIndex(1)
        window.step_next()
        assert "没有可执行的步骤" in window.message_label.text()


# ===========================================================================
# 步骤编辑器：勾选 / 删除 / 上移 / 下移 / 插入 / 保存
# ===========================================================================
class TestStepEditor:
    def _prepare(self, window, qapp):
        events = _sample_events()
        _load_into_window(window, events)
        _install_null_controllers(window)
        _pump(qapp, 60)
        return events

    def test_checkbox_selection_api(self, window, qapp):
        self._prepare(window, qapp)
        editor = window.event_list
        total = len(window._script_steps)
        assert editor.checked_count == 0

        editor.toggle_row(0)
        editor.toggle_row(2)
        assert editor.checked_indices() == [0, 2]

        editor.select_all()
        assert editor.checked_count == total
        editor.invert_selection()
        assert editor.checked_count == 0
        editor.invert_selection()
        assert editor.checked_count == total
        editor.clear_selection()
        assert editor.checked_count == 0

    def test_checkbox_column_renders_check_state(self, window, qapp):
        """勾选状态要写进复选框列的数据（供自绘代理读取）。"""
        from ui.event_list import ROLE_CHECKED

        self._prepare(window, qapp)
        window.event_list.set_checked([1])
        item = window.event_list.table.item(1, 0)
        assert item is not None and bool(item.data(ROLE_CHECKED)) is True
        item0 = window.event_list.table.item(0, 0)
        assert bool(item0.data(ROLE_CHECKED)) is False

    def test_delete_selected_steps(self, window, qapp):
        self._prepare(window, qapp)
        before = len(window._script_steps)
        window.event_list.set_checked([1, 2])
        window._on_edit_action("delete")
        _pump(qapp, 60)
        assert len(window._script_steps) == before - 2
        assert window.event_list.table.rowCount() == before - 2
        assert "已删除 2 个步骤" in window.message_label.text()
        # 时间戳重排后仍然单调不减
        times = [int(e["t"]) for e in window._script_events]
        assert times == sorted(times)

    def test_delete_without_selection_hints(self, window, qapp):
        self._prepare(window, qapp)
        before = len(window._script_steps)
        window.event_list.clear_selection()
        window._on_edit_action("delete")
        assert len(window._script_steps) == before
        assert "请先勾选" in window.message_label.text()

    def test_move_step_down(self, window, qapp):
        self._prepare(window, qapp)
        first = window._script_steps[0].description
        second = window._script_steps[1].description
        window.event_list.set_checked([0])
        window._on_edit_action("move_down")
        _pump(qapp, 60)
        descriptions = [s.description for s in window._script_steps]
        assert descriptions[0] == second
        assert descriptions[1] == first
        assert "下移" in window.message_label.text()
        # 勾选状态跟随步骤一起移动
        assert window.event_list.checked_indices() == [1]

    def test_move_step_up(self, window, qapp):
        self._prepare(window, qapp)
        first = window._script_steps[0].description
        second = window._script_steps[1].description
        window.event_list.set_checked([1])
        window._on_edit_action("move_up")
        _pump(qapp, 60)
        descriptions = [s.description for s in window._script_steps]
        assert descriptions[0] == second
        assert descriptions[1] == first

    def test_move_at_boundary_shows_hint(self, window, qapp):
        self._prepare(window, qapp)
        window.event_list.set_checked([0])
        window._on_edit_action("move_up")
        assert "已经是第一步" in window.message_label.text()

        last = len(window._script_steps) - 1
        window.event_list.set_checked([last])
        window._on_edit_action("move_down")
        assert "已经是最后一步" in window.message_label.text()

    def test_insert_step_at_position(self, window, qapp):
        """插入步骤：直接走底层接口验证数据流（对话框另有独立用例）。"""
        self._prepare(window, qapp)
        before = len(window._script_steps)
        new_events, description = ep.make_step_events("click", x=321, y=654, button="right")
        events, index = ep.insert_step(window._script_steps, 1, new_events, description)
        window._set_steps(events, ep.build_steps(events), [index])

        assert len(window._script_steps) == before + 1
        assert window._script_steps[1].kind == "click"
        assert "右击" in window._script_steps[1].description
        assert "321" in window._script_steps[1].description
        assert window.event_list.checked_indices() == [1]
        times = [int(e["t"]) for e in window._script_events]
        assert times == sorted(times)

    def test_insert_step_via_window(self, window, qapp, monkeypatch):
        """主窗口的插入入口：用假对话框替换真弹窗，验证整条链路。"""
        self._prepare(window, qapp)
        before = len(window._script_steps)

        class _FakeDialog:
            Accepted = 1

            def __init__(self, *a, **k):
                self._events, self._desc = ep.make_step_events("click", x=11, y=22)

            def exec(self):
                return 1

            def result_step(self):
                return self._events, self._desc

        monkeypatch.setattr("ui.main_window.InsertStepDialog", _FakeDialog)
        window.insert_step_at(0)
        _pump(qapp, 60)
        assert len(window._script_steps) == before + 1
        assert window._script_steps[0].kind == "click"
        assert "已在第 1 步插入" in window.message_label.text()

    def test_step_dialog_builds_click_events(self, window, qapp):
        """插入对话框的点选/输入应能生成正确的事件。"""
        from ui.step_dialog import InsertStepDialog

        dialog = InsertStepDialog(window, position=0)
        dialog.kind_combo.setCurrentIndex(0)               # 鼠标点击
        dialog.spin_x.setValue(50)
        dialog.spin_y.setValue(60)
        dialog.button_combo.setCurrentIndex(1)             # 右键
        dialog.spin_repeat.setValue(1)                     # 单击才是「右击」
        dialog._on_accept()
        events, description = dialog.result_step()
        assert len(events) == 2                            # 1 组按下/释放
        assert all(e["button"] == "right" for e in events)
        assert all(e["x"] == 50 and e["y"] == 60 for e in events)
        assert "右击" in description
        dialog.deleteLater()

    def test_step_dialog_click_repeat_makes_double_click(self, window, qapp):
        """repeat=2 现在产出「鼠标双击」，并且能被 build_steps 识别为 multi。"""
        from ui.step_dialog import InsertStepDialog

        dialog = InsertStepDialog(window, position=0)
        dialog.kind_combo.setCurrentIndex(0)
        dialog.spin_x.setValue(50)
        dialog.spin_y.setValue(60)
        dialog.button_combo.setCurrentIndex(0)             # 左键
        dialog.spin_repeat.setValue(2)
        dialog._on_accept()
        events, description = dialog.result_step()
        assert len(events) == 4
        assert description == "鼠标双击 (50, 60)"
        built = ep.build_steps(events)
        assert len(built) == 1 and built[0].kind == "multi"
        assert built[0].click_count == 2
        dialog.deleteLater()

    def test_step_dialog_keyboard_kind(self, window, qapp):
        from ui.step_dialog import InsertStepDialog

        dialog = InsertStepDialog(window, position=0)
        dialog.kind_combo.setCurrentIndex(1)               # 键盘按键
        dialog._on_accept()
        events, description = dialog.result_step()
        assert len(events) == 2
        assert events[0]["action"] == "press" and events[1]["action"] == "release"
        assert "键盘" in description
        dialog.deleteLater()

    def test_step_dialog_scroll_kind(self, window, qapp):
        from ui.step_dialog import InsertStepDialog

        dialog = InsertStepDialog(window, position=0)
        dialog.kind_combo.setCurrentIndex(3)               # 滚轮滚动
        dialog.spin_amount.setValue(4)
        dialog._on_accept()
        events, description = dialog.result_step()
        assert len(events) == 1
        assert events[0]["type"] == "mouse_scroll"
        assert abs(events[0]["dy"]) == 4
        assert "滚轮" in description
        dialog.deleteLater()

    def test_save_edited_steps_writes_file(self, window, qapp):
        self._prepare(window, qapp)
        window.set_script_name("编辑保存测试")
        path = window.save_script()
        assert path is not None
        original_count = len(ep.load_script(path)["events"])

        window.event_list.set_checked([0])
        window._on_edit_action("delete")
        _pump(qapp, 60)
        assert window._unsaved is True

        saved = window.save_edited_steps()
        assert saved == path
        assert window._unsaved is False
        payload = ep.load_script(path)
        assert len(payload["events"]) < original_count
        assert "已保存修改" in window.message_label.text()

    def test_editing_disabled_while_replaying(self, window, qapp):
        self._prepare(window, qapp)
        window.event_list.set_checked([0])
        window._set_state(State.REPLAYING)
        assert window.event_list.btn_delete.isEnabled() is False
        window._set_state(State.IDLE)
        assert window.event_list.btn_delete.isEnabled() is True

    def test_editing_blocked_while_recording(self, window, qapp, monkeypatch):
        """录制过程中步骤编辑必须被拦住，不能改到正在录的数据。"""
        events = self._prepare(window, qapp)
        before = len(window._script_steps)
        assert before > 0

        # 开始录制会清空当前内容，让 stop 返回原始事件以便录制后仍有内容可查
        monkeypatch.setattr(window.recorder, "start", lambda **kwargs: True)
        monkeypatch.setattr(window.recorder, "stop", lambda: list(events))
        window.start_recording()
        _pump(qapp, 60)

        for event in events:
            window.sig_recorded.emit(event)
        _pump(qapp, 200)
        window.event_list.set_checked([0, 1])
        window._on_edit_action("delete")
        assert "正在录制中" in window.message_label.text()
        # 编辑被拦截：步骤数量没有被删掉
        assert len(window._script_steps) == before

        window.stop_recording()
        _pump(qapp, 80)
        assert len(window._script_steps) == before


# ===========================================================================
# 脚本库
# ===========================================================================
class TestScriptLibrary:
    def _seed(self, window, qapp, names: list[str]) -> list[Path]:
        paths = []
        for name in names:
            paths.append(ep.save_script(window.config.scripts_path() / f"{name}.json",
                                        name, _sample_events()))
        window.refresh_script_library()
        _pump(qapp, 60)
        return paths

    def test_library_lists_scripts(self, window, qapp):
        self._seed(window, qapp, ["脚本A", "脚本B", "脚本C"])
        assert window.page_library.table.rowCount() == 3
        assert "共 3 个脚本" in window.page_library.count_label.text()

    def test_library_preview_shows_events(self, window, qapp):
        self._seed(window, qapp, ["预览测试"])
        window.page_library.select_path(window.config.scripts_path() / "预览测试.json")
        _pump(qapp, 60)
        preview = window.page_library.preview_table
        assert preview.rowCount() == len(_sample_events())
        assert preview.item(0, 1).text() == "鼠标移动"
        assert "预览测试" in window.page_library.preview_title.text()

    def test_load_selected_script_into_main(self, window, qapp):
        """「加载该脚本」不再跳转到录制页，而是留在本页准备回放。"""
        self._seed(window, qapp, ["待加载"])
        path = window.config.scripts_path() / "待加载.json"
        window.page_library.select_path(path)
        _pump(qapp, 60)
        window.load_selected_script()
        _pump(qapp, 60)
        assert window.pages.currentIndex() == PAGE_SCRIPTS      # 停留在「回放与脚本」
        assert window.script_name() == "待加载"
        assert len(window._script_events) == len(_sample_events())
        assert window._current_script == path
        assert window.btn_start_play.isEnabled() is True
        # 页面上的状态同步刷新
        assert "待加载" in window.page_library.current_label.text()

    def test_load_same_script_twice_is_noop(self, window, qapp):
        """重复加载同一个脚本不打扰用户（不弹未保存提示）。"""
        self._seed(window, qapp, ["同一个"])
        path = window.config.scripts_path() / "同一个.json"
        window.page_library.select_path(path)
        window.load_selected_script()
        _pump(qapp, 40)
        window.load_selected_script()
        _pump(qapp, 40)
        assert "已经是当前脚本" in window.message_label.text()

    def test_create_script(self, window, qapp, monkeypatch):
        monkeypatch.setattr("ui.main_window.ask_name",
                            lambda *a, **k: "新建脚本X")
        window.create_script()
        _pump(qapp, 60)
        target = window.config.scripts_path() / "新建脚本X.json"
        assert target.exists()
        assert window.page_library.table.rowCount() == 1
        payload = ep.load_script(target)
        assert payload["events"] == []
        # 新建后自动选中并加载
        assert window._current_script == target

    def test_create_script_cancelled(self, window, qapp, monkeypatch):
        monkeypatch.setattr("ui.main_window.ask_name", lambda *a, **k: None)
        window.create_script()
        _pump(qapp, 40)
        assert window.page_library.table.rowCount() == 0

    def test_rename_script(self, window, qapp, monkeypatch):
        paths = self._seed(window, qapp, ["旧名字"])
        monkeypatch.setattr("ui.main_window.ask_name",
                            lambda *a, **k: "新名字")
        window.page_library.select_path(paths[0])
        window.rename_selected_script()
        _pump(qapp, 60)
        assert not paths[0].exists()
        renamed = window.config.scripts_path() / "新名字.json"
        assert renamed.exists()
        assert ep.load_script(renamed)["name"] == "新名字"
        assert "已重命名" in window.message_label.text()

    def test_delete_script(self, window, qapp, monkeypatch):
        paths = self._seed(window, qapp, ["要删除的"])
        monkeypatch.setattr("ui.main_window.ask_question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        window.page_library.select_path(paths[0])
        window.delete_selected_script()
        _pump(qapp, 60)
        assert not paths[0].exists()
        assert window.page_library.table.rowCount() == 0

    def test_delete_script_cancelled(self, window, qapp, monkeypatch):
        paths = self._seed(window, qapp, ["保留的"])
        monkeypatch.setattr("ui.main_window.ask_question",
                            staticmethod(lambda *a, **k: QMessageBox.No))
        window.page_library.select_path(paths[0])
        window.delete_selected_script()
        assert paths[0].exists()

    def test_library_shows_broken_script_without_crash(self, window, qapp):
        broken = window.config.scripts_path() / "坏文件.json"
        broken.write_text("{ 这不是 JSON", encoding="utf-8")
        window.refresh_script_library()
        _pump(qapp, 60)
        assert window.page_library.table.rowCount() == 1
        # 选中它不应抛异常
        window.page_library.select_path(broken)
        _pump(qapp, 60)
        assert window.page_library.preview_table.rowCount() >= 1


# ===========================================================================
# 热键与设置
# ===========================================================================
class TestHotkeys:
    def test_default_bindings_displayed(self, window):
        assert window.hotkeys.binding_display("record_start") == "F12"
        assert window.hotkeys.binding_display("record_stop") == "F9"
        assert window.hotkeys.binding_display("step_next") == "PageDown"
        # 空闲态的状态栏提示应包含「F12 开始录制」与「F6 开始回放」
        assert "F12" in window.hotkey_label.text()
        assert "F6" in window.hotkey_label.text()

    def test_step_mode_hints(self, window, qapp):
        window.mode_combo.setCurrentIndex(1)
        _pump(qapp, 60)
        text = window.hotkey_label.text()
        assert "PageDown" in text and "PageUp" in text and "Home" in text

    def test_settings_page_loads_config(self, window):
        window._show_page(PAGE_SETTINGS)
        page = window.page_settings
        assert page._edits["play_toggle"].token() == "f6"
        assert page._edits["step_next"].token() == "page_down"
        assert page.edit_scripts.text() == str(window.config.scripts_path())

    def test_save_hotkeys_rebinds_and_persists(self, window, qapp):
        window._show_page(PAGE_SETTINGS)
        window._save_hotkeys({
            "record_stop": "f10", "play_toggle": "f6", "pause_toggle": "f7",
            "play_stop": "f8", "step_next": "page_down", "step_prev": "page_up",
            "step_reset": "home",
        })
        _pump(qapp, 60)
        assert window.hotkeys.binding_display("record_stop") == "F10"
        loaded = Config(window.config.path)
        assert loaded.hotkey("record_stop") == "f10"
        window._show_page(PAGE_RECORD)
        window._refresh_status()
        # 空闲态只列「现在按了有效」的键，停止录制显示在录制态里，
        # 所以这里切到录制态验证改键确实反映到提示上。
        window._set_state(State.RECORDING)
        window._refresh_hotkey_hint()
        assert "F10" in window.hotkey_label.text()
        window._set_state(State.IDLE)
        window._refresh_status()

    def test_duplicate_hotkeys_rejected(self, window, qapp, monkeypatch):
        calls = []
        monkeypatch.setattr(QMessageBox, "warning",
                            staticmethod(lambda *a, **k: calls.append(a)))
        window._show_page(PAGE_SETTINGS)
        window._save_hotkeys({
            "record_stop": "f6", "play_toggle": "f6", "pause_toggle": "f7",
            "play_stop": "f8", "step_next": "page_down", "step_prev": "page_up",
            "step_reset": "home",
        })
        assert calls, "重复热键应弹出提示"
        assert window.hotkeys.binding_display("record_stop") == "F9"   # 未生效

    def test_invalid_hotkey_rejected(self, window, qapp, monkeypatch):
        calls = []
        monkeypatch.setattr(QMessageBox, "warning",
                            staticmethod(lambda *a, **k: calls.append(a)))
        window._show_page(PAGE_SETTINGS)
        window._save_hotkeys({
            "record_stop": "这不是键", "play_toggle": "f6", "pause_toggle": "f7",
            "play_stop": "f8", "step_next": "page_down", "step_prev": "page_up",
            "step_reset": "home",
        })
        assert calls, "无法识别的热键应弹出提示"
        assert window.hotkeys.binding_display("record_stop") == "F9"
        assert Config(window.config.path).hotkey("record_stop") == "f9"

    def test_save_options_persists(self, window, qapp):
        window._save_options({"move_min_interval_ms": 70,
                              "move_min_distance_px": 4, "speed": 2.0})
        _pump(qapp, 60)
        loaded = Config(window.config.path)
        assert loaded.get("player.move_min_interval_ms") == 70
        assert loaded.get("player.move_min_distance_px") == 4
        assert loaded.get("player.speed") == 2.0
        assert window.player.speed == 2.0

    def test_mode_switch_persists(self, window, qapp):
        window.mode_combo.setCurrentIndex(1)
        _pump(qapp, 60)
        assert Config(window.config.path).get("player.default_mode") == MODE_STEP
        window.mode_combo.setCurrentIndex(0)
        _pump(qapp, 60)
        assert Config(window.config.path).get("player.default_mode") == MODE_TIMED

    def test_recording_hotkey_forces_timed_mode(self, window, qapp, monkeypatch):
        """模式二下点“开始录制”会自动切回模式一。"""
        monkeypatch.setattr(window.recorder, "start", lambda **kwargs: True)
        monkeypatch.setattr(window.recorder, "stop", lambda: [])
        window.mode_combo.setCurrentIndex(1)
        _pump(qapp, 60)
        window.start_recording()
        assert window.mode_combo.currentData() == MODE_TIMED
        assert window.state is State.RECORDING
        window.stop_recording()
        _pump(qapp, 80)


# ===========================================================================
# 状态栏
# ===========================================================================
class TestHotkeysAnyMode:
    """第九阶段：任何模式下所有热键都生效，并自动切到对应模式。"""

    @staticmethod
    def _events():
        return [ep.make_move_event(0, 10, 10),
                ep.make_move_event(400, 20, 20),
                ep.make_move_event(800, 30, 30)]

    def _prepare(self, window, qapp):
        _load_into_window(window, self._events())
        _install_null_controllers(window)
        _pump(qapp, 60)

    def test_f6_in_step_mode_switches_to_timed_and_plays(self, window, qapp):
        """模式二下按 F6：自动切到模式一并开始原速回放。"""
        self._prepare(window, qapp)
        window.mode_combo.setCurrentIndex(1)
        _pump(qapp, 60)
        window._on_hotkey_action("play_toggle")
        _pump(qapp, 60)
        assert window.mode_combo.currentData() == MODE_TIMED
        assert window.state is State.REPLAYING
        assert "模式一" in window.mode_label.text()
        window.stop_replay()
        _pump(qapp, 60)

    def test_f7_in_step_mode_starts_timed_replay(self, window, qapp):
        """模式二下按 F7：视为切到模式一并开始回放。"""
        self._prepare(window, qapp)
        window.mode_combo.setCurrentIndex(1)
        _pump(qapp, 60)
        window._on_hotkey_action("pause_toggle")
        _pump(qapp, 60)
        assert window.mode_combo.currentData() == MODE_TIMED
        assert window.state is State.REPLAYING
        window.stop_replay()
        _pump(qapp, 60)

    def test_f8_stops_replay_in_any_mode(self, window, qapp):
        """F8 任何模式下都停止当前回放。"""
        self._prepare(window, qapp)
        window.mode_combo.setCurrentIndex(0)
        window.start_replay()
        _pump(qapp, 120)
        assert window.state is State.REPLAYING
        window._on_hotkey_action("play_stop")
        _pump(qapp, 60)
        assert window.state is State.IDLE
        assert "回放已停止" in window.message_label.text()

    def test_f8_in_step_mode_resets_pointer(self, window, qapp):
        """模式二空闲时按 F8：执行指针归零。"""
        self._prepare(window, qapp)
        window._on_hotkey_action("step_next")
        _pump(qapp, 60)
        assert window.mode_combo.currentData() == MODE_STEP
        assert window.player.step_index == 1
        window._on_hotkey_action("play_stop")
        _pump(qapp, 60)
        assert window.player.step_index == 0
        assert "重置" in window.message_label.text()

    def test_pagedown_during_timed_replay_stops_and_steps(self, window, qapp):
        """模式一回放中按 PageDown：先停止回放，切模式二，执行一步。

        策略：两个执行通道互斥（见 _enter_step_mode 注释）。
        """
        self._prepare(window, qapp)
        window.mode_combo.setCurrentIndex(0)
        window.start_replay()
        _pump(qapp, 120)
        assert window.state is State.REPLAYING
        window._on_hotkey_action("step_next")
        _pump(qapp, 60)
        assert window.state is State.IDLE, "回放应先被停止"
        assert window.mode_combo.currentData() == MODE_STEP
        assert window.player.step_index == 1, "停止后应执行第一步"
        assert "模式二" in window.mode_label.text()

    def test_home_resets_pointer_and_switches_to_step_mode(self, window, qapp):
        """任何模式下按 Home：切模式二并重置步骤指针。"""
        self._prepare(window, qapp)
        window._on_hotkey_action("step_next")
        _pump(qapp, 60)
        assert window.player.step_index == 1
        # 手动切回模式一（模拟用户在模式一界面下按 Home）
        window.mode_combo.setCurrentIndex(0)
        _pump(qapp, 60)
        window._on_hotkey_action("step_reset")
        _pump(qapp, 60)
        assert window.player.step_index == 0
        assert window.mode_combo.currentData() == MODE_STEP

    def test_ui_mode_switch_during_replay_stops_replay(self, window, qapp):
        """回放中通过下拉框切模式：先停止当前回放。"""
        self._prepare(window, qapp)
        window.mode_combo.setCurrentIndex(0)
        window.start_replay()
        _pump(qapp, 120)
        assert window.state is State.REPLAYING
        window.mode_combo.setCurrentIndex(1)
        _pump(qapp, 60)
        assert window.state is State.IDLE
        assert "模式二" in window.mode_label.text()


class TestStatusBar:
    def test_status_texts(self, window):
        assert "模式一" in window.mode_label.text()
        assert window.step_label.text().startswith("步骤 0 /")
        assert window.hotkey_label.text().startswith("热键：")

    def test_mode_label_follows_combo(self, window, qapp):
        window.mode_combo.setCurrentIndex(1)
        _pump(qapp, 60)
        assert "模式二" in window.mode_label.text()

    def test_event_count_label(self, window, qapp):
        events = _sample_events()
        _load_into_window(window, events)
        window._refresh_status()
        text = window.event_count_label.text()
        assert "8 条事件" in text
        # 合并后：移动 / 点击 / 滚轮 / 键盘按下并释放 = 4 步
        assert "4 步" in text


# ===========================================================================
# 第三阶段：步骤时间编辑 / 总时长重设 / 状态栏同步 / 图标
# ===========================================================================
class TestStepTimeEditingGUI:
    def _prepare(self, window, qapp):
        events = [
            ep.make_move_event(0, 1, 1),
            ep.make_scroll_event(1000, 0, -1, 1, 1),
            ep.make_key_event(2000, "press", "vk:65", 65),
            ep.make_key_event(2040, "release", "vk:65", 65),
        ]
        _load_into_window(window, events)
        _install_null_controllers(window)
        _pump(qapp, 60)
        return events

    def test_edit_step_time_via_dialog(self, window, qapp, monkeypatch):
        """双击时间列 -> 弹窗 -> 修改后时间轴同步更新。"""
        self._prepare(window, qapp)
        monkeypatch.setattr("ui.main_window.ask_step_time",
                            staticmethod(lambda *a, **k: (5000, 0)))
        window.edit_step_time(1)
        _pump(qapp, 60)
        starts = [s.t_start for s in window._script_steps]
        assert starts[1] == 5000
        # 其后的键盘步骤整体平移，间隔保持
        assert starts[2] == 6000
        assert window._unsaved is True
        assert "时间已改为" in window.message_label.text()
        # 列表时间列同步刷新
        assert "5.000s" in window.event_list.table.item(1, 4).text()

    def test_edit_step_duration_via_dialog(self, window, qapp, monkeypatch):
        """第九阶段：弹窗改持续时长 —— 内部事件等比缩放，后续整体平移。"""
        self._prepare(window, qapp)
        # 键盘步骤（press 2000 / release 2040，时长 40ms）改成 2000ms
        monkeypatch.setattr("ui.main_window.ask_step_time",
                            staticmethod(lambda *a, **k: (2000, 2000)))
        window.edit_step_time(2)
        _pump(qapp, 60)
        step = window._script_steps[2]
        assert step.t_start == 2000, "起始时间不变"
        assert step.t_end == 4000, "结束 = 起始 + 新时长"
        assert window._unsaved is True
        assert "持续" in window.message_label.text()
        # 时间列同步刷新（按住区间显示 起始 → 结束）
        assert "4.000s" in window.event_list.table.item(2, 4).text()

    def test_edit_step_duration_shifts_following(self, window, qapp, monkeypatch):
        """改中间步骤的时长：后续步骤按差值整体平移。"""
        self._prepare(window, qapp)
        # 滚轮步骤（1000ms，单事件时长 0）时长改成 1000ms → 后续 +1000ms
        monkeypatch.setattr("ui.main_window.ask_step_time",
                            staticmethod(lambda *a, **k: (1000, 1000)))
        window.edit_step_time(1)
        _pump(qapp, 60)
        step = window._script_steps[2]
        assert step.t_start == 3000, f"后续步骤应 +1000ms：{step.t_start}"
        assert step.t_end == 3040

    def test_edit_step_time_unchanged_is_noop(self, window, qapp, monkeypatch):
        """起始与时长都没改时不写数据。"""
        self._prepare(window, qapp)
        monkeypatch.setattr("ui.main_window.ask_step_time",
                            staticmethod(lambda *a, **k: (2000, 40)))
        window.edit_step_time(2)
        assert "未改动" in window.message_label.text()
        assert window._unsaved is False

    def test_step_time_dialog_preview_and_warning(self, window, qapp):
        """弹窗实时预览「新起始 → 新结束」，早于上一步结束时给出警告。"""
        from ui.time_dialog import StepTimeDialog

        dialog = StepTimeDialog("左键单击", 2, 5, 5000, 5040, 3000, window)
        # 实时预览：起始 5s + 持续 40ms → 结束 5.04s
        assert "5.000s" in dialog.preview.text()
        assert "5.040s" in dialog.preview.text()
        assert dialog.preview.objectName() == "DialogPreview"
        # 改持续时长 → 结束联动
        dialog.duration_fields.set_value_ms(2000)
        assert "7.000s" in dialog.preview.text()
        # 起始早于上一步结束 → 警告配色 + 提示
        dialog.start_fields.set_value_ms(1000)
        assert dialog.preview.objectName() == "DialogWarn"
        assert "早于上一步结束" in dialog.preview.text()
        dialog.deleteLater()

    def test_step_time_dialog_two_field_groups(self, window, qapp):
        """弹窗必须同时有「起始时间」「持续时长」两组输入与各自的快捷按钮。"""
        from PySide6.QtWidgets import QGroupBox

        from ui.time_dialog import StepTimeDialog

        dialog = StepTimeDialog("左键单击", 1, 5, 1000, 1000, 0, window)
        boxes = [b.title() for b in dialog.findChildren(QGroupBox)]
        assert any("起始时间" in t for t in boxes), boxes
        assert any("持续时长" in t for t in boxes), boxes
        # 初始值：起始 1000ms、持续 0ms
        assert dialog.start_fields.value_ms() == 1000
        assert dialog.duration_fields.value_ms() == 0
        assert dialog.values() == (1000, 0)
        dialog.deleteLater()

    def test_edit_step_time_cancelled(self, window, qapp, monkeypatch):
        events = self._prepare(window, qapp)
        before = [s.t_start for s in window._script_steps]
        monkeypatch.setattr("ui.main_window.ask_step_time",
                            staticmethod(lambda *a, **k: None))
        window.edit_step_time(1)
        assert [s.t_start for s in window._script_steps] == before

    def test_time_edit_blocked_while_recording(self, window, qapp, monkeypatch):
        monkeypatch.setattr(window.recorder, "start", lambda **kwargs: True)
        monkeypatch.setattr(window.recorder, "stop", lambda: [])
        self._prepare(window, qapp)
        window.start_recording()
        window.edit_step_time(0)
        assert "正在录制中" in window.message_label.text()
        window.stop_recording()
        _pump(qapp, 80)

    def test_time_edit_blocked_while_replaying(self, window, qapp):
        self._prepare(window, qapp)
        window._set_state(State.REPLAYING)
        window.edit_step_time(0)
        assert "回放中无法修改" in window.message_label.text()
        window._set_state(State.IDLE)

    def test_reset_total_duration(self, window, qapp, monkeypatch):
        self._prepare(window, qapp)
        monkeypatch.setattr("ui.main_window.ask_total_duration",
                            staticmethod(lambda *a, **k: 30_000))
        window.reset_total_duration()
        _pump(qapp, 60)
        assert ep.events_duration_ms(window._script_events) == 30_000
        assert "均匀铺开" in window.message_label.text()
        assert window._unsaved is True

    def test_reset_total_duration_cancelled(self, window, qapp, monkeypatch):
        events = self._prepare(window, qapp)
        before = ep.events_duration_ms(window._script_events)
        monkeypatch.setattr("ui.main_window.ask_total_duration",
                            staticmethod(lambda *a, **k: None))
        window.reset_total_duration()
        assert ep.events_duration_ms(window._script_events) == before

    def test_reset_duration_persists_after_save(self, window, qapp, monkeypatch):
        self._prepare(window, qapp)
        window.set_script_name("时长重设测试")
        monkeypatch.setattr("ui.main_window.ask_total_duration",
                            staticmethod(lambda *a, **k: 20_000))
        window.reset_total_duration()
        _pump(qapp, 60)
        path = window.save_edited_steps()
        assert path is not None
        payload = ep.load_script(path)
        assert payload["duration_ms"] == 20_000

    def test_reset_duration_without_content(self, window):
        window.reset_total_duration()
        assert "没有可调整的步骤" in window.message_label.text()

    def test_time_dialog_parses_parts(self, window, qapp):
        from ui.time_dialog import DurationDialog

        dialog = DurationDialog(60_000, 5, window)
        dialog.spin_min.setValue(1)
        dialog.spin_sec.setValue(30)
        dialog.spin_ms.setValue(250)
        assert dialog.value_ms() == 90_250
        assert "90" in dialog.preview.text()
        dialog.set_value_ms(5000)
        assert dialog.value_ms() == 5000
        dialog.deleteLater()


class TestModeOneStatusSync:
    def _timeline(self, count: int = 6) -> list[dict]:
        """构造 ``count`` 个互不合并的步骤（移动/滚动交替）。"""
        events: list[dict] = []
        for i in range(count):
            if i % 2 == 0:
                events.append(ep.make_move_event(i * 100, i, i))
            else:
                events.append(ep.make_scroll_event(i * 100, 0, -1, i, i))
        return events

    def test_step_progress_refreshes_during_replay(self, window, qapp):
        """BUG 修复回归：模式一播放时底部「步骤 x / N」必须跟着走。"""
        events = self._timeline(6)
        _load_into_window(window, events)
        _install_null_controllers(window)
        window.mode_combo.setCurrentIndex(0)
        _pump(qapp, 60)

        assert len(window._script_steps) == 6
        assert window.step_label.text() == "步骤 0 / 6"
        window.start_replay()
        labels = set()
        for _ in range(30):
            _pump(qapp, 40)
            labels.add(window.step_label.text())
        window.stop_replay()
        _pump(qapp, 60)
        # 回放期间出现过「步骤 1 / 6」之后的状态，说明进度确实在刷新
        assert any(lbl != "步骤 0 / 6" for lbl in labels), f"进度未刷新：{labels}"

    def test_replay_highlight_matches_progress(self, window, qapp):
        """高亮行号与状态栏进度必须一致（同一份步骤数据）。

        用较长的步骤间隔，保证断言时回放仍在进行中。
        """
        events: list[dict] = []
        for i in range(8):
            if i % 2 == 0:
                events.append(ep.make_move_event(i * 400, i, i))
            else:
                events.append(ep.make_scroll_event(i * 400, 0, -1, i, i))
        _load_into_window(window, events)
        _install_null_controllers(window)
        window.mode_combo.setCurrentIndex(0)
        window.start_replay()
        _pump(qapp, 900)
        assert window.state is State.REPLAYING, "此时应仍在回放中"
        row = window.event_list.highlighted
        assert row >= 0, "回放中应有高亮行"
        assert window.step_label.text() == f"步骤 {row + 1} / 8"
        window.stop_replay()
        _pump(qapp, 60)
        assert window.step_label.text() == "步骤 0 / 8"
        assert window.event_list.highlighted == -1

    def test_update_step_progress_helper(self, window, qapp):
        events = self._timeline(4)
        _load_into_window(window, events)
        assert len(window._script_steps) == 4
        window._update_step_progress(2)
        assert window.step_label.text() == "步骤 3 / 4"
        window.player.step_reset()
        window._update_step_progress(None)
        assert window.step_label.text() == "步骤 0 / 4"


class TestAppIcon:
    def test_icon_file_exists_and_valid(self):
        """assets/icon.ico 必须是合法的多尺寸 ICO。"""
        import struct

        root = Path(__file__).resolve().parent.parent
        icon = root / "assets" / "icon.ico"
        assert icon.exists(), "缺少 assets/icon.ico（可运行 tools/make_icon.py 生成）"
        data = icon.read_bytes()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        assert reserved == 0 and kind == 1
        assert count >= 4, f"ICO 应包含多个尺寸，实际 {count}"
        sizes = []
        for i in range(count):
            entry = data[6 + 16 * i:6 + 16 * (i + 1)]
            sizes.append(entry[0] or 256)
            offset = struct.unpack("<I", entry[12:16])[0]
            assert data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n", "目录项应指向 PNG 数据"
        assert 256 in sizes and 32 in sizes

    def test_load_app_icon(self, qapp):
        from utils.win_utils import load_app_icon

        icon = load_app_icon()
        assert icon is not None and not icon.isNull()

    def test_window_uses_icon(self, qapp):
        from utils.win_utils import load_app_icon

        icon = load_app_icon()
        qapp.setWindowIcon(icon)
        assert not qapp.windowIcon().isNull()

    def test_fallback_icon_renders(self, qapp):
        """没有 ico 文件时也要能现画一个图标（不返回 None）。"""
        from utils.win_utils import build_fallback_icon, ensure_cjk_font

        ensure_cjk_font()
        icon = build_fallback_icon()
        assert not icon.isNull()
        assert icon.availableSizes()


# ===========================================================================
# 第四阶段：热键独占模式
# ===========================================================================
class TestHotkeyExclusiveGUI:
    def test_exclusive_defaults_on(self, window):
        assert window.config.hotkey_exclusive is True
        assert window.hotkeys.exclusive is True
        assert window.page_settings.chk_exclusive.isChecked() is True

    def test_toggle_off_disables_exclusive(self, window, qapp):
        window._show_page(PAGE_SETTINGS)
        window.page_settings.chk_exclusive.setChecked(False)
        _pump(qapp, 60)

        assert window.hotkeys.exclusive is False
        assert window.config.hotkey_exclusive is False
        assert Config(window.config.path).hotkey_exclusive is False
        assert "已关闭热键独占模式" in window.message_label.text()
        # 关闭后热键仍须可用
        assert window.hotkeys.is_running

    def test_toggle_back_on_enables_exclusive(self, window, qapp):
        window._show_page(PAGE_SETTINGS)
        window.page_settings.chk_exclusive.setChecked(False)
        _pump(qapp, 40)
        window.page_settings.chk_exclusive.setChecked(True)
        _pump(qapp, 60)

        assert window.hotkeys.exclusive is True
        assert window.config.hotkey_exclusive is True
        if hook_supported():
            assert window.hotkeys.is_running
            assert window.hotkeys._hook is not None, "独占模式应使用 Win32 钩子"
            assert window.hotkeys._listener is None, "独占模式不应再挂 pynput 监听"

    def test_settings_load_reflects_config(self, window, qapp):
        window.config.set_hotkey_exclusive(False)
        window.config.save()
        window.page_settings.load_from_config(window.config)
        assert window.page_settings.chk_exclusive.isChecked() is False
        # 载入不应反过来触发信号
        assert window.hotkeys.exclusive is True or window.config.hotkey_exclusive is False

    def test_exclusive_fallback_keeps_hotkeys_working(self, window, qapp, monkeypatch):
        """独占启动失败时要退化到普通监听，热键不能失效。"""
        import ui.main_window as mw_mod

        monkeypatch.setattr(mw_mod.HotkeyManager, "_start_exclusive", lambda self: False)
        # 屏蔽“热键注册失败”的延时提示，避免它覆盖退化提示
        monkeypatch.setattr(mw_mod.QTimer, "singleShot",
                            staticmethod(lambda *a, **k: None))
        window.config.set_hotkey_exclusive(True)
        window._register_hotkeys()
        _pump(qapp, 80)

        # 退化成功 => 仍在运行，只是不再是独占
        assert window.hotkeys.is_running, "退化后热键必须仍然可用"
        assert window.hotkeys.exclusive is False
        assert window._hotkey_failures == [], window._hotkey_failures
        assert "退化为普通监听" in window.message_label.text()
        # 配置仍保留用户的选择，下次启动会再试一次独占
        assert window.config.hotkey_exclusive is True

    @pytest.mark.skipif(not hook_supported(), reason="仅 Windows 支持独占钩子")
    def test_close_uninstalls_hook(self, qapp, monkeypatch, tmp_path):
        """窗口关闭后钩子必须卸载，系统键盘恢复正常。"""
        cfg = Config(tmp_path / "cfg.json")
        cfg.set("scripts_dir", str(tmp_path / "scripts"))
        cfg.save()
        window = MainWindow(cfg)
        window.show()
        _pump(qapp, 60)
        hook = window.hotkeys._hook
        assert hook is not None and hook.hook_alive()
        window.close()
        _pump(qapp, 60)
        assert window.hotkeys._hook is None
        assert not hook.is_running
        assert not hook.hook_alive()


# ===========================================================================
# 第五阶段：智能合并开关 / 步骤参数编辑 / 历史脚本下拉框
# ===========================================================================
class TestMergeTogglesGUI:
    def test_toggles_default_on(self, window):
        assert window.config.merge_combo is True
        assert window.config.merge_multi_click is True
        assert window.page_settings.chk_merge_combo.isChecked() is True
        assert window.page_settings.chk_merge_multi.isChecked() is True

    def test_toggle_combo_off_resplits_steps(self, window, qapp):
        """关掉组合键合并 -> 列表立刻从 1 步变成 3 步。"""
        events = [
            ep.make_key_event(0, "press", "vk:17", 17),
            ep.make_key_event(15, "press", "vk:86", 86),
            ep.make_key_event(60, "release", "vk:86", 86),
            ep.make_key_event(75, "release", "vk:17", 17),
        ]
        _load_into_window(window, events)
        assert [s.kind for s in window._script_steps] == ["combo"]
        assert window._script_steps[0].description == "组合键 Ctrl+V"

        window.page_settings.chk_merge_combo.setChecked(False)
        _pump(qapp, 60)
        assert window.config.merge_combo is False
        assert [s.kind for s in window._script_steps] == ["key", "key", "key"]
        assert window._script_steps[0].description == "键盘 Ctrl 按下"
        # 原始事件不能被破坏
        assert len(window._script_events) == 4

        window.page_settings.chk_merge_combo.setChecked(True)
        _pump(qapp, 60)
        assert [s.kind for s in window._script_steps] == ["combo"]

    def test_toggle_multi_click_off(self, window, qapp):
        events = [
            ep.make_click_event(0, "press", "left", 10, 20),
            ep.make_click_event(40, "release", "left", 10, 20),
            ep.make_click_event(120, "press", "left", 10, 20),
            ep.make_click_event(160, "release", "left", 10, 20),
        ]
        _load_into_window(window, events)
        assert [s.kind for s in window._script_steps] == ["multi"]

        window.page_settings.chk_merge_multi.setChecked(False)
        _pump(qapp, 60)
        assert [s.kind for s in window._script_steps] == ["click", "click"]

    def test_merge_toggle_persists(self, window, qapp):
        window.page_settings.chk_merge_combo.setChecked(False)
        _pump(qapp, 60)
        reloaded = Config(window.config.path)
        assert reloaded.merge_combo is False
        assert reloaded.merge_multi_click is True, (
            f"multi_click 被意外改掉了：path={window.config.path} "
            f"checkbox={window.page_settings.chk_merge_multi.isChecked()}"
        )

    def test_old_script_auto_merges_on_load(self, window, qapp, tmp_path):
        """旧脚本（没有合并结构）载入时自动合并成组合键一步。"""
        path = tmp_path / "old.json"
        ep.save_script(path, "旧脚本", [
            ep.make_key_event(0, "press", "vk:17", 17),
            ep.make_key_event(15, "press", "vk:86", 86),
            ep.make_key_event(60, "release", "vk:86", 86),
            ep.make_key_event(75, "release", "vk:17", 17),
        ])
        window.load_script_file(path)
        _pump(qapp, 60)
        assert len(window._script_steps) == 1
        assert window._script_steps[0].kind == "combo"
        assert window._script_steps[0].description == "组合键 Ctrl+V"


class TestStepParamEditGUI:
    def _load_single_click(self, window, qapp, x=100, y=200):
        _load_into_window(window, [
            ep.make_click_event(0, "press", "left", x, y),
            ep.make_click_event(40, "release", "left", x, y),
        ])
        _pump(qapp, 40)

    def test_edit_click_coordinates(self, window, qapp, monkeypatch):
        from ui.step_editor import StepEdit

        self._load_single_click(window, qapp)
        monkeypatch.setattr("ui.main_window.edit_step_dialog",
                            staticmethod(lambda *a, **k: StepEdit(
                                kind="click", x=777, y=888,
                                button="left", click_count=1)))
        window.edit_step_params(0)
        _pump(qapp, 60)

        events = window._script_events
        assert all(e["x"] == 777 and e["y"] == 888 for e in events)
        assert all(e["button"] == "left" for e in events)
        assert window._unsaved is True
        assert "777" in window._script_steps[0].description

    def test_edit_click_to_double_click(self, window, qapp, monkeypatch):
        from ui.step_editor import StepEdit

        self._load_single_click(window, qapp)
        monkeypatch.setattr("ui.main_window.edit_step_dialog",
                            staticmethod(lambda *a, **k: StepEdit(
                                kind="multi", x=100, y=200,
                                button="left", click_count=2)))
        window.edit_step_params(0)
        _pump(qapp, 60)

        assert window._script_steps[0].kind == "multi"
        assert window._script_steps[0].description == "鼠标双击 (100, 200)"
        assert len(window._script_events) == 4

    def test_edit_click_to_right_button(self, window, qapp, monkeypatch):
        from ui.step_editor import StepEdit

        self._load_single_click(window, qapp)
        monkeypatch.setattr("ui.main_window.edit_step_dialog",
                            staticmethod(lambda *a, **k: StepEdit(
                                kind="click", x=100, y=200,
                                button="right", click_count=1)))
        window.edit_step_params(0)
        _pump(qapp, 60)
        assert all(e["button"] == "right" for e in window._script_events)
        assert "右击" in window._script_steps[0].description

    def test_edit_key_changes_token(self, window, qapp, monkeypatch):
        from ui.step_editor import StepEdit

        _load_into_window(window, [
            ep.make_key_event(0, "press", "vk:65", 65),
            ep.make_key_event(40, "release", "vk:65", 65),
        ])
        _pump(qapp, 40)
        monkeypatch.setattr("ui.main_window.edit_step_dialog",
                            staticmethod(lambda *a, **k: StepEdit(
                                kind="key", key="vk:66", vk=66)))
        window.edit_step_params(0)
        _pump(qapp, 60)

        assert all(e["key"] == "vk:66" for e in window._script_events)
        # 按下/释放配对保持不变
        assert [e["action"] for e in window._script_events] == ["press", "release"]
        assert "B" in window._script_steps[0].description

    def test_edit_combo_modifiers(self, window, qapp, monkeypatch):
        from ui.step_editor import StepEdit

        _load_into_window(window, [
            ep.make_key_event(0, "press", "vk:17", 17),
            ep.make_key_event(15, "press", "vk:86", 86),
            ep.make_key_event(60, "release", "vk:86", 86),
            ep.make_key_event(75, "release", "vk:17", 17),
        ])
        _pump(qapp, 40)
        monkeypatch.setattr("ui.main_window.edit_step_dialog",
                            staticmethod(lambda *a, **k: StepEdit(
                                kind="combo", modifiers=["Ctrl", "Shift"],
                                key="vk:86", vk=86)))
        window.edit_step_params(0)
        _pump(qapp, 60)
        assert window._script_steps[0].kind == "combo"
        assert window._script_steps[0].description == "组合键 Ctrl+Shift+V"

    def test_edit_move_coordinates(self, window, qapp, monkeypatch):
        from ui.step_editor import StepEdit

        _load_into_window(window, [ep.make_move_event(0, 5, 5)])
        _pump(qapp, 40)
        monkeypatch.setattr("ui.main_window.edit_step_dialog",
                            staticmethod(lambda *a, **k: StepEdit(
                                kind="move", x=640, y=480)))
        window.edit_step_params(0)
        _pump(qapp, 60)
        assert window._script_events[0]["x"] == 640
        assert window._script_events[0]["y"] == 480

    def test_edit_cancelled_changes_nothing(self, window, qapp, monkeypatch):
        self._load_single_click(window, qapp)
        before = [dict(e) for e in window._script_events]
        monkeypatch.setattr("ui.main_window.edit_step_dialog",
                            staticmethod(lambda *a, **k: None))
        window.edit_step_params(0)
        assert window._script_events == before

    def test_edit_blocked_while_recording(self, window, qapp, monkeypatch):
        self._load_single_click(window, qapp)
        monkeypatch.setattr(window.recorder, "start", lambda **kwargs: True)
        monkeypatch.setattr(window.recorder, "stop", lambda: [])
        window.start_recording()
        window.edit_step_params(0)
        assert "无法编辑" in window.message_label.text()
        window.stop_recording()
        _pump(qapp, 80)

    def test_edit_blocked_while_replaying(self, window, qapp):
        self._load_single_click(window, qapp)
        window._set_state(State.REPLAYING)
        window.edit_step_params(0)
        assert "回放中无法编辑" in window.message_label.text()
        window._set_state(State.IDLE)

    def test_edit_out_of_range_row(self, window, qapp):
        _load_into_window(window, [ep.make_move_event(0, 1, 1)])
        window.edit_step_params(99)
        assert "请先在列表里点选" in window.message_label.text()

    def test_edit_step_action_via_toolbar(self, window, qapp, monkeypatch):
        """工具栏「编辑这一步」按钮走的是 edit_step 动作。"""
        from ui.step_editor import StepEdit

        self._load_single_click(window, qapp)
        window.event_list.set_focus_row(0)
        monkeypatch.setattr("ui.main_window.edit_step_dialog",
                            staticmethod(lambda *a, **k: StepEdit(
                                kind="click", x=321, y=654,
                                button="left", click_count=1)))
        window._on_edit_action("edit_step")
        _pump(qapp, 60)
        assert window._script_events[0]["x"] == 321

    def test_edit_then_save_persists(self, window, qapp, monkeypatch):
        """编辑后保存，重新加载脚本修改仍然生效。"""
        from ui.step_editor import StepEdit

        self._load_single_click(window, qapp)
        window.set_script_name("参数编辑测试")
        monkeypatch.setattr("ui.main_window.edit_step_dialog",
                            staticmethod(lambda *a, **k: StepEdit(
                                kind="multi", x=444, y=333,
                                button="left", click_count=2)))
        window.edit_step_params(0)
        _pump(qapp, 60)
        path = window.save_edited_steps()
        assert path is not None

        payload = ep.load_script(path)
        assert all(e["x"] == 444 and e["y"] == 333 for e in payload["events"])
        assert len(payload["events"]) == 4
        steps = ep.build_steps(payload["events"])
        assert steps[0].kind == "multi"
        assert steps[0].description == "鼠标双击 (444, 333)"


class TestMouseAndKeyStepEditorDialogs:
    """对话框本身的行为（不经过主窗口）。"""

    def test_mouse_dialog_defaults_from_step(self, qapp):
        from ui.step_editor import MouseStepDialog

        step = ep.build_steps([
            ep.make_click_event(0, "press", "right", 11, 22),
            ep.make_click_event(40, "release", "right", 11, 22),
        ])[0]
        dialog = MouseStepDialog(step)
        assert dialog.spin_x.value() == 11
        assert dialog.spin_y.value() == 22
        edit = dialog.collect()
        assert edit.button == "right" and edit.click_count == 1
        assert edit.kind == "click"
        dialog.deleteLater()

    def test_mouse_dialog_double_click(self, qapp):
        from ui.step_editor import MouseStepDialog

        step = ep.build_steps([
            ep.make_click_event(0, "press", "left", 5, 6),
            ep.make_click_event(40, "release", "left", 5, 6),
            ep.make_click_event(120, "press", "left", 5, 6),
            ep.make_click_event(160, "release", "left", 5, 6),
        ])[0]
        assert step.kind == "multi"
        dialog = MouseStepDialog(step)
        edit = dialog.collect()
        assert edit.kind == "multi" and edit.click_count == 2
        assert "双击" in dialog.preview.text()
        dialog.deleteLater()

    def test_mouse_dialog_switch_to_triple(self, qapp):
        from ui.step_editor import MOUSE_ACTIONS, MouseStepDialog

        step = ep.build_steps([
            ep.make_click_event(0, "press", "left", 5, 6),
            ep.make_click_event(40, "release", "left", 5, 6),
        ])[0]
        dialog = MouseStepDialog(step)
        index = next(i for i, (_l, b, c) in enumerate(MOUSE_ACTIONS)
                     if b == "left" and c == 3)
        dialog.action_combo.setCurrentIndex(index)
        edit = dialog.collect()
        assert edit.click_count == 3 and edit.kind == "multi"
        assert "三击" in dialog.preview.text()
        dialog.deleteLater()

    def test_move_dialog_keeps_kind(self, qapp):
        from ui.step_editor import MoveStepDialog

        step = ep.build_steps([ep.make_move_event(0, 7, 8)])[0]
        dialog = MoveStepDialog(step)
        dialog.spin_x.setValue(99)
        edit = dialog.collect()
        assert edit.kind == "move" and edit.x == 99 and edit.y == 8
        dialog.deleteLater()

    def test_scroll_dialog_edits_amount(self, qapp):
        from ui.step_editor import MoveStepDialog

        step = ep.build_steps([ep.make_scroll_event(0, 0, -2, 3, 4)])[0]
        dialog = MoveStepDialog(step)
        dialog.spin_dy.setValue(-5)
        edit = dialog.collect()
        assert edit.kind == "scroll" and edit.dy == -5
        dialog.deleteLater()

    def test_key_dialog_collects_token(self, qapp):
        from ui.step_editor import KeyStepDialog

        step = ep.build_steps([
            ep.make_key_event(0, "press", "vk:65", 65),
            ep.make_key_event(40, "release", "vk:65", 65),
        ])[0]
        dialog = KeyStepDialog(step)
        assert dialog.capture.token() == "vk:65"
        dialog.key_combo.setCurrentIndex(
            next(i for i in range(dialog.key_combo.count())
                 if dialog.key_combo.itemData(i) == "v"))
        edit = dialog.collect()
        assert edit.kind == "key" and edit.key == "v"
        dialog.deleteLater()

    def test_combo_dialog_edits_modifiers(self, qapp):
        from ui.step_editor import ComboStepDialog

        step = ep.build_steps([
            ep.make_key_event(0, "press", "vk:17", 17),
            ep.make_key_event(15, "press", "vk:86", 86),
            ep.make_key_event(60, "release", "vk:86", 86),
            ep.make_key_event(75, "release", "vk:17", 17),
        ])[0]
        dialog = ComboStepDialog(step)
        assert dialog.mod_checks["Ctrl"].isChecked()
        assert not dialog.mod_checks["Shift"].isChecked()
        dialog.mod_checks["Shift"].setChecked(True)
        edit = dialog.collect()
        assert edit.kind == "combo"
        assert edit.modifiers == ["Ctrl", "Shift"]
        assert "Ctrl+Shift" in dialog.preview.text()
        dialog.deleteLater()

    def test_combo_dialog_requires_modifier(self, qapp):
        from ui.step_editor import ComboStepDialog

        step = ep.build_steps([
            ep.make_key_event(0, "press", "vk:17", 17),
            ep.make_key_event(15, "press", "vk:86", 86),
            ep.make_key_event(60, "release", "vk:86", 86),
            ep.make_key_event(75, "release", "vk:17", 17),
        ])[0]
        dialog = ComboStepDialog(step)
        dialog.mod_checks["Ctrl"].setChecked(False)
        dialog._on_accept()
        assert dialog.result_edit() is None, "没有修饰键时不应接受"
        assert "至少要勾选" in dialog.preview.text()
        dialog.deleteLater()

    def test_edit_step_dialog_dispatch(self, qapp, monkeypatch):
        """edit_step_dialog 按类型选择正确的对话框类。"""
        import ui.step_editor as se

        cases = {
            "click": se.MouseStepDialog,
            "multi": se.MouseStepDialog,
            "move": se.MoveStepDialog,
            "scroll": se.MoveStepDialog,
            "key": se.KeyStepDialog,
            "combo": se.ComboStepDialog,
        }
        for kind, expected in cases.items():
            captured: dict = {}

            class _Probe(expected):  # type: ignore[misc, valid-type]
                def __init__(self, step, parent=None):
                    captured["cls"] = type(self).__mro__[1]
                    raise RuntimeError("stop-early")

            monkeypatch.setattr(se, expected.__name__, _Probe)
            step = ep.Step(index=1, kind=kind, description="x", events=[
                {"t": 0, "type": "key", "action": "press", "key": "vk:17", "vk": 17},
            ])
            with pytest.raises(RuntimeError):
                se.edit_step_dialog(step)
            assert captured["cls"] is expected


class TestScriptHistoryCombo:
    def test_combo_lists_scripts(self, window, qapp):
        combo = window.script_name_edit
        names = [combo.itemText(i) for i in range(combo.count())]
        for path in ep.list_scripts(window.config.scripts_path()):
            assert path.stem in names

    def test_selecting_script_loads_it(self, window, qapp):
        import time as _time

        scripts = ep.list_scripts(window.config.scripts_path())
        if not scripts:
            pytest.skip("脚本目录为空")
        target = scripts[0]
        combo = window.script_name_edit
        index = next(i for i in range(combo.count())
                     if combo.itemData(i) == str(target))
        combo.setCurrentIndex(index)
        combo.last_popup_hide = _time.monotonic()  # 模拟用户在下拉列表里点选
        window._on_script_combo_activated(index)
        _pump(qapp, 80)

        assert window._current_script == target
        assert window.script_name() == target.stem
        assert len(window._script_events) > 0
        assert window.step_label.text().startswith("步骤 0 /")
        assert "已从下拉框载入" in window.message_label.text()

    def test_spurious_activation_ignored(self, window, qapp):
        """焦点离开引起的伪 activated 不得加载脚本、不得跳页。

        真实 Bug（render_shot 复现）：可编辑下拉框在焦点离开时会
        发射 activated，切到「回放与脚本」页的瞬间被拉回录制页。
        """
        scripts_dir = window.config.scripts_path()
        scripts_dir.mkdir(parents=True, exist_ok=True)
        ep.save_script(scripts_dir / "伪激活测试.json", "伪激活测试",
                       [ep.make_move_event(0, 1, 1)])
        window.refresh_script_library()
        window._show_page(PAGE_SCRIPTS)
        before = window._current_script
        # 没有打开下拉列表、也没有手输 → 伪激活，必须被忽略
        window._on_script_combo_activated(0)
        _pump(qapp, 40)
        assert window._current_script == before
        assert window.pages.currentIndex() == PAGE_SCRIPTS

    def test_combo_refresh_keeps_typed_name(self, window, qapp):
        window.set_script_name("我还没保存的名字")
        window.refresh_script_library()
        _pump(qapp, 40)
        assert window.script_name() == "我还没保存的名字"

    def test_combo_disabled_while_recording(self, window, qapp, monkeypatch):
        import time as _time

        monkeypatch.setattr(window.recorder, "start", lambda **kwargs: True)
        monkeypatch.setattr(window.recorder, "stop", lambda: [])
        window.start_recording()
        before = window._current_script
        scripts = ep.list_scripts(window.config.scripts_path())
        if scripts:
            combo = window.script_name_edit
            combo.setCurrentIndex(0)
            combo.last_popup_hide = _time.monotonic()  # 模拟真实点选
            window._on_script_combo_activated(0)
            assert window._current_script == before
            assert "无法加载脚本" in window.message_label.text()
        window.stop_recording()
        _pump(qapp, 80)

    def test_combo_is_editable(self, window):
        assert window.script_name_edit.isEditable()
        assert window.script_name_edit.lineEdit() is not None


class TestMergedStepDisplay:
    def test_combo_row_shows_type_and_desc(self, window, qapp):
        _load_into_window(window, [
            ep.make_key_event(0, "press", "vk:17", 17),
            ep.make_key_event(15, "press", "vk:86", 86),
            ep.make_key_event(60, "release", "vk:86", 86),
            ep.make_key_event(75, "release", "vk:17", 17),
        ])
        table = window.event_list.table
        assert table.item(0, 2).text() == "组合键"
        assert "组合键 Ctrl+V" in table.item(0, 3).text()

    def test_double_click_row_shows_type_and_desc(self, window, qapp):
        _load_into_window(window, [
            ep.make_click_event(0, "press", "left", 10, 20),
            ep.make_click_event(40, "release", "left", 10, 20),
            ep.make_click_event(120, "press", "left", 10, 20),
            ep.make_click_event(160, "release", "left", 10, 20),
        ])
        table = window.event_list.table
        assert table.item(0, 2).text() == "鼠标连击"
        desc = table.item(0, 3).text()
        assert "鼠标双击 (10, 20)" in desc
        assert "2 连击" in desc


class TestMultiClickUnlimitedGUI:
    """连击次数不设上限：N>=2 全部合成一步，模式二一次热键快速连发。"""

    @staticmethod
    def _clicks(count: int, x=100, y=200) -> list[dict]:
        events: list[dict] = []
        for i in range(count):
            events += [ep.make_click_event(i * 120, "press", "left", x, y),
                       ep.make_click_event(i * 120 + 40, "release", "left", x, y)]
        return events

    @pytest.mark.parametrize("count", [2, 4, 6, 10])
    def test_all_clicks_merge_into_one_step(self, window, qapp, count):
        _load_into_window(window, self._clicks(count))
        assert len(window._script_steps) == 1, \
            [s.description for s in window._script_steps]
        step = window._script_steps[0]
        assert step.kind == "multi"
        assert step.click_count == count
        assert step.event_count == count * 2

    def test_display_wording(self, window, qapp):
        _load_into_window(window, self._clicks(6))
        table = window.event_list.table
        assert table.item(0, 2).text() == "鼠标连击"
        desc = table.item(0, 3).text()
        assert "鼠标连击6次 (100, 200)" in desc
        assert "6 连击" in desc
        # 时间列对多连击用 (N击) 缩短显示
        assert "(6击)" in table.item(0, 4).text()

    def test_mode2_fires_all_clicks_quickly(self, window, qapp):
        """一次热键要发完 N 次点击，总耗时远小于 N × 按压间隔。"""
        import time as _time

        _load_into_window(window, self._clicks(8))
        _install_null_controllers(window)
        window.mode_combo.setCurrentIndex(1)
        _pump(qapp, 40)

        seen: list[tuple[str, str]] = []
        inner = window.player._mouse_ctl()

        class _Probe:
            position = inner.position

            def press(self, button):
                seen.append(("press", str(button)))
                inner.press(button)

            def release(self, button):
                seen.append(("release", str(button)))
                inner.release(button)

            def scroll(self, *a):
                pass

            def release_all(self):
                pass

        window.player.set_controllers(mouse_ctl=_Probe(),
                                      keyboard_ctl=window.player._keyboard_ctl())
        t0 = _time.monotonic()
        window.player.step_next()
        elapsed = (_time.monotonic() - t0) * 1000

        assert len([1 for a, _ in seen if a == "press"]) == 8, seen
        assert elapsed < 400, f"8 连击耗时 {elapsed:.0f}ms，太慢"

    def test_split_button_creates_independent_steps(self, window, qapp, monkeypatch):
        """编辑弹窗勾选「拆分为独立单击」-> 变成 N 个单击步骤且不再被合并。"""
        from ui.step_editor import StepEdit

        _load_into_window(window, self._clicks(5))
        assert len(window._script_steps) == 1
        monkeypatch.setattr("ui.main_window.edit_step_dialog",
                            staticmethod(lambda *a, **k: StepEdit(
                                kind="click", split_clicks=True)))
        window.edit_step_params(0)
        _pump(qapp, 60)

        steps = window._script_steps
        assert len(steps) == 5, [s.description for s in steps]
        assert all(s.kind == "click" for s in steps)
        assert all("左键单击" in s.description for s in steps)
        # 事件总数守恒
        assert len(window._script_events) == 10
        assert "拆分为 5 个独立单击步骤" in window.message_label.text()
        assert window._unsaved is True

    def test_split_survives_reload(self, window, qapp, monkeypatch, tmp_path):
        """拆分结果保存后重新载入仍是 5 个单击步骤。"""
        from ui.step_editor import StepEdit

        _load_into_window(window, self._clicks(4))
        monkeypatch.setattr("ui.main_window.edit_step_dialog",
                            staticmethod(lambda *a, **k: StepEdit(
                                kind="click", split_clicks=True)))
        window.edit_step_params(0)
        _pump(qapp, 60)
        assert len(window._script_steps) == 4

        path = window.save_edited_steps()
        assert path is not None
        payload = ep.load_script(path)
        # 文件里存的是原始事件流（没有边界标记），载入时按开关重新分组
        assert len(payload["events"]) == 8
        assert all(ep.BOUNDARY_KEY not in e for e in payload["events"])

    def test_split_single_click_is_noop(self, window, qapp, monkeypatch):
        from ui.step_editor import StepEdit

        _load_into_window(window, [
            ep.make_click_event(0, "press", "left", 10, 20),
            ep.make_click_event(40, "release", "left", 10, 20),
        ])
        monkeypatch.setattr("ui.main_window.edit_step_dialog",
                            staticmethod(lambda *a, **k: StepEdit(
                                kind="click", split_clicks=True)))
        window.edit_step_params(0)
        _pump(qapp, 40)
        assert len(window._script_steps) == 1
        assert "本来就是单击" in window.message_label.text()

    def test_edit_to_high_click_count(self, window, qapp, monkeypatch):
        """编辑器把次数改成 9 也要一步到位。"""
        from ui.step_editor import StepEdit

        _load_into_window(window, self._clicks(2))
        monkeypatch.setattr("ui.main_window.edit_step_dialog",
                            staticmethod(lambda *a, **k: StepEdit(
                                kind="multi", button="left", click_count=9,
                                x=100, y=200)))
        window.edit_step_params(0)
        _pump(qapp, 60)
        assert len(window._script_steps) == 1
        assert window._script_steps[0].click_count == 9
        assert window._script_steps[0].description == "鼠标连击9次 (100, 200)"

    def test_dialog_custom_count(self, qapp):
        """对话框选「自定义动作」后可自由填连击次数。"""
        from ui.step_editor import MouseStepDialog

        step = ep.build_steps(self._clicks(2))[0]
        dialog = MouseStepDialog(step)
        # 2 连击能精确匹配下拉项
        assert not dialog._is_custom()
        assert dialog.spin_count.value() == 2

        # 选自定义那一项，改成 7
        dialog.action_combo.setCurrentIndex(
            next(i for i in range(dialog.action_combo.count())
                 if dialog.action_combo.itemData(i) is None))
        assert dialog._is_custom()
        dialog.spin_count.setValue(7)
        edit = dialog.collect()
        assert edit.click_count == 7
        assert edit.kind == "multi"
        assert "连击7次" in dialog.preview.text()
        dialog.deleteLater()

    def test_dialog_high_count_lands_on_custom(self, qapp):
        """原始步骤是 6 连击时，下拉自动落到「自定义动作」。"""
        from ui.step_editor import MouseStepDialog

        step = ep.build_steps(self._clicks(6))[0]
        dialog = MouseStepDialog(step)
        assert dialog._is_custom()
        assert dialog.spin_count.value() == 6
        assert dialog.collect().click_count == 6
        dialog.deleteLater()

    def test_dialog_split_checkbox_disables_other_controls(self, qapp):
        from ui.step_editor import MouseStepDialog

        step = ep.build_steps(self._clicks(4))[0]
        dialog = MouseStepDialog(step)
        assert dialog.action_combo.isEnabled()

        dialog.chk_split.setChecked(True)
        assert not dialog.action_combo.isEnabled()
        assert not dialog.spin_x.isEnabled()
        assert not dialog.spin_count.isEnabled()
        assert not dialog.btn_read_pos.isEnabled()
        assert "拆分为 4 个" in dialog.preview.text()
        assert dialog.collect().split_clicks is True

        dialog.chk_split.setChecked(False)
        assert dialog.action_combo.isEnabled()
        assert dialog.collect().split_clicks is False
        dialog.deleteLater()

    def test_split_then_toggle_merge_stays_split(self, window, qapp, monkeypatch):
        """拆分后再开关合并选项，拆出来的步骤保持独立。"""
        from ui.step_editor import StepEdit

        _load_into_window(window, self._clicks(4))
        monkeypatch.setattr("ui.main_window.edit_step_dialog",
                            staticmethod(lambda *a, **k: StepEdit(
                                kind="click", split_clicks=True)))
        window.edit_step_params(0)
        _pump(qapp, 60)
        assert len(window._script_steps) == 4

        window.page_settings.chk_merge_multi.setChecked(False)
        _pump(qapp, 40)
        assert len(window._script_steps) == 4
        window.page_settings.chk_merge_multi.setChecked(True)
        _pump(qapp, 40)
        assert len(window._script_steps) == 4


# ===========================================================================
# 第六阶段：窗口缩放 / 弹窗风格 / 循环回放 / 页面结构
# ===========================================================================
class TestWindowResize:
    """窗口必须能自由拖拽缩放，且尺寸位置持久化。

    第七阶段起回归原生窗口：边缘缩放/双箭头光标/双击最大化全部由
    Windows 系统边框提供，不再有任何自定义热区/遮罩/手动缩放逻辑。
    离屏平台测不到系统级拖拽，因此这里只断言「窗口属性正确」——
    真实的拖拽手感由用户肉眼验收。
    """

    def test_not_fixed_size(self, window, qapp):
        from PySide6.QtCore import Qt

        assert not (window.windowFlags() & Qt.MSWindowsFixedSizeDialogHint)
        assert not (window.windowFlags() & Qt.FramelessWindowHint)
        assert window.minimumSize() != window.maximumSize()
        assert window.maximumWidth() > 10000

    def test_geometry_persists(self, qapp, tmp_path):
        cfg = Config(tmp_path / "geo.json")
        cfg.set("scripts_dir", str(tmp_path / "scripts"))
        cfg.save()
        first = MainWindow(cfg)
        first.show()
        _pump(qapp, 80)
        first.resize(1180, 760)
        first.move(140, 110)
        _pump(qapp, 60)
        first._save_window_geometry()
        first.close()
        _pump(qapp, 40)

        reloaded = Config(cfg.path)
        assert (reloaded.get("window.width"), reloaded.get("window.height")) == (1180, 760)
        second = MainWindow(reloaded)
        second.show()
        _pump(qapp, 120)
        assert (second.width(), second.height()) == (1180, 760)
        second.close()
        _pump(qapp, 40)


class TestNumberField:
    """自绘数字框（替代 QSpinBox，免疫高 DPI 下数值被侧挡的问题）。"""

    def test_initial_value_and_clamp(self, qapp):
        from ui.spin_box import NumberField

        nf = NumberField(1, 9999, 5)
        assert nf.value() == 5
        nf.setValue(99999)
        assert nf.value() == 9999
        nf.setValue(-3)
        assert nf.value() == 1

    def test_step_buttons_and_signal(self, qapp):
        from ui.spin_box import NumberField

        nf = NumberField(0, 10, 5)
        seen: list[int] = []
        nf.valueChanged.connect(seen.append)
        nf.step_by(1)
        nf.step_by(-2)
        assert nf.value() == 4
        assert seen == [6, 4]

    def test_text_input_syncs_value(self, qapp):
        from ui.spin_box import NumberField

        nf = NumberField(0, 999, 0)
        nf.edit.setText("672")
        assert nf.value() == 672
        # 超出上限的输入会被夹紧到最大值
        nf.edit.setText("99999")
        assert nf.value() == 999
        # 失焦时文本规范成当前值（去掉越界残留文本）
        nf._on_editing_finished()
        assert nf.edit.text() == "999"

    def test_set_value_updates_text_and_blocks(self, qapp):
        from ui.spin_box import NumberField

        nf = NumberField(0, 100, 0)
        seen: list[int] = []
        nf.valueChanged.connect(seen.append)
        nf.blockSignals(True)
        nf.setValue(42)
        nf.blockSignals(False)
        assert nf.edit.text() == "42"
        assert seen == []

    def test_disabled_state(self, qapp):
        from ui.spin_box import NumberField

        nf = NumberField(0, 100, 0)
        nf.setEnabled(False)
        assert nf.isEnabled() is False
        assert nf.edit.isEnabled() is False
        assert nf.btn_up.isEnabled() is False


class TestDialogStyling:
    """所有步骤编辑弹窗都必须套用主界面 QSS。"""

    @staticmethod
    def _click_step():
        return ep.build_steps([
            ep.make_click_event(0, "press", "left", 1, 2),
            ep.make_click_event(40, "release", "left", 1, 2),
        ])[0]

    @staticmethod
    def _key_step():
        return ep.build_steps([
            ep.make_key_event(0, "press", "vk:65", 65),
            ep.make_key_event(40, "release", "vk:65", 65),
        ])[0]

    @staticmethod
    def _combo_step():
        return ep.build_steps([
            ep.make_key_event(0, "press", "vk:17", 17),
            ep.make_key_event(10, "press", "vk:86", 86),
            ep.make_key_event(50, "release", "vk:86", 86),
            ep.make_key_event(60, "release", "vk:17", 17),
        ])[0]

    def _all_dialogs(self):
        from ui.name_dialog import NameInputDialog
        from ui.step_dialog import InsertStepDialog
        from ui.step_editor import (ComboStepDialog, KeyStepDialog,
                                    MouseStepDialog, MoveStepDialog)
        from ui.time_dialog import DurationDialog, StepTimeDialog

        return [
            ("鼠标编辑", MouseStepDialog(self._click_step())),
            ("移动编辑", MoveStepDialog(
                ep.build_steps([ep.make_move_event(0, 1, 1)])[0])),
            ("滚轮编辑", MoveStepDialog(
                ep.build_steps([ep.make_scroll_event(0, 0, -1, 1, 1)])[0])),
            ("键盘编辑", KeyStepDialog(self._key_step())),
            ("组合键编辑", ComboStepDialog(self._combo_step())),
            ("插入步骤", InsertStepDialog()),
            ("单步时间", StepTimeDialog("左键单击", 1, 5, 1000, 1040, 0)),
            ("总时长", DurationDialog(30000, 5)),
            ("新建脚本", NameInputDialog("新建脚本", "输入名称：")),
        ]

    def test_every_dialog_has_stylesheet(self, qapp):
        for name, dialog in self._all_dialogs():
            assert dialog.styleSheet(), f"{name} 没有套用样式表"
            dialog.deleteLater()

    def test_mouse_dialog_all_actions_no_crash(self, qapp):
        """回归：切换「动作」下拉框所有选项 + 改连击次数不得崩溃。

        历史 Bug：_on_action_changed 里引用了不存在的局部变量
        `button`，一切换动作就抛 `name 'button' is not defined`。
        """
        from ui.step_editor import MouseStepDialog

        dialog = MouseStepDialog(self._click_step())
        for index in range(dialog.action_combo.count()):
            dialog.action_combo.setCurrentIndex(index)
            assert dialog.preview.text(), f"动作 {index} 预览为空"
        # 自定义动作下改连击次数
        dialog.action_combo.setCurrentIndex(dialog.action_combo.count() - 1)
        for count in (1, 2, 3, 7):
            dialog.spin_count.setValue(count)
            assert dialog.preview.text()
        # 「拆分」开关与动作切换组合也不能崩
        dialog.chk_split.setChecked(True)
        dialog.action_combo.setCurrentIndex(0)
        dialog.chk_split.setChecked(False)
        dialog.deleteLater()

    def test_buttons_use_design_objectnames(self, qapp):
        """确认/取消按钮必须用 PrimaryButton / GhostButton，不能是原生灰按钮。"""
        from PySide6.QtWidgets import QPushButton

        for name, dialog in self._all_dialogs():
            buttons = dialog.findChildren(QPushButton)
            names = {b.objectName() for b in buttons}
            assert "PrimaryButton" in names, f"{name} 缺少主按钮样式：{names}"
            assert "GhostButton" in names, f"{name} 缺少次按钮样式：{names}"
            # 不允许有无 objectName 的裸按钮（那就是原生样式）
            bare = [b.text() for b in buttons if not b.objectName()]
            assert not bare, f"{name} 存在未设样式的按钮：{bare}"
            dialog.deleteLater()

    def test_inputs_get_global_qss(self, qapp):
        """输入框/下拉框/数字框由全局 QSS 覆盖（不是原生外观）。"""
        from PySide6.QtWidgets import QComboBox, QLineEdit

        from ui.spin_box import NumberField

        qss = styles.build_stylesheet()
        for probe in ("QLineEdit", "QComboBox", "QSpinBox", "QDialogButtonBox"):
            assert probe in qss, probe
        # 第七阶段：SpinBox 的上下调节按钮与箭头也必须被 QSS 接管，
        # 否则会残留原生 Windows 灰色小箭头
        # （设置页的「回放速度」仍是 QDoubleSpinBox，规则不能删）
        for probe in ("QSpinBox::up-button", "QSpinBox::down-button",
                      "QSpinBox::up-arrow", "QSpinBox::down-arrow",
                      "QDoubleSpinBox::up-button", "QDoubleSpinBox::down-button"):
            assert probe in qss, probe
        # 实际控件确实存在（说明弹窗里真有这些控件被样式覆盖）
        # 第八阶段：弹窗整数框已全部换成自绘 NumberField
        dialog = self._all_dialogs()[0][1]
        assert dialog.findChildren(NumberField)
        dialog.deleteLater()
        dialog = self._all_dialogs()[5][1]
        assert dialog.findChildren(QComboBox)
        dialog.deleteLater()
        dialog = self._all_dialogs()[8][1]
        assert dialog.findChildren(QLineEdit)
        dialog.deleteLater()

    def test_warning_preview_uses_warn_style(self, qapp):
        from ui.step_editor import ComboStepDialog

        dialog = ComboStepDialog(self._combo_step())
        assert dialog.preview.objectName() == "DialogPreview"
        dialog.mod_checks["Ctrl"].setChecked(False)
        assert dialog.preview.objectName() == "DialogWarn", "无修饰键时应转为警告配色"
        dialog.mod_checks["Ctrl"].setChecked(True)
        assert dialog.preview.objectName() == "DialogPreview"
        dialog.deleteLater()


class TestCoordinateTerminology:
    """第八阶段：界面术语统一「坐标」，步骤弹窗数字框全部换自绘 NumberField。"""

    @staticmethod
    def _click_step():
        return ep.build_steps([
            ep.make_click_event(0, "press", "left", 1, 2),
            ep.make_click_event(40, "release", "left", 1, 2),
        ])[0]

    def test_no_qspinbox_in_step_dialogs(self, qapp):
        """步骤相关弹窗不再使用 QSpinBox（高 DPI 下数值会被按钮侧挡）。"""
        from PySide6.QtWidgets import QSpinBox

        from ui.spin_box import NumberField
        from ui.step_dialog import InsertStepDialog
        from ui.step_editor import MouseStepDialog, MoveStepDialog

        dialogs = [
            MouseStepDialog(self._click_step()),
            MoveStepDialog(ep.build_steps([ep.make_move_event(0, 1, 1)])[0]),
            MoveStepDialog(ep.build_steps([ep.make_scroll_event(0, 0, -1, 1, 1)])[0]),
            InsertStepDialog(),
        ]
        for dialog in dialogs:
            title = dialog.windowTitle()
            assert dialog.findChildren(NumberField), f"{title} 缺少 NumberField"
            assert not dialog.findChildren(QSpinBox), f"{title} 仍残留 QSpinBox"
            dialog.deleteLater()

    def test_settings_int_fields_are_number_fields(self, window):
        """设置页的毫秒/像素框也换成 NumberField（同类遮挡问题）。"""
        from ui.spin_box import NumberField

        page = window.page_settings
        assert isinstance(page.spin_interval, NumberField)
        assert isinstance(page.spin_distance, NumberField)

    def test_coordinate_wording_in_mouse_dialog(self, qapp):
        """「编辑鼠标步骤」弹窗：行标签与按钮都必须说「坐标」。"""
        from PySide6.QtWidgets import QLabel

        from ui.step_editor import MouseStepDialog

        dialog = MouseStepDialog(self._click_step())
        texts = [lbl.text() for lbl in dialog.findChildren(QLabel)]
        assert "坐标" in texts, f"行标签应包含「坐标」：{texts}"
        assert "位置" not in texts, f"行标签不应再出现「位置」：{texts}"
        assert dialog.btn_read_pos.text() == "读取当前鼠标坐标"
        assert "位置" not in dialog.btn_read_pos.toolTip()
        dialog.deleteLater()

    def test_clicks_only_checkboxes_removed(self, window):
        """第九阶段：两页的「仅录制点击坐标」「记录鼠标移动」复选框已删除。"""
        assert not hasattr(window, "chk_clicks_only")
        assert not hasattr(window, "chk_record_move")
        assert not hasattr(window.page_settings, "chk_clicks_only")
        assert not hasattr(window.page_settings, "chk_mouse_move")


class TestScreenPicker:
    """第八阶段：全屏遮罩取点（十字准星 + 左键取点 + Esc 取消）。"""

    @staticmethod
    def _click_step():
        return ep.build_steps([
            ep.make_click_event(0, "press", "left", 1, 2),
            ep.make_click_event(40, "release", "left", 1, 2),
        ])[0]

    def test_geometry_covers_virtual_desktop(self, qapp):
        """遮罩必须覆盖所有屏幕的并集（多显示器）。"""
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import QRect

        from ui.screen_picker import ScreenPicker

        picker = ScreenPicker()
        expected = QRect()
        for screen in QGuiApplication.screens():
            expected = expected.united(screen.geometry())
        assert picker.geometry() == expected
        # 无边框 + 置顶 + 半透明
        assert picker.windowFlags() & Qt.FramelessWindowHint
        assert picker.windowFlags() & Qt.WindowStaysOnTopHint
        picker.deleteLater()

    def test_left_click_picks_global_coordinates(self, qapp):
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent

        from ui.screen_picker import ScreenPicker

        picker = ScreenPicker()
        event = QMouseEvent(
            QEvent.MouseButtonPress, QPointF(10, 10), QPointF(500, 300),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )
        picker.mousePressEvent(event)
        assert picker.picked == (500, 300)
        assert picker.result() == QDialog.Accepted
        picker.deleteLater()

    def test_right_click_and_esc_cancel(self, qapp):
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QKeyEvent, QMouseEvent

        from ui.screen_picker import ScreenPicker

        picker = ScreenPicker()
        event = QMouseEvent(
            QEvent.MouseButtonPress, QPointF(10, 10), QPointF(500, 300),
            Qt.RightButton, Qt.RightButton, Qt.NoModifier,
        )
        picker.mousePressEvent(event)
        assert picker.picked is None
        assert picker.result() == QDialog.Rejected
        picker.deleteLater()

        picker = ScreenPicker()
        picker.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        assert picker.picked is None
        assert picker.result() == QDialog.Rejected
        picker.deleteLater()

    def test_mouse_dialog_read_cursor_via_picker(self, qapp, monkeypatch):
        """「读取当前鼠标坐标」走全屏取点并把结果填进 X / Y。"""
        import ui.screen_picker as picker_mod
        from ui.step_editor import MouseStepDialog

        monkeypatch.setattr(picker_mod, "pick_screen_point",
                            lambda _parent=None: (123, 456))
        dialog = MouseStepDialog(self._click_step())
        dialog._read_cursor()
        assert dialog.spin_x.value() == 123
        assert dialog.spin_y.value() == 456
        dialog.deleteLater()

    def test_mouse_dialog_pick_cancelled_keeps_values(self, qapp, monkeypatch):
        import ui.screen_picker as picker_mod
        from ui.step_editor import MouseStepDialog

        monkeypatch.setattr(picker_mod, "pick_screen_point",
                            lambda _parent=None: None)
        dialog = MouseStepDialog(self._click_step())
        before = (dialog.spin_x.value(), dialog.spin_y.value())
        dialog._read_cursor()
        assert (dialog.spin_x.value(), dialog.spin_y.value()) == before
        dialog.deleteLater()

    def test_insert_dialog_pick_button(self, qapp, monkeypatch):
        """「插入新步骤」坐标行带「屏幕取点」按钮，取点结果填进 X / Y。"""
        import ui.screen_picker as picker_mod
        from ui.step_dialog import InsertStepDialog

        monkeypatch.setattr(picker_mod, "pick_screen_point",
                            lambda _parent=None: (640, 480))
        dialog = InsertStepDialog()
        assert dialog.btn_pick.text() == "屏幕取点"
        assert dialog.btn_pick.objectName() == "GhostButton"
        dialog._pick_coord()
        assert dialog.spin_x.value() == 640
        assert dialog.spin_y.value() == 480
        dialog.deleteLater()


class TestUnifiedButtons:
    """第十阶段：按钮统一为蓝色单色系 —— 主操作实心蓝 / 次操作浅蓝填充 /
    工具透明幽灵 / 危险蓝边描边；全界面不出现红/绿/橙，也不残留原生灰或白底黑边。"""

    @staticmethod
    def _block(qss: str, selector: str) -> str:
        import re

        match = re.search(re.escape(selector) + r"\s*\{(.*?)\}", qss, re.S)
        assert match, f"样式表缺少 {selector}"
        return match.group(1)

    def test_base_button_is_secondary_blue(self, qapp):
        """无标记按钮（提示框的确定/取消等）落浅蓝次操作，不再是原生灰。"""
        qss = styles.build_stylesheet()
        base = self._block(qss, "QPushButton")
        assert f"background: {styles.BTN_SECONDARY}" in base, "按钮应为浅蓝背景"
        assert f"color: {styles.BTN_PRIMARY}" in base, "按钮应为蓝色字体"
        assert "border: none" in base, "按钮应无边框"
        assert "border-radius: 6px" in base, "按钮应为 6px 圆角"

    def test_four_kinds_have_blue_rules(self, qapp):
        """四档配色各自有规则，且都在蓝色系内。"""
        qss = styles.build_stylesheet()

        primary = self._block(qss, "QPushButton#PrimaryButton")
        assert f"background: {styles.BTN_PRIMARY}" in primary
        assert "color: #FFFFFF" in primary
        assert "font-weight: 600" in primary, "主操作应加粗"

        secondary = self._block(qss, "QPushButton#SecondaryButton")
        assert f"background: {styles.BTN_SECONDARY}" in secondary
        assert f"color: {styles.BTN_PRIMARY}" in secondary

        ghost = self._block(qss, "QPushButton#GhostButton, QPushButton#ToolButton")
        assert "background: transparent" in ghost
        assert f"color: {styles.BTN_GHOST_TEXT}" in ghost

        danger = self._block(qss, "QPushButton#DangerButton")
        assert "background: #FFFFFF" in danger
        assert f"border: 1px solid {styles.BTN_PRIMARY}" in danger

    def test_hover_pressed_are_blue_scale(self, qapp):
        """悬停/按下都是蓝色深浅变化，而非旧版灰阶。"""
        qss = styles.build_stylesheet()
        hover = self._block(qss, "QPushButton:hover")
        assert f"background: {styles.BTN_SECONDARY_HOVER}" in hover
        pressed = self._block(qss, "QPushButton:pressed")
        assert f"background: {styles.BTN_SECONDARY_PRESS}" in pressed

    def test_no_legacy_grays_or_black_border(self, qapp):
        """旧的白底黑边 / 深灰 pressed 已移除，避免残留原生观感。"""
        qss = styles.build_stylesheet()
        assert "#D9D9D9" not in qss, "残留旧的深灰 pressed"
        assert f"border: 1px solid {styles.TEXT}" not in qss, "残留白底黑边按钮"

    def test_button_objectnames_kept_in_code(self, window):
        """语义 objectName 保留（供测试与维护引用）：清空改为危险、主/次/工具各归其位。"""
        assert window.btn_start_record.objectName() == "PrimaryButton"
        assert window.btn_stop_play.objectName() == "DangerButton"
        assert window.btn_clear.objectName() == "DangerButton"
        assert window.btn_new_script.objectName() == "SecondaryButton"

    def test_record_action_bar_not_clipped(self, qapp, window):
        """录制页动作行的按钮不能被压得比理想宽度更窄（否则文字被裁）。

        回归背景：加上「续录脚本」后这一行变成 10 个按钮。用全局 14px 左右
        内边距时，默认 1000x700 窗口下理想宽度合计 954px、实际只有 876px，
        按钮被压窄、文字左右被裁（实测「打开脚本文件」显示成「开脚本文」、
        「重置指针」少了「针」）。现在 #RecordActionBar 把内边距收到 9px、
        间距收到 6px，合计 836px，刚好排得下。以后再往这行加按钮时，这条
        用例会先失败，避免静默裁字。
        """
        window.resize(1000, 700)
        window._show_page(PAGE_RECORD)
        _pump(qapp, 80)
        buttons = (window.btn_start_record, window.btn_append_record,
                   window.btn_pause_record, window.btn_stop_record,
                   window.btn_start_play, window.btn_stop_play,
                   window.btn_reset_step, window.btn_load,
                   window.btn_save_as, window.btn_clear)
        squeezed = [(b.text(), b.width(), b.sizeHint().width())
                    for b in buttons if b.width() < b.sizeHint().width()]
        assert squeezed == [], f"这些按钮被压窄，文字会被按钮边缘裁掉：{squeezed}"


class TestScriptsPageStructure:
    """「回放与脚本」成为默认页，并整合回放控制。"""

    def test_default_page_is_scripts(self, window, qapp):
        _pump(qapp, 60)
        assert window.pages.currentIndex() == PAGE_SCRIPTS
        assert window.sidebar._buttons["library"].is_active

    def test_sidebar_order(self, window):
        """侧边栏第一项应是「回放与脚本」（library）。"""
        keys = list(window.sidebar._buttons.keys())
        assert keys[0] == "library", keys
        assert keys[1] == "record", keys

    def test_page_has_playback_controls(self, window):
        page = window.page_library
        for attr in ("btn_play", "btn_stop_play", "loop_combo", "spin_rounds",
                     "round_label", "current_label", "btn_load"):
            assert hasattr(page, attr), attr
        assert page.btn_load.text() == "加载该脚本"

    def test_switch_to_record_page(self, window, qapp):
        window._show_page(PAGE_RECORD)
        _pump(qapp, 40)
        assert window.pages.currentIndex() == PAGE_RECORD
        assert window.sidebar._buttons["record"].is_active

    def test_status_bar_mirrored_on_scripts_page(self, window, qapp):
        _load_into_window(window, _sample_events())
        # 模拟「已加载某个脚本」的状态
        window._current_script = window.config.scripts_path() / "演示.json"
        window._refresh_status()
        _pump(qapp, 40)
        text = window.page_library.current_label.text()
        assert text.startswith("当前：演示.json"), text
        assert "步" in text

    def test_status_bar_is_window_level_singleton(self, window, qapp):
        """状态栏提升为窗口级：全窗口只有一份，且不属于任何单个页面。"""
        from PySide6.QtWidgets import QWidget

        cards = window.findChildren(QWidget, "StatusCard")
        assert len(cards) == 1, f"状态栏卡片应只有一份，实际 {len(cards)} 份"
        assert window.status_bar.parentWidget().parentWidget() is window.content_host
        # 录制页不再单独持有状态栏
        assert not window.page_record.findChildren(QWidget, "StatusCard")
        # 切换页面后依然可见（同一份，不随页面堆栈隐藏）
        window._show_page(PAGE_SCRIPTS)
        _pump(qapp, 40)
        assert window.status_bar.isVisible()
        window._show_page(PAGE_RECORD)
        _pump(qapp, 40)
        assert window.status_bar.isVisible()

    def test_mode_combo_lives_on_scripts_page(self, window, qapp):
        """模式下拉框必须在「回放与脚本」页的回放控制卡里。

        「控件建在哪一页」这类断言以前没有覆盖，正是第六阶段
        「测试全绿但用户找不到控件」翻车的根源，这里补上。
        """
        from PySide6.QtWidgets import QComboBox

        # 主窗口属性名不变，与回放页引用同一个实例
        assert window.mode_combo is window.page_library.mode_combo
        # 直接挂在回放页的控制卡上（父链：combo -> card -> page）
        assert window.mode_combo.parentWidget().parentWidget() is window.page_library
        # 录制页不再持有模式下拉框
        assert window.mode_combo not in window.page_record.findChildren(QComboBox)
        # 切模式的功能本身不受影响
        window.mode_combo.setCurrentIndex(1)
        _pump(qapp, 40)
        assert window.mode_combo.currentData() == MODE_STEP
        window.mode_combo.setCurrentIndex(0)
        _pump(qapp, 40)
        assert window.mode_combo.currentData() == MODE_TIMED


class TestLoopPlaybackGUI:
    """循环回放的界面接线与状态栏显示。"""

    def _short_events(self):
        return [ep.make_move_event(0, 10, 10),
                ep.make_move_event(60, 20, 20),
                ep.make_move_event(120, 30, 30)]

    def test_default_is_no_loop(self, window, qapp):
        assert window.page_library.loop_mode() == "none"
        assert window.config.loop_mode == "none"
        # 不循环时轮数框不可用
        assert window.page_library.spin_rounds.isEnabled() is False

    def test_fixed_rounds_enables_spin(self, window, qapp):
        combo = window.page_library.loop_combo
        combo.setCurrentIndex(combo.findData("fixed"))
        _pump(qapp, 40)
        assert window.page_library.spin_rounds.isEnabled() is True
        assert window.config.loop_mode == "fixed"

    def test_loop_setting_persists(self, window, qapp):
        combo = window.page_library.loop_combo
        combo.setCurrentIndex(combo.findData("fixed"))
        window.page_library.spin_rounds.setValue(4)
        _pump(qapp, 60)
        reloaded = Config(window.config.path)
        assert reloaded.loop_mode == "fixed"
        assert reloaded.loop_count == 4

    def test_fixed_rounds_runs_and_stops(self, window, qapp):
        """固定轮数跑完自动停止，状态栏显示轮次。"""
        _load_into_window(window, self._short_events())
        _install_null_controllers(window)
        combo = window.page_library.loop_combo
        combo.setCurrentIndex(combo.findData("fixed"))
        window.page_library.spin_rounds.setValue(3)
        _pump(qapp, 40)

        window.start_replay()
        seen_rounds = set()
        for _ in range(200):
            _pump(qapp, 30)
            seen_rounds.add(window.loop_label.text())
            if window.state is State.IDLE:
                break
        assert window.state is State.IDLE, "固定轮数应自动停止"
        assert "第 1 / 3 轮" in seen_rounds, seen_rounds
        assert window.player.loop_rounds_done == 3
        assert "共执行 3 轮" in window.message_label.text()

    def test_round_text_advances_during_loop(self, window, qapp):
        """BUG 修复回归：循环回放时「第 X / N 轮」必须实时递增。

        之前轮次文本只在状态切换时刷新（_refresh_status），回放线程
        递增 _loop_round 不会触发状态切换，轮次一直停在「第 1 / N 轮」。
        修复后由 120ms 定时器比对 loop_progress_text() 实时更新，
        状态栏与「回放与脚本」页镜像必须同步推进。
        """
        _load_into_window(window, self._short_events())
        _install_null_controllers(window)
        combo = window.page_library.loop_combo
        combo.setCurrentIndex(combo.findData("fixed"))
        window.page_library.spin_rounds.setValue(3)
        _pump(qapp, 40)

        window.start_replay()
        seen_status: set[str] = set()
        seen_mirror: set[str] = set()
        for _ in range(200):
            _pump(qapp, 30)
            seen_status.add(window.loop_label.text())
            seen_mirror.add(window.page_library.round_label.text())
            if window.state is State.IDLE:
                break
        assert window.state is State.IDLE, "固定轮数应自动停止"
        assert "第 2 / 3 轮" in seen_status, f"状态栏轮次未实时刷新：{seen_status}"
        assert "第 2 / 3 轮" in seen_mirror, f"回放页镜像轮次未实时刷新：{seen_mirror}"

    def test_infinite_runs_until_stop(self, window, qapp):
        _load_into_window(window, self._short_events())
        _install_null_controllers(window)
        combo = window.page_library.loop_combo
        combo.setCurrentIndex(combo.findData("infinite"))
        _pump(qapp, 40)

        window.start_replay()
        _pump(qapp, 800)
        assert window.state in (State.REPLAYING, State.PAUSED), "无限循环不应自己停下"
        assert "无限循环" in window.loop_label.text()
        window.stop_replay()
        _pump(qapp, 60)
        assert window.state is State.IDLE
        assert window.player.loop_rounds_done >= 2

    def test_play_button_on_scripts_page(self, window, qapp):
        _load_into_window(window, self._short_events())
        _install_null_controllers(window)
        window._refresh_status()
        _pump(qapp, 40)
        assert window.page_library.btn_play.isEnabled()
        window.page_library.btn_play.click()
        _pump(qapp, 60)
        assert window.state in (State.REPLAYING, State.PAUSED)
        window.page_library.btn_stop_play.click()
        _pump(qapp, 60)
        assert window.state is State.IDLE

    def test_pause_button_state_reflected(self, window, qapp):
        _load_into_window(window, self._short_events())
        _install_null_controllers(window)
        combo = window.page_library.loop_combo
        combo.setCurrentIndex(combo.findData("infinite"))
        window.start_replay()
        _pump(qapp, 200)
        window._toggle_pause()
        _pump(qapp, 60)
        assert window.state is State.PAUSED
        assert window.page_library.btn_play.text() == "暂停中"
        window.stop_replay()
        _pump(qapp, 60)
        assert window.page_library.btn_play.text() == "开始回放"

    def test_new_script_button_toplevel(self, window, qapp, monkeypatch):
        monkeypatch.setattr("ui.main_window.ask_name", lambda *a, **k: "顶部新建")
        assert window.btn_new_script.isEnabled()
        window.btn_new_script.click()
        _pump(qapp, 60)
        target = window.config.scripts_path() / "顶部新建.json"
        assert target.exists()
        # 建好后自动加入下拉框并选中
        names = [window.script_name_edit.itemText(i)
                 for i in range(window.script_name_edit.count())]
        assert "顶部新建" in names
        assert window.script_name() == "顶部新建"


class TestUnsavedGuards:
    """切换/新建脚本时的未保存确认，避免误丢修改。"""

    def _make_dirty(self, window, qapp):
        _load_into_window(window, _sample_events())
        window._mark_unsaved(True)
        _pump(qapp, 40)

    def test_new_script_asks_before_discard(self, window, qapp, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        self._make_dirty(window, qapp)
        calls: list = []
        monkeypatch.setattr("ui.main_window.ask_question",
                            staticmethod(lambda *a, **k: calls.append(a) or QMessageBox.Cancel))
        monkeypatch.setattr("ui.main_window.ask_name", lambda *a, **k: "不该创建")
        window.create_script()
        _pump(qapp, 40)
        assert calls, "有未保存修改时应先确认"
        assert not (window.config.scripts_path() / "不该创建.json").exists()
        assert "已取消" in window.message_label.text()

    def test_load_script_asks_before_discard(self, window, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox

        path = tmp_path / "另一个.json"
        ep.save_script(path, "另一个", _sample_events())
        self._make_dirty(window, qapp)
        calls: list = []
        monkeypatch.setattr("ui.main_window.ask_question",
                            staticmethod(lambda *a, **k: calls.append(a) or QMessageBox.Cancel))
        window.load_script_file  # noqa: B018 - 仅确认存在
        before = list(window._script_events)
        # 直接调 load_selected_script 的守卫生效路径
        assert window._confirm_discard_unsaved("加载脚本") is False
        assert calls
        assert window._script_events == before, "取消后内容不应变化"

    def test_save_choice_persists(self, window, qapp, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        self._make_dirty(window, qapp)
        window.set_script_name("未保存确认测试")
        monkeypatch.setattr("ui.main_window.ask_question",
                            staticmethod(lambda *a, **k: QMessageBox.Save))
        assert window._confirm_discard_unsaved("新建脚本") is True
        assert (window.config.scripts_path() / "未保存确认测试.json").exists()
        assert window._unsaved is False

    def test_clean_state_does_not_ask(self, window, qapp, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        _load_into_window(window, _sample_events())
        window._unsaved = False
        monkeypatch.setattr("ui.main_window.ask_question",
                            staticmethod(lambda *a, **k: pytest.fail("不应弹确认框")))
        assert window._confirm_discard_unsaved("新建脚本") is True

    def test_overwrite_confirmation_on_save(self, window, qapp, monkeypatch):
        """保存到已存在的同名脚本时要确认。"""
        from PySide6.QtWidgets import QMessageBox

        directory = window.config.scripts_path()
        existing = directory / "已存在的脚本.json"
        ep.save_script(existing, "已存在的脚本", _sample_events())

        _load_into_window(window, _sample_events())
        window._current_script = None          # 模拟「刚录完、还没关联文件」
        window.set_script_name("已存在的脚本")

        calls: list = []
        monkeypatch.setattr("ui.main_window.ask_question",
                            staticmethod(lambda *a, **k: calls.append(a) or QMessageBox.No))
        result = window.save_script()
        assert result is None, "用户拒绝覆盖时不应保存"
        assert calls, "覆盖已有脚本前应确认"
        assert "已取消保存" in window.message_label.text()

    def test_overwrite_allowed_when_confirmed(self, window, qapp, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        directory = window.config.scripts_path()
        existing = directory / "覆盖目标.json"
        ep.save_script(existing, "覆盖目标", [])

        _load_into_window(window, _sample_events())
        window._current_script = None
        window.set_script_name("覆盖目标")
        monkeypatch.setattr("ui.main_window.ask_question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        result = window.save_script()
        assert result is not None
        assert len(ep.load_script(result)["events"]) == len(_sample_events())

    def test_saving_current_script_does_not_ask(self, window, qapp, monkeypatch):
        """正常「保存修改」到当前脚本不该多问一次。"""
        from PySide6.QtWidgets import QMessageBox

        _load_into_window(window, _sample_events())
        window.set_script_name("当前脚本")
        path = window.save_script()
        assert path is not None
        monkeypatch.setattr("ui.main_window.ask_question",
                            staticmethod(lambda *a, **k: pytest.fail("不应弹确认框")))
        assert window.save_edited_steps() == path


# ===========================================================================
# 空脚本录制 / 默认名静默保存（v1.2.0 弹窗优化）
# ===========================================================================
class TestEmptyScriptFlow:
    def test_record_empty_script_does_not_prompt(self, window, qapp, monkeypatch):
        """新建空脚本（0 步）点「开始录制」不该弹「覆盖现有步骤」确认。"""
        from PySide6.QtWidgets import QMessageBox

        monkeypatch.setattr(window.recorder, "start", lambda **kwargs: True)
        monkeypatch.setattr(window.recorder, "stop", lambda: [])
        # 空脚本：无步骤，但有「已加载脚本」的文件句柄
        window._script_steps = []
        window._script_events = []
        window._current_script = window.config.scripts_path() / "空脚本.json"

        called = []
        monkeypatch.setattr("ui.main_window.ask_question",
                            staticmethod(lambda *a, **k: called.append(a)))
        window.start_recording()
        assert called == [], "空脚本开始录制不应弹覆盖确认"
        assert window.state is State.RECORDING
        window.stop_recording()
        _pump(qapp, 80)

    def test_record_nonempty_script_prompts(self, window, qapp, monkeypatch):
        """有真实步骤时开始录制仍应弹覆盖确认。"""
        monkeypatch.setattr(window.recorder, "start", lambda **kwargs: True)
        monkeypatch.setattr(window.recorder, "stop", lambda: [])
        _load_into_window(window, _sample_events())

        called = []
        monkeypatch.setattr(window, "_confirm_replace_for_record",
                            lambda: called.append(True) or False)
        window.start_recording()
        assert called, "有步骤时开始录制应弹覆盖确认"
        assert window.state is not State.RECORDING  # 用户取消后不进入录制

    def test_default_name_save_is_silent(self, window, qapp, monkeypatch):
        """默认名「录制-YYYYMMDD-HHMMSS」保存时静默覆盖，不弹确认。"""
        from PySide6.QtWidgets import QMessageBox

        _load_into_window(window, _sample_events())
        window._current_script = None
        window.set_script_name("录制-20261101-114254")

        # 预先放置同名文件，模拟同秒重复录制
        existing = window.config.scripts_path() / "录制-20261101-114254.json"
        ep.save_script(existing, "录制-20261101-114254", [])

        monkeypatch.setattr("ui.main_window.ask_question",
                            staticmethod(lambda *a, **k: pytest.fail("默认名保存不应弹窗")))
        result = window.save_script()
        assert result is not None
        assert len(ep.load_script(result)["events"]) == len(_sample_events())

    def test_custom_name_save_still_prompts(self, window, qapp, monkeypatch):
        """用户自定义名保存到已存在的同名脚本时，仍应弹确认。"""
        from PySide6.QtWidgets import QMessageBox

        directory = window.config.scripts_path()
        existing = directory / "我的脚本.json"
        ep.save_script(existing, "我的脚本", _sample_events())

        _load_into_window(window, _sample_events())
        window._current_script = None
        window.set_script_name("我的脚本")

        calls = []
        monkeypatch.setattr("ui.main_window.ask_question",
                            staticmethod(lambda *a, **k: calls.append(a) or QMessageBox.No))
        assert window.save_script() is None
        assert calls, "自定义名覆盖前应确认"

    def test_is_default_script_name(self, window):
        assert window._is_default_script_name("录制-20261101-114254") is True
        assert window._is_default_script_name("录制-20261101-114254") is True
        assert window._is_default_script_name("我的脚本") is False
        assert window._is_default_script_name("") is True
        assert window._is_default_script_name("录制-2026abc-114254") is False


# ===========================================================================
# 窗口几何持久化
# ===========================================================================
class TestWindowGeometry:
    def test_geometry_saved_on_close(self, qapp, config, monkeypatch):
        monkeypatch.setattr("ui.main_window.ask_question",
                            staticmethod(lambda *a, **k: QMessageBox.Discard))
        win = MainWindow(config)
        win.show()
        _pump(qapp, 120)
        win.setGeometry(120, 80, 880, 560)
        _pump(qapp, 60)
        actual_geo = win.geometry()
        actual_frame = win.frameGeometry()
        win.close()
        _pump(qapp, 60)
        reloaded = Config(config.path)
        # 位置记录的是 frameGeometry（move() 移动的是含边框整体），
        # 尺寸记录的是客户区 geometry（resize() 恢复的是客户区）
        assert reloaded.get("window.x") == actual_frame.x()
        assert reloaded.get("window.y") == actual_frame.y()
        assert reloaded.get("window.width") == actual_geo.width()
        assert reloaded.get("window.height") == actual_geo.height()

    def test_out_of_range_position_centered(self, qapp, config, monkeypatch):
        monkeypatch.setattr("ui.main_window.ask_question",
                            staticmethod(lambda *a, **k: QMessageBox.Discard))
        config.set("window.x", 99999)
        config.set("window.y", 99999)
        config.save()
        win = MainWindow(config)
        win.show()
        _pump(qapp, 120)
        from utils.win_utils import primary_screen_size

        screen_w, screen_h = primary_screen_size()
        assert 0 <= win.x() <= max(0, screen_w - 100)
        assert 0 <= win.y() <= max(0, screen_h - 100)
        win.close()
        _pump(qapp, 40)


# ===========================================================================
# 测试替身：不做真实注入
# ===========================================================================
class _NullMouse:
    """空鼠标控制器：只更新内部位置，不真的移动光标。"""

    def __init__(self) -> None:
        self._position = (0, 0)

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, value) -> None:
        self._position = value

    def press(self, button) -> None:
        pass

    def release(self, button) -> None:
        pass

    def scroll(self, dx, dy) -> None:
        pass

    def release_all(self) -> None:
        pass


class _NullKeyboard:
    """空键盘控制器。"""

    def press(self, key) -> None:
        pass

    def release(self, key) -> None:
        pass

    def release_all(self) -> None:
        pass


# ===========================================================================
# 续录（在已有脚本末尾追加）
# ===========================================================================
class TestAppendRecording:
    """续录：新事件接在老脚本后面，原有步骤一条不动。"""

    @staticmethod
    def _five_second_script() -> list[dict]:
        """一段 5.0 秒结束的脚本：移动 + 点击 + 末尾移动。"""
        return [
            ep.make_move_event(0, 100, 100),
            ep.make_click_event(1000, "press", "left", 100, 100),
            ep.make_click_event(1040, "release", "left", 100, 100),
            ep.make_move_event(5000, 200, 200),
        ]

    @staticmethod
    def _start_append(window, monkeypatch, offset_box: dict) -> None:
        """启动续录，并记录传给录制器的偏移量（不启动真实监听）。"""
        def fake_start(**kwargs):
            offset_box.update(kwargs)
            return True

        monkeypatch.setattr(window.recorder, "start", fake_start)
        monkeypatch.setattr(window.recorder, "stop", lambda: [])
        monkeypatch.setattr(window.poll_recorder, "start", lambda **k: False)
        monkeypatch.setattr(window.poll_recorder, "stop", lambda: [])
        window.start_recording(append=True)

    def test_button_disabled_when_script_empty(self, window):
        assert window.btn_append_record.isEnabled() is False
        assert window.btn_append_record.text() == "续录脚本"

    def test_button_enabled_after_loading_script(self, window, qapp):
        _load_into_window(window, self._five_second_script())
        _pump(qapp, 40)
        assert window.btn_append_record.isEnabled() is True

    def test_button_disabled_while_recording(self, window, qapp, monkeypatch):
        _load_into_window(window, self._five_second_script())
        _pump(qapp, 40)
        offset_box: dict = {}
        self._start_append(window, monkeypatch, offset_box)
        assert window.btn_append_record.isEnabled() is False
        assert window.btn_start_record.isEnabled() is False
        window.stop_recording()
        _pump(qapp, 60)

    def test_append_without_steps_is_rejected(self, window, qapp):
        """空脚本调用续录：不进入录制态，给出提示。"""
        window.start_recording(append=True)
        assert window.state is State.IDLE
        assert "没有步骤可续录" in window.message_label.text()

    def test_offset_is_last_end_plus_gap(self, window, qapp, monkeypatch):
        _load_into_window(window, self._five_second_script())
        _pump(qapp, 40)
        offset_box: dict = {}
        self._start_append(window, monkeypatch, offset_box)
        assert window.state is State.RECORDING
        # 5.0s 结束 + 0.5s 间隔 = 5500ms
        assert offset_box["time_offset_ms"] == ep.events_duration_ms(
            self._five_second_script()) + 500 == 5500
        window.stop_recording()
        _pump(qapp, 60)

    def test_append_result_keeps_old_steps_and_extends_time(self, window, qapp,
                                                            monkeypatch):
        """核心断言：新增步骤接在末尾，总时长与最后一步结束时间正确。"""
        original = self._five_second_script()
        _load_into_window(window, original)
        _pump(qapp, 40)
        window._current_script = Path("已有脚本.json")
        old_steps = len(window._script_steps)

        offset_box: dict = {}
        self._start_append(window, monkeypatch, offset_box)
        offset = offset_box["time_offset_ms"]

        # 续录开始 1 秒后点了一下（按用户给的例子：5.0 + 0.5 + 1.0 = 6.5s）
        new_events = [
            ep.make_move_event(offset + 1000, 300, 300),
            ep.make_click_event(offset + 1100, "press", "left", 300, 300),
            ep.make_click_event(offset + 1140, "release", "left", 300, 300),
        ]
        assert new_events[0]["t"] == 6500
        for evt in new_events:
            window._on_recorder_event(evt)

        window.stop_recording()
        _pump(qapp, 80)

        assert window.state is State.IDLE
        # 原有 3 步 + 新增 1 个点击步；新增的移动事件与末尾那个移动步是
        # 连续 move，按既有合并规则并进同一步（轨迹步），所以只多 1 步
        assert len(window._script_steps) == old_steps + 1
        # 新增的点击确实独立成步，且落在末尾
        assert window._script_steps[-1].kind == "click"
        assert "300, 300" in window._script_steps[-1].description
        # 老事件一条都没被改
        for index, evt in enumerate(original):
            assert window._script_events[index] == evt
        # 新事件接在末尾，时间戳连续
        assert len(window._script_events) == len(original) + 3
        assert window._script_events[-1]["t"] == 6640
        assert ep.events_duration_ms(window._script_events) == 6640
        # 最后一步的结束时间 = 新录的释放事件
        assert window._script_steps[-1].events[-1]["t"] == 6640
        # 未保存标记 + 脚本路径保留（保存时覆盖原脚本）
        assert window.is_dirty is True
        assert window._current_script == Path("已有脚本.json")
        assert "续录完成" in window.message_label.text()

    def test_append_reports_new_event_count(self, window, qapp, monkeypatch):
        _load_into_window(window, self._five_second_script())
        _pump(qapp, 40)
        offset_box: dict = {}
        self._start_append(window, monkeypatch, offset_box)
        for evt in (ep.make_move_event(offset_box["time_offset_ms"] + 100, 5, 5),
                    ep.make_move_event(offset_box["time_offset_ms"] + 200, 6, 6)):
            window._on_recorder_event(evt)
        window.stop_recording()
        _pump(qapp, 80)
        assert "新增 2 条事件" in window.message_label.text()

    def test_append_supports_pause_and_stop(self, window, qapp, monkeypatch):
        """续录同样支持 F11 暂停 / F9 停止。"""
        _load_into_window(window, self._five_second_script())
        _pump(qapp, 40)
        offset_box: dict = {}
        self._start_append(window, monkeypatch, offset_box)

        window._on_hotkey_action("record_pause")
        assert window.state is State.RECORD_PAUSED
        window._on_hotkey_action("record_pause")
        assert window.state is State.RECORDING
        window._on_hotkey_action("record_stop")
        _pump(qapp, 80)
        assert window.state is State.IDLE

    def test_append_mid_recording_does_not_clear_list(self, window, qapp, monkeypatch):
        """续录期间列表里应同时看到旧步骤，而不是被清空重录。"""
        _load_into_window(window, self._five_second_script())
        _pump(qapp, 40)
        old_steps = len(window._script_steps)
        offset_box: dict = {}
        self._start_append(window, monkeypatch, offset_box)
        assert len(window._script_steps) == old_steps
        assert len(window.event_list.checked_indices()) == 0
        window.stop_recording()
        _pump(qapp, 60)

    def test_fresh_recording_still_clears(self, window, qapp, monkeypatch):
        """回归：全新录制仍然会清空旧步骤（续录不得改变原有语义）。"""
        _load_into_window(window, self._five_second_script())
        _pump(qapp, 40)
        monkeypatch.setattr(window.recorder, "start", lambda **kwargs: True)
        monkeypatch.setattr(window.recorder, "stop", lambda: [])
        monkeypatch.setattr(window.poll_recorder, "start", lambda **k: False)
        monkeypatch.setattr(window.poll_recorder, "stop", lambda: [])
        # 覆盖确认弹窗是真实 QMessageBox 实例（clickedButton 判定），
        # 离屏环境点不了按钮，这里直接放过确认步骤
        monkeypatch.setattr(window, "_confirm_replace_for_record",
                            lambda: True)

        window.start_recording()          # 全新录制
        assert window.state is State.RECORDING
        assert window._script_steps == []
        assert window._script_events == []
        window.stop_recording()
        _pump(qapp, 60)


# ===========================================================================
# 批量选择：按类型全选 + 鼠标框选
# ===========================================================================
class TestBatchSelection:
    """「全选此类型」与「按住左键框选」两种批量勾选方式。

    四种交互（框选 / 按类型全选 / 单击 / 点空白）互不打架，是本类的核心断言。
    """

    def _prepare(self, window, qapp):
        _load_into_window(window, _sample_events())
        _install_null_controllers(window)
        _pump(qapp, 60)
        return window.event_list

    @staticmethod
    def _rows_of_kind(editor, kind: str) -> list[int]:
        return [i for i, step in enumerate(editor.steps) if step.kind == kind]

    def _drag_rows(self, editor, first: int, last: int) -> None:
        """从 first 行拖到 last 行（真实鼠标事件，走 viewport 事件过滤器）。"""
        from ui.event_list import COL_DESC

        table = editor.table
        start = table.visualItemRect(table.item(first, COL_DESC)).center()
        end = table.visualItemRect(table.item(last, COL_DESC)).center()
        QTest.mousePress(table.viewport(), Qt.LeftButton, Qt.NoModifier, start)
        QTest.mouseMove(table.viewport(), start + QPoint(0, 10))
        QTest.mouseMove(table.viewport(), end)
        QTest.mouseRelease(table.viewport(), Qt.LeftButton, Qt.NoModifier, end)

    # ---------------- 任务 1：按类型全选 ----------------
    def test_kind_combo_lists_all_step_kinds(self, window, qapp):
        """下拉里六种类型齐全，且每项都带 kind 数据。"""
        from ui.event_list import KIND_CHOICES

        editor = self._prepare(window, qapp)
        combo = editor.kind_combo
        assert combo.count() == len(KIND_CHOICES) == 6
        assert [combo.itemData(i) for i in range(combo.count())] == \
            [kind for kind, _ in KIND_CHOICES]
        assert combo.itemText(0) == "鼠标移动"

    def test_select_by_kind_checks_only_that_kind(self, window, qapp):
        """先清空再全选：只勾中目标类型，别的类型一个都不留。"""
        editor = self._prepare(window, qapp)
        clicks = self._rows_of_kind(editor, "click")
        assert clicks, "样例事件里应该有鼠标点击步骤"
        editor.set_checked([0])                     # 先随便勾一个别的类型
        assert 0 not in clicks

        editor.kind_combo.setCurrentIndex(1)        # 鼠标点击
        editor.select_by_kind()
        _pump(qapp, 40)
        assert editor.checked_indices() == clicks
        assert "已按类型全选" in window.message_label.text()
        assert "鼠标点击" in window.message_label.text()

    def test_select_by_kind_enables_batch_buttons(self, window, qapp):
        """勾上之后「删除 / 编辑这一步」必须变可用，否则批量操作无从下手。"""
        editor = self._prepare(window, qapp)
        assert not editor.btn_delete.isEnabled()
        editor.kind_combo.setCurrentIndex(0)        # 鼠标移动
        editor.select_by_kind()
        _pump(qapp, 40)
        assert editor.checked_count > 0
        assert editor.btn_delete.isEnabled()
        assert editor.btn_edit_step.isEnabled()
        assert "已选" in editor.selection_label.text()

    def test_select_by_kind_without_matches_hints(self, window, qapp):
        """选了个列表里没有的类型：勾选为空 + 给一句提示，不能静默无反应。"""
        editor = self._prepare(window, qapp)
        editor.set_checked([0])
        index = editor.kind_combo.findData("multi")   # 样例里没有连击
        editor.kind_combo.setCurrentIndex(index)
        editor.select_by_kind()
        _pump(qapp, 40)
        assert editor.checked_count == 0
        assert not editor.btn_delete.isEnabled()
        assert "没有" in window.message_label.text()

    # ---------------- 任务 2：鼠标框选 ----------------
    def test_table_uses_row_extended_selection(self, window, qapp):
        """表格必须开着「整行 + 扩展选择」，否则橡皮筋拖动根本不会出现。"""
        from PySide6.QtWidgets import QAbstractItemView

        editor = self._prepare(window, qapp)
        assert editor.table.selectionBehavior() == QAbstractItemView.SelectRows
        assert editor.table.selectionMode() == QAbstractItemView.ExtendedSelection

    def test_drag_select_checks_every_touched_row(self, window, qapp):
        """框选 0~2 行：高亮 + 复选框同步勾选 + 状态栏报数量。"""
        editor = self._prepare(window, qapp)
        editor.clear_selection()
        assert editor.checked_count == 0

        self._drag_rows(editor, 0, 2)
        _pump(qapp, 40)
        assert editor.selected_rows() == [0, 1, 2]
        assert editor.checked_indices() == [0, 1, 2]
        assert editor.btn_delete.isEnabled()
        assert "框选 3 步" in window.message_label.text()

    def test_drag_select_replaces_previous_checks(self, window, qapp):
        """框选是「以框为准」：上一次的勾选不会残留，避免误删。"""
        editor = self._prepare(window, qapp)
        editor.set_checked([4])
        self._drag_rows(editor, 0, 1)
        _pump(qapp, 40)
        assert editor.checked_indices() == [0, 1]

    def test_single_click_clears_checks_and_highlight(self, window, qapp):
        """框选之后再单击行身：勾选与高亮一并清掉（老逻辑不能丢）。"""
        from ui.event_list import COL_DESC

        editor = self._prepare(window, qapp)
        self._drag_rows(editor, 0, 2)
        _pump(qapp, 40)
        assert editor.checked_indices() == [0, 1, 2]

        editor._on_cell_clicked(1, COL_DESC)
        _pump(qapp, 40)
        assert editor.checked_indices() == []
        assert editor.selected_rows() == []
        assert not editor.btn_delete.isEnabled()

    def test_drag_end_does_not_swallow_next_click(self, window, qapp):
        """框选收尾时视图可能补发一次 clicked——不能把用户的下一次点击吃掉。"""
        from ui.event_list import COL_DESC

        editor = self._prepare(window, qapp)
        self._drag_rows(editor, 0, 2)
        _pump(qapp, 60)          # 让拖选收尾的 0ms 定时器跑完
        editor._on_cell_clicked(1, COL_DESC)
        _pump(qapp, 40)
        assert editor.checked_indices() == []
        # 再来一次单击：如果上一次被"拖选守卫"吞了，这里就还原不了任何状态
        editor.set_checked([2])
        editor._on_cell_clicked(0, COL_DESC)
        _pump(qapp, 40)
        assert editor.checked_indices() == []

    def test_blank_click_clears_checks_and_highlight(self, window, qapp):
        """点列表空白处：高亮解除 + 复选框全部取消（保持原有逻辑）。"""
        editor = self._prepare(window, qapp)
        self._drag_rows(editor, 0, 1)
        _pump(qapp, 40)
        assert editor.checked_count == 2

        table = editor.table
        blank = QPoint(40, table.viewport().height() - 6)
        QTest.mousePress(table.viewport(), Qt.LeftButton, Qt.NoModifier, blank)
        QTest.mouseRelease(table.viewport(), Qt.LeftButton, Qt.NoModifier, blank)
        _pump(qapp, 40)
        assert editor.checked_indices() == []
        assert editor.selected_rows() == []

    def test_keyboard_space_toggles_row_without_stray_highlight(self, window, qapp):
        """表格拿焦点后：↓ 只移焦点（不留蓝色选中行），空格切换该行勾选。"""
        editor = self._prepare(window, qapp)
        editor.clear_selection()
        table = editor.table
        table.setFocus()

        QTest.keyClick(table, Qt.Key_Down)
        _pump(qapp, 30)
        assert editor.selected_rows() == []
        assert editor._focus_row >= 0

        row = editor._focus_row
        QTest.keyClick(table, Qt.Key_Space)
        _pump(qapp, 30)
        assert editor.checked_indices() == [row]


# ===========================================================================
# 开始录制热键（默认 F12）
# ===========================================================================
class TestRecordStartHotkeyGUI:
    """F12 = 开始录制：空闲生效、录制/回放中静默挡掉、本身不进脚本。"""

    def _stub_recorder(self, window, monkeypatch) -> list:
        """打桩录制器，返回「start 被调用了几次」的计数列表。

        不能靠状态栏文字判断有没有重复启动——录制中的 120ms 刷新定时器会
        不停改写那条消息，断言文字必然是脆的。
        """
        calls: list[dict] = []

        def fake_start(**kwargs):
            calls.append(kwargs)
            return True

        monkeypatch.setattr(window.recorder, "start", fake_start)
        monkeypatch.setattr(window.recorder, "stop", lambda: [])
        monkeypatch.setattr(window.poll_recorder, "start", lambda **kwargs: False)
        monkeypatch.setattr(window.poll_recorder, "stop", lambda: [])
        return calls

    def test_hotkey_starts_recording(self, window, qapp, monkeypatch):
        calls = self._stub_recorder(window, monkeypatch)
        assert window.state is State.IDLE
        window._on_hotkey_action("record_start")
        assert window.state is State.RECORDING
        assert len(calls) == 1
        # 开始录制的提示只在按下瞬间有效（随后会被 120ms 刷新覆盖）
        assert "正在录制" in window.message_label.text()
        window.stop_recording()
        _pump(qapp, 60)
        assert window.state is State.IDLE

    def test_hotkey_ignored_while_recording(self, window, qapp, monkeypatch):
        """录制中再按 F12 不能重开一段（那会清掉刚录的内容）。"""
        calls = self._stub_recorder(window, monkeypatch)
        window._on_hotkey_action("record_start")
        for event in _sample_events():
            window.sig_recorded.emit(event)
        _pump(qapp, 200)
        before = len(window._script_events)

        window._on_hotkey_action("record_start")
        _pump(qapp, 20)
        assert window.state is State.RECORDING
        assert len(calls) == 1, "录制中再按 F12 不许重开一段（会清掉刚录的内容）"
        assert len(window._script_events) >= before

        window.stop_recording()
        _pump(qapp, 60)

    def test_hotkey_ignored_while_replaying(self, window, qapp, monkeypatch):
        """回放途中按 F12 不抢状态机。"""
        calls = self._stub_recorder(window, monkeypatch)
        _load_into_window(window, _sample_events())
        _install_null_controllers(window)
        window.start_replay()
        _pump(qapp, 40)
        assert window.state in (State.REPLAYING, State.PAUSED)

        window._on_hotkey_action("record_start")
        _pump(qapp, 20)
        assert window.state in (State.REPLAYING, State.PAUSED)
        assert calls == [], "回放中按 F12 绝不能启动录制"
        window.stop_replay()
        _pump(qapp, 60)

    def test_start_key_never_recorded(self, window, qapp, monkeypatch):
        """F12 必须进「永不录制」名单（token 与 vk 两种写法都要有）。"""
        self._stub_recorder(window, monkeypatch)
        window._on_hotkey_action("record_start")
        ignored = window.recorder.ignored_keys
        assert "f12" in ignored
        assert "vk:123" in ignored
        window.stop_recording()
        _pump(qapp, 60)

    def test_start_key_follows_rebinding(self, window, qapp, monkeypatch):
        """用户把开始键改成 F5 后，忽略名单要跟着变，别把 F5 录进脚本。"""
        self._stub_recorder(window, monkeypatch)
        window.config.set("hotkeys.record_start", "f5")
        window._on_hotkey_action("record_start")
        ignored = window.recorder.ignored_keys
        assert "f5" in ignored
        assert "vk:116" in ignored
        assert "f12" not in ignored
        window.stop_recording()
        _pump(qapp, 60)


# ===========================================================================
# 任务栏状态徽标（ui/state_icon.py）
# ===========================================================================
class TestStateIcon:
    """窗口图标按状态叠角标：录制红点 / 暂停琥珀双竖条 / 回放绿三角。

    为什么要按颜色断言而不是「图标对象变了」了事：徽标是画在像素上的，
    绘制代码一旦抛异常就会被 ``_apply_state_icon`` 的 try/except 吞掉，
    窗口图标静默停留在上一张——看起来"什么都没变"，肉眼很难发现。
    这里直接数像素，能把这个坑钉死（历史上 ``drawPolygon`` 就踩过一次）。
    """

    #: 期望的徽标底色（写死字面量，不引用常量，才测得出"配色被改错"）
    REC_COLOR = "#e5484d"        # 录制红
    PAUSE_COLOR = "#f0a22e"      # 录制暂停：琥珀
    PLAY_COLOR = "#2fa36b"       # 回放中：绿（刻意不用强调蓝，图标本身是蓝底）
    PLAY_PAUSE_COLOR = "#1f7a57"  # 回放暂停：暗绿（保留"回放"语义，压暗表示停着）

    @staticmethod
    def _image(window, size: int = 48):
        """取窗口当前图标在指定尺寸下的位图。"""
        return window.windowIcon().pixmap(size, size).toImage()

    @staticmethod
    def _count(image, color: str, tol: int = 8) -> int:
        """统计接近目标颜色的像素个数。"""
        from PySide6.QtGui import QColor

        target = QColor(color)
        hits = 0
        for y in range(image.height()):
            for x in range(image.width()):
                c = image.pixelColor(x, y)
                if (abs(c.red() - target.red()) <= tol
                        and abs(c.green() - target.green()) <= tol
                        and abs(c.blue() - target.blue()) <= tol):
                    hits += 1
        return hits

    # -- 纯函数层 ---------------------------------------------------------
    def test_replaying_badge_actually_painted(self, qapp):
        """回归：回放徽标必须真的画出来（曾因 drawPolygon 抛 TypeError 而静默失效）。"""
        from utils.win_utils import load_app_icon
        from ui.state_icon import state_icon

        base = load_app_icon()
        assert base is not None and not base.isNull()

        icon = state_icon(base, "replaying")
        assert icon is not None
        image = icon.pixmap(48, 48).toImage()
        assert self._count(image, self.PLAY_COLOR) >= 40, "回放徽标没画出来"

    @pytest.mark.parametrize("state,color", [
        ("recording", REC_COLOR),
        ("record_paused", PAUSE_COLOR),
        ("replaying", PLAY_COLOR),
        ("paused", PLAY_PAUSE_COLOR),
    ])
    def test_each_badge_uses_its_own_color(self, qapp, state, color):
        """四种徽标各自的底色必须真的出现在图标上。"""
        from utils.win_utils import load_app_icon
        from ui.state_icon import state_icon

        image = state_icon(load_app_icon(), state).pixmap(48, 48).toImage()
        assert self._count(image, color) >= 40

        # 其余三种颜色都不该出现，避免"画错色"蒙混过关
        others = {self.REC_COLOR, self.PAUSE_COLOR,
                  self.PLAY_COLOR, self.PLAY_PAUSE_COLOR} - {color}
        for other in others:
            assert self._count(image, other) == 0, f"{state} 上混进了 {other}"

    def test_idle_and_unknown_state_keep_base_icon(self, qapp):
        """空闲 / 未知状态不叠任何徽标，原图标原样返回。"""
        from utils.win_utils import load_app_icon
        from ui.state_icon import state_icon

        base = load_app_icon()
        assert state_icon(base, "") is base
        assert state_icon(base, "idle") is base

    def test_none_base_is_tolerated(self, qapp):
        """没有基图标时返回 None，不抛异常（图标文件缺失也不该崩）。"""
        from ui.state_icon import state_icon

        assert state_icon(None, "recording") is None

    def test_cache_reuses_same_icon(self, qapp):
        """同一状态重复取图标应命中缓存（否则每次状态切换都重画 7 张位图）。"""
        from utils.win_utils import load_app_icon
        from ui.state_icon import state_icon

        base = load_app_icon()
        assert state_icon(base, "recording") is state_icon(base, "recording")

    def test_two_pause_states_are_distinguishable(self, qapp):
        """录制暂停与回放暂停必须长得不一样——最小化后要能分清停的是什么。"""
        from utils.win_utils import load_app_icon
        from ui.state_icon import state_icon

        base = load_app_icon()
        rec_pause = state_icon(base, "record_paused").pixmap(48, 48).toImage()
        play_pause = state_icon(base, "paused").pixmap(48, 48).toImage()
        replay = state_icon(base, "replaying").pixmap(48, 48).toImage()

        assert rec_pause != play_pause, "两种暂停共用一套配色，用户分不清"
        assert replay != play_pause

    def test_all_sizes_are_available(self, qapp):
        """16/24/32/48 都要有位图，任务栏大小图标才不会糊。"""
        from utils.win_utils import load_app_icon
        from ui.state_icon import state_icon

        icon = state_icon(load_app_icon(), "recording")
        for size in (16, 24, 32, 48):
            assert not icon.pixmap(size, size).isNull()
        # 16px 上也得真的画得下（像素太小也别丢整块徽标）
        small = icon.pixmap(16, 16).toImage()
        assert self._count(small, self.REC_COLOR, tol=28) >= 8

    # -- 窗口接线层 -------------------------------------------------------
    @pytest.mark.parametrize("state,color", [
        (State.RECORDING, REC_COLOR),
        (State.RECORD_PAUSED, PAUSE_COLOR),
        (State.REPLAYING, PLAY_COLOR),
        (State.PAUSED, PLAY_PAUSE_COLOR),
    ])
    def test_set_state_swaps_taskbar_icon(self, window, qapp, state, color):
        """切换状态时窗口图标要跟着换（任务栏按钮与 Alt+Tab 都会变）。"""
        from ui.state_icon import clear_cache

        clear_cache()
        window._set_state(State.IDLE)
        _pump(qapp, 20)
        assert self._count(self._image(window), color) == 0

        window._set_state(state)
        _pump(qapp, 20)
        assert self._count(self._image(window), color) >= 40, f"{state} 没换上徽标"

    def test_state_change_never_touches_title(self, window, qapp):
        """徽标只换图标，窗口标题必须始终是「宏匠」。"""
        for state in (State.IDLE, State.RECORDING, State.RECORD_PAUSED,
                      State.REPLAYING, State.PAUSED):
            window._set_state(state)
            _pump(qapp, 10)
            assert window.windowTitle() == WINDOW_TITLE
        window._set_state(State.IDLE)

    def test_back_to_idle_restores_plain_icon(self, window, qapp):
        """回到空闲要把徽标去掉，不能留着上一状态的残影。"""
        window._set_state(State.RECORDING)
        _pump(qapp, 10)
        assert self._count(self._image(window), self.REC_COLOR) >= 40

        window._set_state(State.IDLE)
        _pump(qapp, 10)
        assert self._count(self._image(window), self.REC_COLOR) == 0

    def test_missing_base_icon_is_harmless(self, window, qapp):
        """拿不到基图标时状态切换照常进行（不抛异常、不影响状态机）。"""
        window._base_icon = None
        window._set_state(State.REPLAYING)
        _pump(qapp, 10)
        assert window.state is State.REPLAYING
        window._set_state(State.IDLE)


# ===========================================================================
# 状态提示音（core/sound.py 与状态机的接线）
# ===========================================================================
class TestStateCues:
    """状态迁移必须触发对应的提示音，且停止 / 空转不发声。

    这里把 ``core.sound.play`` 打桩成记账，只验"该响哪一个"，
    真发声（好不好听、会不会破音）只能人耳验收。
    """

    @staticmethod
    def _record_calls(monkeypatch) -> list[str]:
        calls: list[str] = []

        def fake_play(cue: str) -> bool:
            calls.append(cue)
            return True

        monkeypatch.setattr("core.sound.play", fake_play)
        return calls

    @pytest.mark.parametrize("previous,state,cue", [
        (State.IDLE, State.RECORDING, "record_start"),
        (State.RECORDING, State.RECORD_PAUSED, "record_pause"),
        (State.RECORD_PAUSED, State.RECORDING, "record_resume"),
        (State.IDLE, State.REPLAYING, "replay_start"),
        (State.REPLAYING, State.PAUSED, "replay_pause"),
        (State.PAUSED, State.REPLAYING, "replay_resume"),
    ])
    def test_transition_plays_expected_cue(self, window, qapp, monkeypatch,
                                           previous, state, cue):
        calls = self._record_calls(monkeypatch)
        window._set_state(previous)
        _pump(qapp, 10)
        calls.clear()

        window._set_state(state)
        _pump(qapp, 10)
        assert calls == [cue]
        window._set_state(State.IDLE)

    def test_stop_record_is_silent(self, window, qapp, monkeypatch):
        """停止录制不发声：那是用户主动按下的确定动作。

        停止回放**会**响（见 TestMoreCues）——回放可能是自然播完的，
        用户此刻常在看别处。两者取舍不同，别一起改。
        """
        calls = self._record_calls(monkeypatch)
        window._set_state(State.RECORDING)
        _pump(qapp, 10)
        calls.clear()

        window._set_state(State.IDLE)
        _pump(qapp, 10)
        assert calls == []

    def test_same_state_twice_is_silent(self, window, qapp, monkeypatch):
        """状态原地不动不能再响（否则误按热键会一直滴滴叫）。"""
        calls = self._record_calls(monkeypatch)
        window._set_state(State.RECORDING)
        _pump(qapp, 10)
        calls.clear()

        window._set_state(State.RECORDING)
        _pump(qapp, 10)
        assert calls == []
        window._set_state(State.IDLE)

    def test_disabled_in_config_is_silent(self, window, qapp, monkeypatch):
        """设置页关掉提示音后，所有状态迁移都不出声。"""
        calls = self._record_calls(monkeypatch)
        window.config.set("sound.enabled", False)

        for state in (State.RECORDING, State.RECORD_PAUSED, State.IDLE,
                      State.REPLAYING, State.PAUSED, State.IDLE):
            window._set_state(state)
            _pump(qapp, 10)
        assert calls == []

    def test_sound_defaults_on(self, config):
        """默认开启（老配置文件深合并后也要有这一项）。"""
        assert config.get("sound.enabled", None) is True

    def test_settings_checkbox_persists(self, window, qapp):
        """设置页的「状态提示音」开关能关掉并写回配置。"""
        page = window.page_settings
        assert page.chk_sound.isChecked() is True

        page.chk_sound.setChecked(False)
        page._on_save()
        _pump(qapp, 20)
        assert window.config.get("sound.enabled") is False
        assert page.chk_sound.isChecked() is False

        # 复位，避免影响后续用例（各用例配置独立，这里只是稳妥）
        page.chk_sound.setChecked(True)
        page._on_save()
        _pump(qapp, 20)
        assert window.config.get("sound.enabled") is True


# ===========================================================================
# 停止回放 / 单步上下步 的提示音
# ===========================================================================
class TestMoreCues:
    """补上「回放结束」与「模式二上下步」，以及单步那个独立开关。"""

    @staticmethod
    def _record_calls(monkeypatch) -> list[str]:
        calls: list[str] = []

        def fake_play(cue: str) -> bool:
            calls.append(cue)
            return True

        monkeypatch.setattr("core.sound.play", fake_play)
        return calls

    @pytest.mark.parametrize("previous", [State.REPLAYING, State.PAUSED])
    def test_replay_stop_cues(self, window, qapp, monkeypatch, previous):
        """停止回放（含暂停中停止）要响一声「结束」。"""
        calls = self._record_calls(monkeypatch)
        window._set_state(previous)
        _pump(qapp, 10)
        calls.clear()

        window._set_state(State.IDLE)
        _pump(qapp, 10)
        assert calls == ["replay_stop"]

    def test_stop_record_still_silent(self, window, qapp, monkeypatch):
        """停止录制仍然不发声——只加了停止回放。"""
        calls = self._record_calls(monkeypatch)
        window._set_state(State.RECORDING)
        _pump(qapp, 10)
        calls.clear()

        window._set_state(State.IDLE)
        _pump(qapp, 10)
        assert calls == []

    def _load_two_steps(self, window) -> None:
        """装两个最简单的步骤，供单步执行用。"""
        events = [
            ep.make_move_event(0, 100, 100),
            ep.make_move_event(120, 200, 200),
            ep.make_click_event(240, 200, 200),
        ]
        _install_null_controllers(window)
        _load_into_window(window, events)
        _pump(window.parent() and QApplication.instance(), 30)

    def test_step_next_cues(self, window, qapp, monkeypatch):
        """模式二按下一步要响 step_next。"""
        calls = self._record_calls(monkeypatch)
        _install_null_controllers(window)
        _load_into_window(window, _sample_events())
        _pump(qapp, 30)
        calls.clear()

        window.step_next()
        _pump(qapp, 30)
        assert calls == ["step_next"]
        assert window.state not in (State.RECORDING, State.RECORD_PAUSED)

    def test_step_prev_cues(self, window, qapp, monkeypatch):
        """模式二按上一步要响 step_prev。"""
        calls = self._record_calls(monkeypatch)
        _install_null_controllers(window)
        _load_into_window(window, _sample_events())
        _pump(qapp, 30)
        window.step_next()                 # 先走一步，才谈得上"回退"
        _pump(qapp, 30)
        calls.clear()

        window.step_prev()
        _pump(qapp, 30)
        assert calls == ["step_prev"]

    def test_step_cue_silent_when_no_steps(self, window, qapp, monkeypatch):
        """没有步骤时按单步不该响（什么都没执行，响就是误导）。"""
        calls = self._record_calls(monkeypatch)
        window.step_next()
        _pump(qapp, 10)
        window.step_prev()
        _pump(qapp, 10)
        assert calls == []

    def test_step_switch_off_silences_only_steps(self, window, qapp, monkeypatch):
        """关掉单步开关：上下步不响，但状态提示音照常。"""
        calls = self._record_calls(monkeypatch)
        window.config.set("sound.step_enabled", False)
        _install_null_controllers(window)
        _load_into_window(window, _sample_events())
        _pump(qapp, 30)
        calls.clear()

        window.step_next()
        _pump(qapp, 30)
        assert calls == []

        window._set_state(State.REPLAYING)
        _pump(qapp, 10)
        assert calls == ["replay_start"]
        window._set_state(State.IDLE)

    def test_master_switch_off_silences_steps_too(self, window, qapp, monkeypatch):
        """总开关关掉后，单步提示音也不能漏出来。"""
        calls = self._record_calls(monkeypatch)
        window.config.set("sound.enabled", False)
        _install_null_controllers(window)
        _load_into_window(window, _sample_events())
        _pump(qapp, 30)
        calls.clear()

        window.step_next()
        _pump(qapp, 30)
        assert calls == []

    def test_step_sound_checkbox_persists(self, window, qapp):
        """设置页的单步开关能写回配置，并跟随总开关灰显。"""
        page = window.page_settings
        assert page.chk_step_sound.isChecked() is True
        assert page.chk_step_sound.isEnabled() is True

        page.chk_step_sound.setChecked(False)
        page._on_save()
        _pump(qapp, 20)
        assert window.config.get("sound.step_enabled") is False

        # 总开关关掉 → 子选项灰掉（勾着却没声音会让人困惑）
        page.chk_sound.setChecked(False)
        _pump(qapp, 10)
        assert page.chk_step_sound.isEnabled() is False

        page.chk_sound.setChecked(True)
        page.chk_step_sound.setChecked(True)
        page._on_save()
        _pump(qapp, 20)
        assert page.chk_step_sound.isEnabled() is True
        assert window.config.get("sound.step_enabled") is True
        assert window.config.get("sound.enabled") is True


# ===========================================================================
# 回放 ↔ 单步 的位置接力
# ===========================================================================
class TestHandoffBetweenReplayAndStep:
    """「回放跑几步 → 暂停 → 按单步」接着走，「单步几步 → 继续回放」接着放。

    旧实现切模式前直接 ``stop_replay()``，而 ``stop()`` 把执行指针归零，
    于是暂停后按单步永远从第一步重来（用户 2026-11-11 报告的 bug）。
    现在 ``_enter_step_mode`` 先对齐再带指针停止，两个指针可以互相换算。
    """

    @staticmethod
    def _five_steps(window) -> None:
        """5 个独立点击步骤，raw 区间 (0,2)(2,4)(4,6)(6,8)(8,10)。

        间隔必须大于 ``MULTI_CLICK_GAP_MS``，否则会被并成「连击」一步。
        """
        events: list[dict] = []
        for i in range(5):
            t = i * 300
            x = 10 + i * 40
            events.append(ep.make_click_event(t, "press", "left", x, 10))
            events.append(ep.make_click_event(t + 40, "release", "left", x, 10))
        _install_null_controllers(window)
        _load_into_window(window, events)
        assert len(window.player.steps) == 5

    @staticmethod
    def _record_calls(monkeypatch) -> list[str]:
        calls: list[str] = []
        monkeypatch.setattr("core.sound.play",
                            lambda cue: calls.append(cue) or True)
        return calls

    def test_paused_then_step_continues_from_pause_point(self, window, qapp):
        """暂停在第 3 步中间（事件 5）→ 下一步应跑第 3 步，而不是第 1 步。"""
        self._five_steps(window)
        window.player._position = 5           # 第 3 步（index 2）跑到一半
        window._set_state(State.PAUSED)
        _pump(qapp, 20)

        window.step_next()
        _pump(qapp, 30)

        # 半步算未完成 → 对齐到 2，执行完这一步后指针 = 3
        assert window.player.step_index == 3, "暂停后单步必须接着走，不能从第一步重来"
        # 执行的是第 3 步本身（不是第 1 步的描述）
        assert window.message_label.text() == (
            f"已执行步骤 3：{window._script_steps[2].description}")

    def test_paused_at_boundary_steps_to_next(self, window, qapp):
        """正好跑完第 2 步（事件 4）→ 下一步是第 3 步。"""
        self._five_steps(window)
        window.player._position = 4
        window._set_state(State.PAUSED)
        _pump(qapp, 20)

        window.step_next()
        _pump(qapp, 30)
        assert window.player.step_index == 3
        assert window.mode_combo.currentData() == MODE_STEP

    def test_handoff_message_announces_resume_point(self, window, qapp):
        """切到单步时要说清「接着第几步」，否则用户以为又从头了。"""
        self._five_steps(window)
        window.player._position = 6           # 跑完第 3 步
        window._set_state(State.REPLAYING)
        _pump(qapp, 20)

        window._enter_step_mode()
        _pump(qapp, 20)
        assert window.player.step_index == 3
        text = window.message_label.text()
        assert "接着第 4 步开始" in text
        assert "3 步已经跑过" in text

    def test_mode_switch_does_not_play_replay_stop(self, window, qapp, monkeypatch):
        """回放暂停 → 切单步属于模式切换，不该响「回放结束」音。"""
        calls = self._record_calls(monkeypatch)
        self._five_steps(window)
        window.player._position = 4
        window._set_state(State.PAUSED)
        _pump(qapp, 20)
        calls.clear()

        window.step_next()
        _pump(qapp, 30)
        assert "replay_stop" not in calls
        assert calls == ["step_next"]

    def test_steps_then_replay_resumes_from_next_step(self, window, qapp):
        """单步 2 步后按 F6：从第 3 步对应的原始事件接着放。"""
        self._five_steps(window)
        window.step_next()
        _pump(qapp, 20)
        window.step_next()
        _pump(qapp, 20)
        assert window.player.step_index == 2

        window._on_hotkey_action("play_toggle")       # 等价于 F6
        _pump(qapp, 40)
        assert window.mode_combo.currentData() == MODE_TIMED
        assert window.player.position == 4, "应从第 3 步（事件 4）接着放"
        assert "接着回放" in window.message_label.text()
        window.stop_replay()
        _pump(qapp, 40)

    def test_replay_from_step_end_loops_from_start(self, window, qapp):
        """指针停在末尾时「继续回放」= 从头再放一轮。"""
        self._five_steps(window)
        for _ in range(5):
            window.step_next()
            _pump(qapp, 15)
        assert window.player.step_index == 5

        window._on_hotkey_action("play_toggle")
        _pump(qapp, 40)
        assert window.player.position == 0
        assert "接着回放" not in window.message_label.text()
        window.stop_replay()
        _pump(qapp, 40)

    def test_stop_replay_still_zeroes_pointer(self, window, qapp):
        """用户主动停止保持老行为：指针归零，下次从头开始。"""
        self._five_steps(window)
        window.step_next()
        _pump(qapp, 20)
        window._set_state(State.REPLAYING)
        _pump(qapp, 10)

        window.stop_replay()
        _pump(qapp, 30)
        assert window.player.step_index == 0
        assert window.player.position == 0

    def test_finished_replay_zeroes_pointer(self, window, qapp):
        """自然播完后指针收回开头，不能沿用上一轮单步留下的位置。"""
        self._five_steps(window)
        window.step_next()
        window.step_next()
        _pump(qapp, 20)
        assert window.player.step_index == 2

        window._set_state(State.REPLAYING)
        window._on_player_finished("finished")
        _pump(qapp, 30)
        assert window.state is State.IDLE
        assert window.player.step_index == 0

    def test_real_replay_pause_then_step(self, window, qapp):
        """真线程回归：无限循环回放跑到中途暂停，再单步必须往前走。"""
        # 落点拉开到 40px（远超连击容差），保证是 5 个独立步骤而不是被并成一步
        events: list[dict] = []
        for i in range(5):
            t = i * 150
            x = 10 + i * 40
            events.append(ep.make_click_event(t, "press", "left", x, 10))
            events.append(ep.make_click_event(t + 40, "release", "left", x, 10))
        _install_null_controllers(window)
        _load_into_window(window, events)
        assert len(window.player.steps) == 5
        combo = window.page_library.loop_combo
        combo.setCurrentIndex(combo.findData("infinite"))
        window.start_replay()
        assert _wait_until(qapp, lambda: window.player.position >= 4, 3000)

        window._toggle_pause()
        _pump(qapp, 40)
        assert window.state is State.PAUSED
        paused_position = window.player.position

        window.step_next()
        _pump(qapp, 40)
        assert window.state is State.IDLE, "切到单步后不应还挂着回放状态"
        # 暂停点之后的那一步必须已经执行过（指针前进了，且没回到 0）
        assert window.player.step_index > 0, "暂停后单步不能从第一步重来"
        assert window.player.step_index >= paused_position // 2


# ===========================================================================
# 快捷输入（文本模拟打字）
# ===========================================================================
class TestQuickInputSettings:
    """设置页「快捷输入」区域：控件、开关联动、保存落盘。"""

    def test_widgets_exist(self, window, qapp):
        page = window.page_quick
        assert page.chk_enabled.isChecked() is False   # 默认关闭
        assert page.chk_paste.isChecked() is True
        assert len(page._pwd_edits) == 3
        assert len(page._eye_buttons) == 3
        for index in (1, 2, 3):
            assert f"quick_pwd_{index}" in page._edits
        assert page.btn_save.isEnabled() is True, (
            "保存按钮必须一直可用：否则取消勾选后没法把「关闭」存下去")

    def test_password_fields_masked(self, window, qapp):
        from PySide6.QtWidgets import QLineEdit

        for edit in window.page_quick._pwd_edits.values():
            assert edit.echoMode() == QLineEdit.Password

    def test_method_combo_defaults_to_auto(self, window, qapp):
        """通道下拉默认「自动」。

        钉死某条通道等于关掉整条降级阶梯——2026-11-11 就是配置里写着
        ``unicode``，受保护窗口一个字符都没进去，状态栏却报「已敲入 N 个字符」。
        """
        page = window.page_quick
        assert page.combo_method.currentData() == "auto"
        # 下拉里必须能选到「物理按键」与「窗口消息」，否则用户没法手工换通道
        values = [page.combo_method.itemData(i)
                  for i in range(page.combo_method.count())]
        assert "physical" in values and "message" in values
        assert page.combo_method.isEnabled() is False   # 总开关默认关

    def test_save_persists_method(self, window, qapp):
        page = window.page_quick
        page.chk_enabled.setChecked(True)
        _pump(qapp, 10)
        index = page.combo_method.findData("physical")
        page.combo_method.setCurrentIndex(index)
        page.btn_save.click()
        _pump(qapp, 60)
        assert window.config.quick_method == "physical"
        # 重新载入也要回到这一项，不能悄悄回落
        page.load_from_config(window.config)
        assert page.combo_method.currentData() == "physical"

    def test_click_first_checkbox_defaults_on(self, window, qapp):
        """「敲字前先点一下左键」默认勾上：页面内输入框不点就收不到字。"""
        page = window.page_quick
        assert page.chk_click.isChecked() is True
        assert page.chk_click.isEnabled() is False   # 总开关默认关

    def test_save_persists_click_first(self, window, qapp):
        page = window.page_quick
        page.chk_enabled.setChecked(True)
        _pump(qapp, 10)
        page.chk_click.setChecked(False)
        page.btn_save.click()
        _pump(qapp, 60)
        assert window.config.quick_click_first is False
        page.load_from_config(window.config)
        assert page.chk_click.isChecked() is False

    def test_eye_button_toggles_plaintext(self, window, qapp):
        from PySide6.QtWidgets import QLineEdit

        page = window.page_quick
        page.chk_enabled.setChecked(True)
        _pump(qapp, 10)
        edit = page._pwd_edits["pwd_1"]
        eye = page._eye_buttons["pwd_1"]
        assert edit.echoMode() == QLineEdit.Password
        assert eye.text() == "显示"

        eye.setChecked(True)
        _pump(qapp, 10)
        assert edit.echoMode() == QLineEdit.Normal
        assert eye.text() == "隐藏"

        eye.setChecked(False)
        _pump(qapp, 10)
        assert edit.echoMode() == QLineEdit.Password

    def test_master_switch_greys_children(self, window, qapp):
        page = window.page_quick
        assert page.spin_delay.isEnabled() is False
        assert page.chk_paste.isEnabled() is False
        page.chk_enabled.setChecked(True)
        _pump(qapp, 10)
        assert page.spin_delay.isEnabled() is True
        assert page.chk_paste.isEnabled() is True
        assert page.btn_save.isEnabled() is True
        assert page._edits["quick_pwd_1"].isEnabled() is True

    def test_save_writes_config_and_encrypts(self, window, qapp, tmp_path):
        """保存后：开关落盘、密码是密文、热键注册到位。"""
        page = window.page_quick
        page.chk_enabled.setChecked(True)
        page._pwd_edits["pwd_1"].setText("Pass123")
        page.btn_save.click()
        _pump(qapp, 60)

        cfg = window.config
        assert cfg.quick_input_enabled is True
        assert cfg.quick_password("pwd_1") == "Pass123"
        raw = json.loads(cfg.path.read_text(encoding="utf-8"))
        assert raw["quick_input"]["pwd_1"].startswith("enc2:")
        assert "Pass123" not in raw["quick_input"]["pwd_1"]
        # F1 + F2 都该注册上（只填了 pwd_1，F3/F4 不占键）
        bindings = window._hotkey_bindings()
        assert bindings.get("quick_paste") == "f1"
        assert bindings.get("quick_pwd_1") == "f2"
        assert "quick_pwd_2" not in bindings
        assert "快捷输入已保存" in window.message_label.text()

    def test_save_off_releases_hotkeys(self, window, qapp):
        page = window.page_quick
        page.chk_enabled.setChecked(True)
        page._pwd_edits["pwd_1"].setText("Pass123")
        page.btn_save.click()
        _pump(qapp, 60)
        assert "quick_paste" in window._hotkey_bindings()

        page.chk_enabled.setChecked(False)
        page.btn_save.click()
        _pump(qapp, 60)
        assert window.config.quick_input_enabled is False
        assert not [a for a in window._hotkey_bindings()
                    if a.startswith("quick_")], "关闭后必须释放 F1–F4"
        assert "已关闭" in window.message_label.text()

    def test_settings_save_does_not_touch_quick_input(self, window, qapp):
        """两页各存各的：设置页的「保存设置」不再顺手写快捷输入。

        拆页前是「保存设置」把快捷输入一起存（怕用户改完密码点错按钮）；
        现在密码只属于快捷输入页，用设置页的按钮去写它反而会让「改了没生效」
        变得更难判断——两边各有一个明确的保存按钮才是对的。
        """
        page = window.page_quick
        page._pwd_edits["pwd_1"].setText("OnlyInQuickPage")
        window.page_settings._on_save()
        _pump(qapp, 60)
        assert window.config.quick_password("pwd_1") == ""
        page.btn_save.click()
        _pump(qapp, 60)
        assert window.config.quick_password("pwd_1") == "OnlyInQuickPage"

    def test_config_loads_into_page(self, window, qapp):
        window.config.set("quick_input.enabled", True)
        window.config.set_quick_password("pwd_2", "loaded")
        window.config.save()
        window.page_quick.load_from_config(window.config)
        _pump(qapp, 10)
        page = window.page_quick
        assert page.chk_enabled.isChecked() is True
        assert page._pwd_edits["pwd_2"].text() == "loaded"
        assert page._pwd_edits["pwd_1"].text() == ""

    def test_duplicate_hotkey_rejected(self, window, qapp):
        """把密码热键改成 F6（= 开始回放）必须被拦下，否则两个动作抢一个键。"""
        page = window.page_quick
        page.chk_enabled.setChecked(True)
        page._pwd_edits["pwd_1"].setText("x")
        page._edits["quick_pwd_1"].set_token("f6")
        page.btn_save.click()
        _pump(qapp, 60)
        # 校验失败：热键没被写进配置，仍是默认的 F2
        assert window.config.hotkey("quick_pwd_1") == "f2"


class TestQuickInputDispatch:
    """热键动作分发：该敲什么、什么时候不敲。"""

    @staticmethod
    def _spy_typing(monkeypatch) -> list[tuple[str, int]]:
        """替身：记录 (文本, 间隔)，不碰真实键盘。"""
        calls: list[tuple[str, int]] = []

        def fake_type(text, *, delay_ms=8, method="auto", sender=None,
                      sleep=None, should_stop=None, report=None):
            calls.append((text, delay_ms))
            if report is not None:
                report("unicode")
            return len(qi.sanitize(text))

        monkeypatch.setattr("core.quick_input.type_text", fake_type)
        return calls

    def _enable(self, window, qapp, passwords: dict[str, str] | None = None):
        page = window.page_quick
        page.chk_enabled.setChecked(True)
        for slot, text in (passwords or {}).items():
            page._pwd_edits[slot].setText(text)
        page.btn_save.click()
        _pump(qapp, 60)

    def test_clipboard_path(self, window, qapp, monkeypatch):
        calls = self._spy_typing(monkeypatch)
        monkeypatch.setattr("core.quick_input.read_clipboard_text",
                            lambda: "abc123")
        self._enable(window, qapp)

        window._on_hotkey_action("quick_paste")
        assert _wait_until(qapp, lambda: calls, 2000)
        assert calls == [("abc123", 8)]
        assert _wait_until(qapp, lambda: "已敲入 6 个字符" in window.message_label.text(),
                           2000)

    def test_clipboard_filters_chinese(self, window, qapp, monkeypatch):
        """中英混合：只敲 ASCII 部分，并在状态栏说清丢了几个字。"""
        calls = self._spy_typing(monkeypatch)
        monkeypatch.setattr("core.quick_input.read_clipboard_text",
                            lambda: "abc中文123！")
        self._enable(window, qapp)

        window._on_hotkey_action("quick_paste")
        assert _wait_until(qapp, lambda: calls, 2000)
        assert calls[0][0] == "abc中文123！"          # 交给 type_text 自己过滤
        assert _wait_until(qapp, lambda: "已敲入 6 个字符" in window.message_label.text(),
                           2000)

    def test_password_path(self, window, qapp, monkeypatch):
        calls = self._spy_typing(monkeypatch)
        self._enable(window, qapp, {"pwd_1": "P@ss!"})

        window._on_hotkey_action("quick_pwd_1")
        assert _wait_until(qapp, lambda: calls, 2000)
        assert calls == [("P@ss!", 8)]

    def test_empty_password_warns_without_typing(self, window, qapp, monkeypatch):
        calls = self._spy_typing(monkeypatch)
        self._enable(window, qapp)                     # 三个密码都空

        window._on_hotkey_action("quick_pwd_2")
        _pump(qapp, 40)
        assert calls == []
        assert "还是空的" in window.message_label.text()

    def test_empty_clipboard_warns(self, window, qapp, monkeypatch):
        calls = self._spy_typing(monkeypatch)
        monkeypatch.setattr("core.quick_input.read_clipboard_text", lambda: "")
        self._enable(window, qapp)

        window._on_hotkey_action("quick_paste")
        _pump(qapp, 40)
        assert calls == []
        assert "没有可敲入的文本" in window.message_label.text()

    def test_non_ascii_only_is_refused(self, window, qapp, monkeypatch):
        """整段都是中文/全角：明确告诉用户敲不出来，而不是默默无事发生。"""
        calls = self._spy_typing(monkeypatch)
        self._enable(window, qapp, {"pwd_1": "中文密码！"})

        window._on_hotkey_action("quick_pwd_1")
        _pump(qapp, 40)
        assert calls == []
        assert "没有可模拟输入的字符" in window.message_label.text()

    def test_disabled_master_switch_warns(self, window, qapp, monkeypatch):
        calls = self._spy_typing(monkeypatch)
        window._on_hotkey_action("quick_paste")
        _pump(qapp, 40)
        assert calls == []
        assert "总开关是关的" in window.message_label.text()

    def test_recording_ignores_quick_input(self, window, qapp, monkeypatch):
        """录制中一个字符都不许打（否则用户分不清录到了什么）。"""
        calls = self._spy_typing(monkeypatch)
        monkeypatch.setattr("core.quick_input.read_clipboard_text",
                            lambda: "abc")
        self._enable(window, qapp)
        window._set_state(State.RECORDING)
        _pump(qapp, 20)

        window._on_hotkey_action("quick_paste")
        _pump(qapp, 40)
        assert calls == []
        assert "录制中已忽略快捷输入" in window.message_label.text()

    def test_paused_recording_also_ignored(self, window, qapp, monkeypatch):
        calls = self._spy_typing(monkeypatch)
        monkeypatch.setattr("core.quick_input.read_clipboard_text",
                            lambda: "abc")
        self._enable(window, qapp)
        window._set_state(State.RECORD_PAUSED)
        _pump(qapp, 20)

        window._on_hotkey_action("quick_paste")
        _pump(qapp, 40)
        assert calls == []

    def test_typing_does_not_change_state(self, window, qapp, monkeypatch):
        """快捷输入不进状态机：敲完仍然是空闲，不能把界面切成录制/回放。"""
        self._spy_typing(monkeypatch)
        monkeypatch.setattr("core.quick_input.read_clipboard_text",
                            lambda: "abc")
        self._enable(window, qapp)
        window._on_hotkey_action("quick_paste")
        _pump(qapp, 80)
        assert window.state is State.IDLE


class TestQuickInputRecorderPollution:
    """录制防污染：热键本身与注入的按键都不能进脚本。"""

    @staticmethod
    def _record_ignored(window, qapp) -> set[str]:
        """真跑一次「开始录制→停止」，拿到录制期间的忽略名单。"""
        window.start_recording()
        _pump(qapp, 60)
        try:
            return set(window.recorder.ignored_keys)
        finally:
            window.stop_recording()
            _pump(qapp, 60)

    def test_enabled_hotkeys_entered_ignore_list(self, window, qapp):
        page = window.page_quick
        page.chk_enabled.setChecked(True)
        page._pwd_edits["pwd_1"].setText("Pass123")
        page.btn_save.click()
        _pump(qapp, 60)

        ignored = self._record_ignored(window, qapp)
        assert "f1" in ignored, "F1 是热键，不能录成一步按键"
        assert "vk:112" in ignored, "F1 的 vk 形式（0x70=112）也要屏蔽"
        assert "f2" in ignored
        assert "f3" not in ignored, "没填密码的 F3 仍是普通按键，不该屏蔽"

    def test_disabled_quick_input_keeps_keys_recordable(self, window, qapp):
        """总开关关着：F1/F2/F3 就是普通按键，用户有权把它们录进脚本。"""
        ignored = self._record_ignored(window, qapp)
        assert "f1" not in ignored
        assert "f2" not in ignored
        assert "vk:112" not in ignored

    def test_typing_goes_through_send_input(self, window, qapp, monkeypatch):
        """敲击必须走 win_input 的 SendInput（不是 pynput / 剪贴板）。

        默认通道是 ``KEYEVENTF_UNICODE``：``wVk`` 留 0、字符进 ``wScan``。
        这三条缺一个就会被输入法接管（中文态下 ``b`` 变「杯」）。
        """
        from core import win_input

        seen: list = []
        monkeypatch.setattr(win_input, "_call_send_input",
                            lambda inp: seen.append(inp) or 1)
        # 钉死 unicode 通道：这里验的是「注入报文长什么样」，
        # 走 auto 会先做一次真实注入探测（本用例把 SendInput 换成了替身）。
        qi.type_text("a", delay_ms=1, method="unicode", sleep=lambda _s: None)
        assert len(seen) == 2                       # 按下 + 抬起
        assert [inp.ki.wScan for inp in seen] == [ord("a"), ord("a")]
        assert [inp.ki.dwFlags for inp in seen] == [
            win_input.KEYEVENTF_UNICODE,
            win_input.KEYEVENTF_UNICODE | win_input.KEYEVENTF_KEYUP,
        ]
        for inp in seen:
            assert inp.type == win_input.INPUT_KEYBOARD
            assert inp.ki.wVk == 0
