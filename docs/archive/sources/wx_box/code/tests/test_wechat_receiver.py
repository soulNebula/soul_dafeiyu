# -*- coding: utf-8 -*-
"""801 网关接收适配器：只转发白名单好友的入站文本消息。

全部用 queue.Queue 假后端，不 import wxauto、不起真实微信、不发任何消息。
"""
from __future__ import annotations

import queue
import threading
import time

import pytest

from agi.wechat_gateway.receiver import WeChatReceiver


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


def test_recall_and_quote_system_messages_are_forwarded(receiver_factory):
    backend, receiver, messages = receiver_factory()
    receiver.start()
    backend.events.put(message(kind="system", text="对方撤回了一条消息", msg_id="sys-recall"))
    backend.events.put(message(kind="system", text="[引用消息]", msg_id="sys-quote"))
    backend.events.put(message(kind="system", text="拍了拍你", msg_id="sys-pat"))
    backend.events.put(message(kind="emoji", text="", msg_id="emoji-empty"))

    assert wait_for(lambda: len(messages) == 3)
    ids = [m["id"] for m in messages]
    assert ids == ["sys-recall", "sys-quote", "emoji-empty"]
