# -*- coding: utf-8 -*-
"""801 回复合批（"等他把话说完"）+ 主动反应登记。"""
from __future__ import annotations

from agi.wechat_gateway import proactive_worker
from agi.wechat_gateway.server import Gateway


def _gw(tmp_path, **overrides) -> Gateway:
    cfg = {
        "enabled": True,
        "port": 801,
        "dry_run": True,
        "allowed_contacts": ["好友A"],
        "send_mode": "auto",
        "max_chars": 500,
        "reply_debounce_ms": 2000,
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
    }
    cfg.update(overrides)
    return Gateway(cfg)


def test_consecutive_texts_are_batched(tmp_path, monkeypatch):
    monkeypatch.setattr(proactive_worker, "mark_response", lambda *a, **k: "")
    g = _gw(tmp_path)
    calls = []
    g._reply = lambda msg: calls.append(dict(msg))
    g._buffer_inbound({"friend": "好友A", "text": "你好", "kind": "text"})
    g._buffer_inbound({"friend": "好友A", "text": "在吗", "kind": "text"})
    assert calls == []            # 还在攒
    g._flush_pending()
    assert calls == []            # 没到 deadline
    g.pending["好友A"]["deadline"] = 0
    g._flush_pending()
    assert len(calls) == 1
    assert calls[0]["text"] == "你好\n在吗"
    assert calls[0]["kind"] == "text"


def test_image_bypasses_batching(tmp_path, monkeypatch):
    monkeypatch.setattr(proactive_worker, "mark_response", lambda *a, **k: "")
    g = _gw(tmp_path)
    calls = []
    g._reply = lambda msg: calls.append(dict(msg))
    g._buffer_inbound({"friend": "好友A", "text": "[图片]", "kind": "image",
                       "media_path": "x.png"})
    assert len(calls) == 1 and calls[0]["kind"] == "image"


def test_debounce_zero_replies_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(proactive_worker, "mark_response", lambda *a, **k: "")
    g = _gw(tmp_path, reply_debounce_ms=0)
    calls = []
    g._reply = lambda msg: calls.append(dict(msg))
    g._buffer_inbound({"friend": "好友A", "text": "hi", "kind": "text"})
    assert len(calls) == 1


def test_overflow_flushes_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(proactive_worker, "mark_response", lambda *a, **k: "")
    g = _gw(tmp_path, max_chars=10)
    calls = []
    g._reply = lambda msg: calls.append(dict(msg))
    g._buffer_inbound({"friend": "好友A", "text": "12345", "kind": "text"})
    g._buffer_inbound({"friend": "好友A", "text": "67890", "kind": "text"})
    assert len(calls) == 1          # 超过上限立刻回
    assert "好友A" not in g.pending


def test_inbound_marks_proactive_response(tmp_path, monkeypatch):
    marked = []
    monkeypatch.setattr(proactive_worker, "mark_response",
                        lambda db, text: marked.append(text) or "positive")
    g = _gw(tmp_path, reply_debounce_ms=0)
    g._reply = lambda msg: None
    g._buffer_inbound({"friend": "好友A", "text": "哈哈", "kind": "text"})
    assert marked == ["哈哈"]
