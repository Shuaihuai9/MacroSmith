"""现代极简卡片风格：配色常量与全局 QSS。

设计基调
--------
* 浅色主题、留白充足、卡片式布局（白底 + 1px 浅边框 + 柔和圆角）
* 强调色克制：低饱和度蓝 ``#4A6CF7``（用于选中态 / 徽标 / 侧边栏激活）
* 按钮统一为**蓝色单色系**（主操作实心蓝 / 次操作浅蓝填充 / 工具透明幽灵 /
  危险蓝边描边）；全界面只出现蓝与中性灰，不出现红绿橙，也不残留原生灰按钮
* 没有立体阴影堆叠，按钮/输入框都是扁平风格
* 分割线极浅（``#EAEAEA``），不与内容抢视线
"""


# ---------------------------------------------------------------------------
# 配色常量
# ---------------------------------------------------------------------------
ACCENT = "#4A6CF7"           # 强调色（低饱和蓝）
ACCENT_HOVER = "#3E5DE0"     # 悬停
ACCENT_PRESS = "#3550C8"     # 按下
ACCENT_LIGHT = "#EEF1FE"     # 极浅蓝（选中行 / 标签底色）
ACCENT_BORDER = "#C9D4FB"    # 浅蓝描边

ROOT_BG = "#F7F7F8"          # 主背景（浅灰白）
CARD = "#FFFFFF"             # 卡片 / 面板背景
SIDEBAR_BG = "#F3F3F3"       # 侧边栏背景（极浅灰）
SIDEBAR_HOVER = "#E8E8EA"    # 侧边栏按钮悬停
SIDEBAR_ACTIVE = "#E4E8FB"   # 侧边栏按钮激活底色

TEXT = "#1A1A1A"             # 主文字（深灰黑）
TEXT_SECONDARY = "#7A7A7A"   # 次要文字（中灰）
TEXT_MUTED = "#A6A6A6"       # 更弱的提示文字
BORDER = "#EAEAEA"           # 分割线 / 边框
BORDER_STRONG = "#DCDCDC"    # 需要更明显时的边框
HOVER_BG = "#F2F2F3"         # 通用悬停背景
FIELD_BG = "#FFFFFF"         # 输入框背景

DANGER = "#E5484D"           # 危险操作（删除）
DANGER_HOVER = "#D93B40"
DANGER_LIGHT = "#FDEDEE"
SUCCESS = "#2FA36B"          # 状态绿（仅用于状态点，不是主强调色）
RECORD_RED = "#E5484D"       # 录制脉冲点

# ---------------------------------------------------------------------------
# 按钮蓝色单色系（所有界面统一）
# ---------------------------------------------------------------------------
# 全界面按钮只允许出现蓝色与中性灰，不允许红 / 绿 / 橙，也不允许原生灰按钮
# 或白底黑边按钮。四档语义见 build_stylesheet() 的「按钮」段落。
BTN_PRIMARY = "#2563EB"           # 主操作：实心蓝底 + 白字
BTN_PRIMARY_HOVER = "#1D4ED8"
BTN_PRIMARY_PRESS = "#1E40AF"
BTN_SECONDARY = "#EFF6FF"         # 次操作：浅蓝填充 + 蓝字
BTN_SECONDARY_HOVER = "#DBEAFE"
BTN_SECONDARY_PRESS = "#BFDBFE"
BTN_GHOST_TEXT = "#6B7280"        # 工具：透明幽灵 + 灰字
BTN_GHOST_HOVER_BG = "#F3F4F6"
BTN_GHOST_PRESS_BG = "#E5E7EB"
BTN_DISABLED_BG = "#F3F4F6"       # 禁用底
BTN_DISABLED_TEXT = "#9CA3AF"     # 禁用字 / 侧边栏未选中

# 按钮图标颜色（随按钮分类，避免蓝底上出现黑图标）
ICON_ON_PRIMARY = "#FFFFFF"
ICON_ON_SECONDARY = BTN_PRIMARY
ICON_ON_GHOST = BTN_GHOST_TEXT
ICON_ON_DANGER = BTN_PRIMARY

