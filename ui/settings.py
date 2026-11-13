"""设置页：热键配置、录制选项、脚本目录。

热键捕获控件 :class:`HotkeyEdit` 直接截获按键事件（不依赖全局钩子），
文本框中显示 ``F9``、``PageDown`` 等键名；保存时统一交给
:class:`core.hotkey_manager.HotkeyManager` 重新注册。

★ 快捷输入**不在这里**：它已独立成一级页面（``ui/quick_page.py``）。
两页共用的只有 :class:`HotkeyEdit`（从本模块导入），别再把它搬回去。

★ 出厂不绑任何热键（2026-11-13）：设置页最上面是「快捷键方案」下拉，选中哪个
就用哪个方案的键；一个都没选时下面 9 个框是空的，表示「这个功能没有热键」。
详见 :mod:`core.hotkey_sets`。
"""


from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QToolButton,
                               QVBoxLayout, QWidget)

from core.config import HOTKEY_LABELS
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
#
# ★ 这里必须用 **Qt 的真实枚举名**（去掉 ``Key_`` 前缀再小写），别用 pynput 的
#   写法想当然。2026-11-13 实测：``Qt.Key_PageDown`` 的 name 是 ``Key_PageDown``
#   -> ``pagedown``，而这里原来只写了 pynput 风格的 ``pgdown``，于是用户在热键框
#   里按 PageDown / PageUp / Insert / Delete / 小键盘 Enter **毫无反应**
#   （映射查不到 -> 退回 ``event.text()`` -> 这些键没有文本 -> 返回空串）。
#   标准方案里 ``step_next`` 恰好就是 PageDown，用户想改它都改不了。
#   两种写法都留着：pynput 风格供内部 token 复用，Qt 风格供这里查表。
_QT_KEY_FALLBACK: dict[str, str] = {
    "pgup": "page_up", "pgdown": "page_down",
    "pageup": "page_up", "pagedown": "page_down",
    "escape": "escape",
    "return": "enter", "enter": "enter",
    "del": "delete", "delete": "delete",
    "ins": "insert", "insert": "insert",
    "backspace": "backspace", "space": "space", "tab": "tab",
    "capslock": "caps_lock", "up": "up", "down": "down",
    "left": "left", "right": "right", "home": "home", "end": "end",
}


