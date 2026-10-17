"""全屏遮罩取点：十字准星 + 实时坐标，左键取点，Esc / 右键取消。

用在「编辑鼠标步骤」的「读取当前鼠标坐标」与「插入新步骤」的坐标行：
点击按钮后弹出覆盖全部显示器的半透明深色遮罩，鼠标移动时显示
十字线与当前屏幕坐标，左键单击把坐标填回输入框。

多显示器说明
------------
窗口几何取所有屏幕的并集（Qt 虚拟桌面），多屏时左上角可能是负坐标，
``setGeometry`` 直接吃这个并集即可；鼠标事件里的 ``globalPosition``
天然就是屏幕坐标，无需任何换算。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import (QColor, QGuiApplication, QKeyEvent, QMouseEvent,
                           QPainter, QPen)
from PySide6.QtWidgets import QDialog, QWidget

#: 遮罩底色（半透明深黑）
_OVERLAY = QColor(15, 17, 21, 118)
#: 十字线与文字颜色
_CROSS = QColor(255, 255, 255, 235)
#: 坐标标签底色
_LABEL_BG = QColor(20, 22, 26, 220)


class ScreenPicker(QDialog):
    """无边框全屏取点遮罩。

    用法::

        point = pick_screen_point(parent)
        if point is not None:
            x, y = point
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.setModal(True)
        #: 取到的屏幕坐标；取消时为 None
        self.picked: Optional[tuple[int, int]] = None
        self._local = QPoint(-1, -1)    # 光标（窗口内坐标，供绘制）
        self._global = QPoint(-1, -1)   # 光标（屏幕坐标，供显示/取点）
        # 覆盖所有屏幕（虚拟桌面并集）
        self.setGeometry(self._virtual_geometry())

    @staticmethod
    def _virtual_geometry() -> QRect:
        rect = QRect()
        screens = QGuiApplication.screens()
        for screen in screens:
            rect = rect.united(screen.geometry())
        if rect.isNull():  # pragma: no cover - 理论上总有主屏
            rect = QGuiApplication.primaryScreen().geometry()
        return rect

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.fillRect(self.rect(), _OVERLAY)
        self._paint_hint(painter)
        if self._local.x() >= 0:
            self._paint_crosshair(painter)
        painter.end()

    def _paint_hint(self, painter: QPainter) -> None:
        """顶部居中操作提示。"""
        text = "移动鼠标选择坐标 · 左键取点 · Esc / 右键取消"
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 32
        height = metrics.height() + 14
        x = (self.width() - width) // 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(_LABEL_BG)
        painter.drawRoundedRect(x, 24, width, height, 7, 7)
        painter.setPen(_CROSS)
        painter.drawText(QRect(x, 24, width, height), Qt.AlignCenter, text)

    def _paint_crosshair(self, painter: QPainter) -> None:
        pen = QPen(_CROSS)
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(0, self._local.y(), self.width(), self._local.y())
        painter.drawLine(self._local.x(), 0, self._local.x(), self.height())

        text = f"X: {self._global.x()}   Y: {self._global.y()}"
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 20
        height = metrics.height() + 10
        # 默认放光标右下方；靠近右/下边缘时翻到左/上侧，防止画出屏幕
        x = self._local.x() + 14
        y = self._local.y() + 14
        if x + width > self.width() - 4:
            x = self._local.x() - width - 14
        if y + height > self.height() - 4:
            y = self._local.y() - height - 14
        painter.setPen(Qt.NoPen)
        painter.setBrush(_LABEL_BG)
        painter.drawRoundedRect(x, y, width, height, 5, 5)
        painter.setPen(_CROSS)
        painter.drawText(QRect(x, y, width, height), Qt.AlignCenter, text)

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._local = event.position().toPoint()
        self._global = event.globalPosition().toPoint()
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            pos = event.globalPosition().toPoint()
            self.picked = (pos.x(), pos.y())
            self.accept()
        else:
            # 右键 / 中键都视为取消（与 Esc 等价）
            self.reject()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    @staticmethod
    def pick(parent: QWidget | None = None) -> Optional[tuple[int, int]]:
        """弹出遮罩取点；取消返回 ``None``，成功返回 ``(x, y)``。"""
        dialog = ScreenPicker(parent)
        if dialog.exec() == QDialog.Accepted and dialog.picked is not None:
            return dialog.picked
        return None


def pick_screen_point(parent: QWidget | None = None) -> Optional[tuple[int, int]]:
    """``ScreenPicker.pick`` 的函数式封装，方便调用方与测试打桩。"""
    return ScreenPicker.pick(parent)
