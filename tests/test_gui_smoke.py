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

import os
import sys
from pathlib import Path

import pytest

# 离屏平台必须在导入 QtWidgets 之前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QElapsedTimer, QPoint, QTimer, Qt    # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from core import event_parser as ep                              # noqa: E402
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
    monkeypatch.setattr(QMessageBox, "question",
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
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        window.page_library.select_path(paths[0])
        window.delete_selected_script()
        _pump(qapp, 60)
        assert not paths[0].exists()
        assert window.page_library.table.rowCount() == 0

    def test_delete_script_cancelled(self, window, qapp, monkeypatch):
        paths = self._seed(window, qapp, ["保留的"])
        monkeypatch.setattr(QMessageBox, "question",
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
        assert window.hotkeys.binding_display("record_stop") == "F9"
        assert window.hotkeys.binding_display("step_next") == "PageDown"
        # 模式一下的状态栏提示应包含“停止录制 F9”与“开始回放 F6”
        assert "F9" in window.hotkey_label.text()
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
        assert "F10" in window.hotkey_label.text()

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
        monkeypatch.setattr(QMessageBox, "question",
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
        monkeypatch.setattr(QMessageBox, "question",
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
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Save))
        assert window._confirm_discard_unsaved("新建脚本") is True
        assert (window.config.scripts_path() / "未保存确认测试.json").exists()
        assert window._unsaved is False

    def test_clean_state_does_not_ask(self, window, qapp, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        _load_into_window(window, _sample_events())
        window._unsaved = False
        monkeypatch.setattr(QMessageBox, "question",
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
        monkeypatch.setattr(QMessageBox, "question",
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
        monkeypatch.setattr(QMessageBox, "question",
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
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: pytest.fail("不应弹确认框")))
        assert window.save_edited_steps() == path


# ===========================================================================
# 窗口几何持久化
# ===========================================================================
class TestWindowGeometry:
    def test_geometry_saved_on_close(self, qapp, config, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question",
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
        monkeypatch.setattr(QMessageBox, "question",
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
