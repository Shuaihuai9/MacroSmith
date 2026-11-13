"""快捷输入：把一段文本「敲」进当前获得焦点的输入框。

为什么不用剪贴板粘贴
--------------------
``Ctrl+V`` 走剪贴板，会被一部分程序区别对待：密码框拒收、远程桌面/虚拟机
需要双向剪贴板映射、某些安全软件会拦截跨进程剪贴板读取；而且粘贴会**覆盖**
用户原本的剪贴板内容。逐字符敲击走的是 ``SendInput``，对目标程序来说与真人
敲键盘没有区别，绕开了上述全部限制。

为什么默认只打 ASCII（产品取舍，不是技术限制）
----------------------------------------------
走 ``KEYEVENTF_UNICODE`` 之后，字符是直接塞进消息队列的，技术上中文/全角
也能打进去。仍然过一遍 :func:`sanitize` 只留 ``0x20–0x7E``，是因为本功能的
典型场景——密码、验证码、邮箱、URL——全在 ASCII 内；而多打的字符一旦落进
「确认密码」这类框里就是纯粹的破坏。想放开只需放宽 :func:`sanitize`。

顺带说明这条通道为什么**不是**「虚拟键码 + Shift」：那条路会被输入法接管
（中文态下 ``b`` 变「杯」、``;`` 变「；」）、被键盘布局改写（qwertz/dvorak）、
被 ``CapsLock`` 翻大小写。``KEYEVENTF_UNICODE`` 三个坑全绕开。

为什么还要一条**窗口消息**通道（``message``）
----------------------------------------------
``SendInput`` 的入口是系统输入栈，带内核防护的程序（QQ 游戏大厅、游戏
客户端、部分银行安全控件）会把它整体掐掉：实测 ``SendInput`` /
``keybd_event`` / ``mouse_event`` 全无效、低级钩子收不到、``GetAsyncKeyState``
读回 0 —— 事件根本没进系统。这时任何「换键码 / 换扫描码」的改法都没用。
:mod:`core.direct_input` 改投窗口消息（``WM_CHAR`` / ``WM_SETTEXT``），
不产生输入事件，反注入驱动拦不到。

在它之前还有两级注入（``physical`` → ``simulate``），因为拦截规则并不总是
「一刀切」：有的保护模块只按标志位过滤，丢掉 ``KEYEVENTF_UNICODE`` 的注入
却放行带扫描码的物理按键。``method="auto"`` 会**逐级探针验证**再决定走哪条
路，状态栏会说明这次走的是哪一级。

> 自动阶梯**不包含** ``unicode``：它测的是「注入能不能进系统」，而
> ``KEYEVENTF_UNICODE`` 是「进得去、被目标挑食」，探针看不出来。要它请在
> 设置页手工选。

与录制的关系
------------
这些热键与敲出来的按键**都不能进脚本**：
* 热键本身 → 由主窗口把它们加进 ``Recorder.ignored_keys``；
* 录制期间整组功能直接拒动（``_handle_quick_input`` 提前返回）；
* 打字**途中**若开始录制 → ``type_text(should_stop=...)`` 下一个字符前就收手。
  这条最容易被忽略：用户按 F1 敲一段长密码，敲到第 5 个字符时顺手按了开始
  录制，剩下 15 个字符若继续注入就会变成脚本步骤——录制器分不清注入与真实
  输入（我们**故意**不做"注入即丢弃"，否则屏幕键盘/远程桌面的输入也录不到），
  所以只能在打字这一侧主动停下。

至于注入事件本身：``HotkeyManager._on_press`` 会丢弃 ``injected`` 的按键，
免得敲出来的字母碰巧命中用户绑的单键热键；独占模式下的 Win32 键盘钩子
也按 ``LLKHF_INJECTED`` 放行。

密码的本地保护
--------------
:func:`protect` / :func:`unprotect` 用 Windows DPAPI（``CryptProtectData``）
加密，密钥由系统按**当前用户**派生，配置文件被拷到别的机器/别的用户下解不开。
这不是"安全的密码管理器"（本进程有权限就能解），只是避免 ``config.json``
里躺着明晃晃的明文密码——那是最常见的一种泄漏方式。
失败时（非 Windows、系统 API 异常）自动降级为明文，保证功能不因此失效。
"""

