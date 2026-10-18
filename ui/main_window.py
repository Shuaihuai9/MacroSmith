"""主窗口：现代极简布局（侧边栏导航 + 卡片式内容区）。

职责边界
--------
* 本模块只做“界面 + 编排”，录制/回放/热键的具体逻辑都在 ``core/`` 里；
* pynput 的监听线程回调一律通过 **Qt 信号** 转发到主线程后再操作界面，
  绝不在监听线程里直接碰控件；
* 程序自己注入的事件由 ``MacroPlayer.is_executing`` 标志位过滤，
  状态栏与列表都不会被自我回放污染；
* **步骤编辑**（删除 / 上移 / 下移 / 插入）统一在 :meth:`_apply_step_edit`
  里完成：改数据 → 重排时间戳 → 同步回放器与列表 → 置脏标记。
"""


import logging
import time
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton,
                               QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from core import event_parser as ep
from core.config import Config, HOTKEY_LABELS
from core.hotkey_manager import HotkeyManager, display_name, parse_key
from core.player import MODE_STEP, MODE_TIMED, MacroPlayer
from core.recorder import Recorder
from core.poll_recorder import PollRecorder
from ui import icons, styles
from ui.event_list import EventListWidget, _make_icon
from ui.frameless import WindowShell
from ui.script_manager import ScriptManagerPage
from ui.settings import SettingsPage
from ui.sidebar import Sidebar
from ui.name_dialog import ask_name, script_stems
from ui.step_dialog import InsertStepDialog
from ui.step_editor import edit_step_dialog
from ui.time_dialog import ask_total_duration
from utils.win_utils import WINDOW_TITLE, ensure_dir, is_admin

log = logging.getLogger(__name__)

APP_VERSION = "1.0.0"

#: 内容区页面下标
#: 页面下标。第六阶段把「脚本库」上移为第一页并改名为「回放与脚本」：
#: 日常使用的入口放在最前面，降低「停在录制页误覆写已保存脚本」的风险。
PAGE_SCRIPTS = 0      # 回放与脚本（默认页）
PAGE_RECORD = 1       # 录制与步骤编辑
PAGE_SETTINGS = 2
PAGE_ABOUT = 3
#: 向后兼容别名（旧代码/测试里用 PAGE_LIBRARY 指脚本页）
PAGE_LIBRARY = PAGE_SCRIPTS

#: 按钮内图标尺寸
_TOOL_ICON_SIZE = QSize(15, 15)


