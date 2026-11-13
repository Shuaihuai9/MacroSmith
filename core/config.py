"""配置持久化管理。

- 开发阶段：项目根目录 ``config.json``
- 正式发布：``%APPDATA%\\MacroSmith\\config.json``

配置结构（节选）::

    {
      "hotkey_profile": "builtin:standard",   # 当前启用的「热键套」，空串 = 一个键都不绑
      "hotkeys": {"record_start": "f12", "record_stop": "f9", ...},
      "window":  {"x": null, "y": null, "width": 1000, "height": 700},
      "player":  {"default_mode": "timed", "speed": 1.0, ...},
      "scripts_dir": "data/scripts",
      "last_script": null
    }

★ ``hotkeys`` 是**当前生效**的绑定，出厂是空字典（一个键都不占）。
用户从「热键套」下拉里选一套、或者自己改某个键，才会往里写东西；
套文件本身放在 ``<config 同目录>/hotkey_sets/`` 下，见 :mod:`core.hotkey_sets`。
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Optional

from utils.win_utils import app_data_dir, is_frozen, project_root
from core.hotkey_sets import (HotkeySet, STANDARD_HOTKEYS, find_set,
                              list_sets, sanitize_bindings, save_set,
                              suggest_name)

#: 默认窗口尺寸（第五阶段从 900x600 提到 1000x700）
DEFAULT_WINDOW_WIDTH = 1000
DEFAULT_WINDOW_HEIGHT = 700
#: 历史上出现过的“默认尺寸”。用户配置里若正好等于这些值，
#: 说明他从来没手动调过窗口大小，此时跟随新版默认值升级；
#: 其它的值一律视为用户手动调整过的结果，原样保留。
_LEGACY_DEFAULT_SIZES: tuple[tuple[int, int], ...] = (
    (900, 600), (640, 480), (820, 560), (820, 540),
)

log = logging.getLogger(__name__)

#: 「标准套」的按键定义。**不是出厂绑定**——出厂一个键都不绑，
#: 用户从「热键套」下拉里选中它，这些键才会被注册。
#: 保留这个名字是因为大量代码与测试拿它当「那套熟悉的热键」的参照。
DEFAULT_HOTKEYS: dict[str, str] = dict(STANDARD_HOTKEYS)

#: 「快捷输入」专用的热键动作。它们有自己的设置区域（含密码框），
#: 因此不进 :data:`HOTKEY_LABELS`——否则会混进「全局热键」列表里多出四行。
QUICK_HOTKEY_ACTIONS: tuple[str, ...] = (
    "quick_paste", "quick_pwd_1", "quick_pwd_2", "quick_pwd_3",
)

#: 快捷输入三个密码槽位（与 quick_pwd_1..3 一一对应）
QUICK_PASSWORD_SLOTS: tuple[str, ...] = ("pwd_1", "pwd_2", "pwd_3")

#: 热键的界面显示名与说明
HOTKEY_LABELS: dict[str, tuple[str, str]] = {
    "record_start": ("开始录制", "空闲时立即开始录制，等同点「开始录制」按钮"),
    "record_stop": ("停止录制", "录制过程中结束录制并保存事件序列"),
    "record_pause": ("暂停 / 继续录制", "录制过程中暂停采集，再次按下继续（暂停时长不计入脚本）"),
    "play_toggle": ("开始回放", "模式一：按录制时的时间间隔原速回放"),
    "pause_toggle": ("暂停 / 继续", "模式一：暂停后保持执行位置，再次按下继续"),
    "play_stop": ("停止回放", "模式一：终止回放并把执行指针归零"),
    "step_next": ("下一步（模式二）", "模式二：执行下一个操作单元"),
    "step_prev": ("上一步（模式二）", "模式二：回退到上一个操作单元"),
    "step_reset": ("重置指针（模式二）", "模式二：把步骤指针复位到开头"),
}

#: 快捷输入热键的界面显示名（注册失败 / 冲突的提示要用）。
#: ★ 名字里**不许再写死按键**：热键已可自定义，写死就会出现「F1 F7 敲入剪贴板」。
QUICK_HOTKEY_LABELS: dict[str, str] = {
    "quick_paste": "敲入剪贴板",
    "quick_pwd_1": "快捷密码 1",
    "quick_pwd_2": "快捷密码 2",
    "quick_pwd_3": "快捷密码 3",
}

DEFAULT_CONFIG: dict[str, Any] = {
    # ★ 出厂不绑任何热键：F1–F12 在别的软件里各有用途，装完就抢键会被当成
    #   「键盘坏了」。用户选一个「热键套」或自己改某个键之后才会有绑定。
    "hotkeys": {},
    #: 当前生效的热键套标识（``builtin:standard`` / ``user:我的热键`` / 空串）
    "hotkey_profile": "",
    "window": {"x": None, "y": None, "width": DEFAULT_WINDOW_WIDTH,
               "height": DEFAULT_WINDOW_HEIGHT},
    #: 热键独占：运行期间吞掉已注册热键，不再把按键传给前台程序
    "hotkey_exclusive": True,
    "merge": {
        "combo": True,        # 自动把 修饰键+普通键 合并成「组合键 Ctrl+V」
        "multi_click": True,  # 自动把连续快速点击合并成「鼠标双击 / 三击」
    },
    "loop": {
        "mode": "none",       # none = 不循环，fixed = 固定轮数，infinite = 无限循环
        "count": 1,           # 固定轮数时的轮数
    },
    "player": {
        "default_mode": "timed",     # timed = 模式一原速回放，step = 模式二单步
        "speed": 1.0,                # 回放速度倍率（1.0 为原速）
        "move_min_interval_ms": 50,  # 鼠标移动抖动过滤：最小时间间隔
        "move_min_distance_px": 2,   # 鼠标移动抖动过滤：最小位移
        # 第九阶段起：record_mouse_move / record_clicks_only 已删除，
        # 录制始终记录鼠标移动 + 点击；旧配置文件里的这两个键会被忽略。
    },
    #: 状态提示音：开始/暂停/继续/结束 录制与回放各响一声（见 core/sound.py）
    #: ``step_enabled`` 是模式二「上一步 / 下一步」的独立开关（连按时可能嫌吵）
    "sound": {"enabled": True, "step_enabled": True},
    #: 快捷输入（文本模拟打字）：按热键把文本逐字符敲进当前焦点窗口
    #: ``enabled`` 默认**关闭**：F1/F2/F3 在多数软件里本来有用途（帮助/重命名/
    #: 查找），独占钩子一旦吞掉它们，用户会以为键盘坏了。要用手动打开。
    #: ``pwd_1..3`` 存的是 ``core.quick_input.protect()`` 的产物（DPAPI 密文），
    #: 没有 ``enc1:`` 前缀就按明文处理（兼容手工编辑与降级场景）。
    "quick_input": {
        "enabled": False,
        "paste_enabled": True,
        "delay_ms": 8,
        #: 注入通道：``auto``（默认，按 物理按键 → 仅扫描码 → 窗口消息 降级）/
        #: ``unicode`` / ``message`` / ``vk``。平时 auto 就够了。
        "mode": "auto",
        #: 敲字前先在**当前指针位置**补一次左键单击。页面内输入框
        #: （QQ / 腾讯微云 / WeGame 的登录框都是 CEF/Electron 自绘）只有收到
        #: 真实鼠标按下才把键盘焦点交给页面里的 input，不发点击它们就把按键
        #: 丢掉。代价是**指针必须放在要输入的框上**（参考工具也是这个规矩）。
        "click_first": True,
        "pwd_1": "",
        "pwd_2": "",
        "pwd_3": "",
    },
    "scripts_dir": "data/scripts",
    "last_script": None,
}

# 开发阶段旧版扁平键名 -> 新结构，用于向后兼容
_LEGACY_KEYS = {
    "hotkey_stop_record": ("hotkeys", "record_stop"),
    "hotkey_play": ("hotkeys", "play_toggle"),
    "hotkey_pause": ("hotkeys", "pause_toggle"),
    "hotkey_stop_play": ("hotkeys", "play_stop"),
    "hotkey_step_next": ("hotkeys", "step_next"),
    "hotkey_step_prev": ("hotkeys", "step_prev"),
    "hotkey_step_reset": ("hotkeys", "step_reset"),
}


class Config:
    """配置对象：负责加载、合并默认值、点号路径读写与保存。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path: Path = Path(path) if path else self.default_path()
        self.data: dict[str, Any] = {}
        self.load()

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    @staticmethod
    def default_path() -> Path:
        """决定配置文件位置：打包后放 %APPDATA%，开发阶段放项目根目录。"""
        if is_frozen():
            return app_data_dir() / "config.json"
        return project_root() / "config.json"

    @property
    def base_dir(self) -> Path:
        """脚本相对路径的解析基准目录。"""
        return self.path.parent

    # ------------------------------------------------------------------
    # 加载 / 保存
    # ------------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        """读取配置文件；文件缺失或损坏时回退到默认配置并备份坏文件。"""
        raw: dict[str, Any] = {}
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                if isinstance(loaded, dict):
                    raw = loaded
                else:
                    log.warning("配置文件顶层不是对象，已忽略：%s", self.path)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("配置文件读取失败（%s），使用默认配置：%s", self.path, exc)
                try:
                    shutil.copy2(self.path, self.path.with_suffix(".json.bak"))
                except OSError:
                    pass

        self.data = _deep_merge(DEFAULT_CONFIG, raw)
        self._migrate_legacy(raw)
        self._sanitize()
        if not self.path.exists():
            # 首次运行：把默认配置落盘，方便用户发现并手动编辑配置文件
            self.save()
        return self.data

    def save(self) -> bool:
        """把当前配置写回磁盘。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再替换，避免写入中途崩溃导致配置文件损坏
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(self.data, fp, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
            return True
        except OSError as exc:
            log.error("配置保存失败：%s", exc)
            return False

    # ------------------------------------------------------------------
    # 读写访问
    # ------------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        """按 ``"hotkeys.record_stop"`` 形式的点号路径读取配置。"""
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        """按点号路径写入配置（中间层级不存在时自动创建）。"""
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    # ------------------------------------------------------------------
    # 便捷属性
    # ------------------------------------------------------------------
    @property
    def hotkeys(self) -> dict[str, str]:
        """当前生效的绑定（**只含已绑定的动作**：没绑的不会以空串出现）。"""
        return sanitize_bindings(self.data.get("hotkeys") or {})

    def hotkey(self, action: str) -> str:
        """某个动作当前的按键；**没绑定时返回空串**（不再回落到标准套）。"""
        return str(self.get(f"hotkeys.{action}", "") or "")

    def set_hotkey(self, action: str, key: str) -> None:
        """写入热键；传空串表示「清空这个功能的键」——直接把条目删掉。

        不留下 ``"action": ""`` 这种空壳：空串在读取时会被过滤掉，但写在文件里
        既难看又容易让人以为「配了但没生效」。
        """
        token = (key or "").strip().lower()
        hk = self.data.setdefault("hotkeys", {})
        if token:
            hk[action] = token
        else:
            hk.pop(action, None)

    # ------------------------------------------------------------------
    # 热键套（一套命名好的绑定）
    # ------------------------------------------------------------------
    @property
    def hotkey_profile(self) -> str:
        """当前生效的套：``builtin:standard`` / ``user:<文件名>`` / 空串。"""
        return str(self.get("hotkey_profile", "") or "")

    def set_hotkey_profile(self, key: str) -> None:
        self.set("hotkey_profile", str(key or ""))

    def hotkey_sets(self) -> list[HotkeySet]:
        """全部可选的套（内置 + 磁盘上的用户套）。"""
        return list_sets(self.path)

    def current_hotkey_set(self) -> Optional[HotkeySet]:
        """当前套对象；没选套、或文件已被删掉时返回 ``None``。"""
        return find_set(self.path, self.hotkey_profile)

    def apply_hotkey_set(self, item: HotkeySet) -> None:
        """整体换成另一套（覆盖当前绑定并记下 profile，**不落盘**）。"""
        self.data["hotkeys"] = sanitize_bindings(item.hotkeys)
        self.set_hotkey_profile(item.key)

    def clear_hotkeys(self) -> None:
        """不启用任何热键套：一个键都不绑。"""
        self.data["hotkeys"] = {}
        self.set_hotkey_profile("")

    def save_hotkeys_as_set(self, name: str) -> Optional[HotkeySet]:
        """把当前绑定存成一个用户套（同名覆盖），并切到它。"""
        item = save_set(self.path, name, self.hotkeys)
        if item is not None:
            self.set_hotkey_profile(item.key)
        return item

    def ensure_custom_profile(self) -> Optional[HotkeySet]:
        """用户改了某个键之后调用：保证改动落在**用户自己的套**里。

        * 绑定和当前套**完全一样**（只是点了保存、没改键）→ 什么都不做。
          这一条很关键：不加的话，用户随手点一下「保存设置」就会把内置套
          悄悄复制成「我的热键」，下拉里凭空多一项。
        * 当前已经是用户套 → 覆盖那个套的文件（返回 ``None``，不算新建）；
        * 当前是内置套或没选套 → **自动新建**一个套文件（名字 ``我的热键``，
          重名自动加序号），并把 profile 切过去。

        这样「改了一个键」不会污染内置套，用户下次也能在下拉里找回这一套；
        而第一次改键就是用户第一次拥有自己的套。
        """
        bindings = self.hotkeys
        current = self.current_hotkey_set()
        if current is None and not bindings:
            return None                    # 一个键都没有：没有可存的套
        if current is not None and bindings == sanitize_bindings(current.hotkeys):
            return None                    # 没改键
        if current is not None and not current.builtin:
            save_set(self.path, current.name, bindings)   # 写回同一个套
            return None
        return self.save_hotkeys_as_set(suggest_name(self.path))

    def migrate_hotkey_profile(self) -> bool:
        """把「有绑定但没有套」的老配置迁移成用户套（返回是否发生了迁移）。

        1.2.0 及之前每个动作都有出厂键，配置文件里躺着一整套绑定却没有
        ``hotkey_profile``。不迁移的话，用户升级后下拉框显示「未设置」，
        但他改过的键其实还在生效——界面和现实对不上。这里把它们原样存成
        「我的热键」套，profile 指向它，界面与现实立刻一致。
        """
        current = self.hotkey_profile
        if current and find_set(self.path, current) is not None:
            return False
        if current:
            log.warning("配置里的热键套 %s 已不存在，按现有绑定重建", current)
            self.set_hotkey_profile("")
        if not self.hotkeys:
            return False
        item = self.save_hotkeys_as_set(suggest_name(self.path))
        if item is None:
            return False
        log.info("已把现有热键绑定另存为热键套「%s」（%d 个键）",
                 item.name, item.count)
        return True

    @property
    def hotkey_exclusive(self) -> bool:
        """是否启用「热键独占模式」。"""
        return bool(self.get("hotkey_exclusive", True))

    def set_hotkey_exclusive(self, exclusive: bool) -> None:
        self.set("hotkey_exclusive", bool(exclusive))

    def merge_options(self) -> "MergeOptions":
        """自动合并开关（组合键 / 双击三击）。"""
        from core.event_parser import MergeOptions

        return MergeOptions(
            combo=bool(self.get("merge.combo", True)),
            multi_click=bool(self.get("merge.multi_click", True)),
        )

    @property
    def merge_combo(self) -> bool:
        return bool(self.get("merge.combo", True))

    @property
    def merge_multi_click(self) -> bool:
        return bool(self.get("merge.multi_click", True))

    def set_merge(self, combo: Optional[bool] = None,
                  multi_click: Optional[bool] = None) -> None:
        if combo is not None:
            self.set("merge.combo", bool(combo))
        if multi_click is not None:
            self.set("merge.multi_click", bool(multi_click))

    # ------------------------------------------------------------------
    # 快捷输入（文本模拟打字）
    # ------------------------------------------------------------------
    @property
    def quick_input_enabled(self) -> bool:
        """总开关。默认关闭——F1/F2/F3 在多数软件里本来有别的用途。"""
        return bool(self.get("quick_input.enabled", False))

    @property
    def quick_paste_enabled(self) -> bool:
        """F1「敲入剪贴板内容」的独立开关。"""
        return bool(self.get("quick_input.paste_enabled", True))

    @property
    def quick_delay_ms(self) -> int:
        """字符之间的敲击间隔（毫秒，已夹紧到合法范围）。"""
        from core.quick_input import clamp_delay

        return clamp_delay(self.get("quick_input.delay_ms", 8))

    @property
    def quick_method(self) -> str:
        """注入通道：``auto``（默认）/ ``unicode`` / ``simulate`` / ``message`` / ``vk``。"""
        from core.quick_input import normalize_method

        return normalize_method(self.get("quick_input.mode", "auto"))

    @property
    def quick_click_first(self) -> bool:
        """敲字前是否先在当前指针位置补一次左键单击（默认开）。

        页面内输入框（QQ / 腾讯微云 / WeGame 的登录框）只有收到真实鼠标按下
        才把键盘焦点交给页面，不发点击按键会被丢掉。
        """
        return bool(self.get("quick_input.click_first", True))

    def quick_password(self, slot: str) -> str:
        """读出某个密码槽的**明文**（自动解密）。"""
        from core.quick_input import unprotect

        return unprotect(self.get(f"quick_input.{slot}", ""))

    def set_quick_password(self, slot: str, text: str) -> None:
        """写入某个密码槽（自动加密后落盘）。"""
        from core.quick_input import protect

        self.set(f"quick_input.{slot}", protect(text))

    def upgrade_quick_passwords(self) -> int:
        """把旧密文（``enc1:``，无附加熵）重新加密成 ``enc2:``。

        为什么需要这一步：``enc1:`` 只用了 DPAPI 的「当前用户」密钥，**同一
        Windows 用户下任何程序都能解开**——那正是用户不希望发生的事。新格式
        带附加熵（见 :data:`core.quick_input._SECRET_ENTROPY`），拿不到那串
        字节就解不开。老配置不能丢，所以先按旧格式读懂、再按新格式重写。

        返回升级的条数（调用方决定要不要立刻 ``save()``）。
        """
        from core.quick_input import needs_upgrade, protect, unprotect

        upgraded = 0
        for slot in QUICK_PASSWORD_SLOTS:
            raw = self.get(f"quick_input.{slot}", "")
            if not needs_upgrade(raw):
                continue
            plain = unprotect(raw)
            if not plain:
                # 解密失败的就不动它：重写成空会把用户的密码彻底弄丢
                log.warning("密码槽 %s 的旧密文解不开，保持原样", slot)
                continue
            self.set(f"quick_input.{slot}", protect(plain))
            upgraded += 1
        if upgraded:
            log.info("已把 %d 个密码槽升级为带附加熵的加密格式（enc2）", upgraded)
        return upgraded

    def quick_hotkey_bindings(self) -> dict[str, str]:
        """当前**应当注册**的快捷输入热键。

        只返回真正可用的那几条：
        * 总开关关闭 → 一条都不注册（否则独占钩子会白白吞掉 F1–F4）；
        * 「敲剪贴板」关掉 → 不注册 ``quick_paste``；
        * 某个密码为空 → 不注册对应热键（按下只会打出空气，还不如让它透传）。

        用户改键后的 token 从 ``hotkeys`` 段读，所以这里返回的可以直接并进
        :meth:`Config.hotkeys` 交给 ``HotkeyManager.register_all``。
        """
        if not self.quick_input_enabled:
            return {}
        out: dict[str, str] = {}
        if self.quick_paste_enabled and self.hotkey("quick_paste"):
            out["quick_paste"] = self.hotkey("quick_paste")
        for index, slot in enumerate(QUICK_PASSWORD_SLOTS, start=1):
            token = self.hotkey(f"quick_pwd_{index}")
            if str(self.get(f"quick_input.{slot}", "") or "").strip() and token:
                out[f"quick_pwd_{index}"] = token
        return out

    # ------------------------------------------------------------------
    # 循环回放（只作用于模式一）
    # ------------------------------------------------------------------
    @property
    def loop_mode(self) -> str:
        """循环模式：``none`` / ``fixed`` / ``infinite``。"""
        from core.player import normalize_loop_mode

        return normalize_loop_mode(self.get("loop.mode", "none"))

    @property
    def loop_count(self) -> int:
        """固定轮数模式的目标轮数。"""
        from core.player import normalize_loop_count

        return normalize_loop_count(self.get("loop.count", 1))

    def set_loop(self, mode: Optional[str] = None,
                 count: Optional[int] = None) -> None:
        from core.player import normalize_loop_count, normalize_loop_mode

        if mode is not None:
            self.set("loop.mode", normalize_loop_mode(mode))
        if count is not None:
            self.set("loop.count", normalize_loop_count(count))

    def scripts_path(self) -> Path:
        """脚本目录（绝对路径，自动创建）。"""
        from utils.win_utils import ensure_dir, scripts_dir as resolve_scripts

        return ensure_dir(resolve_scripts(self.base_dir, self.get("scripts_dir")))

    def log_dir(self) -> Path:
        from utils.win_utils import ensure_dir

        return ensure_dir(app_data_dir() / "logs")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _migrate_legacy(self, raw: dict[str, Any]) -> None:
        """把开发阶段可能存在的扁平热键键名迁移到 hotkeys 节。"""
        for old, (section, new) in _LEGACY_KEYS.items():
            if old in raw and raw[old]:
                self.data.setdefault(section, {})[new] = raw[old]
                self.data.pop(old, None)

    def _sanitize(self) -> None:
        """修正非法值，保证后续模块拿到的一定是可用数据。"""
        # 打包版：开发阶段默认脚本目录 data/scripts 迁移到 %APPDATA% 下的 scripts/
        # （config_dir 已是 %APPDATA%\MacroSmith，"scripts" 解析后即 %APPDATA%\MacroSmith\scripts）
        if is_frozen() and self.data.get("scripts_dir") == "data/scripts":
            self.data["scripts_dir"] = "scripts"

        # 热键：只做「去空 / 转小写」的清理，**绝不补默认值**。
        # 出厂就是一个键都不绑（见 module docstring），补默认等于把用户没同意
        # 的按键抢过来——F1 帮助键会变成「敲剪贴板」，这事被投诉过。
        self.data["hotkeys"] = sanitize_bindings(self.data.get("hotkeys"))
        profile = self.data.get("hotkey_profile")
        self.data["hotkey_profile"] = profile if isinstance(profile, str) else ""

        win = self.data.setdefault("window", {})
        # 尺寸迁移：正好等于历史默认尺寸 => 用户没调过，升级到新版默认 1000x700
        try:
            stored = (int(win.get("width") or 0), int(win.get("height") or 0))
        except (TypeError, ValueError):
            stored = (0, 0)
        if stored in _LEGACY_DEFAULT_SIZES:
            win["width"], win["height"] = DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT

        # 宽高各自有下限：宽度 820（界面横向布局需要），高度 560（标题栏+内容区）
        for key, floor in (("width", 820), ("height", 560)):
            try:
                value = int(win.get(key) or DEFAULT_CONFIG["window"][key])
            except (TypeError, ValueError):
                value = int(DEFAULT_CONFIG["window"][key])
            win[key] = max(floor, value)
        for key in ("x", "y"):
            try:
                win[key] = None if win.get(key) is None else int(win[key])
            except (TypeError, ValueError):
                win[key] = None

        merge = self.data.setdefault("merge", {})
        for key in ("combo", "multi_click"):
            value = merge.get(key)
            merge[key] = True if value is None else bool(value)

        loop = self.data.setdefault("loop", {})
        from core.player import normalize_loop_count, normalize_loop_mode

        loop["mode"] = normalize_loop_mode(loop.get("mode", "none"))
        loop["count"] = normalize_loop_count(loop.get("count", 1))

        player = self.data.setdefault("player", {})
        if player.get("default_mode") not in ("timed", "step"):
            player["default_mode"] = "timed"
        for key, default in (
            ("speed", 1.0),
            ("move_min_interval_ms", 50),
            ("move_min_distance_px", 2),
        ):
            try:
                player[key] = type(default)(player.get(key, default))
            except (TypeError, ValueError):
                player[key] = default

        quick = self.data.setdefault("quick_input", {})
        for key, default in (("enabled", False), ("paste_enabled", True),
                             ("click_first", True)):
            quick[key] = bool(default if quick.get(key) is None else quick[key])
        from core.quick_input import clamp_delay, normalize_method

        quick["delay_ms"] = clamp_delay(quick.get("delay_ms", 8))
        # 通道只允许白名单取值，写错（手工编辑）时回落到默认，别让打字直接哑掉
        quick["mode"] = normalize_method(quick.get("mode", "auto"))
        # 密码槽统一成字符串：手工编辑成数字/对象时也不该让程序崩在打字前
        for slot in QUICK_PASSWORD_SLOTS:
            quick[slot] = str(quick.get(slot) or "")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并字典：以 base 为骨架，用 override 覆盖同名键。

    注意：``base`` 里嵌套的字典必须**深拷贝**，不能直接复用对象。
    否则 ``Config.set("merge.combo", ...)`` 会顺着共享引用改到
    :data:`DEFAULT_CONFIG` 上，导致「同一进程内新建的配置全都带上了
    上一个配置的改动」——测试之间互相污染就是这么来的。
    """
    out: dict[str, Any] = {}
    for key, value in (base or {}).items():
        out[key] = copy.deepcopy(value)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