from __future__ import annotations

import base64
import ctypes
import logging
import os
import time
from ctypes import wintypes
from typing import Callable, Optional, Sequence

from core.win_input import (injection_delivered, send_char, send_key,
                            send_key_scancode, send_physical_key)

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

#: 字符之间的默认间隔（毫秒）。太快会把目标输入框灌丢字符，
#: 太慢又让人等到怀疑没生效；5~10ms 是实测的甜点区。
DEFAULT_DELAY_MS = 8
#: 允许的间隔范围（设置页/配置里越界时夹紧）
MIN_DELAY_MS = 1
MAX_DELAY_MS = 50

#: 左 Shift 的虚拟键码（大写字母与上档符号要靠它，仅 ``vk`` 通道使用）
VK_SHIFT = 0xA0

#: 注入通道：
#:
#: * ``auto``（默认）—— 依次试：``unicode`` → ``simulate`` → ``message``。
#:   每一级都先用探针确认「事件真进了系统」再整段下发，不是靠返回值猜；
#: * ``unicode`` —— ``KEYEVENTF_UNICODE`` 逐字符注入，不受输入法、键盘布局、
#:   CapsLock 影响（:func:`core.win_input.send_char`）；
#: * ``simulate`` —— **模拟物理按键**：只给扫描码 + ``KEYEVENTF_SCANCODE``
#:   （:func:`core.win_input.send_key_scancode`）。有些保护程序只按标志位
#:   过滤，丢字符注入但放行扫描码按键，这条就派上用场；代价是会受输入法
#:   与键盘布局影响；
#: * ``physical`` —— **键码 + 扫描码都填准**，形态最接近真键盘
#:   （:func:`core.win_input.send_physical_key`，也就是 AutoIt ``Send()``
#:   的做法）。目标程序若在应用层校验「键码与扫描码是否匹配」，只有这条能过；
#: * ``message`` —— 窗口消息（``WM_CHAR`` / ``WM_SETTEXT``），不经过输入栈，
#:   反注入驱动拦不到（:mod:`core.direct_input`）；
#: * ``vk`` —— 虚拟键码 + Shift 包裹（:func:`core.win_input.send_key`）。
#:   只认键码、不处理 ``WM_CHAR`` 的程序里也能敲，但**中文输入法开着时会串味**。
TYPING_METHODS: tuple[str, ...] = ("auto", "unicode", "physical", "simulate",
                                   "message", "vk")
DEFAULT_METHOD = "auto"


# ---------------------------------------------------------------------------
# ASCII 过滤
# ---------------------------------------------------------------------------
def sanitize(text: object) -> str:
    """只保留可打印 ASCII（0x20–0x7E），其余全部丢弃。

    这是**产品取舍**：``unicode`` 通道技术上能打中文，但本功能的典型场景
    （密码、验证码、邮箱、URL）全在 ASCII 内，多打的字符一旦落进「确认密码」
    这类框里就是纯粹的破坏。制表符与换行同样丢弃——它们会让后续字符跑到
    别的输入框或别的地方去。

    >>> sanitize("abc中文123！")
    'abc123'
    """
    return "".join(ch for ch in str(text or "") if 0x20 <= ord(ch) <= 0x7E)


#: 上档符号 -> (虚拟键码, 是否需要 Shift)
_SHIFTED: dict[str, int] = {
    "!": 0x31, "@": 0x32, "#": 0x33, "$": 0x34, "%": 0x35,
    "^": 0x36, "&": 0x37, "*": 0x38, "(": 0x39, ")": 0x30,
    "_": 0xBD, "+": 0xBB, "{": 0xDB, "}": 0xDD, "|": 0xDC,
    ":": 0xBA, '"': 0xDE, "<": 0xBC, ">": 0xBE, "?": 0xBF,
    "~": 0xC0,
}

