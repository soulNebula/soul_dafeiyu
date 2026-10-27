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

def send_to_contact(contact: str, text: str, cfg: dict[str, Any], *,
                    settle: float = 0.5) -> dict[str, Any]:
    """窗口级"按联系人发送"（wxauto4/UIA 失效时的兜底）。

    步骤：微信置前 → 点搜索框 → 粘贴联系人名 → 回车打开会话 → 点输入框 →
    粘贴正文 → 回车发送。坐标按窗口尺寸比例计算（可用配置覆盖），
    **无法校验会话名**，结果一律 ``verified=False``。
    """
    value = str(text or "").strip()
    who = str(contact or "").strip()
    if not value or not who:
        return _result(False, "failed", error="contact/text 为空")
    loaded, err = _load()
    if loaded is None:
        return _result(False, "failed", error=f"wx_core 不可用：{err}")
    win, clipboard = loaded
    try:
        info = win.find_wechat_window()
    except Exception as exc:  # noqa: BLE001
        return _result(False, "failed", error=f"{type(exc).__name__}: {exc}")
    if info is None:
        return _result(False, "failed", error="没找到微信窗口")
    prev_fg = None
    try:
        import ctypes as _ct
        prev_fg = _ct.windll.user32.GetForegroundWindow()
    except Exception:  # noqa: BLE001
        prev_fg = None
    try:
        activated = False
        for _ in range(3):
            if win.bring_to_front(info) or win.is_foreground(info):
                time.sleep(settle)
                if win.is_foreground(info):
                    activated = True
                    break
            time.sleep(0.4)
        if not activated:
            return _result(False, "failed", error="前台不是微信窗口（切换到前台失败）")
        img = win.capture(info)
        w, h = (img.size if img else (960, 900))
        # ① 搜索框（比例坐标，默认 0.17w / 0.066h）
        sb = cfg.get("window_search_box") or [0.17, 0.066]
        win.post_click(info, int(w * float(sb[0])), int(h * float(sb[1])))
        time.sleep(0.3)
        if not clipboard.set_text(who):
            return _result(False, "failed", error="写剪贴板失败")
        time.sleep(0.15)
        _ctrl_v()
        time.sleep(0.5)
        win.press_key(VK_RETURN)
        time.sleep(1.0)
        # ② 输入框（比例坐标，默认 0.68w / 0.93h）
        ib = cfg.get("fallback_input_box") or [0.68, 0.93]
        try:
            win.real_click(info, int(w * float(ib[0])), int(h * float(ib[1])))
        except Exception:  # noqa: BLE001
            win.post_click(info, int(w * float(ib[0])), int(h * float(ib[1])))
        time.sleep(0.4)
        if not clipboard.set_text(value):
            return _result(False, "failed", error="写剪贴板失败")
        time.sleep(0.15)
        _ctrl_v()
        time.sleep(0.5)
        win.press_key(VK_RETURN)
        time.sleep(0.6)
    except Exception as exc:  # noqa: BLE001
        return _result(False, "failed", error=f"{type(exc).__name__}: {exc}")
    try:
        import ctypes as _ct
        if prev_fg:
            _ct.windll.user32.SetForegroundWindow(prev_fg)
    except Exception:  # noqa: BLE001
        pass
    return _result(True, "sent",
                   warning="窗口级发送（搜索打开会话）无法校验会话，请人工抽查")

def _copy_image_to_clipboard(path: str) -> bool:
    """把图片文件放进 Windows 剪贴板（CF_DIB），供微信输入框 Ctrl+V 粘贴发送。"""
    try:
        from PIL import Image
        import io
        import win32clipboard
        import win32con
        img = Image.open(path)
        if img.mode not in ("RGB",):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "BMP")
        data = buf.getvalue()[14:]  # 去掉 BMP 文件头，留 DIB
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_DIB, data)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False



def _verify_chat_open(win, info, contact: str) -> str:
    """OCR 校验当前会话标题是否包含 contact。

    返回 ``"ok"`` / ``"mismatch"`` / ``"unknown"``（OCR 不可用或识别为空）。
    """
    try:
        img = win.capture(info)
        if img is None:
            return "unknown"
        w, h = img.size
        from wx_core.ocr import OCRWorker
        with OCRWorker() as worker:
            lines = worker.ocr(img)
        # 只取窗口顶部 12% 且 x>28% 的文本 = 聊天标题区
        parts = []
        for ln in lines:
            box = getattr(ln, "box", None) or (0, 0, 0, 0)
            if box[1] < h * 0.12 and box[0] >= w * 0.28:
                parts.append(str(getattr(ln, "text", "") or ""))
        text = "".join(parts).replace(" ", "")
        target = str(contact).replace(" ", "")
        if not text:
            return "unknown"
        return "ok" if target and target in text else "mismatch"
    except Exception:  # noqa: BLE001
        return "unknown"


def _ocr_lines(win, info):
    from wx_core.ocr import OCRWorker
    img = win.capture(info)
    if img is None:
        return [], (0, 0)
    with OCRWorker() as worker:
        lines = worker.ocr(img)
    return lines, img.size


