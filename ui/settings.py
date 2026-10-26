"""设置页：热键配置、录制选项、脚本目录。

热键捕获控件 :class:`HotkeyEdit` 直接截获按键事件（不依赖全局钩子），
文本框中显示 ``F9``、``PageDown`` 等键名；保存时统一交给
:class:`core.hotkey_manager.HotkeyManager` 重新注册。
"""


from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

from core.config import DEFAULT_HOTKEYS, HOTKEY_LABELS
from core.hotkey_manager import display_name, normalize_token, probe_hotkey
from ui import styles
from ui.spin_box import NumberField


def _unit_row(field: QWidget, unit: str) -> QWidget:
    """数字框 + 框外独立单位标签（后缀放框内高 DPI 下会被按钮侧挡）。"""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    lay.addWidget(field)
    lbl = QLabel(unit)
    lbl.setObjectName("Muted")
    lay.addWidget(lbl)
    lay.addStretch(1)
    return row

# Qt 按键枚举名 -> 我们的键名 token 的后备映射（pynput 名与 Qt 名不完全一致）
_QT_KEY_FALLBACK: dict[str, str] = {
    "pgup": "page_up", "pgdown": "page_down", "escape": "escape",
    "return": "enter", "del": "delete", "ins": "insert",
    "backspace": "backspace", "space": "space", "tab": "tab",
    "capslock": "caps_lock", "up": "up", "down": "down",
    "left": "left", "right": "right", "home": "home", "end": "end",
}


class HotkeyEdit(QLineEdit):
    """热键捕获输入框：聚焦后按任意键即录入该键。"""

    key_captured = Signal(str)

    def __init__(self, token: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setAlignment(Qt.AlignCenter)
        self.setPlaceholderText("点击后按键")
        self.setFixedWidth(130)
        self.setToolTip("点击此框后按下想使用的按键（例如 F6、PageDown）")
        self.set_token(token)

    def set_token(self, token: str) -> None:
        self._token = normalize_token(token)
        self.setText(display_name(self._token))

    def token(self) -> str:
        return self._token

    # ------------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        """捕获按键：转成统一的键名 token，Esc 取消录入。"""
        key = event.key()
        if key in (Qt.Key_Escape, Qt.Key_unknown):
            self.clearFocus()
            event.accept()
            return
        # 纯修饰键单击不视为热键
        if key in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta,
                   Qt.Key_AltGr, Qt.Key_CapsLock):
            event.accept()
            return

        token = self._token_from_event(event)
        if not token:
            return
        if not probe_hotkey(token):
            self.setText(f"不支持：{token}")
            event.accept()
            return
        self._token = normalize_token(token)
        self.setText(display_name(self._token))
        self.key_captured.emit(self._token)
        event.accept()

    @staticmethod
    def _token_from_event(event: QKeyEvent) -> str:
        """把 Qt 按键事件转换成热键 token。

        优先使用 ``Qt.Key_F9`` 这类命名键；普通字符键用字符本身。
        """
        key = event.key()
        # 功能键区间
        if Qt.Key_F1 <= key <= Qt.Key_F35:
            return f"f{key - Qt.Key_F1 + 1}"

        name = getattr(event, "text", lambda: "")() or ""
        # 先用 Qt 枚举名换算
        try:
            qt_name = Qt.Key(key).name.decode() if isinstance(Qt.Key(key).name, bytes) else str(Qt.Key(key).name)
        except Exception:
            qt_name = ""
        qt_name = qt_name.lower()
        if qt_name.startswith("key_"):
            short = qt_name[4:]
            if short in _QT_KEY_FALLBACK:
                return _QT_KEY_FALLBACK[short]
            if len(short) == 1 and short.isalnum():
                return short

        if name and len(name) == 1 and not name.isspace():
            return name.lower()
        if key == Qt.Key_Space:
            return "space"
        return ""


