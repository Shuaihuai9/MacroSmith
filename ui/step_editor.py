"""步骤参数编辑对话框：鼠标（动作 + 坐标分离）、键盘（改键）、组合键、连击。

设计要点
--------
* **鼠标「动作」与「坐标」分离**：动作下拉框负责 单击 / 双击 / 三击 /
  左键 / 右键 / 中键；坐标两个 NumberField 负责 (x, y)。两者互不影响。
* **键盘改键**：直接按键捕获，或从常用键下拉里选。
  若原步骤是合并的「按下 + 释放」，只替换按键标识，配对逻辑保持不变。
* **组合键**：勾选修饰键（Ctrl / Shift / Alt / Win）+ 选目标键。
* **连击**：点击次数 1/2/3，实时预览会在描述里体现「双击 / 三击」。

对话框只负责收集参数并返回一份 ``StepEdit``，真正重建事件、
重排时间戳由 ``core.event_parser`` 完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

from core import event_parser as ep
from core.hotkey_manager import display_name, normalize_token, probe_hotkey
from ui import styles
from ui.spin_box import NumberField
from ui.step_dialog import KeyCaptureEdit

#: 鼠标动作下拉项：``(显示文本, 按键, 连击次数, 模式)``
#: 模式：``"click"`` 普通点击 / ``"hold"`` 长按 / ``"repeat"`` 无限连击
MOUSE_ACTIONS: list[tuple[str, str, int, str]] = [
    ("单击（左键）", "left", 1, "click"),
    ("双击（左键）", "left", 2, "click"),
    ("三击（左键）", "left", 3, "click"),
    ("右键（单击）", "right", 1, "click"),
    ("右键双击", "right", 2, "click"),
    ("中键（单击）", "middle", 1, "click"),
    ("中键双击", "middle", 2, "click"),
    ("左键长按", "left", 1, "hold"),
    ("右键长按", "right", 1, "hold"),
    ("中键长按", "middle", 1, "hold"),
    ("左键连击", "left", 0, "repeat"),
    ("右键连击", "right", 0, "repeat"),
    ("中键连击", "middle", 0, "repeat"),
]
#: 「动作」下拉里代表「用下面的连击次数」的那一项
CUSTOM_ACTION_LABEL = "自定义动作（用下面的连击次数）"

#: 常用键下拉项
COMMON_KEYS: list[tuple[str, str]] = [
    ("A", "a"), ("B", "b"), ("C", "c"), ("D", "d"), ("E", "e"), ("F", "f"),
    ("V", "v"), ("X", "x"), ("Z", "z"),
    ("Enter", "enter"), ("Space", "space"), ("Tab", "tab"), ("Esc", "esc"),
    ("Backspace", "backspace"), ("Delete", "delete"),
    ("↑", "up"), ("↓", "down"), ("←", "left"), ("→", "right"),
    ("Home", "home"), ("End", "end"),
    ("PageUp", "page_up"), ("PageDown", "page_down"),
    ("F1", "f1"), ("F2", "f2"), ("F5", "f5"), ("F9", "f9"), ("F12", "f12"),
]


@dataclass
class StepEdit:
    """编辑结果：只带用户真正改过的字段。"""

    kind: str                       # 新的步骤类型（click / multi / key / combo / move / scroll）
    x: Optional[int] = None
    y: Optional[int] = None
    button: Optional[str] = None
    click_count: Optional[int] = None
    key: Optional[str] = None
    vk: Optional[int] = None
    modifiers: Optional[list[str]] = None
    dx: Optional[int] = None
    dy: Optional[int] = None
    description: str = ""
    #: True 表示「把这个连击步骤拆成 N 个独立单击步骤」
    split_clicks: bool = False
    # ---- 时间（任务 2：从独立弹窗合并进来）----
    start_ms: Optional[int] = None        # 新的起始时间（None 表示不改）
    duration_ms: Optional[int] = None     # 新的持续时长（None 表示不改）
    # ---- 长按 + 无限连击（任务 3）----
    explicit_hold_ms: Optional[int] = None    # 长按持续时间（0=关闭）
    infinite_repeat: Optional[bool] = None    # 是否无限连击
    repeat_interval_ms: Optional[int] = None  # 连击间隔
    # ---- 鼠标移动：起始坐标（优化 2）----
    start_x: Optional[int] = None
    start_y: Optional[int] = None


def _int_field(minimum: int, maximum: int, value: int) -> NumberField:
    """自绘数字框（不用 QSpinBox：高 DPI 下编辑区会被上下按钮侧挡）。"""
    field = NumberField(minimum, maximum, int(value))
    return field


def _fmt_sec(value: float) -> str:
    """秒值文本：最多 3 位小数并去掉尾随 0（2.000 -> "2"，1.155 -> "1.155"）。"""
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _to_physical_point(point: tuple[int, int]) -> tuple[int, int]:
    """屏幕取点结果：Qt 逻辑坐标 → 物理像素。

    进程是 Per-Monitor DPI Aware（win_utils）+ Qt PassThrough 缩放，
    ScreenPicker 的 globalPosition 是逻辑像素；而录制/回放（pynput、
    SetCursorPos）用物理像素。显示缩放 ≠100% 时若不换算，取到的点
    回放会偏移缩放倍数（表现为「点击了其他坐标」）。
    """
    from PySide6.QtGui import QGuiApplication

    x, y = int(point[0]), int(point[1])
    screen = QGuiApplication.screenAt(QPoint(x, y))
    ratio = float(screen.devicePixelRatio()) if screen is not None else 1.0
    if ratio <= 0:
        ratio = 1.0
    return int(round(x * ratio)), int(round(y * ratio))


class _BaseEditDialog(QDialog):
    """编辑对话框基类：统一按钮、预览与主界面同款 QSS。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(430)
        # 显式套一次全局样式：保证弹窗里的按钮/输入框与主界面完全一致，
        # 不出现原生 Windows 灰色按钮。
        styles.apply_to_dialog(self)
        self._result: Optional[StepEdit] = None

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(18, 16, 18, 16)
        self._outer.setSpacing(12)

        head = QLabel(title)
        head.setObjectName("DialogHeading")
        self._outer.addWidget(head)

        self.preview = QLabel("")
        self.preview.setObjectName("DialogPreview")
        self.preview.setWordWrap(True)
        self._outer.addWidget(self.preview)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        self._ok = self._buttons.button(QDialogButtonBox.Ok)
        self._ok.setText("确定")
        self._ok.setObjectName("PrimaryButton")
        self._ok.setCursor(Qt.PointingHandCursor)
        self._cancel = self._buttons.button(QDialogButtonBox.Cancel)
        self._cancel.setText("取消")
        self._cancel.setObjectName("GhostButton")
        self._cancel.setCursor(Qt.PointingHandCursor)
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)

    def _finish(self, form: QWidget) -> None:
        """把表单塞进布局并接上按钮（子类构造末尾调用）。"""
        self._outer.addWidget(form)
        self._outer.addStretch(1)
        self._outer.addWidget(self._buttons)

    def result_edit(self) -> Optional[StepEdit]:
        return self._result

    def _set_preview_warning(self, warning: bool) -> None:
        """预览条在「提示」与「警告」两种配色间切换。"""
        name = "DialogWarn" if warning else "DialogPreview"
        if self.preview.objectName() != name:
            self.preview.setObjectName(name)
            # 改了 objectName 必须重新套一次样式，QSS 的选择器才会重新匹配
            self.preview.style().unpolish(self.preview)
            self.preview.style().polish(self.preview)

    def _on_accept(self) -> None:
        self._result = self.collect()
        self.accept()

    def collect(self) -> StepEdit:  # pragma: no cover - 子类实现
        raise NotImplementedError


