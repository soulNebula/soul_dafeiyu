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


def test_parse_wechat_explicit_feedback():
    from agi.wechat_gateway.server import parse_feedback
    assert parse_feedback("反馈 好") == "good"
    assert parse_feedback("评价：一般") == "ok"
    assert parse_feedback("feedback bad") == "bad"
    assert parse_feedback("好评") is None
    assert parse_feedback("好") is None


# --------------------------------------------------------------------------- #
# P22：微信特殊消息识别（拍一拍 / @提及 / 引用前缀；撤回不回归）
# --------------------------------------------------------------------------- #
def _message(data: dict) -> dict:
    return {"type": "message", "data": data}


def test_receiver_forwards_system_and_marks_mention():
    backend = FakeBackend()
    got: list[dict] = []
    receiver = WeChatReceiver(backend, ["文件传输助手"], got.append, mention_name="小明")
    receiver.handle_event(_message({
        "friend": "文件传输助手", "sender": "friend", "kind": "system",
        "text": "「文件传输助手」拍了拍我", "id": "sys-pat"}))
    receiver.handle_event(_message({
        "friend": "文件传输助手", "sender": "friend", "kind": "system",
        "text": "对方撤回了一条消息", "id": "sys-recall"}))
    receiver.handle_event(_message({
        "friend": "文件传输助手", "sender": "friend", "kind": "system",
        "text": "[引用消息] 原话", "id": "sys-quote"}))
    receiver.handle_event(_message({
        "friend": "文件传输助手", "sender": "friend", "kind": "system",
        "text": "对方正在输入…", "id": "sys-other"}))          # 仍是 system，过滤
    receiver.handle_event(_message({
        "friend": "文件传输助手", "sender": "friend", "kind": "text",
        "text": "在吗 @小明", "id": "mention-hit"}))
    receiver.handle_event(_message({
        "friend": "文件传输助手", "sender": "friend", "kind": "text",
        "text": "在吗 @别人", "id": "mention-miss"}))

    ids = [m["id"] for m in got]
    assert ids == ["sys-pat", "sys-recall", "sys-quote", "mention-hit", "mention-miss"]
    by_id = {m["id"]: m for m in got}
    assert by_id["sys-pat"]["text"] == "「文件传输助手」拍了拍我"
    assert by_id["mention-hit"]["mention"] is True
    assert by_id["mention-hit"]["mention_name"] == "小明"
    assert "mention" not in by_id["mention-miss"]
    assert receiver.status()["last_filtered_reason"] == "system"


def _pat_gateway(tmp_path):
    from agi.wechat_gateway.server import Gateway
    return Gateway({
        "enabled": True, "port": 801, "dry_run": True,
        "allowed_contacts": ["好友A"],
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "quiet_hours": [0, 0],
    })


class _CapturingAgent:
    texts: list[str] = []

    def __init__(self, *_a, **_k):
        pass

    def send(self, text, image=None):
        type(self).texts.append(str(text))
        return {"bubbles": ["收到"], "reply": "收到", "turn_id": "T-P22"}


def test_server_pat_direction_and_recall_quote(tmp_path, monkeypatch):
    monkeypatch.setattr("agi.selflearn.chat.SelfLearnAgent", _CapturingAgent)
    _CapturingAgent.texts = []
    g = _pat_gateway(tmp_path)

    g._reply({"friend": "好友A", "kind": "system", "text": "「好友A」拍了拍我"})
    g._reply({"friend": "好友A", "kind": "system", "text": "我拍了拍「好友A」"})
    g._reply({"friend": "好友A", "kind": "system", "text": "拍了拍你"})
    g._reply({"friend": "好友A", "kind": "system", "text": "对方撤回了一条消息"})
    g._reply({"friend": "好友A", "kind": "system", "text": "[引用消息] 原话"})

    assert _CapturingAgent.texts == [
        "[拍一拍] 对方拍了拍你",
        "[拍一拍] 你拍了拍对方",
        "[拍一拍] 对方拍了拍你",
        "[对方撤回了一条消息]",
        "[引用] 对方引用了这条消息：原话",
    ]


def test_server_mention_prefix_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("agi.selflearn.chat.SelfLearnAgent", _CapturingAgent)
    _CapturingAgent.texts = []
    g = _pat_gateway(tmp_path)
    g.cfg["mention_name"] = "小明"

    g._reply({"friend": "好友A", "kind": "text", "text": "在吗 @小明"})
    g._reply({"friend": "好友A", "kind": "text", "text": "@别人 在吗"})
    g._reply({"friend": "好友A", "kind": "text", "text": "在吗 @小明",
              "mention": True})
    g._reply({"friend": "好友A", "kind": "text", "text": "[对方@了你] 已经标过"})

    assert _CapturingAgent.texts == [
        "[对方@了你] 在吗 @小明",
        "@别人 在吗",
        "[对方@了你] 在吗 @小明",           # mention 标记 + 防御识别也只加一次
        "[对方@了你] 已经标过",             # 幂等：不重复加
    ]


def test_chat_pat_returns_one_char_not_normal_reply(tmp_path, monkeypatch):
    from agi.selflearn import chat

    agent = chat.SelfLearnAgent(tmp_path / "selflearn.sqlite3")
    captured: dict = {}

    def fake_simple(user_id, reply, st, attachment, **kwargs):
        captured.update(user_id=user_id, reply=reply)
        return {"ok": True, "reply": reply, "bubbles": [reply]}

    monkeypatch.setattr(agent, "_simple_reply", fake_simple)
    out = agent.send("[拍一拍] 对方拍了拍你")
    assert out["reply"] == "嗯" and len(out["reply"]) <= 2
    assert captured["reply"] == "嗯"