#: 无需 Shift 的符号（OEM 键的虚拟键码与字符本身不是同一个值）
_PLAIN: dict[str, int] = {
    " ": 0x20, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE,
    "/": 0xBF, "`": 0xC0,
}


def char_to_stroke(ch: str) -> Optional[tuple[int, bool]]:
    """单个字符 -> ``(虚拟键码, 是否需要按住 Shift)``；无法敲出时返回 None。"""
    if len(ch) != 1:
        return None
    if "a" <= ch <= "z":
        return ord(ch.upper()), False
    if "A" <= ch <= "Z":
        return ord(ch), True
    if "0" <= ch <= "9":
        return ord(ch), False
    vk = _SHIFTED.get(ch)
    if vk is not None:
        return vk, True
    vk = _PLAIN.get(ch)
    if vk is not None:
        return vk, False
    return None


def text_to_strokes(text: object) -> list[tuple[int, bool]]:
    """把文本转成敲击序列（已过滤掉敲不出来的字符）。"""
    strokes: list[tuple[int, bool]] = []
    for ch in sanitize(text):
        stroke = char_to_stroke(ch)
        if stroke is not None:
            strokes.append(stroke)
    return strokes


def clamp_delay(delay_ms: object) -> int:
    """把间隔夹到 :data:`MIN_DELAY_MS` .. :data:`MAX_DELAY_MS`。"""
    try:
        value = int(delay_ms)
    except (TypeError, ValueError):
        return DEFAULT_DELAY_MS
    return max(MIN_DELAY_MS, min(MAX_DELAY_MS, value))


# ---------------------------------------------------------------------------
# 敲击
# ---------------------------------------------------------------------------
def normalize_method(value: object) -> str:
    """把 ``method`` 归一化成 :data:`TYPING_METHODS` 里的成员。"""
    text = str(value or "").strip().lower()
    return text if text in TYPING_METHODS else DEFAULT_METHOD


def _unicode_groups(text: object) -> list[list[tuple[str, bool]]]:
    """``unicode`` 通道：每个字符 = 一次按下 + 一次抬起。"""
    return [[(ch, False), (ch, True)] for ch in sanitize(text)]


def _vk_groups(text: object) -> list[list[tuple[int, bool]]]:
    """``vk`` 通道：每个字符 = 一次按下 + 一次抬起，上档再加 Shift 包裹。"""
    groups: list[list[tuple[int, bool]]] = []
    for vk, need_shift in text_to_strokes(text):
        if need_shift:
            groups.append([(VK_SHIFT, False), (vk, False),
                           (vk, True), (VK_SHIFT, True)])
        else:
            groups.append([(vk, False), (vk, True)])
    return groups


def _notify(report: Optional[Callable[[str], None]], channel: str) -> None:
    """把「这条文本最终走了哪条通道」告诉调用方（供状态栏说明用）。"""
    if report is None:
        return
    try:
        report(channel)
    except Exception:  # pragma: no cover - 回调异常不影响打字
        log.debug("report 回调异常", exc_info=True)


#: 每条注入通道对应的下发函数（``auto`` 会自己先选一条再取）
_SENDERS: dict[str, Callable[[object, bool], bool]] = {
    "unicode": send_char,
    "physical": send_physical_key,
    "simulate": send_key_scancode,
    "vk": send_key,
}


