"""侧边栏与操作图标（全部用 QPainter 线性绘制，无图片资源依赖）。

风格统一：2px 线性描边、圆头端点、不填充色块，符合现代极简的视觉语言。
新增 ``cursor`` / ``library`` / ``plus`` / ``minus`` / ``arrow_up`` / ``arrow_down``
等编辑与脚本库图标。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

from ui import styles

# 导航图标
ICON_RECORD = "record"
ICON_CURSOR = "cursor"
ICON_PLAY = "play"
ICON_STEP = "step"
ICON_LIBRARY = "library"
ICON_SETTINGS = "settings"
ICON_ABOUT = "about"
ICON_BACK = "back"

# 编辑工具栏图标
ICON_CHECK_ALL = "check_all"
ICON_CHECK_NONE = "check_none"
ICON_INVERT = "invert"
ICON_TRASH = "trash"
ICON_ARROW_UP = "arrow_up"
ICON_ARROW_DOWN = "arrow_down"
ICON_PLUS = "plus"
ICON_SAVE = "save"
ICON_REFRESH = "refresh"
ICON_FOLDER = "folder"
ICON_CLOCK = "clock"
ICON_TIMELINE = "timeline"
ICON_EDIT = "edit"


def paint_icon(painter: QPainter, name: str, rect: QRectF,
               color: str | QColor, pulse: float = 0.0) -> None:
    """在 ``rect`` 内绘制指定图标。

    参数
    ----
    pulse:
        0~1 的脉冲强度，仅对 ``record`` 生效：录制时在图标右下角画一个
        红色圆点，半径与透明度随该值呼吸变化。
    """
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    col = QColor(color) if not isinstance(color, QColor) else color
    side = min(rect.width(), rect.height())
    box = QRectF(
        rect.center().x() - side / 2.0,
        rect.center().y() - side / 2.0,
        side, side,
    )
    stroke = max(1.4, side * 0.09)
    pen = QPen(col)
    pen.setWidthF(stroke)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    drawer = _DRAWERS.get(name)
    if drawer is None:
        return
    try:
        if name == ICON_RECORD:
            drawer(painter, box, pen, col, pulse)
        else:
            drawer(painter, box, pen, col)
    finally:
        painter.restore()


# ---------------------------------------------------------------------------
# 各个图标（统一约定：函数签名 (painter, box, pen, color)）
# ---------------------------------------------------------------------------
def _r(box: QRectF, fx: float, fy: float) -> QPointF:
    """按 0~1 的比例取 box 内的点，方便按比例描点。"""
    return QPointF(box.left() + box.width() * fx, box.top() + box.height() * fy)


def _circle(painter: QPainter, box: QRectF, scale: float = 0.36) -> None:
    r = box.width() * scale
    painter.drawEllipse(box.center(), r, r)


def _paint_record(painter: QPainter, box: QRectF, pen: QPen, color: QColor,
                  pulse: float = 0.0) -> None:
    """录制：同心圆（镜头感），录制中右下角加红色脉冲圆点。"""
    painter.setPen(pen)
    _circle(painter, box, 0.37)
    r2 = box.width() * 0.15
    painter.save()
    painter.setPen(Qt.NoPen)
    painter.setBrush(pen.color())
    painter.drawEllipse(box.center(), r2, r2)
    painter.restore()

    if pulse > 0.001:
        dot_c = QPointF(box.right() - box.width() * 0.10, box.bottom() - box.height() * 0.10)
        radius = box.width() * (0.12 + 0.07 * pulse)
        glow = QColor(styles.RECORD_RED)
        glow.setAlphaF(0.18 + 0.5 * pulse)
        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(dot_c, radius * 1.9, radius * 1.9)
        painter.setBrush(QColor(styles.RECORD_RED))
        painter.drawEllipse(dot_c, radius, radius)
        painter.restore()


def _paint_cursor(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """鼠标指针（箭头轮廓），表示“点击坐标”。"""
    painter.setPen(pen)
    path = QPainterPath()
    path.moveTo(_r(box, 0.28, 0.18))
    path.lineTo(_r(box, 0.28, 0.80))
    path.lineTo(_r(box, 0.45, 0.63))
    path.lineTo(_r(box, 0.56, 0.85))
    path.lineTo(_r(box, 0.68, 0.79))
    path.lineTo(_r(box, 0.57, 0.57))
    path.lineTo(_r(box, 0.78, 0.53))
    path.closeSubpath()
    painter.drawPath(path)


def _paint_play(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """回放：描边三角形。"""
    painter.setPen(pen)
    path = QPainterPath()
    path.moveTo(_r(box, 0.34, 0.24))
    path.lineTo(_r(box, 0.76, 0.50))
    path.lineTo(_r(box, 0.34, 0.76))
    path.closeSubpath()
    painter.drawPath(path)


def _paint_edit(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """编辑：一支斜放的铅笔。"""
    painter.setPen(pen)
    # 笔身：从左上到右下的平行四边形
    path = QPainterPath()
    path.moveTo(_r(box, 0.22, 0.66))
    path.lineTo(_r(box, 0.60, 0.24))
    path.lineTo(_r(box, 0.78, 0.40))
    path.lineTo(_r(box, 0.38, 0.80))
    path.closeSubpath()
    painter.drawPath(path)
    # 笔尖：左下角的小三角
    painter.drawLine(_r(box, 0.22, 0.66), _r(box, 0.38, 0.80))
    # 底部横线（“写在纸上”）
    painter.drawLine(_r(box, 0.20, 0.88), _r(box, 0.80, 0.88))


def _paint_step(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """单步：播放三角 + 右侧竖条。"""
    painter.setPen(pen)
    path = QPainterPath()
    path.moveTo(_r(box, 0.28, 0.26))
    path.lineTo(_r(box, 0.64, 0.50))
    path.lineTo(_r(box, 0.28, 0.74))
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawLine(_r(box, 0.74, 0.28), _r(box, 0.74, 0.72))


def _paint_library(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """脚本库：叠放的书本 / 图层。"""
    painter.setPen(pen)
    painter.drawRoundedRect(QRectF(_r(box, 0.20, 0.22), _r(box, 0.80, 0.48)), 1.6, 1.6)
    painter.drawLine(_r(box, 0.14, 0.62), _r(box, 0.86, 0.62))
    painter.drawLine(_r(box, 0.20, 0.75), _r(box, 0.80, 0.75))
    painter.drawLine(_r(box, 0.30, 0.34), _r(box, 0.70, 0.34))


def _paint_settings(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """设置：六齿齿轮轮廓 + 中心点。"""
    painter.setPen(pen)
    center = box.center()
    r_ring = box.width() * 0.23
    r_tooth = box.width() * 0.40
    r_inner = box.width() * 0.31
    for i in range(6):
        angle = math.radians(i * 60 - 90)
        x1 = center.x() + math.cos(angle) * r_inner
        y1 = center.y() + math.sin(angle) * r_inner
        x2 = center.x() + math.cos(angle) * r_tooth
        y2 = center.y() + math.sin(angle) * r_tooth
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    painter.drawEllipse(center, r_ring, r_ring)
    painter.save()
    painter.setBrush(pen.color())
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(center, box.width() * 0.07, box.width() * 0.07)
    painter.restore()


def _paint_about(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """关于：圆圈里一个 i。"""
    painter.setPen(pen)
    r = box.width() * 0.38
    painter.drawEllipse(box.center(), r, r)
    painter.save()
    painter.setBrush(pen.color())
    painter.setPen(Qt.NoPen)
    dot_r = max(1.0, box.width() * 0.045)
    painter.drawEllipse(QPointF(box.center().x(), box.center().y() - r * 0.44), dot_r, dot_r)
    painter.restore()
    painter.setPen(pen)
    painter.drawLine(
        QPointF(box.center().x(), box.center().y() - r * 0.06),
        QPointF(box.center().x(), box.center().y() + r * 0.46),
    )


def _paint_back(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """返回：左向箭头。"""
    painter.setPen(pen)
    painter.drawLine(_r(box, 0.26, 0.50), _r(box, 0.78, 0.50))
    painter.drawLine(_r(box, 0.26, 0.50), _r(box, 0.48, 0.28))
    painter.drawLine(_r(box, 0.26, 0.50), _r(box, 0.48, 0.72))


def _paint_check_all(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """全选：方框 + 勾。"""
    painter.setPen(pen)
    painter.drawRoundedRect(QRectF(_r(box, 0.18, 0.18), _r(box, 0.82, 0.82)), 2.0, 2.0)
    painter.drawLine(_r(box, 0.32, 0.52), _r(box, 0.45, 0.66))
    painter.drawLine(_r(box, 0.45, 0.66), _r(box, 0.70, 0.36))


def _paint_check_none(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """取消全选：空方框。"""
    painter.setPen(pen)
    painter.drawRoundedRect(QRectF(_r(box, 0.18, 0.18), _r(box, 0.82, 0.82)), 2.0, 2.0)


def _paint_invert(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """反选：方框 + 对角斜线。"""
    painter.setPen(pen)
    painter.drawRoundedRect(QRectF(_r(box, 0.18, 0.18), _r(box, 0.82, 0.82)), 2.0, 2.0)
    painter.drawLine(_r(box, 0.32, 0.68), _r(box, 0.68, 0.32))


def _paint_trash(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """删除：垃圾桶轮廓。"""
    painter.setPen(pen)
    painter.drawLine(_r(box, 0.18, 0.30), _r(box, 0.82, 0.30))
    painter.drawLine(_r(box, 0.40, 0.30), _r(box, 0.40, 0.20))
    painter.drawLine(_r(box, 0.40, 0.20), _r(box, 0.60, 0.20))
    painter.drawLine(_r(box, 0.60, 0.20), _r(box, 0.60, 0.30))
    path = QPainterPath()
    path.moveTo(_r(box, 0.26, 0.30))
    path.lineTo(_r(box, 0.31, 0.82))
    path.lineTo(_r(box, 0.69, 0.82))
    path.lineTo(_r(box, 0.74, 0.30))
    painter.drawPath(path)


def _paint_arrow_up(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    painter.setPen(pen)
    painter.drawLine(_r(box, 0.50, 0.22), _r(box, 0.50, 0.78))
    painter.drawLine(_r(box, 0.50, 0.22), _r(box, 0.28, 0.44))
    painter.drawLine(_r(box, 0.50, 0.22), _r(box, 0.72, 0.44))


def _paint_arrow_down(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    painter.setPen(pen)
    painter.drawLine(_r(box, 0.50, 0.22), _r(box, 0.50, 0.78))
    painter.drawLine(_r(box, 0.50, 0.78), _r(box, 0.28, 0.56))
    painter.drawLine(_r(box, 0.50, 0.78), _r(box, 0.72, 0.56))


def _paint_plus(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    painter.setPen(pen)
    painter.drawLine(_r(box, 0.22, 0.50), _r(box, 0.78, 0.50))
    painter.drawLine(_r(box, 0.50, 0.22), _r(box, 0.50, 0.78))


def _paint_save(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """保存：软盘轮廓。"""
    painter.setPen(pen)
    path = QPainterPath()
    path.moveTo(_r(box, 0.22, 0.22))
    path.lineTo(_r(box, 0.68, 0.22))
    path.lineTo(_r(box, 0.78, 0.34))
    path.lineTo(_r(box, 0.78, 0.78))
    path.lineTo(_r(box, 0.22, 0.78))
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawLine(_r(box, 0.36, 0.22), _r(box, 0.36, 0.42))
    painter.drawLine(_r(box, 0.36, 0.42), _r(box, 0.62, 0.42))
    painter.drawLine(_r(box, 0.62, 0.42), _r(box, 0.62, 0.22))
    painter.drawRect(QRectF(_r(box, 0.34, 0.56), _r(box, 0.66, 0.78)))


def _paint_refresh(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """刷新：带缺口圆环 + 箭头。"""
    painter.setPen(pen)
    arc = QRectF(_r(box, 0.22, 0.22), _r(box, 0.78, 0.78))
    painter.drawArc(arc, 60 * 16, 280 * 16)
    painter.drawLine(_r(box, 0.62, 0.16), _r(box, 0.78, 0.30))
    painter.drawLine(_r(box, 0.78, 0.30), _r(box, 0.66, 0.36))


def _paint_folder(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    painter.setPen(pen)
    path = QPainterPath()
    path.moveTo(_r(box, 0.16, 0.74))
    path.lineTo(_r(box, 0.16, 0.28))
    path.lineTo(_r(box, 0.42, 0.28))
    path.lineTo(_r(box, 0.50, 0.38))
    path.lineTo(_r(box, 0.84, 0.38))
    path.lineTo(_r(box, 0.84, 0.74))
    path.closeSubpath()
    painter.drawPath(path)


def _paint_clock(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """时间：圆形表盘 + 指针。"""
    painter.setPen(pen)
    r = box.width() * 0.38
    painter.drawEllipse(box.center(), r, r)
    painter.drawLine(box.center(), QPointF(box.center().x(), box.center().y() - r * 0.55))
    painter.drawLine(box.center(), QPointF(box.center().x() + r * 0.45, box.center().y()))


def _paint_timeline(painter: QPainter, box: QRectF, pen: QPen, color: QColor) -> None:
    """总时长：一条横向时间轴 + 三个刻度点。"""
    painter.setPen(pen)
    painter.drawLine(_r(box, 0.16, 0.50), _r(box, 0.84, 0.50))
    painter.save()
    painter.setBrush(pen.color())
    for fx in (0.16, 0.50, 0.84):
        painter.drawEllipse(_r(box, fx, 0.50), box.width() * 0.075, box.width() * 0.075)
    painter.restore()


_DRAWERS = {
    ICON_RECORD: _paint_record,
    ICON_CURSOR: _paint_cursor,
    ICON_PLAY: _paint_play,
    ICON_STEP: _paint_step,
    ICON_LIBRARY: _paint_library,
    ICON_SETTINGS: _paint_settings,
    ICON_ABOUT: _paint_about,
    ICON_BACK: _paint_back,
    ICON_CHECK_ALL: _paint_check_all,
    ICON_CHECK_NONE: _paint_check_none,
    ICON_INVERT: _paint_invert,
    ICON_TRASH: _paint_trash,
    ICON_ARROW_UP: _paint_arrow_up,
    ICON_ARROW_DOWN: _paint_arrow_down,
    ICON_PLUS: _paint_plus,
    ICON_SAVE: _paint_save,
    ICON_REFRESH: _paint_refresh,
    ICON_FOLDER: _paint_folder,
    ICON_CLOCK: _paint_clock,
    ICON_TIMELINE: _paint_timeline,
    ICON_EDIT: _paint_edit,
}
