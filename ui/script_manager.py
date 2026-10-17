"""脚本库界面：列出、预览、加载、重命名、删除、新建脚本。

布局（左右分栏）::

    ┌ 脚本库  脚本目录：…/data/scripts            [新建] [重命名] [删除] [刷新] ┐
    ├───────────────────────────┬─────────────────────────────────────────────┤
    │ 名称        步数  时长     │ 预览：脚本名称（18 步 · 3.42s）             │
    │ 登录流程     18   3.42s   │ ┌─────────────────────────────────────────┐ │
    │ 下单流程      9   1.10s   │ │ 序号 类型     内容              时间    │ │
    │ …                        │ │  1   鼠标移动 移动到 (640, 400)  0.000s │ │
    │                           │ └─────────────────────────────────────────┘ │
    │                           │                        [加载到主界面]        │
    └───────────────────────────┴─────────────────────────────────────────────┘

本组件只负责界面与交互，真正的文件操作通过信号交给主窗口执行
（主窗口掌握配置、脏标记与"当前脚本"状态）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout,
                               QHeaderView, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from core import event_parser as ep
from ui import icons, styles
from ui.event_list import _make_icon
from ui.spin_box import NumberField

SCRIPT_COL_NAME, SCRIPT_COL_STEPS, SCRIPT_COL_DURATION, SCRIPT_COL_MTIME, SCRIPT_COL_COUNT = range(5)
PREVIEW_COL_INDEX, PREVIEW_COL_TYPE, PREVIEW_COL_DESC, PREVIEW_COL_TIME = range(4)


class ScriptManagerPage(QWidget):
    """「回放与脚本」页面（第六阶段：由原「脚本库」上移而来）。

    结构::

        ┌ 回放与脚本  当前：登录流程.json（18 步 · 3.42s）      [新建][重命名][删除][刷新] ┐
        ├──────────────────────────────────────────────────────────────────────────────┤
        │ 回放控制   [开始回放] [停止回放]  循环：[不循环 ▾]  轮数：[1]  第 2 / 5 轮      │
        ├───────────────────────────┬──────────────────────────────────────────────────┤
        │ 名称   步数  时长  修改时间 │ 预览：脚本名称（18 步 · 3.42s）                   │
        │ 登录流程 18   3.42s        │ ┌────────────────────────────────────────────┐   │
        │ 下单流程  9   1.10s        │ │ 序号 类型     内容              时间       │   │
        │                           │ └────────────────────────────────────────────┘   │
        │                           │                          [加载该脚本]              │
        └───────────────────────────┴──────────────────────────────────────────────────┘

    本组件只负责界面与交互，真正的文件操作与回放控制通过信号交给主窗口执行。
    """

    create_requested = Signal()
    rename_requested = Signal()
    delete_requested = Signal()
    #: 「加载该脚本」：把选中脚本载入并准备回放（不再跳转到别的页）
    load_requested = Signal()
    refresh_requested = Signal()
    #: 回放控制
    play_requested = Signal()
    stop_play_requested = Signal()
    #: 循环设置变化：``(模式, 轮数)``，模式 ∈ {"none","fixed","infinite"}
    loop_changed = Signal(str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scripts: list[Path] = []
        self._selected: Optional[Path] = None
        self._playing = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(styles.CONTENT_MARGIN, 12, styles.CONTENT_MARGIN,
                                 styles.CONTENT_MARGIN)
        outer.setSpacing(styles.CARD_SPACING)

        # ---------------- 顶部标题 + 工具条 ----------------
        header_card = QWidget(self)
        header_card.setObjectName("Card")
        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(14, 12, 14, 12)
        header_layout.setSpacing(10)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title = QLabel("回放与脚本")
        title.setObjectName("CardTitle")
        title_row.addWidget(title)

        self.count_label = QLabel("共 0 个脚本")
        self.count_label.setObjectName("Hint")
        title_row.addWidget(self.count_label)

        self.current_label = QLabel("当前：未加载脚本")
        self.current_label.setObjectName("Badge")
        self.current_label.setToolTip("当前已载入、准备回放的脚本")
        title_row.addWidget(self.current_label)
        title_row.addStretch(1)

        self.dir_label = QLabel("")
        self.dir_label.setObjectName("Muted")
        self.dir_label.setToolTip("脚本存放目录（由配置文件决定）")
        title_row.addWidget(self.dir_label)
        header_layout.addLayout(title_row)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.btn_new = self._button("新建脚本", icons.ICON_PLUS, "创建一个空脚本文件")
        self.btn_rename = self._button("重命名", icons.ICON_SAVE, "重命名选中的脚本文件")
        self.btn_delete = self._button("删除", icons.ICON_TRASH, "删除选中的脚本文件", danger=True)
        self.btn_refresh = self._button("刷新", icons.ICON_REFRESH, "重新扫描脚本目录")
        for button in (self.btn_new, self.btn_rename, self.btn_delete, self.btn_refresh):
            toolbar.addWidget(button)
        toolbar.addStretch(1)

        hint = QLabel("双击脚本即可加载该脚本")
        hint.setObjectName("Muted")
        toolbar.addWidget(hint)
        header_layout.addLayout(toolbar)
        outer.addWidget(header_card)

        # ---------------- 回放控制 ----------------
        outer.addWidget(self._build_play_card())

        # ---------------- 左右分栏 ----------------
        body = QHBoxLayout()
        body.setSpacing(styles.CARD_SPACING)

        list_card = QWidget(self)
        list_card.setObjectName("ManagerCard")
        list_layout = QVBoxLayout(list_card)
        list_layout.setContentsMargins(12, 10, 12, 12)
        list_layout.setSpacing(8)

        list_title = QLabel("全部脚本")
        list_title.setObjectName("SectionTitle")
        list_layout.addWidget(list_title)

        self.table = QTableWidget(0, SCRIPT_COL_COUNT, list_card)
        self.table.setObjectName("ScriptTable")
        self.table.setHorizontalHeaderLabels(["名称", "步数", "时长", "修改时间"])
        self._setup_table(self.table)
        header = self.table.horizontalHeader()
        # 名称列固定宽度并允许省略号，其余列按内容取宽，避免在窄栏里被挤成“演…”
        header.setSectionResizeMode(SCRIPT_COL_NAME, QHeaderView.Stretch)
        for col in (SCRIPT_COL_STEPS, SCRIPT_COL_DURATION, SCRIPT_COL_MTIME):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
        self.table.setColumnWidth(SCRIPT_COL_STEPS, 52)
        self.table.setColumnWidth(SCRIPT_COL_DURATION, 64)
        self.table.setColumnWidth(SCRIPT_COL_MTIME, 112)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.setToolTip("双击某一行即可加载该脚本到主界面")
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(lambda _i: self.load_requested.emit())
        list_layout.addWidget(self.table, 1)
        body.addWidget(list_card, 4)

        preview_card = QWidget(self)
        preview_card.setObjectName("PreviewCard")
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(12, 10, 12, 12)
        preview_layout.setSpacing(8)

        preview_head = QHBoxLayout()
        preview_head.setSpacing(8)
        self.preview_title = QLabel("脚本预览")
        self.preview_title.setObjectName("SectionTitle")
        preview_head.addWidget(self.preview_title)
        self.preview_meta = QLabel("请选择左侧脚本")
        self.preview_meta.setObjectName("Hint")
        preview_head.addWidget(self.preview_meta)
        preview_head.addStretch(1)
        preview_layout.addLayout(preview_head)

        self.preview_table = QTableWidget(0, 4, preview_card)
        self.preview_table.setObjectName("PreviewTable")
        self.preview_table.setHorizontalHeaderLabels(["序号", "类型", "内容", "时间"])
        self._setup_table(self.preview_table)
        pheader = self.preview_table.horizontalHeader()
        pheader.setSectionResizeMode(PREVIEW_COL_INDEX, QHeaderView.Fixed)
        pheader.setSectionResizeMode(PREVIEW_COL_TYPE, QHeaderView.Fixed)
        pheader.setSectionResizeMode(PREVIEW_COL_DESC, QHeaderView.Stretch)
        pheader.setSectionResizeMode(PREVIEW_COL_TIME, QHeaderView.Fixed)
        self.preview_table.setColumnWidth(PREVIEW_COL_INDEX, 46)
        self.preview_table.setColumnWidth(PREVIEW_COL_TYPE, 74)
        self.preview_table.setColumnWidth(PREVIEW_COL_TIME, 96)
        preview_layout.addWidget(self.preview_table, 1)

        preview_actions = QHBoxLayout()
        preview_actions.addStretch(1)
        self.btn_load = QPushButton("加载该脚本")
        self.btn_load.setObjectName("PrimaryButton")
        self.btn_load.setToolTip(
            "把选中的脚本载入并准备回放（右侧预览即为该脚本内容）"
        )
        self.btn_load.setCursor(Qt.PointingHandCursor)
        self.btn_load.clicked.connect(self.load_requested.emit)
        preview_actions.addWidget(self.btn_load)
        preview_layout.addLayout(preview_actions)
        body.addWidget(preview_card, 4)

        outer.addLayout(body, 1)

        # 信号
        self.btn_new.clicked.connect(self.create_requested.emit)
        self.btn_rename.clicked.connect(self.rename_requested.emit)
        self.btn_delete.clicked.connect(self.delete_requested.emit)
        self.btn_refresh.clicked.connect(self.refresh_requested.emit)

        self._update_actions()

    # ==================================================================
    # 回放控制卡片
    # ==================================================================
    def _build_play_card(self) -> QWidget:
        """回放控制：开始/停止 + 回放模式 + 循环模式 + 轮数 + 轮次显示。"""
        from core.player import MODE_STEP, MODE_TIMED

        card = QWidget(self)
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)

        self.btn_play = QPushButton("开始回放")
        self.btn_play.setObjectName("PrimaryButton")
        self.btn_play.setIcon(_make_icon(icons.ICON_PLAY, styles.ICON_ON_PRIMARY))
        self.btn_play.setIconSize(QSize(15, 15))
        self.btn_play.setCursor(Qt.PointingHandCursor)
        self.btn_play.setToolTip("按当前脚本原速回放（模式一）；快捷键 F6")
        self.btn_play.clicked.connect(self.play_requested.emit)
        row.addWidget(self.btn_play)

        self.btn_stop_play = QPushButton("停止回放")
        self.btn_stop_play.setObjectName("DangerButton")
        self.btn_stop_play.setCursor(Qt.PointingHandCursor)
        self.btn_stop_play.setToolTip("停止回放并释放所有按键；快捷键 F8")
        self.btn_stop_play.clicked.connect(self.stop_play_requested.emit)
        row.addWidget(self.btn_stop_play)

        # 回放模式：第七阶段从录制页移到本页（回放是日常主场景，
        # 模式切换应紧跟「开始回放」按钮）。主窗口通过
        # ``self.mode_combo = page_library.mode_combo`` 引用它，
        # 信号连接与配置读写仍在主窗口完成。
        self.mode_combo = QComboBox(card)
        self.mode_combo.addItem("模式一 · 原速回放", MODE_TIMED)
        self.mode_combo.addItem("模式二 · 单步热键", MODE_STEP)
        self.mode_combo.setFixedWidth(158)
        self.mode_combo.setToolTip(
            "模式一按录制时间间隔回放；模式二用 PageUp/PageDown 单步执行")
        row.addWidget(self.mode_combo)

        row.addWidget(self._vline())

        loop_label = QLabel("循环")
        loop_label.setObjectName("Muted")
        row.addWidget(loop_label)

        self.loop_combo = QComboBox(card)
        self.loop_combo.addItem("不循环", "none")
        self.loop_combo.addItem("固定轮数", "fixed")
        self.loop_combo.addItem("无限循环", "infinite")
        self.loop_combo.setFixedWidth(122)
        self.loop_combo.setToolTip(
            "循环回放只作用于模式一（原速回放）。\n"
            "模式二由你手动按热键触发，不参与自动循环。"
        )
        self.loop_combo.currentIndexChanged.connect(self._on_loop_changed)
        row.addWidget(self.loop_combo)

        # 自绘数字框（QSpinBox 在高 DPI 下后缀会被按钮侧挡，见 spin_box.py）
        self.spin_rounds = NumberField(1, 9999, 1, card)
        self.spin_rounds.setFixedWidth(78)
        self.spin_rounds.setToolTip("固定轮数模式下要执行的轮数")
        self.spin_rounds.valueChanged.connect(self._on_loop_changed)
        row.addWidget(self.spin_rounds)

        rounds_label = QLabel("轮")
        rounds_label.setObjectName("Muted")
        row.addWidget(rounds_label)

        self.round_label = QLabel("")
        self.round_label.setObjectName("Badge")
        self.round_label.setVisible(False)
        row.addWidget(self.round_label)

        row.addStretch(1)

        self.mode_hint = QLabel("")
        self.mode_hint.setObjectName("Muted")
        row.addWidget(self.mode_hint)
        layout.addLayout(row)

        self.loop_hint = QLabel("")
        self.loop_hint.setObjectName("Hint")
        self.loop_hint.setWordWrap(True)
        layout.addWidget(self.loop_hint)

        self._update_loop_hint()
        return card

    def _on_loop_changed(self, _index: int = 0) -> None:
        self._update_loop_hint()
        self.loop_changed.emit(self.loop_mode(), self.loop_rounds())

    def _update_loop_hint(self) -> None:
        mode = self.loop_mode()
        self.spin_rounds.setEnabled(mode == "fixed")
        if mode == "none":
            self.loop_hint.setText("回放一遍后自动停止。")
        elif mode == "fixed":
            self.loop_hint.setText(
                f"回放 {self.spin_rounds.value()} 轮后自动停止；"
                "过程中仍可用 F7 暂停 / F8 停止。"
            )
        else:
            self.loop_hint.setText(
                "一直循环下去，直到按 F8（或点「停止回放」）；"
                "过程中仍可用 F7 暂停 / F8 停止。"
            )

    def loop_mode(self) -> str:
        data = self.loop_combo.currentData()
        return str(data) if data else "none"

    def loop_rounds(self) -> int:
        return int(self.spin_rounds.value())

    def set_loop(self, mode: str, rounds: int) -> None:
        """从配置回填循环设置（不触发 ``loop_changed``）。"""
        index = self.loop_combo.findData(str(mode))
        if index >= 0:
            self.loop_combo.blockSignals(True)
            self.loop_combo.setCurrentIndex(index)
            self.loop_combo.blockSignals(False)
        self.spin_rounds.blockSignals(True)
        self.spin_rounds.setValue(max(1, int(rounds or 1)))
        self.spin_rounds.blockSignals(False)
        self._update_loop_hint()

    # ==================================================================
    # 状态更新（由主窗口驱动）
    # ==================================================================
    def set_playing(self, playing: bool, paused: bool = False) -> None:
        """回放状态变化时切换按钮与说明。"""
        self._playing = bool(playing)
        self.btn_play.setEnabled(not playing)
        self.btn_stop_play.setEnabled(playing)
        self.btn_play.setText("暂停中" if (playing and paused) else "开始回放")
        if playing:
            self.mode_hint.setText("回放中…")
        else:
            self.mode_hint.setText("")

    def set_round(self, text: str) -> None:
        """显示当前轮次，例如 ``第 2 / 5 轮``。"""
        self.round_label.setText(text or "")
        self.round_label.setVisible(bool(text))

    def set_current_script(self, name: str, events: int = 0, steps: int = 0,
                           duration_ms: int = 0) -> None:
        """显示当前已加载的脚本（供「准备回放」参考）。"""
        if not name:
            self.current_label.setText("当前：未加载脚本")
            return
        seconds = duration_ms / 1000.0
        self.current_label.setText(
            f"当前：{name}（{steps} 步 · {events} 条事件 · {seconds:.2f}s）"
        )

    def set_play_enabled(self, enabled: bool) -> None:
        """没有可回放内容时禁用开始按钮。"""
        self.btn_play.setEnabled(enabled and not self._playing)

    # ==================================================================
    # 构建辅助
    # ==================================================================
    @staticmethod
    def _vline() -> QWidget:
        line = QWidget()
        line.setFixedSize(1, 20)
        line.setStyleSheet(f"background: {styles.BORDER};")
        return line

    def _button(self, text: str, icon_name: str, tooltip: str,
                danger: bool = False) -> QPushButton:
        """卡片头部小按钮：危险=蓝边描边，其余=次操作（浅蓝填充）。"""
        if danger:
            name, icon_color = "DangerButton", styles.ICON_ON_DANGER
        else:
            name, icon_color = "SecondaryButton", styles.ICON_ON_SECONDARY
        button = QPushButton(text, self)
        button.setObjectName(name)
        button.setToolTip(tooltip)
        button.setIcon(_make_icon(icon_name, icon_color))
        button.setIconSize(QSize(15, 15))
        button.setCursor(Qt.PointingHandCursor)
        return button

    @staticmethod
    def _setup_table(table: QTableWidget) -> None:
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.verticalHeader().setDefaultSectionSize(30)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

    def minimumSizeHint(self):  # noqa: N802 - Qt 命名
        return QSize(520, 320)

    # ==================================================================
    # 数据
    # ==================================================================
    @property
    def scripts(self) -> list[Path]:
        return list(self._scripts)

    def selected_path(self) -> Optional[Path]:
        """当前选中的脚本路径（没有选中时返回 None）。"""
        row = self.table.currentRow()
        if 0 <= row < len(self._scripts):
            return self._scripts[row]
        return None

    def set_directory_hint(self, directory: Path) -> None:
        self.dir_label.setText(f"脚本目录：{directory}")

    def set_scripts(self, scripts: Sequence[Path], keep_selection: bool = True) -> None:
        """刷新脚本列表。``keep_selection`` 为 True 时尽量保持原选中项。"""
        previous = self._selected if keep_selection else None
        self._scripts = list(scripts)

        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(self._scripts))
            for row, path in enumerate(self._scripts):
                self._fill_script_row(row, path)
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
        finally:
            self.table.setUpdatesEnabled(True)

        self.count_label.setText(f"共 {len(self._scripts)} 个脚本")

        # 恢复选中
        target = -1
        if previous is not None:
            for row, path in enumerate(self._scripts):
                if path == previous:
                    target = row
                    break
        if target < 0 and self._scripts:
            target = 0
        if target >= 0:
            self.table.selectRow(target)
            self.table.setCurrentCell(target, SCRIPT_COL_NAME)
            self._show_preview(self._scripts[target])
        else:
            self._show_preview(None)
        self._update_actions()

    def _fill_script_row(self, row: int, path: Path) -> None:
        summary = _summarize(path)
        values = (
            path.stem,
            str(summary["steps"]),
            f"{summary['duration_ms'] / 1000.0:.2f}s",
            summary["mtime"],
        )
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            if col == SCRIPT_COL_NAME:
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                item.setToolTip(str(path))
                if summary["error"]:
                    item.setForeground(QColor(styles.DANGER))
                    item.setToolTip(f"读取失败：{summary['error']}")
            elif col == SCRIPT_COL_MTIME:
                item.setTextAlignment(Qt.AlignCenter)
                item.setForeground(QColor(styles.TEXT_MUTED))
                font = QFont(item.font())
                font.setFamily("Consolas")
                item.setFont(font)
            else:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                item.setForeground(QColor(styles.TEXT_SECONDARY))
                font = QFont(item.font())
                font.setFamily("Consolas")
                item.setFont(font)
            self.table.setItem(row, col, item)

    # ==================================================================
    # 预览
    # ==================================================================
    def _on_selection_changed(self) -> None:
        path = self.selected_path()
        self._show_preview(path)
        self._update_actions()

    def _show_preview(self, path: Optional[Path]) -> None:
        self._selected = path
        self.preview_table.setUpdatesEnabled(False)
        try:
            self.preview_table.setRowCount(0)
            if path is None:
                self.preview_title.setText("脚本预览")
                self.preview_meta.setText("请选择左侧脚本")
                return
            try:
                payload = ep.load_script(path)
            except ep.ScriptError as exc:
                self.preview_title.setText(path.stem)
                self.preview_meta.setText("读取失败")
                self.preview_table.setRowCount(1)
                item = QTableWidgetItem(str(exc))
                item.setForeground(QColor(styles.DANGER))
                self.preview_table.setItem(0, PREVIEW_COL_DESC, item)
                return

            events = payload.get("events", [])
            steps = ep.build_steps(events)
            duration = int(payload.get("duration_ms") or ep.events_duration_ms(events))
            self.preview_title.setText(str(payload.get("name") or path.stem))
            self.preview_meta.setText(
                f"{len(events)} 条事件 · {len(steps)} 步 · {duration / 1000.0:.2f}s · "
                f"{payload.get('created_at') or '—'}"
            )

            self.preview_table.setRowCount(len(events))
            for row, event in enumerate(events):
                values = (
                    str(row + 1),
                    ep.type_label(event),
                    ep.describe(event),
                    f"{int(event.get('t') or 0) / 1000.0:.3f}s",
                )
                for col, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    if col == PREVIEW_COL_INDEX:
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                        item.setForeground(QColor(styles.TEXT_MUTED))
                    elif col == PREVIEW_COL_TYPE:
                        item.setTextAlignment(Qt.AlignCenter)
                        item.setForeground(QColor(styles.TEXT_SECONDARY))
                    elif col == PREVIEW_COL_TIME:
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                        item.setForeground(QColor(styles.TEXT_SECONDARY))
                        font = QFont(item.font())
                        font.setFamily("Consolas")
                        item.setFont(font)
                    self.preview_table.setItem(row, col, item)
        finally:
            self.preview_table.setUpdatesEnabled(True)

    # ==================================================================
    # 状态
    # ==================================================================
    def _update_actions(self) -> None:
        has_selection = self.selected_path() is not None
        self.btn_rename.setEnabled(has_selection)
        self.btn_delete.setEnabled(has_selection)
        self.btn_load.setEnabled(has_selection)
        # 有选中脚本才能「加载该脚本」；能否回放由主窗口按内容再判定
        self.btn_play.setEnabled(has_selection and not self._playing)

    def select_path(self, path: Path) -> None:
        """按路径选中某一行（用于新建/重命名后定位）。"""
        for row, item_path in enumerate(self._scripts):
            if item_path == path:
                self.table.selectRow(row)
                self.table.setCurrentCell(row, SCRIPT_COL_NAME)
                return


def _summarize(path: Path) -> dict[str, Any]:
    """读取脚本概要信息（步数 / 时长 / 修改时间）；失败时返回错误说明。"""
    info: dict[str, Any] = {"steps": 0, "duration_ms": 0, "mtime": "", "error": ""}
    try:
        stat = path.stat()
        import time as _time

        info["mtime"] = _time.strftime("%m-%d %H:%M", _time.localtime(stat.st_mtime))
    except OSError as exc:
        info["error"] = str(exc)
        return info
    try:
        payload = ep.load_script(path)
    except ep.ScriptError as exc:
        info["error"] = str(exc)
        return info
    events = payload.get("events", [])
    info["steps"] = len(ep.build_steps(events))
    info["duration_ms"] = int(payload.get("duration_ms") or ep.events_duration_ms(events))
    return info