def _pick_auto_channel() -> str:
    """``auto`` 的第一段：挑一条**真的能把事件送进系统**的注入通道。

    ★ 2026-11-12 对照实验推翻了这里原来的顺序。原来的逻辑是「``unicode``
    最稳，能用就用」，而探针通过只说明**注入通道整体没被封**——探针本身用的
    就是 vk 形式的按键，它根本测不出 ``KEYEVENTF_UNICODE`` 会不会被
    目标挑食。实测（``tools/captures/watch_ref_*.log`` + ``tools/try_type.py``）：
    QQ 游戏大厅的密码框**拒收 ``KEYEVENTF_UNICODE``（``wVk=0`` + 字符码）
    的事件，但任何带真扫描码的按键都收**。所以 auto 必须先走 ``physical``。

    阶梯：

    1. 注入能进系统 → ``physical``（``wVk`` 与 ``wScan`` 都填准，和真键盘
       发出的东西一模一样，也是参考工具实际用的形态）；
    2. vk 形式被拦、扫描码还能过 → ``simulate``；
    3. 两条都不通 → 返回空串，由调用方整段改走窗口消息。

    探针各花约 25ms，只在用户真的按下热键时发生一次，感知不到。

    ※ 探针键是**左 Shift**（原来是 F24）。F24 被部分安全驱动专门过滤，
    会让这里整段误判成「两条都不通」而直接跳窗口消息——2026-11-12 17:30
    的日志就是这么错的（目标明明是 ``class='Edit'``）。
    """
    if injection_delivered():
        log.info("自动通道：物理按键（vk+扫描码）")
        return "physical"
    if injection_delivered(scancode=True):
        log.info("vk 形式被拦，扫描码通道可用，改用仅扫描码")
        return "simulate"
    log.info("两条注入通道都被拦，改走窗口消息（若目标其实收物理按键，"
             "多半是探针被针对性过滤了，可在设置页手工指定「物理按键」）")
    return ""


def _type_via_messages(text: object, *, delay_ms: int,
                       sleep: Callable[[float], None],
                       should_stop: Optional[Callable[[], bool]],
                       report: Optional[Callable[[str], None]]) -> int:
    """窗口消息通道（不经过输入栈）。"""
    from core import direct_input

    typed = direct_input.type_text(
        text, delay_ms=delay_ms, sleep=sleep, should_stop=should_stop)
    _notify(report, "message" if typed else "message-failed")
    if typed:
        log.info("快捷输入：走 message 通道，送出 %d 个字符", typed)
    else:
        log.info("窗口消息通道没有送出任何字符（焦点控件可能不接受文本消息）")
    return typed


