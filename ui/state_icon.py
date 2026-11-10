"""任务栏图标的状态徽标。

问题：窗口最小化或被别的程序盖住时，用户看不到「是不是还在录 / 还在回放」。
做法：按当前状态在**应用图标右下角**画一个小徽标，然后 ``setWindowIcon``——
Windows 任务栏按钮、Alt+Tab 列表都会跟着变，窗口标题一个字都不用动。

四种状态（:data:`BADGES`）：

============  ==========  ==========================================
状态          颜色        图形
============  ==========  ==========================================
录制中        录制红      实心圆点（像录音键）
录制已暂停    暂停琥珀    两条竖杠
回放中        状态绿      三角播放键
回放已暂停    暗绿        两条竖杠
============  ==========  ==========================================

空闲（IDLE）不画徽标，直接用原图标。

配色为什么这么定：

* **红只代表"正在录"**——红色是安全信号，绝不能出现在录制已停的时候，
  否则用户以为还在录（或反过来）会出真问题。所以录制一暂停就换成琥珀。
* **回放暂停保留绿色但压暗一档**：绿色一直代表"回放这个会话"，
  压暗 = 没在动。这样两种"暂停"能分开——只靠形状区分的话，
  最小化后根本看不出停的是录制还是回放，而这两件事的后果完全不同。
* **不用强调蓝**：应用图标本身就是蓝底，蓝徽标贴上去几乎看不见（实测过）。

为什么是把徽标画进图标、而不是用 ``ITaskbarList3::SetOverlayIcon``：
后者需要在 ctypes 里自己拼 32bpp DIB 转 HICON，代码量与失败面都大得多，
而任务栏按钮本来就按窗口图标绘制，改图标已经能一眼看出状态。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygonF

from ui import styles

#: 徽标配色（暂停琥珀只在这里出现，不进全局 QSS）
PAUSE_AMBER = "#F0A22E"
#: 回放暂停用的暗绿：比"正在回放"的状态绿压暗一档，一眼看出是停着的
REPLAY_PAUSED_GREEN = "#1F7A57"

#: 状态 -> (徽标底色, 图形)。IDLE 不在此表里，表示"不画徽标"。
#: 红只给"正在录"，回放用状态绿（蓝徽标贴蓝底图标上看不见）。
BADGES: dict[str, tuple[str, str]] = {
    "recording": (styles.RECORD_RED, "dot"),
    "record_paused": (PAUSE_AMBER, "pause"),
    "replaying": (styles.SUCCESS, "play"),
    "paused": (REPLAY_PAUSED_GREEN, "pause"),
}

#: 徽标直径占图标边长的比例。任务栏按钮上的图标本身就小，徽标不够大就不"显眼"；
#: 但太大又会把底图（蓝底白字「宏」）压没。这两档是实测调出来的观感平衡点，
#: 想再放大/收小改这一行即可（`<= SMALL_SIZE_LIMIT` 的尺寸用 SMALL）。
BADGE_RATIO_SMALL = 0.80
BADGE_RATIO_LARGE = 0.72
SMALL_SIZE_LIMIT = 24

#: 需要为这些尺寸各画一张，任务栏小的用 16/24，大的用 32/48，Alt+Tab 用更大的。
SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)

#: (base.cacheKey(), state) -> QIcon，避免每次状态切换都重画 7 张位图
_CACHE: dict[tuple[int, str], QIcon] = {}


def state_icon(base: Optional[QIcon], state: str) -> Optional[QIcon]:
    """返回带状态徽标的图标；``state`` 不在 :data:`BADGES` 里时返回 ``base``。

    ``base`` 为 None 时返回 None（调用方保持原样即可）。
    """
    if base is None or base.isNull():
        return base
    badge = BADGES.get(state)
    if badge is None:
        return base

    key = (base.cacheKey(), state)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    color, glyph = badge
    icon = QIcon()
    for size in SIZES:
        pixmap = base.pixmap(size, size)
        if pixmap.isNull():
            continue
        canvas = QPixmap(pixmap)
        painter = QPainter(canvas)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            _paint_badge(painter, float(size), QColor(color), glyph)
        finally:
            painter.end()
        icon.addPixmap(canvas)

    if icon.isNull():                      # 基图标一张位图都拿不到
        return base
    _CACHE[key] = icon
    return icon


def clear_cache() -> None:
    """清空徽标缓存（测试或换图标时用）。"""
    _CACHE.clear()


def _paint_badge(painter: QPainter, size: float, color: QColor,
                 glyph: str) -> None:
    """在右下角画圆形徽标 + 白色图形。"""
    # 直径比例见模块顶部 BADGE_RATIO_SMALL / BADGE_RATIO_LARGE
    ratio = BADGE_RATIO_SMALL if size <= SMALL_SIZE_LIMIT else BADGE_RATIO_LARGE
    diameter = size * ratio
    margin = max(0.5, size * 0.01)
    box = QRectF(size - diameter - margin, size - diameter - margin,
                 diameter, diameter)

    # 白色描边让徽标从底图里"跳"出来（深色/浅色底图都适用）
    ring = max(0.8, size * 0.035)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#FFFFFF"))
    painter.drawEllipse(box)
    inner = box.adjusted(ring, ring, -ring, -ring)
    painter.setBrush(color)
    painter.drawEllipse(inner)

    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#FFFFFF"))
    center = inner.center()
    d = inner.width()

    if glyph == "dot":
        r = d * 0.30
        painter.drawEllipse(center, r, r)
    elif glyph == "pause":
        bar_w = d * 0.18
        bar_h = d * 0.52
        gap = d * 0.15
        for sign in (-1, 1):
            x = center.x() + sign * (gap / 2 + bar_w / 2)
            painter.drawRoundedRect(
                QRectF(x - bar_w / 2, center.y() - bar_h / 2, bar_w, bar_h),
                bar_w * 0.45, bar_w * 0.45)
    elif glyph == "play":
        # 等腰三角：略向右偏，视觉上才居中。
        # 注意 PySide6 的 drawPolygon 不接受三个 QPointF 变参（会抛
        # TypeError: too many arguments），必须先装进 QPolygonF。
        w = d * 0.48
        h = d * 0.54
        left = center.x() - w * 0.42
        painter.drawPolygon(QPolygonF([
            QPointF(left, center.y() - h / 2),
            QPointF(left, center.y() + h / 2),
            QPointF(left + w, center.y()),
        ]))
