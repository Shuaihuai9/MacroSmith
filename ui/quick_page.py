"""快捷输入页（独立一级页面）。

为什么从「设置」里拆出来
------------------------
这个功能有自己的热键（F1–F4）、自己的保存按钮、自己的开关键，占的篇幅比
设置页里其它所有选项加起来还多。混在设置页里有两个实际问题：

1. 用户为了改一个密码，要先在设置页里滚半天找；
2. 「保存设置」和「保存快捷输入」两个按钮都能存同一批字段，用户永远搞不清
   自己点的是哪一个。

拆成一级页面之后：**一页只干一件事**，保存按钮只有一个。

默认关闭（★ 产品决定，别改）
----------------------------
F1–F4 在多数软件里本来另有用途（F1 帮助、F2 重命名）。默认开着就等于抢键，
用户第一次会觉得「键盘坏了」。所以 ``quick_input.enabled`` 出厂为 ``False``，
用户在本页**手动勾选一次**之后才写进配置、一直保持——那一次勾选是明示同意。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from core.config import QUICK_PASSWORD_SLOTS
from ui import styles
from ui.settings import HotkeyEdit
from ui.spin_box import NumberField

#: 输入通道下拉项：``(显示文字, 写入 config 的值)``。
#:
#: 只列用户真用得上的五条；``vk``（只给键码、不带扫描码）是历史通道，留作
#: 配置兼容，不放进界面——它比「物理按键」少填一个扫描码，没有单独使用的理由。
#:
#: 顺序 = ``auto`` 的降级阶梯（2026-11-12 起是 物理按键 → 仅扫描码 → 窗口消息）。
#: 别按「谁更高级」排：用户照着下拉从上往下试，顺序错了就会一直在错的那条上打转。
QUICK_METHOD_ITEMS: tuple[tuple[str, str], ...] = (
    ("自动（推荐）", "auto"),
    ("物理按键（键码+扫描码）", "physical"),
    ("仅扫描码", "simulate"),
    ("字符（Unicode）", "unicode"),
    ("窗口消息（WM_CHAR）", "message"),
)


class QuickInputPage(QWidget):
    """快捷输入：总开关 + F1 剪贴板 + 三个密码槽 + 通道/间隔/补点击。"""

    #: 用户点了「保存快捷输入」：payload 见 :meth:`values`
    save_requested = Signal(dict)
    #: 总开关被勾选/取消：**立即生效并落盘**，不必等保存按钮
    #: （否则「我明明开了，重启又关了」是必然发生的困惑）
    enabled_toggled = Signal(bool)
    #: 某个热键框被改了（动作名）：主窗口据此在第一次改键时另存为新方案
    hotkey_edited = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        #: 密码槽 -> 输入框（``pwd_1..3``）
        self._pwd_edits: dict[str, QLineEdit] = {}
        #: 密码槽 -> 「显示 / 隐藏」按钮
        self._eye_buttons: dict[str, QPushButton] = {}
        #: 热键框（``quick_paste`` + ``quick_pwd_1..3``），四条都属于本页
        self._edits: dict[str, HotkeyEdit] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(styles.CONTENT_MARGIN, 14, styles.CONTENT_MARGIN, 14)
        outer.setSpacing(10)

        title = QLabel("快捷输入")
        title.setObjectName("CardTitle")
        outer.addWidget(title)

        hint = QLabel(
            "按一个热键，把一段文本逐字符「敲」进当前获得焦点的输入框——"
            "不经过剪贴板，密码框、远程桌面、禁止粘贴的输入框都能收到。")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
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

        layout.addWidget(self._build_switch_group())
        layout.addWidget(self._build_paste_group())
        layout.addWidget(self._build_password_group())
        layout.addWidget(self._build_advanced_group())
        layout.addStretch(1)

        # 底部操作条
        buttons = QHBoxLayout()
        self.lbl_state = QLabel("")
        self.lbl_state.setObjectName("Muted")
        buttons.addWidget(self.lbl_state, 1)
        self.btn_save = QPushButton("保存快捷输入")
        self.btn_save.setObjectName("PrimaryButton")
        self.btn_save.setToolTip(
            "保存热键、密码与开关。密码用 Windows DPAPI + 附加熵加密后写入\n"
            "配置文件：只有宏匠自己解得到明文，别的程序读到密文也解不开。")
        self.btn_save.clicked.connect(
            lambda: self.save_requested.emit(self.values()))
        buttons.addWidget(self.btn_save)
        outer.addLayout(buttons)

        self._set_enabled_ui(False)
        self._refresh_state()

    # ==================================================================
    # 构建
    # ==================================================================
    def _build_switch_group(self) -> QGroupBox:
        group = QGroupBox("总开关")
        box = QVBoxLayout(group)
        box.setContentsMargins(10, 8, 10, 8)
        box.setSpacing(8)

        self.chk_enabled = QCheckBox("启用快捷输入")
        self.chk_enabled.setToolTip(
            "按下热键时，把文本逐字符「敲」进当前获得焦点的输入框。\n"
            "走模拟键盘输入（SendInput），不经过剪贴板。\n\n"
            "默认关闭：F1–F4 在多数软件里本来另有用途，\n"
            "开启后这些键在宏匠运行期间会被独占。")
        # 立即生效：勾上就落盘并注册热键，不等「保存」
        self.chk_enabled.toggled.connect(self._on_enabled_toggled)
        box.addWidget(self.chk_enabled)

        self.lbl_first_run = QLabel(
            "首次使用请先勾选上面的开关：勾选即生效、并一直保持。"
            "没勾选时这里设的键完全是普通按键，宏匠不会占用它们。")
        self.lbl_first_run.setObjectName("Hint")
        self.lbl_first_run.setWordWrap(True)
        box.addWidget(self.lbl_first_run)

        guide = QLabel(
            "· 只敲英文字母、数字和半角符号；中文、全角标点与 Emoji 会被丢弃"
            "（密码、验证码、邮箱、URL 都在这个范围内）。\n"
            "· 不受输入法、键盘布局与 CapsLock 影响：敲进去的就是你填的字符。\n"
            "· 录制期间自动失效：热键与敲出的字符都不会进入脚本。")
        guide.setObjectName("Hint")
        guide.setWordWrap(True)
        box.addWidget(guide)
        return group

    def _build_paste_group(self) -> QGroupBox:
        group = QGroupBox("剪贴板")
        box = QVBoxLayout(group)
        box.setContentsMargins(10, 8, 10, 8)
        box.setSpacing(6)

        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.chk_paste = QCheckBox("敲入剪贴板内容")
        self.chk_paste.setToolTip(
            "读取系统剪贴板里的文本，逐字符敲进当前焦点窗口。\n"
            "好处是不改动、不消耗剪贴板，也不需要目标程序支持粘贴。")
        lay.addWidget(self.chk_paste)
        # 勾掉「敲剪贴板」后它就不占键了，总开关文案要跟着变
        self.chk_paste.toggled.connect(lambda _checked: self.refresh_hotkey_summary())

        # ★ 剪贴板键**不再是固定 F1**（用户 2026-11-13 要求）：
        # 它和三个密码槽一样可改，留空 = 这个功能没有热键。
        self.edit_paste = HotkeyEdit("", row)
        self.edit_paste.setToolTip("敲入剪贴板的热键（点击后按键即改；留空则该功能不用热键）")
        self.edit_paste.key_captured.connect(
            lambda _token: self.hotkey_edited.emit("quick_paste"))
        self._edits["quick_paste"] = self.edit_paste

        hint = QLabel("热键")
        hint.setObjectName("Muted")
        lay.addWidget(hint)
        lay.addWidget(self.edit_paste)
        lay.addStretch(1)
        box.addWidget(row)
        return group

    def _build_password_group(self) -> QGroupBox:
        group = QGroupBox("快捷密码")
        box = QVBoxLayout(group)
        box.setContentsMargins(10, 8, 10, 8)
        box.setSpacing(8)

        tip = QLabel(
            "每个槽一段文本 + 一个热键；槽留空则该热键不占用。"
            "密码以 DPAPI + 附加熵加密保存，只有宏匠能解出明文。")
        tip.setObjectName("Hint")
        tip.setWordWrap(True)
        box.addWidget(tip)

        for index, slot in enumerate(QUICK_PASSWORD_SLOTS, start=1):
            box.addWidget(self._build_password_row(index, slot))
        return group

    def _build_password_row(self, index: int, slot: str) -> QWidget:
        """一行密码槽：``密码 N [密文框] [显示] 热键 [F2]``。"""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        label = QLabel(f"密码 {index}")
        label.setFixedWidth(52)
        layout.addWidget(label)

        edit = QLineEdit()
        edit.setEchoMode(QLineEdit.Password)
        edit.setPlaceholderText("留空则不启用该热键")
        edit.setToolTip("按对应热键时逐字符敲入这段文本（只能用 ASCII 字符）")
        self._pwd_edits[slot] = edit
        layout.addWidget(edit, 1)

        eye = QPushButton("显示")
        eye.setObjectName("GhostButton")
        eye.setCheckable(True)
        eye.setFixedWidth(56)
        eye.setToolTip("临时切换明文/密文显示")

        def toggle(checked: bool, field: QLineEdit = edit, button=eye) -> None:
            field.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
            button.setText("隐藏" if checked else "显示")

        eye.toggled.connect(toggle)
        self._eye_buttons[slot] = eye
        layout.addWidget(eye)

        hotkey = HotkeyEdit("", row)
        hotkey.setToolTip(f"敲入密码 {index} 的热键，点击后按键即可改；留空则该槽不用热键")
        hotkey.key_captured.connect(
            lambda _token, a=f"quick_pwd_{index}": self.hotkey_edited.emit(a))
        self._edits[f"quick_pwd_{index}"] = hotkey
        hint = QLabel("热键")
        hint.setObjectName("Muted")
        layout.addWidget(hint)
        layout.addWidget(hotkey)
        return row

    def _build_advanced_group(self) -> QGroupBox:
        group = QGroupBox("敲击细节")
        box = QVBoxLayout(group)
        box.setContentsMargins(10, 8, 10, 8)
        box.setSpacing(8)

        # ---- 敲击间隔 ----
        self.spin_delay = NumberField(1, 50, 8)
        self.spin_delay.setFixedWidth(110)
        self.spin_delay.setToolTip(
            "相邻两个字符之间的间隔。太小（1~3ms）某些输入框会丢字符，\n"
            "太大（>20ms）肉眼能看到明显的「一个字一个字蹦」。")
        delay_row = QWidget()
        delay_layout = QHBoxLayout(delay_row)
        delay_layout.setContentsMargins(0, 0, 0, 0)
        delay_layout.setSpacing(6)
        delay_layout.addWidget(QLabel("字符间隔"))
        delay_layout.addWidget(self.spin_delay)
        unit = QLabel("毫秒")
        unit.setObjectName("Muted")
        delay_layout.addWidget(unit)
        delay_layout.addStretch(1)
        box.addWidget(delay_row)

        # ---- 输入通道 ----
        # 必须露出到界面：``auto`` 只在「事件进不了系统」时才降级，而有些程序
        # （QQ 登录、QQ 游戏大厅）事件进得去却被应用层丢掉，自动模式看不出差别。
        self.combo_method = QComboBox()
        for label, value in QUICK_METHOD_ITEMS:
            self.combo_method.addItem(label, value)
        # 240 而不是 200：最长一项「自动（推荐）」的中文宽度约 211px，
        # 200 会把标签吃掉一个「荐」字（见 tools/_tmp_geom.py 的 minimumSizeHint）
        self.combo_method.setFixedWidth(240)
        self.combo_method.setToolTip(
            "自动（推荐）：先用空键探针确认事件能进系统，再按"
            "「物理按键 → 仅扫描码 → 窗口消息」降级。\n"
            "物理按键：键码与扫描码都填准，形态最接近真键盘"
            "（QQ 游戏大厅的密码框只认这一种）。\n"
            "字符（Unicode）：不受输入法与 CapsLock 影响，但受保护控件会拒收。\n"
            "仅扫描码：只发硬件扫描码，给只按标志位过滤的程序。\n"
            "窗口消息：把文本消息直接投给窗口，不经过输入栈。\n\n"
            "自动模式下敲不进去时，按上面顺序换一条再试。")
        method_row = QWidget()
        method_layout = QHBoxLayout(method_row)
        method_layout.setContentsMargins(0, 0, 0, 0)
        method_layout.setSpacing(6)
        method_layout.addWidget(QLabel("输入通道"))
        method_layout.addWidget(self.combo_method)
        method_layout.addStretch(1)
        box.addWidget(method_row)

        # ---- 敲字前补一次左键 ----
        self.chk_click = QCheckBox("敲字前先点一下左键（页面内输入框需要）")
        self.chk_click.setToolTip(
            "QQ、腾讯微云、WeGame 的登录框是页面内输入框：只有收到真实的鼠标\n"
            "按下，它们才把键盘焦点交给那个框；不点就会把敲进去的字直接丢掉。\n\n"
            "代价是**指针必须放在要输入的框上**，和参考工具的使用方式一样。\n"
            "普通输入框里嫌麻烦，可以取消勾选。")
        box.addWidget(self.chk_click)
        return group

    # ==================================================================
    # 状态联动
    # ==================================================================
    def _on_enabled_toggled(self, checked: bool) -> None:
        self._set_enabled_ui(checked)
        self._refresh_state()
        self.enabled_toggled.emit(bool(checked))

    def _set_enabled_ui(self, enabled: bool) -> None:
        """总开关关闭时把子项灰掉（勾着却按了没反应最让人困惑）。

        **保存按钮不灰**：否则用户取消勾选后就没法把「关闭」这个决定存下去，
        下次启动又变回开启——这比「灰得不够彻底」严重得多。
        """
        for widget in (self.chk_paste, self.spin_delay, self.combo_method,
                       self.chk_click, *self._pwd_edits.values(),
                       *self._edits.values(),
                       *self._eye_buttons.values()):
            if widget is not None:
                widget.setEnabled(bool(enabled))

    def _refresh_state(self) -> None:
        on = self.chk_enabled.isChecked()
        self.lbl_state.setText("当前：已启用" if on else "当前：已关闭（这里设的键未被占用）")
        self.lbl_first_run.setVisible(not on)
        self.refresh_hotkey_summary()

    def refresh_hotkey_summary(self) -> None:
        """把「总开关」文案换成**当前实际绑定的键**。

        以前这里写死「F1–F4 由宏匠接管」：用户把键改成别的之后，这句话就成了
        假话——而且它是页面上最显眼的一句，误导性最强。
        """
        keys = [edit.text() for action, edit in self._edits.items()
                if edit.token() and (action != "quick_paste"
                                     or self.chk_paste.isChecked())]
        if keys:
            self.chk_enabled.setText(
                f"启用快捷输入（{' / '.join(keys)} 由宏匠接管）")
        else:
            self.chk_enabled.setText("启用快捷输入（尚未设置热键）")

    # ==================================================================
    # 读写
    # ==================================================================
    def values(self) -> dict:
        """收集本页当前值（密码为**明文**，由主窗口负责加密）。"""
        return {
            "enabled": self.chk_enabled.isChecked(),
            "paste_enabled": self.chk_paste.isChecked(),
            "delay_ms": self.spin_delay.value(),
            "method": self.combo_method.currentData(),
            "click_first": self.chk_click.isChecked(),
            "hotkeys": {action: edit.token() for action, edit in self._edits.items()},
            "passwords": {slot: edit.text()
                          for slot, edit in self._pwd_edits.items()},
        }

    def load_from_config(self, config) -> None:
        """从配置填充本页（密码自动解密）。"""
        self.chk_enabled.blockSignals(True)
        self.chk_enabled.setChecked(config.quick_input_enabled)
        self.chk_enabled.blockSignals(False)
        self.chk_paste.setChecked(config.quick_paste_enabled)
        self.chk_click.setChecked(config.quick_click_first)
        self.spin_delay.setValue(config.quick_delay_ms)
        index = self.combo_method.findData(config.quick_method)
        if index < 0:
            # 配置里是 ``vk`` 这类不在界面上的历史值：回落到「自动」，
            # 而不是让下拉框停在第一项却把真实值悄悄留着。
            index = self.combo_method.findData("auto")
        self.combo_method.setCurrentIndex(max(0, index))
        for action, edit in self._edits.items():
            edit.set_token(config.hotkey(action))
        for slot, edit in self._pwd_edits.items():
            edit.setText(config.quick_password(slot))
        self._set_enabled_ui(config.quick_input_enabled)
        self._refresh_state()

    def load_hotkeys(self, hotkeys: dict[str, str]) -> None:
        """只更新**传进来的**那几个热键框（保存后两页互相同步用）。

        注意不要改成「没传的清空」：设置页保存时只会传它那 9 个动作，
        清空会把用户刚填的密码槽热键一起抹掉。
        """
        for action, token in hotkeys.items():
            edit = self._edits.get(action)
            if edit is not None:
                edit.set_token(token)
        self.refresh_hotkey_summary()

    def apply_hotkeys(self, hotkeys: dict[str, str]) -> None:
        """整体替换（换方案用）：没出现在 map 里的框一律清空。"""
        for action, edit in self._edits.items():
            edit.set_token(hotkeys.get(action, ""))
        self.refresh_hotkey_summary()

    def active_hotkeys(self) -> dict[str, str]:
        """本页当前真正绑了键的动作。

        剪贴板那条如果被复选框关掉了就不算——它不会被注册，也就谈不上冲突。
        """
        out: dict[str, str] = {}
        for action, edit in self._edits.items():
            if action == "quick_paste" and not self.chk_paste.isChecked():
                continue
            if edit.token():
                out[action] = edit.token()
        return out

    def mark_conflicts(self, conflicts: dict[str, str]) -> None:
        """把「动作名 -> 冲突说明」画到对应输入框上。"""
        for action, edit in self._edits.items():
            edit.set_conflict(conflicts.get(action, ""))