def type_text(text: object, *, delay_ms: int = DEFAULT_DELAY_MS,
              method: str = DEFAULT_METHOD,
              sender: Optional[Callable[[object, bool], bool]] = None,
              sleep: Optional[Callable[[float], None]] = None,
              should_stop: Optional[Callable[[], bool]] = None,
              report: Optional[Callable[[str], None]] = None) -> int:
    """把文本逐字符送进当前焦点窗口，返回真正送出的字符数。

    ``method`` 决定注入通道（见 :data:`TYPING_METHODS`），默认 ``auto``：

    * 先走 ``physical``（``SendInput``，``wVk`` 与 ``wScan`` 都填准）——
      和真键盘发出的东西一模一样。**2026-11-12 的对照实验**证明 QQ 游戏大厅
      的密码框只认这种形态：它拒收 ``KEYEVENTF_UNICODE``（``wVk=0``）的
      事件，却收任何带真扫描码的按键（参考工具用的正是这条）；
    * 探针确认 vk 形式进不去、只有扫描码能过时改 ``simulate``；
    * 两条注入通道都被拒（典型场景是前台程序 ``BlockInput`` 或带反注入
      驱动）时**自动改走 ``message``**（窗口消息，不经输入栈），整段重新
      送一遍。逐字符阶段首个字符失败也触发同样的降级。

    ``report`` 会在选定通道后被调用一次，参数是 ``"physical"`` /
    ``"simulate"`` / ``"unicode"`` / ``"message"`` / ``"message-failed"`` /
    ``"vk"``，主窗口据此在状态栏说明「这次是走哪条路进去的」——对受保护
    窗口排障很关键。

    ``sender`` / ``sleep`` 可注入替身，测试据此不碰真实键盘。``sender`` 的
    第一个参数随通道而变：``unicode`` 是字符，``vk`` 是虚拟键码；
    签名都是 ``fn(payload, up) -> bool``。

    ``should_stop`` 每送一个字符问一次，返回 True 就立刻收手。主窗口用它实现
    「打字途中按下开始录制 → 剩余字符一个都不许再送」，否则那半截字符会
    变成脚本里的按键步骤（录制器无法分辨注入与真实输入，见 ``Recorder``）。

    延时只加在**字符之间**，末尾不空等；一个字符的多条事件（Shift 包裹）
    之间不加延时，否则一个 `!` 会被拆成四次有间隔的输入，输入法容易吃掉。
    """
    kind = normalize_method(method)
    wait = sleep or time.sleep

    if kind == "message":
        return _type_via_messages(text, delay_ms=delay_ms, sleep=wait,
                                  should_stop=should_stop, report=report)

    auto = kind == "auto"
    if auto:
        if sender is not None:
            # 测试替身：不探针，但通道要和真实 auto 选的一致（physical），
            # 否则替身测出来的行为和线上行为就是两码事。
            kind = "physical"
        else:
            # 先确认注入事件真的能进系统：反注入驱动下 SendInput 会**返回成功
            # 却什么都不到**（QQ / QQ 游戏大厅实测），只看返回值等于白打。
            chosen = _pick_auto_channel()
            if not chosen:
                return _type_via_messages(
                    text, delay_ms=delay_ms, sleep=wait,
                    should_stop=should_stop, report=report)
            kind = chosen
    send = sender or _SENDERS[kind]
    gap = max(0.0, clamp_delay(delay_ms) / 1000.0)

    groups = _unicode_groups(text) if kind == "unicode" else _vk_groups(text)
    for index, group in enumerate(groups):
        if should_stop is not None and should_stop():
            return index
        try:
            for order, (payload, up) in enumerate(group):
                ok = send(payload, up)
                # 首个字符的第一次按下就被拒：输入栈被整体封了，改走窗口消息。
                # 只看第一组：中途失败更可能是注入被临时打断，不该整段重来。
                if auto and ok is False and index == 0 and order == 0:
                    log.info("SendInput 被系统拒绝（输入被拦截），改走窗口消息通道")
                    return _type_via_messages(
                        text, delay_ms=delay_ms, sleep=wait,
                        should_stop=should_stop, report=report)
        except Exception:  # pragma: no cover - 注入被拦截时不让线程炸掉
            log.warning("快捷输入在第 %d 个字符处中断", index + 1, exc_info=True)
            if auto and index == 0:
                return _type_via_messages(
                    text, delay_ms=delay_ms, sleep=wait,
                    should_stop=should_stop, report=report)
            return index
        if gap > 0 and index + 1 < len(groups):
            wait(gap)
    # 成功路径也记一行：排障时「日志里什么都没有」和「走的是字符通道」是
    # 两种完全不同的结论，一个 INFO 就能分开（2026-11-11 QQ 那轮吃过亏）。
    log.info("快捷输入：走 %s 通道，送出 %d 个字符", kind, len(groups))
    _notify(report, kind)
    return len(groups)


# ---------------------------------------------------------------------------
# 剪贴板（Win32，避免 core 层依赖 Qt）
# ---------------------------------------------------------------------------
CF_UNICODETEXT = 13
_OPEN_RETRIES = 5
_OPEN_RETRY_SLEEP = 0.02


def read_clipboard_text() -> str:
    """读取系统剪贴板里的文本；没有文本或读取失败返回空串。

    剪贴板是**进程间独占**资源，别的程序正在写入时 ``OpenClipboard`` 会失败，
    所以这里重试几次再放弃（直接报错会让用户以为是本程序的 bug）。
    """
    if not IS_WINDOWS:  # pragma: no cover - 仅为可移植性
        return ""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    opened = False
    for attempt in range(_OPEN_RETRIES):
        try:
            if user32.OpenClipboard(None):
                opened = True
                break
        except Exception:  # pragma: no cover - 依赖系统 API
            log.debug("OpenClipboard 调用异常", exc_info=True)
            return ""
        time.sleep(_OPEN_RETRY_SLEEP * (attempt + 1))
    if not opened:
        log.warning("剪贴板被其它程序占用，读取失败")
        return ""

    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""                     # 剪贴板里不是文本（图片 / 文件）
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            log.warning("剪贴板内存锁定失败")
            return ""
        try:
            return ctypes.wstring_at(ptr) or ""
        finally:
            kernel32.GlobalUnlock(handle)
    except Exception:  # pragma: no cover - 依赖系统 API
        log.warning("读取剪贴板失败", exc_info=True)
        return ""
    finally:
        try:
            user32.CloseClipboard()
        except Exception:  # pragma: no cover
            log.debug("CloseClipboard 调用异常", exc_info=True)


