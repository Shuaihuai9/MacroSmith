"""插入步骤对话框：让用户填写新步骤的参数。

支持四种步骤类型，切换类型时表单会同步切换：

* **鼠标移动**：目标坐标
* **鼠标点击**：坐标 + 左/右/中键 + 重复次数
* **滚轮滚动**：方向与格数（可选坐标）
* **键盘按键**：单键捕获 + 重复次数
"""


from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from core import event_parser as ep
from core.hotkey_manager import display_name, normalize_token, probe_hotkey
from ui import styles
from ui.spin_box import NumberField


class KeyCaptureEdit(QLineEdit):
    """单键捕获输入框：聚焦后按任意键即录入。"""

    #: 键名变化时发出（token）；供编辑对话框实时刷新预览
    token_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setAlignment(Qt.AlignCenter)
        self.setPlaceholderText("点击后按键")
        self.setToolTip("点击此框后按下要模拟的按键（如 A、F5、Enter）")
        self._token = "a"
        self._text_display = "A"
        self._set_display("a")

    def _set_display(self, token: str) -> None:
        self._token = normalize_token(token)
        self._text_display = display_name(self._token)
        self.setText(self._text_display)

    def set_token(self, token: str) -> None:
        """外部设置当前按键（不会触发 ``token_changed``，避免递归）。"""
        text = str(token or "").strip()
        if text and probe_hotkey(text):
            self._set_display(text)
        elif text:
            # 无法解析（例如老脚本里的未知键）：原样展示，避免静默丢失
            self._token = text
            self._text_display = display_name(text)
            self.setText(self._text_display)

    def token(self) -> str:
        return self._token

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key_Escape, Qt.Key_unknown):
            self.clearFocus()
            event.accept()
            return
        if key in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_CapsLock):
            event.accept()
            return
        token = _token_from_event(event)
        if token and probe_hotkey(token):
            self._set_display(token)
            self.token_changed.emit(self._token)
        event.accept()


def _token_from_event(event: QKeyEvent) -> str:
    """把 Qt 按键事件转成键名 token（与热键设置页保持一致的规则）。"""
    key = event.key()
    if Qt.Key_F1 <= key <= Qt.Key_F35:
        return f"f{key - Qt.Key_F1 + 1}"
    specials = {
        Qt.Key_Space: "space", Qt.Key_Return: "enter", Qt.Key_Enter: "enter",
        Qt.Key_Tab: "tab", Qt.Key_Backspace: "backspace", Qt.Key_Delete: "delete",
        Qt.Key_Insert: "insert", Qt.Key_Home: "home", Qt.Key_End: "end",
        Qt.Key_PageUp: "page_up", Qt.Key_PageDown: "page_down",
        Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Left: "left", Qt.Key_Right: "right",
        Qt.Key_Escape: "escape",
    }
    if key in specials:
        return specials[key]
    text = event.text() or ""
    if len(text) == 1 and not text.isspace():
        return text.lower()
    return ""


class InsertStepDialog(QDialog):
    """插入新步骤类型选择窗口（精简版）。

    只让用户选步骤类型，点「插入」后由调用方弹出对应的编辑弹窗
    让用户填写具体参数。

    用法::

        dialog = InsertStepDialog(parent, position=0)
        if dialog.exec() == QDialog.Accepted:
            kind = dialog.selected_kind()
    """

    def __init__(self, parent: QWidget | None = None, position: int = 0) -> None:
        super().__init__(parent)
        self.setWindowTitle("插入新步骤")
        self.setModal(True)
        self.setMinimumWidth(380)
        styles.apply_to_dialog(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        title = QLabel(f"插入新步骤（将插入到第 {position + 1} 步）")
        title.setObjectName("DialogHeading")
        outer.addWidget(title)

        hint = QLabel("选择步骤类型后点击「插入」，将在下一步弹出对应的编辑弹窗填写参数。")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.kind_combo = QComboBox()
        self.kind_combo.addItem("鼠标点击", "click")
        self.kind_combo.addItem("鼠标移动", "move")
        self.kind_combo.addItem("鼠标滚轮", "scroll")
        self.kind_combo.addItem("键盘按键", "key")
        form.addRow("步骤类型", self.kind_combo)
        outer.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("插入")
        buttons.button(QDialogButtonBox.Ok).setObjectName("PrimaryButton")
        buttons.button(QDialogButtonBox.Ok).setCursor(Qt.PointingHandCursor)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Cancel).setObjectName("GhostButton")
        buttons.button(QDialogButtonBox.Cancel).setCursor(Qt.PointingHandCursor)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def selected_kind(self) -> str:
        """返回用户选中的步骤类型标识。"""
        return str(self.kind_combo.currentData())
