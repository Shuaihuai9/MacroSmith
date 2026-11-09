"""pytest 配置：把项目根目录加入 sys.path，使 ``core`` / ``ui`` / ``tools`` 可导入。

运行方式（项目根目录下）::

    python -m pytest tests -v
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

collect_ignore: list[str] = []


def pytest_configure(config) -> None:
    """全局兜底：让 QMessageBox 的模态方法不阻塞测试。

    被测代码里可能通过 ``QTimer.singleShot`` 弹提示框，而 ``exec()`` 会
    一直阻塞事件循环，导致测试挂死。这里在 Qt 可用时统一打桩。
    """
    try:
        from PySide6.QtWidgets import QMessageBox
    except ImportError:  # pragma: no cover - 未安装 PySide6 时跳过
        return

    QMessageBox.exec = lambda self, *a, **k: 0            # type: ignore[assignment]
    QMessageBox.exec_ = lambda self, *a, **k: 0           # type: ignore[assignment]
    QMessageBox.open = lambda self, *a, **k: None         # type: ignore[assignment]
    for name in ("information", "warning", "critical", "question", "about"):
        if hasattr(QMessageBox, name):
            setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Yes))