# ---------------------------------------------------------------------------
# 密码本地加密（Windows DPAPI）
# ---------------------------------------------------------------------------
#: 加密值的前缀。没有该前缀的一律当作明文（兼容手工编辑过的配置文件）
#: 当前密文前缀：``enc2:`` = DPAPI + **附加熵**。
PROTECT_PREFIX = "enc2:"
#: 旧前缀（2026-11-13 之前）：只用 DPAPI，没有附加熵。
#:
#: 那种密文**同一 Windows 用户下的任何程序都能解开**（DPAPI 是按用户派生密钥的，
#: 不需要密码），只防住了「配置文件被拷到别的机器」。所以留它只为**能读懂老配置**，
#: 读出来之后马上会被 :meth:`Config.upgrade_quick_passwords` 重新加密成 ``enc2:``。
LEGACY_PROTECT_PREFIX = "enc1:"

#: 附加熵（optional entropy）。DPAPI 的 ``pOptionalEntropy`` 参与密钥派生：
#: 不带着这串字节就解不开，于是「别的程序拿到 config.json 也读不出密码」成立。
#: 这不是密码学意义上的秘密（常量就在 exe 里），但足以挡住顺手读配置的第三方工具。
_SECRET_ENTROPY = b"MacroSmith/quick_input/v2"

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


_dpapi_ready: Optional[tuple] = None
_dpapi_failed = False


def _dpapi():
    """懒加载 crypt32/kernel32 并显式声明签名（不声明会截断 64 位指针）。"""
    global _dpapi_ready, _dpapi_failed
    if _dpapi_ready is not None:
        return _dpapi_ready
    if _dpapi_failed or not IS_WINDOWS:
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_p = ctypes.POINTER(_DATA_BLOB)
        for name in ("CryptProtectData", "CryptUnprotectData"):
            func = getattr(crypt32, name)
            func.argtypes = [blob_p, wintypes.LPCWSTR, blob_p,
                             ctypes.c_void_p, ctypes.c_void_p,
                             wintypes.DWORD, blob_p]
            func.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        _dpapi_ready = (crypt32, kernel32)
        return _dpapi_ready
    except Exception:  # pragma: no cover - 依赖系统 API
        _dpapi_failed = True
        log.warning("DPAPI 不可用，密码将以明文保存", exc_info=True)
        return None


