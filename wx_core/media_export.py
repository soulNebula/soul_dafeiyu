"""用微信自带「另存为」导出原图 / 原 GIF（合规路线，不碰加密、不读内存）。

原理
----
微信聊天窗口里的图片/表情包，右键菜单自带「另存为...」，弹出的标准保存对话框点「保存」
就会由**微信自己**把原始文件解密写到磁盘——我们只是帮忙点这几下：

    投递右键(窗口消息) → 菜单窗(Qt51514QWindowToolSaveBits) 截图+OCR 找「另存为」
    → 投递左键点它 → 「另存为」对话框(#32770)点「保存」按钮(BM_CLICK)
    → 到默认保存目录找最新文件 → 挪到项目 media/originals/

全程使用 **PostMessage / BM_CLICK**（不需要抢前台、不动真实鼠标），
所以可以在后台监听线程里自动跑，只在真的导出时才短暂动一下微信界面。

限制：图片/GIF 多时每条要 2~4 秒；微信升级后菜单/对话框结构可能变。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

from . import win as W

W_ = W.user32
W_.EnumChildWindows.argtypes = [wintypes.HWND, W.WNDENUMPROC, wintypes.LPARAM]
W_.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
W_.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
MENU_HINTS = ("另存为", "保存图片", "保存")
DIALOG_TITLES = ("另存为", "Save As", "另存为...")


def _enum_windows(pred) -> list[int]:
    out: list[int] = []

    @W.WNDENUMPROC
    def cb(h, l):
        try:
            if pred(h):
                out.append(h)
        except Exception:
            pass
        return True

    W_.EnumWindows(cb, 0)
    return out


def _children(hwnd: int) -> list[int]:
    out: list[int] = []

    @W.WNDENUMPROC
    def cb(h, l):
        out.append(h)
        return True

    W_.EnumChildWindows(hwnd, cb, 0)
    return out


def _post_at(hwnd: int, x: int, y: int, left: bool = True) -> None:
    lp = W._make_lparam(x, y)
    if left:
        down, up, flag = 0x0201, 0x0202, 1
    else:
        down, up, flag = 0x0204, 0x0205, 2
    W_.PostMessageW(hwnd, 0x0200, 0, lp)
    time.sleep(0.02)
    W_.PostMessageW(hwnd, down, flag, lp)
    time.sleep(0.04)
    W_.PostMessageW(hwnd, up, 0, lp)


def _menu_windows() -> list[int]:
    got = []
    for h in _enum_windows(lambda h: W_.IsWindowVisible(h)):
        cls = W._class_name(h)
        if "ToolSaveBits" in cls or cls.startswith("#32768") or "Menu" in cls:
            got.append(h)
    return got


def _save_dialogs() -> list[int]:
    return _enum_windows(lambda h: W_.IsWindowVisible(h) and W._class_name(h) == "#32770"
                         and any(t in W._window_title(h) for t in DIALOG_TITLES))


def _watch_dirs() -> list[Path]:
    home = Path.home()
    return [home / "Desktop", home / "Downloads", home / "Pictures", home / "Documents"]


def _snapshot() -> dict[str, tuple[float, int]]:
    got: dict[str, tuple[float, int]] = {}
    for d in _watch_dirs():
        try:
            for f in os.scandir(d):
                if os.path.isfile(f.path) and f.name.lower().endswith(IMAGE_EXTS):
                    try:
                        st = f.stat()
                        got[f.path] = (st.st_mtime, st.st_size)
                    except OSError:
                        continue
        except Exception:
            continue
    return got


def _close_popups() -> None:
    """清理可能残留的菜单/对话框（Esc）。"""
    for h in _menu_windows() + _save_dialogs():
        try:
            W_.PostMessageW(h, 0x0100, 0x1B, 0)
            W_.PostMessageW(h, 0x0101, 0x1B, 1)
        except Exception:
            continue


def _click_save_button(dialog: int) -> bool:
    for k in _children(dialog):
        if W._class_name(k) == "Button":
            text = W._window_title(k).replace("&", "")
            if text.startswith("保存") or text in ("Save", "保存(S)"):
                W_.SendMessageW(k, 0x00F5, 0, 0)  # BM_CLICK
                return True
    # 退路：IDOK
    W_.PostMessageW(dialog, 0x0111, 1, 0)
    return True


def _handle_overwrite() -> None:
    for d in _enum_windows(lambda h: W_.IsWindowVisible(h) and W._class_name(h) == "#32770"):
        if any(k in W._window_title(d) for k in ("确认", "替换", "覆盖", "已存在")):
            for k in _children(d):
                if W._class_name(k) == "Button" and W._window_title(k).replace("&", "").startswith(("是", "Yes")):
                    W_.SendMessageW(k, 0x00F5, 0, 0)
                    return
            W_.PostMessageW(d, 0x0111, 6, 0)  # IDYES


def copy_message_text(win, block, ocr, timeout: float = 8.0) -> str:
    """右键该消息 → 菜单「复制」→ 读剪贴板，拿到**精确原文**（含 [微笑] 这类表情名）。

    读完会把用户原来的剪贴板内容还原（尽量不打扰用户）。
    """
    from . import clipboard

    if win is None or not getattr(win, "alive", False):
        return ""
    box = block.box
    cx = (box[0] + box[2]) // 2
    cy = (box[1] + box[3]) // 2
    ox = win.client_rect[0] - win.window_rect[0]
    oy = win.client_rect[1] - win.window_rect[1]
    old_clip = clipboard.get_text()
    try:
        _post_at(win.hwnd, cx - ox, cy - oy, left=False)
        deadline = time.time() + timeout
        menus: list[int] = []
        while time.time() < deadline:
            time.sleep(0.2)
            menus = _menu_windows()
            if menus:
                break
        if not menus:
            return ""
        pop = menus[0]
        pr = W._rect(pop)
        shot = W._grab_window(pop, pr[2] - pr[0], pr[3] - pr[1], W.PW_RENDERFULLCONTENT)
        if shot is None:
            return ""
        lines = ocr.ocr(shot)
        target = None
        for ln in lines:
            t = ln.text.replace(" ", "")
            if t in ("复制", "Copy") or t.startswith("复制"):
                target = ln
                break
        if target is None:
            return ""
        mx = (target.box[0] + target.box[2]) // 2
        my = (target.box[1] + target.box[3]) // 2
        _post_at(pop, mx, my, left=True)
        time.sleep(0.5)
        text = clipboard.get_text()
        if old_clip and old_clip != text:
            clipboard.set_text(old_clip)
        return text
    finally:
        _close_popups()


def export_block(win, block, ocr, dest_dir: Path, timeout: float = 12.0) -> Path | None:
    """用微信「另存为」导出这个图片/表情包块的原始文件，返回落盘后的路径。"""
    if win is None or not getattr(win, "alive", False):
        return None
    box = block.box
    cx = (box[0] + box[2]) // 2
    cy = (box[1] + box[3]) // 2
    ox = win.client_rect[0] - win.window_rect[0]
    oy = win.client_rect[1] - win.window_rect[1]
    before = _snapshot()
    try:
        _post_at(win.hwnd, cx - ox, cy - oy, left=False)
        deadline = time.time() + timeout
        menus: list[int] = []
        while time.time() < deadline:
            time.sleep(0.25)
            menus = _menu_windows()
            if menus:
                break
        if not menus:
            return None
        pop = menus[0]
        pr = W._rect(pop)
        shot = W._grab_window(pop, pr[2] - pr[0], pr[3] - pr[1], W.PW_RENDERFULLCONTENT)
        if shot is None:
            _close_popups()
            return None
        lines = ocr.ocr(shot)
        target = None
        for ln in lines:
            t = ln.text.replace(" ", "")
            if any(hint in t for hint in MENU_HINTS):
                target = ln
                break
        if target is None:
            _close_popups()
            return None
        mx = (target.box[0] + target.box[2]) // 2
        my = (target.box[1] + target.box[3]) // 2
        _post_at(pop, mx, my, left=True)
        # 等保存对话框
        dialogs: list[int] = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.25)
            dialogs = _save_dialogs()
            if dialogs:
                break
        if not dialogs:
            _close_popups()
            return None
        _click_save_button(dialogs[0])
        time.sleep(1.2)
        _handle_overwrite()
        # 找新文件（保存对话框默认目录 = 上次使用目录）
        deadline = time.time() + timeout
        new_path: Path | None = None
        while time.time() < deadline:
            time.sleep(0.4)
            after = _snapshot()
            fresh = [(p, v) for p, v in after.items() if p not in before or after[p] != before[p]]
            if fresh:
                fresh.sort(key=lambda x: x[1][0], reverse=True)
                new_path = Path(fresh[0][0])
                break
        if new_path is None:
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        dst = dest_dir / new_path.name
        i = 1
        while dst.exists():
            dst = dest_dir / f"{new_path.stem}_{i}{new_path.suffix}"
            i += 1
        try:
            new_path.replace(dst)
        except Exception:
            try:
                import shutil

                shutil.copy2(new_path, dst)
                new_path.unlink(missing_ok=True)
            except Exception:
                return new_path
        return dst if dst.exists() else new_path
    finally:
        _close_popups()
