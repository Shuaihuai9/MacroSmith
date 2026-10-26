"""左侧导航侧边栏（浅灰极简风格）。

宽度 64px、背景 ``#F3F3F3``、右侧 1px 分割线；图标为线性描边风格，
选中项用浅蓝底色 + 蓝色图标表示（不再是深色高亮）。
录制中会显示红色脉冲圆点。
"""


from PySide6.QtCore import (QEasingCurve, QPointF, QRectF, Qt,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from ui import icons, styles

SIDEBAR_WIDTH = styles.SIDEBAR_WIDTH


class IconButton(QWidget):
    """导航图标按钮：悬停浅灰底、激活浅蓝底 + 蓝色图标。"""

    clicked = Signal()

    def __init__(self, icon_name: str, tooltip: str, parent: QWidget | None = None,
                 size: int = 46, icon_size: int = 21) -> None:
        super().__init__(parent)
        self.icon_name = icon_name
        self._active = False
        self._hover = False
        self._pressed = False
        self._pulse = 0.0
        self._icon_size = icon_size
        self.setFixedSize(SIDEBAR_WIDTH, size)
        self.setToolTip(tooltip)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)

        # 脉冲动画：仅录制按钮使用，0 -> 1 -> 0 循环
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(780)
        self._anim.setEasingCurve(QEasingCurve.InOutSine)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_pulse_value)

    # ------------------------------------------------------------------
    def set_active(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self.update()

    @property
    def is_active(self) -> bool:
        return self._active

    def set_pulsing(self, pulsing: bool) -> None:
        """开始/停止红色脉冲动画。"""
        if pulsing:
            if self._anim.state() != QVariantAnimation.Running:
                self._anim.start()
        else:
            self._anim.stop()
            self._pulse = 0.0
            self.update()

    def _on_pulse_value(self, value) -> None:
        self._pulse = float(value)
        self.update()

    # ------------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self._pressed = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._pressed = True
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._pressed:
            self._pressed = False
            self.update()
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        rect = QRectF(self.rect())
        block = rect.adjusted(9, 5, -9, -5)
        if self._active:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(styles.SIDEBAR_ACTIVE))
            painter.drawRoundedRect(block, 9, 9)
        elif self._hover or self._pressed:
            painter.setPen(Qt.NoPen)
            color = QColor(styles.SIDEBAR_HOVER)
            if self._pressed:
                color = color.darker(104)
            painter.setBrush(color)
            painter.drawRoundedRect(block, 9, 9)

        icon_color = QColor(styles.SIDEBAR_ICON_ACTIVE if self._active
                            else styles.SIDEBAR_ICON)
        if self._hover and not self._active:
            icon_color = QColor(styles.BTN_GHOST_TEXT)
        icon_rect = QRectF(0, 0, self._icon_size, self._icon_size)
        icon_rect.moveCenter(rect.center())
        icons.paint_icon(painter, self.icon_name, icon_rect, icon_color, self._pulse)
        painter.end()


class Sidebar(QWidget):
    """浅色导航侧边栏。

    导航项：回放与脚本 / 录制与步骤编辑 / 设置 / 关于。
    回放没有独立页面（入口是录制页的按钮与全局热键），因此不再单列一项。
    """

    record_clicked = Signal()
    library_clicked = Signal()
    settings_clicked = Signal()
    about_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(SIDEBAR_WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 14, 0, 14)
        layout.setSpacing(4)

        # 顶部 logo：圆角方块 + 「宏」字
        self.logo = QWidget(self)
        self.logo.setFixedSize(SIDEBAR_WIDTH, 38)
        layout.addWidget(self.logo)
        layout.addSpacing(8)

        # 「回放与脚本」放在第一位：日常使用的主入口。
        # 录制页后移，避免用户默认停在录制页误覆写已保存脚本。
        self.btn_library = IconButton(
            icons.ICON_LIBRARY,
            "回放与脚本（选择脚本 · 循环设置 · 开始/停止回放）", self)
        self.btn_record = IconButton(
            icons.ICON_RECORD, "录制与步骤编辑（F9 停止录制）", self)
        self.btn_settings = IconButton(icons.ICON_SETTINGS, "设置（热键与录制选项）", self)
        self.btn_about = IconButton(icons.ICON_ABOUT, "关于 宏匠", self)

        self._buttons = {
            "library": self.btn_library,
            "record": self.btn_record,
            "settings": self.btn_settings,
            "about": self.btn_about,
        }
        for btn in self._buttons.values():
            layout.addWidget(btn, 0, Qt.AlignHCenter)

        layout.addStretch(1)

        self.btn_record.clicked.connect(self.record_clicked.emit)
        self.btn_library.clicked.connect(self.library_clicked.emit)
        self.btn_settings.clicked.connect(self.settings_clicked.emit)
        self.btn_about.clicked.connect(self.about_clicked.emit)

        self.set_active("library")

    # ------------------------------------------------------------------
    def set_active(self, key: str) -> None:
        """高亮当前选中的功能项。"""
        for name, btn in self._buttons.items():
            btn.set_active(name == key)

    def set_recording(self, recording: bool) -> None:
        self.btn_record.set_pulsing(recording)

    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        """自绘浅灰背景 + 底部圆角，与窗口圆角贴合。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect())
        r = float(styles.RADIUS_WINDOW)
        from PySide6.QtGui import QPainterPath

        path = QPainterPath()
        path.moveTo(rect.left(), rect.top())
        path.lineTo(rect.right(), rect.top())
        path.lineTo(rect.right(), rect.bottom() - r)
        path.quadTo(rect.right(), rect.bottom(), rect.right() - r, rect.bottom())
        path.lineTo(rect.left(), rect.bottom())
        path.closeSubpath()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(styles.SIDEBAR_BG))
        painter.drawPath(path)

        # 右侧分割线（到底部圆角处收住）
        painter.setPen(QColor(styles.BORDER))
        painter.drawLine(int(rect.right()) - 1, 0, int(rect.right()) - 1, int(rect.bottom()) - 6)

        # logo：圆角蓝底 + 白色「宏」
        logo_rect = QRectF(self.logo.geometry())
        block = QRectF(0, 0, 30, 30)
        block.moveCenter(logo_rect.center())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(styles.ACCENT))
        painter.drawRoundedRect(block, 8, 8)
        painter.setPen(QColor("#FFFFFF"))
        font = painter.font()
        font.setPointSize(12)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(block, Qt.AlignCenter, "宏")
        painter.end()
