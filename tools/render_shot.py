"""渲染各界面截图到 docs/，用于目视验收。

    python tools/render_shot.py

生成：
* ``screenshot-record.png``   录制与步骤编辑器（模式一，带勾选）
* ``screenshot-steps.png``    模式二单步 + 当前执行行高亮
* ``screenshot-library.png``  脚本库（列表 + 预览）
* ``screenshot-settings.png`` 设置页
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core import event_parser as ep  # noqa: E402
from core.config import Config  # noqa: E402
from ui import styles  # noqa: E402
from ui.main_window import (PAGE_RECORD, PAGE_SCRIPTS,  # noqa: E402
                            PAGE_SETTINGS, MainWindow)
from utils.win_utils import enable_dpi_awareness  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "docs"


def sample_events() -> list[dict]:
    """一段有代表性的演示脚本。

    刻意控制在 9 步左右，让截图里能完整看到每一种步骤形态
    （移动 / 单击 / 滚轮 / 键盘 / 组合键 / N 连击）。
    """
    return [
        ep.make_move_event(0, 480, 320),
        ep.make_move_event(14, 486, 322),
        ep.make_move_event(28, 502, 330),
        ep.make_move_event(90, 640, 400),
        ep.make_click_event(140, "press", "left", 640, 400),
        ep.make_click_event(180, "release", "left", 640, 400),
        ep.make_scroll_event(300, 0, -3, 760, 468),
        ep.make_key_event(420, "press", "vk:65", 65),
        ep.make_key_event(460, "release", "vk:65", 65),
        ep.make_key_event(520, "press", "vk:16", 16),
        ep.make_key_event(560, "press", "vk:65", 65),
        ep.make_key_event(600, "release", "vk:65", 65),
        ep.make_key_event(640, "release", "vk:16", 16),
        # v0.5：真实录制里 Ctrl 拿不到 vk，用键名形态；会被识别成一步组合键
        ep.make_key_event(860, "press", "ctrl_l", None),
        ep.make_key_event(875, "press", "vk:86", 86),
        ep.make_key_event(920, "release", "vk:86", 86),
        ep.make_key_event(935, "release", "ctrl_l", None),
        # v0.5：连续 4 次快速点击 -> 一步「鼠标连击4次」（连击次数不设上限）
        ep.make_click_event(1080, "press", "left", 640, 400),
        ep.make_click_event(1120, "release", "left", 640, 400),
        ep.make_click_event(1200, "press", "left", 640, 400),
        ep.make_click_event(1240, "release", "left", 640, 400),
        ep.make_click_event(1320, "press", "left", 640, 400),
        ep.make_click_event(1360, "release", "left", 640, 400),
        ep.make_click_event(1440, "press", "left", 640, 400),
        ep.make_click_event(1480, "release", "left", 640, 400),
    ]


class _NullMouse:
    """不做真实注入的鼠标控制器。"""

    _position = (0, 0)

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, value):
        self._position = value

    def press(self, _button):
        pass

    def release(self, _button):
        pass

    def scroll(self, _dx, _dy):
        pass

    def release_all(self):
        pass


class _NullKeyboard:
    def press(self, _key):
        pass

    def release(self, _key):
        pass

    def release_all(self):
        pass


def main() -> int:
    enable_dpi_awareness()
    app = QApplication(sys.argv[:1])
    app.setStyleSheet(styles.build_stylesheet())

    # 截图工具不应弹出任何模态对话框（否则会卡住渲染流程）
    from PySide6.QtWidgets import QMessageBox

    for name in ("question", "warning", "information", "critical"):
        setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Discard))

    tmp = Path(tempfile.mkdtemp())
    config = Config(tmp / "config.json")
    config.set("scripts_dir", str(tmp / "scripts"))
    config.save()

    window = MainWindow(config)
    events = sample_events()
    window.set_script_name("演示脚本-登录流程", mark_dirty=False)
    window.apply_steps(events, mark_unsaved=False)
    window.player.set_controllers(mouse_ctl=_NullMouse(), keyboard_ctl=_NullKeyboard())
    window.show()

    # 另外造两个脚本文件，供脚本库页面展示
    scripts_dir = config.scripts_path()
    ep.save_script(scripts_dir / "演示脚本-登录流程.json", "演示脚本-登录流程", events)
    ep.save_script(scripts_dir / "演示脚本-下单流程.json", "演示脚本-下单流程",
                   [ep.make_move_event(i * 120, 300 + i * 10, 240 + i * 8) for i in range(6)]
                   + [ep.make_click_event(800, "press", "left", 360, 280),
                      ep.make_click_event(840, "release", "left", 360, 280)])
    ep.save_script(scripts_dir / "演示脚本-签到.json", "演示脚本-签到",
                   [ep.make_key_event(0, "press", "vk:16", 16)])

    steps: list[tuple[str, Callable[[], None], Optional[int]]] = []

    def shot_record() -> None:
        window._show_page(PAGE_RECORD)
        window.mode_combo.setCurrentIndex(0)
        window.refresh_script_library()
        window.event_list.set_checked([1, 2, 3])
        window._set_message("模式一：按 F6 开始原速回放，F7 暂停/继续，F8 停止。已勾选 3 个步骤。")

    def shot_steps() -> None:
        window.player.step_reset()
        for _ in range(3):
            window.player.step_next()
        window.mode_combo.setCurrentIndex(1)
        window.event_list.set_checked([])
        window._refresh_step_view()
        window._set_message("模式二：PageDown 下一步，PageUp 上一步，当前执行到步骤 3。")

    def shot_library() -> None:
        """「回放与脚本」页：顺便演示循环设置。"""
        window.event_list.set_checked([])
        window._show_page(PAGE_SCRIPTS)
        window.refresh_script_library()
        paths = ep.list_scripts(config.scripts_path())
        if paths:
            window.page_library.select_path(paths[0])
            window.load_script_file(paths[0])
        # 演示「固定轮数」
        combo = window.page_library.loop_combo
        combo.setCurrentIndex(combo.findData("fixed"))
        window.page_library.spin_rounds.setValue(5)
        window.page_library.set_round("第 2 / 5 轮")
        window._set_message("循环回放：固定 5 轮，正在执行第 2 轮。F7 暂停 / F8 停止依然有效。")

    def shot_settings() -> None:
        window._show_page(PAGE_SETTINGS)

    def grab(name: str) -> Callable[[], None]:
        def inner() -> None:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            pixmap = window.grab()
            path = OUT_DIR / name
            pixmap.save(str(path))
            item = window.event_list.table.item(window.event_list.highlighted, 3) \
                if window.event_list.highlighted >= 0 else None
            print(f"已保存 {path}（{pixmap.width()}x{pixmap.height()}）"
                  f" [page={window.pages.currentIndex()}"
                  f" highlight={window.event_list.highlighted}"
                  f" bg={item.background().color().name() if item else '-'}"
                  f" total={len(window._script_steps)}]")

        return inner

    schedule = [
        (300, shot_record),
        (600, grab("screenshot-record.png")),
        (800, shot_steps),
        (1100, grab("screenshot-steps.png")),
        (1300, shot_library),
        (1700, grab("screenshot-library.png")),
        (1900, shot_settings),
        (2300, grab("screenshot-settings.png")),
        (2500, app.quit),
    ]
    for delay, callback in schedule:
        QTimer.singleShot(delay, callback)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
