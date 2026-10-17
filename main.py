"""MacroSmith 宏匠 —— 程序入口。

开发阶段直接用批处理启动::

    start.bat          # 等价于 python main.py

打包后（PyInstaller）同样以本文件为入口。
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path

# 保证以任何工作目录启动时都能 import 到 core/ui/utils
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging() -> Path:
    """配置日志：控制台 + %APPDATA%\\MacroSmith\\logs\\macrosmith.log。"""
    from utils.win_utils import app_data_dir, ensure_dir

    log_dir = ensure_dir(app_data_dir() / "logs")
    log_file = log_dir / "macrosmith.log"

    handlers: list[logging.Handler] = []
    try:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError:
        pass
    # 打包成 exe（windowed）后没有控制台，stdout 可能为 None，需要判断
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=handlers or [logging.NullHandler()],
    )
    return log_file


def install_excepthook() -> None:
    """把未捕获异常写日志并弹窗提示，避免程序静默退出。"""

    def hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logging.getLogger("macrosmith").error("未捕获异常:\n%s", text)
        try:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(
                None, "MacroSmith 发生错误",
                f"程序遇到未处理的错误：\n\n{exc_value}\n\n"
                "详细信息已写入日志：%APPDATA%\\MacroSmith\\logs\\macrosmith.log",
            )
        except Exception:
            pass

    sys.excepthook = hook


def main() -> int:
    """程序主入口。"""
    # DPI 感知必须在 Qt 初始化之前设置
    from utils.win_utils import acquire_single_instance, enable_dpi_awareness

    enable_dpi_awareness()
    log_file = setup_logging()
    log = logging.getLogger("macrosmith")
    log.info("MacroSmith 启动，Python %s，日志：%s", sys.version.split()[0], log_file)

    if not acquire_single_instance():
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication(sys.argv)
        QMessageBox.information(None, "宏匠", "宏匠 已经在运行了。")
        return 0

    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:  # pragma: no cover
        print("缺少依赖 PySide6，请先执行：pip install -r requirements.txt")
        print(f"具体错误：{exc}")
        return 2

    # 高分屏缩放策略（Qt6 默认已启用缩放，这里只统一取整策略）
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("MacroSmith")
    # 面向用户的名称只显示中文（窗口标题 / 任务栏 / Alt+Tab）
    app.setApplicationDisplayName("宏匠")
    app.setOrganizationName("MacroSmith")
    app.setQuitOnLastWindowClosed(True)
    install_excepthook()

    from core.config import Config
    from ui import styles
    from ui.main_window import MainWindow
    from utils.win_utils import (app_data_dir, ensure_cjk_font, is_frozen,
                                 load_app_icon, project_root)

    # 某些环境（offscreen / 精简系统 / 打包后）Qt 枚举不到系统字体，
    # 中文会显示成方框；这里显式注册一次中文字体。
    ensure_cjk_font()

    app.setStyleSheet(styles.build_stylesheet())

    # 应用图标：窗口左上角 / 任务栏按钮 / Alt+Tab 都会使用它
    icon = load_app_icon()
    if icon is not None and not icon.isNull():
        app.setWindowIcon(icon)
        log.info("已加载应用图标")
    else:
        log.warning("未找到 assets/icon.ico，使用系统默认图标")

    config = Config()
    # 把当前生效的配置路径写进日志，便于排查“开发版/发布版配置不一致”的问题
    log.info("配置文件：%s", config.path)
    log.info("脚本目录：%s", config.scripts_path())

    window = MainWindow(config)

    # 支持 `python main.py --script xxx.json` 直接载入脚本（开发调试用）
    args = list(sys.argv[1:])
    if "--check" in args:
        return run_startup_check(app, window, log)

    idx = 0
    while idx < len(args):
        if args[idx] == "--script" and idx + 1 < len(args):
            window.load_script_file(Path(args[idx + 1]))
            idx += 2
            continue
        idx += 1

    window.show()
    window.raise_()
    window.activateWindow()
    log.info("窗口标题：%s", window.windowTitle())
    return app.exec()


def run_startup_check(app, window, log) -> int:
    """``--check`` 模式：只验证启动链路，不进入界面事件循环。

    用于批处理/CI 里确认“程序能被正常拉起、窗口属性与热键就绪”。
    """
    window.show()
    for _ in range(10):
        app.processEvents()

    hotkeys = window.hotkeys.display_map()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget
    from ui.main_window import PAGE_SCRIPTS
    from ui.styles import SIDEBAR_WIDTH, build_stylesheet
    from utils.win_utils import load_app_icon

    icon = load_app_icon()
    combo = window.script_name_edit
    combo_scripts = sum(1 for i in range(combo.count()) if combo.itemData(i))
    page = window.page_library
    checks = [
        ("窗口标题", window.windowTitle().startswith("宏匠"), window.windowTitle()),
        ("原生窗口边框", (not (window.windowFlags() & Qt.FramelessWindowHint))
         and window.mask().isEmpty(),
         "系统标题栏+系统边框，边缘缩放/双击最大化/Win+方向键由 Windows 提供"),
        ("默认窗口尺寸", (window.DEFAULT_WIDTH, window.DEFAULT_HEIGHT) == (1000, 700),
         f"{window.DEFAULT_WIDTH}x{window.DEFAULT_HEIGHT}"),
        ("窗口可缩放", window.maximumWidth() > 10000 and window.minimumWidth() >= 800,
         f"最小 {window.minimumWidth()}x{window.minimumHeight()}，无固定尺寸限制"),
        ("默认页为「回放与脚本」", window.pages.currentIndex() == PAGE_SCRIPTS,
         "回放与脚本 / 录制与步骤编辑 / 设置 / 关于"),
        ("侧边栏宽度", window.sidebar.width() == SIDEBAR_WIDTH,
         f"{window.sidebar.width()}px（期望 {SIDEBAR_WIDTH}px）"),
        ("侧边栏无「回放」入口", not hasattr(window.sidebar, "btn_play"),
         "已移除"),
        ("应用图标", icon is not None and not icon.isNull(),
         "assets/icon.ico" if icon is not None else "未加载"),
        ("回放控制就绪", all(hasattr(page, n) for n in
                        ("btn_play", "btn_stop_play", "loop_combo", "spin_rounds")),
         "开始/停止回放 + 循环模式 + 轮数"),
        ("模式下拉框在回放页", (window.mode_combo is page.mode_combo
         and window.mode_combo not in window.page_record.findChildren(QWidget)),
         "模式一/模式二切换紧跟「开始回放」按钮"),
        ("循环回放设置", window.config.loop_mode in ("none", "fixed", "infinite"),
         f"模式={window.config.loop_mode} 轮数={window.config.loop_count}"),
        ("「加载该脚本」按钮", page.btn_load.text() == "加载该脚本",
         page.btn_load.text()),
        ("全局状态栏", (len(window.findChildren(QWidget, "StatusCard")) == 1
         and window.status_bar.parentWidget().parentWidget() is window.content_host),
         "只创建一次，挂在窗口底部，两个页面共享"),
        ("步骤编辑器就绪", window.event_list.table.columnCount() == 5,
         f"{window.event_list.table.columnCount()} 列（复选框/序号/类型/内容/时间）"),
        ("步骤参数编辑入口", all(hasattr(window.event_list, n) for n in
                             ("btn_edit_step", "btn_duration")),
         "编辑这一步 / 重设总时长（改时间已并入「编辑这一步」弹窗）"),
        ("脚本库就绪", page.table.columnCount() == 4,
         f"{page.table.columnCount()} 列"),
        ("历史脚本下拉框", combo.isEditable() and window.btn_new_script.isEnabled(),
         f"可编辑，已列出 {combo_scripts} 个脚本，旁边有「新建脚本」按钮"),
        ("弹窗样式统一", "QSpinBox::up-button" in build_stylesheet()
         and "QSpinBox::down-arrow" in build_stylesheet(),
         "步骤编辑/时间/命名弹窗共用主界面 QSS，SpinBox 调节按钮已覆盖原生样式"),
        ("自动合并开关", (window.page_settings.chk_merge_combo.isChecked()
                     and window.page_settings.chk_merge_multi.isChecked()),
         f"组合键={window.config.merge_combo} / 双击三击={window.config.merge_multi_click}"),
        ("配置文件", window.config.path.exists(), str(window.config.path)),
        ("脚本目录", window.config.scripts_path().exists(), str(window.config.scripts_path())),
        ("全局热键监听", window.hotkeys.is_running, "已启动" if window.hotkeys.is_running else "未启动"),
        ("热键独占模式", window.hotkeys.exclusive,
         "已开启（命中热键不再传给前台程序）" if window.hotkeys.exclusive
         else "已关闭（按键会同时触发前台程序）"),
        ("热键绑定完整", len(hotkeys) == 7, "、".join(f"{k}={v}" for k, v in hotkeys.items())),
        ("停止录制键不入脚本", "f9" in window.recorder.ignored_keys,
         "、" .join(sorted(window.recorder.ignored_keys))),
        ("始终记录移动与点击", not hasattr(window, "chk_clicks_only"),
         "「仅录制点击坐标」「记录鼠标移动」开关已删除（第九阶段）"),
        ("录制器待命", not window.recorder.is_recording, "未录制"),
        ("回放器待命", not window.player.is_replaying, "未回放"),
    ]
    failed = 0
    print("=" * 60)
    print("MacroSmith 宏匠 —— 启动自检（--check）")
    print("=" * 60)
    for name, ok, detail in checks:
        print(f"  [{'通过' if ok else '失败'}] {name} —— {detail}")
        if not ok:
            # 打包版（windowed）没有控制台，print 看不到；失败项必须进日志
            log.warning("自检失败：%s —— %s", name, detail)
        failed += 0 if ok else 1
    print("=" * 60)
    print(f"结论：{len(checks) - failed}/{len(checks)} 项通过")
    log.info("启动自检完成，失败 %d 项", failed)
    window.close()
    app.processEvents()
    return 1 if failed else 0


if __name__ == "__main__":
    # 冻结后 multiprocessing 需要（PyInstaller 打包阶段使用）
    if getattr(sys, "frozen", False):
        import multiprocessing

        multiprocessing.freeze_support()
    sys.exit(main())
