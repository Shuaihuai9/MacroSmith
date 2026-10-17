"""Windows 平台相关工具函数。

本模块集中处理与操作系统相关的差异（路径、DPI、单实例、激活窗口等），
其余模块不直接依赖 win32 API，方便后续替换或移植。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

APP_NAME = "MacroSmith"
APP_NAME_CN = "宏匠"
#: 面向用户的窗口/任务栏标题（按需求只显示中文名）
WINDOW_TITLE = APP_NAME_CN
#: 兼容旧引用：带英文名的完整标题（日志等内部场合可用）
FULL_TITLE = f"{APP_NAME} {APP_NAME_CN}"

IS_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------

def project_root() -> Path:
    """返回项目根目录（main.py 所在目录）。"""
    return Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的 exe 中。"""
    return bool(getattr(sys, "frozen", False))


def app_data_dir() -> Path:
    """正式发布时的数据目录：%APPDATA%\\MacroSmith。"""
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_NAME


def scripts_dir(config_dir: Path, configured: str | None = None) -> Path:
    """根据配置解析脚本存放目录。

    - 开发阶段：项目根目录 ``data/scripts/``
    - 正式发布：``%APPDATA%\\MacroSmith\\scripts\\``
    - 配置中若给出相对路径，则相对 ``config_dir`` 解析。
    """
    if configured:
        p = Path(configured)
        if not p.is_absolute():
            p = (config_dir / p).resolve()
        return p
    return config_dir / "scripts"


