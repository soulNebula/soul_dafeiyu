# -*- coding: utf-8 -*-
"""P5-b 回归：被动回复里的媒体气泡必须真的发出去（此前被 _reply 丢弃）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agi.wechat_gateway import server as S  # noqa: E402


class _FakeAgent:
    media: dict | None = None

    def __init__(self, db_path):
        self.db_path = db_path

    def send(self, text, image=None):
        return {"turn_id": "t1", "bubbles": ["在的"], "media": _FakeAgent.media}


def _gateway(tmp_path, **over):
    cfg = {
        "allowed_contacts": ["x"],
        "send_mode": "auto",
        "dry_run": True,
        "media_enabled": True,
        "quiet_hours": [0, 0],
        "engine_enabled": True,
        "admin_token": "t",
    }
    cfg.update(over)
    gw = S.Gateway(cfg, stop_path=tmp_path / "stop", config_path=tmp_path / "config.json")
    gw.draft_path = Path(tempfile.mkdtemp()) / "drafts.jsonl"
    return gw


def test_reply_sends_media_bubbles(tmp_path, monkeypatch):
    import agi.selflearn.chat as chat_mod
    _FakeAgent.media = {"bubbles": [
        {"type": "sticker", "path": r"C:\tmp\a.png", "name": "贴纸"},
    ]}
    monkeypatch.setattr(chat_mod, "SelfLearnAgent", _FakeAgent)
    gw = _gateway(tmp_path)
    calls = []
    monkeypatch.setattr(gw, "send_media_batch",
                        lambda contact, items, **kw: calls.append((contact, items, kw)) or
                        {"ok": True, "sent": len(items), "items": []})
    gw._reply({"friend": "x", "text": "在吗", "kind": "text"})
    assert calls, "媒体气泡没有被发送"
    contact, items, kw = calls[0]
    assert contact == "x" and items[0]["type"] == "sticker"
    assert kw.get("reactive") is True


def test_reply_without_media_does_not_send(tmp_path, monkeypatch):
    import agi.selflearn.chat as chat_mod
    _FakeAgent.media = None
    monkeypatch.setattr(chat_mod, "SelfLearnAgent", _FakeAgent)
    gw = _gateway(tmp_path)
    calls = []
    monkeypatch.setattr(gw, "send_media_batch",
                        lambda *a, **k: calls.append(a) or {"ok": True})
    gw._reply({"friend": "x", "text": "在吗", "kind": "text"})
    assert calls == []
