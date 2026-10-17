"""自绘数字输入框：QLineEdit + 两个迷你上下按钮，布局完全自控。

为什么不用 QSpinBox
-------------------
QSpinBox 的上下按钮是 Qt 子控件，「编辑区」几何由样式系统内部计算，
QSS 的 padding 对它的文本裁剪边界不起作用（本项目实测：longhand
padding-right 完全无效，全写 padding 也不可靠）。在 125%/150% DPI、
不同字体下，数值与后缀会被右侧按钮侧挡——时间弹窗与轮数框先后
三次复现。与其和子控件几何斗智斗勇，不如自己拼一个：

* 外框 = 一个普通 QWidget（圆角边框、hover/聚焦变色，走 QSS）
* 数字区 = 一个无边框 QLineEdit（占满剩余宽度，天然不会“被挡”）
* 调节区 = 两个上下排列的迷你 QToolButton（支持按住连续增减）

接口与 QSpinBox 的常用子集兼容：``setRange`` / ``setValue`` /
``value`` / ``valueChanged`` / ``setEnabled`` / ``blockSignals`` /
``setFixedWidth``，调用方基本不用改代码。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRegularExpression, Qt, Signal
from PySide6.QtGui import QIntValidator, QPainter, QRegularExpressionValidator
from PySide6.QtWidgets import (QHBoxLayout, QLineEdit, QStyle, QStyleOption,
                               QToolButton, QVBoxLayout, QWidget)


class NumberField(QWidget):
    """数值输入框（自绘版 QSpinBox）。

    ``decimals=0``（默认）为整数模式，行为与旧版完全一致；
    ``decimals>0`` 启用小数模式（如秒输入），``value()`` 返回 ``float``，
    显示时去掉尾随 0，``step`` 为上下按钮/滚轮的步长。
    """

    #: 整数模式发 ``int``，小数模式发 ``float``（object 信号原样透传）
    valueChanged = Signal(object)

    def __init__(self, minimum: float = 0, maximum: float = 999, value: float = 0,
                 parent: QWidget | None = None, decimals: int = 0,
                 step: float = 1.0) -> None:
        super().__init__(parent)
        self.setObjectName("NumberField")
        self._decimals = max(0, int(decimals))
        self._step = float(step) if step else 1.0
        self._minimum = self._quantize(minimum)
        self._maximum = self._quantize(maximum)
        self._value = self._clamp(value)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 1, 3, 1)
        layout.setSpacing(0)

        self.edit = QLineEdit(self._display(self._value), self)
        self.edit.setObjectName("NumberFieldEdit")
        if self._decimals:
            self.edit.setValidator(QRegularExpressionValidator(
                QRegularExpression(r"-?\d{0,6}(?:\.\d{0,%d})?" % self._decimals),
                self))
        else:
            self.edit.setValidator(QIntValidator(self._minimum, self._maximum, self))
        self.edit.textChanged.connect(self._on_text_changed)
        self.edit.editingFinished.connect(self._on_editing_finished)
        self.edit.installEventFilter(self)
        layout.addWidget(self.edit, 1)

        arrows = QVBoxLayout()
        arrows.setContentsMargins(0, 0, 0, 0)
        arrows.setSpacing(0)
        self.btn_up = self._make_arrow("▲", "NumberFieldUp", 1)
        self.btn_down = self._make_arrow("▼", "NumberFieldDown", -1)
        arrows.addWidget(self.btn_up)
        arrows.addWidget(self.btn_down)
        layout.addLayout(arrows)

        self.setFocusProxy(self.edit)
        self.setMinimumHeight(30)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """QWidget 子类必须显式画自己，QSS 的背景/边框才会生效。"""
        opt = QStyleOption()
        opt.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PE_Widget, opt, painter, self)
        painter.end()

    def _make_arrow(self, text: str, name: str, step: int) -> QToolButton:
        btn = QToolButton(self)
        btn.setObjectName(name)
        btn.setText(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setAutoRepeat(True)          # 按住连续增减，手感同 QSpinBox
        btn.setAutoRepeatInterval(80)
        btn.clicked.connect(lambda _c=False, s=step: self.step_by(s))
        return btn

    # ------------------------------------------------------------------
    # 值
    # ------------------------------------------------------------------
    def _quantize(self, value: float):
        """按模式量化：整数模式取整，小数模式四舍五入到设定小数位。"""
        if self._decimals:
            return round(float(value), self._decimals)
        return int(value)

    def _display(self, value) -> str:
        """当前值的显示文本（小数模式去掉尾随 0：2.000 -> "2"）。"""
        if self._decimals:
            text = f"{float(value):.{self._decimals}f}".rstrip("0").rstrip(".")
            return text or "0"
        return str(value)

    def _clamp(self, value: float):
        return max(self._minimum, min(self._maximum, self._quantize(value)))

    def value(self):
        return self._value

    def setValue(self, value: float) -> None:
        value = self._clamp(value)
        changed = value != self._value
        self._value = value
        if self.edit.text() != self._display(value):
            self.edit.blockSignals(True)
            self.edit.setText(self._display(value))
            self.edit.blockSignals(False)
        if changed:
            self.valueChanged.emit(value)

    def setRange(self, minimum: float, maximum: float) -> None:
        self._minimum = self._quantize(minimum)
        self._maximum = self._quantize(maximum)
        if not self._decimals:
            self.edit.setValidator(QIntValidator(self._minimum, self._maximum, self))
        self.setValue(self._value)      # 按新区间重新夹紧

    def step_by(self, delta: int) -> None:
        if self._decimals:
            self.setValue(self._value + self._step * delta)
        else:
            self.setValue(self._value + delta)

    # ------------------------------------------------------------------
    # 输入同步
    # ------------------------------------------------------------------
    def _on_text_changed(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        try:
            value = float(text) if self._decimals else int(text)
        except ValueError:
            return
        value = self._clamp(value)
        if value != self._value:
            self._value = value
            self.valueChanged.emit(value)

    def _on_editing_finished(self) -> None:
        """失焦/回车时把文本规范成当前值（去前导零、空框回填）。"""
        if self.edit.text() != self._display(self._value):
            self.edit.blockSignals(True)
            self.edit.setText(self._display(self._value))
            self.edit.blockSignals(False)

    # ------------------------------------------------------------------
    # 键盘 / 滚轮 / 聚焦样式
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 命名
        if obj is self.edit:
            etype = event.type()
            if etype == QEvent.KeyPress:
                key = event.key()
                if key == Qt.Key_Up:
                    self.step_by(1)
                    return True
                if key == Qt.Key_Down:
                    self.step_by(-1)
                    return True
            elif etype == QEvent.FocusIn:
                self._set_focused_style(True)
            elif etype == QEvent.FocusOut:
                self._set_focused_style(False)
        return super().eventFilter(obj, event)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.step_by(1 if event.angleDelta().y() > 0 else -1)
        event.accept()

    def _set_focused_style(self, focused: bool) -> None:
        """QSS 不支持 :focus-within，用动态属性驱动聚焦边框变色。"""
        if self.property("focused") == focused:
            return
        self.setProperty("focused", focused)
        self.style().unpolish(self)
        self.style().polish(self)
