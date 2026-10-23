"""后端事件桥：消费 ``wx_core`` 后端事件、维护网页快照、向 SSE 订阅者广播。

设计要点：
* ``wx_core`` 后端的 ``events`` 队列只允许一个消费者，网页端启动时由本模块独占消费；
* 所有数据在内存里保留一份有界快照（消息最多 ``max_messages`` 条、日志 600 条），
  新连接先拿 :meth:`WebBridge.snapshot` 首屏快照，再订阅增量事件；
* 广播事件统一补 ``ts`` 信封字段，方便前端排序/去重；
* 线程模型：一个消费线程 + 每个 SSE 连接一个 ``queue.Queue``，全部读写都在锁内完成，
  广播时先复制订阅者列表再入队，避免慢客户端卡住采集。
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque

# 日志最多保留条数（与桌面界面量级一致，足够排障）
MAX_LOGS = 600
# 单个 SSE 订阅者的缓冲上限；超过说明前端卡住了，丢弃并通知它重新拉快照
SUB_QUEUE_SIZE = 512


class WebBridge:
    """把 ``Backend`` 事件流转换成网页需要的线程安全快照 + 广播。

    :param backend: 满足 ``wx_core.api.Backend`` 协议的后端（真实后端或 ``DevBackend``）。
    :param mode: ``"real"``（真实微信）或 ``"fake"``（演示）。
    :param started_at: 服务启动时间（epoch 秒），默认当前时间。
    :param max_messages: 内存中保留的消息条数上限。
    """

    def __init__(self, backend, *, mode: str, started_at: float | None = None,
                 max_messages: int = 3000):
        self.backend = backend
        self.mode = str(mode or "real")
        self.started_at = float(started_at or time.time())
        self.max_messages = max(10, int(max_messages or 3000))

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False

        self._state: dict = {}
        self._friends: list[dict] = []
        self._messages: deque[dict] = deque(maxlen=self.max_messages)
        self._message_ids: set[str] = set()
        self._message_order: deque[str] = deque()
        self._logs: deque[dict] = deque(maxlen=MAX_LOGS)
        self._per_friend_counts: dict[str, int] = {}
        self._per_friend_last: dict[str, float] = {}
        self._total = 0

        self._subs: dict[int, "queue.Queue[dict]"] = {}
        self._next_sub = 1

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动消费线程（幂等）。"""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="wxbox-web-bridge", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """停止消费线程并通知所有订阅者断开（不 stop 底层 backend）。"""
        with self._lock:
            self._running = False
            self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        with self._lock:
            subs = list(self._subs.values())
            self._subs.clear()
        for q in subs:
            self._put(q, {"type": "bye", "data": {"reason": "server_stopped"}, "ts": time.time()})

    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # 订阅 / 广播
    # ------------------------------------------------------------------
    def subscribe(self) -> tuple["queue.Queue[dict]", int]:
        """注册一个 SSE 订阅者，返回 ``(队列, 订阅 id)``。"""
        q: "queue.Queue[dict]" = queue.Queue(maxsize=SUB_QUEUE_SIZE)
        with self._lock:
            sub_id = self._next_sub
            self._next_sub += 1
            self._subs[sub_id] = q
        return q, sub_id

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            self._subs.pop(int(sub_id), None)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def broadcast(self, event: dict) -> None:
        """向所有 SSE 订阅者广播一个事件；事件缺 ``ts`` 时补上。

        订阅者队列满（前端消费太慢）时丢弃排队事件并发一条 ``resync``，
        让前端重新拉 bootstrap，保证不会因为慢客户端拖垮采集。
        """
        if not isinstance(event, dict):
            return
        payload = dict(event)
        payload.setdefault("ts", time.time())
        with self._lock:
            subs = list(self._subs.values())
        for q in subs:
            self._put(q, payload)

    @staticmethod
    def _put(q: "queue.Queue[dict]", event: dict) -> None:
        try:
            q.put_nowait(event)
        except queue.Full:
            # 慢客户端：清空积压，只留一条 resync 提示
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
            try:
                q.put_nowait({"type": "resync", "data": {"reason": "overflow"}, "ts": time.time()})
            except queue.Full:  # pragma: no cover - 极小概率
                pass

    # ------------------------------------------------------------------
    # 消费后端事件
    # ------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event = self.backend.events.get(timeout=0.2)
            except queue.Empty:
                continue
            except Exception:  # noqa: BLE001 - backend 队列异常不应让网页端线程退出
                time.sleep(0.1)
                continue
            try:
                self._handle(event)
            except Exception:  # noqa: BLE001 - 单条坏事件只丢自己
                continue

    def _handle(self, event: dict) -> None:
        etype = str((event or {}).get("type") or "")
        data = event.get("data") if isinstance(event, dict) else None
        if etype == "status" and isinstance(data, dict):
            with self._lock:
                self._state = dict(data)
            self.broadcast({"type": "status", "data": dict(data)})
        elif etype == "friends" and isinstance(data, dict):
            friends = [dict(f) for f in (data.get("friends") or []) if isinstance(f, dict)]
            with self._lock:
                self._friends = friends
            self.broadcast({"type": "friends", "data": dict(data)})
        elif etype == "message" and isinstance(data, dict):
            self._add_message(data)
        elif etype == "log" and isinstance(data, dict):
            entry = {
                "ts": time.time(),
                "level": str(data.get("level") or "info"),
                "text": str(data.get("text") or ""),
            }
            with self._lock:
                self._logs.append(entry)
            self.broadcast({"type": "log", "data": entry})
        else:
            # 未知类型原样转发，方便后续扩展（如 moments_*）
            self.broadcast(event)

    def _add_message(self, data: dict, *, broadcast: bool = True) -> bool:
        msg = dict(data)
        # 统一清洗微信系统提示/应用消息（红包、转账、HTML、appmsg XML），
        # 避免网页显示原始标签。
        try:
            from webui.wechat_text import db_message_id, normalize_message

            kind, text, extra = normalize_message(
                str(msg.get("kind") or ""), str(msg.get("text") or ""),
                msg.get("extra") if isinstance(msg.get("extra"), dict) else {},
            )
            msg["kind"] = kind
            msg["text"] = text
            msg["extra"] = extra
            # 实时与“同步历史”的同一条 DB 消息用统一 id，避免重复显示
            local_id = extra.get("db_local_id")
            if local_id:
                msg["id"] = db_message_id(str(msg.get("friend") or ""), local_id)
        except Exception:  # noqa: BLE001 - 清洗失败不影响原消息
            pass
        msg_id = str(msg.get("id") or "").strip()
        with self._lock:
            if msg_id and msg_id in self._message_ids:
                return False
            if msg_id:
                self._message_ids.add(msg_id)
                self._message_order.append(msg_id)
                if len(self._message_order) > self.max_messages:
                    old = self._message_order.popleft()
                    self._message_ids.discard(old)
            self._messages.append(msg)
            self._total += 1
            friend = str(msg.get("friend") or "")
            if friend:
                self._per_friend_counts[friend] = self._per_friend_counts.get(friend, 0) + 1
                ts = float(msg.get("ts") or time.time())
                self._per_friend_last[friend] = max(self._per_friend_last.get(friend, 0.0), ts)
        if broadcast:
            self.broadcast({"type": "message", "data": msg})
        return True

    def replace_friends(self, friends: list[dict]) -> int:
        """用新的好友/群聊列表替换内存列表并广播（供“刷新列表”使用）。"""
        normalized: list[dict] = []
        seen: set[str] = set()
        for item in friends or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            normalized.append(dict(item))
        with self._lock:
            self._friends = normalized
            # 严格只保留新列表里的会话计数，避免旧账号残留名字被 snapshot 的
            # extra_names 逻辑补回好友列表
            valid = {str(item.get("name") or "") for item in normalized}
            self._per_friend_counts = {
                name: count for name, count in self._per_friend_counts.items() if name in valid
            }
            self._per_friend_last = {
                name: ts for name, ts in self._per_friend_last.items() if name in valid
            }
        self.broadcast({"type": "friends",
                        "data": {"total": len(normalized), "friends": normalized}})
        return len(normalized)

    def clear_friends(self) -> int:
        """清空内存中的好友/群聊列表并广播空列表。"""
        with self._lock:
            cleared = len(self._friends)
            self._friends = []
        self.broadcast({"type": "friends", "data": {"total": 0, "friends": []}})
        return cleared

    def reset_backend_db(self, account_wxid: str = "") -> None:
        """账号切换/手动刷新后，让采集线程用当前账号重建数据库直读桥。"""
        self._reset_backend_db()
        try:
            cfg = getattr(self.backend, "config", None)
            if cfg is not None and account_wxid:
                cfg["image_wxid"] = account_wxid
                cfg["image_xor_key"] = ""
                cfg["image_aes_key"] = ""
                cfg["image_cfg_dword"] = ""
                cfg["friends_cache"] = []
        except Exception:  # noqa: BLE001
            pass

    def add_messages(self, messages: list[dict], *, broadcast: bool = True) -> int:
        """批量把历史消息加入内存（去重），返回真正新增的条数。

        供历史聊天记录同步使用：消息只在本进程内存里，程序退出即消失；
        默认逐条广播 ``message`` 事件，多标签页/刷新后的页面能收到。
        """
        added = 0
        for item in messages or []:
            if not isinstance(item, dict):
                continue
            try:
                if self._add_message(item, broadcast=broadcast):
                    added += 1
            except Exception:  # noqa: BLE001 - 单条坏数据只跳过
                continue
        return added

    def clear_messages(self, *, include_logs: bool = True) -> int:
        """清空内存中的消息、计数与（可选）日志，返回清掉的消息条数。

        只影响网页进程内存；不删除 ``log/``/``media/`` 里桌面版自己的文件，
        也不影响正在运行的监听后端。
        """
        with self._lock:
            cleared = len(self._messages)
            self._messages.clear()
            self._message_ids.clear()
            self._message_order.clear()
            self._per_friend_counts.clear()
            self._per_friend_last.clear()
            self._total = 0
            if include_logs:
                self._logs.clear()
        return cleared

    # ------------------------------------------------------------------
    # 快照 / 查询
    # ------------------------------------------------------------------
    def state(self) -> dict:
        """当前状态快照（backend 状态 + 网页端统计）。"""
        with self._lock:
            state = dict(self._state)
            state["message_total"] = self._total
            state["per_friend_counts"] = dict(self._per_friend_counts)
            state.setdefault("monitoring", [])
        return state

    def snapshot(self) -> dict:
        """首屏快照：状态 / 好友 / 消息 / 日志。"""
        with self._lock:
            state = dict(self._state)
            state["message_total"] = self._total
            state["per_friend_counts"] = dict(self._per_friend_counts)
            state.setdefault("monitoring", [])
            state.setdefault("running", bool(getattr(self.backend, "is_running", lambda: True)()))
            state.setdefault("wechat_found", False)
            state.setdefault("chat_title", "")
            state.setdefault("error", None)
            state.setdefault("tick", 0)

            names = [str(f.get("name") or "") for f in self._friends if str(f.get("name") or "")]
            extra_names = [n for n in self._per_friend_counts if n and n not in names]
            friends = []
            for base in self._friends:
                name = str(base.get("name") or "")
                if not name:
                    continue
                item = dict(base)
                item["count"] = self._per_friend_counts.get(name, 0)
                item["last_ts"] = self._per_friend_last.get(name, 0.0)
                friends.append(item)
            for name in extra_names:
                friends.append({
                    "name": name,
                    "wxid": "",
                    "letter": "",
                    "count": self._per_friend_counts.get(name, 0),
                    "last_ts": self._per_friend_last.get(name, 0.0),
                })
            friends.sort(key=lambda f: str(f.get("name") or ""))

            messages = list(self._messages)
            # 按消息自身时间排序；时间相同保持到达顺序（sort 稳定）
            messages.sort(key=lambda m: float(m.get("ts") or 0.0))
            logs = list(self._logs)
            return {
                "state": state,
                "friends": friends,
                "messages": messages,
                "logs": logs,
            }

    def scope_messages(self, scope: dict, limit: int = 80) -> list[dict]:
        """按范围取最近 ``limit`` 条消息（时间升序），供 LLM 拼上下文。

        scope::

            {"type": "friend", "friend": "白云一片去悠悠"}
            {"type": "all"}
            {"type": "monitored", "friend_names": ["A", "B"]}   # friend_names 可选
        """
        scope = dict(scope or {})
        stype = str(scope.get("type") or "all")
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 80
        limit = max(1, min(limit, 2000))
        with self._lock:
            messages = list(self._messages)
            monitoring = [str(n) for n in (self._state.get("monitoring") or [])]
        if stype == "friend":
            target = str(scope.get("friend") or "")
            picked = [m for m in messages if str(m.get("friend") or "") == target]
        elif stype == "monitored":
            names = [str(n) for n in (scope.get("friend_names") or monitoring)]
            nameset = set(names)
            picked = [m for m in messages if str(m.get("friend") or "") in nameset]
        else:
            picked = list(messages)
        picked.sort(key=lambda m: float(m.get("ts") or 0.0))
        return picked[-limit:]

    # ------------------------------------------------------------------
    # 控制后端
    # ------------------------------------------------------------------
    def set_monitored(self, names: list[str]) -> dict:
        clean = [str(n).strip() for n in (names or []) if str(n).strip()]
        self.backend.set_monitored(clean)
        # 核心 MonitorBackend 的数据库直读桥只在第一次启动时按当时的监听名单建一次；
        # 之后在网页上勾选新的好友/群聊，DbBridge.sources 不会自动增加，导致新会话
        # 的实时消息收不到（只能靠“同步历史”）。这里在名单变化后丢弃旧 DbBridge，
        # 让采集线程用最新名单重新建立直读桥（下一次 _tick 自动 _init_db）。
        self._reset_backend_db()
        with self._lock:
            self._state["monitoring"] = list(clean)
        return {"ok": True, "monitoring": clean}

    def _reset_backend_db(self) -> None:
        """让后端下一次轮询用最新监听名单重建数据库直读桥（只读、幂等）。"""
        backend = self.backend
        db = getattr(backend, "_db", None)
        if db is None:
            return
        lock = getattr(backend, "_lock", None)
        acquired = False
        if lock is not None:
            try:
                lock.acquire()
                acquired = True
            except Exception:  # noqa: BLE001
                acquired = False
        try:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                backend._db = None
                if hasattr(backend, "_db_failed"):
                    backend._db_failed = False
            except Exception:  # noqa: BLE001
                pass
        finally:
            if acquired:
                try:
                    lock.release()
                except Exception:  # noqa: BLE001
                    pass

    def refresh_friends(self) -> dict:
        try:
            self.backend.list_friends(refresh=True)
        except Exception as exc:  # noqa: BLE001 - 好友枚举失败返回可读错误
            return {"ok": False, "error": f"重新同步好友失败：{exc}"}
        return {"ok": True}

    def start_backend(self) -> dict:
        try:
            self.backend.start()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"启动监听失败：{exc}"}
        return {"ok": True, "running": True}

    def stop_backend(self) -> dict:
        try:
            self.backend.stop()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"停止监听失败：{exc}"}
        # MonitorBackend.stop() 会关闭 DbBridge 但不置空；置空后下次 start 会用当前
        # 监听名单重新建立数据库直读桥，避免“停止后再开始”收不到消息。
        try:
            if hasattr(self.backend, "_db"):
                self.backend._db = None
            if hasattr(self.backend, "_db_failed"):
                self.backend._db_failed = False
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            self._state["running"] = False
        return {"ok": True, "running": False}
