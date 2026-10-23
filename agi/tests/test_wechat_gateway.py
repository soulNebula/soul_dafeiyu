# -*- coding: utf-8 -*-
"""801 网关：sender/receiver 的离线测试，不连接真实微信。"""
from __future__ import annotations

import queue

from agi.wechat_gateway.receiver import WeChatReceiver
from agi.wechat_gateway.sender import WxAutoSender


class FakeWeChat:
    def __init__(self, *args, **kwargs):
        self.sent = []
        self.info = {"chat_name": "文件传输助手"}

    def ChatWith(self, contact, exact=True):
        return contact

    def ChatInfo(self):
        return self.info

    def SendMsg(self, text):
        self.sent.append(text)
        return {"status": "成功"}


class FakeModule:
    WeChat = FakeWeChat


def test_sender_dry_run_and_success():
    s = WxAutoSender(FakeModule)
    planned = s.send("文件传输助手", "测试", dry_run=True)
    assert planned["status"] == "planned"
    sent = s.send("文件传输助手", "测试")
    assert sent["ok"] is True and sent["status"] == "sent"


def test_sender_rejects_wrong_window():
    class Wrong(FakeWeChat):
        def ChatInfo(self):
            return {"chat_name": "别人"}

    class M:
        WeChat = Wrong
    out = WxAutoSender(M).send("文件传输助手", "测试")
    assert out["ok"] is False and "不匹配" in out["error"]


def test_sender_chatwith_fallback_when_chatinfo_empty():
    class Empty(FakeWeChat):
        def ChatInfo(self):
            return {}
    class M:
        WeChat = Empty
    out = WxAutoSender(M).send("文件传输助手", "测试")
    assert out["ok"] is True


class FakeBackend:
    def __init__(self):
        self.events = queue.Queue()
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False


def test_receiver_filters_and_dedupes():
    backend = FakeBackend()
    got = []
    r = WeChatReceiver(backend, ["文件传输助手"], got.append)
    r.handle_event({"type": "message", "data": {
        "friend": "文件传输助手", "sender": "friend", "kind": "text", "text": "你好", "id": "1"}})
    r.handle_event({"type": "message", "data": {
        "friend": "文件传输助手", "sender": "friend", "kind": "text", "text": "你好", "id": "1"}})
    r.handle_event({"type": "message", "data": {
        "friend": "文件传输助手", "sender": "self", "kind": "text", "text": "我发的", "id": "2"}})
    r.handle_event({"type": "message", "data": {
        "friend": "别人", "sender": "friend", "kind": "text", "text": "不在白名单", "id": "3"}})
    assert len(got) == 1 and got[0]["text"] == "你好"
