# -*- coding: utf-8 -*-
"""801 confirm 模式草稿流转：不批准不发、批准才发、发送失败不丢内容。"""
from __future__ import annotations

from agi.wechat_gateway import drafts
from agi.wechat_gateway.server import Gateway


def _gateway(tmp_path, **overrides) -> Gateway:
    cfg = {
        "enabled": True,
        "port": 801,
        "dry_run": True,
        "allowed_contacts": ["好友A"],
        "send_mode": "confirm",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
    }
    cfg.update(overrides)
    return Gateway(cfg)


def test_confirm_mode_creates_draft_without_sending(tmp_path):
    g = _gateway(tmp_path)
    out = g._send("好友A", "在吗？")
    assert out["ok"] is False and out["status"] == "draft"
    assert out["requires_confirm"] is True and out["draft_id"]
    rows = drafts.list_all(path=g.draft_path)
    assert len(rows) == 1 and rows[0]["text"] == "在吗？"
    assert g.status()["drafts_pending"] == 1


def test_per_contact_auto_bypasses_draft(tmp_path):
    g = _gateway(tmp_path, send_mode="confirm", contact_modes={"好友A": "auto"})
    out = g._send("好友A", "hi")
    assert out["status"] == "planned"  # dry_run 下模拟发送成功
    assert drafts.list_all(path=g.draft_path) == []


def test_force_approval_sends_directly(tmp_path):
    g = _gateway(tmp_path)
    draft = drafts.add("好友A", "批准这条", path=g.draft_path)
    item = drafts.discard(draft["id"], path=g.draft_path)
    out = g._send(item["contact"], item["text"], force=True)
    assert out["status"] == "planned"
    assert drafts.list_all(path=g.draft_path) == []


class _FailingSender:
    def send(self, contact, text, dry_run=False):
        return {"ok": False, "status": "failed", "error": "wxauto 崩了"}


def test_send_failure_archives_draft(tmp_path):
    g = _gateway(tmp_path, send_mode="auto")
    g.sender = _FailingSender()
    out = g._send("好友A", "会失败的一句")
    assert out["ok"] is False
    rows = drafts.list_all(path=g.draft_path)
    assert len(rows) == 1
    assert rows[0]["text"] == "会失败的一句"
    assert rows[0]["reason"] == "send_failed"