class _ScriptComboBox(QComboBox):
    """记录「下拉列表是否刚被用户操作过」的可编辑下拉框。

    可编辑 QComboBox 有个坑：**焦点离开**时会把文本框内容当作一次
    "选择"并发射 ``activated`` —— 用户点侧边栏切换页面时就会误触发
    「加载脚本 + 跳回录制页」。

    用 ``_user_picked`` flag 区分「用户真在列表里点选了一项」与
    「焦点离开引起的伪激活」：hidePopup 在下拉列表确实打开过时置
    True，activated 消费后清除；若用户关掉下拉框但不选项，
    QTimer.singleShot(0) 在下一个事件循环周期清除 flag，避免后续
    伪 activated 误触发。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.last_popup_hide = 0.0
        self._user_picked = False

    def hidePopup(self) -> None:  # noqa: N802 - Qt 命名
        was_open = self.view() is not None and self.view().isVisible()
        super().hidePopup()
        if was_open:
            self.last_popup_hide = time.monotonic()
            self._user_picked = True
            # 若用户关掉下拉框但不选项（activated 不会发射），
            # 在下一个事件循环清除 flag，防止后续伪 activated 误触发
            QTimer.singleShot(0, self._clear_user_picked)

    def _clear_user_picked(self) -> None:
        self._user_picked = False


class State(Enum):
    """程序运行状态。"""

    IDLE = "idle"
    RECORDING = "recording"
    REPLAYING = "replaying"
    PAUSED = "paused"


class MainWindow(WindowShell):
    """MacroSmith 主窗口。"""

    # --- 跨线程信号（pynput 监听线程 -> Qt 主线程）----------------------
    sig_recorded = Signal(dict)          # 录制到一条事件
    sig_recorder_error = Signal(str)
    sig_hotkey = Signal(str)             # 热键动作名
    sig_player_event = Signal(int, dict)  # 模式一执行到某条原始事件
    sig_player_finished = Signal(str)    # finished / stopped
    sig_player_error = Signal(str)
    sig_poll_stop = Signal()             # 兼容轮询通道检测到停止热键（轮询线程）

    def __init__(self, config: Optional[Config] = None) -> None:
        super().__init__(WINDOW_TITLE)
        self.config = config or Config()
        self.state = State.IDLE
        # 样式表同时挂到窗口上，保证子控件无论从哪一层都能取到样式
        self.setStyleSheet(styles.build_stylesheet())
        # 无边框窗口需要显式设置窗口图标（任务栏与 Alt+Tab 都用它）
        try:
            from utils.win_utils import load_app_icon

            icon = load_app_icon()
            if icon is not None and not icon.isNull():
                self.setWindowIcon(icon)
        except Exception:  # pragma: no cover - 图标缺失不应影响启动
            log.debug("设置窗口图标失败", exc_info=True)

        self._pending_records: list[dict[str, Any]] = []   # 录制事件缓冲
        self._pending_highlights: list[int] = []           # 待高亮行号缓冲
        self._unsaved = False
        self._script_events: list[dict[str, Any]] = []     # 当前载入/录制的事件
        self._script_steps: list[ep.Step] = []             # 当前步骤（编辑后的事实来源）
        self._current_script: Optional[Path] = None        # 当前脚本文件路径
        self._hotkey_failures: list[str] = []
        self._initializing = False                         # 初始化期间抑制模式切换提示

        # ---------------- 核心组件 ----------------
        self.recorder = Recorder(
            is_executing=lambda: self.player.is_executing,
            on_event=self._on_recorder_event,
            on_error=self.sig_recorder_error.emit,
        )
        # 兼容轮询通道：腾讯系（WeGame/QQ游戏大厅）前台时低层钩子被反作弊
        # 过滤，轮询通道自动接管；普通前台时它不产事件，与钩子通道互补。
        self.poll_recorder = PollRecorder(
            is_executing=lambda: self.player.is_executing,
            on_event=self._on_recorder_event,
            on_error=self.sig_recorder_error.emit,
            on_stop_hotkey=self.sig_poll_stop.emit,
        )
        # 轮询线程检测到「停止录制热键」按下沿 -> 切回主线程停止录制
        self.sig_poll_stop.connect(self.stop_recording)
        self.player = MacroPlayer(
            on_event=lambda idx, evt: self.sig_player_event.emit(idx, evt),
            on_finished=self.sig_player_finished.emit,
            on_error=self.sig_player_error.emit,
        )
        self.hotkeys = HotkeyManager(
            is_executing=lambda: self.player.is_executing,
            exclusive=self.config.hotkey_exclusive,
        )

        # ---------------- 界面 ----------------
        self._build_ui()
        self._connect_signals()
        self._apply_options_from_config()
        self._restore_window_geometry()
        self._register_hotkeys()
        self._refresh_status()
        self._set_state(State.IDLE)
        self.refresh_script_library()

        # 录制事件与回放高亮都以 120ms 为周期批量刷新，避免高频 UI 重绘
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(120)
        self._ui_timer.timeout.connect(self._flush_ui_buffers)
        self._ui_timer.start()

        self._record_timer = QTimer(self)
        self._record_timer.setInterval(200)
        self._record_timer.timeout.connect(self._on_record_tick)

    # ==================================================================
    # 界面搭建
    # ==================================================================
    def _build_ui(self) -> None:
        """装配侧边栏 + 页面堆栈。"""
        self.setWindowTitle(WINDOW_TITLE)

        content = QWidget(self.content_host)
        layout = QHBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.content_host_layout = QVBoxLayout(self.content_host)
        self.content_host_layout.setContentsMargins(0, 0, 0, 0)
        self.content_host_layout.setSpacing(0)
        self.content_host_layout.addWidget(content, 1)

        self.sidebar = Sidebar(content)
        layout.addWidget(self.sidebar)

        self.pages = QStackedWidget(content)
        self.pages.setObjectName("ContentArea")
        self.pages.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.pages, 1)

        self.page_record = self._build_record_page()
        self.page_library = ScriptManagerPage(self.pages)
        self.page_settings = SettingsPage(self.pages)
        self.page_about = self._build_about_page()
        for page in (self.page_library, self.page_record, self.page_settings,
                     self.page_about):
            self.pages.addWidget(page)
        self.pages.setCurrentIndex(PAGE_SCRIPTS)

        # 模式下拉框建在「回放与脚本」页的回放控制卡里（第七阶段起），
        # 这里把主窗口的属性名指向同一个实例——全程序 20+ 处引用
        # （_on_mode_changed / _set_state / _apply_options_from_config 等）
        # 都不需要改。
        self.mode_combo = self.page_library.mode_combo

        # 窗口级全局状态栏：挂在 content_host 底部、页面堆栈之下，
        # 「回放与脚本」「录制与步骤编辑」（以及设置/关于）共享同一份。
        # 全窗口只能创建这一次：它内部会赋值 self.mode_label /
        # self.step_label 等属性，若再建一次，两份实例互相覆盖，
        # 只有一份在更新（参见 PROJECT_BIBLE 6.2 的警示）。
        status_wrap = QWidget(self.content_host)
        status_wrap_layout = QVBoxLayout(status_wrap)
        status_wrap_layout.setContentsMargins(styles.CONTENT_MARGIN, 0,
                                              styles.CONTENT_MARGIN, 10)
        status_wrap_layout.setSpacing(0)
        self.status_bar = self._build_status_bar(status_wrap)
        status_wrap_layout.addWidget(self.status_bar)
        self.content_host_layout.addWidget(status_wrap)

    # ------------------------------------------------------------------
    def _build_record_page(self) -> QWidget:
        """录制/回放主页面：顶部脚本与操作按钮 + 中部步骤编辑器 + 底部状态栏。"""
        page = QWidget(self.pages)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(styles.CONTENT_MARGIN, 12, styles.CONTENT_MARGIN,
                                 styles.CONTENT_MARGIN)
        layout.setSpacing(styles.CARD_SPACING)

        # ---------------- 顶部：脚本信息 + 操作按钮 ----------------
        top = QWidget(page)
        top.setObjectName("Card")
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(14, 10, 14, 10)
        top_layout.setSpacing(7)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        script_icon = QLabel()
        script_icon.setPixmap(_make_icon(icons.ICON_FOLDER, styles.TEXT_SECONDARY).pixmap(16, 16))
        script_icon.setStyleSheet("background: transparent;")
        title_row.addWidget(script_icon)

        self.script_name_edit = _ScriptComboBox(page)
        self.script_name_edit.setEditable(True)
        self.script_name_edit.setInsertPolicy(QComboBox.NoInsert)
        self.script_name_edit.setMinimumWidth(220)
        self.script_name_edit.setMinimumHeight(30)
        self.script_name_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.script_name_edit.lineEdit().setPlaceholderText("脚本名称（留空则按时间自动命名）")
        self.script_name_edit.setToolTip(
            "可直接输入脚本名称，也可以下拉选择历史脚本：\n"
            "· 输入名称 → 保存时按该名称写入脚本目录\n"
            "· 下拉选择 → 立即载入那个脚本"
        )
        self.script_name_edit.setCurrentText(self._default_script_name())
        self.script_name_edit.editTextChanged.connect(self._on_script_name_edited)
        self.script_name_edit.activated.connect(self._on_script_combo_activated)
        self.script_name_edit.lineEdit().textEdited.connect(self._on_script_name_typed)
        self._combo_guard = False       # 程序刷新下拉项/回填名称时抑制副作用
        self._combo_user_typed = False  # 用户手输过名称（回车提交算有效激活）
        title_row.addWidget(self.script_name_edit, 1)

        # 「新建脚本」：与下拉框并列的显式入口
        self.btn_new_script = QPushButton("新建脚本", page)
        self.btn_new_script.setObjectName("SecondaryButton")
        self.btn_new_script.setIcon(_make_icon(icons.ICON_PLUS, styles.ICON_ON_SECONDARY))
        self.btn_new_script.setIconSize(_TOOL_ICON_SIZE)
        self.btn_new_script.setCursor(Qt.PointingHandCursor)
        self.btn_new_script.setToolTip("新建一个空脚本（会先让你确认名称）")
        title_row.addWidget(self.btn_new_script)

        # 模式下拉框已移到「回放与脚本」页的回放控制卡（见
        # ScriptManagerPage._build_play_card）；本页通过
        # self.mode_combo 引用同一个实例，引用点在 _build_ui。
        top_layout.addLayout(title_row)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.btn_start_record = QPushButton("开始录制", page)
        self.btn_start_record.setObjectName("PrimaryButton")
        self.btn_start_record.setIcon(_make_icon(icons.ICON_RECORD, styles.ICON_ON_PRIMARY))
        self.btn_stop_record = QPushButton("停止录制", page)
        self.btn_stop_record.setObjectName("DangerButton")
        self.btn_start_play = QPushButton("开始回放", page)
        self.btn_start_play.setObjectName("PrimaryButton")
        self.btn_start_play.setIcon(_make_icon(icons.ICON_PLAY, styles.ICON_ON_PRIMARY))
        self.btn_stop_play = QPushButton("停止回放", page)
        self.btn_stop_play.setObjectName("DangerButton")
        self.btn_reset_step = QPushButton("重置指针", page)
        self.btn_reset_step.setObjectName("SecondaryButton")
        self.btn_reset_step.setIcon(_make_icon(icons.ICON_REFRESH, styles.ICON_ON_SECONDARY))
        self.btn_reset_step.setToolTip("模式二：把步骤指针复位到开头")

        for btn in (self.btn_start_record, self.btn_stop_record, self.btn_start_play,
                    self.btn_stop_play, self.btn_reset_step):
            btn.setIconSize(_TOOL_ICON_SIZE)
            btn.setCursor(Qt.PointingHandCursor)
            button_row.addWidget(btn)
        button_row.addStretch(1)

        self.btn_load = QPushButton("打开脚本", page)
        self.btn_load.setObjectName("SecondaryButton")
        self.btn_save_as = QPushButton("另存为…", page)
        self.btn_save_as.setObjectName("SecondaryButton")
        self.btn_clear = QPushButton("清空", page)
        self.btn_clear.setObjectName("DangerButton")
        for btn in (self.btn_load, self.btn_save_as, self.btn_clear):
            btn.setCursor(Qt.PointingHandCursor)
            button_row.addWidget(btn)
        top_layout.addLayout(button_row)

        # 第九阶段起：「仅录制点击坐标」与「记录鼠标移动」两个开关已删除，
        # 录制始终记录鼠标移动（带抖动过滤）+ 点击 + 滚轮 + 键盘。

        layout.addWidget(top)

        # ---------------- 中部：步骤编辑器 ----------------
        self.event_list = EventListWidget(page)
        layout.addWidget(self.event_list, 1)

        # 状态栏已提升为窗口级（见 _build_ui），本页不再单独放置。
        # 页面最小高度相应调低，保证窗口仍能缩到 820x560 的下限。
        page.setMinimumHeight(380)
        return page

    def minimumSizeHint(self):  # noqa: N802 - Qt 命名
        """允许窗口缩到 820x560；默认初始尺寸是 1000x700（见 WindowShell.sizeHint）。"""
        from PySide6.QtCore import QSize

        return QSize(self.MIN_WIDTH, self.MIN_HEIGHT)

    def _build_status_bar(self, parent: QWidget) -> QWidget:
        """底部状态栏：当前模式、步骤进度、状态消息、热键提示。"""
        card = QWidget(parent)
        card.setObjectName("StatusCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(3)

        line1 = QHBoxLayout()
        line1.setSpacing(12)
        self.mode_label = QLabel("当前模式：未开始")
        self.mode_label.setObjectName("StatusLabel")
        line1.addWidget(self.mode_label)

        self.step_label = QLabel("步骤 0 / 0")
        self.step_label.setObjectName("StepProgress")
        line1.addWidget(self.step_label)

        self.loop_label = QLabel("")
        self.loop_label.setObjectName("Badge")
        self.loop_label.setToolTip("循环回放的当前轮次（只作用于模式一）")
        self.loop_label.setVisible(False)
        line1.addWidget(self.loop_label)
        line1.addStretch(1)

        self.event_count_label = QLabel("事件 0 条")
        self.event_count_label.setObjectName("StatusHint")
        line1.addWidget(self.event_count_label)
        layout.addLayout(line1)

        self.message_label = QLabel("准备就绪。点击“开始录制”或按热键开始。")
        self.message_label.setObjectName("StatusHint")
        layout.addWidget(self.message_label)

        self.hotkey_label = QLabel("")
        self.hotkey_label.setObjectName("StatusHint")
        self.hotkey_label.setWordWrap(True)
        layout.addWidget(self.hotkey_label)
        return card

    def _build_about_page(self) -> QWidget:
        """关于页。"""
        page = QWidget(self.pages)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(styles.CONTENT_MARGIN, styles.CONTENT_MARGIN,
                                 styles.CONTENT_MARGIN, styles.CONTENT_MARGIN)
        layout.setSpacing(styles.CARD_SPACING)

        card = QWidget(page)
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(7)

        name = QLabel("宏匠")
        name.setObjectName("CardTitle")
        card_layout.addWidget(name)

        lines = [
            f"版本：v{APP_VERSION}",
            "定位：键鼠录制/回放工具，类似按键精灵，单步热键执行，图形化步骤编辑。",
            "",
            "功能概览",
            "· 录制鼠标移动、点击、滚轮与键盘按键，带毫秒级相对时间戳",
            "· 步骤编辑器：勾选、批量删除、上移 / 下移、插入新步骤、保存回写",
            "· 脚本库：浏览、预览、加载、重命名、删除、新建脚本",
            "· 模式一 原速回放：按录制时的间隔重现全部操作，支持暂停与停止",
            "· 模式二 单步热键：按操作单元逐步执行，PageUp/PageDown 控制进度",
            "· 回放期间程序注入的事件会被自动忽略，不会二次录制",
        ]
        for text in lines:
            label = QLabel(text)
            label.setObjectName("SectionTitle" if text == "功能概览" else "Hint")
            label.setWordWrap(True)
            card_layout.addWidget(label)

        card_layout.addSpacing(8)
        for caption, value in (("配置文件", self.config.path),
                               ("脚本目录", self.config.scripts_path())):
            row = QHBoxLayout()
            row.setSpacing(8)
            tag = QLabel(caption)
            tag.setObjectName("SectionTitle")
            tag.setFixedWidth(64)
            row.addWidget(tag)
            path_label = QLabel(str(value))
            path_label.setObjectName("Hint")
            path_label.setWordWrap(True)
            path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row.addWidget(path_label, 1)
            card_layout.addLayout(row)

        #: 开发者 / GitHub / 反馈建议（脚本目录下方三行）
        info_rows = [
            ("开发者", "帅帅@Shuaihuai9"),
            ("GitHub",
             '<a href="https://github.com/Shuaihuai9/MacroSmith">'
             'https://github.com/Shuaihuai9/MacroSmith</a>'),
            ("反馈建议",
             "欢迎在 GitHub 提 Issue 或发送邮件至 2669850983@qq.com"),
        ]
        for caption, value in info_rows:
            row = QHBoxLayout()
            row.setSpacing(8)
            tag = QLabel(caption)
            tag.setObjectName("SectionTitle")
            tag.setFixedWidth(64)
            row.addWidget(tag)
            info_label = QLabel(value)
            info_label.setObjectName("Hint")
            info_label.setWordWrap(True)
            info_label.setTextFormat(Qt.RichText)
            info_label.setOpenExternalLinks(True)
            info_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
            row.addWidget(info_label, 1)
            card_layout.addLayout(row)

        layout.addWidget(card)
        layout.addStretch(1)
        return page

    # ==================================================================
    # 信号连接
    # ==================================================================
    def _connect_signals(self) -> None:
        # 侧边栏
        self.sidebar.record_clicked.connect(lambda: self._show_page(PAGE_RECORD))
        self.sidebar.library_clicked.connect(lambda: self._show_page(PAGE_SCRIPTS))
        self.sidebar.settings_clicked.connect(lambda: self._show_page(PAGE_SETTINGS))
        self.sidebar.about_clicked.connect(lambda: self._show_page(PAGE_ABOUT))

        # 顶部按钮
        self.btn_start_record.clicked.connect(self.start_recording)
        self.btn_stop_record.clicked.connect(self.stop_recording)
        self.btn_start_play.clicked.connect(self.start_replay)
        self.btn_stop_play.clicked.connect(self.stop_replay)
        self.btn_reset_step.clicked.connect(self.reset_step_pointer)
        self.btn_load.clicked.connect(self.load_script_dialog)
        self.btn_save_as.clicked.connect(self.save_script_as)
        self.btn_clear.clicked.connect(self.clear_events)
        self.btn_new_script.clicked.connect(self.create_script)

        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        # 步骤编辑器
        self.event_list.row_activated.connect(self._on_row_activated)
        self.event_list.edit_requested.connect(self._on_edit_action)

        # 「回放与脚本」页（原脚本库）
        self.page_library.create_requested.connect(self.create_script)
        self.page_library.rename_requested.connect(self.rename_selected_script)
        self.page_library.delete_requested.connect(self.delete_selected_script)
        self.page_library.load_requested.connect(self.load_selected_script)
        self.page_library.refresh_requested.connect(self.refresh_script_library)
        self.page_library.play_requested.connect(self.start_replay)
        self.page_library.stop_play_requested.connect(self.stop_replay)
        self.page_library.loop_changed.connect(self.on_loop_changed)

        # 设置页
        self.page_settings.hotkeys_saved.connect(self._save_hotkeys)
        self.page_settings.options_saved.connect(self._save_options)
        self.page_settings.exclusive_toggled.connect(self._on_exclusive_toggled)
        self.page_settings.merge_changed.connect(self._on_merge_changed)

        # 跨线程信号（Qt 会按 AutoConnection 自动排队到主线程执行）
        self.sig_recorded.connect(self._on_recorded_on_ui)
        self.sig_recorder_error.connect(self._on_error)
        self.sig_player_event.connect(self._on_player_event)
        self.sig_player_finished.connect(self._on_player_finished)
        self.sig_player_error.connect(self._on_error)
        self.sig_hotkey.connect(self._on_hotkey_action)

    # ==================================================================
    # 热键
    # ==================================================================
    def _register_hotkeys(self) -> None:
        """按配置重新注册全部全局热键，失败时提示用户改键。"""
        callbacks = {
            "record_stop": lambda: self.sig_hotkey.emit("record_stop"),
            "play_toggle": lambda: self.sig_hotkey.emit("play_toggle"),
            "pause_toggle": lambda: self.sig_hotkey.emit("pause_toggle"),
            "play_stop": lambda: self.sig_hotkey.emit("play_stop"),
            "step_next": lambda: self.sig_hotkey.emit("step_next"),
            "step_prev": lambda: self.sig_hotkey.emit("step_prev"),
            "step_reset": lambda: self.sig_hotkey.emit("step_reset"),
        }
        for action, callback in callbacks.items():
            self.hotkeys.set_callback(action, callback)

        # 独占模式由配置决定（用户可在设置页开关）
        self.hotkeys.set_exclusive(self.config.hotkey_exclusive)

        failed = self.hotkeys.register_all(self.config.hotkeys)
        # register_all 内部会在需要时重挂；这里保证「首次启动」也走同一路径，
        # 独占失败时 restart() 会自动退化到普通监听，热键不会失效。
        started = True
        if not self.hotkeys.is_running:
            started = self.hotkeys.restart()
        degraded = started and not self.hotkeys.exclusive
        if not started:
            failed = list(self.config.hotkeys.keys())
            err = getattr(self.hotkeys, "_last_error", "") or "未知原因"
            self._set_message(f"全局热键启动失败（{err}），请到“设置”页检查。")
        self._hotkey_failures = sorted(set(failed))

        if self._hotkey_failures:
            names = "、".join(
                HOTKEY_LABELS.get(a, (a, ""))[0] for a in self._hotkey_failures
            )
            self._set_message(f"以下热键注册失败：{names}。请到“设置”页改用其它按键。")
            QTimer.singleShot(300, lambda: self._warn_hotkey_failure(names))
        elif self.hotkeys.exclusive:
            # 独占模式已启用：钩子若被更高权限程序压制（UIPI），主动提示
            QTimer.singleShot(1200, self._check_exclusive_health)
        elif degraded:
            # 退化提示必须保留：用户需要知道按键不再被独占
            self._set_message(
                "热键独占模式启动失败，已退化为普通监听："
                "热键可用，但按键仍会传给前台程序。"
            )
        self._refresh_hotkey_hint()

    def _check_exclusive_health(self) -> None:
        """检查独占钩子是否真的在生效，失效时给出可操作的建议。"""
        if not self.hotkeys.exclusive or self.state is not State.IDLE:
            return
        if self.hotkeys.is_running and not self.hotkeys.hook_alive:
            self._set_message(
                "热键独占可能未生效：前台有更高权限（管理员）的程序时，"
                "Windows 会忽略本程序的键盘钩子。可尝试以管理员身份运行 宏匠，"
                "或在设置页关闭独占模式。"
            )

    def _warn_hotkey_failure(self, names: str) -> None:
        exclusive = self.config.hotkey_exclusive
        extra = (
            "\n\n当前已开启「热键独占模式」。若独占失败，"
            "可尝试以管理员身份运行本程序，或在设置页关闭独占模式。"
            if exclusive else ""
        )
        QMessageBox.warning(
            self,
            "热键注册失败",
            f"以下全局热键无法注册：\n{names}\n\n"
            "可能被其它程序占用或被安全软件拦截。\n"
            "请进入“设置”页把失败的热键改成其它按键后保存。" + extra,
        )
        self._show_page(PAGE_SETTINGS)

    def _on_hotkey_action(self, action: str) -> None:
        """热键动作分发（已在主线程执行）。

        第九阶段策略：**任何模式下所有热键都生效，并自动切到对应模式**。

        * F6 开始回放：空闲时无论当前是模式一还是模式二，一律切到
          **模式一**并开始原速回放；已在回放/暂停中则是暂停/继续切换。
        * F7 暂停/继续：回放/暂停中切换暂停；**模式二空闲**时视为
          「切到模式一并开始回放」；模式一空闲时提示先按 F6。
        * F8 停止：回放/暂停中停止并指针归零；模式二空闲且指针不在
          开头时把执行指针归零。
        * PageDown / PageUp / Home：一律切到 **模式二** 再执行
          （切换由 _enter_step_mode 完成；若模式一正在回放，会先停止
          当前回放再单步，两个执行通道互斥，见 _enter_step_mode）。
        """
        if action == "record_stop":
            if self.state is State.RECORDING:
                self.stop_recording()
            return
        if action == "play_toggle":
            if self.state is State.RECORDING:
                return
            if self.state in (State.REPLAYING, State.PAUSED):
                self._toggle_pause()
            else:
                # 模式二下按 F6：先切回模式一，再开始原速回放
                self._ensure_mode(MODE_TIMED, "开始回放")
                self.start_replay()
            return
        if action == "pause_toggle":
            if self.state is State.RECORDING:
                return
            if self.state in (State.REPLAYING, State.PAUSED):
                self._toggle_pause()
            elif self.mode_combo.currentData() == MODE_STEP:
                # 模式二下按 F7：视为「切到模式一并开始回放」
                self._ensure_mode(MODE_TIMED, "暂停/继续")
                self.start_replay()
            else:
                self._set_message("当前不在回放中，F7 无效（需要先按 F6 开始回放）。")
            return
        if action == "play_stop":
            if self.state in (State.REPLAYING, State.PAUSED):
                self.stop_replay()
            elif self.player.step_index > 0:
                # 模式二空闲时 F8 = 停止：执行指针归零
                self.reset_step_pointer()
            return
        if action == "step_next":
            self.step_next()
            return
        if action == "step_prev":
            self.step_prev()
            return
        if action == "step_reset":
            if self.state is State.RECORDING:
                self._set_message("正在录制中，无法重置指针。")
                return
            self._enter_step_mode()
            self.reset_step_pointer()
            return

    # ==================================================================
    # 录制
    # ==================================================================
    def start_recording(self) -> None:
        """开始录制。"""
        if not self._ensure_mode(MODE_TIMED, "开始录制"):
            return
        player = self.config.get("player", {}) or {}
        ok = self.recorder.start(
            min_interval_ms=int(player.get("move_min_interval_ms", 50)),
            min_distance_px=int(player.get("move_min_distance_px", 2)),
        )
        if not ok:
            self._on_error("录制启动失败：可能是监听器无法启动（权限或安全软件限制）。")
            return
        # 兼容通道与钩子通道共用时间基与参数；它只在腾讯系前台产出事件，
        # 启动失败不影响普通录制，仅记录日志。
        stop_vk = 0x78  # 默认 F9
        try:
            token = (self.config.get("hotkeys", {}) or {}).get(
                "record_stop", "f9")
            vk = getattr(parse_key(token), "vk", None)
            if isinstance(vk, int):
                stop_vk = vk
        except Exception:
            log.debug("解析停止热键 VK 失败，兼容通道使用默认 F9", exc_info=True)
        if not self.poll_recorder.start(
                t0=self.recorder.t0,
                stop_vk=stop_vk,
                min_interval_ms=int(player.get("move_min_interval_ms", 50)),
                min_distance_px=int(player.get("move_min_distance_px", 2)),
        ):
            log.warning("兼容轮询通道启动失败，普通窗口录制不受影响。")
        self._set_state(State.RECORDING)
        self.event_list.clear()
        self._script_events = []
        self._script_steps = []
        self._current_script = None
        self.player.clear()
        self._unsaved = False
        self._pending_records.clear()
        self._record_timer.start()
        self._set_message(
            f"正在录制… 按 "
            f"{self.hotkeys.binding_display('record_stop') or 'F9'} 停止录制。"
        )

    def stop_recording(self) -> None:
        """停止录制，保存到内存并刷新界面。"""
        if self.state is not State.RECORDING:
            return
        self._record_timer.stop()
        self.recorder.stop()
        self.poll_recorder.stop()
        # 两通道事件都经 on_event 管线实时进入 _script_events，此处统一清洗：
        # 补全未释放按键、剔除停止热键、保证时间戳单调（sanitize 幂等）。
        self._flush_ui_buffers()
        events = ep.sanitize_events(
            self._script_events, ignore_keys=self.recorder.ignored_keys)
        if not events:
            # 兜底：清洗后为空时退回原始缓冲，保证界面流程走通
            events = list(self._script_events)
        self._sync_after_recording(events)
        duration = ep.events_duration_ms(events) if events else 0
        self._set_message(
            f"录制完成：共 {len(events)} 条事件、"
            f"{len(self._script_steps)} 个步骤，时长 {duration / 1000:.2f} 秒。"
            "可在下方勾选编辑，或直接回放。"
        )

    def _sync_after_recording(self, events: Optional[list[dict[str, Any]]] = None) -> None:
        """把录制结果同步到界面与回放器（停止录制、或外部直接喂数据时使用）。

        ``events`` 为 None 时取缓冲里已经收到的事件，这样即使录制器因为
        权限问题没能真正启动，界面流程也能走通。
        """
        self._flush_ui_buffers()
        if events is None:
            events = list(self._script_events)
        self._script_events = list(events)
        self._script_steps = self._build_steps(self._script_events)
        self.player.load(self._script_events, self.script_name_edit.currentText().strip())
        self.event_list.set_steps(self._script_steps)
        self._unsaved = bool(self._script_events)
        self._set_state(State.IDLE)
        self._refresh_status()

    def _on_recorder_event(self, event: dict[str, Any]) -> None:
        """录制回调（pynput 线程）——只入队，真正的界面更新在计时器里做。"""
        self._pending_records.append(event)

    def _on_recorded_on_ui(self, event: dict[str, Any]) -> None:
        """``sig_recorded`` 的槽：运行在主线程，把事件排进批量刷新缓冲。

        ``Recorder`` 的回调在监听线程里执行，直接 ``emit`` 会被 Qt 以
        QueuedConnection 排队到主线程，因此这里可以安全地碰数据。
        """
        self._pending_records.append(event)

    def stop_button_disabled(self) -> bool:
        """辅助判断：停止录制按钮是否处于禁用状态（供测试与自检使用）。"""
        return not self.btn_stop_record.isEnabled()

    def _on_record_tick(self) -> None:
        """录制中的状态栏计时刷新。"""
        if self.state is State.RECORDING:
            elapsed = self.recorder.elapsed_ms() / 1000.0
            total = self.recorder.event_count + self.poll_recorder.event_count
            text = f"正在录制… 已录制 {elapsed:.1f} 秒 / {total} 条事件"
            if not is_admin():
                text += "（未以管理员运行：腾讯系界面可能录不到按键，请用管理员身份启动）"
            self.message_label.setText(text)
            self.event_count_label.setText(f"事件 {total} 条")

    # ==================================================================
    # 模式一：原速回放
    # ==================================================================
    def start_replay(self) -> None:
        """开始回放：模式一走时间轴，模式二提示使用热键单步。"""
        if self.state is State.RECORDING:
            self._set_message("正在录制中，无法回放。请先按停止录制。")
            return
        events = self._current_events()
        if not events:
            self._set_message("没有可回放的内容，请先录制或载入脚本。")
            return

        if self.mode_combo.currentData() == MODE_STEP:
            self._enter_step_mode()
            self._set_message(
                f"模式二 · 单步热键执行：按 "
                f"{self.hotkeys.binding_display('step_next')} 执行下一步，"
                f"{self.hotkeys.binding_display('step_prev')} 回退，"
                f"{self.hotkeys.binding_display('step_reset')} 重置指针。"
            )
            self._refresh_status()
            return

        self.player.load(events, self.script_name_edit.currentText().strip())
        self.player.speed = float(self.config.get("player.speed", 1.0) or 1.0)
        # 循环设置来自配置（界面上的下拉框会写回配置）
        self.player.set_loop(self.config.loop_mode, self.config.loop_count)
        if not self.player.start_timed():
            self._set_message("回放启动失败。")
            return
        self._set_state(State.REPLAYING)
        loop_note = self._loop_note()
        self._set_message(
            f"模式一 · 原速回放中（{self.player.speed:g}×）{loop_note}… "
            f"{self.hotkeys.binding_display('pause_toggle')} 暂停/继续，"
            f"{self.hotkeys.binding_display('play_stop')} 停止。"
        )
        self._refresh_status()

    def _loop_note(self) -> str:
        """回放提示里的循环说明片段。"""
        mode = self.player.loop_mode
        if mode == "fixed":
            return f"，循环 {self.player.loop_count} 轮"
        if mode == "infinite":
            return "，无限循环"
        return ""

    def on_loop_changed(self, mode: str, rounds: int) -> None:
        """界面上的循环设置变化：写配置 + 同步给回放器。"""
        self.config.set_loop(mode, rounds)
        self.config.save()
        self.player.set_loop(mode, rounds)
        text = {"none": "不循环", "fixed": f"固定 {rounds} 轮",
                "infinite": "无限循环"}.get(mode, mode)
        self._set_message(f"循环设置已更新：{text}（只作用于模式一原速回放）。")
        self._refresh_status()

    def stop_replay(self) -> None:
        """停止回放：终止线程并把指针归零。"""
        if self.state not in (State.REPLAYING, State.PAUSED):
            return
        rounds = self.player.loop_rounds_done
        self.player.stop()
        self._pending_highlights.clear()
        self._set_state(State.IDLE)
        self.event_list.highlight(-1)
        prefix = f"已停止（已跑完 {rounds} 轮）。" if rounds > 1 else ""
        self._set_message(f"{prefix}回放已停止，执行指针已归零。")
        self._refresh_status()

    def _toggle_pause(self) -> None:
        """暂停 / 继续切换。"""
        paused = self.player.toggle_pause()
        if paused is None:
            return
        if paused:
            self._set_state(State.PAUSED)
            self._set_message(
                f"已暂停在第 {self.player.position} 条事件。"
                f"按 {self.hotkeys.binding_display('pause_toggle')} 继续。"
            )
        else:
            self._set_state(State.REPLAYING)
            self._set_message("已继续回放。")
        self._refresh_status()

    def _on_player_event(self, index: int, _event: dict[str, Any]) -> None:
        """模式一每执行一条事件（回放线程）——入队等待批量高亮。"""
        self._pending_highlights.append(index)

    def _on_player_finished(self, reason: str) -> None:
        """回放结束（回放线程 -> 主线程）。"""
        rounds = self.player.loop_rounds_done
        if reason == "finished":
            self._set_state(State.IDLE)
            if rounds > 1:
                self._set_message(f"循环回放完成，共执行 {rounds} 轮。")
            else:
                self._set_message("回放完成。")
        self._pending_highlights.clear()
        self._refresh_status()

    # ==================================================================
    # 模式二：单步热键执行
    # ==================================================================
    def step_next(self) -> None:
        """执行下一步操作单元。"""
        if self.state is State.RECORDING:
            self._set_message("正在录制中，无法单步执行。")
            return
        if not self._current_events():
            self._set_message("没有可执行的步骤，请先录制或载入脚本。")
            return
        self._enter_step_mode()
        result = self.player.step_next()
        if result is None:
            self._set_message(
                f"已经到最后一步（共 {self.player.step_total} 步）。"
                f"按 {self.hotkeys.binding_display('step_reset')} 重置指针。"
            )
            self._refresh_status()
            return
        self._refresh_step_view()
        if 0 <= result - 1 < len(self._script_steps):
            self._set_message(f"已执行步骤 {result}：{self._script_steps[result - 1].description}")
        else:
            self._set_message(f"已执行步骤 {result}。")

    def step_prev(self) -> None:
        """回退并重新执行上一步操作单元。"""
        if self.state is State.RECORDING:
            self._set_message("正在录制中，无法单步执行。")
            return
        if not self._current_events():
            self._set_message("没有可执行的步骤，请先录制或载入脚本。")
            return
        self._enter_step_mode()
        result = self.player.step_prev()
        if result is None:
            self._set_message("已经回到开头，没有更早的步骤了。")
            self._refresh_status()
            return
        self._refresh_step_view()
        if result <= 0:
            # 指针退到开头：没有「所在的那一步」可重放
            self._set_message("已回到开头（步骤 0）。")
        elif 0 <= result - 1 < len(self._script_steps):
            self._set_message(
                f"已回退到步骤 {result} 并重新执行：{self._script_steps[result - 1].description}")
        else:
            self._set_message(f"已回退到步骤 {result} 并重新执行。")

    def reset_step_pointer(self) -> None:
        """把步骤指针复位到开头（不执行任何操作）。"""
        self.player.step_reset()
        self._refresh_step_view()
        self.event_list.highlight(-1)
        self._set_message("步骤指针已重置到开头（步骤 0）。")

    def _enter_step_mode(self) -> None:
        """切到模式二：同步下拉框与回放器的步骤数据。

        第九阶段策略：模式一原速回放与模式二单步执行**互斥**。
        若切入模式二时模式一仍在回放/暂停，先停止当前回放
        （回放线程终止、执行指针归零），再进入单步模式 ——
        否则两个执行通道会同时向系统注入键鼠事件，行为不可预期。
        本方法是 step_next / step_prev / step_reset / 双击行跳转
        的共同入口，策略放在这里只维护一份。
        """
        if self.state in (State.REPLAYING, State.PAUSED):
            self.stop_replay()
        if self.mode_combo.currentData() != MODE_STEP:
            self._initializing = True
            try:
                self.mode_combo.setCurrentIndex(1)
            finally:
                self._initializing = False
        self._sync_player_steps()

    def _sync_player_steps(self) -> None:
        """确保 ``_script_steps`` 与回放器持有最新（可能被编辑过）的步骤。

        正常情况下 :meth:`_set_steps` / :meth:`_sync_after_recording` 已经把两边
        对齐；这里主要兜住“外部直接改了事件、步骤列表还是空的”场景。
        """
        if not self._script_steps:
            self._script_steps = self._build_steps(self._current_events())
        events = self._current_events()
        if not events:
            return
        if (len(self.player.steps) != len(self._script_steps)
                or self.player.events != events):
            self.player.load(events, self.script_name_edit.currentText().strip())

    def _refresh_step_view(self) -> None:
        """刷新步骤进度与高亮。

        ``step_index`` 是「已执行步数」；回退到 0 时传 -1 取消列表高亮，
        否则会残留上一行的高亮（看起来像没退回去）。
        """
        index = self.player.step_index
        self.event_list.highlight(index - 1)
        self._refresh_status()

    def _on_row_activated(self, row: int) -> None:
        """双击列表行：直接跳到并执行该步骤（模式二语义）。"""
        if not self._current_events():
            return
        self._enter_step_mode()
        result = self.player.run_step_at(row)
        if result is None:
            return
        self._refresh_step_view()
        self._set_message(f"已跳转并执行步骤 {result}：{self._script_steps[result - 1].description}")

    # ==================================================================
    # 步骤编辑
    # ==================================================================
    def _build_steps(self, events: Optional[list[dict[str, Any]]] = None) -> list[ep.Step]:
        """按当前配置的自动合并开关把事件序列分组为步骤列表。

        统一入口：载入脚本、录制收尾、编辑后重算都走这里，
        保证「列表里看到的步骤」与设置页的开关始终一致。
        """
        return ep.build_steps(
            events if events is not None else self._script_events,
            self.config.merge_options(),
        )

    def _on_edit_action(self, action: str) -> None:
        """步骤编辑器工具栏动作统一入口。"""
        if self.state is State.RECORDING:
            self._set_message("正在录制中，无法编辑步骤。")
            return

        # 插入步骤：空脚本也允许（作为第 1 步插入）
        if action == "insert":
            self.insert_step_at(self.event_list.insert_position())
            return

        if not self._script_steps:
            self._set_message("没有可编辑的步骤，请先录制或载入脚本。")
            return

        if action == "save":
            self.save_edited_steps()
            return

        if action == "duration":
            self.reset_total_duration()
            return

        if action == "edit_step":
            self.edit_step_params(self.event_list.mode_row())
            return

        selected = self.event_list.checked_indices()
        if not selected:
            self._set_message("请先勾选要操作的步骤（可点行首复选框，或用“全选”）。")
            return

        if action == "delete":
            self._apply_step_edit(lambda steps: ep.delete_steps(steps, selected),
                                  f"已删除 {len(selected)} 个步骤")
        elif action == "move_up":
            self._move_steps(selected, -1)
        elif action == "move_down":
            self._move_steps(selected, 1)

    def edit_step_params(self, row: int) -> None:
        """编辑某一步的全部参数（动作/坐标、时间、长按、无限连击）。

        第十阶段（任务 2）：时间编辑已合并进本弹窗（起始时间 + 持续时长），
        不再有独立的「改这一步时间」按钮。

        编辑顺序：先重建事件内容（动作/坐标/键/高级选项）→ 再调持续时长
        → 最后调起始时间（后续步骤平移）。
        """
        if self.state is State.RECORDING:
            self._set_message("正在录制中，无法编辑步骤。")
            return
        if self.state in (State.REPLAYING, State.PAUSED):
            self._set_message("回放中无法编辑步骤，请先停止回放。")
            return
        if not (0 <= row < len(self._script_steps)):
            self._set_message("请先在列表里点选（或用复选框勾选）要编辑的步骤。")
            return

        step = self._script_steps[row]
        if step.kind not in ("click", "multi", "move", "scroll", "key", "combo"):
            self._set_message(f"暂不支持编辑「{step.kind_label}」类型的步骤。")
            return

        edit = edit_step_dialog(step, self)
        if edit is None:
            return

        # ---- 「拆分为独立单击」：把连击步骤还原成 N 个独立步骤 ----
        if edit.split_clicks:
            groups = ep.split_click_step(step)
            if len(groups) <= 1:
                self._set_message("这一步本来就是单击，无需拆分。")
                return
            try:
                edited = ep.replace_step_with_many(self._script_steps, row, groups,
                                                   kind="click")
            except ep.StepEditError as exc:
                self._set_message(str(exc))
                return
            total = len(self._build_steps(edited))
            self._apply_step_edit(lambda _steps: edited, "")
            self.event_list.set_checked([row])
            self._set_message(
                f"已把第 {row + 1} 步「{step.description}」拆分为 "
                f"{len(groups)} 个独立单击步骤（现共 {total} 步）。"
                f"拆分结果带边界标记，不会被自动合并粘回去。"
            )
            return

        # ---- 第一步：重建事件内容（动作/坐标/键 + 长按/无限连击元数据）----
        try:
            new_events = ep.rebuild_step_events(
                step,
                x=edit.x, y=edit.y,
                button=edit.button, click_count=edit.click_count,
                key=edit.key, vk=edit.vk,
                modifiers=edit.modifiers,
                dx=edit.dx, dy=edit.dy,
                explicit_hold_ms=edit.explicit_hold_ms,
                infinite_repeat=edit.infinite_repeat,
                repeat_interval_ms=edit.repeat_interval_ms,
                start_x=edit.start_x, start_y=edit.start_y,
                duration_ms=edit.duration_ms,
            )
        except ep.StepEditError as exc:
            self._set_message(str(exc))
            return

        def editor(steps: list[ep.Step]) -> list[dict[str, Any]]:
            return ep.replace_step(steps, row, new_events, kind=edit.kind)

        self._apply_step_edit(editor, "")

        # ---- 第二步：调整持续时长（内部等比缩放，后续平移）----
        old_duration = step.t_end - step.t_start
        events = self._script_events
        if edit.duration_ms is not None and edit.duration_ms != old_duration:
            try:
                events = ep.set_step_duration(events, row, edit.duration_ms)
            except ep.StepEditError as exc:
                self._set_message(str(exc))
                return
            self._set_steps(events, self._build_steps(events), [row])

        # ---- 第三步：调整起始时间（整体平移）----
        current_step = self._script_steps[row]
        if edit.start_ms is not None and edit.start_ms != current_step.t_start:
            try:
                events = ep.set_step_time(self._script_events, row, edit.start_ms)
            except ep.StepEditError as exc:
                self._set_message(str(exc))
                return
            self._set_steps(events, self._build_steps(events), [row])

        new_step = self._script_steps[row]
        old_desc = step.description
        new_desc = new_step.description

        # 附加信息：长按 / 无限连击
        extras = []
        if (edit.explicit_hold_ms is not None
                and edit.explicit_hold_ms != step.explicit_hold_ms):
            if edit.explicit_hold_ms > 0:
                extras.append(f"长按 {edit.explicit_hold_ms}ms")
            else:
                extras.append("取消长按")
        if (edit.infinite_repeat is not None
                and edit.infinite_repeat != bool(getattr(step, "infinite_repeat", False))):
            if edit.infinite_repeat:
                extras.append(f"无限连击（{edit.repeat_interval_ms}ms 间隔）")
            else:
                extras.append("取消无限连击")
        extra_str = "，" + "、".join(extras) if extras else ""

        self.event_list.set_checked([row])
        self._set_message(
            f"第 {row + 1} 步已修改：{old_desc} → {new_desc}{extra_str}"
            f"（{len(self._script_steps)} 步，时间戳已重排）。"
        )

    def _move_steps(self, selected: list[int], delta: int) -> None:
        """上移/下移选中的步骤，并在重排后恢复它们的勾选状态。"""
        before = list(self._script_steps)
        try:
            events, mapping = ep.move_steps(before, selected, delta)
        except ep.StepEditError as exc:
            self._set_message(str(exc))
            return

        # 用“身份签名”把原来的勾选映射到新下标（重排不改变步骤内容）
        signatures = [ep.step_signature(before[i]) for i in selected]
        new_steps = self._build_steps(events)
        restored: list[int] = []
        available = set(range(len(new_steps)))
        for signature in signatures:
            for i in sorted(available):
                if ep.step_signature(new_steps[i]) == signature:
                    restored.append(i)
                    available.discard(i)
                    break

        self._set_steps(events, new_steps, restored)
        direction = "上移" if delta < 0 else "下移"
        self._set_message(f"已把 {len(selected)} 个步骤{direction}一格，时间戳已自动重排。")

    def _apply_step_edit(self, editor, success_message: str) -> None:
        """执行一次步骤编辑：改数据 → 重排时间戳 → 同步界面。"""
        before = list(self._script_steps)
        try:
            events = editor(before)
        except ep.StepEditError as exc:
            self._set_message(str(exc))
            return
        self._set_steps(events, self._build_steps(events), [])
        self._set_message(f"{success_message}，时间戳已自动重排。")
        # 编辑后内容可能从无到有（或反之），按钮状态需要同步
        self._set_state(self.state)

    def _set_steps(self, events: list[dict[str, Any]], steps: list[ep.Step],
                   keep_checked: Optional[list[int]] = None) -> None:
        """把新的步骤序列落到所有相关位置（列表 / 回放器 / 脏标记）。

        这是「步骤数据变更」的唯一入口：编辑、载入、录制收尾都走这里，
        保证列表、回放器、状态栏三处的步骤数据始终一致。
        """
        self._script_events = list(events)
        self._script_steps = list(steps)
        self.player.load(self._script_events, self.script_name_edit.currentText().strip())
        self.event_list.set_steps(self._script_steps)
        if keep_checked:
            self.event_list.set_checked(keep_checked)
        self.event_list.highlight(-1)
        self._unsaved = True
        self._set_state(self.state)

    def apply_steps(self, events: list[dict[str, Any]],
                    mark_unsaved: bool = True) -> None:
        """外部载入步骤序列的公开入口（会同步列表 / 回放器 / 状态栏）。

        ``mark_unsaved=False`` 用于「载入磁盘脚本」这类不产生修改的场景。
        """
        steps = self._build_steps(events)
        self._script_events = list(events)
        self._script_steps = list(steps)
        self.player.load(self._script_events, self.script_name_edit.currentText().strip())
        self.event_list.set_steps(self._script_steps)
        self.event_list.highlight(-1)
        self._unsaved = mark_unsaved
        self._set_state(self.state)

    def insert_step_at(self, position: int) -> None:
        """弹出类型选择 → 编辑弹窗 → 插入新步骤。"""
        # 第一步：选择步骤类型
        type_dialog = InsertStepDialog(self, position=position)
        if type_dialog.exec() != InsertStepDialog.Accepted:
            return
        kind = type_dialog.selected_kind()

        # 第二步：构造默认事件 + 临时 Step，弹出对应的编辑弹窗
        try:
            default_events, default_desc = ep.make_step_events(kind)
        except ep.StepEditError as exc:
            self._set_message(str(exc))
            return

        # 用 build_steps 把默认事件转成 Step（单步），供编辑弹窗使用
        temp_steps = ep.build_steps(default_events)
        if not temp_steps:
            self._set_message("无法创建该类型的步骤。")
            return
        temp_step = temp_steps[0]

        edit = edit_step_dialog(temp_step, self)
        if edit is None:
            return

        # 第三步：用编辑结果重建事件
        try:
            new_events = ep.rebuild_step_events(
                temp_step,
                x=edit.x, y=edit.y,
                button=edit.button, click_count=edit.click_count,
                key=edit.key, vk=edit.vk,
                modifiers=edit.modifiers,
                dx=edit.dx, dy=edit.dy,
                explicit_hold_ms=edit.explicit_hold_ms,
                infinite_repeat=edit.infinite_repeat,
                repeat_interval_ms=edit.repeat_interval_ms,
                start_x=edit.start_x, start_y=edit.start_y,
                duration_ms=edit.duration_ms,
            )
        except ep.StepEditError as exc:
            self._set_message(str(exc))
            return

        # 第四步：插入到指定位置
        try:
            events, index = ep.insert_step(
                self._script_steps, position, new_events,
                description=temp_step.description)
        except ep.StepEditError as exc:
            self._set_message(str(exc))
            return
        self._set_steps(events, self._build_steps(events), [index])
        total = len(self._script_steps)
        self._set_message(
            f"已在第 {index + 1} 步插入新步骤（共 {total} 步），时间戳已自动重排。"
        )

    # edit_step_time 已合并到 edit_step_params（编辑这一步弹窗内含时间区），
    # 时间列双击与右键「编辑这一步」统一走 edit_step_params。

    def reset_total_duration(self) -> None:
        """弹窗输入目标总时长，把整段脚本的步骤均匀重排。"""
        if self.state is State.RECORDING:
            self._set_message("正在录制中，无法重设总时长。")
            return
        if not self._script_steps:
            self._set_message("没有可调整的步骤，请先录制或载入脚本。")
            return
        if self.state in (State.REPLAYING, State.PAUSED):
            self._set_message("回放中无法重设总时长，请先停止回放。")
            return

        current = ep.events_duration_ms(self._script_events)
        target = ask_total_duration(current, len(self._script_steps), self)
        if target is None:
            return
        events = ep.redistribute_duration(self._script_events, target)
        self._set_steps(events, self._build_steps(events), [])
        self._set_message(
            f"已把 {len(self._script_steps)} 个步骤均匀铺开到 "
            f"{ep.format_duration(ep.events_duration_ms(events))}"
            f"（原 {ep.format_duration(current)}），可点「保存修改」写回脚本。"
        )

    def save_edited_steps(self) -> Optional[Path]:
        """把编辑后的步骤序列写回脚本文件。

        若目标是**已存在的脚本文件**且当前脚本名与它不一致（例如在录制页
        改了名字后点保存），先弹覆盖确认，避免把别的脚本覆盖掉。
        """
        if not self._script_steps:
            self._set_message("没有可保存的步骤。")
            return None
        if self._current_script is not None:
            if not self._confirm_overwrite(self._current_script):
                return None
            path = self._save_to_path(self._current_script)
            if path is not None:
                self._set_message(f"已保存修改：{path.name}（{len(self._script_steps)} 步）")
            return path
        # 还没有对应文件（刚录制完）：走常规保存流程
        return self.save_script()

    def _confirm_overwrite(self, target: Path) -> bool:
        """覆盖已存在脚本前征求确认。

        只有「目标就是当前正在编辑的那个脚本」才算正常保存，不再多问；
        其余情况（例如在录制页把名字改成另一个已存在的脚本名再保存）
        都要确认，避免误覆盖别人的脚本。
        """
        target = Path(target)
        if not target.exists():
            return True
        if self._current_script is not None and Path(self._current_script) == target:
            return True
        answer = QMessageBox.question(
            self, "覆盖确认",
            f"脚本目录里已经存在「{target.name}」。\n确定要覆盖它吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self._set_message("已取消保存（未覆盖已有脚本）。")
            return False
        return True

    def _save_to_path(self, path: Path, silent: bool = False,
                      ask_overwrite: bool = False) -> Optional[Path]:
        """把当前步骤写回指定文件。"""
        events = self._current_events()
        if not events:
            return None
        if ask_overwrite and not self._confirm_overwrite(Path(path)):
            return None
        name = self.script_name_edit.currentText().strip() or path.stem
        try:
            saved = ep.save_script(path, name, events)
        except ep.ScriptError as exc:
            self._on_error(str(exc))
            return None
        self._current_script = saved
        self._unsaved = False
        self.config.set("last_script", str(saved))
        self.config.save()
        self.refresh_script_library()
        if not silent:
            self._set_message(f"已保存脚本：{saved.name}")
        return saved

    # ==================================================================
    # 脚本
    # ==================================================================
    def save_script(self, silent: bool = False) -> Optional[Path]:
        """把当前步骤保存到脚本目录（文件名取脚本名称）。"""
        events = self._current_events()
        if not events:
            if not silent:
                self._set_message("没有可保存的内容，请先录制。")
            return None
        name = self.script_name_edit.currentText().strip() or self._default_script_name()
        # 这里只是把补全后的名字回填到输入框，不能因此把脚本标记成「已修改」
        self.set_script_name(name, mark_dirty=False)
        target = ensure_dir(self.config.scripts_path()) / (_safe_filename(name) + ".json")
        return self._save_to_path(target, silent=silent, ask_overwrite=True)

    def save_script_as(self) -> None:
        """另存为指定路径。"""
        events = self._current_events()
        if not events:
            self._set_message("没有可保存的内容，请先录制。")
            return
        start_dir = str(self.config.scripts_path())
        default_name = _safe_filename(self.script_name_edit.currentText().strip()
                                      or self._default_script_name()) + ".json"
        path_str, _filter = QFileDialog.getSaveFileName(
            self, "另存为脚本", str(Path(start_dir) / default_name), "MacroSmith 脚本 (*.json)"
        )
        if not path_str:
            return
        self._save_to_path(Path(path_str), ask_overwrite=True)

    def load_script_dialog(self) -> None:
        """从磁盘载入脚本 JSON。"""
        if self.state is State.RECORDING:
            self._set_message("正在录制中，无法载入脚本。")
            return
        start_dir = str(self.config.scripts_path())
        path_str, _filter = QFileDialog.getOpenFileName(
            self, "载入脚本", start_dir, "MacroSmith 脚本 (*.json);;所有文件 (*)"
        )
        if not path_str:
            return
        self.load_script_file(Path(path_str))

    def load_script_file(self, path: Path) -> Optional[Path]:
        """载入指定脚本文件并刷新界面。成功返回路径，失败返回 None。"""
        try:
            payload = ep.load_script(path)
        except ep.ScriptError as exc:
            self._on_error(str(exc))
            return None
        events = payload.get("events", [])
        name = str(payload.get("name") or Path(path).stem)
        self._script_events = list(events)
        self._script_steps = self._build_steps(self._script_events)
        self.player.load(self._script_events, name)
        self.event_list.set_steps(self._script_steps)
        self.set_script_name(name, mark_dirty=False)
        self._current_script = Path(path)
        self._unsaved = False
        self.config.set("last_script", str(path))
        self.config.save()
        duration = ep.events_duration_ms(self._script_events) / 1000.0
        self._set_message(
            f"已载入 {Path(path).name}：{len(self._script_events)} 条事件 / "
            f"{len(self._script_steps)} 步，时长 {duration:.2f} 秒。"
        )
        # 载入后内容从无到有，按钮可用状态要跟着刷新
        self._set_state(self.state)
        return Path(path)

    def clear_events(self) -> None:
        """清空当前事件与步骤指针。"""
        if self.state is State.RECORDING:
            self._set_message("正在录制中，无法清空。")
            return
        if self._current_events() and self._unsaved:
            answer = QMessageBox.question(
                self, "清空步骤",
                "当前内容尚未保存，确定要清空吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.player.clear()
        self.event_list.clear()
        self._script_events = []
        self._script_steps = []
        self._current_script = None
        self._unsaved = False
        self._set_message("已清空步骤列表。")
        # 内容清空后“开始回放”等按钮要变成禁用
        self._set_state(self.state)

    # ==================================================================
    # 脚本库
    # ==================================================================
    def refresh_script_library(self) -> None:
        """重新扫描脚本目录并刷新脚本库界面 + 顶部历史脚本下拉框。"""
        directory = self.config.scripts_path()
        self.page_library.set_directory_hint(directory)
        paths = ep.list_scripts(directory)
        self.page_library.set_scripts(paths)
        self._refresh_script_combo(paths)

    # ==================================================================
    # 顶部历史脚本下拉框
    # ==================================================================
    def _refresh_script_combo(self, paths: Optional[list[Path]] = None) -> None:
        """把脚本目录下的 JSON 列进顶部下拉框（保留用户正在输入的名称）。"""
        combo = getattr(self, "script_name_edit", None)
        if combo is None:
            return
        if paths is None:
            paths = ep.list_scripts(self.config.scripts_path())

        self._combo_guard = True
        try:
            current = combo.currentText().strip()
            combo.clear()
            names = [p.stem for p in paths]
            for path in paths:
                combo.addItem(path.stem, str(path))
            # 正在编辑的名称不在列表里时也保留下来（用户可能想「另存为」）
            if current and current not in names:
                combo.insertItem(0, current, None)
            combo.setCurrentText(current)
        finally:
            self._combo_guard = False
            self._combo_user_typed = False

    def _on_script_combo_activated(self, index: int) -> None:
        """用户从下拉框里选了一个历史脚本：立即载入到主界面。"""
        if getattr(self, "_combo_guard", False) or index < 0:
            return
        combo = self.script_name_edit
        # 过滤伪激活：只有「下拉列表刚关闭」（用户在列表里点选）或
        # 「手输后回车」才算真实选择，其余一律回滚文本、不加载。
        picked = getattr(combo, "_user_picked", False)
        combo._user_picked = False  # 消费 flag
        typed = getattr(self, "_combo_user_typed", False)
        if not (picked or typed):
            self._sync_script_combo_text()
            return
        self._combo_user_typed = False
        path_str = combo.itemData(index)
        if not path_str:
            return
        if self.state is State.RECORDING:
            self._set_message("正在录制中，无法加载脚本。")
            self._sync_script_combo_text()
            return
        if self.state in (State.REPLAYING, State.PAUSED):
            self.stop_replay()
        path = Path(str(path_str))
        if self.load_script_file(path) is not None:
            self._set_message(
                f"已从下拉框载入：{path.name}，可直接回放或继续编辑。"
            )

    def _sync_script_combo_text(self) -> None:
        """把下拉框文本同步成「当前脚本名」（取消选择后回滚显示用）。"""
        combo = getattr(self, "script_name_edit", None)
        if combo is None:
            return
        name = (self._current_script.stem if self._current_script is not None
                else combo.currentText().strip())
        self._combo_guard = True
        try:
            combo.setCurrentText(name)
        finally:
            self._combo_guard = False
            self._combo_user_typed = False

    def script_name(self) -> str:
        """当前脚本名称（下拉框可编辑部分的文本，已去空白）。"""
        return self.script_name_edit.currentText().strip()

    def _on_script_name_edited(self, _text: str) -> None:
        """用户手动改了脚本名：置脏标记（程序回填时由 ``_combo_guard`` 抑制）。"""
        if getattr(self, "_combo_guard", False):
            return
        self._mark_unsaved(True)

    def _on_script_name_typed(self, _text: str) -> None:
        """用户手输了脚本名：标记为真实输入（回车提交时视为有效激活）。"""
        self._combo_user_typed = True

    def set_script_name(self, name: str, mark_dirty: bool = True) -> None:
        """设置脚本名称。

        ``mark_dirty=False`` 用于「载入脚本 / 保存后回填」这类不该产生
        未保存标记的场景 —— QComboBox 内部是 QLineEdit，
        ``combo.blockSignals()`` 挡不住 ``editTextChanged``，必须用守卫标志。
        """
        combo = self.script_name_edit
        self._combo_guard = True
        try:
            combo.setCurrentText(str(name))
        finally:
            self._combo_guard = False
            self._combo_user_typed = False
        if mark_dirty:
            self._mark_unsaved(True)

    def load_selected_script(self) -> None:
        """「加载该脚本」：把选中的脚本载入并准备回放（停留在本页）。

        与旧行为（跳转到录制页）不同：现在直接留在「回放与脚本」页，
        右侧预览就是该脚本内容，点「开始回放」即可。有未保存修改时先确认。
        """
        path = self.page_library.selected_path()
        if path is None:
            self._set_message("请先在列表里选择一个脚本。")
            return
        if self.state is State.RECORDING:
            self._set_message("正在录制中，无法加载脚本。")
            return
        if self._current_script == path:
            self._set_message(f"「{path.name}」已经是当前脚本，可直接点「开始回放」。")
            return
        if not self._confirm_discard_unsaved("加载脚本"):
            return
        if self.load_script_file(path) is not None:
            # 停留在本页，方便直接回放
            self._show_page(PAGE_SCRIPTS)
            self._set_message(
                f"已加载：{path.name}（{len(self._script_steps)} 步），"
                f"点「开始回放」即可执行。"
            )

    def create_script(self) -> None:
        """新建一个空脚本（弹窗输入名称 → 建文件 → 加入下拉框并选中）。

        如果当前有未保存修改，先确认，避免误丢。
        """
        if self.state is State.RECORDING:
            self._set_message("正在录制中，无法新建脚本。")
            return
        if not self._confirm_discard_unsaved("新建脚本"):
            return

        directory = ensure_dir(self.config.scripts_path())
        default_name = self._default_script_name()
        name = ask_name(
            "新建脚本", "输入新脚本的名称：", initial=default_name,
            placeholder="例如：登录流程",
            hint="脚本会以该名称保存到脚本目录，创建后可以直接录制或插入步骤。",
            existing=script_stems(directory),
            parent=self,
        )
        if not name:
            return

        target = directory / (_safe_filename(name) + ".json")
        try:
            ep.save_script(target, name, [])
        except ep.ScriptError as exc:
            self._on_error(str(exc))
            return

        self.refresh_script_library()
        self.page_library.select_path(target)
        self.load_script_file(target)
        self._show_page(PAGE_RECORD)
        self._set_message(f"已新建空脚本：{target.name}，可直接录制或插入步骤。")
    def rename_selected_script(self) -> None:
        """重命名选中的脚本文件。"""
        path = self.page_library.selected_path()
        if path is None:
            self._set_message("请先在脚本库中选择一个脚本。")
            return
        directory = path.parent
        new_name = ask_name(
            "重命名脚本", f"把「{path.stem}」重命名为：", initial=path.stem,
            hint="脚本内的名称字段会一并同步。",
            existing=script_stems(directory),
            ignore={path.stem, (self._current_script.stem
                                if self._current_script else path.stem)},
            parent=self,
        )
        if not new_name or new_name == path.stem:
            return
        target = path.with_name(_safe_filename(new_name) + ".json")
        if target.exists():
            QMessageBox.warning(self, "重命名脚本", f"目标文件已存在：{target.name}")
            return
        try:
            path.rename(target)
        except OSError as exc:
            self._on_error(f"重命名失败：{exc}")
            return
        # 同步脚本内的 name 字段，避免与文件名不一致
        try:
            payload = ep.load_script(target)
            ep.save_script(target, new_name, payload.get("events", []),
                           int(payload.get("duration_ms") or 0))
        except ep.ScriptError as exc:
            log.warning("重命名后同步 name 字段失败：%s", exc)

        if self._current_script == path:
            self._current_script = target
            self.set_script_name(new_name, mark_dirty=False)
        self.refresh_script_library()
        self.page_library.select_path(target)
        self._set_message(f"已重命名为：{target.name}")

    def delete_selected_script(self) -> None:
        """删除选中的脚本文件（需要确认）。"""
        path = self.page_library.selected_path()
        if path is None:
            self._set_message("请先在脚本库中选择一个脚本。")
            return
        answer = QMessageBox.question(
            self, "删除脚本",
            f"确定要删除脚本「{path.stem}」吗？\n\n此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            path.unlink()
        except OSError as exc:
            self._on_error(f"删除失败：{exc}")
            return
        if self._current_script == path:
            self._current_script = None
        self.refresh_script_library()
        self._set_message(f"已删除脚本：{path.name}")

    # ==================================================================
    # 设置页联动
    # ==================================================================
    def _save_hotkeys(self, hotkeys: dict[str, str]) -> None:
        """保存热键：先整体校验（可解析 + 不重复），通过后再落盘并重新注册。

        校验不通过时**不做任何修改**，避免出现“一半新一半旧”的绑定状态。
        """
        parsed = {action: parse_key(token) for action, token in hotkeys.items()}
        bad = [action for action, key in parsed.items() if key is None]
        if bad:
            names = "、".join(HOTKEY_LABELS.get(a, (a, ""))[0] for a in bad)
            QMessageBox.warning(self, "热键无法识别",
                                f"以下热键无法识别：\n{names}\n\n"
                                "请使用功能键（F1–F24）、PageUp/PageDown、Home 或单个字母数字键。")
            return

        seen: dict[Any, list[str]] = {}
        for action, key in parsed.items():
            seen.setdefault(key, []).append(action)
        duplicates = {key: actions for key, actions in seen.items() if len(actions) > 1}
        if duplicates:
            detail = "\n".join(
                "、".join(HOTKEY_LABELS.get(a, (a, ""))[0] for a in actions)
                + f"  →  {display_name(hotkeys[actions[0]])}"
                for actions in duplicates.values()
            )
            QMessageBox.warning(self, "热键重复",
                                f"以下动作使用了相同的按键：\n{detail}\n\n请改成不同的按键。")
            return

        for action, token in hotkeys.items():
            self.config.set_hotkey(action, token)
        self.config.save()
        self.page_settings.load_hotkeys(hotkeys)
        self._register_hotkeys()
        self._set_message("热键已更新并重新注册。")
        self._refresh_status()

    def _save_options(self, options: dict[str, Any]) -> None:
        """保存录制/回放选项（设置页入口）。"""
        for key, value in options.items():
            self.config.set(f"player.{key}", value)
        self.config.save()
        self._apply_options_from_config()
        self._set_message("设置已保存。")
    def _on_exclusive_toggled(self, exclusive: bool) -> None:
        """切换「热键独占模式」：写配置 → 重挂热键 → 反馈结果。"""
        self.config.set_hotkey_exclusive(exclusive)
        self.config.save()
        self._register_hotkeys()
        if not self.config.hotkey_exclusive:
            self._set_message(
                "已关闭热键独占模式：热键仍然可用，但 F6/F9 等按键也会同时触发前台软件的功能。"
            )
            return
        if self.hotkeys.is_running:
            self._set_message(
                "已开启热键独占模式：命中热键时系统不再把按键传给其它程序。"
            )
            QTimer.singleShot(800, self._check_exclusive_health)
        else:
            err = getattr(self.hotkeys, "_last_error", "") or "未知原因"
            self._set_message(
                f"热键独占模式启动失败（{err}）。已保留普通监听，热键仍可用但不会独占。"
            )

    def _on_merge_changed(self, combo: bool, multi_click: bool) -> None:
        """切换「自动合并组合键 / 双击三击」：写配置并立即重算步骤列表。"""
        self.config.set_merge(combo=combo, multi_click=multi_click)
        self.config.save()
        self.reapply_merge()

    def reapply_merge(self) -> None:
        """按当前合并开关重算步骤列表（保留原始事件与时间戳）。"""
        if not self._script_events:
            state = "开启" if self.config.merge_combo else "关闭"
            state2 = "开启" if self.config.merge_multi_click else "关闭"
            self._set_message(
                f"已更新合并设置（组合键 {state} / 双击三击 {state2}）。"
            )
            return
        if self.state in (State.REPLAYING, State.PAUSED):
            self._set_message("回放中无法改变合并方式，请先停止回放。")
            return

        before = len(self._script_steps)
        events = self.strip_boundaries(self._script_events)
        # 用户手工拆分过的步骤带边界标记，重新合并时不能被跨越
        boundary_map = [bool(e.get(ep.BOUNDARY_KEY)) for e in self._script_events]
        for i, flag in enumerate(boundary_map):
            if flag and i < len(events):
                events[i][ep.BOUNDARY_KEY] = True

        steps = self._build_steps(events)
        self._script_events = events
        self._script_steps = steps
        self.player.load(self._script_events, self.script_name_edit.currentText().strip())
        self.event_list.set_steps(self._script_steps)
        self.event_list.highlight(-1)
        self._set_state(self.state)

        combo_state = "开启" if self.config.merge_combo else "关闭"
        multi_state = "开启" if self.config.merge_multi_click else "关闭"
        self._set_message(
            f"已更新合并设置（组合键 {combo_state} / 双击三击 {multi_state}）："
            f"步骤 {before} → {len(self._script_steps)} 步。"
        )

    @staticmethod
    def strip_boundaries(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """复制一份事件序列并去掉内部边界标记（保留其它字段）。"""
        return [
            {k: v for k, v in e.items() if k != ep.BOUNDARY_KEY}
            for e in events if isinstance(e, dict)
        ]

    def _apply_options_from_config(self) -> None:
        """把配置应用到运行时对象与界面。"""
        self.player.speed = float(self.config.get("player.speed", 1.0) or 1.0)
        self.page_settings.load_from_config(self.config)
        # 循环回放设置
        self.player.set_loop(self.config.loop_mode, self.config.loop_count)
        self.page_library.set_loop(self.config.loop_mode, self.config.loop_count)
        mode = self.config.get("player.default_mode", MODE_TIMED)
        self._initializing = True
        try:
            self.mode_combo.setCurrentIndex(1 if mode == MODE_STEP else 0)
        finally:
            self._initializing = False
    def _on_mode_changed(self, _index: int) -> None:
        """切换回放模式：写回配置并给出对应提示。

        第九阶段策略：用户通过下拉框切换模式时，若模式一仍在回放，
        先停止当前回放（指针归零），再等用户用新模式的热键操作 ——
        两个执行通道互斥，与 _enter_step_mode 一致。
        """
        self.config.set("player.default_mode", self.mode_combo.currentData())
        self.config.save()
        if getattr(self, "_initializing", False):
            return
        if self.state in (State.REPLAYING, State.PAUSED):
            self.stop_replay()
        if self.mode_combo.currentData() == MODE_STEP:
            self._set_message("已切换到模式二（单步热键执行）：PageDown 下一步，PageUp 上一步。")
        else:
            self._set_message("已切换到模式一（原速回放）：F6 开始，F7 暂停/继续，F8 停止。")
        self._refresh_status()

    def _ensure_mode(self, mode: str, action: str) -> bool:
        """确保处于指定模式后再执行动作；模式二的动作不允许在此入口触发。"""
        if self.mode_combo.currentData() == mode:
            return True
        if mode == MODE_TIMED:
            self._set_message(f"{action}会自动切回模式一（原速回放）。")
            self._initializing = True
            try:
                self.mode_combo.setCurrentIndex(0)
            finally:
                self._initializing = False
            self.config.set("player.default_mode", MODE_TIMED)
            self._refresh_status()
            return True
        return False

    # ==================================================================
    # 状态与刷新
    # ==================================================================
    def _set_state(self, state: State) -> None:
        """状态机切换：统一刷新按钮可用性与侧边栏动画。"""
        self.state = state
        recording = state is State.RECORDING
        replaying = state in (State.REPLAYING, State.PAUSED)
        has_content = bool(self._current_events())
        editing = not recording and not replaying

        self.btn_start_record.setEnabled(editing)
        self.btn_stop_record.setEnabled(recording)
        self.btn_start_play.setEnabled(editing and has_content)
        self.btn_stop_play.setEnabled(replaying)
        self.mode_combo.setEnabled(editing)
        self.btn_reset_step.setEnabled(editing)
        self.btn_clear.setEnabled(editing)
        self.script_name_edit.setEnabled(editing)
        self.btn_save_as.setEnabled(editing)
        self.btn_load.setEnabled(editing)
        self.btn_new_script.setEnabled(editing)
        # 回放/录制过程中禁用步骤编辑，避免改到正在执行的数据
        self.event_list.set_editing_enabled(editing)
        self.page_library.btn_new.setEnabled(editing)
        self.page_library.btn_rename.setEnabled(editing and self.page_library.selected_path() is not None)
        self.page_library.btn_delete.setEnabled(editing and self.page_library.selected_path() is not None)
        self.page_library.btn_load.setEnabled(editing and self.page_library.selected_path() is not None)

        self.sidebar.set_recording(recording)
        if recording:
            self.sidebar.set_active("record")

        if state is State.PAUSED:
            self.btn_start_play.setText("继续回放")
        else:
            self.btn_start_play.setText("开始回放")
        self._refresh_status()

    def _refresh_status(self) -> None:
        """刷新状态栏的模式、步骤进度、事件数、循环轮数与热键提示。"""
        mode = self.mode_combo.currentData()
        mode_text = {
            State.RECORDING: "录制中",
            State.REPLAYING: "回放中",
            State.PAUSED: "已暂停",
        }.get(self.state, "就绪")
        mode_name = "模式二 · 单步热键" if mode == MODE_STEP else "模式一 · 原速回放"
        self.mode_label.setText(f"当前模式：{mode_name}（{mode_text}）")

        total = len(self._script_steps)
        index = self.player.step_index
        self.step_label.setText(f"步骤 {index} / {total}")

        # 循环轮数：回放中显示实时轮次，空闲时显示即将使用的循环设置
        round_text = ""
        if self.state in (State.REPLAYING, State.PAUSED):
            round_text = self.player.loop_progress_text()
        elif self.player.loop_mode != "none":
            if self.player.loop_mode == "fixed":
                round_text = f"循环 {self.player.loop_count} 轮"
            else:
                round_text = "无限循环"
        self.loop_label.setText(round_text)
        self.loop_label.setVisible(bool(round_text))

        events = self._current_events()
        duration = ep.events_duration_ms(events) / 1000.0
        self.event_count_label.setText(
            f"{len(events)} 条事件 · {total} 步 · 时长 {duration:.2f}s"
            + ("　·　未保存" if self._unsaved else "")
        )
        # 「回放与脚本」页的镜像状态
        self.page_library.set_playing(self.state in (State.REPLAYING, State.PAUSED),
                                      self.state is State.PAUSED)
        self.page_library.set_round(round_text)
        self.page_library.set_current_script(
            self._current_script.name if self._current_script else "",
            len(self._current_events()), total, ep.events_duration_ms(events),
        )
        self._refresh_hotkey_hint()

    def _refresh_hotkey_hint(self) -> None:
        """按当前状态拼出底部灰色热键提示。"""
        def key(action: str) -> str:
            return self.hotkeys.binding_display(action) or action

        parts: list[str] = []
        if self.state is State.RECORDING:
            parts.append(f"{key('record_stop')} 停止录制")
        elif self.state in (State.REPLAYING, State.PAUSED):
            parts.append(f"{key('play_toggle')} 暂停/继续")
            parts.append(f"{key('play_stop')} 停止回放")
        elif self.mode_combo.currentData() == MODE_STEP:
            parts.append(f"{key('step_next')} 下一步")
            parts.append(f"{key('step_prev')} 上一步")
            parts.append(f"{key('step_reset')} 重置指针")
        else:
            parts.append(f"{key('play_toggle')} 开始回放")
            parts.append(f"{key('pause_toggle')} 暂停/继续")
            parts.append(f"{key('play_stop')} 停止回放")
            parts.append(f"{key('step_next')}/{key('step_prev')} 单步")
            parts.append(f"{key('record_stop')} 停止录制")
        text = "　·　".join(parts)
        if self._hotkey_failures:
            text += "　⚠ 部分热键注册失败，请到设置页修改"
        self.hotkey_label.setText(f"热键：{text}")

    def _set_message(self, text: str) -> None:
        """更新状态栏消息。"""
        self.message_label.setText(text)
        log.debug("status: %s", text)

    def _current_events(self) -> list[dict[str, Any]]:
        """当前界面上的事件序列。

        以列表组件持有的数据为准（录制增量与载入都会同步过去），
        这样界面显示与回放内容永远一致。
        """
        events = self.event_list.events
        if events:
            return list(events)
        return list(self._script_events)

    def _mark_unsaved(self, unsaved: bool) -> None:
        self._unsaved = unsaved

    def _confirm_discard_unsaved(self, action: str) -> bool:
        """有未保存修改时先征求用户同意（切脚本 / 新建 / 清空前调用）。

        返回 ``True`` 表示可以继续该操作。选「保存」时会先落盘；
        保存失败或用户取消都返回 ``False``。
        """
        if not self._unsaved or not self._current_events():
            return True
        answer = QMessageBox.question(
            self, action,
            f"当前脚本「{self.script_name() or '未命名'}」有未保存的修改。\n"
            f"要先保存再{action}吗？",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer == QMessageBox.Cancel:
            self._set_message(f"已取消{action}。")
            return False
        if answer == QMessageBox.Save:
            saved = (self._save_to_path(self._current_script, silent=True)
                     if self._current_script is not None
                     else self.save_script(silent=True))
            if saved is None:
                self._set_message("保存失败，已取消操作。")
                return False
        return True

    def _default_script_name(self) -> str:
        import time
        return time.strftime("录制-%Y%m%d-%H%M%S")

    def _show_page(self, index: int) -> None:
        """切换主内容区页面并同步侧边栏高亮。"""
        self.pages.setCurrentIndex(index)
        self.sidebar.set_active({
            PAGE_SCRIPTS: "library", PAGE_RECORD: "record",
            PAGE_SETTINGS: "settings", PAGE_ABOUT: "about",
        }.get(index, "library"))
        if index == PAGE_SETTINGS:
            self.page_settings.load_from_config(self.config)
        elif index == PAGE_SCRIPTS:
            self.refresh_script_library()
            self.page_library.set_playing(self.state in (State.REPLAYING,
                                                         State.PAUSED))
            self.page_library.set_current_script(
                self._current_script.name if self._current_script else "",
                len(self._script_events), len(self._script_steps),
                ep.events_duration_ms(self._script_events))

    # ==================================================================
    # 定时批量刷新（把监听线程的数据搬到界面）
    # ==================================================================
    def _flush_ui_buffers(self) -> None:
        """把缓冲的录制事件与回放高亮一次性刷进界面。

        录制时鼠标移动事件频率很高，逐条刷新会导致界面明显卡顿，
        因此统一由 120ms 的定时器批量处理。
        """
        if self._pending_records:
            batch = self._pending_records
            self._pending_records = []
            self._script_events.extend(batch)
            self._script_steps = self._build_steps(self._script_events)
            self.event_list.set_steps(self._script_steps)
            self.event_list.highlight(-1)
            self._unsaved = True
            self.event_count_label.setText(
                f"正在录制：{len(self._script_events)} 条事件 / {len(self._script_steps)} 步"
            )

        if self._pending_highlights:
            # BUG 修复：模式一的进度必须和高亮一起刷新。
            # 回放线程不会触发状态机切换，之前只有 highlight 在变、
            # 状态栏一直停在「步骤 0 / N」，看起来像没在回放。
            # 这里不再按 state 过滤：回放刚结束的那一批也需要落到界面上。
            row = self._step_row_for_raw(self._pending_highlights[-1])
            self.event_list.highlight(row)
            self._update_step_progress(row)
            self._pending_highlights = []

        # 循环轮次实时刷新：_loop_round 在回放线程里递增，不会触发
        # 状态机切换，_refresh_status() 不会跟着调用，轮次文本会停在
        # 初始值。这里每 120ms 比对一次，文本变了才写界面。
        if self.state in (State.REPLAYING, State.PAUSED):
            round_text = self.player.loop_progress_text()
            if round_text != self.loop_label.text():
                self.loop_label.setText(round_text)
                self.loop_label.setVisible(bool(round_text))
                self.page_library.set_round(round_text)

    def _step_row_for_raw(self, raw_index: int) -> int:
        """把模式一的“原始事件下标”换算成步骤列表行号。"""
        for row, step in enumerate(self._script_steps):
            if step.raw_start <= raw_index < step.raw_end:
                return row
        return -1

    def _update_step_progress(self, row: Optional[int] = None) -> None:
        """刷新底部状态栏的「步骤 x / N」。

        ``row`` 为模式一当前高亮的行号（0 基）；为 None 时取模式二指针。
        """
        total = len(self._script_steps)
        if row is not None and row >= 0:
            current = row + 1
        else:
            current = self.player.step_index
        self.step_label.setText(f"步骤 {current} / {total}")

    # ==================================================================
    # 窗口几何与关闭
    # ==================================================================
    def _restore_window_geometry(self) -> None:
        """从配置恢复窗口尺寸与位置（校验是否仍在可见屏幕内）。

        窗口构造阶段先调用一次并设置位置；尺寸在布局首次计算完成后
        再补一次 ``resize``，否则会被布局的最小尺寸顶到更大。
        用户从未调整过窗口时使用 :attr:`WindowShell.DEFAULT_WIDTH/HEIGHT`
        （1000x700）；调整过则 ``window.width`` / ``window.height``
        已在 :meth:`_save_window_geometry` 里写进 config.json，这里直接沿用。
        """
        from utils.win_utils import primary_screen_size

        default_w = self.DEFAULT_WIDTH
        default_h = self.DEFAULT_HEIGHT
        width = int(self.config.get("window.width", default_w) or default_w)
        height = int(self.config.get("window.height", default_h) or default_h)
        width = max(self.MIN_WIDTH, width)
        height = max(self.MIN_HEIGHT, height)
        self.resize(width, height)

        x = self.config.get("window.x")
        y = self.config.get("window.y")
        screen_w, screen_h = primary_screen_size()
        if isinstance(x, int) and isinstance(y, int):
            # 位置越界（例如换过显示器）时回退到居中
            if -50 <= x <= screen_w - 100 and -20 <= y <= screen_h - 100:
                self.move(x, y)
            else:
                self._center_on_screen()
        else:
            self._center_on_screen()

        # 等布局算完再设一次，保证默认就是 1000x700
        self._target_size = (width, height)
        QTimer.singleShot(0, self._apply_target_size)

    def _apply_target_size(self) -> None:
        """应用目标尺寸（只在用户没有手动改变过窗口大小时生效一次）。"""
        if getattr(self, "_target_size", None) is None:
            return
        width, height = self._target_size
        self._target_size = None
        if not self.isMaximized():
            self.resize(width, height)

    def _center_on_screen(self) -> None:
        from utils.win_utils import primary_screen_size

        screen_w, screen_h = primary_screen_size()
        self.move(max(0, (screen_w - self.width()) // 2), max(0, (screen_h - self.height()) // 2))

    def _save_window_geometry(self) -> None:
        """把窗口位置尺寸写回配置。

        原生窗口注意点：``move()`` 移动的是**含边框**的整体
        （frameGeometry），而 ``geometry()`` 是客户区，两者在
        Windows 上差一个边框宽度。位置必须存 frameGeometry，
        否则每次保存/恢复都会把窗口向右下"漂移"几像素。
        尺寸仍存客户区（geometry），与恢复时的 resize() 语义一致。
        """
        if self.isMinimized():
            return
        frame = self.frameGeometry()
        geo = self.geometry()
        self.config.set("window.x", int(frame.x()))
        self.config.set("window.y", int(frame.y()))
        self.config.set("window.width", int(geo.width()))
        self.config.set("window.height", int(geo.height()))
        self.config.save()

    def closeEvent(self, event) -> None:  # noqa: N802
        """关闭前：停止录制/回放、保存窗口位置、提示未落盘的修改。"""
        if self.state is State.RECORDING:
            self.stop_recording()
        if self.state in (State.REPLAYING, State.PAUSED):
            self.stop_replay()
        if self._unsaved and self._current_events():
            answer = QMessageBox.question(
                self, "退出 宏匠",
                "当前步骤尚未保存，退出前要保存吗？",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if answer == QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.Save:
                if self._current_script is not None:
                    self._save_to_path(self._current_script, silent=True)
                else:
                    self.save_script(silent=True)
        try:
            self.hotkeys.stop()
        except Exception:  # pragma: no cover
            pass
        self._save_window_geometry()
        super().closeEvent(event)

    # ==================================================================
    # 错误提示
    # ==================================================================
    def _on_error(self, message: str) -> None:
        """统一错误出口：写日志 + 状态栏 + 弹窗。"""
        log.error(message)
        self._set_message(f"错误：{message}")
        QMessageBox.warning(self, "MacroSmith 提示", message)


def _safe_filename(name: str) -> str:
    """把脚本名转成安全的文件名（去掉 Windows 非法字符）。"""
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in invalid else ch for ch in name).strip(" .")
    return cleaned or "未命名脚本"
