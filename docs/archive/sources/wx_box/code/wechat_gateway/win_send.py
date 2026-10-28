# -*- coding: utf-8 -*-
"""wxauto 失败时的窗口级兜底（M1）。

**默认关闭，且默认只做"人工协助"**：把文本放进剪贴板并记一条审计，
由人切到目标聊天手动粘贴。原因是窗口级输入无法可靠校验"当前打开的是谁"，
盲发可能把消息发错人 —— 这比发送失败更糟。

显式开启 ``fallback_mode: "auto"`` 后才会模拟 Ctrl+V + Enter；即便如此，
仍需配置 ``fallback_input_box: [x, y]``（截图坐标的点，用于点击输入框），
并且要求微信窗口能切到前台；结果一律标记 ``verified: false``，请人工抽查。

不在本模块打开微信进程句柄、不注入、不 Hook（只用公开 Win32 输入 API）。
"""
from __future__ import annotations

import time
from typing import Any

VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D


def _load():
    """惰性导入 wx_core 的窗口/剪贴板工具；导入失败返回 (None, 错误)。"""
    try:
        from wx_core import clipboard, win  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    return (win, clipboard), ""


def _ctrl_v() -> None:
    import ctypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_V, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_V, 0, 2, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_CONTROL, 0, 2, 0)


def _result(ok: bool, status: str, *, error: str = "", warning: str = "") -> dict[str, Any]:
    return {"ok": bool(ok), "status": status, "method": "window",
            "verified": False, "error": error, "warning": warning}


def send_via_window(text: str, cfg: dict[str, Any], *,
                    settle: float = 0.4, timeout: float = 8.0) -> dict[str, Any]:
    """按配置走"人工协助"或"自动粘贴发送"；任何异常 fail-closed。"""
    value = str(text or "").strip()
    if not value:
        return _result(False, "failed", error="text 为空")
    if not cfg.get("fallback_enabled"):
        return _result(False, "disabled", error="fallback 未开启")
    loaded, err = _load()
    if loaded is None:
        return _result(False, "failed", error=f"wx_core 不可用：{err}")
    win, clipboard = loaded
    mode = str(cfg.get("fallback_mode") or "manual").strip().lower()

    if mode != "auto":
        # 人工协助：只放剪贴板 + 审计，不自动发送
        if not clipboard.set_text(value):
            return _result(False, "failed", error="写剪贴板失败")
        return _result(True, "clipboard",
                       warning="已复制到剪贴板：请切到目标聊天手动粘贴后发送")

    box = cfg.get("fallback_input_box") or []
    if not (isinstance(box, (list, tuple)) and len(box) == 2):
        return _result(False, "failed",
                       error="auto 模式必须配置 fallback_input_box: [x, y]（截图坐标）")
    try:
        info = win.find_wechat_window()
    except Exception as exc:  # noqa: BLE001
        return _result(False, "failed", error=f"{type(exc).__name__}: {exc}")
    if info is None:
        return _result(False, "failed", error="没找到微信窗口")
    try:
        if not win.activate(info):
            return _result(False, "failed", error="微信窗口无法切到前台")
        time.sleep(settle)
        if not win.is_foreground(info):
            return _result(False, "failed", error="前台不是微信窗口")
        win.post_click(info, int(box[0]), int(box[1]))
        time.sleep(settle)
        if not clipboard.set_text(value):
            return _result(False, "failed", error="写剪贴板失败")
        time.sleep(0.2)
        _ctrl_v()
        time.sleep(0.3)
        win.press_key(VK_RETURN)
        time.sleep(0.2)
    except Exception as exc:  # noqa: BLE001
        return _result(False, "failed", error=f"{type(exc).__name__}: {exc}")
    return _result(True, "sent",
                   warning="窗口级发送无法校验会话，请人工抽查是否发对")
