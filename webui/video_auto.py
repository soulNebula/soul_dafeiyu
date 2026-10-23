"""实验性：聊天视频自动缓存（UI 自动化，默认关闭）。

聊天视频在数据库里没有可直接下载的 CDN URL，只能让微信客户端自己下载。这里在用户
显式开启实验功能后，对「新收到且本机未缓存」的视频消息执行一次：

1. 微信窗口切到前台，搜索并打开对应好友/群聊；
2. 滚到聊天底部，用 OCR 找到视频缩略图上的时长文字（如 ``00:12``）并点击；
3. 等微信下载/播放几秒后按 Esc 关闭播放器，恢复原前台窗口；
4. 网页随后可通过 ``/api/video`` 播放缓存文件。

只点击视频缩略图，不点赞、不评论、不发消息；每条消息只尝试一次；微信最小化或找不到
视频时长文字时放弃。UI 自动化可能点偏，默认关闭，失败只记日志。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

from wx_core import extract, layout as L, win as W
from wx_core.ocr import OCRWorker

VK_CONTROL = 0x11
VK_A = 0x41
VK_BACK = 0x08
_DURATION_RE = re.compile(r"^\d{1,2}:\d{2}$")


class ChatVideoAutoCache:
    def __init__(self, bridge: Any, videos: Any, *, log: Callable[[str], None] | None = None):
        self._bridge = bridge
        self._videos = videos
        self._log = log or (lambda *_a, **_k: None)
        self._lock = threading.Lock()
        self._enabled = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._attempted: set[str] = set()
        self._last_attempt = 0.0
        self._ocr = None
        self._last_result = ""
        self._last_error = ""

    # ------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "attempted": len(self._attempted),
                "last_result": self._last_result,
                "last_error": self._last_error,
            }

    def set_enabled(self, enabled: bool) -> dict:
        enabled = bool(enabled)
        with self._lock:
            self._enabled = enabled
        if enabled:
            self._stop.clear()
            if self._thread is None or not self._thread.is_alive():
                thread = threading.Thread(target=self._loop, name="wxbox-chat-video-auto", daemon=True)
                self._thread = thread
                thread.start()
            self._log("已开启实验性聊天视频自动缓存（会切微信窗口点视频）")
        else:
            self._stop.set()
            self._thread = None
            self._log("已关闭实验性聊天视频自动缓存")
        self._broadcast()
        return self.status()

    def stop(self) -> None:
        self._stop.set()
        self._enabled = False
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    def _broadcast(self) -> None:
        broadcast = getattr(self._bridge, "broadcast", None)
        if callable(broadcast):
            try:
                broadcast({"type": "video_auto_status", "data": self.status()})
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.wait(5.0):
            with self._lock:
                if not self._enabled:
                    continue
            try:
                self._scan_once()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._log(f"实验性聊天视频缓存异常：{exc}")

    def _scan_once(self) -> None:
        now = time.time()
        if now - self._last_attempt < 60:
            return
        snapshot = getattr(self._bridge, "snapshot", None)
        if not callable(snapshot):
            return
        try:
            messages = list((snapshot() or {}).get("messages") or [])
        except Exception:  # noqa: BLE001
            return
        for message in reversed(messages[-200:]):
            if not isinstance(message, dict):
                continue
            if message.get("kind") != "video":
                continue
            extra = message.get("extra") or {}
            local_id = extra.get("db_local_id")
            friend = str(message.get("friend") or "").strip()
            if not local_id or not friend:
                continue
            key = f"{friend}|{local_id}"
            with self._lock:
                if key in self._attempted:
                    continue
            try:
                if self._videos.resolve(friend, int(local_id)) is not None:
                    with self._lock:
                        self._attempted.add(key)
                    continue
            except Exception:  # noqa: BLE001
                pass
            if now - (float(message.get("ts") or 0)) > 600:
                continue
            with self._lock:
                self._attempted.add(key)
                self._last_attempt = time.time()
            self._cache_one(friend, int(local_id))
            return

    # ------------------------------------------------------------------
    def _cache_one(self, friend: str, local_id: int) -> None:
        win = W.find_wechat_window()
        if win is None:
            self._last_error = "未找到微信窗口"
            self._broadcast()
            return
        if win.minimized:
            self._last_error = "微信窗口最小化"
            self._broadcast()
            return
        prev_fg = W.user32.GetForegroundWindow()
        try:
            if not W.activate(win) and not W.bring_to_front(win):
                self._last_error = "无法激活微信窗口"
                return
            time.sleep(0.35)
            img = W.capture(win)
            if img is None:
                self._last_error = "无法截取微信窗口"
                return
            lay = L.analyze(img, win)
            ocr = self._ensure_ocr()
            if ocr is None:
                self._last_error = "OCR 不可用"
                return
            if not self._switch_to(win, lay, ocr, img, friend):
                self._last_error = f"未能切换到聊天：{friend}"
                return
            time.sleep(0.5)
            self._scroll_bottom(win, lay)
            time.sleep(0.5)
            img = W.capture(win)
            if img is None:
                return
            box = self._find_video_duration(img, lay, ocr)
            if box is None:
                self._last_error = "聊天里没找到视频时长文字，跳过"
                return
            x = (box[0] + box[2]) // 2
            y = (box[1] + box[3]) // 2
            W.real_click(win, x, y)
            time.sleep(5.0)
            try:
                W.press_key(W.VK_ESCAPE)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
            ready = self._videos.resolve(friend, local_id) is not None
            self._last_result = "已缓存" if ready else "点击后仍未缓存"
            self._last_error = "" if ready else "微信未在本次点击后缓存该视频"
            self._log(f"实验性视频缓存：{friend} local_id={local_id} → {self._last_result}")
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._log(f"实验性视频缓存失败：{exc}")
        finally:
            try:
                if prev_fg and prev_fg != win.hwnd:
                    W.user32.SetForegroundWindow(prev_fg)
            except Exception:  # noqa: BLE001
                pass
            self._broadcast()

    def _ensure_ocr(self):
        if self._ocr is not None:
            return self._ocr
        try:
            ocr = OCRWorker()
            if hasattr(ocr, "warmup"):
                ocr.warmup()
            self._ocr = ocr
        except Exception as exc:  # noqa: BLE001
            self._log(f"OCR 初始化失败：{exc}")
            self._ocr = None
        return self._ocr

    def _switch_to(self, win, lay, ocr, img, target: str) -> bool:
        sx = (lay.search_box[0] + lay.search_box[2]) // 2
        sy = (lay.search_box[1] + lay.search_box[3]) // 2
        W.post_click(win, sx, sy)
        time.sleep(0.4)
        try:
            W.user32.keybd_event(VK_CONTROL, 0, 0, 0)
            W.user32.keybd_event(VK_A, 0, 0, 0)
            time.sleep(0.02)
            W.user32.keybd_event(VK_A, 0, 2, 0)
            W.user32.keybd_event(VK_CONTROL, 0, 2, 0)
            W.press_key(VK_BACK)
        except Exception:  # noqa: BLE001
            pass
        W.type_text(target)
        time.sleep(1.0)
        img2 = W.capture(win)
        if img2 is None:
            return False
        rows = extract.read_list_rows(img2, lay, ocr)
        best = None
        for row in rows:
            name = str(row.get("name") or "")
            if name == target or target in name or name in target:
                best = row
                break
        if best is None:
            try:
                W.press_key(W.VK_ESCAPE)
            except Exception:  # noqa: BLE001
                pass
            return False
        W.post_click(
            win,
            int(best.get("center_x") or (lay.conv_list[0] + lay.conv_list[2]) // 2),
            int(best.get("center_y") or 0),
        )
        time.sleep(0.9)
        img3 = W.capture(win)
        if img3 is None:
            return False
        title = extract.read_chat_title(img3, lay, ocr)
        return bool(title and (title == target or target in title or title in target))

    def _scroll_bottom(self, win, lay) -> None:
        x = (lay.chat[0] + lay.chat[2]) // 2
        y = lay.chat[3] - 40
        try:
            for _ in range(12):
                W.post_scroll(win, x, y, -120)
                time.sleep(0.02)
        except Exception:  # noqa: BLE001
            pass

    def _find_video_duration(self, img, lay, ocr):
        cl, ct, cr, cb = lay.chat
        crop = img.crop((max(0, cl), max(0, ct), min(img.width, cr), min(img.height, cb)))
        if crop.width < 20 or crop.height < 20:
            return None
        try:
            lines = ocr.ocr(crop)
        except Exception:  # noqa: BLE001
            return None
        best = None
        for line in lines:
            text = str(getattr(line, "text", "") or "").strip()
            if not _DURATION_RE.match(text):
                continue
            box = getattr(line, "box", None)
            if not box:
                continue
            b = (box[0] + cl, box[1] + ct, box[2] + cl, box[3] + ct)
            if best is None or b[1] > best[1]:
                best = b
        return best
