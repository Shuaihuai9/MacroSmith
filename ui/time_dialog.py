"""时间编辑对话框：单个步骤时间戳 / 整个脚本总时长。

* :class:`StepTimeDialog` —— 修改某一步骤的**起始时间**与**持续时长**
  （第九阶段起支持时长编辑：内部事件等比缩放、后续步骤整体平移）
* :class:`DurationDialog` —— 一键重设整个脚本的总时长（各步骤均匀分配）

交互风格与主界面一致：分/秒/毫秒分列输入（自绘 NumberField），
避免用户手写奇怪的时间字符串。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from core import event_parser as ep
from ui import styles
from ui.spin_box import NumberField


def _ms_to_parts(ms: int) -> tuple[int, int, int, int]:
    """毫秒拆成 (分, 秒, 毫秒, 是否含分钟)。"""
    ms = max(0, int(ms))
    minutes, rem = divmod(ms, 60_000)
    seconds, millis = divmod(rem, 1000)
    return minutes, seconds, millis, minutes


def _parts_to_ms(minutes: int, seconds: int, millis: int) -> int:
    return max(0, int(minutes) * 60_000 + int(seconds) * 1000 + int(millis))


class _TimeInputDialog(QDialog):
    """时间输入对话框基类：时/分/秒/毫秒分列 + 快捷预设。"""

    def __init__(self, title: str, hint: str, preset_label: str,
                 presets: tuple[tuple[str, int], ...],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(430)
        # 与步骤编辑弹窗同一套 QSS，避免原生灰色按钮
        styles.apply_to_dialog(self)

        self._value_ms = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        heading = QLabel(title)
        heading.setObjectName("DialogHeading")
        outer.addWidget(heading)

        hint_label = QLabel(hint)
        hint_label.setObjectName("Hint")
        hint_label.setWordWrap(True)
        outer.addWidget(hint_label)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        # 自绘数字框（不用 QSpinBox：其编辑区几何不吃 QSS padding，
        # 高 DPI 下数值会被右侧按钮侧挡，本项目三次实测复现）。
        # 单位用框外独立标签，三框统一固定宽度，任何字体/DPI 下等宽。
        self.spin_min = NumberField(0, 999, 0)
        self.spin_sec = NumberField(0, 59, 0)
        self.spin_ms = NumberField(0, 999, 0)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        for spin, unit in ((self.spin_min, "分"), (self.spin_sec, "秒"),
                           (self.spin_ms, "毫秒")):
            spin.setFixedWidth(78)
            row_layout.addWidget(spin)
            unit_label = QLabel(unit)
            unit_label.setObjectName("Muted")
            row_layout.addWidget(unit_label)
        row_layout.addStretch(1)
        form.addRow("时间", row)

        self.preview = QLabel("")
        self.preview.setObjectName("DialogPreview")
        form.addRow(preset_label, self.preview)
        outer.addLayout(form)

        if presets:
            preset_row = QHBoxLayout()
            preset_row.setSpacing(6)
            preset_row.addWidget(QLabel("快速设置"))
            for text, value in presets:
                button = QPushButton(text)
                button.setObjectName("GhostButton")
                button.setCursor(Qt.PointingHandCursor)
                button.clicked.connect(lambda _c=False, v=value: self.set_value_ms(v))
                preset_row.addWidget(button)
            preset_row.addStretch(1)
            outer.addLayout(preset_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Ok).setObjectName("PrimaryButton")
        buttons.button(QDialogButtonBox.Ok).setCursor(Qt.PointingHandCursor)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Cancel).setObjectName("GhostButton")
        buttons.button(QDialogButtonBox.Cancel).setCursor(Qt.PointingHandCursor)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        for spin in (self.spin_min, self.spin_sec, self.spin_ms):
            spin.valueChanged.connect(self._refresh_preview)

    # ------------------------------------------------------------------
    def set_value_ms(self, ms: int) -> None:
        minutes, seconds, millis, _ = _ms_to_parts(ms)
        for spin, value in ((self.spin_min, minutes), (self.spin_sec, seconds),
                            (self.spin_ms, millis)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self._refresh_preview()

    def value_ms(self) -> int:
        return _parts_to_ms(self.spin_min.value(), self.spin_sec.value(),
                            self.spin_ms.value())

    def _refresh_preview(self) -> None:
        self._value_ms = self.value_ms()
        self.preview.setText(f"= {self._value_ms} 毫秒（{ep.format_duration(self._value_ms)}）")


class _TimeFields(QWidget):
    """一组「分 / 秒 / 毫秒」NumberField + 快捷设置按钮（可复用的时间输入组）。"""

    valueChanged = Signal()

    def __init__(self, presets: tuple[tuple[str, int], ...],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 自绘数字框（不用 QSpinBox：其编辑区几何不吃 QSS padding，
        # 高 DPI 下数值会被右侧按钮侧挡，本项目三次实测复现）。
        self.spin_min = NumberField(0, 999, 0)
        self.spin_sec = NumberField(0, 59, 0)
        self.spin_ms = NumberField(0, 999, 0)
        row = QWidget(self)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        for spin, unit in ((self.spin_min, "分"), (self.spin_sec, "秒"),
                           (self.spin_ms, "毫秒")):
            spin.setFixedWidth(78)
            row_layout.addWidget(spin)
            unit_label = QLabel(unit)
            unit_label.setObjectName("Muted")
            row_layout.addWidget(unit_label)
        row_layout.addStretch(1)
        layout.addWidget(row)

        if presets:
            preset_row = QWidget(self)
            preset_layout = QHBoxLayout(preset_row)
            preset_layout.setContentsMargins(0, 0, 0, 0)
            preset_layout.setSpacing(6)
            label = QLabel("快速设置")
            label.setObjectName("Muted")
            preset_layout.addWidget(label)
            for text, value in presets:
                button = QPushButton(text)
                button.setObjectName("GhostButton")
                button.setCursor(Qt.PointingHandCursor)
                button.clicked.connect(
                    lambda _c=False, v=value: self.set_value_ms(v))
                preset_layout.addWidget(button)
            preset_layout.addStretch(1)
            layout.addWidget(preset_row)

        for spin in (self.spin_min, self.spin_sec, self.spin_ms):
            spin.valueChanged.connect(lambda _v: self.valueChanged.emit())

    def set_value_ms(self, ms: int) -> None:
        minutes, seconds, millis, _ = _ms_to_parts(ms)
        for spin, value in ((self.spin_min, minutes), (self.spin_sec, seconds),
                            (self.spin_ms, millis)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self.valueChanged.emit()

    def value_ms(self) -> int:
        return _parts_to_ms(self.spin_min.value(), self.spin_sec.value(),
                            self.spin_ms.value())


class StepTimeDialog(QDialog):
    """修改单个步骤的**起始时间**与**持续时长**（第九阶段重写）。

    联动规则（与 :func:`event_parser.set_step_time` /
    :func:`set_step_duration` 一一对应）：

    * 改「起始时间」：该步骤整体平移（内部间隔不变），其后所有步骤
      整体平移相同偏移量；
    * 改「持续时长」：起始不变，结束时间变化；步骤内部事件的时间
      偏移**等比缩放**（组合键/连击的内部节奏按比例保持），其后所有
      步骤整体平移相应偏移量；
    * 起始时间不能早于上一步的结束时间：超出时界面上给出警告，
      应用时会被自动夹到边界（主窗口消息里会注明实际调整值）。
    """

    def __init__(self, step_description: str, step_index: int, total: int,
                 current_start_ms: int, current_end_ms: int,
                 previous_end_ms: int,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("修改步骤时间")
        self.setModal(True)
        self.setMinimumWidth(480)
        # 与步骤编辑弹窗同一套 QSS，避免原生灰色按钮
        styles.apply_to_dialog(self)

        self._previous_end_ms = int(previous_end_ms)
        current_duration = max(0, int(current_end_ms) - int(current_start_ms))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        heading = QLabel("修改步骤时间")
        heading.setObjectName("DialogHeading")
        outer.addWidget(heading)

        info = QLabel(
            f"第 {step_index} / {total} 步：{step_description}\n"
            f"当前起始 {ep.format_duration(current_start_ms)}　·　"
            f"当前结束 {ep.format_duration(current_end_ms)}　·　"
            f"持续 {ep.format_duration(current_duration)}"
        )
        info.setObjectName("Hint")
        info.setWordWrap(True)
        outer.addWidget(info)

        # ---------- 起始时间 ----------
        start_box = QGroupBox("起始时间（修改后该步骤与其后所有步骤整体平移）", self)
        start_layout = QVBoxLayout(start_box)
        self.start_fields = _TimeFields(
            (("0", 0), ("0.5 秒", 500), ("1 秒", 1000),
             ("2 秒", 2000), ("5 秒", 5000)), start_box)
        start_layout.addWidget(self.start_fields)
        outer.addWidget(start_box)

        # ---------- 持续时长 ----------
        duration_box = QGroupBox(
            "持续时长（修改后该步骤结束时间变化，内部事件等比缩放，后续步骤整体平移）",
            self)
        duration_layout = QVBoxLayout(duration_box)
        self.duration_fields = _TimeFields(
            (("0.1 秒", 100), ("0.5 秒", 500), ("1 秒", 1000),
             ("2 秒", 2000), ("5 秒", 5000)), duration_box)
        duration_layout.addWidget(self.duration_fields)
        outer.addWidget(duration_box)

        hint = QLabel(
            "组合键 / 连击步骤修改时长时，内部事件的时间间隔按比例缩放（整体缩放），"
            "节奏保持不变。"
            + (f"\n起始时间不能早于 {ep.format_duration(self._previous_end_ms)}"
               "（上一步的结束时间），超出会自动调整到边界。"
               if self._previous_end_ms > 0 else "")
        )
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self.preview = QLabel("")
        self.preview.setObjectName("DialogPreview")
        self.preview.setWordWrap(True)
        outer.addWidget(self.preview)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Ok).setObjectName("PrimaryButton")
        buttons.button(QDialogButtonBox.Ok).setCursor(Qt.PointingHandCursor)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Cancel).setObjectName("GhostButton")
        buttons.button(QDialogButtonBox.Cancel).setCursor(Qt.PointingHandCursor)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self.start_fields.valueChanged.connect(self._refresh_preview)
        self.duration_fields.valueChanged.connect(self._refresh_preview)
        self.start_fields.set_value_ms(current_start_ms)
        self.duration_fields.set_value_ms(current_duration)
        self._refresh_preview()

    # ------------------------------------------------------------------
    def values(self) -> tuple[int, int]:
        """返回 ``(起始时间 ms, 持续时长 ms)``。"""
        return self.start_fields.value_ms(), self.duration_fields.value_ms()

    def _refresh_preview(self) -> None:
        start, duration = self.values()
        end = start + duration
        warning = (self._previous_end_ms > 0
                   and start < self._previous_end_ms + max(1, int(ep.MIN_STEP_GAP_MS)))
        name = "DialogWarn" if warning else "DialogPreview"
        if self.preview.objectName() != name:
            self.preview.setObjectName(name)
            # 改了 objectName 必须重新套一次样式，QSS 的选择器才会重新匹配
            self.preview.style().unpolish(self.preview)
            self.preview.style().polish(self.preview)
        text = (f"新起始 {ep.format_duration(start)} → "
                f"新结束 {ep.format_duration(end)}"
                f"（持续 {ep.format_duration(duration)}）")
        if warning:
            text += (f"　⚠ 早于上一步结束 {ep.format_duration(self._previous_end_ms)}，"
                     "应用时将自动调整到边界")
        self.preview.setText(text)


class DurationDialog(_TimeInputDialog):
    """重设整个脚本的总时长。"""

    def __init__(self, current_ms: int, step_count: int,
                 parent: QWidget | None = None) -> None:
        hint = (f"当前总时长 {ep.format_duration(current_ms)}（{current_ms} 毫秒），"
                f"共 {step_count} 个步骤。\n"
                f"确认后会把这 {step_count} 个步骤在目标时长内均匀铺开，"
                f"步骤内部的间隔保持不变。")
        super().__init__("重设总时长", hint, "目标时长",
                         (("10 秒", 10_000), ("30 秒", 30_000),
                          ("1 分钟", 60_000), ("2 分钟", 120_000),
                          ("5 分钟", 300_000)),
                         parent)
        self.set_value_ms(current_ms)
        if current_ms <= 0:
            self.set_value_ms(10_000)


def ask_step_time(step_description: str, step_index: int, total: int,
                  current_start_ms: int, current_end_ms: int,
                  previous_end_ms: int,
                  parent: QWidget | None = None
                  ) -> Optional[tuple[int, int]]:
    """弹出「修改步骤时间」对话框。

    返回 ``(起始时间 ms, 持续时长 ms)``；取消返回 ``None``。
    """
    dialog = StepTimeDialog(step_description, step_index, total,
                            current_start_ms, current_end_ms,
                            previous_end_ms, parent)
    if dialog.exec() == QDialog.Accepted:
        return dialog.values()
    return None


def ask_total_duration(current_ms: int, step_count: int,
                       parent: QWidget | None = None) -> Optional[int]:
    """弹出「重设总时长」对话框，返回毫秒；取消返回 None。"""
    dialog = DurationDialog(current_ms, step_count, parent)
    if dialog.exec() == QDialog.Accepted:
        return dialog.value_ms()
    return None