class MouseStepDialog(_BaseEditDialog):
    """鼠标步骤编辑：动作（单击/双击/三击/自定义连击/左中右键）+ 坐标（x, y）。

    另外提供「拆分为独立单击」：把自动合并出来的连击步骤还原成 N 个单击步骤
    （拆分后的步骤带边界标记，不会再被自动合并粘回去）。
    """

    def __init__(self, step: ep.Step, parent: QWidget | None = None) -> None:
        super().__init__("编辑鼠标步骤", parent)
        self._step = step

        first = step.events[0] if step.events else {}
        x = int(first.get("x") or 0)
        y = int(first.get("y") or 0)
        button = str(first.get("button") or "left")
        count = step.click_count or ep.click_count_from_events(step.events) or 1

        form = QWidget(self)
        layout = QFormLayout(form)
        layout.setHorizontalSpacing(14)
        layout.setVerticalSpacing(8)

        # ---------- 动作 ----------
        self.action_combo = QComboBox(form)
        for label, btn, cnt, mode in MOUSE_ACTIONS:
            self.action_combo.addItem(label, (btn, cnt, mode))
        self.action_combo.addItem(CUSTOM_ACTION_LABEL, None)
        self.action_combo.setCurrentIndex(self._find_action(button, count, step))
        self.action_combo.currentIndexChanged.connect(self._on_action_changed)
        layout.addRow("动作", self.action_combo)

        # ---------- 连击次数（自定义动作时生效） ----------
        count_row = QWidget(form)
        count_layout = QHBoxLayout(count_row)
        count_layout.setContentsMargins(0, 0, 0, 0)
        count_layout.setSpacing(6)
        self.spin_count = NumberField(1, ep.MAX_CLICK_COUNT, count, count_row)
        self.spin_count.setFixedWidth(78)
        self.spin_count.setToolTip(
            "连续点击次数：≥2 时会被识别成一次「连击 N 次」步骤，\n"
            "模式二按一次热键就快速连发 N 次，保证系统能识别。"
        )
        self.spin_count.valueChanged.connect(self._refresh_preview)
        self.lbl_count_hint = QLabel("次连续点击", count_row)
        self.lbl_count_hint.setObjectName("Muted")
        count_layout.addWidget(self.spin_count)
        count_layout.addWidget(self.lbl_count_hint)
        count_layout.addStretch(1)
        layout.addRow("连击次数", count_row)

        # ---------- 坐标 ----------
        pos_row = QWidget(form)
        pos_layout = QHBoxLayout(pos_row)
        pos_layout.setContentsMargins(0, 0, 0, 0)
        pos_layout.setSpacing(6)
        self.spin_x = _int_field(-32768, 32767, x)
        self.spin_y = _int_field(-32768, 32767, y)
        pos_layout.addWidget(QLabel("X"))
        pos_layout.addWidget(self.spin_x, 1)
        pos_layout.addWidget(QLabel("Y"))
        pos_layout.addWidget(self.spin_y, 1)
        self.spin_x.valueChanged.connect(self._refresh_preview)
        self.spin_y.valueChanged.connect(self._refresh_preview)
        layout.addRow("坐标", pos_row)

        pick_row = QWidget(form)
        pick_layout = QHBoxLayout(pick_row)
        pick_layout.setContentsMargins(0, 0, 0, 0)
        from PySide6.QtWidgets import QPushButton

        self.btn_read_pos = QPushButton("读取当前鼠标坐标", pick_row)
        self.btn_read_pos.setObjectName("GhostButton")
        self.btn_read_pos.setCursor(Qt.PointingHandCursor)
        self.btn_read_pos.setToolTip(
            "打开全屏取点：移动鼠标查看实时坐标，左键单击填入 X / Y，Esc 取消"
        )
        self.btn_read_pos.clicked.connect(self._read_cursor)
        pick_layout.addWidget(self.btn_read_pos)
        pick_layout.addStretch(1)
        layout.addRow("", pick_row)

        # ---------- 拆分 ----------
        self.chk_split = QCheckBox("拆分为独立单击", form)
        self.chk_split.setToolTip(
            "把这个连击步骤还原成 N 个独立的「左键单击」步骤。\n"
            "拆分后的步骤不会再被自动合并粘回去（编辑结果会被尊重）。\n"
            "勾选后上面的动作与坐标设置不再生效。"
        )
        self.chk_split.toggled.connect(self._on_split_toggled)
        layout.addRow("", self.chk_split)

        # ---------- 时间 ----------
        time_box = QGroupBox("时间", form)
        time_layout = QFormLayout(time_box)
        time_layout.setContentsMargins(10, 8, 10, 8)
        time_layout.setHorizontalSpacing(14)
        time_layout.setVerticalSpacing(6)

        t_row = QWidget(time_box)
        t_layout = QHBoxLayout(t_row)
        t_layout.setContentsMargins(0, 0, 0, 0)
        t_layout.setSpacing(6)
        self.spin_start = NumberField(0, 99999.999, step.t_start / 1000.0,
                                      t_row, decimals=3, step=0.1)
        self.spin_start.setFixedWidth(90)
        self.spin_start.valueChanged.connect(self._refresh_preview)
        t_layout.addWidget(self.spin_start)
        lbl_s = QLabel("秒", t_row)
        lbl_s.setObjectName("Muted")
        t_layout.addWidget(lbl_s)
        t_layout.addStretch(1)
        time_layout.addRow("起始时间", t_row)

        d_row = QWidget(time_box)
        d_layout = QHBoxLayout(d_row)
        d_layout.setContentsMargins(0, 0, 0, 0)
        d_layout.setSpacing(6)
        self.spin_duration = NumberField(0, 99999.999, step.hold_ms / 1000.0,
                                         d_row, decimals=3, step=0.1)
        self.spin_duration.setFixedWidth(90)
        self.spin_duration.valueChanged.connect(self._refresh_preview)
        d_layout.addWidget(self.spin_duration)
        lbl_d = QLabel("秒", d_row)
        lbl_d.setObjectName("Muted")
        d_layout.addWidget(lbl_d)
        d_layout.addStretch(1)
        time_layout.addRow("持续时长", d_row)

        layout.addRow("", time_box)

        # ---------- 高级：长按 + 无限连击 ----------
        adv_box = QGroupBox("高级", form)
        adv_layout = QFormLayout(adv_box)
        adv_layout.setContentsMargins(10, 8, 10, 8)
        adv_layout.setHorizontalSpacing(14)
        adv_layout.setVerticalSpacing(6)

        self.chk_hold = QCheckBox("长按（按下后保持一段时间再释放）", adv_box)
        self.chk_hold.setChecked(step.explicit_hold_ms > 0)
        self.chk_hold.toggled.connect(self._on_hold_toggled)
        adv_layout.addRow("", self.chk_hold)

        hold_row = QWidget(adv_box)
        hold_layout = QHBoxLayout(hold_row)
        hold_layout.setContentsMargins(0, 0, 0, 0)
        hold_layout.setSpacing(6)
        self.spin_hold = NumberField(0.001, 99999.999,
                                     max(1, int(step.explicit_hold_ms or 100)) / 1000.0,
                                     hold_row, decimals=3, step=0.1)
        self.spin_hold.setFixedWidth(90)
        self.spin_hold.valueChanged.connect(self._refresh_preview)
        hold_layout.addWidget(self.spin_hold)
        lbl_h = QLabel("秒", hold_row)
        lbl_h.setObjectName("Muted")
        hold_layout.addWidget(lbl_h)
        hold_layout.addStretch(1)
        adv_layout.addRow("长按持续", hold_row)

        self.chk_infinite = QCheckBox("无限连击（一直重复直到按 F8 停止）", adv_box)
        self.chk_infinite.setChecked(bool(getattr(step, "infinite_repeat", False)))
        self.chk_infinite.toggled.connect(self._on_infinite_toggled)
        adv_layout.addRow("", self.chk_infinite)

        rep_row = QWidget(adv_box)
        rep_layout = QHBoxLayout(rep_row)
        rep_layout.setContentsMargins(0, 0, 0, 0)
        rep_layout.setSpacing(6)
        self.spin_repeat = NumberField(0.005, 99999.999,
                                       max(5, int(getattr(
                                           step, "repeat_interval_ms", 50))) / 1000.0,
                                       rep_row, decimals=3, step=0.05)
        self.spin_repeat.setFixedWidth(90)
        self.spin_repeat.valueChanged.connect(self._refresh_preview)
        rep_layout.addWidget(self.spin_repeat)
        lbl_r = QLabel("秒", rep_row)
        lbl_r.setObjectName("Muted")
        rep_layout.addWidget(lbl_r)
        rep_layout.addStretch(1)
        adv_layout.addRow("连击间隔", rep_row)

        layout.addRow("", adv_box)

        self._finish(form)
        self._sync_count_enabled()
        self._sync_advanced_enabled()
        self._refresh_preview()

    @staticmethod
    def _find_action(button: str, count: int, step: ep.Step) -> int:
        """按 (按键, 次数, 模式) 反查下拉项；没有精确匹配时用「自定义动作」。"""
        is_hold = getattr(step, "explicit_hold_ms", 0) > 0
        is_repeat = bool(getattr(step, "infinite_repeat", False))
        mode = "hold" if is_hold else ("repeat" if is_repeat else "click")
        for i, (_label, btn, cnt, md) in enumerate(MOUSE_ACTIONS):
            if btn == button and cnt == count and md == mode:
                return i
        # 退化匹配：只按按钮和次数
        for i, (_label, btn, cnt, md) in enumerate(MOUSE_ACTIONS):
            if btn == button and cnt == count and md == "click":
                return i
        return len(MOUSE_ACTIONS)      # 自定义那一项

    def _is_custom(self) -> bool:
        return self.action_combo.currentData() is None

    def _on_split_toggled(self, checked: bool) -> None:
        """勾选拆分时，动作/次数/坐标都不再参与，界面上灰掉以免误解。"""
        for widget in (self.action_combo, self.spin_x, self.spin_y,
                       self.btn_read_pos):
            widget.setEnabled(not checked)
        self._sync_count_enabled()
        self._sync_advanced_enabled()
        self._refresh_preview()

    def _sync_count_enabled(self) -> None:
        enabled = self._is_custom() and not self.chk_split.isChecked()
        self.spin_count.setEnabled(enabled)
        self.lbl_count_hint.setEnabled(enabled)

    def _sync_advanced_enabled(self) -> None:
        """拆分时禁用高级选项（拆分后每个单击步可单独设置）。"""
        disabled = self.chk_split.isChecked()
        self.chk_hold.setEnabled(not disabled)
        self.chk_infinite.setEnabled(not disabled)
        self.spin_hold.setEnabled(not disabled and self.chk_hold.isChecked())
        self.spin_repeat.setEnabled(not disabled and self.chk_infinite.isChecked())

    def _on_hold_toggled(self, checked: bool) -> None:
        self.spin_hold.setEnabled(checked)
        self._refresh_preview()

    def _on_infinite_toggled(self, checked: bool) -> None:
        self.spin_repeat.setEnabled(checked)
        self._refresh_preview()

    def _read_cursor(self) -> None:
        """全屏遮罩取点：左键把屏幕坐标填入 X / Y，Esc / 右键取消。"""
        from ui.screen_picker import pick_screen_point

        point = pick_screen_point(self)
        if point is not None:
            x, y = _to_physical_point(point)
            self.spin_x.setValue(x)
            self.spin_y.setValue(y)

    def _current(self) -> tuple[str, int]:
        """返回 ``(按键, 连击次数)``；选「自定义动作」时次数取 SpinBox。

        注意：先根据下拉项决定 SpinBox 是否可用，再读它的值 ——
        否则「切到自定义」的瞬间 SpinBox 仍是禁用状态，读到的还是旧值。
        """
        data = self.action_combo.currentData()
        if data is None:
            self._sync_count_enabled()
            return ("left", self.spin_count.value())
        button = str(data[0])
        count = int(data[1])
        # 连击模式的动作次数由 SpinBox 决定
        mode = data[2] if len(data) > 2 else "click"
        if mode == "repeat":
            count = self.spin_count.value() if self.spin_count.isEnabled() else 1
        return (button, max(1, count))

    def _refresh_preview(self) -> None:
        button, count = self._current()
        x, y = self.spin_x.value(), self.spin_y.value()

        if self.chk_split.isChecked():
            n = ep.click_count_from_events(self._step.events) or count
            self.preview.setText(
                f"将把这一步拆分为 {n} 个独立的「单击」步骤"
                f"（坐标与按键沿用当前设置，之后可分别编辑）。"
            )
            return

        name = ep.MOUSE_BUTTON_CN.get(button, button)
        verb = ep.click_verb(count)
        if button == "left":
            desc = f"鼠标{verb} ({x}, {y})"
        else:
            desc = f"鼠标{name}{verb} ({x}, {y})"

        # 附加信息：长按 / 无限连击
        extras = []
        if self.chk_hold.isChecked():
            extras.append(f"长按 {_fmt_sec(self.spin_hold.value())}s")
        if self.chk_infinite.isChecked():
            extras.append(f"无限连击（间隔 {_fmt_sec(self.spin_repeat.value())}s）")
        extra_str = "　·　" + "、".join(extras) if extras else ""
        event = {"t": 0, "type": "mouse_click", "action": "press",
                 "button": button, "x": x, "y": y}
        self.preview.setText(
            f"修改后：{desc}{extra_str}　（事件 {ep.describe(event)}）"
        )

    def _on_action_changed(self, _index: int) -> None:
        """切换动作时先把 SpinBox 的可用状态对齐，再刷新预览。

        选中长按类动作 → 自动勾选「长按」并启用持续时间输入框；
        选中连击类动作 → 自动勾选「无限连击」并启用间隔输入框。
        """
        data = self.action_combo.currentData()
        if data is not None:
            mode = data[2] if len(data) > 2 else "click"
            if mode == "hold":
                self.chk_hold.setChecked(True)
                self.chk_infinite.setChecked(False)
            elif mode == "repeat":
                self.chk_infinite.setChecked(True)
                self.chk_hold.setChecked(False)
        self._sync_count_enabled()
        self._sync_advanced_enabled()
        self._refresh_preview()

    def collect(self) -> StepEdit:
        button, count = self._current()
        if self.chk_split.isChecked():
            return StepEdit(kind="click", split_clicks=True)
        hold_ms = self.spin_hold.value() if self.chk_hold.isChecked() else 0
        return StepEdit(
            kind="multi" if count >= 2 else "click",
            button=button,
            click_count=count,
            x=self.spin_x.value(),
            y=self.spin_y.value(),
            start_ms=int(round(self.spin_start.value() * 1000)),
            duration_ms=int(round(self.spin_duration.value() * 1000)),
            explicit_hold_ms=hold_ms,
            infinite_repeat=self.chk_infinite.isChecked(),
            repeat_interval_ms=int(round(self.spin_repeat.value() * 1000)),
        )


