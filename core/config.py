"""配置持久化管理。

- 开发阶段：项目根目录 ``config.json``
- 正式发布：``%APPDATA%\\MacroSmith\\config.json``

配置结构（节选）::

    {
      "hotkeys": {"record_stop": "f9", "play_toggle": "f6", ...},
      "window":  {"x": null, "y": null, "width": 1000, "height": 700},
      "player":  {"default_mode": "timed", "speed": 1.0, ...},
      "scripts_dir": "data/scripts",
      "last_script": null
    }
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Optional

from utils.win_utils import app_data_dir, is_frozen, project_root

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

#: 所有可配置热键的默认值（键名 -> 默认按键）
DEFAULT_HOTKEYS: dict[str, str] = {
    "record_stop": "f9",     # 停止录制
    "play_toggle": "f6",     # 模式一：开始回放
    "pause_toggle": "f7",    # 模式一：暂停/继续
    "play_stop": "f8",       # 模式一：停止回放
    "step_next": "page_down",  # 模式二：下一步
    "step_prev": "page_up",    # 模式二：上一步
    "step_reset": "home",      # 模式二：重置指针
}

#: 热键的界面显示名与说明
HOTKEY_LABELS: dict[str, tuple[str, str]] = {
    "record_stop": ("停止录制", "录制过程中结束录制并保存事件序列"),
    "play_toggle": ("开始回放", "模式一：按录制时的时间间隔原速回放"),
    "pause_toggle": ("暂停 / 继续", "模式一：暂停后保持执行位置，再次按下继续"),
    "play_stop": ("停止回放", "模式一：终止回放并把执行指针归零"),
    "step_next": ("下一步（模式二）", "模式二：执行下一个操作单元"),
    "step_prev": ("上一步（模式二）", "模式二：回退到上一个操作单元"),
    "step_reset": ("重置指针（模式二）", "模式二：把步骤指针复位到开头"),
}

DEFAULT_CONFIG: dict[str, Any] = {
    "hotkeys": dict(DEFAULT_HOTKEYS),
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
        return dict(self.data.get("hotkeys", {}))

    def hotkey(self, action: str) -> str:
        return str(self.get(f"hotkeys.{action}", DEFAULT_HOTKEYS.get(action, "")))

    def set_hotkey(self, action: str, key: str) -> None:
        self.set(f"hotkeys.{action}", key)

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

        hk = self.data.setdefault("hotkeys", {})
        for action, default in DEFAULT_HOTKEYS.items():
            value = hk.get(action)
            if not isinstance(value, str) or not value.strip():
                hk[action] = default
            else:
                hk[action] = value.strip().lower()

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
