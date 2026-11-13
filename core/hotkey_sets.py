"""快捷键方案（hotkey set）：一组「动作 -> 按键」的命名组合。

**术语**：界面上一律叫「快捷键方案」（2026-11-13 用户定名）。代码里的
``set`` / ``hotkey_sets`` / ``hotkey_profile`` 这些标识**保持不动**——它们
是持久化契约（配置键名、目录名），改了就要搬用户已有的文件，为一个措辞不值。
叫「方案」而不是「套」的原因：「套」是量词，当名词用时「当前套 / 删除此套」
都得靠上下文补宾语。

为什么要有这个东西
------------------
2026-11-13 之前，13 个动作全都有出厂热键：录制 F12/F11/F9、回放 F6/F7/F8、
单步 PageUp/PageDown/Home、快捷输入 F1–F4。两个实际麻烦：

1. **一装就占了半个键盘**。用户只想点点按钮，却发现 F1 按下去弹的是
   「敲剪贴板」——而这些键在别的软件里本来各有用途（F1 帮助、F2 重命名）。
2. **换一套习惯要点 13 次**。想从 F 区换到小键盘，只能在设置页一个个改。

所以改成：出厂**一个键都不绑**；用户从「快捷键方案」下拉里挑一个现成的
（随软件附带 :data:`BUILTIN_SETS`），或者直接去改某一个功能——第一次改键时
自动把当前绑定另存成一个新的方案文件，之后的改动都落在那个方案里。

文件位置
--------
``<config 同目录>/hotkey_sets/<方案名>.json``，内容::

    {"name": "我的热键", "hotkeys": {"record_stop": "f9", "play_toggle": "f6"}}

内置方案**不落盘**（写在代码里），只有用户自己的方案才建文件 —— 这样以后调整
内置方案的按键时，不会和用户已经改过的文件打架。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

log = logging.getLogger(__name__)

#: 内置「标准方案」的标识（存进 config 的 ``hotkey_profile``）
STANDARD_KEY = "builtin:standard"

#: 随软件附带的标准方案按键。**这不是出厂绑定**：出厂是空的，用户选中它才有。
#: 内容与 1.2.0 之前版本的默认热键完全一致，老用户凭记忆直接选它即可。
STANDARD_HOTKEYS: dict[str, str] = {
    "record_start": "f12",   # 开始录制
    "record_stop": "f9",     # 停止录制
    "record_pause": "f11",   # 暂停/继续录制
    "play_toggle": "f6",     # 模式一：开始回放
    "pause_toggle": "f7",    # 模式一：暂停/继续
    "play_stop": "f8",       # 模式一：停止回放
    "step_next": "page_down",  # 模式二：下一步
    "step_prev": "page_up",    # 模式二：上一步
    "step_reset": "home",      # 模式二：重置指针
    # 快捷输入（文本模拟打字）：F1 敲剪贴板内容，F2/F3/F4 敲三个密码
    "quick_paste": "f1",
    "quick_pwd_1": "f2",
    "quick_pwd_2": "f3",
    "quick_pwd_3": "f4",
}

#: 内置方案：``唯一标识 -> (显示名, 绑定)``。**别放需要 IO 的东西**。
BUILTIN_SETS: dict[str, tuple[str, dict[str, str]]] = {
    STANDARD_KEY: ("标准方案（F6–F12 · PgUp/PgDn · Home）", dict(STANDARD_HOTKEYS)),
}

#: 用户方案的标识前缀
USER_PREFIX = "user:"

#: 自动新建的方案用什么名字（重名时自动加序号）。
#: 名字保持「我的热键」不改：它是用户能改的名字，不是术语；
#: 而且老用户机器上已有的方案文件就叫这个，改名反而像另一个东西。
DEFAULT_SET_NAME = "我的热键"

#: 文件名里不能出现的字符（与 ui/name_dialog.py 保持一致）
_INVALID_CHARS = '<>:"/\\|?*'


@dataclass
class HotkeySet:
    """一个快捷键方案。

    ``key`` 是持久化标识：内置方案 ``builtin:<名字>``，用户方案 ``user:<文件名>``。
    名字可以改，``key`` 不改——改名字等于另存为新文件（见 :func:`save_set`）。
    """

    key: str
    name: str
    hotkeys: dict[str, str] = field(default_factory=dict)
    builtin: bool = False

    @property
    def count(self) -> int:
        """这个方案里绑了几个键（给界面显示用）。"""
        return len(self.hotkeys)

    def summary(self) -> str:
        """``标准方案（13 个键）`` 这样的短描述。"""
        return f"{self.name}（{self.count} 个键）"


# ===========================================================================
# 路径与清理
# ===========================================================================
def sets_dir(config_path: Path) -> Path:
    """方案目录（``hotkey_sets/``）：与配置文件同级，方便一起备份/迁移。"""
    return Path(config_path).parent / "hotkey_sets"


def slugify(name: str) -> str:
    """把方案名变成安全的文件名（保留中文，只换掉非法字符）。"""
    cleaned = "".join("_" if ch in _INVALID_CHARS else ch for ch in (name or ""))
    return cleaned.strip(" .") or ""


def sanitize_bindings(bindings: Any) -> dict[str, str]:
    """只保留「非空字符串」的绑定，并统一成小写。

    ``""`` 代表「这个功能不绑键」——在本项目里是合法状态（出厂全是空的），
    所以**不能当错误丢掉**，丢掉就变成「键不存在」，读的时候还得再兜一层。
    这里统一转成「不存在」：空值直接不写进字典。
    """
    out: dict[str, str] = {}
    if not isinstance(bindings, Mapping):
        return out
    for action, token in bindings.items():
        if not isinstance(action, str) or not action.strip():
            continue
        if not isinstance(token, str):
            continue
        token = token.strip().lower()
        if token:
            out[action.strip()] = token
    return out


# ===========================================================================
# 读写
# ===========================================================================
def builtin_list() -> list[HotkeySet]:
    """内置方案（不读磁盘）。"""
    return [
        HotkeySet(key=key, name=name, hotkeys=dict(bindings), builtin=True)
        for key, (name, bindings) in BUILTIN_SETS.items()
    ]


def list_sets(config_path: Path) -> list[HotkeySet]:
    """全部方案：内置在前，用户方案按文件名排序在后。

    坏文件（JSON 语法错误、顶层不是对象）**跳过并记日志**，不让一个手改坏的
    文件把整个下拉框干掉——用户还有别的方案能用。
    """
    sets = builtin_list()
    directory = sets_dir(config_path)
    if not directory.exists():
        return sets

    for path in sorted(directory.glob("*.json"), key=lambda p: p.stem):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("跳过无法读取的方案 %s：%s", path.name, exc)
            continue
        if not isinstance(data, dict):
            log.warning("方案 %s 顶层不是对象，已跳过", path.name)
            continue
        name = str(data.get("name") or path.stem)
        sets.append(HotkeySet(
            key=f"{USER_PREFIX}{path.stem}",
            name=name,
            hotkeys=sanitize_bindings(data.get("hotkeys")),
            builtin=False,
        ))
    return sets


def find_set(config_path: Path, key: str) -> Optional[HotkeySet]:
    """按 ``key`` 找方案；找不到返回 ``None``（配置文件里的残留标识）。"""
    if not key:
        return None
    for item in list_sets(config_path):
        if item.key == key:
            return item
    return None


def save_set(config_path: Path, name: str, hotkeys: Mapping[str, Any]) -> Optional[HotkeySet]:
    """把一份绑定写成用户方案文件（同名覆盖）。名字非法时返回 ``None``。"""
    stem = slugify(name)
    if not stem:
        return None
    directory = sets_dir(config_path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{stem}.json"
        payload = {"name": name.strip() or stem,
                   "hotkeys": sanitize_bindings(hotkeys)}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.error("方案保存失败：%s", exc)
        return None
    log.info("方案已写入：%s（%d 个键）", path.name, len(payload["hotkeys"]))
    return HotkeySet(key=f"{USER_PREFIX}{stem}", name=payload["name"],
                     hotkeys=payload["hotkeys"], builtin=False)


def delete_set(config_path: Path, key: str) -> bool:
    """删除一个**用户**方案；内置方案不可删（返回 False）。"""
    if not key.startswith(USER_PREFIX):
        return False
    stem = key[len(USER_PREFIX):]
    path = sets_dir(config_path) / f"{stem}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.error("方案删除失败：%s", exc)
        return False
    log.info("方案已删除：%s", path.name)
    return True


def suggest_name(config_path: Path, base: str = DEFAULT_SET_NAME) -> str:
    """给新方案起一个不重名的名字：``我的热键`` / ``我的热键 2`` / ``我的热键 3``…"""
    used = {slugify(item.name) for item in list_sets(config_path) if not item.builtin}
    used |= {slugify(path.stem) for path in _existing_files(config_path)}
    if slugify(base) not in used:
        return base
    index = 2
    while slugify(f"{base} {index}") in used:
        index += 1
    return f"{base} {index}"


def _existing_files(config_path: Path) -> Iterable[Path]:
    directory = sets_dir(config_path)
    if not directory.exists():
        return ()
    return tuple(directory.glob("*.json"))