class MoveStepDialog(_BaseEditDialog):
    """鼠标移动 / 滚轮步骤编辑：坐标（滚轮还可改格数）。"""

    def __init__(self, step: ep.Step, parent: QWidget | None = None) -> None:
        super().__init__("编辑鼠标移动" if step.kind == "move" else "编辑鼠标滚轮",
                         parent)
        self._step = step
        first = step.events[0] if step.events else {}
        last = step.events[-1] if step.events else {}

        form = QWidget(self)
        layout = QFormLayout(form)
        layout.setHorizontalSpacing(14)
        layout.setVerticalSpacing(8)

        is_move = (step.kind == "move")

        if is_move:
            # ---- 起始坐标 ----
            start_row = QWidget(form)
            start_layout = QHBoxLayout(start_row)
            start_layout.setContentsMargins(0, 0, 0, 0)
            start_layout.setSpacing(6)
            self.spin_start_x = _int_field(-32768, 32767, int(first.get("x") or 0))
            self.spin_start_y = _int_field(-32768, 32767, int(first.get("y") or 0))
            start_layout.addWidget(QLabel("X"))
            start_layout.addWidget(self.spin_start_x, 1)
            start_layout.addWidget(QLabel("Y"))
            start_layout.addWidget(self.spin_start_y, 1)
            self.btn_pick_start = QPushButton("屏幕取点")
            self.btn_pick_start.setObjectName("GhostButton")
            self.btn_pick_start.setCursor(Qt.PointingHandCursor)
            self.btn_pick_start.setToolTip("打开全屏取点：左键填入起始坐标，Esc 取消")
            self.btn_pick_start.clicked.connect(lambda: self._pick_point(self.spin_start_x, self.spin_start_y))
            start_layout.addWidget(self.btn_pick_start)
            layout.addRow("起始坐标", start_row)

            # ---- 移动到坐标 ----
            end_row = QWidget(form)
            end_layout = QHBoxLayout(end_row)
            end_layout.setContentsMargins(0, 0, 0, 0)
            end_layout.setSpacing(6)
            self.spin_x = _int_field(-32768, 32767, int(last.get("x") or 0))
            self.spin_y = _int_field(-32768, 32767, int(last.get("y") or 0))
            end_layout.addWidget(QLabel("X"))
            end_layout.addWidget(self.spin_x, 1)
            end_layout.addWidget(QLabel("Y"))
            end_layout.addWidget(self.spin_y, 1)
            self.btn_pick_end = QPushButton("屏幕取点")
            self.btn_pick_end.setObjectName("GhostButton")
            self.btn_pick_end.setCursor(Qt.PointingHandCursor)
            self.btn_pick_end.setToolTip("打开全屏取点：左键填入目标坐标，Esc 取消")
            self.btn_pick_end.clicked.connect(lambda: self._pick_point(self.spin_x, self.spin_y))
            end_layout.addWidget(self.btn_pick_end)
            layout.addRow("移动到坐标", end_row)

            self.spin_start_x.valueChanged.connect(self._refresh_preview)
            self.spin_start_y.valueChanged.connect(self._refresh_preview)
        else:
            # ---- 滚轮坐标（单一坐标，与移动坐标一样支持屏幕取点） ----
            pos_row = QWidget(form)
            pos_layout = QHBoxLayout(pos_row)
            pos_layout.setContentsMargins(0, 0, 0, 0)
            pos_layout.setSpacing(6)
            self.spin_x = _int_field(-32768, 32767, int(first.get("x") or 0))
            self.spin_y = _int_field(-32768, 32767, int(first.get("y") or 0))
            pos_layout.addWidget(QLabel("X"))
            pos_layout.addWidget(self.spin_x, 1)
            pos_layout.addWidget(QLabel("Y"))
            pos_layout.addWidget(self.spin_y, 1)
            self.btn_pick_end = QPushButton("屏幕取点")
            self.btn_pick_end.setObjectName("GhostButton")
            self.btn_pick_end.setCursor(Qt.PointingHandCursor)
            self.btn_pick_end.setToolTip("打开全屏取点：左键填入滚轮坐标，Esc 取消")
            self.btn_pick_end.clicked.connect(
                lambda: self._pick_point(self.spin_x, self.spin_y))
            pos_layout.addWidget(self.btn_pick_end)
            layout.addRow("坐标", pos_row)

        self.spin_dx: Optional[NumberField] = None
        self.spin_dy: Optional[NumberField] = None
        if step.kind == "scroll":
            scroll_row = QWidget(form)
            scroll_layout = QHBoxLayout(scroll_row)
            scroll_layout.setContentsMargins(0, 0, 0, 0)
            scroll_layout.setSpacing(6)
            self.spin_dy = _int_field(-50, 50, int(first.get("dy") or 0))
            self.spin_dx = _int_field(-50, 50, int(first.get("dx") or 0))
            scroll_layout.addWidget(QLabel("垂直"))
            scroll_layout.addWidget(self.spin_dy, 1)
            scroll_layout.addWidget(QLabel("水平"))
            scroll_layout.addWidget(self.spin_dx, 1)
            layout.addRow("滚动格数", scroll_row)
            self.spin_dy.valueChanged.connect(self._refresh_preview)
            self.spin_dx.valueChanged.connect(self._refresh_preview)

        self.spin_x.valueChanged.connect(self._refresh_preview)
        self.spin_y.valueChanged.connect(self._refresh_preview)

        # ---------- 时间 ----------
        time_box = QGroupBox("时间", form)
        time_layout = QFormLayout(time_box)
        time_layout.setContentsMargins(10, 8, 10, 8)
        time_layout.setHorizontalSpacing(14)
        time_layout.setVerticalSpacing(6)

        t_row = QWidget(time_box)
        t_layout = QHBoxLayout(t_row)
        t_layout.setContentsMargins(0, 0, 0, 0)
        t_layout.setSpacing(6)
        self.spin_start = NumberField(0, 99999.999, step.t_start / 1000.0,
                                      t_row, decimals=3, step=0.1)
        self.spin_start.setFixedWidth(90)
        self.spin_start.valueChanged.connect(self._refresh_preview)
        t_layout.addWidget(self.spin_start)
        lbl_s = QLabel("秒", t_row)
        lbl_s.setObjectName("Muted")
        t_layout.addWidget(lbl_s)
        t_layout.addStretch(1)
        time_layout.addRow("起始时间", t_row)

        d_row = QWidget(time_box)
        d_layout = QHBoxLayout(d_row)
        d_layout.setContentsMargins(0, 0, 0, 0)
        d_layout.setSpacing(6)
        self.spin_duration = NumberField(0, 99999.999, step.hold_ms / 1000.0,
                                         d_row, decimals=3, step=0.1)
        self.spin_duration.setFixedWidth(90)
        self.spin_duration.valueChanged.connect(self._refresh_preview)
        d_layout.addWidget(self.spin_duration)
        lbl_d = QLabel("秒", d_row)
        lbl_d.setObjectName("Muted")
        d_layout.addWidget(lbl_d)
        d_layout.addStretch(1)
        time_layout.addRow("持续时长", d_row)

        layout.addRow("", time_box)

        self._finish(form)
        self._refresh_preview()

    def _pick_point(self, spin_x: NumberField, spin_y: NumberField) -> None:
        """全屏遮罩取点：左键把屏幕坐标填入指定输入框。"""
        from ui.screen_picker import pick_screen_point

        point = pick_screen_point(self)
        if point is not None:
            x, y = _to_physical_point(point)
            spin_x.setValue(x)
            spin_y.setValue(y)

    def _refresh_preview(self) -> None:
        x, y = self.spin_x.value(), self.spin_y.value()
        if self._step.kind == "scroll":
            dy = self.spin_dy.value() if self.spin_dy else 0
            dx = self.spin_dx.value() if self.spin_dx else 0
            direction = "上滚" if dy > 0 else ("下滚" if dy < 0 else "横向滚动")
            self.preview.setText(
                f"修改后：滚轮{direction} {abs(dy) or abs(dx)} 格 @({x}, {y})"
            )
        else:
            sx = self.spin_start_x.value()
            sy = self.spin_start_y.value()
            self.preview.setText(
                f"修改后：从 ({sx}, {sy}) 移动到 ({x}, {y})"
            )

    def collect(self) -> StepEdit:
        if self._step.kind == "move":
            return StepEdit(
                kind=self._step.kind,
                x=self.spin_x.value(),
                y=self.spin_y.value(),
                start_x=self.spin_start_x.value(),
                start_y=self.spin_start_y.value(),
                start_ms=int(round(self.spin_start.value() * 1000)),
                duration_ms=int(round(self.spin_duration.value() * 1000)),
            )
        return StepEdit(
            kind=self._step.kind,
            x=self.spin_x.value(),
            y=self.spin_y.value(),
            dx=self.spin_dx.value() if self.spin_dx else None,
            dy=self.spin_dy.value() if self.spin_dy else None,
            start_ms=int(round(self.spin_start.value() * 1000)),
            duration_ms=int(round(self.spin_duration.value() * 1000)),
        )


