"""中部「步骤编辑器」：可勾选、可增删改移的事件列表。

界面结构（整体是一张白色卡片）::

    ┌ 步骤编辑 共 12 步  [已选 3 步]                     ─────────────┐
    │ [全选] [反选]  │  [删除] [上移] [下移] [插入]  │  [保存] │
    ├────────────────────────────────────────────────────────────────┤
    │ ☑ 序号 类型     内容                        时间              │
    │ ☑  1   鼠标移动 移动到 (640, 400)            0.000s → 0.090s  │
    │ ☐  2   鼠标点击 左键单击 (640, 400)          0.140s → 0.180s  │
    └────────────────────────────────────────────────────────────────┘

* 「当前执行行」用浅蓝底 + 左侧蓝色竖条高亮（模式一/模式二回放时同步）
* 勾选行用极浅蓝底表示；复选框列点击即可切换
* 编辑动作本身不在这里实现，全部通过信号交给主窗口处理
"""


from typing import Any, Optional, Sequence

from PySide6.QtCore import QEvent, QModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView,
                               QLabel, QMenu, QPushButton, QStyle,
                               QStyledItemDelegate, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from core import event_parser as ep
from core.event_parser import Step
from ui import icons, styles

COL_CHECK, COL_INDEX, COL_TYPE, COL_DESC, COL_TIME = range(5)
HEADERS = ["", "序号", "类型", "内容", "时间"]
COLUMN_COUNT = 5

#: 自定义数据角色
ROLE_CHECKED = Qt.UserRole + 1
ROLE_CURRENT = Qt.UserRole + 2
ROLE_FOCUS = Qt.UserRole + 3


class _RowDelegate(QStyledItemDelegate):
    """行背景 + 复选框的自绘代理。

    样式表里的 ``QTableWidget::item`` 规则会覆盖 ``item.setBackground()``，
    所以行底色（当前执行行 = 浅蓝，勾选行 = 极浅蓝）必须在这里画，
    否则「当前执行到哪一步」在界面上看不出来。
    """

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        row = index.row()
        checked = bool(index.data(ROLE_CHECKED))
        current = bool(index.data(ROLE_CURRENT))
        focused = bool(index.data(ROLE_FOCUS))

        # 清掉焦点/选中状态，避免 Windows 原生样式再画一圈蓝色焦点框
        option.state &= ~QStyle.State_HasFocus
        option.state &= ~QStyle.State_Selected

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        # ---------------- 行底色 ----------------
        if current:
            base = QColor(styles.ACCENT_LIGHT)
        elif checked:
            base = QColor("#F7F9FF")
        elif focused:
            base = QColor("#F4F4F5")
        else:
            base = QColor(styles.CARD)
        painter.fillRect(option.rect, base)

        # 当前执行行：左侧 3px 强调色竖条
        if current:
            painter.fillRect(QRectF(option.rect.left(), option.rect.top(), 3,
                                    option.rect.height()), QColor(styles.ACCENT))

        # 底部分割线
        painter.setPen(QColor(styles.BORDER))
        painter.drawLine(option.rect.left(), option.rect.bottom(),
                         option.rect.right(), option.rect.bottom())

        if index.column() == COL_CHECK:
            _draw_checkbox(painter, option.rect, checked)
        else:
            text = index.data(Qt.DisplayRole)
            if text:
                self._draw_text(painter, option, index, str(text))
        painter.restore()

    @staticmethod
    def _draw_text(painter: QPainter, option, index: QModelIndex, text: str) -> None:
        """按列的对齐方式与配色绘制文本。"""
        col = index.column()
        rect = QRectF(option.rect).adjusted(7, 0, -7, 0)
        align = Qt.AlignVCenter | (Qt.AlignLeft if col == COL_DESC else Qt.AlignRight)
        if col == COL_CHECK:
            return
        if col == COL_DESC:
            painter.setPen(QColor(styles.TEXT))
        elif col == COL_INDEX:
            painter.setPen(QColor(styles.TEXT_MUTED))
        else:
            painter.setPen(QColor(styles.TEXT_SECONDARY))
        font = painter.font()
        if col == COL_TIME:
            font.setFamily("Consolas")
        painter.setFont(font)
        painter.drawText(rect, int(align), text)

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(34, 30)


