"""命令行测试工具的公共部分。

本模块在导入时会先把**项目根目录**插入 ``sys.path``，
因此既支持 ``python tools/xxx.py``（脚本目录被加入 sys.path），
也支持 ``python -m tools.xxx`` 两种运行方式。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _use_utf8_console() -> None:
    """Windows 控制台默认是 GBK，直接打印中文会乱码，这里统一切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


_use_utf8_console()


def setup_console_logging(level: int = logging.INFO) -> None:
    """命令行工具统一日志格式（只输出到控制台）。"""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def fmt_ms(ms: int) -> str:
    """毫秒格式化成 ``秒.毫秒``。"""
    return f"{ms / 1000.0:7.3f}s"


def default_scripts_dir() -> Path:
    """按配置解析脚本目录。"""
    from core.config import Config
    from utils.win_utils import ensure_dir

    return ensure_dir(Config().scripts_path())