class KeyStepDialog(_BaseEditDialog):
    """键盘步骤编辑：直接按键捕获，或从常用键下拉里选。"""

    def __init__(self, step: ep.Step, parent: QWidget | None = None) -> None:
        super().__init__("编辑键盘步骤", parent)
        self._step = step
        first = step.events[0] if step.events else {}
        token = str(first.get("key") or "vk:65")

        form = QWidget(self)
        layout = QFormLayout(form)
        layout.setHorizontalSpacing(14)
        layout.setVerticalSpacing(8)

        self.capture = KeyCaptureEdit(form)
        self.capture.set_token(token)
        self.capture.token_changed.connect(lambda _t: self._refresh_preview())
        layout.addRow("按键", self.capture)

        common_row = QWidget(form)
        common_layout = QHBoxLayout(common_row)
        common_layout.setContentsMargins(0, 0, 0, 0)
        common_layout.setSpacing(6)
        self.key_combo = QComboBox(common_row)
        self.key_combo.addItem("— 从常用键选择 —", "")
        for label, value in COMMON_KEYS:
            self.key_combo.addItem(label, value)
        self.key_combo.currentIndexChanged.connect(self._on_common)
        common_layout.addWidget(self.key_combo, 1)
        layout.addRow("常用键", common_row)

        hint = QLabel("提示：合并的「按下 + 释放」步骤只替换按键，按下/释放配对保持不变。")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        layout.addRow("", hint)

        # 捕获框没有信号，用定时器轮询来刷新预览
        from PySide6.QtCore import QTimer

        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._refresh_preview)
        self._timer.start()

        # ---------- 时间 ----------
        time_box = QGroupBox("时间", form)
        time_layout = QFormLayout(time_box)
        time_layout.setContentsMargins(10, 8, 10, 8)
        time_layout.setHorizontalSpacing(14)
        time_layout.setVerticalSpacing(6)

        t_row = QWidget(time_box)
        t_layout = QHBoxLayout(t_row)
        t_layout.setContentsMargins(0, 0, 0, 0)
        t_layout.setSpacing(6)
        self.spin_start = NumberField(0, 99999.999, step.t_start / 1000.0,
                                      t_row, decimals=3, step=0.1)
        self.spin_start.setFixedWidth(90)
        self.spin_start.valueChanged.connect(self._refresh_preview)
        t_layout.addWidget(self.spin_start)
        lbl_s = QLabel("秒", t_row)
        lbl_s.setObjectName("Muted")
        t_layout.addWidget(lbl_s)
        t_layout.addStretch(1)
        time_layout.addRow("起始时间", t_row)

        d_row = QWidget(time_box)
        d_layout = QHBoxLayout(d_row)
        d_layout.setContentsMargins(0, 0, 0, 0)
        d_layout.setSpacing(6)
        self.spin_duration = NumberField(0, 99999.999, step.hold_ms / 1000.0,
                                         d_row, decimals=3, step=0.1)
        self.spin_duration.setFixedWidth(90)
        self.spin_duration.valueChanged.connect(self._refresh_preview)
        d_layout.addWidget(self.spin_duration)
        lbl_d = QLabel("秒", d_row)
        lbl_d.setObjectName("Muted")
        d_layout.addWidget(lbl_d)
        d_layout.addStretch(1)
        time_layout.addRow("持续时长", d_row)

        layout.addRow("", time_box)

        # ---------- 高级：长按 + 无限连击 ----------
        adv_box = QGroupBox("高级", form)
        adv_layout = QFormLayout(adv_box)
        adv_layout.setContentsMargins(10, 8, 10, 8)
        adv_layout.setHorizontalSpacing(14)
        adv_layout.setVerticalSpacing(6)

        self.chk_hold = QCheckBox("长按（按下后保持一段时间再释放）", adv_box)
        self.chk_hold.setChecked(step.explicit_hold_ms > 0)
        self.chk_hold.toggled.connect(self._on_hold_toggled)
        adv_layout.addRow("", self.chk_hold)

        hold_row = QWidget(adv_box)
        hold_layout = QHBoxLayout(hold_row)
        hold_layout.setContentsMargins(0, 0, 0, 0)
        hold_layout.setSpacing(6)
        self.spin_hold = NumberField(0.001, 99999.999,
                                     max(1, int(step.explicit_hold_ms or 100)) / 1000.0,
                                     hold_row, decimals=3, step=0.1)
        self.spin_hold.setFixedWidth(90)
        self.spin_hold.valueChanged.connect(self._refresh_preview)
        hold_layout.addWidget(self.spin_hold)
        lbl_h = QLabel("秒", hold_row)
        lbl_h.setObjectName("Muted")
        hold_layout.addWidget(lbl_h)
        hold_layout.addStretch(1)
        adv_layout.addRow("长按持续", hold_row)

        self.chk_infinite = QCheckBox("无限连击（一直重复直到按 F8 停止）", adv_box)
        self.chk_infinite.setChecked(bool(getattr(step, "infinite_repeat", False)))
        self.chk_infinite.toggled.connect(self._on_infinite_toggled)
        adv_layout.addRow("", self.chk_infinite)

        rep_row = QWidget(adv_box)
        rep_layout = QHBoxLayout(rep_row)
        rep_layout.setContentsMargins(0, 0, 0, 0)
        rep_layout.setSpacing(6)
        self.spin_repeat = NumberField(0.005, 99999.999,
                                       max(5, int(getattr(
                                           step, "repeat_interval_ms", 50))) / 1000.0,
                                       rep_row, decimals=3, step=0.05)
        self.spin_repeat.setFixedWidth(90)
        self.spin_repeat.valueChanged.connect(self._refresh_preview)
        rep_layout.addWidget(self.spin_repeat)
        lbl_r = QLabel("秒", rep_row)
        lbl_r.setObjectName("Muted")
        rep_layout.addWidget(lbl_r)
        rep_layout.addStretch(1)
        adv_layout.addRow("连击间隔", rep_row)

        layout.addRow("", adv_box)

        self._finish(form)
        self._sync_advanced_enabled()
        self._refresh_preview()

    def _on_hold_toggled(self, checked: bool) -> None:
        self.spin_hold.setEnabled(checked)
        self._refresh_preview()

    def _on_infinite_toggled(self, checked: bool) -> None:
        self.spin_repeat.setEnabled(checked)
        self._refresh_preview()

    def _sync_advanced_enabled(self) -> None:
        self.spin_hold.setEnabled(self.chk_hold.isChecked())
        self.spin_repeat.setEnabled(self.chk_infinite.isChecked())

    def _on_common(self, _index: int) -> None:
        value = self.key_combo.currentData()
        if value:
            self.capture.set_token(str(value))
            self._refresh_preview()

    def _refresh_preview(self) -> None:
        token = self.capture.token()
        name = display_name(token)
        verb = "按下并释放" if len(self._step.events) >= 2 else (
            "按下" if self._step.events and self._step.events[0].get("action") == "press"
            else "释放"
        )
        extras = []
        if self.chk_hold.isChecked():
            extras.append(f"长按 {_fmt_sec(self.spin_hold.value())}s")
        if self.chk_infinite.isChecked():
            extras.append(f"无限连击（间隔 {_fmt_sec(self.spin_repeat.value())}s）")
        extra_str = "　·　" + "、".join(extras) if extras else ""
        self.preview.setText(f"修改后：键盘 {name} {verb}{extra_str}")

    def collect(self) -> StepEdit:
        token = self.capture.token()
        hold_ms = self.spin_hold.value() if self.chk_hold.isChecked() else 0
        return StepEdit(
            kind="key", key=token, vk=None,
            start_ms=int(round(self.spin_start.value() * 1000)),
            duration_ms=int(round(self.spin_duration.value() * 1000)),
            explicit_hold_ms=hold_ms,
            infinite_repeat=self.chk_infinite.isChecked(),
            repeat_interval_ms=int(round(self.spin_repeat.value() * 1000)),
        )


