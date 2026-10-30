# -*- coding: utf-8 -*-
"""801 网关接收适配器：只转发白名单好友的入站文本消息。

全部用 queue.Queue 假后端，不 import wxauto、不起真实微信、不发任何消息。
"""
from __future__ import annotations

import queue
import threading
import time

import pytest

from agi.wechat_gateway.receiver import ReceiveCursor, WeChatReceiver


class FakeBackend:
    """最小后端：只有 events 队列；start/stop 用来断言接收器不碰后端生命周期。"""

    def __init__(self):
        self.events = queue.Queue()
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1


def message(
    friend="小明",
    text="在吗",
    *,
    sender="friend",
    kind="text",
    msg_id="m-1",
    ts=1_700_000_000.0,
):
    """构造一条符合 Backend 事件协议的 message 事件。"""
    return {
        "type": "message",
        "data": {
            "friend": friend,
            "sender": sender,
            "text": text,
            "kind": kind,
            "ts": ts,
            "id": msg_id,
        },
    }


def wait_for(predicate, timeout=3.0):
    """等待条件成立（消费线程是异步的），返回是否在超时前成立。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def alive_consumers(receiver):
    return [t for t in threading.enumerate()
            if t.name == receiver.thread_name and t.is_alive()]


@pytest.fixture
def receiver_factory():
    """按需造接收器；测试结束统一 stop，避免线程泄漏。"""
    created = []

    def make(*, backend=None, allowed=("小明",), on_message=None, **kwargs):
        backend = backend if backend is not None else FakeBackend()
        messages = []
        callback = on_message if on_message is not None else messages.append
        receiver = WeChatReceiver(
            backend, allowed, callback, poll_interval=0.01, **kwargs
        )
        created.append(receiver)
        return backend, receiver, messages

    yield make
    for receiver in created:
        receiver.stop()


def test_friend_text_is_forwarded(receiver_factory):
    backend, receiver, messages = receiver_factory()
    receiver.start()

    event = message(text="晚上一起吃饭？", msg_id="m-1")
    backend.events.put(event)

    assert wait_for(lambda: len(messages) == 1)
    payload = messages[0]
    assert payload["friend"] == "小明"
    assert payload["text"] == "晚上一起吃饭？"
    assert payload["sender"] == "friend"
    assert payload["kind"] == "text"
    assert payload["id"] == "m-1"
    assert payload is not event["data"]  # 回调拿到的是副本

    status = receiver.status()
    assert status["running"] is True
    assert status["events_received"] == 1
    assert status["messages_forwarded"] == 1
    assert status["received"] == 1
    # 红线：接收器不创建/不启停真实后端
    assert backend.start_calls == 0
    assert backend.stop_calls == 0


def test_only_inbound_text_messages_are_forwarded(receiver_factory):
    backend, receiver, messages = receiver_factory()
    receiver.start()

    for event in (
        message(text="我自己发的", sender="self", msg_id="self-1"),
        message(kind="image", text="[图片]", msg_id="img-1"),
        message(kind="emoji", text="[微笑]", msg_id="emoji-1"),
        message(text="   \n\t", msg_id="blank-1"),
        {"type": "status", "data": {"running": True}},
        {"type": "log", "data": {"level": "info", "text": "监听服务已启动"}},
        {"type": "friends", "data": {"total": 1, "friends": [{"name": "小明"}]}},
        "not-a-dict",
    ):
        backend.events.put(event)
    backend.events.put(message(text="入站哨兵", msg_id="ok-1"))

    assert wait_for(lambda: len(messages) == 3)
    assert [m["id"] for m in messages] == ["img-1", "emoji-1", "ok-1"]

    status = receiver.status()
    assert status["messages_forwarded"] == 3
    assert status["messages_filtered"] == 2
    assert status["non_message_ignored"] == 4
    assert status["ignored"] == 2


def test_contact_outside_whitelist_is_ignored(receiver_factory):
    backend, receiver, messages = receiver_factory(allowed=("小明", "阿强"))
    receiver.start()

    backend.events.put(message(friend="小红", text="不在白名单", msg_id="out-1"))
    backend.events.put(message(friend="小明 ", text="白名单内（名字带空格）", msg_id="in-1"))

    assert wait_for(lambda: len(messages) == 1)
    assert messages[0]["id"] == "in-1"
    assert messages[0]["friend"] == "小明"  # 回调里的 friend 已 strip
    assert receiver.status()["last_filtered_reason"] == "not_allowed"
    assert receiver.status()["messages_filtered"] == 1


def test_empty_whitelist_forwards_nothing(receiver_factory):
    backend, receiver, messages = receiver_factory(allowed=())
    receiver.start()

    backend.events.put(message(msg_id="any-1"))

    assert wait_for(lambda: receiver.status()["messages_filtered"] == 1)
    assert messages == []
    assert receiver.status()["events_received"] == 1
    assert receiver.status()["messages_forwarded"] == 0


def test_duplicate_message_id_is_forwarded_once(receiver_factory):
    backend, receiver, messages = receiver_factory()
    receiver.start()

    backend.events.put(message(text="重复一", msg_id="dup-1"))
    backend.events.put(message(text="重复二", msg_id="dup-1"))
    backend.events.put(message(text="新消息", msg_id="new-1"))

    assert wait_for(lambda: len(messages) == 2)
    assert [m["id"] for m in messages] == ["dup-1", "new-1"]
    assert receiver.status()["duplicates_dropped"] == 1
    assert receiver.status()["messages_forwarded"] == 2


def test_stop_halts_consumption(receiver_factory):
    backend, receiver, messages = receiver_factory()
    receiver.start()

    backend.events.put(message(msg_id="before-stop"))
    assert wait_for(lambda: len(messages) == 1)

    receiver.stop()
    assert receiver.is_running() is False
    assert alive_consumers(receiver) == []

    # 停止后再来的消息不再消费，而是留在队列里
    backend.events.put(message(text="stop 之后", msg_id="after-stop"))
    time.sleep(0.1)
    assert len(messages) == 1
    assert backend.events.qsize() == 1
    assert receiver.status()["events_received"] == 1

    receiver.stop()  # 重复 stop 安全
    assert receiver.status()["running"] is False


def test_start_is_idempotent(receiver_factory):
    backend, receiver, messages = receiver_factory()
    receiver.start()
    receiver.start()

    assert receiver.is_running() is True
    assert len(alive_consumers(receiver)) == 1

    backend.events.put(message(msg_id="once-1"))
    assert wait_for(lambda: len(messages) == 1)
    assert [m["id"] for m in messages] == ["once-1"]


def test_restart_after_stop_consumes_again(receiver_factory):
    backend, receiver, messages = receiver_factory()
    receiver.start()
    backend.events.put(message(msg_id="first-run"))
    assert wait_for(lambda: len(messages) == 1)

    receiver.stop()
    receiver.start()
    backend.events.put(message(msg_id="second-run"))

    assert wait_for(lambda: len(messages) == 2)
    assert [m["id"] for m in messages] == ["first-run", "second-run"]


def test_callback_error_does_not_kill_consumer(receiver_factory):
    seen = []

    def on_message(payload):
        seen.append(payload)
        if payload["id"] == "boom-1":
            raise RuntimeError("boom")

    backend, receiver, _ = receiver_factory(on_message=on_message)
    receiver.start()

    backend.events.put(message(msg_id="boom-1"))
    backend.events.put(message(msg_id="boom-2"))

    assert wait_for(lambda: len(seen) == 2)
    assert receiver.is_running() is True
    status = receiver.status()
    assert status["callback_errors"] == 1
    assert "boom" in status["last_error"]


def test_dedupe_memory_is_bounded(receiver_factory):
    backend, receiver, messages = receiver_factory(max_seen_ids=2)
    receiver.start()

    for msg_id in ("a", "b", "c", "a"):
        backend.events.put(message(msg_id=msg_id))

    # 只记最近 2 个 id，所以 "a" 被淘汰后会再次转发
    assert wait_for(lambda: len(messages) == 4)
    assert [m["id"] for m in messages] == ["a", "b", "c", "a"]
    assert receiver.status()["seen_ids"] == 2


def test_since_rejects_older_messages(receiver_factory):
    backend, receiver, messages = receiver_factory(since=1000.0)
    receiver.start()

    backend.events.put(message(text="历史消息", msg_id="old-1", ts=900.0))
    backend.events.put(message(text="新消息", msg_id="new-1", ts=1100.0))

    assert wait_for(lambda: len(messages) == 1)
    assert [m["id"] for m in messages] == ["new-1"]
    assert receiver.status()["last_filtered_reason"] == "stale"


def test_requires_injected_backend_and_callback():
    with pytest.raises(TypeError):
        WeChatReceiver(object(), {"小明"}, lambda msg: None)
    with pytest.raises(TypeError):
        WeChatReceiver(FakeBackend(), {"小明"}, None)


def test_recall_quote_and_pat_system_messages_are_forwarded(receiver_factory):
    backend, receiver, messages = receiver_factory()
    receiver.start()
    backend.events.put(message(kind="system", text="对方撤回了一条消息", msg_id="sys-recall"))
    backend.events.put(message(kind="system", text="[引用消息]", msg_id="sys-quote"))
    backend.events.put(message(kind="system", text="拍了拍你", msg_id="sys-pat"))
    backend.events.put(message(kind="emoji", text="", msg_id="emoji-empty"))

    assert wait_for(lambda: len(messages) == 4)
    ids = [m["id"] for m in messages]
    assert ids == ["sys-recall", "sys-quote", "sys-pat", "emoji-empty"]


# ---------------------------------------------------------------------------
# A7：接收游标 + 启动补采（重启期间消息不丢）
# ---------------------------------------------------------------------------
def msg_data(*, text="在吗", ts=1_700_000_000.0, msg_id="m-1", friend="小明"):
    """补采 loader 返回的 data dict（等价 ``Message.to_dict()`` 的最小形态）。"""
    return {"friend": friend, "sender": "friend", "text": text,
            "kind": "text", "ts": ts, "id": msg_id}


def test_cursor_roundtrip_monotonic_and_corrupt(tmp_path):
    """游标：缺失→None；写入后可读回；只前进不后退；坏文件降级为 None。"""
    path = tmp_path / "nested" / "cursor.json"
    cursor = ReceiveCursor(path)
    assert cursor.load() is None  # 首次部署：没有游标

    assert cursor.save(1000.0, "m-1") is True
    assert cursor.load() == pytest.approx(1000.0)
    assert cursor.last_id == "m-1"

    # 回放旧消息不能把游标拉回去
    assert cursor.save(900.0, "old") is True
    assert cursor.load() == pytest.approx(1000.0)
    assert cursor.last_id == "old"  # id 按最后一次写入，ts 仍单调

    path.write_text("{not json", encoding="utf-8")
    broken = ReceiveCursor(path)
    assert broken.load() is None
    assert "cursor_load_failed" in broken.last_error


def test_gateway_save_cursor_advances(tmp_path):
    """网关每处理一条入站就推进游标（不跑完整 Gateway.__init__，避免副作用）。"""
    from agi.wechat_gateway.server import Gateway

    gateway = Gateway.__new__(Gateway)
    gateway.cursor = ReceiveCursor(tmp_path / "cursor.json")
    gateway._cursor_warned = ""
    gateway._cursor_enabled = True
    gateway._record = lambda *args, **kwargs: None

    gateway._save_cursor({"ts": 1_700_000_000.0, "id": "m-1"})
    gateway._save_cursor({"ts": 1_700_000_100.0, "id": "m-2"})
    assert gateway.cursor.load() == pytest.approx(1_700_000_100.0)
    assert gateway.cursor.last_id == "m-2"


def test_catch_up_only_after_cursor(receiver_factory):
    """游标存在 → 启动只补 (游标, 启动时刻] 的消息，游标之前的历史不补。"""
    now = time.time()
    since = now - 3600.0
    backend, receiver, messages = receiver_factory(since=since)
    receiver.start()

    rows = [
        msg_data(text="游标之前的历史", ts=now - 7200.0, msg_id="old-1"),
        msg_data(text="停机期间的消息", ts=now - 600.0, msg_id="new-1"),
    ]
    stats = receiver.catch_up(
        lambda s, u, limit: [d for d in rows if s <= d["ts"] <= u],
        since_ts=since, until_ts=now)

    assert stats["ok"] is True and stats["ready"] is True
    assert stats["forwarded"] == 1
    assert [m["id"] for m in messages] == ["new-1"]
    assert [m["text"] for m in messages] == ["停机期间的消息"]


def test_no_cursor_means_no_history_replay(receiver_factory, tmp_path):
    """无游标（首次部署）→ 退回旧行为：since=now，更早的消息一律不补/不转发。"""
    cursor = ReceiveCursor(tmp_path / "cursor.json")
    assert cursor.load() is None  # 网关据此走 since=time.time()

    backend, receiver, messages = receiver_factory(since=time.time())
    receiver.start()
    backend.events.put(message(text="很久以前的消息", msg_id="old-1",
                               ts=time.time() - 3600.0))

    assert wait_for(lambda: receiver.status()["messages_filtered"] == 1)
    assert messages == []
    assert receiver.status()["received"] == 0


def test_catch_up_keeps_id_dedupe(receiver_factory):
    """补采与正常事件共用同一张 id 去重表：同 id 只转发一次。"""
    now = time.time()
    backend, receiver, messages = receiver_factory(since=now - 3600.0)
    receiver.start()

    row = msg_data(text="重复的一条", ts=now - 600.0, msg_id="dup-1")
    stats = receiver.catch_up(lambda s, u, limit: [dict(row), dict(row)],
                              since_ts=now - 3600.0, until_ts=now)
    assert stats["forwarded"] == 1
    assert stats["duplicates"] >= 1
    assert len(messages) == 1

    # 正常轮询路径再推同一条（重启后 DB 轮询与补采可能都看到它）→ 仍被去重
    backend.events.put({"type": "message", "data": dict(row)})
    assert wait_for(lambda: receiver.status()["events_received"] >= 3)
    assert wait_for(lambda: receiver.status()["duplicates_dropped"] >= 2)
    assert len(messages) == 1
    assert messages[0]["id"] == "dup-1"


def test_catch_up_skips_cursor_last_id(receiver_factory):
    """游标里那条"最后已处理"消息（ts 恰好 == since）必须按 id 跳过，不能重复回放。"""
    now = time.time()
    since = now - 3600.0
    backend, receiver, messages = receiver_factory(since=since)
    receiver.start()

    last = msg_data(text="重启前最后一条", ts=since, msg_id="last-1")
    following = msg_data(text="之后的新消息", ts=since + 60.0, msg_id="after-1")
    stats = receiver.catch_up(lambda s, u, limit: [dict(last), dict(following)],
                              since_ts=since, until_ts=now, exclude_id="last-1")
    assert stats["skipped_cursor"] == 1
    assert stats["forwarded"] == 1
    assert [m["id"] for m in messages] == ["after-1"]


def test_catch_up_reports_backend_not_ready(receiver_factory):
    """DB 还没就绪（loader 返回 None）→ 报告 not ready，网关据此隔 2s 重试。"""
    now = time.time()
    backend, receiver, messages = receiver_factory(since=now - 3600.0)
    receiver.start()

    stats = receiver.catch_up(lambda s, u, limit: None,
                              since_ts=now - 3600.0, until_ts=now)
    assert stats["ok"] is False
    assert stats["ready"] is False
    assert stats["error"] == "backend_not_ready"
    assert messages == []


def test_monitor_recent_messages_accepts_names_kwarg():
    """A7 真机回归：网关补采会传 names=monitored_contacts。

    2026-10-31 真机事故：`_catchup_loader` 传了 names=，但 MonitorBackend.recent_messages
    没这个参数 → 补采 120s 超时。这里锁住签名，避免再次只靠 fake backend 测试。
    """
    from wx_core.monitor import MonitorBackend

    backend = MonitorBackend({"monitored": ["好友A"]})
    assert backend.recent_messages(
        since_ts=0.0, until_ts=1.0, limit=10, names=["好友A"]) is None