class HotkeyEdit(QLineEdit):
    """热键捕获输入框：聚焦后按任意键录入该键，右侧 × 清空。

    三件事一起做，因为它们都发生在同一个控件里：

    * **录入**：点进来按一个键就设成那个键（Esc 只是退出录入状态，不改值）；
    * **清空**：右侧常驻一个小小的 ×（**一直显示**，空值时置灰不可点）。
      用户的原始诉求是「想清空这个键，按 ESC 只是退出选择状态，旁边也没清空
      按钮」——**清空必须有一条看得见的路**，不能指望用户猜「再按一次别的键覆盖」。
      清空的语义是「这个功能没有热键」，仍可点按钮操作，所以不会真的让功能失效；
    * **冲突标记**：同一个键被两个功能占用时整框标红 + tooltip 说明和谁撞了。
      标记由上层统一算（要跨两个页面），这里只负责显示。
    """

    key_captured = Signal(str)
    #: 用户点了 × 清空（此时 ``key_captured`` 也会发一次空串）
    cleared = Signal()

    def __init__(self, token: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setAlignment(Qt.AlignCenter)
        self.setPlaceholderText("点击后按键")
        self.setFixedWidth(130)

        self._token = ""
        self._conflict = ""
        self._base_tip = (
            "点击此框后按下想使用的按键（例如 F6、PageDown）。\n"
            "改错了点右侧 × 清空：这个功能将没有热键，仍可点按钮操作。")
        super().setToolTip(self._base_tip)

        self._clear_btn = QToolButton(self)
        self._clear_btn.setObjectName("HotkeyClear")
        self._clear_btn.setText("×")
        self._clear_btn.setToolTip("清空这个热键（该功能将没有热键，仍可点按钮操作）")
        self._clear_btn.setCursor(Qt.PointingHandCursor)
        self._clear_btn.setFixedSize(18, 18)
        self._clear_btn.setFocusPolicy(Qt.NoFocus)
        self._clear_btn.clicked.connect(self.clear_token)

        self.set_token(token)

    # ------------------------------------------------------------------
    def set_token(self, token: str) -> None:
        self._token = normalize_token(token)
        self.setText(display_name(self._token))
        # ★ × 常显（用户 2026-11-13 要求，别再改成按需隐藏）：
        #   空框里也留一个位置，「这里能清空」才一眼可见；空值时置灰不可点。
        self._clear_btn.setEnabled(bool(self._token))

    def token(self) -> str:
        return self._token

    def clear_token(self) -> None:
        """清空这个热键（只改界面，落盘由上层在「保存」时决定）。"""
        if not self._token:
            return
        self.set_token("")
        self.key_captured.emit("")
        self.cleared.emit()

    def set_conflict(self, text: str = "") -> None:
        """标出「这个键和别的功能撞了」；传空串表示无冲突。"""
        self._conflict = text or ""
        conflict = bool(self._conflict)
        if self.property("hotkeyConflict") != conflict:
            self.setProperty("hotkeyConflict", conflict)
            # 动态属性变了必须 unpolish/polish，否则 QSS 不会重新求值
            self.style().unpolish(self)
            self.style().polish(self)
        tip = self._base_tip
        if conflict:
            tip = f"{tip}\n\n⚠ {self._conflict}"
        super().setToolTip(tip)

    @property
    def conflict(self) -> str:
        return self._conflict

    # ------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._clear_btn.move(self.width() - 22,
                             (self.height() - self._clear_btn.height()) // 2)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        """捕获按键：转成统一的键名 token，Esc 取消录入。

        ★ Esc **不是清空**：它是「我不改了，退出」的通用语义，改成清空会让
        误按一下就把热键弄丢。清空有专门的 × 按钮。
        """
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
        # ★ 必须走 set_token()，不能在这儿直接写 ``self._token``。
        #   2026-11-13 栽过：这里手写赋值漏掉了 × 的启用状态，于是
        #   「空框里抓一个键」之后 × 仍是灰的、点不动，非要点一次保存
        #   （走 load_hotkeys -> set_token）才恢复——用户看到的就是
        #   「想清空必须先保存一下」。
        self.set_token(token)
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
    hotkeys_reload = Signal()         # 「撤销热键改动」：从配置重读，不写盘
    hotkey_edited = Signal(str)       # 用户改了某个热键框（动作名）
    hotkey_profile_changed = Signal(str)   # 换方案（方案的 key，空串 = 不启用）
    hotkey_profile_save_as = Signal()      # 「另存为新方案…」
    hotkey_profile_delete = Signal()       # 「删除此方案」
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

        hint = QLabel("热键在「快捷键方案」里选一个即可使用；脚本会保存到下方脚本目录。")
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

        # ----------------------- 快捷键方案 -----------------------
        layout.addWidget(self._build_profile_group())

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
            # 出厂是空串：没选方案时这些框就是空的（= 该功能没有热键）
            edit = HotkeyEdit("", row)
            edit.setToolTip(describe)
            # 改键要通知主窗口：它负责「第一次改键时自动另存为新方案」
            edit.key_captured.connect(lambda _token, a=action: self.hotkey_edited.emit(a))
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

        self.chk_sound = QCheckBox("状态提示音")
        self.chk_sound.setToolTip(
            "开始 / 暂停 / 继续 / 结束 录制与回放时各响一声：\n"
            "录制是低音区（上行＝开始、下行＝暂停、单声＝继续），\n"
            "回放是高音区（另外结束时是三连降），同一个动作换音区，\n"
            "闭着眼也能分清在录还是在放。停止录制不发声。"
        )
        opt_form.addRow("", self.chk_sound)

        # 单步提示音：模式二连按十几次，很多人希望这里安静，所以给独立开关
        self.chk_step_sound = QCheckBox("单步上下步也发提示音")
        self.chk_step_sound.setToolTip(
            "模式二下按「上一步 / 下一步」热键时各响一个极短的提示音\n"
            "（下一步高一点、上一步低一点），确认按键真的生效了。\n"
            "连按几十次时可能会觉得吵，可单独关掉这一项。\n"
            "总开关「状态提示音」关闭时，本项也一并失效。"
        )
        step_sound_row = QWidget()
        step_sound_layout = QHBoxLayout(step_sound_row)
        step_sound_layout.setContentsMargins(22, 0, 0, 0)   # 缩进，表明从属于上一项
        step_sound_layout.setSpacing(0)
        step_sound_layout.addWidget(self.chk_step_sound)
        step_sound_layout.addStretch(1)
        opt_form.addRow("", step_sound_row)
        # 总开关关掉时，子选项跟着灰掉，避免「勾着却没声音」的困惑
        self.chk_sound.toggled.connect(self.chk_step_sound.setEnabled)
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
        self.btn_reset = QPushButton("撤销热键改动")
        self.btn_reset.setObjectName("GhostButton")
        self.btn_reset.setToolTip("把热键框恢复成当前配置里的样子（不写盘）")
        self.btn_reset.clicked.connect(self.reset_hotkeys)
        buttons.addWidget(self.btn_reset)
        self.btn_save = QPushButton("保存设置")
        self.btn_save.setObjectName("PrimaryButton")
        self.btn_save.clicked.connect(self._on_save)
        buttons.addWidget(self.btn_save)
        outer.addLayout(buttons)

    def _build_profile_group(self) -> QGroupBox:
        """「快捷键方案」：整套绑定一起切换。

        为什么要有这一层（用户 2026-11-13 的反馈）：13 个功能原来全都硬绑着
        出厂键，一装就占了半个键盘；而且「功能 = 某个键」被写死在提示里，
        用户改了键，提示还是旧键。现在把「一组按键绑定」当成一个可命名的对象
        来管理，提示语也跟着当前绑定实时生成（见 ``MainWindow._refresh_sidebar_hints``）。

        叫「方案」而不是「套」（2026-11-13 用户定名）：「套」是量词，当名词用
        时「当前套 / 删除此套」都得靠上下文补宾语；「方案」是正经名词，也贴行业
        习惯（JetBrains 的 Keymap 官方中文就叫「键位方案」）。**只改显示文字，
        内部标识一律不动**——配置键仍叫 ``hotkey_profile``，目录仍是
        ``hotkey_sets/``，改了就要搬用户的文件，为一个措辞不值。
        """
        group = QGroupBox("快捷键方案")
        box = QVBoxLayout(group)
        box.setContentsMargins(10, 8, 10, 8)
        box.setSpacing(8)

        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(QLabel("当前方案"))

        self.combo_profile = QComboBox()
        self.combo_profile.setFixedWidth(300)
        self.combo_profile.setToolTip(
            "选中一个方案，下面的热键立刻换成那个方案的按键（并写入配置）。\n"
            "「不使用方案」= 一个键都不占用，全部功能只能点按钮。")
        self.combo_profile.currentIndexChanged.connect(self._on_profile_changed)
        lay.addWidget(self.combo_profile)

        self.btn_profile_save = QPushButton("另存为新方案…")
        self.btn_profile_save.setObjectName("GhostButton")
        self.btn_profile_save.setToolTip(
            "把当前这些按键存成一个新方案（可自己起名），\n"
            "文件写在配置目录旁边的 hotkey_sets 文件夹里。")
        self.btn_profile_save.clicked.connect(self.hotkey_profile_save_as.emit)
        lay.addWidget(self.btn_profile_save)

        self.btn_profile_delete = QPushButton("删除此方案")
        self.btn_profile_delete.setObjectName("GhostButton")
        self.btn_profile_delete.setToolTip(
            "删除当前的用户方案（内置的「标准方案」不能删）")
        self.btn_profile_delete.setEnabled(False)
        self.btn_profile_delete.clicked.connect(self.hotkey_profile_delete.emit)
        lay.addWidget(self.btn_profile_delete)
        lay.addStretch(1)
        box.addWidget(row)

        # ★ 强调用 <b>，不要用 markdown 的 **：QLabel 不认 markdown，
        #   以前这里写的是 ``**不占用任何按键**``，界面上会把星号原样显示出来。
        hint = QLabel(
            "· 出厂<b>不占用任何按键</b>：选一个现成的方案，或者直接去改下面某一个键。\n"
            "· 第一次改键时，当前方案会<b>自动另存为一个新方案</b>（名字叫「我的热键」），"
            "之后的改动都写进那个方案里，内置方案不会被改坏。\n"
            "· 留空的框表示「这个功能没有热键」，可以照常点按钮操作。")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        box.addWidget(hint)
        return group

    def _on_profile_changed(self, index: int) -> None:
        """下拉换方案：把 key 抛给主窗口（主窗口才知道怎么落盘与重挂）。"""
        if index < 0:
            return
        key = str(self.combo_profile.itemData(index) or "")
        self.btn_profile_delete.setEnabled(key.startswith("user:"))
        self.hotkey_profile_changed.emit(key)

    def load_profiles(self, profiles, current_key: str) -> None:
        """填充「快捷键方案」下拉（不触发切换信号）。"""
        self.combo_profile.blockSignals(True)
        self.combo_profile.clear()
        self.combo_profile.addItem("不使用方案（不占用任何按键）", "")
        for item in profiles:
            suffix = "" if item.builtin else "  ·自定义"
            self.combo_profile.addItem(f"{item.name}{suffix}", item.key)
        index = self.combo_profile.findData(current_key)
        self.combo_profile.setCurrentIndex(max(0, index))
        self.combo_profile.blockSignals(False)
        self.btn_profile_delete.setEnabled(str(current_key).startswith("user:"))

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
        self.chk_sound.setChecked(bool(config.get("sound.enabled", True)))
        self.chk_step_sound.setChecked(bool(config.get("sound.step_enabled", True)))
        self.chk_step_sound.setEnabled(self.chk_sound.isChecked())

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

    def apply_hotkeys(self, hotkeys: dict[str, str]) -> None:
        """整体替换（换方案用）：没出现在 map 里的框一律清空。"""
        for action, edit in self._edits.items():
            edit.set_token(hotkeys.get(action, ""))

    def reset_hotkeys(self) -> None:
        """「撤销热键改动」：请主窗口按配置重填，不写盘。

        以前这里是「恢复默认热键」（填 :data:`DEFAULT_HOTKEYS`）。有了快捷键方案
        之后「默认」不再是一个固定集合——可能是用户自己的方案，所以改成
        「回到已保存的样子」，这才是用户按这个按钮时真正想要的结果。
        """
        self.hotkeys_reload.emit()

    def hotkey_values(self) -> dict[str, str]:
        return {action: edit.token() for action, edit in self._edits.items()}

    def active_hotkeys(self) -> dict[str, str]:
        """本页当前（可能还没保存）真正绑了键的动作。"""
        return {action: edit.token() for action, edit in self._edits.items()
                if edit.token()}

    def mark_conflicts(self, conflicts: dict[str, str]) -> None:
        """把「动作名 -> 冲突说明」画到对应输入框上（不在表里=无冲突）。"""
        for action, edit in self._edits.items():
            edit.set_conflict(conflicts.get(action, ""))

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
            # 带点的键是完整配置路径（提示音不在 player 段下），
            # 主窗口的 _save_options 会识别后再决定是否加 player. 前缀
            "sound.enabled": self.chk_sound.isChecked(),
            "sound.step_enabled": self.chk_step_sound.isChecked(),
        })