class ComboStepDialog(_BaseEditDialog):
    """组合键编辑：勾选修饰键 + 选目标键。"""

    def __init__(self, step: ep.Step, parent: QWidget | None = None) -> None:
        super().__init__("编辑组合键", parent)
        self._step = step

        form = QWidget(self)
        layout = QFormLayout(form)
        layout.setHorizontalSpacing(14)
        layout.setVerticalSpacing(8)

        # ---------- 修饰键 ----------
        mod_box = QGroupBox("修饰键", form)
        mod_layout = QHBoxLayout(mod_box)
        mod_layout.setContentsMargins(8, 6, 8, 6)
        mod_layout.setSpacing(10)
        self.mod_checks: dict[str, QCheckBox] = {}
        current = set(step.modifiers or ["Ctrl"])
        for name in ep.all_modifier_names():
            chk = QCheckBox(name, mod_box)
            chk.setChecked(name in current)
            chk.toggled.connect(self._refresh_preview)
            self.mod_checks[name] = chk
            mod_layout.addWidget(chk)
        mod_layout.addStretch(1)
        layout.addRow("", mod_box)

        # ---------- 目标键 ----------
        key_row = QWidget(form)
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        self.capture = KeyCaptureEdit(key_row)
        target_evt = ep._combo_target_event(step.events) or {}
        self.capture.set_token(str(target_evt.get("key") or "vk:86"))
        self.capture.token_changed.connect(lambda _t: self._refresh_preview())
        key_layout.addWidget(self.capture, 1)
        self.key_combo = QComboBox(key_row)
        self.key_combo.addItem("— 常用键 —", "")
        for label, value in COMMON_KEYS:
            self.key_combo.addItem(label, value)
        self.key_combo.currentIndexChanged.connect(self._on_common)
        key_layout.addWidget(self.key_combo, 1)
        layout.addRow("目标键", key_row)

        from PySide6.QtCore import QTimer

        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._refresh_preview)
        self._timer.start()

        # ---------- 时间 ----------
        time_box = QGroupBox("时间", form)
        time_layout = QFormLayout(time_box)
        time_layout.setContentsMargins(10, 8, 10, 8)
        time_layout.setHorizontalSpacing(14)
        time_layout.setVerticalSpacing(6)

        t_row = QWidget(time_box)
        t_layout = QHBoxLayout(t_row)
        t_layout.setContentsMargins(0, 0, 0, 0)
        t_layout.setSpacing(6)
        self.spin_start = NumberField(0, 99999.999, step.t_start / 1000.0,
                                      t_row, decimals=3, step=0.1)
        self.spin_start.setFixedWidth(90)
        self.spin_start.valueChanged.connect(self._refresh_preview)
        t_layout.addWidget(self.spin_start)
        lbl_s = QLabel("秒", t_row)
        lbl_s.setObjectName("Muted")
        t_layout.addWidget(lbl_s)
        t_layout.addStretch(1)
        time_layout.addRow("起始时间", t_row)

        d_row = QWidget(time_box)
        d_layout = QHBoxLayout(d_row)
        d_layout.setContentsMargins(0, 0, 0, 0)
        d_layout.setSpacing(6)
        self.spin_duration = NumberField(0, 99999.999, step.hold_ms / 1000.0,
                                         d_row, decimals=3, step=0.1)
        self.spin_duration.setFixedWidth(90)
        self.spin_duration.valueChanged.connect(self._refresh_preview)
        d_layout.addWidget(self.spin_duration)
        lbl_d = QLabel("秒", d_row)
        lbl_d.setObjectName("Muted")
        d_layout.addWidget(lbl_d)
        d_layout.addStretch(1)
        time_layout.addRow("持续时长", d_row)

        layout.addRow("", time_box)

        self._finish(form)
        self._refresh_preview()

    def _on_common(self, _index: int) -> None:
        value = self.key_combo.currentData()
        if value:
            self.capture.set_token(str(value))
            self._refresh_preview()

    def _selected_mods(self) -> list[str]:
        return [name for name, chk in self.mod_checks.items() if chk.isChecked()]

    def _refresh_preview(self) -> None:
        mods = self._selected_mods()
        target = display_name(self.capture.token())
        if not mods:
            self._set_preview_warning(True)
            self.preview.setText("⚠ 至少要勾选一个修饰键（Ctrl / Shift / Alt / Win）")
            return
        self._set_preview_warning(False)
        combo = "+".join([*mods, target])
        self.preview.setText(f"修改后：{ep.COMBO_PREFIX} {combo}")

    def _on_accept(self) -> None:
        if not self._selected_mods():
            self._set_preview_warning(True)
            self.preview.setText("⚠ 至少要勾选一个修饰键（Ctrl / Shift / Alt / Win）")
            return
        super()._on_accept()

    def collect(self) -> StepEdit:
        token = self.capture.token()
        return StepEdit(kind="combo", modifiers=self._selected_mods(),
                        key=token, vk=None,
                        start_ms=int(round(self.spin_start.value() * 1000)),
                        duration_ms=int(round(self.spin_duration.value() * 1000)))


def edit_step_dialog(step: ep.Step, parent: QWidget | None = None
                     ) -> Optional[StepEdit]:
    """按步骤类型弹出对应的编辑对话框；用户取消时返回 ``None``。"""
    if step.kind == "combo":
        dialog: _BaseEditDialog = ComboStepDialog(step, parent)
    elif step.kind in ("click", "multi"):
        dialog = MouseStepDialog(step, parent)
    elif step.kind in ("move", "scroll"):
        dialog = MoveStepDialog(step, parent)
    elif step.kind == "key":
        dialog = KeyStepDialog(step, parent)
    else:  # pragma: no cover - 未知类型
        return None

    if dialog.exec() != QDialog.Accepted:
        return None
    return dialog.result_edit()
