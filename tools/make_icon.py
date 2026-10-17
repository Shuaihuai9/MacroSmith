"""生成应用图标 `assets/icon.ico`（蓝底白字「宏」）。

    python tools/make_icon.py

图标由 QPainter 现画，不依赖任何外部素材：

* 圆角方块，底色为强调色 ``#4A6CF7``
* 居中一个白色加粗「宏」字
* 输出多尺寸 ICO（16/24/32/48/64/128/256），保证任务栏与 Alt+Tab 都清晰

生成后由 ``utils.win_utils.load_app_icon`` 在启动时加载。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui import styles  # noqa: E402
from utils.win_utils import CJK_FONT_FAMILY, ensure_cjk_font  # noqa: E402

#: ICO 中打包的尺寸（Windows 会按场景挑选：任务栏 32、Alt+Tab 48、设置页 256）
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)

OUTPUT = PROJECT_ROOT / "assets" / "icon.ico"
PNG_PREVIEW = PROJECT_ROOT / "assets" / "icon.png"


def render(size: int, corner_ratio: float = 0.22) -> QImage:
    """画一张 ``size × size`` 的图标位图。"""
    # offscreen 平台下 Qt 枚举不到系统字体，必须先显式注册中文字体，
    # 否则「宏」字会画成豆腐块。
    ensure_cjk_font()

    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.TextAntialiasing, True)

    # 圆角矩形底色
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(styles.ACCENT))
    radius = size * corner_ratio
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    # 居中白色「宏」
    painter.setPen(QColor("#FFFFFF"))
    font = QFont(CJK_FONT_FAMILY)
    font.setPixelSize(max(8, int(size * 0.62)))
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, "宏")
    painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    """把 QImage 编码成 PNG 字节。"""
    array = QByteArray()
    buffer = QBuffer(array)
    buffer.open(QBuffer.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(array)


def write_ico(path: Path, images: list[QImage]) -> None:
    """按 ICO 格式写出多尺寸图标。

    ICO 结构：6 字节文件头 + 每张图 16 字节目录项 + 各图 PNG 数据。
    Vista 之后的 Windows 支持目录项中直接放 PNG 数据。
    """
    count = len(images)
    header = bytearray()
    header += (0).to_bytes(2, "little")      # reserved
    header += (1).to_bytes(2, "little")      # type = icon
    header += count.to_bytes(2, "little")    # image count

    entries = bytearray()
    payload = bytearray()
    offset = 6 + 16 * count
    for image in images:
        data = png_bytes(image)
        width = image.width()
        height = image.height()
        entries += bytes([0 if width >= 256 else width,
                          0 if height >= 256 else height,
                          0,            # 调色板数量（PNG 不需要）
                          0,            # reserved
                          1, 0,         # 色彩平面
                          32, 0])       # 位深
        entries += len(data).to_bytes(4, "little")
        entries += offset.to_bytes(4, "little")
        payload += data
        offset += len(data)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header + entries + payload))


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    images = [render(size) for size in ICON_SIZES]
    write_ico(OUTPUT, images)
    # 顺带存一张 256 的 PNG，方便在文档/README 里预览
    images[-1].save(str(PNG_PREVIEW), "PNG")

    print(f"已生成图标：{OUTPUT}")
    print(f"  尺寸：{', '.join(str(s) for s in ICON_SIZES)}")
    print(f"  大小：{OUTPUT.stat().st_size} 字节")
    print(f"  预览：{PNG_PREVIEW}")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
