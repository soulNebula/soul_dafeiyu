# -*- coding: utf-8 -*-
"""基于系统 .NET UIA 的发送后端（wxauto4 的 Python UIA 失效时的替代方案）。

思路
----
wxauto4 内置的 comtypes UIA 在本机枚举窗口树返回空，但 **系统 .NET UIA
（PowerShell `System.Windows.Automation`）完全正常**。这里用 PowerShell 脚本
（`tools/wechat_uia/query.ps1`）只读查询：
  * 微信主窗口（`mmui::MainWindow`）
  * 左侧会话列表里目标联系人（`mmui::ChatSessionCell`）的屏幕矩形
  * 当前聊天标题（`mmui::ChatTitleBarChatSingleView`）
  * 输入框（最靠下的 Edit 控件）屏幕矩形

Python 侧再用真实鼠标点击矩形中心、剪贴板粘贴、回车发送；每一步都校验，
**标题与联系人不一致就中止，绝不盲发**。

默认关闭：`fallback_mode="auto"` + `fallback_send_mode="uia"` 才会被 server 调用。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "wechat_uia" / "query.ps1"
POWERSHELL = "powershell.exe"


def available() -> bool:
    return SCRIPT.is_file()


def _query(*args: str, timeout: float = 20.0) -> dict:
    if not SCRIPT.is_file():
        return {"ok": False, "error": "script_missing"}
    cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
           str(SCRIPT), *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out = (p.stdout or "").strip()
    if not out:
        return {"ok": False, "error": (p.stderr or "no_output")[-300:]}
    line = out.splitlines()[-1]
    try:
        return json.loads(line)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"bad_json: {exc}", "raw": line[:300]}


def find_session(contact: str) -> dict:
    return _query("-Contact", str(contact))


def chat_title() -> str:
    r = _query("-Title")
    return str(r.get("title") or "")


def find_input() -> dict:
    r = _query("-FindInput")
    return (r.get("input") or {}) if r.get("ok") else {}


def _rect_center(rect: dict) -> tuple[int, int]:
    return (int(rect.get("x", 0)) + int(rect.get("w", 0)) // 2,
            int(rect.get("y", 0)) + int(rect.get("h", 0)) // 2)


def _activate_window() -> bool:
    """把微信窗口切到前台（复用 wx_core.win）。"""
    try:
        from wx_core import win
        info = win.find_wechat_window()
        if info is None:
            return False
        return bool(win.bring_to_front(info)) or bool(win.is_foreground(info))
    except Exception:  # noqa: BLE001
        return False


def _click_screen(x: int, y: int) -> bool:
    try:
        import ctypes
        u = ctypes.windll.user32
        u.SetCursorPos(int(x), int(y))
        time.sleep(0.05)
        u.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.05)
        u.mouse_event(0x0004, 0, 0, 0, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def _paste_and_enter(text: str) -> bool:
    try:
        from wx_core import clipboard
        import ctypes
        if not clipboard.set_text(str(text)):
            return False
        time.sleep(0.15)
        u = ctypes.windll.user32
        u.keybd_event(0x11, 0, 0, 0)   # Ctrl
        time.sleep(0.05)
        u.keybd_event(0x56, 0, 0, 0)   # V
        time.sleep(0.05)
        u.keybd_event(0x56, 0, 2, 0)
        time.sleep(0.05)
        u.keybd_event(0x11, 0, 2, 0)
        time.sleep(0.4)
        u.keybd_event(0x0D, 0, 0, 0)   # Enter
        time.sleep(0.05)
        u.keybd_event(0x0D, 0, 2, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def open_contact(contact: str, *, retries: int = 2) -> tuple[bool, str]:
    """点击左侧会话列表打开联系人会话，并校验标题。"""
    contact = str(contact or "").strip()
    if not contact:
        return False, "empty_contact"
    if not _activate_window():
        return False, "activate_failed"
    time.sleep(0.6)
    for _ in range(max(1, retries)):
        r = find_session(contact)
        if not r.get("ok"):
            return False, str(r.get("error") or "uia_query_failed")
        c = r.get("contact") or {}
        if not c.get("found"):
            return False, "contact_not_in_session_list"
        x, y = _rect_center(c.get("rect") or {})
        if not _click_screen(x, y):
            return False, "click_failed"
        # 点击后标题可能需要 1~4s 才刷新：轮询等待
        last = ""
        for _ in range(10):
            time.sleep(0.5)
            last = chat_title().strip()
            if last == contact:
                return True, ""
            if last and last != contact:
                break   # 明确切到了别人，重试搜索/点击
        time.sleep(0.3)
    return False, f"title_mismatch:{last}"


def send_to_contact(contact: str, text: str, cfg: dict | None = None, *,
                    settle: float = 0.0) -> dict:
    """按联系人发文字：UIA 定位会话 → 校验标题 → 点输入框 → 粘贴 → 回车。"""
    value = str(text or "").strip()
    if not value:
        return {"ok": False, "status": "failed", "method": "uia", "error": "empty_text"}
    ok, why = open_contact(contact)
    if not ok:
        return {"ok": False, "status": "failed", "method": "uia",
                "error": f"open_contact: {why}", "verified": False}
    if settle:
        time.sleep(settle)
    inp = find_input()
    if not inp.get("found"):
        return {"ok": False, "status": "failed", "method": "uia",
                "error": "input_not_found", "verified": False}
    x, y = _rect_center(inp.get("rect") or {})
    if not _click_screen(x, y):
        return {"ok": False, "status": "failed", "method": "uia",
                "error": "input_click_failed", "verified": False}
    time.sleep(0.3)
    if not _paste_and_enter(value):
        return {"ok": False, "status": "failed", "method": "uia",
                "error": "paste_failed", "verified": False}
    return {"ok": True, "status": "sent", "method": "uia", "verified": True,
            "warning": "UIA 定位+标题校验通过后发送"}