def _draw_checkbox(painter: QPainter, cell: QRectF, checked: bool) -> None:
    """画一个 4px 圆角复选框，选中时填充强调色并打勾。"""
    size = 15.0
    box = QRectF(0, 0, size, size)
    box.moveCenter(QRectF(cell).center())

    pen = painter.pen()
    pen.setJoinStyle(Qt.RoundJoin)
    if checked:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(styles.ACCENT))
        painter.drawRoundedRect(box, 3.5, 3.5)
        pen.setColor(QColor("#FFFFFF"))
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(box.left() + 3.4, box.center().y() + 0.4,
                         box.center().x() - 0.6, box.bottom() - 3.6)
        painter.drawLine(box.center().x() - 0.6, box.bottom() - 3.6,
                         box.right() - 3.0, box.top() + 3.6)
    else:
        pen.setColor(QColor(styles.BORDER_STRONG))
        pen.setWidthF(1.2)
        painter.setPen(pen)
        painter.setBrush(QColor(styles.CARD))
        painter.drawRoundedRect(box, 3.5, 3.5)


class EventListWidget(QWidget):
    """步骤编辑器卡片。"""

    row_activated = Signal(int)          # 双击某行（传入步骤下标）
    edit_requested = Signal(str)         # 工具栏动作名
    selection_changed = Signal(int)      # 当前勾选数量

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._steps: list[Step] = []
        self._checked: set[int] = set()
        self._current_row = -1      # 「当前执行行」（回放时高亮）
        self._focus_row = -1        # 「界面焦点行」（用户点击/方向键选中的行）

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        card = QWidget(self)
        card.setObjectName("ListCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 10, 14, 10)
        card_layout.setSpacing(7)

        # ---------------- 标题行 ----------------
        head = QHBoxLayout()
        head.setSpacing(8)
        self.title = QLabel("步骤编辑")
        self.title.setObjectName("SectionTitle")
        head.addWidget(self.title)

        self.count_label = QLabel("共 0 步")
        self.count_label.setObjectName("Hint")
        head.addWidget(self.count_label)

        self.selection_label = QLabel("")
        self.selection_label.setObjectName("Badge")
        self.selection_label.setVisible(False)
        head.addWidget(self.selection_label)
        head.addStretch(1)
        card_layout.addLayout(head)

        # ---------------- 工具栏（分两行，避免窄窗口下被挤掉） ----------------
        # 第一行：勾选类操作
        toolbar1 = QHBoxLayout()
        toolbar1.setSpacing(6)

        self.btn_select_all = self._tool_button(
            "全选", icons.ICON_CHECK_ALL, "勾选全部步骤", secondary=True)
        self.btn_invert = self._tool_button(
            "反选", icons.ICON_INVERT, "反转当前勾选状态", secondary=True)
        # 不再提供「取消选择」按钮：点击任何非复选框区域即自动清除勾选
        for btn in (self.btn_select_all, self.btn_invert):
            toolbar1.addWidget(btn)

        toolbar1.addWidget(self._separator())

        self.btn_delete = self._tool_button("删除", icons.ICON_TRASH, "删除勾选的步骤", danger=True)
        self.btn_up = self._tool_button(
            "上移", icons.ICON_ARROW_UP, "把勾选的步骤整体上移一格", secondary=True)
        self.btn_down = self._tool_button(
            "下移", icons.ICON_ARROW_DOWN, "把勾选的步骤整体下移一格", secondary=True)
        self.btn_insert = self._tool_button(
            "插入步骤", icons.ICON_PLUS, "在当前选中位置插入一个新步骤", primary=True)
        for btn in (self.btn_delete, self.btn_up, self.btn_down, self.btn_insert):
            toolbar1.addWidget(btn)

        toolbar1.addStretch(1)
        self.btn_save = self._tool_button("保存", icons.ICON_SAVE,
                                          "把编辑后的步骤序列写回脚本文件", primary=True)
        toolbar1.addWidget(self.btn_save)
        card_layout.addLayout(toolbar1)

        # 第二行：单步编辑（参数 / 时间）
        toolbar2 = QHBoxLayout()
        toolbar2.setSpacing(6)
        edit_hint = QLabel("编辑")
        edit_hint.setObjectName("Muted")
        toolbar2.addWidget(edit_hint)
        toolbar2.addWidget(self._separator())

        self.btn_edit_step = self._tool_button("编辑这一步", icons.ICON_EDIT,
                                               "修改这一步的参数、时间、长按与连击设置：\n"
                                               "· 鼠标：单击/双击/三击/左中右键 + 坐标\n"
                                               "· 键盘：改键；组合键可改修饰键与目标键\n"
                                               "· 时间：起始时间 + 持续时长\n"
                                               "· 高级：长按 / 无限连击\n"
                                               "（也可在列表里右键选择）")
        self.btn_duration = self._tool_button("重设总时长", icons.ICON_TIMELINE,
                                              "输入目标总时长，把整段脚本的步骤均匀重排")
        toolbar2.addWidget(self.btn_edit_step)
        toolbar2.addWidget(self.btn_duration)
        toolbar2.addStretch(1)
        card_layout.addLayout(toolbar2)

        # ---------------- 表格 ----------------
        self.table = QTableWidget(0, COLUMN_COUNT, card)
        self.table.setObjectName("EventTable")
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setFocusPolicy(Qt.StrongFocus)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setMinimumHeight(170)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        # 键盘/鼠标操作提示放在表格的 tooltip 上，避免占用纵向空间
        self.table.setToolTip(
            "双击「时间」列编辑步骤（含时间/长按/连击）· 双击其它列跳步执行该步骤\n"
            "右键可编辑这一步的全部参数（动作、坐标、时间、高级选项）\n"
            "空格键切换勾选 · 上下方向键移动焦点 · Delete 删除勾选 · Ctrl+A 全选"
        )
        # 整行统一用自定义代理绘制（背景 + 复选框 + 文本配色）。
        # 这里清空 QTableWidget::item 的样式，避免它覆盖代理画出来的行底色。
        self._delegate = _RowDelegate(self.table)
        self.table.setItemDelegate(self._delegate)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        # 完全自管「焦点行」：不用 Qt 的 current cell，
        # 这样 Windows 原生样式就不会再画一圈蓝色焦点框。
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setStyleSheet(
            "QTableWidget#EventTable, QTableWidget#EventTable::item "
            "{ outline: none; padding: 0px; border: none; }"
        )

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_CHECK, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_INDEX, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_TYPE, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_DESC, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_TIME, QHeaderView.Fixed)
        self.table.setColumnWidth(COL_CHECK, 34)
        self.table.setColumnWidth(COL_INDEX, 46)
        self.table.setColumnWidth(COL_TYPE, 78)
        self.table.setColumnWidth(COL_TIME, 190)

        self.table.cellClicked.connect(self._on_cell_clicked)
        # 空白处点击不会触发 cellClicked，需要在 viewport 上补一次拦截
        self.table.viewport().installEventFilter(self)
        self.table.doubleClicked.connect(self._on_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)

        card_layout.addWidget(self.table, 1)

        layout.addWidget(card, 1)

        # 快捷键
        QShortcut(QKeySequence.Delete, self.table, activated=self._emit_delete)
        QShortcut(QKeySequence.SelectAll, self.table, activated=self.select_all)

        # 信号接线
        self.btn_select_all.clicked.connect(self.select_all)
        self.btn_invert.clicked.connect(self.invert_selection)
        self.btn_delete.clicked.connect(self._emit_delete)
        self.btn_up.clicked.connect(lambda: self._emit_move(-1))
        self.btn_down.clicked.connect(lambda: self._emit_move(1))
        self.btn_insert.clicked.connect(lambda: self.edit_requested.emit("insert"))
        self.btn_save.clicked.connect(lambda: self.edit_requested.emit("save"))
        self.btn_edit_step.clicked.connect(self._emit_edit)
        self.btn_duration.clicked.connect(lambda: self.edit_requested.emit("duration"))

        self._update_toolbar()

    # ==================================================================
    # 构建辅助
    # ==================================================================
    def _tool_button(self, text: str, icon_name: str, tooltip: str,
                     danger: bool = False, primary: bool = False,
                     secondary: bool = False) -> QPushButton:
        """创建带线性图标的工具栏小按钮（紧凑高度，节省纵向空间）。

        蓝色单色系四档：primary 主操作（实心蓝）/ secondary 次操作（浅蓝）/
        danger 危险（蓝边描边）/ 其余为工具（透明幽灵）；图标颜色随分类。
        """
        if primary:
            name, icon_color = "PrimaryButton", styles.ICON_ON_PRIMARY
        elif danger:
            name, icon_color = "DangerButton", styles.ICON_ON_DANGER
        elif secondary:
            name, icon_color = "SecondaryButton", styles.ICON_ON_SECONDARY
        else:
            name, icon_color = "GhostButton", styles.ICON_ON_GHOST
        button = QPushButton(text, self)
        button.setObjectName(name)
        button.setToolTip(tooltip)
        button.setIcon(_make_icon(icon_name, icon_color))
        button.setIconSize(QSize(14, 14))
        button.setCursor(Qt.PointingHandCursor)
        button.setFixedHeight(28)
        return button

    def _separator(self) -> QWidget:
        line = QWidget(self)
        line.setFixedSize(1, 20)
        line.setStyleSheet(f"background: {styles.BORDER};")
        return line

    def minimumSizeHint(self):  # noqa: N802 - Qt 命名
        return QSize(460, 260)

    # ==================================================================
    # 数据
    # ==================================================================
    @property
    def steps(self) -> list[Step]:
        """当前步骤列表（副本）。"""
        return list(self._steps)

    @property
    def events(self) -> list[dict[str, Any]]:
        """把步骤摊平回原始事件序列（供回放与保存使用）。"""
        return ep.steps_to_events(self._steps)

    def set_steps(self, steps: Sequence[Step]) -> None:
        """整体替换步骤列表并重建界面。"""
        self._steps = list(steps)
        self._reindex()
        self._checked.clear()
        self._current_row = -1
        self._rebuild()

    def set_events(self, events: Sequence[dict[str, Any]],
                   steps: Optional[Sequence[Step]] = None) -> None:
        """用原始事件刷新（自动分组），保留原有勾选状态。"""
        self.set_steps(list(steps) if steps is not None else ep.build_steps(events))

    def clear(self) -> None:
        """清空列表。"""
        self._steps = []
        self._checked.clear()
        self._current_row = -1
        self._rebuild()

    def _reindex(self) -> None:
        """按当前顺序刷新步骤的显示序号。"""
        for i, step in enumerate(self._steps):
            step.index = i + 1

    # ==================================================================
    # 勾选
    # ==================================================================
    def checked_indices(self) -> list[int]:
        """已勾选的步骤下标（升序）。"""
        return sorted(i for i in self._checked if 0 <= i < len(self._steps))

    @property
    def checked_count(self) -> int:
        return len(self.checked_indices())

    def current_row(self) -> int:
        """界面焦点行；没有焦点行时取最后勾选的行；都没有返回 -1。"""
        if 0 <= self._focus_row < len(self._steps):
            return self._focus_row
        checked = self.checked_indices()
        return checked[-1] if checked else -1

    def insert_position(self) -> int:
        """插入位置：勾选项之前，或焦点行之后，否则追加到末尾。"""
        checked = self.checked_indices()
        if checked:
            return checked[0]
        row = self.current_row()
        if 0 <= row < len(self._steps):
            return row + 1
        return len(self._steps)

    def set_focus_row(self, row: int) -> None:
        """设置界面焦点行（用户点击或方向键移动）。"""
        if row == self._focus_row:
            return
        previous = self._focus_row
        self._focus_row = row if 0 <= row < len(self._steps) else -1
        for target in (previous, self._focus_row):
            if 0 <= target < self.table.rowCount():
                self._set_focus_data(target, target == self._focus_row)
        self._update_toolbar()

    def select_all(self) -> None:
        self._checked = set(range(len(self._steps)))
        self._refresh_check_column()
        self._update_toolbar()

    def clear_selection(self) -> None:
        self._checked.clear()
        self._refresh_check_column()
        self._update_toolbar()

    def invert_selection(self) -> None:
        self._checked = {i for i in range(len(self._steps)) if i not in self._checked}
        self._refresh_check_column()
        self._update_toolbar()

    def set_checked(self, indices: Sequence[int]) -> None:
        """外部设置勾选（编辑后恢复选择状态用）。"""
        self._checked = {i for i in indices if 0 <= i < len(self._steps)}
        self._refresh_check_column()
        self._update_toolbar()

    def toggle_row(self, row: int) -> None:
        if row < 0 or row >= len(self._steps):
            return
        if row in self._checked:
            self._checked.discard(row)
        else:
            self._checked.add(row)
        self._refresh_check_column()
        self._update_toolbar()

    # ==================================================================
    # 信号发出
    # ==================================================================
    def _emit_delete(self) -> None:
        self.edit_requested.emit("delete")

    def _emit_move(self, delta: int) -> None:
        self.edit_requested.emit("move_up" if delta < 0 else "move_down")

    # ==================================================================
    # 右键菜单
    # ==================================================================
    def _on_context_menu(self, pos) -> None:
        """在表格上右键：弹出「编辑本步 / 删除 / 上下移」菜单。"""
        if not self._editing_enabled:
            return
        index = self.table.indexAt(pos)
        row = index.row() if index.isValid() else -1
        if row >= 0:
            self.set_focus_row(row)

        menu = QMenu(self)
        act_edit = menu.addAction("编辑这一步…")
        act_edit.setEnabled(row >= 0)
        menu.addSeparator()

        checked = self.checked_indices()
        targets = checked if checked else ([row] if row >= 0 else [])
        act_delete = menu.addAction(f"删除{'勾选的 ' + str(len(checked)) + ' 步' if checked else '这一步'}")
        act_delete.setEnabled(bool(targets))
        act_up = menu.addAction("上移")
        act_up.setEnabled(bool(targets))
        act_down = menu.addAction("下移")
        act_down.setEnabled(bool(targets))
        menu.addSeparator()
        act_all = menu.addAction("全选")
        act_invert = menu.addAction("反选")
        act_none = menu.addAction("取消选择")

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_edit:
            self.edit_requested.emit("edit_step")
        elif chosen is act_delete:
            self._emit_delete()
        elif chosen is act_up:
            self._emit_move(-1)
        elif chosen is act_down:
            self._emit_move(1)
        elif chosen is act_all:
            self.select_all()
        elif chosen is act_invert:
            self.invert_selection()
        elif chosen is act_none:
            self.clear_selection()

    def _emit_edit(self) -> None:
        """请求编辑当前聚焦/勾选的步骤参数。"""
        if self.mode_row() < 0:
            return
        self.edit_requested.emit("edit_step")

    def mode_row(self) -> int:
        """当前要操作的步骤行：优先勾选的第一行，其次焦点行。"""
        checked = self.checked_indices()
        if checked:
            return checked[0]
        return self.current_row()

    # ==================================================================
    # 表格填充
    # ==================================================================
    def _rebuild(self) -> None:
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(self._steps))
            for row, step in enumerate(self._steps):
                self._fill_row(row, step)
            if self._current_row >= len(self._steps):
                self._current_row = -1
            self._refresh_check_column()
        finally:
            self.table.setUpdatesEnabled(True)
        self.count_label.setText(f"共 {len(self._steps)} 步")
        self._update_toolbar()

    def _fill_row(self, row: int, step: Step) -> None:
        kind_label = step.kind_label
        desc = step.description
        if step.kind == "move" and step.event_count > 1:
            desc = f"{desc}（合并 {step.event_count} 帧）"
        elif step.kind == "combo":
            desc = f"{desc}（一次触发）"
        elif step.kind == "multi":
            desc = f"{desc}（{step.click_count} 连击）"
        # 追加长按 / 无限连击标记（优化 6；时间统一按秒显示）
        hold_ms = int(getattr(step, "explicit_hold_ms", 0))
        infinite = bool(getattr(step, "infinite_repeat", False))
        if hold_ms > 0:
            desc = f"{desc} · 长按 {self._ms_sec(hold_ms)}s"
        if infinite:
            interval = int(getattr(step, "repeat_interval_ms", 50))
            desc = f"{desc} · 连击 间隔 {self._ms_sec(interval)}s"
        values = (
            "",                                  # 复选框由代理绘制
            str(step.index),
            kind_label,
            desc,
            self._format_step_time(step),
        )
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            if col == COL_INDEX:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                item.setForeground(QColor(styles.TEXT_MUTED))
            elif col == COL_TYPE:
                item.setTextAlignment(Qt.AlignCenter)
                item.setForeground(QColor(styles.TEXT_SECONDARY))
            elif col == COL_TIME:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                item.setForeground(QColor(styles.TEXT_SECONDARY))
                font = QFont(item.font())
                font.setFamily("Consolas")
                item.setFont(font)
                item.setToolTip(
                    f"起始 {ep.format_duration(step.t_start)}"
                    + (f" · 按住 {self._ms_sec(step.hold_ms)}s" if step.hold_ms else "")
                    + "\n双击可直接修改这一步的时间"
                )
            elif col == COL_CHECK:
                item.setTextAlignment(Qt.AlignCenter)
            else:
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.table.setItem(row, col, item)
        self._set_checked_data(row, row in self._checked)

    @staticmethod
    def _format_step_time(step: Step) -> str:
        """时间列显示：起始时间，若段内有按住时长则一并显示区间。

        像「连击 8 次」这种内部事件很多的步骤，区间会很长，
        显示成「0.000s → 1.240s (8击)」更可读。
        """
        start = ep.format_duration(step.t_start)
        if step.hold_ms > 0:
            end = ep.format_duration(step.t_end)
            if step.kind == "multi" and step.click_count > 3:
                return f"{start} → {end} ({step.click_count}击)"
            return f"{start} → {end}"
        return start

    @staticmethod
    def _ms_sec(ms: int) -> str:
        """毫秒 → 秒文本：最多 3 位小数并去掉尾随 0（1150 -> "1.15"）。"""
        text = f"{int(ms) / 1000:.3f}".rstrip("0").rstrip(".")
        return text or "0"

    def _set_checked_data(self, row: int, checked: bool) -> None:
        item = self.table.item(row, COL_CHECK)
        if item is not None:
            item.setData(ROLE_CHECKED, checked)

    def _refresh_check_column(self) -> None:
        """只刷新复选框列与当前行的数据（比重建整表快）。"""
        for row in range(self.table.rowCount()):
            self._set_checked_data(row, row in self._checked)
            self._set_current_data(row, row == self._current_row)

    def _set_current_data(self, row: int, current: bool) -> None:
        """把「当前执行行」标记写到整行，供代理高亮。"""
        self._set_role(row, ROLE_CURRENT, current)

    def _set_focus_data(self, row: int, focused: bool) -> None:
        """把「界面焦点行」标记写到整行。"""
        self._set_role(row, ROLE_FOCUS, focused)

    def _set_role(self, row: int, role: int, value: bool) -> None:
        if row < 0 or row >= self.table.rowCount():
            return
        for col in range(COLUMN_COUNT):
            item = self.table.item(row, col)
            if item is not None:
                item.setData(role, value)
        self.table.viewport().update()

    @staticmethod
    def _format_time(ms: int) -> str:
        """毫秒时间戳格式化为 ``秒.毫秒``，超过 1 分钟补上分钟。"""
        return ep.format_duration(ms)

    # ==================================================================
    # 交互
    # ==================================================================
    def _on_cell_clicked(self, row: int, column: int) -> None:
        """单元格点击：复选框列切换该行勾选，其它列一律清空全部勾选。

        勾选是为批量操作服务的临时状态，点到别处就失效——符合「点哪里
        就只关注哪里」的直觉。正因如此，工具栏不再需要单独的「取消选择」。
        """
        self.set_focus_row(row)
        if column == COL_CHECK:
            self.toggle_row(row)
        elif self._checked:
            self.clear_selection()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """点击列表空白处（没有命中任何行）时同样清空勾选。

        ``cellClicked`` 只在有效单元格上触发，表格下方留白不会发出信号，
        所以额外在 viewport 上拦一次鼠标按下。
        """
        if (watched is self.table.viewport()
                and event.type() == QEvent.MouseButtonPress
                and event.button() == Qt.LeftButton
                and not self.table.indexAt(event.pos()).isValid()
                and self._checked):
            self.clear_selection()
        return super().eventFilter(watched, event)

    def _on_double_clicked(self, index) -> None:
        """双击：时间列 -> 编辑步骤（含时间）；其它列 -> 跳步执行。"""
        if not index.isValid():
            return
        if index.column() == COL_TIME:
            self.set_focus_row(index.row())
            self.edit_requested.emit("edit_step")
            return
        self.row_activated.emit(index.row())

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """支持空格切换勾选；上下方向键移动焦点行。"""
        if event.key() == Qt.Key_Space:
            row = self.current_row()
            if row >= 0:
                self.toggle_row(row)
                event.accept()
                return
        if event.key() in (Qt.Key_Up, Qt.Key_Down):
            row = self._focus_row
            row = (max(0, row - 1) if row > 0 else 0) if event.key() == Qt.Key_Up else min(
                self.table.rowCount() - 1, row + 1)
            self.set_focus_row(row)
            event.accept()
            return
        super().keyPressEvent(event)

    # ==================================================================
    # 高亮
    # ==================================================================
    def highlight(self, row: int) -> None:
        """设置「当前执行行」（-1 表示取消）。"""
        if row == self._current_row:
            return
        previous = self._current_row
        self._current_row = row if 0 <= row < len(self._steps) else -1
        if 0 <= previous < self.table.rowCount():
            self._set_current_data(previous, False)
        if self._current_row >= 0:
            self._set_current_data(self._current_row, True)
            self.table.scrollTo(self.table.model().index(self._current_row, COL_DESC),
                                QAbstractItemView.PositionAtCenter)
        self.table.viewport().update()

    @property
    def highlighted(self) -> int:
        return self._current_row

    # ==================================================================
    # 工具栏状态
    # ==================================================================
    def _update_toolbar(self) -> None:
        has_steps = bool(self._steps)
        count = self.checked_count
        self.btn_select_all.setEnabled(has_steps and count < len(self._steps))
        self.btn_invert.setEnabled(has_steps)
        self.btn_delete.setEnabled(count > 0)
        self.btn_up.setEnabled(count > 0)
        self.btn_down.setEnabled(count > 0)
        self.btn_insert.setEnabled(True)
        self.btn_save.setEnabled(has_steps)
        self.btn_edit_step.setEnabled(has_steps and self.mode_row() >= 0)
        self.btn_duration.setEnabled(has_steps)

        if count:
            self.selection_label.setText(f"已选 {count} 步")
            self.selection_label.setVisible(True)
        else:
            self.selection_label.setVisible(False)
        self.selection_changed.emit(count)

    # ==================================================================
    def set_editing_enabled(self, enabled: bool) -> None:
        """录制/回放过程中禁用编辑动作，避免中途改到正在执行的数据。"""
        self._editing_enabled = enabled
        for button in (self.btn_select_all, self.btn_invert,
                       self.btn_delete, self.btn_up, self.btn_down,
                       self.btn_insert, self.btn_save,
                       self.btn_duration, self.btn_edit_step):
            button.setEnabled(enabled)
        if enabled:
            self._update_toolbar()


def _make_icon(name: str, color: str):
    """把自绘图标包成 QIcon，供按钮使用。"""
    from PySide6.QtGui import QIcon, QPixmap

    size = 30
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    icons.paint_icon(painter, name, QRectF(0, 0, size, size), color)
    painter.end()
    return QIcon(pixmap)