def _find_contact_in_list(win, info, contact: str):
    """在左侧会话列表里 OCR 定位联系人名，返回点击坐标（画面坐标）或 None。"""
    lines, (w, h) = _ocr_lines(win, info)
    target = str(contact).replace(" ", "")
    for ln in lines:
        box = getattr(ln, "box", None) or (0, 0, 0, 0)
        text = str(getattr(ln, "text", "") or "").replace(" ", "")
        if not text or target not in text:
            continue
        # 左侧会话列表：x 在 10%~33%，y 在 8%~92%
        if box[0] < w * 0.10 or box[2] > w * 0.34:
            continue
        if box[1] < h * 0.08 or box[1] > h * 0.92:
            continue
        cx = (box[0] + box[2]) // 2
        cy = (box[1] + box[3]) // 2
        return cx, cy
    return None

def _open_chat(win, info, contact: str, cfg: dict, settle: float = 0.5) -> tuple[bool, str]:
    """把微信切前台并打开 ``contact`` 会话（搜索框 → 粘贴 → 回车）。"""
    loaded, _err = _load()
    if loaded is None:
        return False, f"wx_core 不可用：{_err}"
    _win_mod, clipboard = loaded
    if not win.bring_to_front(info) and not win.is_foreground(info):
        return False, "前台不是微信窗口（切换到前台失败）"
    time.sleep(settle)
    if not win.is_foreground(info):
        return False, "前台不是微信窗口"
    img = win.capture(info)
    w, h = (img.size if img else (960, 900))
    # 优先：直接在左侧会话列表里点联系人（比搜索框可靠，且不依赖聚焦）
    spot = _find_contact_in_list(win, info, contact)
    if spot:
        win.real_click(info, int(spot[0]), int(spot[1]))
        time.sleep(1.2)
        state = _verify_chat_open(win, info, contact)
        if state != "ok":
            time.sleep(0.8)
            state = _verify_chat_open(win, info, contact)
        if state == "ok":
            return True, ""
        # 点击没打开正确会话 → 继续走搜索兜底
    sb = cfg.get("window_search_box") or [0.17, 0.066]
    try:
        win.real_click(info, int(w * float(sb[0])), int(h * float(sb[1])))
    except Exception:  # noqa: BLE001
        win.post_click(info, int(w * float(sb[0])), int(h * float(sb[1])))
    time.sleep(0.4)
    if not clipboard.set_text(str(contact)):
        return False, "写剪贴板失败"
    time.sleep(0.15)
    _ctrl_v()
    time.sleep(0.5)
    win.press_key(VK_RETURN)
    time.sleep(1.0)
    # 校验会话标题，防止搜索失败把消息发到别的聊天
    for _attempt in range(2):
        state = _verify_chat_open(win, info, contact)
        if state == "ok":
            return True, ""
        if state == "unknown":
            return True, ""   # OCR 不可用：保持原行为（结果仍标 verified=false）
        # mismatch：重试一次搜索
        try:
            win.real_click(info, int(w * float(sb[0])), int(h * float(sb[1])))
        except Exception:  # noqa: BLE001
            win.post_click(info, int(w * float(sb[0])), int(h * float(sb[1])))
        time.sleep(0.4)
        if not clipboard.set_text(str(contact)):
            break
        time.sleep(0.15)
        _ctrl_v()
        time.sleep(0.5)
        win.press_key(VK_RETURN)
        time.sleep(1.0)
    state = _verify_chat_open(win, info, contact)
    if state == "unknown":
        time.sleep(0.8)
        state = _verify_chat_open(win, info, contact)
    if state != "ok":
        return False, "当前会话与联系人名不匹配或无法确认（已中止发送）"
    return True, ""


def send_media_via_window(contact: str, item: dict, cfg: dict, *,
                          settle: float = 0.5) -> dict[str, Any]:
    """窗口级发送单个媒体气泡（image/gif/sticker 走剪贴板图片，文本走粘贴文字）。"""
    kind = str(item.get("type") or "").strip().lower()
    path = str(item.get("path") or "")
    text = str(item.get("text") or "")
    if kind in ("text", "emoji_text"):
        out = send_to_contact(contact, text, cfg, settle=settle)
        out["type"] = kind
        return out
    if not path:
        return {"ok": False, "status": "failed", "type": kind, "verified": False,
                "error": "缺少媒体文件路径"}
    loaded, err = _load()
    if loaded is None:
        return {"ok": False, "status": "failed", "type": kind, "error": f"wx_core 不可用：{err}"}
    win, _clip = loaded
    info = win.find_wechat_window()
    if info is None:
        return {"ok": False, "status": "failed", "type": kind, "error": "没找到微信窗口"}
    try:
        ok, why = _open_chat(win, info, contact, cfg, settle)
        if not ok:
            return {"ok": False, "status": "failed", "type": kind, "error": why}
        img = win.capture(info)
        w, h = (img.size if img else (960, 900))
        ib = cfg.get("fallback_input_box") or [0.68, 0.93]
        try:
            win.real_click(info, int(w * float(ib[0])), int(h * float(ib[1])))
        except Exception:  # noqa: BLE001
            win.post_click(info, int(w * float(ib[0])), int(h * float(ib[1])))
        time.sleep(0.4)
        if not _copy_image_to_clipboard(path):
            return {"ok": False, "status": "failed", "type": kind,
                    "error": "图片复制到剪贴板失败（PIL/pywin32 不可用或格式不支持）"}
        time.sleep(0.2)
        _ctrl_v()
        time.sleep(0.8)
        win.press_key(VK_RETURN)
        time.sleep(0.8)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "failed", "type": kind,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "status": "sent", "type": kind, "verified": False,
            "warning": "窗口级图片发送未校验会话，请人工抽查"}
