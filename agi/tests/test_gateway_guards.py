# -*- coding: utf-8 -*-
"""801 发送闸门：内容拦截、最小间隔、每小时上限、兜底发送。"""
from __future__ import annotations

from agi.wechat_gateway import drafts, win_send
from agi.wechat_gateway.server import Gateway


def _gw(tmp_path, **overrides) -> Gateway:
    cfg = {
        "enabled": True,
        "port": 801,
        "dry_run": True,
        "allowed_contacts": ["好友A"],
        "send_mode": "auto",
        "max_chars": 20,
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
    }
    cfg.update(overrides)
    return Gateway(cfg)


def test_content_too_long_rejected_without_draft(tmp_path):
    g = _gw(tmp_path)
    out = g._send("好友A", "x" * 21)
    assert out["ok"] is False and out["error"] == "too_long"
    assert drafts.list_all(path=g.draft_path) == []


def test_content_local_path_rejected(tmp_path):
    g = _gw(tmp_path, max_chars=500)
    out = g._send("好友A", "文件在 C:\\Users\\me\\secret.txt")
    assert out["ok"] is False and out["error"] == "sensitive"


def test_hourly_limit_blocks(tmp_path):
    g = _gw(tmp_path, hourly_limit=2)
    import time
    now = time.time()
    g.send_times = [now - 10, now - 20]
    out = g._send("好友A", "你好")
    assert out["ok"] is False and out["error"] == "hourly_limit"


def test_min_interval_blocks_proactive_only(tmp_path):
    import time
    g = _gw(tmp_path, min_interval_s=60.0)
    now = time.time()
    g.send_times = [now - 5]
    blocked = g._send("好友A", "在吗")
    assert blocked["ok"] is False and blocked["error"] == "min_interval"
    # 被动回复不受最小间隔限制
    reactive = g._send("好友A", "在吗", reactive=True)
    assert reactive.get("status") != "blocked"


def test_fallback_disabled_archives_failed_draft(tmp_path):
    g = _gw(tmp_path)
    out = g._on_send_failure("好友A", "发不出去的一句", "wxauto boom")
    assert out["ok"] is False
    rows = drafts.list_all(path=g.draft_path)
    assert len(rows) == 1 and rows[0]["reason"] == "send_failed"


def test_fallback_clipboard_archives_draft(tmp_path, monkeypatch):
    g = _gw(tmp_path, fallback_enabled=True, fallback_mode="manual")
    monkeypatch.setattr(win_send, "send_via_window",
                        lambda text, cfg: {"ok": True, "status": "clipboard",
                                           "warning": "请手动粘贴", "verified": False})
    out = g._on_send_failure("好友A", "兜底一句", "wxauto boom")
    assert out["status"] == "clipboard"
    rows = drafts.list_all(path=g.draft_path)
    assert len(rows) == 1
    assert rows[0]["reason"].startswith("fallback_clipboard")


def test_fallback_unverified_not_counted_as_sent(tmp_path, monkeypatch):
    """P7-b：窗口兜底 verified=False → unverified：不计 sent/sent_today、不重置熔断。"""
    g = _gw(tmp_path, dry_run=False, fallback_enabled=True, fallback_mode="auto")
    monkeypatch.setattr(win_send, "send_via_window",
                        lambda text, cfg: {"ok": True, "status": "sent",
                                           "verified": False, "warning": "人工抽查"})
    out = g._on_send_failure("好友A", "窗口发出去的一句", "wxauto boom")
    assert out["ok"] is True and out["status"] == "sent"
    assert out["classification"] == "unverified"
    assert g.sent_today == 0 and len(g.send_times) == 0
    assert drafts.list_all(path=g.draft_path) == []
    assert any(e.get("kind") == "fallback_unverified" for e in g.recent)
