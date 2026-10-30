"""监听核心（Lead 维护）。

线程模型：
* 一个后台线程做「找窗口 → 截图 → 布局 → OCR → 解析 → 去重 → 推事件」。
* GUI 只读 ``events`` 队列，调用 ``set_monitored`` / ``list_friends`` 等轻量方法。
* 好友枚举与消息轮询共用同一线程，避免同时操作微信界面打架。

红线：只截图 + 只投递窗口消息，不读内存、不注入、不 Hook、不改微信文件。
"""
from __future__ import annotations

import atexit
import hashlib
import queue
import shutil
import tempfile
from pathlib import Path
import threading
import time
from collections import Counter

import numpy as np
from PIL import Image

from . import config as cfg_mod
from . import (builtin_emoji, db_source, emoticon, extract, media_cache, media_export,
               store, textlog, wechat_keys)
from . import layout as L
from . import win as W
from .models import (KIND_EMOJI, KIND_FILE, KIND_IMAGE, KIND_SYSTEM, KIND_TEXT, KIND_VOICE, SENDER_FRIEND, Friend, Message,
                     event_friends, event_log, event_message, event_status)

VK_CONTROL = 0x11
VK_A = 0x41
VK_BACK = 0x08
VK_ESCAPE = 0x1B

_UNREAD_SUFFIX = ("①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "...")

#: A8：数据库直读/轮询失败后的重试退避（秒），第 N 次连续失败用第 N 项，越界取最后一项。
#: 30 → 60 → 120 → 240 → 300 封顶；任何一次成功立即清零（见 ``_reset_db_retry``）。
DB_RETRY_BACKOFF_S = (30.0, 60.0, 120.0, 240.0, 300.0)


def db_retry_delay(attempt: int) -> float:
    """第 ``attempt`` 次连续失败后应等待的秒数（attempt<=0 表示无需等待）。

    A8：旧实现只在 ``_db_failed`` 上做"一次失败即永久退 OCR"，微信重启/升级、
    数据库被占用等**瞬时**故障会把直读永久关掉，必须重启进程才能恢复。这里给出
    纯函数退避表，便于测试与观测，不引入随机抖动（便于按日志复现）。
    """
    try:
        n = int(attempt)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0.0
    return float(DB_RETRY_BACKOFF_S[min(n, len(DB_RETRY_BACKOFF_S)) - 1])


def norm_name(name: str) -> str:
    """会话名归一化：去掉空格、未读数、群人数等信息。"""
    if not name:
        return ""
    s = name.strip().replace(" ", "").replace("\u3000", "")
    for _ in range(3):
        if s and s[-1] in _UNREAD_SUFFIX:
            s = s[:-1]
        if s.endswith(")") or s.endswith("）"):
            i = max(s.rfind("("), s.rfind("（"))
            if i > 0 and s[i + 1:-1].strip().isdigit():
                s = s[:i]
    s = s.replace("[未读]", "")
    return s


def title_matches(title: str, target: str) -> bool:
    a, b = norm_name(title), norm_name(target)
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


class MonitorBackend:
    """实现 api.Backend 协议。"""

    def __init__(self, config: dict | None = None):
        self.config = dict(config or cfg_mod.load())
        self.events: "queue.Queue[dict]" = queue.Queue(maxsize=10000)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False

        self._monitored: list[str] = [str(x) for x in (self.config.get("monitored") or []) if str(x).strip()]
        _cache = [str(n) for n in (self.config.get("friends_cache") or []) if str(n).strip()]
        # 旧版本/误操作可能把几千条原始联系人写进缓存；超过 800 条视为脏数据直接丢弃，
        # 启动时会自动用数据库快速重新同步（recent 模式 0.2s），避免 GUI 卡死。
        if len(_cache) > 800:
            _cache = []
        self._friends: list[Friend] = [Friend(name=n) for n in _cache]
        self._want_friends = False
        self._friends_running = False

        self._ocr = None
        self._ocr_error = ""
        self._win = None
        self._layout: L.Layout | None = None
        self._layout_key = None
        self._tick_no = 0
        self._chat_title = ""
        self._error = ""
        self._monitor_state = "idle"
        self._last_ocr_ms = 0
        self._last_status_push = 0.0

        self._baselines: dict[str, list] = {}
        self._last_switch = 0.0
        self._switch_fail = 0
        self._next_target = self._monitored[0] if self._monitored else ""
        self._rotate_last = 0.0
        self._last_restore = 0.0
        self._anim_boxes: list[tuple[int, int, int, int]] = []
        # 已经推送过的动图区域（避免动画帧变化导致同一条表情包被反复推送）
        self._anim_regions: list[dict] = []
        self._log_friend = ""
        self._used_dats: set[str] = set()
        self._recent_texts: dict[str, list] = {}
        self._image_keys_tried = False
        self._stickers: dict = {}
        self._used_stickers: set[str] = set()
        self._db = None
        # A8：``_db_failed`` 保留原字段名/类型（兼容外部读状态），语义改为"当前处于失败
        # 退避期"，不再永久关闭直读；``_db_retry_at`` 到点后由 ``_tick`` 自动重试。
        self._db_failed = False
        self._db_switch_last = 0.0
        self._db_errors = 0
        self._db_retry_count = 0      # 连续失败次数（成功一次清零）
        self._db_retry_at = 0.0       # 下次允许重试的 epoch 秒（0 = 立即可试）
        self._db_last_error = ""      # 最近一次直读错误（观测用，不改原日志字段）
        self._ephemeral = False            # 兼容字段（历史同步改为按条 persist 控制）
        self._history_tmp: Path | None = None
        self._last_frame_sig = ""
        self._last_header_sig = ""

    # ---------------- 对外接口 ----------------
    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="wx-monitor", daemon=True)
            self._thread.start()
        if self._friends:
            self._emit(event_friends(len(self._friends), list(self._friends)))
        # 每次启动都重新同步一次：好友/群列表只保留"有历史聊天记录"的
        self._want_friends = True
        self._log("监听服务已启动")

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._stop.set()
        th = self._thread
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=3.0)
        self._thread = None
        try:
            if self._ocr is not None:
                self._ocr.close()
        except Exception:
            pass
        self._ocr = None
        # 退出前落盘一次配置（密钥/监听名单/好友缓存），避免"关闭后配置像丢了一样"
        try:
            cfg_mod.save(self.config)
        except Exception:
            pass
        self.clear_history_media()
        self._log("监听服务已停止")
        try:
            if self._db is not None:
                self._db.close()
        except Exception:
            pass

    def is_running(self) -> bool:
        return self._running

    def get_state(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "wechat_found": bool(self._db is not None)
            or bool(self._win is not None and getattr(self._win, "alive", False)),
                "chat_title": self._chat_title,
                "monitoring": list(self._monitored),
                "error": self._error,
                "tick": self._tick_no,
                "monitor_state": self._monitor_state,
                "last_ocr_ms": self._last_ocr_ms,
                "friends_count": len(self._friends),
                # A8：直读状态（新增字段，不改原有键；``db_retry_in_s`` 供界面显示倒计时）
                "db_direct": bool(self._db is not None),
                "db_failed": bool(self._db_failed),
                "db_errors": int(self._db_errors),
                "db_retry_count": int(self._db_retry_count),
                "db_retry_in_s": self._db_retry_in_s(),
                "db_last_error": str(self._db_last_error),
            }

    def sync_history(self, names: list[str], count: int | None = None) -> dict:
        """同步所选好友的历史聊天记录（临时数据：不写 log，媒体放临时目录，退出即删）。

        count=None 表示"全部"（本机缓存到的所有消息）；返回统计信息并发出 history 事件。
        """
        names = [str(n).strip() for n in (names or []) if str(n).strip()]
        result = {"requested": count, "friends": names, "available": 0, "synced": 0,
                  "short": [], "error": ""}
        if not names:
            result["error"] = "请先勾选好友"
            self._emit({"type": "history", "data": result})
            return result
        self._ensure_image_keys()
        if self._history_tmp is None:
            try:
                self._history_tmp = Path(tempfile.mkdtemp(prefix="wxbox_history_"))
                atexit.register(self.clear_history_media)
            except Exception as exc:  # noqa: BLE001
                result["error"] = f"创建临时目录失败: {exc}"
                self._emit({"type": "history", "data": result})
                return result
        bridge = None
        try:
            bridge = db_source.DbBridge(self.config, self._log, media_dir=self._history_tmp)
            ok = [n for n in names if bridge.add_friend(n)]
            if not ok:
                raise RuntimeError("所选好友无法解析（可能是群聊或未同步）")
            self._ephemeral = True
            for name in ok:
                state = bridge.sources.get(name)
                if not state:
                    continue
                rows = bridge.reader.read_messages(state["username"], 0, limit=200000)
                available = len(rows)
                if count is not None and available < count:
                    result["short"].append({"friend": name, "available": available})
                if count is not None and available > count:
                    rows = rows[-count:]
                result["available"] = max(result["available"], available)
                for row in rows:
                    msg = bridge._convert(name, state["username"], row)
                    if msg is not None:
                        # 历史同步：只进界面内存，不写 log/messages.jsonl、不写聊天记录
                        self._publish_message(msg, silent=True, persist=False)
                        result["synced"] += 1
            if result["short"]:
                detail = "、".join(f"「{s['friend']}」只有 {s['available']} 条" for s in result["short"])
                self._log(f"没有这么多条聊天记录：请求 {count} 条，{detail}", "warn")
            self._log(
                f"历史记录同步完成：{len(ok)} 位好友，共 {result['synced']} 条"
                "（临时数据：关闭程序即清除，不写入 log/media）"
            )
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)
            self._log(f"历史记录同步失败: {exc}", "warn")
        finally:
            self._ephemeral = False
            if bridge is not None:
                try:
                    bridge.close()
                except Exception:
                    pass
            self._emit({"type": "history", "data": result})
        return result

    def clear_history_media(self) -> None:
        """清理历史同步的临时媒体（一般由 stop() 调用）。"""
        if self._history_tmp is not None:
            shutil.rmtree(self._history_tmp, ignore_errors=True)
            self._history_tmp = None

    def list_friends(self, refresh: bool = False) -> list[Friend]:
        if refresh:
            self._want_friends = True
        with self._lock:
            return list(self._friends)

    def set_monitored(self, names: list[str]) -> None:
        names = [str(n) for n in names if str(n).strip()]
        with self._lock:
            self._monitored = names
            self._next_target = names[0] if names else ""
            self._baselines.clear()
            self._switch_fail = 0
            self._monitor_state = "monitoring" if names else "idle"
        self.config["monitored"] = names
        cfg_mod.save(self.config)
        self._log(f"监听名单已更新: {', '.join(names) if names else '（空）'}")
        self._push_status()

    # ---------------- 内部工具 ----------------
    def _emit(self, event: dict) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
                self.events.put_nowait(event)
            except Exception:
                pass

    def _log(self, text: str, level: str = "info") -> None:
        self._emit(event_log(text, level))

    def _status(self, **data) -> None:
        payload = {
            "running": self._running,
            "wechat_found": bool(self._db is not None)
            or bool(self._win is not None and getattr(self._win, "alive", False)),
            "chat_title": self._chat_title,
            "monitoring": list(self._monitored),
            "error": self._error,
            "tick": self._tick_no,
            "monitor_state": self._monitor_state,
            "last_ocr_ms": self._last_ocr_ms,
            "friends_count": len(self._friends),
            # A8：与 get_state 同口径（接口/事件两边都能看到退避状态）
            "db_direct": bool(self._db is not None),
            "db_failed": bool(self._db_failed),
            "db_errors": int(self._db_errors),
            "db_retry_count": int(self._db_retry_count),
            "db_retry_in_s": self._db_retry_in_s(),
            "db_last_error": str(self._db_last_error),
        }
        payload.update(data)
        self._emit(event_status(**payload))

    def _push_status(self) -> None:
        self._status()

    def _ensure_ocr(self):
        if self._ocr is not None:
            return self._ocr
        try:
            from .ocr import OCRWorker

            self._ocr = OCRWorker()
            self._ocr_error = ""
            try:  # 预热：避免首帧多等 1.5s 左右
                if hasattr(self._ocr, "warmup"):
                    self._ocr.warmup()
            except Exception:
                pass
            self._log(f"OCR 引擎已就绪（{getattr(self._ocr, 'engine_name', 'unknown')}）")
        except Exception as exc:  # noqa: BLE001
            self._ocr_error = f"OCR 初始化失败: {exc}"
            self._log(self._ocr_error, "error")
            self._ocr = None
        return self._ocr

    # ---------------- 主循环 ----------------
    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self._error = f"内部错误: {exc}"
                self._log(self._error, "error")
                time.sleep(1.0)
            interval = float(self.config.get("poll_interval") or 1.2)
            elapsed = time.time() - started
            if elapsed < interval:
                self._stop.wait(max(0.05, interval - elapsed))
            self._tick_no += 1

    def _check_account_switch(self) -> None:
        """运行中检测微信切换账号：重置直读与密钥状态，下个 tick 自动重连。"""
        if self._db is None:
            return
        acc = self._active_account()
        try:
            cur = getattr(self._db, "account_dir", None)
            if acc is not None and cur is not None and acc.name != cur.name:
                self._log(f"检测到微信切换账号：{cur.name} → {acc.name}，正在重连 ...", "warn")
                try:
                    self._db.close()
                except Exception:
                    pass
                self._db = None
                # A8：换账号仍是"硬重置"语义——清空退避计数，允许立即重建直读
                self._reset_db_retry()
                self._image_keys_tried = False
                self.config["image_wxid"] = ""
                self._stickers = {}
                self._used_stickers = set()
                self._used_dats = set()
                self._recent_texts.clear()
                with self._lock:
                    self._friends = []
                self.config["friends_cache"] = []
                self._want_friends = True
                self.config["image_xor_key"] = ""
                self.config["image_aes_key"] = ""
                self.config["image_cfg_dword"] = ""
        except Exception:
            pass

    # ---------------- A8：DB 直读退避重试 ----------------
    def _db_retry_in_s(self, now: float | None = None) -> float:
        """距离下次允许重试还有多少秒（非退避期为 0）；只用于观测，不加锁。"""
        if not self._db_failed:
            return 0.0
        now = time.time() if now is None else float(now)
        try:
            return max(0.0, round(float(self._db_retry_at or 0.0) - now, 1))
        except (TypeError, ValueError):
            return 0.0

    def _db_retry_due(self, now: float | None = None) -> bool:
        """现在是否允许（重新）尝试启用数据库直读。

        未失败过 → True（首次尝试）；失败过 → 只在退避到点后返回 True。
        """
        if not self._db_failed:
            return True
        now = time.time() if now is None else float(now)
        try:
            return now >= float(self._db_retry_at or 0.0)
        except (TypeError, ValueError):
            return True

    def _arm_db_retry(self, reason: str) -> float:
        """记一次直读失败并把下次重试排到退避之后；返回本次等待秒数。

        只改退避状态，不关闭 OCR 回退路径：退避期内 ``_tick`` 仍走截图/OCR，
        到点后自动再试 DB 直读。
        """
        self._db_retry_count = int(self._db_retry_count) + 1
        delay = db_retry_delay(self._db_retry_count)
        self._db_retry_at = time.time() + delay
        self._db_failed = True
        self._db_last_error = str(reason or "")[:300]
        return delay

    def _reset_db_retry(self) -> None:
        """直读恢复成功（或换账号硬重置）：清空失败/退避状态。"""
        self._db_failed = False
        self._db_errors = 0
        self._db_retry_count = 0
        self._db_retry_at = 0.0
        self._db_last_error = ""

    def _tick(self) -> None:
        self._check_account_switch()
        # 数据库直读不需要截图窗口：先尝试启用/推进直读
        # A8：失败过也要按退避重试（旧实现 not self._db_failed 会永久关掉直读）
        if (self._db is None and self._db_retry_due()
                and self.config.get("db_direct", True) and list(self._monitored)):
            self._ensure_image_keys()
            self._init_db(list(self._monitored))
        if self._db is not None:
            try:
                _w = W.find_wechat_window()
                if _w is not None:
                    self._win = _w
                self._tick_db(_w)
            except Exception as exc:  # noqa: BLE001
                self._log(f"数据库直读异常: {exc}", "warn")
            return
        win = W.find_wechat_window()
        if win is None:
            self._win = None
            self._error = "未找到微信窗口：请确认微信电脑版已登录，并且主窗口没有被最小化到托盘"
            self._monitor_state = "error"
            self._status()
            return
        self._win = win

        if win.minimized:
            self._try_restore(win)
            self._error = "微信窗口被最小化了，无法截图。请把微信窗口恢复（可以被其他窗口遮挡，但不能最小化）"
            self._monitor_state = "error"
            self._status()
            return

        img = W.capture(win)
        if img is None:
            self._error = "截取微信窗口失败（窗口可能被最小化或画面全黑），请恢复微信窗口"
            self._monitor_state = "error"
            self._status()
            return
        self._error = ""

        key = (win.width, win.height, win.dpi)
        if self._layout is None or key != self._layout_key or self._tick_no % 90 == 0:
            try:
                self._layout = L.analyze(img, win)
                self._layout_key = key
            except Exception as exc:  # noqa: BLE001
                self._log(f"布局分析失败: {exc}", "warn")
        lay = self._layout
        if lay is None:
            return

        ocr = self._ensure_ocr()
        if ocr is None:
            self._monitor_state = "error"
            self._status(error=self._ocr_error)
            return

        # 标题：标题栏画面变了才重新 OCR（省 200ms/次）
        header_sig = self._region_signature(img, lay.header)
        if header_sig != self._last_header_sig or not self._chat_title:
            try:
                title = extract.read_chat_title(img, lay, ocr)
                if title:
                    if norm_name(title) != norm_name(self._chat_title):
                        self._log(f"当前会话: {title}")
                    self._chat_title = title
                    self._last_header_sig = header_sig
            except Exception:
                pass

        if self._want_friends:
            self._enumerate_friends(win, lay, img, ocr)
            return

        self._ensure_image_keys()

        names = list(self._monitored)
        if not names:
            self._monitor_state = "idle"
            self._status()
            return

        target = self._next_target if self._next_target in names else names[0]
        matched = next((n for n in names if title_matches(self._chat_title, n)), None)
        if matched is not None:
            self._monitor_state = "monitoring"
            self._next_target = matched
            frame_sig = self._frame_signature(img, lay)
            if frame_sig != self._last_frame_sig or self._baselines.get(matched) is None:
                self._parse_and_emit(win, lay, img, ocr, matched)
                self._last_frame_sig = frame_sig
            else:
                self._status()
                return
            if len(names) > 1 and time.time() - self._rotate_last > float(self.config.get("rotate_interval") or 6.0):
                self._next_target = names[(names.index(matched) + 1) % len(names)]
                self._rotate_last = time.time()
            self._status()
            return

        # 会话不匹配
        self._monitor_state = "waiting"
        if not self.config.get("auto_switch", True):
            self._status(error=f"当前会话是「{self._chat_title}」，不是被监听好友「{target}」（自动切换已关闭）")
            return
        if self._switch_to(win, lay, ocr, target):
            self._status()
            return
        self._status(error=f"切换到「{target}」失败，请手动在微信里打开该好友的聊天窗口")

    def _try_restore(self, win) -> None:
        now = time.time()
        if now - self._last_restore < 5:
            return
        self._last_restore = now
        if self.config.get("auto_restore_window", True):
            ok = W.restore_no_activate(win)
            self._log("检测到微信最小化，已尝试恢复窗口（不会抢焦点）" if ok else "尝试恢复微信窗口失败，请手动恢复", "warn")

    def _active_account(self):
        try:
            return media_cache.active_account_dir()
        except Exception:
            return None

    @staticmethod
    def _account_wxid(acc) -> str:
        """账号目录 <wxid>_<4hex> → wxid（wxid 自身含下划线，必须从右边切）。"""
        if acc is None:
            return ""
        name = acc.name
        if name.startswith("wxid_") and name.count("_") >= 2:
            return name.rsplit("_", 1)[0]
        return name

    def _ensure_image_keys(self) -> None:
        """自动提取图片解密密钥；检测到微信切换账号时强制重提（读内存一次，缓存到 config）。"""
        if not self.config.get("auto_extract_image_key", True):
            return
        acc = self._active_account()
        cur_wxid = str(self.config.get("image_wxid") or "").strip()
        acc_wxid = self._account_wxid(acc)
        switched = bool(acc and cur_wxid and acc_wxid and acc_wxid != cur_wxid)
        if self._image_keys_tried and not switched:
            return
        if switched:
            cached_by_acc = dict(self.config.get("image_keys_by_account") or {})
            hit = cached_by_acc.get(acc_wxid)
            if hit and hit.get("aes_key"):
                self._log(f"检测到微信切换账号（{cur_wxid} → {acc_wxid}），使用缓存密钥")
                self.config["image_xor_key"] = str(hit.get("xor_key", ""))
                self.config["image_aes_key"] = str(hit.get("aes_key", ""))
                self.config["image_cfg_dword"] = hit.get("cfg_dword", "")
                self.config["image_wxid"] = acc_wxid
                self._stickers = {}
                self._used_stickers = set()
                self._used_dats = set()
                self._image_keys_tried = True
                cfg_mod.save(self.config)
                return
            self._log(f"检测到微信切换账号（{cur_wxid} → {acc_wxid}），重新提取图片密钥 ...")
            self.config["image_xor_key"] = ""
            self.config["image_aes_key"] = ""
            self.config["image_cfg_dword"] = ""
            self.config["image_wxid"] = ""
            self._stickers = {}
            self._used_stickers = set()
            self._used_dats = set()
        self._image_keys_tried = True
        if (not switched and str(self.config.get("image_xor_key") or "").strip()
                and str(self.config.get("image_aes_key") or "").strip()):
            self._log("图片解密密钥已就绪（来自配置）")
            return
        try:
            info = wechat_keys.extract_image_keys(self.config, force=True, account_dir=acc)
        except Exception as exc:  # noqa: BLE001
            self._log(f"图片密钥提取异常: {exc}", "warn")
            info = None
        if not info:
            self._log("未提取到图片解密密钥：图片将使用高质量帧/可选导出；可在 config 手动填 image_xor_key/image_aes_key", "warn")
            return
        self.config["image_xor_key"] = str(info["xor_key"])
        self.config["image_aes_key"] = info["aes_key"]
        self.config["image_wxid"] = info.get("wxid", "")
        if info.get("cfg_dword") is not None:
            self.config["image_cfg_dword"] = int(info["cfg_dword"])
        by_acc = dict(self.config.get("image_keys_by_account") or {})
        if info.get("wxid"):
            by_acc[info["wxid"]] = {
                "xor_key": str(info.get("xor_key", "")),
                "aes_key": info.get("aes_key", ""),
                "cfg_dword": info.get("cfg_dword", ""),
            }
            self.config["image_keys_by_account"] = by_acc
        cfg_mod.save(self.config)
        aes = str(info.get("aes_key") or "")
        masked = (aes[:4] + "**" + aes[-2:]) if len(aes) >= 6 else "**"
        self._log(f"已从微信内存提取图片密钥并缓存：xor={info['xor_key']:#x} aes={masked}（完整密钥已写入 config.json）")

    def _ensure_stickers(self) -> dict:
        """加载/增量刷新表情包库（解密 cache/Emoticon 与 business/emoticon）。"""
        first = not self._stickers
        seed = self.config.get("image_cfg_dword")
        wxid = str(self.config.get("image_wxid") or "").strip()
        if not seed or not wxid:
            self._log("表情包解密需要 image_cfg_dword/image_wxid（首次运行自动提取）", "warn")
            return {}
        acc = media_cache.active_account_dir()
        if acc is None:
            return {}
        try:
            self._stickers = emoticon.build_index(acc, int(seed), wxid,
                                                  store.MEDIA_DIR / "stickers")
            if first:
                self._log(f"表情包库已加载：{len(self._stickers)} 个（已解密为原格式）")
        except Exception as exc:  # noqa: BLE001
            self._log(f"表情包库加载失败: {exc}", "warn")
            if first:
                self._stickers = {}
        return self._stickers

    # ---------------- 消息解析 ----------------
    def _region_signature(self, img: Image.Image, box: tuple[int, int, int, int]) -> str:
        try:
            l, t, r, b = box
            crop = img.crop((max(0, l), max(0, t), max(0, r), max(0, b)))
            if crop.width < 4 or crop.height < 4:
                return ""
            small = np.asarray(crop.convert("L").resize((64, 16), Image.BILINEAR))
            return hashlib.sha1(small.tobytes()).hexdigest()[:16]
        except Exception:
            return ""

    def _frame_signature(self, img: Image.Image, lay: L.Layout) -> str:
        """消息区画面指纹；动图区域先涂黑再算，避免动图导致每帧都重新 OCR。"""
        try:
            mx0, my0, mx1, my1 = lay.message_area
            crop = img.crop((mx0, my0, mx1, my1)).convert("L")
            arr = np.asarray(crop).copy()
            for (l, t, r, b) in self._anim_boxes:
                l2, t2 = max(0, l - mx0), max(0, t - my0)
                r2, b2 = max(0, r - mx0), max(0, b - my0)
                if r2 > l2 and b2 > t2:
                    arr[t2:b2, l2:r2] = 0
            if arr.shape[0] > 8:
                arr = arr[::3, ::3]
            return hashlib.sha1(arr.tobytes()).hexdigest()[:16]
        except Exception:
            return ""

    def _parse_and_emit(self, win, lay: L.Layout, img: Image.Image, ocr, friend: str) -> None:
        mx0, my0, mx1, my1 = lay.message_area
        area = img.crop((mx0, my0, mx1, my1))
        t0 = time.perf_counter()
        lines = ocr.ocr(area)
        self._last_ocr_ms = int((time.perf_counter() - t0) * 1000)
        blocks = extract.parse_frame(img, lay, ocr, friend, ocr_lines=lines)
        blocks = self._drop_known_animations(friend, blocks)
        if not blocks:
            self._baselines[friend] = []
            return

        # 时间标签：系统消息里的时间/日期，附加到其后的消息上
        cur_time = ""
        entries = []
        for b in blocks:
            if b.kind == KIND_SYSTEM:
                t = (b.text or "").strip()
                if t and (extract.TIME_RE.search(t) or extract.DATE_RE.search(t)) and len(t) <= 40:
                    cur_time = t
                    continue
                if t and ("撤回" in t or "引用消息" in t):
                    entries.append((b, cur_time))
                continue
            entries.append((b, cur_time))

        base = self._baselines.get(friend)
        entries = [(b, tt, self._signature(b)) for b, tt in entries]
        if base is None:
            self._baselines[friend] = list(entries)
            if self.config.get("emit_history_on_start", False):
                self._log(f"首次抓帧，已加载 {len(entries)} 条历史消息")
            else:
                if entries:
                    self._log(f"已建立基线（当前会话可见 {len(entries)} 条消息，之后的新消息才会推送）")
                return
            new_items = list(entries)
        else:
            new_items = self._diff_frames(base, entries)
            self._baselines[friend] = list(entries)
        new_items = self._filter_recent_duplicates(friend, new_items)
        for b, tt, sig in new_items:
            try:
                self._emit_block(win, lay, friend, b, tt, sig)
            except Exception as exc:  # noqa: BLE001
                self._log(f"消息入库失败: {exc}", "warn")

    def _filter_recent_duplicates(self, friend: str, items: list) -> list:
        """过滤"最近 3 分钟内已经推送过、文本近似"的重复消息（OCR/剪贴板抖动）。"""
        import difflib

        now = time.time()
        recent = [e for e in self._recent_texts.get(friend, []) if now - e[3] < 180]
        out = []
        for b, tt, sig in items:
            text = (b.text or "").strip()
            if b.kind == KIND_TEXT and text:
                dup = False
                for kind, sender, old_text, ts in recent:
                    if kind != KIND_TEXT or sender != b.sender or not old_text:
                        continue
                    if min(len(text), len(old_text)) >= 4:
                        ratio = difflib.SequenceMatcher(None, text, old_text).ratio()
                        if ratio >= 0.9:
                            dup = True
                            break
                if dup:
                    continue
                recent.append((b.kind, b.sender, text, now))
            out.append((b, tt, sig))
        self._recent_texts[friend] = recent[-80:]
        return out

    @staticmethod
    def _diff_frames(base: list, current: list) -> list:
        """对比前后两帧的消息块，返回新增项。

        先按签名精确匹配，再对文本消息做一次"近似文本"匹配：OCR 每次结果可能有细微差异
        （例如多出一个标点），近似匹配能避免同一条消息被重复推送。
        """
        import difflib

        remaining = list(base)
        new_items = []
        # 1) 精确签名匹配（同签名按出现次数配对）
        for item in current:
            sig = item[2]
            for i, old in enumerate(remaining):
                if old[2] == sig:
                    remaining.pop(i)
                    break
            else:
                new_items.append(item)
        # 2) 文本消息近似匹配（OCR 抖动）
        still_new = []
        for item in new_items:
            block = item[0]
            if block.kind == KIND_TEXT:
                text = (block.text or "").strip()
                matched = False
                if len(text) >= 4:
                    for i, old in enumerate(remaining):
                        ob = old[0]
                        if ob.kind != KIND_TEXT or ob.sender != block.sender:
                            continue
                        otext = (ob.text or "").strip()
                        if min(len(text), len(otext)) < 4:
                            continue
                        ratio = difflib.SequenceMatcher(None, text, otext).ratio()
                        if ratio >= 0.88:
                            remaining.pop(i)
                            matched = True
                            break
                if matched:
                    continue
            still_new.append(item)
        return still_new

    def _drop_known_animations(self, friend: str, blocks: list) -> list:
        """已知的动图消息（同一好友、同一区域）不再重复解析推送。"""
        if not self._anim_regions or not blocks:
            return blocks
        out = []
        for b in blocks:
            if b.kind in (KIND_EMOJI, KIND_IMAGE):
                box = b.box
                for reg in self._anim_regions:
                    if reg.get("friend") != friend:
                        continue
                    if _box_iou(box, reg["box"]) > 0.35:
                        break
                else:
                    out.append(b)
                    continue
                continue
            out.append(b)
        return out

    def _remember_animation(self, friend: str, block) -> None:
        self._anim_regions.append({"friend": friend, "box": block.box, "ts": time.time()})
        if len(self._anim_regions) > 40:
            self._anim_regions = self._anim_regions[-40:]

    def _signature(self, block) -> str:
        return f"{block.kind}|{block.sender}|{extract.visual_hash_of(block)}"

    def _emit_block(self, win, lay: L.Layout, friend: str, block, time_text: str, sig: str) -> None:
        extra = dict(block.extra or {})
        # 0) 内置小黄脸表情（emoji 消息）：直接"右键→复制"拿微信官方中文名（如 [微笑]），
        #    这样既不用截图，也能得到你要的中文格式
        if (block.kind == KIND_EMOJI and block.media is not None and win is not None
                and self._ocr is not None):
            bw = block.box[2] - block.box[0]
            bh = block.box[3] - block.box[1]
            if max(bw, bh) < int(90 * getattr(lay, "scale", 1.25)):
                try:
                    txt = (media_export.copy_message_text(
                        win, block, self._ocr,
                        timeout=float(self.config.get("copy_text_timeout", 8.0))) or "").strip()
                except Exception:
                    txt = ""
                if not txt and block.media is not None:
                    # 剪贴板拿不到时，用内置 109 个微信表情素材做图像兜底识别
                    try:
                        name = builtin_emoji.match(block.media)
                    except Exception:
                        name = None
                    if name:
                        txt = f"[{name}]"
                if txt:
                    block.kind = KIND_TEXT
                    block.text = txt
                    block.media = None
                    block.extra = dict(block.extra or {})
                    block.extra["emoji_name"] = txt
                    block.extra["no_copy"] = True
                    extra["emoji_name"] = txt
                    extra["no_copy"] = True
                    sig = self._signature(block)
        # 文本消息：用微信自带「复制」拿精确原文（含 [微笑] 这类表情名），失败则退回 OCR 文本
        if (block.kind == KIND_TEXT and self.config.get("copy_text_exact", True)
                and not (block.extra or {}).get("no_copy")
                and win is not None and self._ocr is not None):
            try:
                exact = media_export.copy_message_text(win, block, self._ocr,
                                                       timeout=float(self.config.get("copy_text_timeout", 8.0)))
                exact = (exact or "").strip()
                if exact:
                    block.text = exact
                    extra["exact_text"] = True
            except Exception as exc:  # noqa: BLE001
                self._log(f"精确复制文本失败: {exc}", "warn")
        if time_text:
            extra.setdefault("time_text", time_text)
        media_path = ""
        if block.media is not None and self.config.get("save_media", True):
            frames = None
            animated = False
            # 1a) 表情包：解密 cache/Emoticon 里的原文件（wxgf 动画转 GIF）
            if block.kind == KIND_EMOJI and block.media is not None:
                try:
                    stickers = self._ensure_stickers()
                    st = emoticon.match_sticker(block.media, getattr(lay, "chat_bg", (30, 30, 31)),
                                                stickers, used=self._used_stickers)
                    if st is not None:
                        media_path = store.rel(st.media_path)
                        extra["original"] = True
                        extra["format"] = st.media_path.suffix.lstrip(".").lower()
                        extra["animated"] = bool(st.animated)
                        extra["sticker_md5"] = st.md5
                        self._used_stickers.add(st.md5)
                        self._log(f"[{friend}] 已解密表情包原文件: {st.media_path.name} ({st.fmt})")
                    else:
                        extra["match_failed"] = True
                        self._log(f"[{friend}] 表情包未匹配到原文件，按设置跳过截图", "warn")
                except Exception as exc:  # noqa: BLE001
                    self._log(f"表情包解密失败: {exc}", "warn")
            # 1b) 图片：本地解密微信缓存的 .dat（需要 image_xor_key / image_aes_key）
            xor_conf = str(self.config.get("image_xor_key") or "").strip()
            aes_conf = str(self.config.get("image_aes_key") or "").strip()
            bw = block.box[2] - block.box[0]
            bh = block.box[3] - block.box[1]
            if xor_conf and block.kind == KIND_IMAGE:
                try:
                    out = media_cache.decrypt_newest_image(
                        int(xor_conf, 0), aes_conf, store.MEDIA_DIR / "originals",
                        used=self._used_dats,
                        aspect=(bw / bh) if bh else None,
                        max_age=float(self.config.get("decrypt_media_max_age", 86400.0)),
                        recent_within=float(self.config.get("decrypt_recent_within", 60.0)),
                    )
                    if out is not None:
                        media_path = store.rel(out)
                        extra["original"] = True
                        extra["format"] = out.suffix.lstrip(".").lower()
                        extra["animated"] = out.suffix.lower() == ".gif"
                        extra["decrypted"] = True
                        self._log(f"[{friend}] 已从缓存解密原文件: {out.name}")
                except Exception as exc:  # noqa: BLE001
                    self._log(f"缓存解密失败: {exc}", "warn")
            # 2) 可选：微信「另存为」导出（用户不推荐，默认关闭）
            exported = None
            if self.config.get("export_original_media", True) and block.kind in (KIND_IMAGE, KIND_EMOJI):
                w = block.box[2] - block.box[0]
                h = block.box[3] - block.box[1]
                is_sticker = block.kind == KIND_EMOJI and max(w, h) >= int(90 * getattr(lay, "scale", 1.25))
                if block.kind == KIND_IMAGE or is_sticker:
                    try:
                        exported = media_export.export_block(
                            win, block, self._ocr, store.MEDIA_DIR / "originals",
                            timeout=float(self.config.get("export_timeout", 12.0)),
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._log(f"导出原图失败: {exc}", "warn")
                        exported = None
                if exported is not None:
                    media_path = store.rel(exported)
                    extra["original"] = True
                    extra["format"] = exported.suffix.lstrip(".").lower()
                    animated = exported.suffix.lower() == ".gif"
                    extra["animated"] = bool(animated)
                    self._log(f"[{friend}] 已导出原始文件: {exported.name}")
            if not media_path and self.config.get("allow_screenshot_media", False):
                # 用户明确要求不要截图；只有显式打开 allow_screenshot_media 才走旧逻辑
                if block.kind in (KIND_EMOJI, KIND_IMAGE) and self.config.get("capture_gif", True):
                    frames, animated = self._probe_animation(win, block)
                extra["animated"] = bool(animated)
                if animated:
                    extra["n_frames"] = len(frames or [])
                    self._remember_animation(friend, block)
                try:
                    media_path = store.save_image(friend, block.kind, block.media, frames)
                except Exception:
                    media_path = ""
        if block.kind == KIND_FILE and (block.text or "").strip():
            try:
                src = media_cache.copy_document(block.text.strip(), store.MEDIA_DIR / "files")
                if src is not None:
                    media_path = store.rel(src)
                    extra["original"] = True
                    extra["original_name"] = src.name
                    try:
                        extra["size_bytes"] = src.stat().st_size
                    except OSError:
                        pass
                    self._log(f"[{friend}] 已拿到原始文件: {src.name}")
                else:
                    extra["original"] = False
            except Exception as exc:  # noqa: BLE001
                extra["original"] = False
                self._log(f"复制原始文件失败: {exc}", "warn")
        extra["visual_hash"] = sig
        if block.kind == KIND_VOICE:
            extra.setdefault("notice", "语音消息未接收")
        msg = Message(friend=friend, kind=block.kind, sender=block.sender, text=block.text,
                      media_path=media_path, extra=extra, ts=time.time())
        store.append_message(msg)
        try:
            if self._log_friend != friend:
                textlog.append_friend_header(friend)
                self._log_friend = friend
            textlog.append(msg)
        except Exception:
            pass
        self._emit(event_message(msg))
        label = {"text": "文字", "image": "图片", "emoji": "表情包", "file": "文件",
                 "voice": "语音", "system": "系统", "redpacket": "红包",
                 "transfer": "转账", "call": "通话"}.get(msg.kind, msg.kind)
        self._log(f"[{friend}] {msg.sender_label}发来{label}" + (f": {msg.text[:40]}" if msg.text else ""))

    # ---------------- 数据库直读模式 ----------------
    def _init_db(self, names: list[str]) -> None:
        try:
            bridge = db_source.DbBridge(self.config, self._log)
            ok = [n for n in names if bridge.add_friend(n)]
            if not ok:
                raise RuntimeError("没有被监听好友能解析出 wxid（可能是群聊）")
            self._db = bridge
            # A8：重建成功即清零失败/退避状态（可能之前失败过多轮）
            self._reset_db_retry()
            try:
                from . import config as _cfg
                _cfg.save(self.config)
            except Exception:
                pass
            self._log(f"数据库直读已启用（{len(ok)} 个会话）：文字精确、图片/表情包/文件/语音时长与消息一一对应")
        except Exception as exc:  # noqa: BLE001
            delay = self._arm_db_retry(f"{type(exc).__name__}: {exc}")
            self._log(
                f"数据库直读不可用，继续 OCR 模式（{delay:.0f}s 后自动重试）：{exc}", "warn")

    def _tick_db(self, win=None) -> None:
        if self._want_friends:
            self._enumerate_friends(win)
        names = list(self._monitored)
        if not names:
            self._monitor_state = "idle"
            self._status()
            return
        try:
            msgs = self._db.poll()
            # A8：轮询成功一次即视为恢复（清零错误计数与退避状态，重新计 5 次窗口）
            self._reset_db_retry()
        except Exception as exc:  # noqa: BLE001
            self._db_errors += 1
            self._db_last_error = f"{type(exc).__name__}: {exc}"[:300]
            self._log(f"数据库轮询失败: {exc}", "warn")
            msgs = []
            if self._db_errors >= 5:
                delay = self._arm_db_retry(
                    f"连续 {self._db_errors} 次轮询失败: {type(exc).__name__}: {exc}")
                self._log(
                    f"数据库直读连续失败，{delay:.0f}s 后自动重试（期间回退 OCR 模式）", "warn")
                try:
                    self._db.close()
                except Exception:
                    pass
                self._db = None
                self._error = ""
                self._monitor_state = "waiting"
                self._status()
                return
        self._error = ""
        for msg in msgs:
            self._publish_message(msg)
        self._monitor_state = "monitoring"
        # 界面能切就切到被监听好友（只影响观感，不影响读库；窗口最小化也能正常监听）
        if win is not None and not win.minimized and self.config.get("auto_switch", True):
            try:
                if time.time() - self._db_switch_last >= float(self.config.get("rotate_interval") or 6.0):
                    img = W.capture(win)
                    if img is not None:
                        lay = self._layout or L.analyze(img, win)
                        self._layout = lay
                        ocr = self._ensure_ocr()
                        title = extract.read_chat_title(img, lay, ocr) if ocr else ""
                        self._chat_title = title or self._chat_title
                        target = self._next_target if self._next_target in names else names[0]
                        if title and not any(title_matches(title, n) for n in names):
                            self._switch_to(win, lay, ocr, target)
                        self._db_switch_last = time.time()
            except Exception:
                pass
        self._status()

    # ---------------- A7：启动补采（只读本地库，不推 events、不改状态） ----------------
    def recent_messages(self, *, since_ts: float, until_ts: float,
                        limit: int = 200):
        """返回 ``since_ts <= ts <= until_ts`` 的入站消息 dict 列表（按时间升序）。

        供网关重启补采用（``Gateway._catchup_loader`` 调用）。语义约束：

        * **只读**：直接走已建立的 DB 直读桥（``db_source.DbBridge``），不写
          messages/文本日志、不 ``_emit``、不推进 ``last_id``，避免和正常轮询抢状态；
        * 数据库直读未就绪（``_db is None``，例如微信刚启动）→ 返回 ``None``，
          网关会隔 2s 重试直到超时；
        * 只取本地账号维度可解析的监听好友，``sender`` 必须是 ``friend``
          （自己发的不补，避免回环）。
        """
        db = self._db
        if db is None:
            return None
        try:
            cap = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            cap = 200
        names = [str(n) for n in (self._monitored or []) if str(n).strip()]
        sources = getattr(db, "sources", None)
        if not isinstance(sources, dict):
            return None
        out: list[dict] = []
        for friend in names:
            state = sources.get(friend) or {}
            username = str(state.get("username") or "")
            if not username:
                continue
            try:
                rows = db.reader.read_messages(username, 0, limit=max(cap * 3, 300))
            except Exception as exc:  # noqa: BLE001 - 单个好友读失败不影响其它人
                self._log(f"补采读库失败（{friend}）: {exc}", "warn")
                continue
            for row in rows or []:
                try:
                    ts = float(row.get("create_time") or 0.0)
                except (TypeError, ValueError):
                    ts = 0.0
                if ts < float(since_ts) or ts > float(until_ts):
                    continue
                try:
                    msg = db._convert(friend, username, row)
                except Exception:  # noqa: BLE001 - 单条坏 row 跳过
                    continue
                if msg is None or msg.sender != SENDER_FRIEND:
                    continue
                out.append(msg.to_dict())
        out.sort(key=lambda d: float(d.get("ts") or 0.0))
        return out[:cap]

    def _publish_message(self, msg: Message, silent: bool = False, persist: bool = True) -> None:
        if persist:
            store.append_message(msg)
            try:
                if self._log_friend != msg.friend:
                    textlog.append_friend_header(msg.friend)
                    self._log_friend = msg.friend
                textlog.append(msg)
            except Exception:
                pass
        self._emit(event_message(msg))
        if silent:
            return
        label = {"text": "文字", "image": "图片", "emoji": "表情包", "file": "文件",
                 "voice": "语音", "system": "系统", "redpacket": "红包",
                 "transfer": "转账", "call": "通话"}.get(msg.kind, msg.kind)
        self._log(f"[{msg.friend}] {msg.sender_label}发来{label}" + (f": {msg.text[:40]}" if msg.text else ""))

    def _probe_animation(self, win, block) -> tuple[list[Image.Image] | None, bool]:
        """判断表情包/图片是否为动图；是则抓几帧用于合成 GIF。"""
        cap = W.capture(win)
        if cap is None:
            return None, False
        box = block.box
        if box[0] < 0 or box[1] < 0 or box[2] > cap.width or box[3] > cap.height:
            return None, False
        try:
            base = block.media.convert("L").resize((64, 64))
            cur = cap.crop(box).convert("L").resize((64, 64))
            diff = _mean_abs_diff(base, cur)
            if diff <= 6:
                return None, False
            frames = [block.media, cap.crop(box)]
            self._anim_boxes.append(tuple(int(v) for v in box))
            if len(self._anim_boxes) > 12:
                self._anim_boxes = self._anim_boxes[-12:]
            for _ in range(6):
                time.sleep(0.12)
                c = W.capture(win)
                if c is None:
                    break
                frames.append(c.crop(box))
            uniq = _dedupe_frames(frames)
            return (uniq, True) if len(uniq) >= 2 else (None, False)
        except Exception:
            return None, False

    # ---------------- 切换会话 ----------------
    def _switch_to(self, win, lay: L.Layout, ocr, target: str) -> bool:
        now = time.time()
        if now - self._last_switch < 4.0:
            return False
        self._last_switch = now

        # 方案 1：左侧会话列表里直接点（不抢焦点）
        if self._click_list_row(win, lay, ocr, target):
            self._switch_fail = 0
            return True
        # 方案 2：搜索框（需要短暂前台输入）
        if self._switch_by_search(win, lay, ocr, target):
            self._switch_fail = 0
            self._rotate_last = now
            return True
        self._switch_fail += 1
        self._log(f"自动切换「{target}」失败（第 {self._switch_fail} 次）", "warn")
        return False

    def _click_list_row(self, win, lay: L.Layout, ocr, target: str) -> bool:
        img = W.capture(win)
        if img is None:
            return False
        try:
            rows = extract.read_list_rows(img, lay, ocr)
        except Exception:
            return False
        for row in rows:
            if title_matches(row.get("name", ""), target):
                x = row.get("center_x") or (lay.conv_list[0] + lay.conv_list[2]) // 2
                y = int(row.get("center_y") or 0)
                if y <= 0:
                    continue
                W.post_click(win, int(x), y)
                time.sleep(0.5)
                img2 = W.capture(win)
                if img2 is None:
                    return False
                title = extract.read_chat_title(img2, lay, ocr)
                if title_matches(title, target):
                    self._chat_title = title
                    self._log(f"已切换到会话「{title}」")
                    return True
        return False

    def _switch_by_search(self, win, lay: L.Layout, ocr, target: str) -> bool:
        if not self.config.get("allow_foreground_switch", True):
            return False
        prev_fg = W.user32.GetForegroundWindow()
        try:
            if not win.alive:
                return False
            if not W.activate(win):
                self._log("无法把微信切到前台（可能被系统限制），自动切换放弃", "warn")
                return False
            time.sleep(0.35)
            img = W.capture(win)
            if img is None:
                return False
            sx = (lay.search_box[0] + lay.search_box[2]) // 2
            sy = (lay.search_box[1] + lay.search_box[3]) // 2
            W.post_click(win, sx, sy)
            time.sleep(0.4)
            # 清空搜索框
            W.user32.keybd_event(VK_CONTROL, 0, 0, 0)
            W.user32.keybd_event(VK_A, 0, 0, 0)
            time.sleep(0.02)
            W.user32.keybd_event(VK_A, 0, 2, 0)
            W.user32.keybd_event(VK_CONTROL, 0, 2, 0)
            time.sleep(0.05)
            W.press_key(VK_BACK)
            time.sleep(0.15)
            W.type_text(target)
            time.sleep(1.0)
            img = W.capture(win)
            if img is None:
                return False
            rows = extract.read_list_rows(img, lay, ocr)
            best = None
            for row in rows:
                if title_matches(row.get("name", ""), target):
                    best = row
                    break
            if best is None:
                # 模糊：包含关系
                for row in rows:
                    n = norm_name(row.get("name", ""))
                    if n and (n in norm_name(target) or norm_name(target) in n):
                        best = row
                        break
            if best is None:
                W.press_key(VK_ESCAPE)
                return False
            W.post_click(win, int(best.get("center_x") or (lay.conv_list[0] + lay.conv_list[2]) // 2),
                         int(best.get("center_y") or 0))
            time.sleep(0.8)
            img2 = W.capture(win)
            if img2 is None:
                return False
            title = extract.read_chat_title(img2, lay, ocr)
            if title_matches(title, target):
                self._chat_title = title
                self._log(f"通过搜索切换到会话「{title}」")
                return True
            W.press_key(VK_ESCAPE)
            return False
        finally:
            try:
                if prev_fg and prev_fg != win.hwnd:
                    W.user32.SetForegroundWindow(prev_fg)
            except Exception:
                pass

    # ---------------- 好友枚举 ----------------
    def _enumerate_friends(self, win=None, lay=None, img=None, ocr=None) -> None:
        """同步好友列表：默认直接从数据库秒读；数据库不可用时回退界面滚动枚举。"""
        self._want_friends = False
        if self._friends_running:
            return
        self._friends_running = True
        self._monitor_state = "syncing"
        self._status()
        names: list[str] = []
        try:
            mode = str(self.config.get("friends_source") or "recent")
            if self.config.get("db_direct", True) and mode != "ui":
                t0 = time.time()
                try:
                    names = db_source.enumerate_friends(self.config, mode, log=self._log)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"数据库读取好友列表失败: {exc}", "warn")
                if names:
                    self._log(
                        f"好友列表已从数据库读取：{len(names)} 个（模式={mode}，"
                        f"耗时 {time.time()-t0:.2f}s，无需滚动通讯录）"
                    )
            if not names:
                # 回退：界面滚动枚举（慢，需要窗口不最小化）
                win = win or W.find_wechat_window()
                if win is None:
                    self._log("界面同步需要微信窗口，但没找到窗口；请恢复窗口后重试，或把 friends_source 设为 recent/all", "warn")
                else:
                    if img is None:
                        img = W.capture(win)
                    if img is not None:
                        if lay is None:
                            lay = L.analyze(img, win)
                            self._layout = lay
                        if ocr is None:
                            ocr = self._ensure_ocr()
                        if ocr is not None:
                            self._log("数据库方式不可用，开始界面同步（滚动通讯录，较慢）...")
                            from .friends import enumerate_friends as ui_enumerate

                            names = ui_enumerate(win, lay, ocr, log_cb=self._log)
            if names:
                with self._lock:
                    self._friends = [Friend(name=n) for n in names]
                self.config["friends_cache"] = names
                cfg_mod.save(self.config)
                self._emit(event_friends(len(names), self._friends))
                self._log(f"好友同步完成：共 {len(names)} 位")
            else:
                self._log("好友同步没有读到任何名字，请确认微信已登录", "warn")
        except Exception as exc:  # noqa: BLE001
            self._log(f"好友同步失败: {exc}", "error")
        finally:
            self._friends_running = False
            self._monitor_state = "monitoring" if self._monitored else "idle"
            self._status()

# ---------------- 工具 ----------------
def _box_iou(a: tuple, b: tuple) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0, ax1 - ax0) * max(0, ay1 - ay0)
    area_b = max(0, bx1 - bx0) * max(0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _mean_abs_diff(img1: Image.Image, img2: Image.Image) -> float:
    import numpy as np

    a = np.asarray(img1, dtype=np.int16)
    b = np.asarray(img2, dtype=np.int16)
    if a.shape != b.shape:
        return 0.0
    return float(np.abs(a - b).mean())


def _dedupe_frames(frames: list[Image.Image]) -> list[Image.Image]:
    out: list[Image.Image] = []
    prev = None
    for f in frames:
        small = f.convert("L").resize((48, 48))
        if prev is not None and _mean_abs_diff(prev, small) <= 1.0:
            continue
        out.append(f)
        prev = small
    return out


def create_backend(config: dict | None = None) -> MonitorBackend:
    return MonitorBackend(config)
