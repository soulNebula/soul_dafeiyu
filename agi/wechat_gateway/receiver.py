# -*- coding: utf-8 -*-
"""801 微信网关 · 接收适配器（持续监听微信来信）。

职责边界
--------
* 只做「消费 ``backend.events`` → 过滤 → 回调」这一件事；
* **不创建真实 wxauto / MonitorBackend，也不发送任何消息**：backend 由外部注入，
  本模块不调用 ``backend.start()`` / ``backend.stop()``，避免接收层偷偷拉起真实微信监听；
* 只用标准库（不 import ``wx_core``），事件就是普通 dict，方便在网关进程和测试里独立运行。

事件协议（来自 ``wx_core.monitor.MonitorBackend``）::

    {"type": "message", "data": Message.to_dict()}

    Message 字段：friend / sender / text / kind / ts / id
    sender == "friend" 才是对方发来的；sender == "self" 是自己发的。

只回调同时满足以下条件的消息：

1. ``type == "message"``；
2. ``data.sender == "friend"``（自己发的不算来信）；
3. ``data.kind == "text"`` 或 ``system``（图片/表情/文件/语音等一律不转发）；
   - ``system`` 只放行撤回 / 引用 / 拍了拍（P22），由网关 server 统一转写成标记；
4. ``data.friend`` 在白名单内；
5. ``data.text`` 非空白；
6. 按 ``data.id`` 去重（同一条消息只回调一次；没有 id 时用 friend/text/ts 兜底去重）。

P22：文本里出现 ``@配置昵称``（``mention_name``）时，在回调的 data 上打
``mention=True`` 标记（不改原文），网关 server 据此加 ``[对方@了你]`` 前缀。

P29：真机拍一拍不一定以 ``kind=system`` 送达——wx_core 可能把它 OCR 成
``kind=text``（原文如 ``"白云一片去悠悠" 拍了拍我``）。短文本含「拍了拍」时在
回调 data 上打 ``pat=True``，server 按 ``[拍一拍] …`` 统一转写并记 ``pat_detected``。

线程模型
--------
``start()`` 起一个 daemon 后台线程，阻塞读 ``backend.events``（带 timeout 以便感知停止）；
``stop()`` 置停止位并 join 该线程，返回后保证不再消费队列里的事件。
生命周期状态、统计、去重表分别由锁保护，``start()`` / ``stop()`` 可重复调用，
也可以在任意线程（包括回调内部）安全调用。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable

__all__ = ["ReceiveCursor", "WeChatReceiver"]

EVENT_MESSAGE = "message"
SENDER_FRIEND = "friend"
KIND_TEXT = "text"

DEFAULT_POLL_INTERVAL = 0.2
DEFAULT_MAX_SEEN_IDS = 4096

#: A7：启动补采的默认上限（条数 / 时间窗），防止游标缺失或太久远时灾难性回放。
DEFAULT_CATCHUP_MAX_ITEMS = 200
DEFAULT_CATCHUP_WINDOW_S = 86400.0
CURSOR_VERSION = 1


def _clean(value: object) -> str:
    """把事件字段规整成去首尾空白的 str（非字符串一律视为空）。"""
    return value.strip() if isinstance(value, str) else ""


#: system 消息里允许放行的关键词（P22：撤回/引用/拍一拍）
SYSTEM_PASSTHROUGH_HINTS = ("撤回", "引用消息", "拍了拍")


def is_mention(text: object, mention_name: object) -> bool:
    """P22：私聊文本里是否 @ 了配置的机器人昵称（``@昵称``）。"""
    value = _clean(text)
    name = _clean(mention_name)
    if not value or not name or "@" not in value:
        return False
    return f"@{name}" in value


def looks_like_pat(text: object) -> bool:
    """P29：拍一拍事件识别（真机可能以 ``kind=text`` 送达）。

    微信/OCR 原文样例（2026-10-27 15:08 真机）：``"白云一片去悠悠" 拍了拍我``
    （ASCII 双引号 + 空格）、``「白云一片去悠悠」拍了拍我``、``我拍了拍你``。

    必须满足以下之一才认（避免把「今天拍了拍照」这类正常句子误判）：
    - ``拍了拍`` 后面紧跟人称（我/你/他/她/它/对方/咱/俺）；
    - 带引号/「」包裹的名字（``"X" 拍了拍我``、``「X」拍了拍我``）。
    文本长度限制 120，避免长段聊天里偶然出现「拍了拍」被当事件。
    """
    value = _clean(text)
    if not value or "拍了拍" not in value or len(value) > 120:
        return False
    after = value.split("拍了拍", 1)[1].lstrip(" 　")
    if after[:1] in ("我", "你", "他", "她", "它", "对", "咱", "俺"):
        return True
    return any(mark in value for mark in ("「", "」", '"', "“", "”"))


class ReceiveCursor:
    """A7：入站接收游标（``agi/data/wechat/cursor.json``，一行 JSON，原子写）。

    记录"网关处理到哪条入站消息"，供重启后补采 ``(last_ts, 启动时刻]`` 之间的消息。
    设计约束：

    * 游标只是**可再生的优化**：文件缺失/损坏/不可写都不抛异常，调用方按旧行为
      （``since=time.time()``）继续跑，绝不让磁盘问题挡住收消息；
    * 单调递增：``save()`` 只会把 ``last_ts`` 往前推，回放的旧消息不会把游标拉回去；
    * 原子写：同目录 ``*.tmp`` + ``os.replace``，避免半截 JSON。
    """

    def __init__(self, path: Path | str | None):
        self.path: Path | None = Path(path) if path else None
        self._lock = threading.Lock()
        self.last_ts = 0.0
        self.last_id = ""
        self.saved = 0
        self.last_error = ""

    def load(self) -> float | None:
        """读游标时间戳；文件不存在/无有效值 → ``None``（调用方退回旧行为）。"""
        if self.path is None:
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001 - 游标坏了按"没有游标"处理
            self.last_error = f"cursor_load_failed: {type(exc).__name__}: {exc}"[:200]
            return None
        if not isinstance(raw, dict):
            self.last_error = "cursor_load_failed: not a dict"
            return None
        try:
            ts = float(raw.get("last_ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts <= 0:
            return None
        self.last_ts = ts
        self.last_id = str(raw.get("last_id") or "")
        return ts

    def save(self, ts: object, msg_id: str = "") -> bool:
        """把游标推进到 ``ts``（只前进不后退）并原子落盘；失败返回 False 不抛异常。"""
        try:
            value = float(ts)
        except (TypeError, ValueError):
            return False
        if value <= 0:
            return False
        with self._lock:
            self.last_ts = max(float(self.last_ts or 0.0), value)
            if msg_id:
                self.last_id = str(msg_id)
            self.saved += 1
            if self.path is None:
                return False
            try:
                target = self.path
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".tmp")
                payload = {"version": CURSOR_VERSION,
                           "last_ts": self.last_ts,
                           "last_id": self.last_id,
                           "updated_at": time.time()}
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                tmp.replace(target)
                self.last_error = ""
                return True
            except Exception as exc:  # noqa: BLE001 - 写游标失败不影响主链
                self.last_error = f"cursor_save_failed: {type(exc).__name__}: {exc}"[:200]
                return False

    def status(self) -> dict:
        """观测快照（/status 用；不含任何聊天内容）。"""
        return {"path": str(self.path) if self.path else "",
                "last_ts": self.last_ts,
                "last_id": self.last_id,
                "saved": self.saved,
                "last_error": self.last_error}


class WeChatReceiver:
    """消费微信网关 backend 事件、只回调入站文本消息的接收适配器。

    参数
    ----
    backend:
        外部注入的真实后端（如 ``wx_core.monitor.MonitorBackend``），必须提供
        ``events``（``queue.Queue`` 风格，支持带 timeout 的 ``get``）。
        本类只读它的队列，不负责启停它。
    allowed_contacts:
        好友显示名白名单（可迭代对象；也接受单个字符串）。**空白名单表示不转发任何消息**
        （fail-closed，避免误把别人的消息送进网关）。匹配时去掉首尾空白，区分大小写。
    on_message:
        回调，参数是一条消息 dict（``Message.to_dict()`` 的浅拷贝，``friend`` 已 strip）。
        回调在消费线程里执行；回调抛异常只记录、不中断监听。
    since:
        可选时间下界（epoch 秒）。``None``（默认）表示不做时间过滤；显式传入时只转发
        ``ts`` 不早于它的消息，用于网关重启后不补处理历史消息。
        ``ts`` 缺失或为 0 时无法判断，按“不早于”处理。
    poll_interval:
        空队列时每次阻塞读取的秒数，也决定 ``stop()`` 的最长感知延迟。
    max_seen_ids:
        去重表最多保留多少条 id（FIFO 淘汰），避免长跑进程内存无限增长。
    thread_name:
        后台线程名，便于排查问题。
    mention_name:
        P22：机器人微信昵称；文本含 ``@昵称`` 时在回调 data 上打 ``mention=True``。
        空字符串（默认）= 不识别。
    """

    def __init__(
        self,
        backend,
        allowed_contacts=None,
        on_message: Callable[[dict], None] | None = None,
        since: float | None = None,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_seen_ids: int = DEFAULT_MAX_SEEN_IDS,
        thread_name: str = "wechat-receiver",
        mention_name: str = "",
    ):
        events = getattr(backend, "events", None)
        if events is None or not callable(getattr(events, "get", None)):
            raise TypeError("backend 必须提供 queue.Queue 风格的 events 队列（缺少 events.get）")
        if on_message is None or not callable(on_message):
            raise TypeError("on_message 必须是一个可调用对象")

        interval = float(poll_interval)
        if interval <= 0:
            raise ValueError("poll_interval 必须大于 0")
        limit = int(max_seen_ids)
        if limit < 1:
            raise ValueError("max_seen_ids 必须 >= 1")

        # 兼容旧字段（server.py 读 backend/allowed/on_message/running/status）
        self.backend = backend
        self.allowed = set(self._iter_contacts(allowed_contacts))
        self.on_message = on_message
        self.since = None if since is None else float(since)
        self.running = False
        self.last_error = ""
        self.received = 0   # 已回调的消息数
        self.ignored = 0    # 被过滤/去重丢弃的消息数
        # P22：私聊 @昵称 识别（空 = 不识别；匹配后在消息上打 mention=True 标记）
        self.mention_name = _clean(mention_name)

        self._queue = events
        self._poll_interval = interval
        self._max_seen = limit
        self._thread_name = str(thread_name) or "wechat-receiver"

        # 生命周期/统计：一把可重入锁，覆盖 start/stop 与计数，避免嵌套问题
        self._state_lock = threading.RLock()
        # 去重表：单独一把锁，消费线程高频访问，尽量不跟生命周期锁抢
        self._seen_lock = threading.Lock()
        self._seen: "OrderedDict[str, None]" = OrderedDict()

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._stats = {
            "events_received": 0,       # 从队列取出的事件数（含非 message 事件）
            "messages_forwarded": 0,    # 通过全部过滤并尝试回调的消息数
            "messages_filtered": 0,     # 是 message 但被 sender/kind/白名单/空文本/时间过滤
            "non_message_ignored": 0,   # 非 message 事件（status/log/friends…）
            "duplicates_dropped": 0,    # 命中 id 去重
            "callback_errors": 0,       # on_message 抛异常次数
        }
        self._last_filter_reason = ""

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动后台消费线程（重复调用无副作用）。"""
        with self._state_lock:
            # 只在没有活线程时启动：旧线程还在退出（例如回调卡住）时不抢着起第二个消费者
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._run, name=self._thread_name, daemon=True
            )
            # 在锁内 start，避免两个线程同时 start() 各起一个消费者
            try:
                thread.start()
            except BaseException:
                self._thread = None
                self.running = False
                raise
            self._thread = thread
            self.running = True

    def stop(self, timeout: float | None = None) -> None:
        """停止消费线程并等待其退出（重复调用、未启动时调用都安全）。

        ``stop()`` 正常返回后后台线程已结束，不会再消费 ``backend.events``。
        """
        with self._state_lock:
            self.running = False
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # 在锁外 join：回调里调用 stop()/status() 不会死锁
            thread.join(
                timeout=timeout if timeout is not None else max(1.0, self._poll_interval * 5)
            )
        with self._state_lock:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None
            # 线程仍活着（比如回调卡住）时保留引用，防止 start() 又起一个消费者

    def is_running(self) -> bool:
        """后台线程是否仍在消费事件。"""
        with self._state_lock:
            return self._alive()

    def process_event(self, event) -> dict | None:
        """同步处理单个事件；返回回调的 payload，未转发时返回 ``None``。

        后台线程内部走的就是这个方法，所以同步调用与异步消费遵守完全相同的过滤/去重规则。
        """
        self._bump("events_received")
        data = self._accept(event)
        if data is None:
            return None

        if not self._remember(self._dedupe_key(data)):
            self._bump("duplicates_dropped")
            self._count_ignored()
            return None

        payload = dict(data)
        payload["friend"] = _clean(data.get("friend"))  # 白名单匹配用归一化后的名字

        with self._state_lock:
            self._stats["messages_forwarded"] += 1
            self.received += 1
        try:
            self.on_message(payload)
        except Exception as exc:  # noqa: BLE001 - 回调出错只记录，不能拖垮监听线程
            with self._state_lock:
                self._stats["callback_errors"] += 1
                self.last_error = f"on_message 回调异常: {type(exc).__name__}: {exc}"
        return payload

    # 旧名字（server 之外的历史代码/测试在用），行为与 process_event 一致
    def handle_event(self, event) -> dict | None:
        return self.process_event(event)

    def status(self) -> dict:
        """当前状态/统计快照（副本，可安全打印或塞进 /status 响应）。

        保留旧字段 ``running / received / ignored / allowed / last_error``，
        另外补充更细的统计，方便网关排查。
        """
        with self._seen_lock:
            seen_ids = len(self._seen)
        with self._state_lock:
            data = dict(self._stats)
            data.update({
                "running": self._alive(),
                "received": self.received,
                "ignored": self.ignored,
                "allowed": sorted(self.allowed),
                "last_error": self.last_error,
                "last_filtered_reason": self._last_filter_reason,
                "seen_ids": seen_ids,
                "since": self.since,
            })
        return data

    # ------------------------------------------------------------------
    # A7：启动补采（游标 → 启动时刻之间的入站消息）
    # ------------------------------------------------------------------
    def catch_up(self, loader: Callable[..., object], *, since_ts: float,
                 until_ts: float | None = None,
                 max_items: int = DEFAULT_CATCHUP_MAX_ITEMS,
                 window_s: float = DEFAULT_CATCHUP_WINDOW_S,
                 exclude_id: str = "") -> dict:
        """把 ``since_ts`` 之后、``until_ts`` 之前的入站消息补进同一条过滤链。

        ``loader(since, until, limit)`` 返回消息 **data dict** 列表（``Message.to_dict()``
        形态），或 ``None`` 表示后端数据库还没就绪（调用方稍后重试）。

        ``exclude_id``：游标里记录的最后一条已处理消息 id。它的 ts 正好等于游标，
        若只按时间过滤会被重复回放（重启后内存去重表已清空），因此按 id 精确跳过。

        复用 :meth:`process_event`：白名单 / kind / sender / 去重 / ``since`` 时间下界
        完全一致，不另开语义；补采只是"另一条把事件喂进来的路径"。
        """
        stats: dict = {"ok": False, "ready": False, "loaded": 0, "forwarded": 0,
                       "duplicates": 0, "filtered": 0, "skipped_cursor": 0,
                       "since": float(since_ts or 0.0), "until": 0.0,
                       "max_items": int(max_items), "window_s": float(window_s),
                       "error": ""}
        now = time.time()
        try:
            until = float(until_ts if until_ts is not None else now)
        except (TypeError, ValueError):
            until = now
        if until <= 0:
            until = now
        # 灾难保护：游标太久远时把补采下界截到 window_s 以内（默认 24h）
        floor = now - max(0.0, float(window_s))
        try:
            since = max(float(since_ts or 0.0), floor)
        except (TypeError, ValueError):
            since = floor
        stats["since"] = since
        stats["until"] = until
        try:
            limit = max(1, min(int(max_items), 1000))
        except (TypeError, ValueError):
            limit = DEFAULT_CATCHUP_MAX_ITEMS
        try:
            rows = loader(since, until, limit)
        except Exception as exc:  # noqa: BLE001 - 补采失败只记录，不影响正常收消息
            stats["error"] = f"catchup_loader_failed: {type(exc).__name__}: {exc}"[:200]
            return stats
        if rows is None:
            stats["error"] = "backend_not_ready"
            return stats
        with self._state_lock:
            before = dict(self._stats)
        try:
            items = list(rows)
        except TypeError:
            items = []
        for data in items[:limit]:
            stats["loaded"] += 1
            if not isinstance(data, dict):
                continue
            try:
                ts = float(data.get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts and not (since <= ts <= until):
                continue
            # 游标里那条"最后已处理"的消息 ts == since，时间窗过滤挡不住，按 id 精确跳过
            if exclude_id and str(data.get("id") or "") == str(exclude_id):
                stats["skipped_cursor"] += 1
                continue
            try:
                self.process_event({"type": EVENT_MESSAGE, "data": dict(data)})
            except Exception as exc:  # noqa: BLE001 - 单条坏数据只丢自己
                self.last_error = (
                    f"catchup_process_failed: {type(exc).__name__}: {exc}"[:200])
        with self._state_lock:
            after = dict(self._stats)
        stats["forwarded"] = max(0, int(after.get("messages_forwarded", 0))
                                 - int(before.get("messages_forwarded", 0)))
        stats["duplicates"] = max(0, int(after.get("duplicates_dropped", 0))
                                  - int(before.get("duplicates_dropped", 0)))
        stats["filtered"] = max(0, int(after.get("messages_filtered", 0))
                                - int(before.get("messages_filtered", 0)))
        stats["ok"] = True
        stats["ready"] = True
        return stats

    # 便于只读观察
    @property
    def thread_name(self) -> str:
        return self._thread_name

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    def __enter__(self) -> "WeChatReceiver":
        self.start()
        return self

    def __exit__(self, *exc_info) -> bool:
        self.stop()
        return False

    def __repr__(self) -> str:
        with self._seen_lock:
            seen = len(self._seen)
        return (
            f"<WeChatReceiver running={self.is_running()} "
            f"allowed={len(self.allowed)} seen={seen}>"
        )

    # ------------------------------------------------------------------
    # 后台线程
    # ------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=self._poll_interval)
            except queue.Empty:
                continue
            except Exception as exc:  # noqa: BLE001 - 队列异常不能让线程疯狂空转
                with self._state_lock:
                    self.last_error = f"读取 backend.events 失败: {type(exc).__name__}: {exc}"
                self._stop.wait(self._poll_interval)
                continue

            # 取出后才发现已停止：把事件放回队列，保证 stop 之后确实「没消费」
            if self._stop.is_set():
                self._requeue(event)
                break

            try:
                self.process_event(event)
            except Exception as exc:  # noqa: BLE001 - 单条坏事件只丢自己
                with self._state_lock:
                    self.last_error = f"处理事件失败: {type(exc).__name__}: {exc}"

    def _requeue(self, event) -> None:
        """尽力把事件放回队列（队列不支持或已满时放弃，不影响停止）。"""
        put = getattr(self._queue, "put_nowait", None)
        if not callable(put):
            return
        try:
            put(event)
        except Exception:  # noqa: BLE001 - 队列满等情况下丢弃
            pass

    # ------------------------------------------------------------------
    # 过滤 / 去重 / 统计
    # ------------------------------------------------------------------
    @staticmethod
    def _iter_contacts(allowed_contacts) -> tuple[str, ...]:
        """把白名单参数统一成去空白的名字元组（None → 空，单个字符串 → 一项）。"""
        if allowed_contacts is None:
            return ()
        if isinstance(allowed_contacts, str):
            return (_clean(allowed_contacts),)
        try:
            items = list(allowed_contacts)
        except TypeError:
            raise TypeError("allowed_contacts 必须是字符串或可迭代的好友名集合") from None
        return tuple(_clean(item) for item in items if _clean(item))

    def _accept(self, event) -> dict | None:
        """过滤事件；通过时返回 ``data`` dict，否则返回 None（并记统计）。"""
        if not isinstance(event, dict):
            self._bump("non_message_ignored")
            return None
        if event.get("type") != EVENT_MESSAGE:
            self._bump("non_message_ignored")
            return None

        data = event.get("data")
        if not isinstance(data, dict):
            self._mark_filtered("data")
            return None

        reason = self._reject_reason(data)
        if reason:
            self._mark_filtered(reason)
            return None
        # P22：@提及标记（不改原文，由 server 统一加 [对方@了你] 前缀）
        data = dict(data)
        if _clean(data.get("kind")) == KIND_TEXT and is_mention(
                data.get("text"), self.mention_name):
            data["mention"] = True
            data["mention_name"] = self.mention_name
        # P29：真机拍一拍可能以 kind=text 送达（带引号/「」的短文案）→ 打 pat 标记
        if looks_like_pat(data.get("text")):
            data["pat"] = True
        return data

    def _reject_reason(self, data: dict) -> str:
        """返回不能转发的理由（空字符串表示可以转发）。"""
        if _clean(data.get("sender")) != SENDER_FRIEND:
            return "sender"
        kind = _clean(data.get("kind"))
        if kind not in ("text", "image", "emoji", "system"):
            return "kind"

        friend = _clean(data.get("friend"))
        if not friend:
            return "friend"
        if friend not in self.allowed:
            return "not_allowed"

        text = data.get("text")
        if kind == "system":
            # P22：撤回 / 引用 / 拍了拍 是**事件**不是用户说的话，需要放行给 server 转写
            if not isinstance(text, str) or not any(
                    k in text for k in SYSTEM_PASSTHROUGH_HINTS):
                return "system"
        elif kind == "text":
            if not isinstance(text, str) or not text.strip():
                return "text"
        # image/emoji 允许空 text；media_path 由网关读取
        if self._is_stale(data):
            return "stale"
        return ""

    def _is_stale(self, data: dict) -> bool:
        """显式指定 since 时，判断这条消息是否早于下界（ts 缺失/为 0 时无法判断）。"""
        if self.since is None:
            return False
        try:
            ts = float(data.get("ts") or 0.0)
        except (TypeError, ValueError):
            return False
        return 0.0 < ts < self.since

    def _dedupe_key(self, data: dict) -> str:
        """消息去重键：优先用 id，缺失时退回 friend/text/ts 的稳定组合。"""
        raw_id = data.get("id")
        if isinstance(raw_id, str):
            key = raw_id.strip()
        elif raw_id is None:
            key = ""
        else:
            key = str(raw_id).strip()
        if key:
            return key
        return (
            "no-id|"
            f"{_clean(data.get('friend'))!r}|"
            f"{data.get('text')!r}|"
            f"{data.get('ts')!r}"
        )

    def _remember(self, key: str) -> bool:
        """登记去重键；首次出现返回 True，重复返回 False。"""
        with self._seen_lock:
            if key in self._seen:
                self._seen.move_to_end(key)
                return False
            self._seen[key] = None
            while len(self._seen) > self._max_seen:
                self._seen.popitem(last=False)
            return True

    def _alive(self) -> bool:
        """线程是否还在跑（调用方需持有 _state_lock，RLock 可重入）。"""
        return bool(
            self.running
            and self._thread is not None
            and self._thread.is_alive()
        )

    def _bump(self, key: str) -> None:
        with self._state_lock:
            self._stats[key] = int(self._stats.get(key, 0)) + 1

    def _count_ignored(self) -> None:
        with self._state_lock:
            self.ignored += 1

    def _mark_filtered(self, reason: str) -> None:
        with self._state_lock:
            self._stats["messages_filtered"] += 1
            self.ignored += 1
            self._last_filter_reason = str(reason)
