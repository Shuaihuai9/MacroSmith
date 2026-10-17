"""脚本命名对话框：新建 / 重命名 / 另存为 共用一个输入弹窗。

不用 ``QInputDialog.getText`` 的原因：那是 Qt 原生弹窗，按钮与输入框
不会继承应用级 QSS，在 Windows 上会退化成灰色的老旧外观，
与本项目的现代极简风格不统一。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QLineEdit, QVBoxLayout, QWidget)

from ui import styles


class NameInputDialog(QDialog):
    """单行文本输入弹窗（脚本名称）。"""

    def __init__(self, title: str, label: str, initial: str = "",
                 placeholder: str = "", hint: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(430)
        styles.apply_to_dialog(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)

        heading = QLabel(title)
        heading.setObjectName("DialogHeading")
        outer.addWidget(heading)

        tip = QLabel(label)
        tip.setObjectName("Hint")
        tip.setWordWrap(True)
        outer.addWidget(tip)

        self.edit = QLineEdit(self)
        self.edit.setText(initial)
        if placeholder:
            self.edit.setPlaceholderText(placeholder)
        self.edit.selectAll()
        self.edit.returnPressed.connect(self._on_accept)
        outer.addWidget(self.edit)

        if hint:
            note = QLabel(hint)
            note.setObjectName("Muted")
            note.setWordWrap(True)
            outer.addWidget(note)

        self.warn = QLabel("")
        self.warn.setObjectName("DialogWarn")
        self.warn.setWordWrap(True)
        self.warn.setVisible(False)
        outer.addWidget(self.warn)

        # 校验提示行：显示「将写入的文件名」
        self.preview = QLabel("")
        self.preview.setObjectName("DialogPreview")
        self.preview.setWordWrap(True)
        outer.addWidget(self.preview)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
                                   self)
        self._ok = buttons.button(QDialogButtonBox.Ok)
        self._ok.setText("确定")
        self._ok.setObjectName("PrimaryButton")
        self._ok.setCursor(Qt.PointingHandCursor)
        cancel = buttons.button(QDialogButtonBox.Cancel)
        cancel.setText("取消")
        cancel.setObjectName("GhostButton")
        cancel.setCursor(Qt.PointingHandCursor)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._existing: set[str] = set()
        self._ignore_stems: set[str] = set()
        self.edit.textChanged.connect(self._refresh_preview)
        self._refresh_preview()

    # ------------------------------------------------------------------
    def set_existing(self, stems: set[str], ignore: set[str] | None = None) -> None:
        """告诉弹窗哪些文件名已存在（用于覆盖提示）。"""
        self._existing = {s.lower() for s in stems}
        self._ignore_stems = {s.lower() for s in (ignore or set())}
        self._refresh_preview()

    def name(self) -> str:
        return self.edit.text().strip()

    def _safe_stem(self) -> str:
        name = self.name()
        if not name:
            return ""
        invalid = '<>:"/\\|?*'
        cleaned = "".join("_" if ch in invalid else ch for ch in name).strip(" .")
        return cleaned or ""

    def _refresh_preview(self) -> None:
        stem = self._safe_stem()
        if not stem:
            self.preview.setText("请输入脚本名称。")
            self._ok.setEnabled(False)
            self.warn.setVisible(False)
            return

        self._ok.setEnabled(True)
        overwrite = (stem.lower() in self._existing
                     and stem.lower() not in self._ignore_stems)
        if overwrite:
            self.preview.setText(f"将写入：{stem}.json")
            self.warn.setText(f"⚠ 已存在同名脚本「{stem}」，确定后会覆盖它。")
            self.warn.setVisible(True)
        else:
            self.preview.setText(f"将写入：{stem}.json")
            self.warn.setVisible(False)

    def _on_accept(self) -> None:
        if not self._safe_stem():
            self._refresh_preview()
            return
        self.accept()

    def result_name(self) -> Optional[str]:
        """返回清洗后的名称（用户取消时返回 ``None``）。"""
        stem = self._safe_stem()
        return stem or None


def ask_name(title: str, label: str, initial: str = "", placeholder: str = "",
             hint: str = "", existing: set[str] | None = None,
             ignore: set[str] | None = None,
             parent: QWidget | None = None) -> Optional[str]:
    """弹出脚本命名对话框；确定返回名称，取消返回 ``None``。"""
    dialog = NameInputDialog(title, label, initial, placeholder, hint, parent)
    if existing:
        dialog.set_existing(existing, ignore)
    if dialog.exec() != QDialog.Accepted:
        return None
    return dialog.result_name()


def script_stems(directory: Path) -> set[str]:
    """脚本目录下已有的文件名（去扩展名），用于覆盖判断。"""
    directory = Path(directory)
    if not directory.exists():
        return set()
    return {p.stem for p in directory.glob("*.json") if p.is_file()}
