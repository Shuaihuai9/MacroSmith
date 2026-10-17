"""窗口外壳：原生窗口（系统标题栏 + 系统边框）+ 内容区容器。

第七阶段起**放弃无边框方案**。

为什么放弃
----------
旧实现用 ``Qt.FramelessWindowHint`` 去掉系统标题栏，再自绘圆角、
自定义标题栏、自定义边缘热区缩放。结果 ``QWindow.startSystemResize()``
在 Windows 上依赖原生非客户区，无边框窗口直接被平台拒绝（实测返回
``False``），用户鼠标变成双箭头却拖不动；手写的替代缩放也始终
达不到系统手感。用户明确要求：不追求无边框/圆角，只要窗口能正常
拖拽缩放、内部 UI 保持现代卡片风格即可。

回归原生窗口后，以下体验全部由系统免费提供：

* 边缘/四角拖拽缩放 + 双箭头光标
* 双击标题栏最大化 / 还原
* Win + 方向键贴边分屏
* 任务栏预览、Alt+Tab 缩略图

本模块因此只剩一件事：把内容区容器装配好（保留 ``container`` /
``body`` / ``content_host`` 这套既有接口，主窗口无需大改）。
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget


class WindowShell(QWidget):
    """原生窗口基类：系统标题栏 + 内容区容器。

    窗口几何（尺寸/位置）的持久化由 ``MainWindow`` 负责
    （``_restore_window_geometry`` / ``_save_window_geometry``），
    原生窗口的 ``geometry()`` / ``resize()`` / ``move()`` 语义一致，
    原有持久化逻辑无需改动。
    """

    #: 默认窗口尺寸（1000x700）。用户拖拽调整后的尺寸会写入
    #: config.json 的 window.width / window.height，下次启动优先使用配置值。
    DEFAULT_WIDTH = 1000
    DEFAULT_HEIGHT = 700
    #: 允许缩放到的最小尺寸（低于它卡片与表格会被挤压错位）
    MIN_WIDTH = 820
    MIN_HEIGHT = 560

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 标准窗口标志（Qt.Widget 默认值）：系统标题栏 + 系统边框，
        # 边缘缩放 / 双击最大化 / Win+方向键全部交给 Windows。
        self.setWindowTitle(f"{title} {subtitle}".strip())
        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)

        self.container = QWidget(self)
        self.container.setObjectName("RootContainer")
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)
        self._outer.addWidget(self.container)

        self.body = QVBoxLayout(self.container)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)

        # 内容区（左深色侧边栏 + 右浅色主区）由子类填充
        self.content_host = QWidget(self.container)
        self.content_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # 显式给出内容区的期望尺寸，避免布局把默认尺寸撑成更高的高度
        # （原无边框方案里要扣掉自定义标题栏 44px，这里沿用同样的内容区下限）
        self.content_host.setMinimumSize(self.MIN_WIDTH, max(1, self.MIN_HEIGHT - 44))
        self.body.addWidget(self.content_host, 1)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        """默认窗口尺寸 1000x700。"""
        return QSize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
