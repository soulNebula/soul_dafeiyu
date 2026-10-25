# -*- coding: utf-8 -*-
"""原生贴纸/收藏表情接口（P5-c 预留，默认关闭且安全）。

调研结论（详见 ``STICKER_NATIVE_RESEARCH.md``）：
- 微信 PC 表情面板是独立顶层窗口 ``mmui::XPopover``；
- **系统表情**（默认表情 tab）每格是 ``ExpressionCollectItem``，UIA Name=中文名，可按名点击；
- **收藏表情**（自定义表情 tab）UIA 只暴露一个「自定义表情」管理格，没有每个贴纸的
  Name/索引/图片路径 → 无法可靠枚举/按名发送；
- ``.wxgf`` 是微信专有格式，不能当文件/图片发成贴纸气泡。

因此本模块：
- ``available()`` 默认 **False**（``STICKER_NATIVE_ENABLED=False``，需显式开启 + backend 可用）；
- ``send_native()`` 默认**不点任何 UI**，直接返回 ``unsupported``；
- ``list_collect_stickers()`` 默认返回 ``[]``；
- 只有显式注入 backend（实验/测试）且 ChatInfo 与目标 contact 一致时才可能点击；
- 失败返回 ``fallback: "image"``，由上层回退 P5-a 的「贴纸当图片」。

安全约定：
1. 默认关闭，任何情况下不自动切换聊天、不向真实好友发消息；
2. 实验 backend 只支持「系统表情」按名点击，不支持收藏贴纸；
3. 所有异常都转成 dict，不抛出到网关主循环。
"""
from __future__ import annotations

import os
import time
from typing import Any

#: 配置开关（默认关）；也可用环境变量 ``WX_STICKER_NATIVE=1`` 临时打开
STICKER_NATIVE_ENABLED = False
ENV_FLAG = "WX_STICKER_NATIVE"

#: 实验 backend（测试可注入 fake；生产默认 None → wxauto4 backend，且只在 enabled 时懒加载）
_BACKEND: Any = None


class EmojiNotFound(RuntimeError):
    pass


class ContactMismatch(RuntimeError):
    pass


class BackendUnavailable(RuntimeError):
    pass


