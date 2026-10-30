# -*- coding: utf-8 -*-
"""A9-P3：发送/生成/熔断统一观测 + 发送 job 分段审计 + 回滚开关。"""
from __future__ import annotations

import time

from agi.wechat_gateway import send_async as send_async_mod
from agi.wechat_gateway.server import Gateway


def _gw(tmp_path, **overrides) -> Gateway:
    cfg = {
        "enabled": True, "allowed_contacts": ["好友A"],
        "monitored_contacts": ["好友A"], "admin_token": "tok",
        "dry_run": True, "send_mode": "auto", "quiet_hours": [0, 0],
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
    }
    cfg.update(overrides)
    return Gateway(cfg)


def test_metrics_snapshot_shape(tmp_path):
    gw = _gw(tmp_path)
    m = gw.metrics_snapshot()
    assert m["ok"] is True
    assert set(m) >= {"queue", "send_async", "reply_pool", "send_breaker", "counts"}
    assert m["send_async"]["enabled"] is False       # 默认关
    assert m["reply_pool"]["enabled"] is False
    assert m["send_breaker"]["open"] is False


def test_send_job_audit_has_wait_and_exec_ms(tmp_path, monkeypatch):
    gw = _gw(tmp_path, send_async=True)
    monkeypatch.setattr(gw, "_send_locked",
                        lambda contact, text, **kw: {"ok": True, "status": "sent"})
    job = send_async_mod.SendJob(contact="好友A", text="x", kwargs={},
                                 priority=send_async_mod.PRIORITY_MANUAL)
    job.started_at = job.created_at + 0.01
    gw._send_job_runner(job)
    events = [e for e in gw.recent if e.get("kind") == "send_job"]
    assert events, "发送 job 必须留 send_job 审计（wait_ms/exec_ms/status）"
    event = events[-1]
    assert event["priority"] == "manual"
    assert event["status"] == "sent"
    assert event["wait_ms"] is not None and event["exec_ms"] is not None


def test_rollback_off_uses_old_inline_path(tmp_path, monkeypatch):
    gw = _gw(tmp_path)                                # send_async / reply_pool 默认关
    assert gw._start_send_worker() is False
    assert gw._start_reply_pool() is False
    calls: list[str] = []
    monkeypatch.setattr(gw, "_reply", lambda msg: calls.append(msg.get("text")))
    gw.pending["好友A"] = {"msg": {"friend": "好友A", "kind": "text"},
                           "texts": ["回滚验证"], "deadline": 0}
    gw._flush_pending(force=True)
    assert calls == ["回滚验证"]
    assert gw.metrics_snapshot()["reply_pool"]["alive"] is False
