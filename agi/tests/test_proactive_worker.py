# -*- coding: utf-8 -*-
"""M5 主动链：per-contact 库、真发送、turn 登记与反馈回填。"""
from __future__ import annotations

from agi.selflearn import db, memory, proactive
from agi.selflearn import ticker
from agi.wechat_gateway import proactive_worker


class FakeGateway:
    def __init__(self, **cfg):
        self.cfg = {
            "proactive_enabled": True,
            "allowed_contacts": ["好友A"],
            "proactive_contacts": [],
            **cfg,
        }
        self.sent: list[tuple] = []
        self.records: list[tuple] = []
        self.last_turns: dict = {}
        self.last_proactive: dict = {}

    def _send(self, contact, text, **kw):
        self.sent.append((contact, text, kw))
        return {"ok": True, "status": "sent"}

    def _record(self, kind, data):
        self.records.append((kind, data))


def _tick_available(turn_id="T-PRO-1"):
    return {"ok": True, "ran": True, "gate": {"allowed": True},
            "proactive": {"ok": True, "available": True,
                          "messages": ["在吗", "刚想到个事"],
                          "message": "在吗", "turn_id": turn_id}}


def test_disabled_returns_early(monkeypatch):
    g = FakeGateway(proactive_enabled=False)
    out = proactive_worker.run_cycle(g)
    assert out["ran"] is False and out["reason"] == "proactive_disabled"
    assert g.sent == []


def test_sends_bubbles_and_links_turn(monkeypatch):
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick_available())
    g = FakeGateway()
    out = proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert out["sent"] == 2 and out["drafts"] == 0
    assert [s[1] for s in g.sent] == ["在吗", "刚想到个事"]
    assert all(s[2].get("proactive") is True for s in g.sent)
    assert g.last_turns["好友A"] == "T-PRO-1"
    assert any(k == "proactive_send" for k, _ in g.records)
    assert g.last_proactive["sent"] == 2


def test_draft_does_not_link_turn(monkeypatch):
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick_available())

    class DraftGateway(FakeGateway):
        def _send(self, contact, text, **kw):
            self.sent.append((contact, text, kw))
            return {"ok": False, "status": "draft", "draft_id": "D-1"}

    g = DraftGateway()
    out = proactive_worker.run_cycle(g, client=object())
    assert out["drafts"] == 2 and out["sent"] == 0
    assert g.last_turns == {}


def test_gate_blocked_is_skipped(monkeypatch):
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: {
        "ok": True, "ran": False, "reason": "quiet_hours",
        "proactive": {"available": False}})
    g = FakeGateway()
    out = proactive_worker.run_cycle(g, client=object())
    assert out["sent"] == 0 and out["skipped"][0]["reason"] == "quiet_hours"
    assert g.sent == []


def test_tick_exception_does_not_break_other_contacts(monkeypatch):
    def boom(path, client, now=None):
        raise RuntimeError("db locked")

    monkeypatch.setattr(ticker, "run_once", boom)
    g = FakeGateway(allowed_contacts=["好友A", "好友B"])
    out = proactive_worker.run_cycle(g, client=object())
    assert out["ok"] is True and len(out["skipped"]) == 2
    assert out["sent"] == 0


def test_proactive_turn_linkage(tmp_path):
    path = tmp_path / "c.sqlite3"
    user_id = memory.store_message(path, "user", "最近有点累")
    turn_id = proactive.record_proactive_turn(path, "MSG-PRO", "那早点睡", now=100.0)
    assert turn_id
    con = db.connect(path)
    try:
        row = con.execute("select * from turns where id=?", (turn_id,)).fetchone()
    finally:
        con.close()
    assert row["user_message_id"] == user_id
    assert row["assistant_message_id"] == "MSG-PRO"
    # 没有用户消息时不应该硬造 turn
    empty = tmp_path / "empty.sqlite3"
    db.connect(empty).close()
    assert proactive.record_proactive_turn(empty, "X", "hi") is None


# ---------------------------------------------------------------- 空闲/负反馈
def test_user_active_skips_proactive(monkeypatch):
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick_available())
    g = FakeGateway(proactive_idle_s=600)
    g.last_inbound = {"好友A": 100.0}
    out = proactive_worker.run_cycle(g, client=object(), now=200.0)
    assert out["sent"] == 0 and out["skipped"][0]["reason"] == "user_active"


def test_negative_feedback_cools_down(monkeypatch, tmp_path):
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick_available())
    g = FakeGateway()
    # 用临时库替代 contact_db，避免污染真实联系人库
    monkeypatch.setattr("agi.wechat_gateway.config.contact_db",
                        lambda contact: tmp_path / "c.sqlite3")
    db_path = tmp_path / "c.sqlite3"
    proactive_worker.record_event(db_path, "好友A", "在吗", "sent", now=100.0)
    assert proactive_worker.mark_response(db_path, "好烦，别发了", now=110.0) == "negative"
    out = proactive_worker.run_cycle(g, client=object(), now=200.0)
    assert out["sent"] == 0 and out["skipped"][0]["reason"] == "negative_cooldown"


def test_mark_response_sentiment(tmp_path):
    db_path = tmp_path / "c.sqlite3"
    proactive_worker.record_event(db_path, "A", "在吗", "sent", now=1.0)
    assert proactive_worker.mark_response(db_path, "哈哈有意思", now=2.0) == "positive"
    proactive_worker.record_event(db_path, "A", "再说一句", "sent", now=3.0)
    assert proactive_worker.mark_response(db_path, "嗯", now=4.0) == "positive"
    proactive_worker.record_event(db_path, "A", "第三句", "sent", now=5.0)
    assert proactive_worker.mark_response(db_path, "今天下雨了", now=6.0) == "neutral"
