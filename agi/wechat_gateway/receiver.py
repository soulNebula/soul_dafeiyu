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
3. ``data.kind == "text"``（图片/表情/文件/语音等一律不转发）；
4. ``data.friend`` 在白名单内；
5. ``data.text`` 非空白；
6. 按 ``data.id`` 去重（同一条消息只回调一次；没有 id 时用 friend/text/ts 兜底去重）。

线程模型
--------
``start()`` 起一个 daemon 后台线程，阻塞读 ``backend.events``（带 timeout 以便感知停止）；
``stop()`` 置停止位并 join 该线程，返回后保证不再消费队列里的事件。
生命周期状态、统计、去重表分别由锁保护，``start()`` / ``stop()`` 可重复调用，
也可以在任意线程（包括回调内部）安全调用。
"""
from __future__ import annotations

import queue
import threading
from collections import OrderedDict
from typing import Callable

__all__ = ["WeChatReceiver"]

EVENT_MESSAGE = "message"
SENDER_FRIEND = "friend"
KIND_TEXT = "text"

DEFAULT_POLL_INTERVAL = 0.2
DEFAULT_MAX_SEEN_IDS = 4096


def _clean(value: object) -> str:
    """把事件字段规整成去首尾空白的 str（非字符串一律视为空）。"""
    return value.strip() if isinstance(value, str) else ""


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
            if not isinstance(text, str) or not any(k in text for k in ("撤回", "引用消息")):
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