def ensure_dir(path: Path) -> Path:
    """确保目录存在并返回该目录。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# DPI 感知
# ---------------------------------------------------------------------------

def enable_dpi_awareness() -> None:
    """开启进程 DPI 感知，避免高分屏下界面模糊与坐标错位。"""
    if not IS_WINDOWS:
        return
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def primary_screen_size() -> tuple[int, int]:
    """返回主屏尺寸（物理像素）。"""
    if IS_WINDOWS:
        try:
            user32 = ctypes.windll.user32
            return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
        except Exception:
            pass
    return 1920, 1080


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """返回虚拟桌面矩形 ``(left, top, width, height)``。

    多显示器环境下该矩形是所有屏幕的并集，坐标原点可能为负数
    （副屏在主屏左侧/上方时）。回放注入坐标前用它做合法性校验与钳制。
    """
    if IS_WINDOWS:
        try:
            user32 = ctypes.windll.user32
            # SM_XVIRTUALSCREEN=76, SM_YVIRTUALSCREEN=77
            # SM_CXVIRTUALSCREEN=78, SM_CYVIRTUALSCREEN=79
            left = int(user32.GetSystemMetrics(76))
            top = int(user32.GetSystemMetrics(77))
            width = int(user32.GetSystemMetrics(78))
            height = int(user32.GetSystemMetrics(79))
            if width > 0 and height > 0:
                return left, top, width, height
        except Exception:
            pass
    width, height = primary_screen_size()
    return 0, 0, width, height


def clamp_to_virtual_screen(x: int, y: int) -> tuple[int, int, bool]:
    """把坐标钳制到虚拟桌面范围内，返回 ``(x, y, changed)``。

    ``changed`` 为 True 表示原坐标越界并被修正。常见原因是录制脚本时的
    显示器配置与当前不同（例如录制时接了副屏），此时若不钳制，
    pynput 会把光标压到屏幕角落，用户看起来就像「完全没有动作」。
    """
    left, top, width, height = virtual_screen_rect()
    max_x = left + width - 1
    max_y = top + height - 1
    try:
        src_x, src_y = int(x), int(y)
    except (TypeError, ValueError):
        return left, top, True
    clamped_x = min(max(src_x, left), max_x)
    clamped_y = min(max(src_y, top), max_y)
    return clamped_x, clamped_y, (clamped_x != src_x or clamped_y != src_y)


# ---------------------------------------------------------------------------
# 单实例控制
# ---------------------------------------------------------------------------

_MUTEX_HANDLE = None


def acquire_single_instance(name: str = "Global\\MacroSmith_SingleInstance") -> bool:
    """创建命名互斥体，保证同一时间只有一个 MacroSmith 实例在运行。"""
    global _MUTEX_HANDLE
    if not IS_WINDOWS:
        return True
    try:
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, name)
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            if handle:
                kernel32.CloseHandle(handle)
            return False
        _MUTEX_HANDLE = handle
        return True
    except Exception:
        return True


def release_single_instance() -> None:
    """释放互斥体（退出时调用，非必需但更干净）。"""
    global _MUTEX_HANDLE
    if _MUTEX_HANDLE and IS_WINDOWS:
        try:
            ctypes.windll.kernel32.CloseHandle(_MUTEX_HANDLE)
        except Exception:
            pass
        _MUTEX_HANDLE = None


# ---------------------------------------------------------------------------
# 窗口激活检测（用于自检/调试）
# ---------------------------------------------------------------------------

def foreground_window_title() -> str:
    """返回当前前台窗口标题，取不到时返回空字符串。"""
    if not IS_WINDOWS:
        return ""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ""


def process_dpi() -> int:
    """返回当前进程 DPI（96 为 100% 缩放）。"""
    if not IS_WINDOWS:
        return 96
    try:
        return int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:
        return 96


# ---------------------------------------------------------------------------
# 应用图标
# ---------------------------------------------------------------------------

#: 图标候选路径（打包后优先用 _MEIPASS 里的资源）
ICON_RELATIVE_PATHS = (
    Path("assets") / "icon.ico",
    Path("assets") / "macrosmith.ico",
)


def icon_search_paths() -> list[Path]:
    """返回按优先级排列的图标路径候选。"""
    candidates: list[Path] = []
    # PyInstaller 解包目录
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        for rel in ICON_RELATIVE_PATHS:
            candidates.append(Path(meipass) / rel)
    for rel in ICON_RELATIVE_PATHS:
        candidates.append(project_root() / rel)
    for rel in ICON_RELATIVE_PATHS:
        candidates.append(app_data_dir() / rel)
    return candidates


def load_app_icon():
    """加载应用图标（``QIcon``）；找不到时返回 None。

    图标会同时用于窗口左上角、任务栏按钮与 Alt+Tab 列表，
    因此这里显式设置多尺寸，避免任务栏显示模糊。
    """
    try:
        from PySide6.QtGui import QIcon
    except ImportError:  # pragma: no cover - 未安装 PySide6
        return None

    for path in icon_search_paths():
        try:
            if path.exists():
                icon = QIcon(str(path))
                if not icon.isNull():
                    return icon
        except OSError:
            continue

    # 兜底：直接画一个蓝底白字「宏」，保证任何环境下都有图标
    try:
        return build_fallback_icon()
    except Exception:  # pragma: no cover - 依赖 Qt 绘制
        return None


def build_fallback_icon():
    """用 QPainter 现画一个 256x256 的蓝底白字「宏」图标。"""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

    ensure_cjk_font()
    size = 256
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#4A6CF7"))
    radius = size * 0.22
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
    painter.setPen(QColor("#FFFFFF"))
    font = QFont(CJK_FONT_FAMILY)
    font.setPixelSize(int(size * 0.62))
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, "宏")
    painter.end()
    return QIcon(pixmap)


#: 首选中文字体族名（与 QSS 中声明的一致）
CJK_FONT_FAMILY = "Microsoft YaHei UI"

#: 系统中文字体候选文件（按优先级）
_CJK_FONT_FILES = (
    r"C:\Windows\Fonts\msyh.ttc",     # 微软雅黑
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf",   # 黑体
    r"C:\Windows\Fonts\simsun.ttc",   # 宋体
    r"C:\Windows\Fonts\Deng.ttf",     # 等线
)

_cjk_font_ready = False


def ensure_cjk_font() -> bool:
    """确保 Qt 能拿到中文字体，返回是否可用。

    正常情况下 Qt 会自己枚举系统字体；但在 ``QT_QPA_PLATFORM=offscreen``
    或某些精简/打包环境里 ``QFontDatabase.families()`` 会是空的，
    中文就会画成「豆腐块」（□□）。这里显式把 Windows 自带的中文字体文件
    注册进 Qt，保证界面与图标里的中文都能正常显示。
    """
    global _cjk_font_ready
    if _cjk_font_ready:
        return True
    try:
        from PySide6.QtGui import QFontDatabase
    except ImportError:  # pragma: no cover
        return False
    try:
        if QFontDatabase.families():
            _cjk_font_ready = True
            return True
        for path in _CJK_FONT_FILES:
            if not Path(path).exists():
                continue
            font_id = QFontDatabase.addApplicationFont(path)
            if font_id >= 0 and QFontDatabase.applicationFontFamilies(font_id):
                _cjk_font_ready = True
                return True
    except Exception:  # pragma: no cover - 字体注册失败不应影响启动
        pass
    return _cjk_font_ready


# ---------------------------------------------------------------------------
# 图标候选（向后兼容旧调用点）
# ---------------------------------------------------------------------------
def icon_path(*parts: str) -> Path:
    """拼接项目内的资源路径。"""
    return project_root().joinpath(*parts)


def set_process_dpi_awareness_if_needed() -> None:
    """兼容旧调用点：需要时开启 DPI 感知。"""
    enable_dpi_awareness()