class SettingsPage(QWidget):
    """设置页。"""

    hotkeys_saved = Signal(dict)      # {动作名: token}
    options_saved = Signal(dict)      # {player.*: 值}
    exclusive_toggled = Signal(bool)  # 热键独占模式开关
    merge_changed = Signal(bool, bool)  # (自动合并组合键, 自动合并双击/三击)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._edits: dict[str, HotkeyEdit] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)

        title = QLabel("设置")
        title.setObjectName("CardTitle")
        outer.addWidget(title)

        hint = QLabel("修改完全局热键后需点击“保存设置”生效；脚本会保存到下方脚本目录。")
        hint.setObjectName("Hint")
        outer.addWidget(hint)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(220)
        inner = QWidget()
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(12)

        # ------------------------- 热键 -------------------------
        group_hotkey = QGroupBox("全局热键")
        form = QFormLayout(group_hotkey)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)

        for action, (label, describe) in HOTKEY_LABELS.items():
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            edit = HotkeyEdit(DEFAULT_HOTKEYS[action], row)
            edit.setToolTip(describe)
            self._edits[action] = edit
            row_layout.addWidget(edit)
            tip = QLabel(describe)
            tip.setObjectName("Hint")
            row_layout.addWidget(tip, 1)
            form.addRow(label, row)
        layout.addWidget(group_hotkey)

        # ------------------------- 热键独占 -------------------------
        group_exclusive = QGroupBox("热键独占")
        ex_layout = QVBoxLayout(group_exclusive)
        ex_layout.setContentsMargins(10, 8, 10, 8)
        ex_layout.setSpacing(6)

        self.chk_exclusive = QCheckBox("热键独占模式（推荐开启）")
        self.chk_exclusive.setToolTip(
            "开启后，运行期间按下已注册的热键会被本程序独占：\n"
            "只执行 宏匠 的功能，不再把按键传给浏览器等前台程序。\n"
            "原理：安装 Win32 低级键盘钩子（WH_KEYBOARD_LL）拦截按键。"
        )
        self.chk_exclusive.toggled.connect(self._on_exclusive_toggled)
        ex_layout.addWidget(self.chk_exclusive)

        ex_hint = QLabel(
            "· 关闭后热键仍然可用，但 F6/F9 等按键也会同时触发前台软件原本的功能。\n"
            "· 若前台是「以管理员身份运行」的程序，Windows 会忽略本程序的钩子，"
            "独占会失效；此时请以管理员身份运行 宏匠，或更换热键。\n"
            "· Win 键与 Esc 永远不会被独占，以免把系统锁死。"
        )
        ex_hint.setObjectName("Hint")
        ex_hint.setWordWrap(True)
        ex_layout.addWidget(ex_hint)
        layout.addWidget(group_exclusive)

        # ------------------------- 录制 / 回放 -------------------------
        group_opt = QGroupBox("录制与回放")
        opt_form = QFormLayout(group_opt)
        opt_form.setHorizontalSpacing(16)
        opt_form.setVerticalSpacing(8)

        # 第九阶段起：「记录鼠标移动」「仅录制点击坐标」开关已删除，
        # 录制始终记录鼠标移动 + 点击；这里只保留抖动过滤参数。

        # 自绘数字框 + 框外单位标签：QSpinBox 的编辑区几何不吃 QSS padding，
        # 高 DPI 下后缀/数值会被上下按钮侧挡（见 ui/spin_box.py）。
        self.spin_interval = NumberField(0, 200, 50)
        self.spin_interval.setFixedWidth(110)
        self.spin_interval.setToolTip("抖动过滤：相邻移动的时间间隔小于该值且位移很小时丢弃")
        opt_form.addRow("移动最小间隔", _unit_row(self.spin_interval, "毫秒"))

        self.spin_distance = NumberField(0, 50, 2)
        self.spin_distance.setFixedWidth(110)
        self.spin_distance.setToolTip("抖动过滤：相邻移动的位移小于该像素数且间隔很小时丢弃")
        opt_form.addRow("移动最小位移", _unit_row(self.spin_distance, "像素"))

        self.spin_speed = QDoubleSpinBox()
        self.spin_speed.setRange(0.1, 10.0)
        self.spin_speed.setSingleStep(0.1)
        self.spin_speed.setDecimals(1)
        self.spin_speed.setFixedWidth(110)
        self.spin_speed.setToolTip("模式一时间轴倍率：1.0 为原速，2.0 为两倍速")
        opt_form.addRow("回放速度", _unit_row(self.spin_speed, "倍"))

        self.edit_scripts = QLineEdit()
        self.edit_scripts.setReadOnly(True)
        self.edit_scripts.setToolTip("脚本 JSON 的存放目录（由配置文件决定）")
        opt_form.addRow("脚本目录", self.edit_scripts)
        layout.addWidget(group_opt)

        # ------------------------- 智能合并 -------------------------
        group_merge = QGroupBox("智能合并（步骤编辑器显示方式）")
        merge_layout = QVBoxLayout(group_merge)
        merge_layout.setContentsMargins(10, 8, 10, 8)
        merge_layout.setSpacing(6)

        self.chk_merge_combo = QCheckBox("自动合并组合键")
        self.chk_merge_combo.setToolTip(
            "把「修饰键按下 + 目标键按下/释放 + 修饰键释放」合并成一步：\n"
            "显示为「组合键 Ctrl+V」。\n"
            "模式二按一次热键就完整触发一次组合键，稳定不失效。"
        )
        self.chk_merge_combo.toggled.connect(self._emit_merge)
        merge_layout.addWidget(self.chk_merge_combo)

        self.chk_merge_multi = QCheckBox("自动合并双击 / 三击")
        self.chk_merge_multi.setToolTip(
            "把同坐标（5 像素内）、同按键、间隔小于 500ms 的连续点击\n"
            "合并成一步：显示为「鼠标双击 (x, y)」。\n"
            "模式二按一次热键就完整触发一次双击，系统能正确识别。"
        )
        self.chk_merge_multi.toggled.connect(self._emit_merge)
        merge_layout.addWidget(self.chk_merge_multi)

        merge_hint = QLabel(
            "· 两个开关默认开启；关闭后保持原有拆分记录方式"
            "（组合键显示为「Ctrl 按下 / V 按下并释放 / Ctrl 释放」三步）。\n"
            "· 改动会立刻对当前列表生效；保存脚本后重新载入仍按当时的开关合并。\n"
            "· 手工拆分过的步骤不会被再次合并，编辑结果会被尊重。"
        )
        merge_hint.setObjectName("Hint")
        merge_hint.setWordWrap(True)
        merge_layout.addWidget(merge_hint)
        layout.addWidget(group_merge)
        layout.addStretch(1)

        # ------------------------- 按钮 -------------------------
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.btn_reset = QPushButton("恢复默认热键")
        self.btn_reset.setObjectName("GhostButton")
        self.btn_reset.clicked.connect(self.reset_hotkeys)
        buttons.addWidget(self.btn_reset)
        self.btn_save = QPushButton("保存设置")
        self.btn_save.setObjectName("PrimaryButton")
        self.btn_save.clicked.connect(self._on_save)
        buttons.addWidget(self.btn_save)
        outer.addLayout(buttons)

    def minimumSizeHint(self):  # noqa: N802 - Qt 命名
        """压低最小高度，避免设置页把窗口最小尺寸顶高。"""
        from PySide6.QtCore import QSize

        return QSize(420, 300)

    # ==================================================================
    # 载入 / 保存
    # ==================================================================
    def load_from_config(self, config) -> None:
        """从配置对象读取当前值填充界面。"""
        for action, edit in self._edits.items():
            edit.set_token(config.hotkey(action))
        self.chk_exclusive.blockSignals(True)
        self.chk_exclusive.setChecked(bool(config.get("hotkey_exclusive", True)))
        self.chk_exclusive.blockSignals(False)
        # 智能合并开关：改动立即生效，因此接上信号
        for chk, key in ((self.chk_merge_combo, "merge.combo"),
                         (self.chk_merge_multi, "merge.multi_click")):
            chk.blockSignals(True)
            chk.setChecked(bool(config.get(key, True)))
            chk.blockSignals(False)
        player = config.get("player", {}) or {}
        self.spin_interval.setValue(int(player.get("move_min_interval_ms", 50)))
        self.spin_distance.setValue(int(player.get("move_min_distance_px", 2)))
        self.spin_speed.setValue(float(player.get("speed", 1.0)))
        self.edit_scripts.setText(str(config.scripts_path()))

    def _on_exclusive_toggled(self, checked: bool) -> None:
        """独占开关立即生效（不必等“保存设置”），因为它影响热键注册方式。"""
        self.exclusive_toggled.emit(bool(checked))

    def _emit_merge(self, _checked: bool) -> None:
        """智能合并开关立即生效：主窗口会按新开关重算步骤列表。"""
        self.merge_changed.emit(self.chk_merge_combo.isChecked(),
                                self.chk_merge_multi.isChecked())

    def load_hotkeys(self, hotkeys: dict[str, str]) -> None:
        """只更新热键输入框（保存前同步用，不影响其它选项）。"""
        for action, token in hotkeys.items():
            edit = self._edits.get(action)
            if edit is not None:
                edit.set_token(token)

    def reset_hotkeys(self) -> None:
        """把热键输入框恢复成默认值（需再点保存才生效）。"""
        for action, edit in self._edits.items():
            edit.set_token(DEFAULT_HOTKEYS[action])

    def hotkey_values(self) -> dict[str, str]:
        return {action: edit.token() for action, edit in self._edits.items()}

    def duplicate_hotkeys(self) -> dict[str, list[str]]:
        """返回重复绑定的键 -> 动作列表，用于保存前提示。"""
        seen: dict[str, list[str]] = {}
        for action, token in self.hotkey_values().items():
            seen.setdefault(token, []).append(action)
        return {token: actions for token, actions in seen.items() if len(actions) > 1}

    def _on_save(self) -> None:
        self.hotkeys_saved.emit(self.hotkey_values())
        self.options_saved.emit({
            "move_min_interval_ms": self.spin_interval.value(),
            "move_min_distance_px": self.spin_distance.value(),
            "speed": self.spin_speed.value(),
        })