def enabled() -> bool:
    """配置开关：常量或环境变量任一开启。"""
    if STICKER_NATIVE_ENABLED:
        return True
    return os.environ.get(ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def set_backend(backend: Any) -> None:
    """注入/覆盖 backend（测试用；生产保持 None 走 wxauto4）。"""
    global _BACKEND
    _BACKEND = backend


def _default_backend() -> Any:
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    _BACKEND = _Wxauto4Backend()
    return _BACKEND


class _Wxauto4Backend:
    """实验性 wxauto4 backend：只支持系统表情按名点击；收藏贴纸不实现。"""

    def __init__(self) -> None:
        self._wx = None
        self._uia = None

    def available(self) -> bool:
        try:
            import wxauto4  # noqa: F401
            import wxauto4.uia as uia  # noqa: F401
        except Exception:  # noqa: BLE001 - 未安装/版本不匹配
            return False
        return True

    def _ensure(self) -> None:
        if self._wx is None:
            import wxauto4
            import wxauto4.uia as uia
            self._wx = wxauto4.WeChat(ads=False)
            self._uia = uia

    def current_chat(self) -> str:
        self._ensure()
        info = self._wx.ChatInfo()
        return str(info.get("chat_name") or "")

    def _find_all(self, ctrl, pred, out=None):
        out = out if out is not None else []
        try:
            if pred(ctrl):
                out.append(ctrl)
            for child in ctrl.GetChildren():
                self._find_all(child, pred, out)
        except Exception:  # noqa: BLE001
            pass
        return out

    def _popover(self):
        root = self._uia.GetRootControl()
        for window in root.GetChildren():
            if window.ClassName == "mmui::XPopover":
                return window
        return None

    def open_emoji_panel(self) -> None:
        self._ensure()
        chatbox = self._wx.ChatBox
        buttons = self._find_all(
            chatbox.control,
            lambda c: c.ControlTypeName == "ButtonControl" and c.Name == "发送表情(Alt+E)")
        if not buttons:
            raise BackendUnavailable("emoji_button_not_found")
        buttons[0].Click()
        for _ in range(10):
            if self._popover() is not None:
                return
            time.sleep(0.1)
        raise BackendUnavailable("emoji_panel_not_opened")

    def click_system_emoji(self, name: str) -> None:
        pop = self._popover()
        if pop is None:
            raise BackendUnavailable("emoji_panel_missing")
        tabs = self._find_all(
            pop, lambda c: c.ControlTypeName == "TabItemControl" and c.Name == "默认表情")
        if tabs:
            tabs[0].Click()
            time.sleep(0.4)
        pop = self._popover()
        cells = self._find_all(
            pop, lambda c: c.ControlTypeName == "TextControl" and c.Name == name)
        if not cells:
            raise EmojiNotFound(name)
        cells[0].Click()
        time.sleep(0.2)

    def send(self) -> None:
        self._ensure()
        chatbox = self._wx.ChatBox
        buttons = self._find_all(
            chatbox.control,
            lambda c: c.ControlTypeName == "ButtonControl" and c.Name == "发送")
        if not buttons:
            raise BackendUnavailable("send_button_not_found")
        buttons[0].Click()

    def close(self) -> None:
        try:
            self._uia.SendKeys("{ESC}")
        except Exception:  # noqa: BLE001
            pass

    def list_collect_stickers(self) -> list[dict]:
        # 调研结论：自定义表情 tab 不暴露每个贴纸的 Name/索引，无法枚举
        return []


def available(backend: Any = None) -> bool:
    """当前环境是否可用原生贴纸（默认 False）。"""
    if not enabled():
        return False
    candidate = backend if backend is not None else _default_backend()
    try:
        return bool(candidate and candidate.available())
    except Exception:  # noqa: BLE001
        return False


def list_collect_stickers(backend: Any = None) -> list[dict]:
    """枚举收藏贴纸（调研结论：当前不可枚举 → []）。"""
    if not enabled():
        return []
    candidate = backend if backend is not None else _default_backend()
    try:
        if candidate and candidate.available():
            return list(candidate.list_collect_stickers())
    except Exception:  # noqa: BLE001
        return []
    return []


def send_native(contact: str, name: str, *, backend: Any = None) -> dict:
    """尝试发送原生系统表情（默认安全 unsupported，不点 UI）。

    注意：收藏贴纸不支持；成功只代表系统表情按名点击成功。
    """
    if not enabled():
        return {"ok": False, "status": "unsupported",
                "error": "sticker_native_disabled", "fallback": "image"}
    candidate = backend if backend is not None else _default_backend()
    try:
        if not candidate or not candidate.available():
            return {"ok": False, "status": "unsupported",
                    "error": "backend_unavailable", "fallback": "image"}
        current = str(candidate.current_chat() or "")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "failed", "error": f"chat_check_failed:{exc}",
                "fallback": "image"}
    if current != str(contact):
        return {"ok": False, "status": "failed", "error": "contact_mismatch",
                "current_chat": current, "fallback": "image"}
    try:
        candidate.open_emoji_panel()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "failed", "error": f"open_panel_failed:{exc}",
                "fallback": "image"}
    try:
        candidate.click_system_emoji(str(name))
    except Exception as exc:  # noqa: BLE001
        try:
            candidate.close()
        except Exception:  # noqa: BLE001
            pass
        status = "failed"
        error = "emoji_not_found" if isinstance(exc, EmojiNotFound) else f"click_failed:{exc}"
        return {"ok": False, "status": status, "error": error, "fallback": "image"}
    try:
        candidate.send()
    except Exception as exc:  # noqa: BLE001
        try:
            candidate.close()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "status": "failed", "error": f"send_failed:{exc}",
                "fallback": "image"}
    try:
        candidate.close()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "status": "sent", "contact": str(contact), "name": str(name),
            "kind": "system_emoji", "sticker_bubble": False}