def _make_blob(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array]:
    """构造 ``DATA_BLOB``，同时返回底层缓冲区（必须持有到调用结束）。"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _take_blob(blob: _DATA_BLOB) -> bytes:
    """读走输出 blob 的内容并释放系统分配的缓冲区。"""
    libs = _dpapi()
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        if libs is not None:
            libs[1].LocalFree(blob.pbData)


def is_protected(value: object) -> bool:
    """该配置值是否已被加密（新旧两种前缀都算）。"""
    return isinstance(value, str) and value.startswith(
        (PROTECT_PREFIX, LEGACY_PROTECT_PREFIX))


def needs_upgrade(value: object) -> bool:
    """是否是「旧加密方式」的密文（无附加熵），需要重新加密。"""
    return isinstance(value, str) and value.startswith(LEGACY_PROTECT_PREFIX)


def protect(text: object) -> str:
    """加密一段文本，返回可直接存进 JSON 的字符串（``enc2:`` 前缀）。

    用 DPAPI + **附加熵**：没有 :data:`_SECRET_ENTROPY` 这串字节就解不开，
    所以即便别人读到 ``config.json`` 里的密文，也拿不到密码。

    失败（非 Windows、系统 API 异常）时原样返回明文——宁可少一层保护，
    也不能让用户存的密码变成一串乱码或干脆丢失。
    """
    raw = str(text or "")
    if not raw:
        return ""
    libs = _dpapi()
    if libs is None:
        return raw
    try:
        blob_in, keep = _make_blob(raw.encode("utf-8"))
        assert keep is not None      # 缓冲区必须活到 CryptProtectData 返回
        entropy, keep_entropy = _make_blob(_SECRET_ENTROPY)
        blob_out = _DATA_BLOB()
        ok = libs[0].CryptProtectData(
            ctypes.byref(blob_in), "MacroSmith", ctypes.byref(entropy), None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        del keep_entropy            # 只为拖到调用结束，别让 GC 提前回收
        if not ok:
            log.warning("CryptProtectData 失败（错误码 %s），改为明文保存",
                        ctypes.get_last_error())
            return raw
        data = _take_blob(blob_out)
        return PROTECT_PREFIX + base64.b64encode(data).decode("ascii")
    except Exception:  # pragma: no cover - 依赖系统 API
        log.warning("密码加密异常，改为明文保存", exc_info=True)
        return raw


def unprotect(value: object) -> str:
    """解密 :func:`protect` 的产物；本来就是明文则原样返回。

    两种前缀都认：

    * ``enc2:`` —— 当前方式，DPAPI + 附加熵，只有带着那串常量才解得开；
    * ``enc1:`` —— 2026-11-13 之前的旧密文，没有附加熵，仍然读得懂
      （否则老用户升级后会看到密码框全空），读出来之后由
      :meth:`Config.upgrade_quick_passwords` 重新加密。

    解不开（换了机器/用户、配置被手工改坏）返回空串并记日志：
    把密文当密码敲出去只会更糟。
    """
    text = str(value or "")
    if not is_protected(text):
        return text
    libs = _dpapi()
    if libs is None:
        return ""
    legacy = text.startswith(LEGACY_PROTECT_PREFIX)
    payload = text.split(":", 1)[1] if ":" in text else ""
    try:
        data = base64.b64decode(payload.encode("ascii"), validate=False)
        blob_in, keep = _make_blob(data)
        assert keep is not None      # 缓冲区必须活到 CryptUnprotectData 返回
        ent_blob = ent_keep = None
        if not legacy:
            ent_blob, ent_keep = _make_blob(_SECRET_ENTROPY)
        blob_out = _DATA_BLOB()
        ok = libs[0].CryptUnprotectData(
            ctypes.byref(blob_in), None,
            ctypes.byref(ent_blob) if ent_blob is not None else None,
            None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        del ent_keep                 # 只为拖到调用结束
        if not ok:
            log.warning("CryptUnprotectData 失败（可能换了机器或用户），"
                        "该密码需要重新设置")
            return ""
        return _take_blob(blob_out).decode("utf-8", "replace")
    except (ValueError, OSError, UnicodeError) as exc:
        # 配置被手工改坏 / 换了机器：只留一句话，不必刷一串栈
        log.warning("密码解密失败（%s），该密码需要重新设置", exc)
        return ""
    except Exception:  # pragma: no cover - 依赖系统 API
        log.warning("密码解密异常", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# 组合：按「快捷输入」的一次触发
# ---------------------------------------------------------------------------
def build_text(source: str, *, clipboard: Callable[[], str] = read_clipboard_text,
               password: str = "") -> str:
    """算出这次要敲的文本：``clipboard`` 取剪贴板，``password`` 用给定密码。"""
    if source == "clipboard":
        return clipboard()
    return str(password or "")


def describe_strokes(text: object) -> str:
    """给人看的一句话：``共 12 个字符（已丢弃 3 个非 ASCII 字符）``。"""
    raw = str(text or "")
    kept = sanitize(raw)
    dropped = len(raw) - len(kept)
    if dropped:
        return f"共 {len(kept)} 个字符（已丢弃 {dropped} 个非 ASCII 字符）"
    return f"共 {len(kept)} 个字符"


def strokes_preview(strokes: Sequence[tuple[int, bool]]) -> str:
    """调试用：把敲击序列渲染成 ``[S]A`` 这样的串（S 表示带 Shift）。"""
    return " ".join(f"{'[S]' if shift else ''}{vk:02X}" for vk, shift in strokes)