#: 兼容旧调用点的别名（老版本用 GREEN 表示主色）
GREEN = ACCENT
GREEN_HOVER = ACCENT_HOVER
GREEN_PRESS = ACCENT_PRESS
GREEN_LIGHT = ACCENT_LIGHT
GREEN_BORDER = ACCENT_BORDER
SIDEBAR_ICON = BTN_DISABLED_TEXT       # 侧边栏未选中图标 #9CA3AF
SIDEBAR_ICON_ACTIVE = BTN_PRIMARY      # 侧边栏选中图标 #2563EB
SIDEBAR_TEXT = BTN_DISABLED_TEXT
BG = ROOT_BG

# ---------------------------------------------------------------------------
# 尺寸常量
# ---------------------------------------------------------------------------
RADIUS_WINDOW = 12           # 窗口圆角
RADIUS_CARD = 10             # 卡片圆角
RADIUS_BUTTON = 6            # 按钮圆角
RADIUS_FIELD = 6             # 输入控件圆角
SIDEBAR_WIDTH = 64           # 侧边栏宽度
CARD_SPACING = 14            # 卡片之间的间距
CONTENT_MARGIN = 16          # 内容区外边距


# ---------------------------------------------------------------------------
# 全局样式表
# ---------------------------------------------------------------------------
def build_stylesheet() -> str:
    """生成整个应用的 QSS。

    弹窗（QDialog / QMessageBox）的控件与主界面共用同一套规则，
    所以「步骤编辑」「时间设置」「脚本命名」这些对话框会自动继承
    现代极简风格，不会退化成原生灰色按钮。
    """
    return f"""
/* ===================== 基础 ===================== */
QWidget {{
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow {{
    background: {ROOT_BG};
}}
#RootContainer {{
    background: {ROOT_BG};
    border: none;
}}

/* ===================== 侧边栏 ===================== */
#Sidebar {{
    background: {SIDEBAR_BG};
    border-right: 1px solid {BORDER};
}}
#SidebarSection {{
    color: {TEXT_MUTED};
    font-size: 10px;
    background: transparent;
}}

/* ===================== 内容区与卡片 ===================== */
#ContentArea {{
    background: {ROOT_BG};
}}
#Card, #ListCard, #StatusCard, #SettingsCard, #ManagerCard, #PreviewCard {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
}}
#CardTitle {{
    font-size: 15px;
    font-weight: 600;
    color: {TEXT};
    background: transparent;
}}
#SectionTitle {{
    font-size: 13px;
    font-weight: 600;
    color: {TEXT};
    background: transparent;
}}
#Hint {{
    color: {TEXT_SECONDARY};
    font-size: 12px;
    background: transparent;
}}
#Muted {{
    color: {TEXT_MUTED};
    font-size: 11px;
    background: transparent;
}}
#StatusLabel {{
    color: {TEXT};
    font-size: 12px;
    background: transparent;
}}
#StatusHint {{
    color: {TEXT_SECONDARY};
    font-size: 11px;
    background: transparent;
}}
#StepProgress {{
    color: {ACCENT};
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}}
#Badge {{
    background: {ACCENT_LIGHT};
    color: {ACCENT};
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}
#Divider {{
    background: {BORDER};
    max-height: 1px;
    min-height: 1px;
    border: none;
}}

/* ===================== 按钮：蓝色单色系 ===================== */
/* 所有界面（主界面 / 二级界面 / 弹窗 / 提示框）的按钮统一为蓝色单色系，
   只出现蓝与中性灰，不出现红/绿/橙，也不残留原生灰或白底黑边。
   四档语义标记（保留在代码里，测试与维护引用）：
     PrimaryButton   主操作 —— 实心蓝
     SecondaryButton 次操作 —— 浅蓝填充
     GhostButton     工具   —— 透明幽灵（ToolButton 为旧别名）
     DangerButton    危险   —— 蓝边描边，悬停反色 */
QPushButton {{
    background: {BTN_SECONDARY};
    color: {BTN_PRIMARY};
    border: none;
    border-radius: {RADIUS_BUTTON}px;
    padding: 6px 14px;
    min-height: 20px;
}}
QPushButton:hover {{
    background: {BTN_SECONDARY_HOVER};
}}
QPushButton:pressed {{
    background: {BTN_SECONDARY_PRESS};
}}
QPushButton:disabled {{
    background: {BTN_DISABLED_BG};
    color: {BTN_DISABLED_TEXT};
}}

/* —— 主操作：实心蓝（加粗白字） —— */
QPushButton#PrimaryButton {{
    background: {BTN_PRIMARY};
    color: #FFFFFF;
    border: none;
    font-weight: 600;
}}
QPushButton#PrimaryButton:hover {{
    background: {BTN_PRIMARY_HOVER};
}}
QPushButton#PrimaryButton:pressed {{
    background: {BTN_PRIMARY_PRESS};
}}
QPushButton#PrimaryButton:disabled {{
    background: {BTN_SECONDARY_PRESS};
    color: #FFFFFF;
}}

/* —— 次操作：浅蓝填充 —— */
QPushButton#SecondaryButton {{
    background: {BTN_SECONDARY};
    color: {BTN_PRIMARY};
    border: none;
}}
QPushButton#SecondaryButton:hover {{
    background: {BTN_SECONDARY_HOVER};
}}
QPushButton#SecondaryButton:pressed {{
    background: {BTN_SECONDARY_PRESS};
}}
QPushButton#SecondaryButton:disabled {{
    background: {BTN_DISABLED_BG};
    color: {BTN_DISABLED_TEXT};
}}

/* —— 工具：透明幽灵 —— */
QPushButton#GhostButton, QPushButton#ToolButton {{
    background: transparent;
    color: {BTN_GHOST_TEXT};
    border: none;
}}
QPushButton#GhostButton:hover, QPushButton#ToolButton:hover {{
    background: {BTN_GHOST_HOVER_BG};
    color: {BTN_PRIMARY};
}}
QPushButton#GhostButton:pressed, QPushButton#ToolButton:pressed {{
    background: {BTN_GHOST_PRESS_BG};
    color: {BTN_PRIMARY};
}}
QPushButton#GhostButton:disabled, QPushButton#ToolButton:disabled {{
    background: transparent;
    color: {BTN_DISABLED_TEXT};
}}

/* —— 危险：蓝边描边，悬停/按下反色 —— */
QPushButton#DangerButton {{
    background: #FFFFFF;
    color: {BTN_PRIMARY};
    border: 1px solid {BTN_PRIMARY};
}}
QPushButton#DangerButton:hover {{
    background: {BTN_PRIMARY};
    color: #FFFFFF;
}}
QPushButton#DangerButton:pressed {{
    background: {BTN_PRIMARY_PRESS};
    color: #FFFFFF;
}}
QPushButton#DangerButton:disabled {{
    background: #FFFFFF;
    color: {BTN_DISABLED_TEXT};
    border-color: {BTN_SECONDARY_PRESS};
}}

/* ===================== 输入控件 ===================== */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {FIELD_BG};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_FIELD}px;
    padding: 5px 9px;
    min-height: 20px;
    selection-background-color: {ACCENT};
    selection-color: #FFFFFF;
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
    border-color: #C6C6C6;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {ACCENT};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    background: #FAFAFA;
    color: {TEXT_MUTED};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 4px solid {TEXT_SECONDARY};
    width: 0px;
    height: 0px;
    margin-right: 6px;
}}
QComboBox QAbstractItemView {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px;
    selection-background-color: {ACCENT_LIGHT};
    selection-color: {TEXT};
    outline: none;
}}

/* QSpinBox 上下调节按钮：覆盖原生灰色箭头，改成圆角浅灰小按钮 */
QSpinBox, QDoubleSpinBox {{
    /* 给右侧调节按钮留位（全写四值：Qt QSS 对 padding 简写与
       padding-right 单边混用不生效）。带长后缀（如“毫秒”）的窄
       SpinBox 应改用「数字框 + 独立单位标签」，不要依赖后缀，
       否则高 DPI 下后缀会被按钮侧挡（参见 time_dialog 的做法） */
    padding: 5px 26px 5px 9px;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 20px;
    background: #F5F5F6;
    border-left: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    border-top-right-radius: 5px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 20px;
    background: #F5F5F6;
    border-left: 1px solid {BORDER};
    border-bottom-right-radius: 5px;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {HOVER_BG};
}}
QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {{
    background: #E2E2E4;
}}
QSpinBox::up-button:disabled, QDoubleSpinBox::up-button:disabled,
QSpinBox::down-button:disabled, QDoubleSpinBox::down-button:disabled {{
    background: #FAFAFA;
}}
/* 用 CSS 三角形替换原生箭头图标 */
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: none;
    width: 0px;
    height: 0px;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-bottom: 5px solid {TEXT_SECONDARY};
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: none;
    width: 0px;
    height: 0px;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 5px solid {TEXT_SECONDARY};
}}
QSpinBox::up-arrow:hover, QDoubleSpinBox::up-arrow:hover {{
    border-bottom-color: {TEXT};
}}
QSpinBox::down-arrow:hover, QDoubleSpinBox::down-arrow:hover {{
    border-top-color: {TEXT};
}}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled,
QSpinBox::up-arrow:off, QDoubleSpinBox::up-arrow:off {{
    border-bottom-color: #C9C9C9;
}}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled,
QSpinBox::down-arrow:off, QDoubleSpinBox::down-arrow:off {{
    border-top-color: #C9C9C9;
}}

/* ===================== NumberField（自绘数字框） ===================== */
/* QSpinBox 的编辑区几何不吃 QSS padding，高 DPI 下数值会被按钮侧挡；
   需要「数字框 + 上下按钮」的地方统一改用 ui.spin_box.NumberField，
   布局由 QHBoxLayout 明确分配，任何 DPI/字体下都不会互相遮挡 */
QWidget#NumberField {{
    background: {FIELD_BG};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_FIELD}px;
}}
QWidget#NumberField:hover {{
    border-color: #C6C6C6;
}}
QWidget#NumberField[focused="true"] {{
    border-color: {ACCENT};
}}
QWidget#NumberField:disabled {{
    background: #FAFAFA;
}}
QLineEdit#NumberFieldEdit {{
    border: none;
    background: transparent;
    padding: 4px 0px;
    min-height: 20px;
    selection-background-color: {ACCENT};
    selection-color: #FFFFFF;
}}
QLineEdit#NumberFieldEdit:disabled {{
    color: {TEXT_MUTED};
}}
QToolButton#NumberFieldUp, QToolButton#NumberFieldDown {{
    border: none;
    background: transparent;
    color: {TEXT_SECONDARY};
    font-size: 8px;
    padding: 0px;
    min-width: 18px;
    max-width: 18px;
    min-height: 12px;
    max-height: 13px;
    border-radius: 3px;
}}
QToolButton#NumberFieldUp:hover, QToolButton#NumberFieldDown:hover {{
    background: {HOVER_BG};
    color: {TEXT};
}}
QToolButton#NumberFieldUp:pressed, QToolButton#NumberFieldDown:pressed {{
    background: #E2E2E4;
}}
QToolButton#NumberFieldUp:disabled, QToolButton#NumberFieldDown:disabled {{
    color: #C9C9C9;
}}
QCheckBox {{
    background: transparent;
    spacing: 7px;
    color: {TEXT};
}}
QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {BORDER_STRONG};
    border-radius: 4px;
    background: {CARD};
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QCheckBox::indicator:disabled {{
    background: #F2F2F2;
    border-color: {BORDER};
}}

/* ===================== 列表 / 表格 ===================== */
QTableWidget#EventTable, QTableWidget#ScriptTable, QTableWidget#PreviewTable {{
    background: {CARD};
    border: none;
    gridline-color: {BORDER};
    outline: none;
    selection-background-color: {ACCENT_LIGHT};
}}
QTableWidget#EventTable::item, QTableWidget#ScriptTable::item,
QTableWidget#PreviewTable::item {{
    padding: 4px 6px;
    border-bottom: 1px solid {BORDER};
    color: {TEXT};
}}
QTableWidget#EventTable::item:selected, QTableWidget#ScriptTable::item:selected,
QTableWidget#PreviewTable::item:selected {{
    background: {ACCENT_LIGHT};
    color: {TEXT};
}}
QHeaderView::section {{
    background: #FCFCFD;
    color: {TEXT_SECONDARY};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 7px 6px;
    font-size: 12px;
    font-weight: 500;
}}
QHeaderView::section:last {{
    border-right: none;
}}
QTableCornerButton::section {{
    background: #FCFCFD;
    border: none;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 9px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #D6D6D8;
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: #BFBFC2;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
    height: 0px;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 9px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: #D6D6D8;
    border-radius: 4px;
    min-width: 28px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: none;
    width: 0px;
}}
QAbstractScrollArea::corner {{
    background: transparent;
}}

/* ===================== 其它 ===================== */
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
    margin-top: 16px;
    padding: 12px 14px 10px 14px;
    background: {CARD};
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 5px;
    color: {TEXT};
}}
QToolTip {{
    background: #2B2B2B;
    color: #FFFFFF;
    border: none;
    border-radius: 4px;
    padding: 5px 8px;
    font-size: 12px;
}}
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QMenu {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 5px;
}}
QMenu::item {{
    padding: 6px 22px 6px 14px;
    border-radius: 5px;
    color: {TEXT};
}}
QMenu::item:selected {{
    background: {ACCENT_LIGHT};
    color: {TEXT};
}}
QMenu::item:disabled {{
    color: {TEXT_MUTED};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 8px;
}}
QDialog {{
    background: {CARD};
}}
#DialogCard {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
}}

/* ===================== 对话框（步骤编辑 / 时间 / 命名） ===================== */
/* 这些弹窗的控件与主界面共用上面同一套 QSS，这里只补弹窗特有的间距与标题 */
QLabel#DialogHeading {{
    color: {TEXT};
    font-size: 15px;
    font-weight: 600;
}}
QLabel#DialogPreview {{
    color: {ACCENT};
    background: {ACCENT_LIGHT};
    border: 1px solid {ACCENT_BORDER};
    border-radius: 6px;
    padding: 7px 10px;
}}
QLabel#DialogWarn {{
    color: {DANGER};
    background: {DANGER_LIGHT};
    border: 1px solid {DANGER};
    border-radius: 6px;
    padding: 7px 10px;
}}
QDialog QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 9px;
    padding: 10px 10px 8px 10px;
    background: {CARD};
}}
QDialog QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {TEXT_SECONDARY};
}}
/* 弹窗底部按钮：至少要有一定宽度，避免「确定/取消」挤成一小块 */
QDialogButtonBox {{
    button-layout: 0;
}}
QDialogButtonBox QPushButton {{
    min-width: 76px;
    min-height: 26px;
    padding: 6px 18px;
}}
QDialog QLabel#Hint, QDialog QLabel#Muted {{
    color: {TEXT_MUTED};
}}
QMessageBox {{
    background: {CARD};
}}
QMessageBox QLabel {{
    color: {TEXT};
}}
QMessageBox QPushButton {{
    min-width: 76px;
    min-height: 26px;
    padding: 6px 18px;
}}
"""


def apply_to_dialog(dialog) -> None:
    """给对话框显式套一次全局样式表。

    Qt 在 Windows 上不保证 ``QDialog`` 一定继承应用级样式
    （尤其是用 ``QDialog(parent)`` 创建、又自行设置过 palette 的情况），
    这里显式设置一次，保证弹窗里的按钮/输入框与主界面完全一致，
    不会退化成原生的灰色按钮。
    """
    try:
        dialog.setStyleSheet(build_stylesheet())
    except Exception:  # pragma: no cover - 样式失败不应影响功能
        pass
