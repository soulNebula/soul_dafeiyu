# -*- coding: utf-8 -*-
"""P0-2：发送链底层闸门加固测试。

覆盖：白名单、min_interval 强制（0→1.5 / GATEWAY_DEBUG）、hourly/daily 上限、
reactive 同文案 60s 去重、approve/draft 需 confirm 且 manual 不绕静默、
?token= query 默认拒绝、弱 admin_token 告警。
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from agi.wechat_gateway import drafts
from agi.wechat_gateway.server import Gateway, admin_token_warning, make_handler

NOW = time.mktime((2026, 10, 28, 12, 0, 0, 0, 0, -1))


def _gw(tmp_path, **overrides) -> Gateway:
    cfg = {
        "enabled": True, "port": 801, "dry_run": True,
        "allowed_contacts": ["好友A"], "monitored_contacts": ["好友A"],
        "send_mode": "auto",
        "admin_token": "tok_secure_123",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "quiet_hours": [0, 0],
        "min_interval_s": 1.5,
        "rate_per_minute": 50, "reactive_rate_per_minute": 50,
        "hourly_limit": 40, "daily_limit": 200,
    }
    cfg.update(overrides)
    return Gateway(cfg, stop_path=tmp_path / "STOP",
                   config_path=tmp_path / "config.json")


def _records(gw, kind):
    return [e for e in gw.recent if e.get("kind") == kind]


# --------------------------------------------------------------------------- #
# 1. 白名单（最底层，manual/force 也拦）
# --------------------------------------------------------------------------- #
def test_send_whitelist_blocks_all_paths(tmp_path):
    gw = _gw(tmp_path)
    assert gw._send("好友B", "hi", reactive=True)["error"] == "not_allowed"
    assert gw._send("好友B", "hi", proactive=True)["error"] == "not_allowed"
    assert gw._send("好友B", "hi", manual=True, force=True)["error"] == "not_allowed"
    assert len(_records(gw, "send_blocked")) == 3
    assert {e["reason"] for e in _records(gw, "send_blocked")} == {"not_allowed"}
    assert gw._send("好友A", "hi", reactive=True)["ok"] is True


# --------------------------------------------------------------------------- #
# 2. min_interval 强制
# --------------------------------------------------------------------------- #
def test_min_interval_enforced_and_debug_override(tmp_path, monkeypatch):
    gw = _gw(tmp_path)
    monkeypatch.setattr("agi.wechat_gateway.server.time.time", lambda: NOW)
    gw.send_times.append(NOW - 0.5)
    out = gw._send("好友A", "hi")
    assert out["status"] == "blocked" and out["error"] == "min_interval"
    assert _records(gw, "send_blocked")[-1]["reason"] == "min_interval"

    # GATEWAY_DEBUG=1 时 0 才真正放开
    monkeypatch.setenv("GATEWAY_DEBUG", "1")
    gw2 = _gw(tmp_path, min_interval_s=0)
    monkeypatch.setattr("agi.wechat_gateway.server.time.time", lambda: NOW)
    gw2.send_times.append(NOW - 0.1)
    assert gw2._send("好友A", "hi")["ok"] is True
    assert gw2._effective_min_interval() == 0.0

    # 未开 debug 时 0 仍视为 1.5
    monkeypatch.delenv("GATEWAY_DEBUG", raising=False)
    gw3 = _gw(tmp_path, min_interval_s=0)
    monkeypatch.setattr("agi.wechat_gateway.server.time.time", lambda: NOW)
    gw3.send_times.append(NOW - 0.1)
    assert gw3._send("好友A", "hi")["error"] == "min_interval"


# --------------------------------------------------------------------------- #
# 3. hourly / daily 上限
# --------------------------------------------------------------------------- #
def test_send_hourly_and_daily_limits(tmp_path, monkeypatch):
    monkeypatch.setattr("agi.wechat_gateway.server.time.time", lambda: NOW)
    gw = _gw(tmp_path, hourly_limit=1, min_interval_s=0)
    gw.send_times.append(NOW - 10)
    assert gw._send("好友A", "hi")["error"] == "hourly_limit"

    gw2 = _gw(tmp_path, daily_limit=1, min_interval_s=0)
    gw2.sent_today = 1
    gw2.sent_day = time.strftime("%Y-%m-%d", time.localtime(NOW))
    assert gw2._send("好友A", "hi")["error"] == "daily_limit"


# --------------------------------------------------------------------------- #
# 4. reactive 同文案 60s 去重
# --------------------------------------------------------------------------- #
def test_reactive_dedupe_same_text_60s(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DEBUG", "1")
    monkeypatch.setattr("agi.wechat_gateway.server.time.time", lambda: NOW)
    gw = _gw(tmp_path, min_interval_s=0)
    first = gw._send("好友A", "同一句话", reactive=True)
    assert first["ok"] is True and first["status"] == "planned"
    second = gw._send("好友A", "同一句话", reactive=True)
    assert second["status"] == "blocked" and second["error"] == "dedupe"
    assert _records(gw, "send_blocked")[-1]["reason"] == "dedupe"
    # 不同文案不拦
    assert gw._send("好友A", "另一句", reactive=True)["ok"] is True


# --------------------------------------------------------------------------- #
# 5. approve / draft approve：confirm + 不绕静默
# --------------------------------------------------------------------------- #
def _call(gw, path, body, *, token="tok_secure_123"):
    handler_cls = make_handler(gw)
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler = handler_cls.__new__(handler_cls)
    handler.path = path
    handler.command = "POST"
    handler.requestline = f"POST {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.close_connection = True
    handler.wfile = io.BytesIO()
    handler._headers_buffer = []
    handler.rfile = io.BytesIO(raw)
    handler.headers = {"Content-Length": str(len(raw)), "Host": "127.0.0.1:801"}
    if token:
        handler.headers["X-Admin-Token"] = token
    handler.do_POST()
    head, _, payload = handler.wfile.getvalue().decode("utf-8").partition("\r\n\r\n")
    status = int(head.split(" ")[1]) if head else 0
    return status, (json.loads(payload) if payload.strip() else None)


def test_approve_requires_confirm_and_quiet_not_bypassed(tmp_path, monkeypatch):
    gw = _gw(tmp_path, quiet_hours=[0, 24])
    monkeypatch.setattr("agi.wechat_gateway.server.time.time", lambda: NOW)
    called: list[dict] = []
    monkeypatch.setattr(gw, "_send", lambda contact, text, **kw: (
        called.append({"contact": contact, "text": text, **kw})
        or {"ok": True, "status": "planned"}))

    status, data = _call(gw, "/api/approve", {
        "contact": "好友A", "text": "hi"})
    assert status == 400 and data["error"] == "confirm_required"

    status, data = _call(gw, "/api/approve", {
        "contact": "好友A", "text": "hi", "confirm": True,
        "force_quiet": True})
    assert status == 200 and called[-1]["force"] is True
    assert called[-1]["manual"] is True and called[-1]["force_quiet"] is True

    # 默认 confirm 但不 force_quiet → _send 收到 force_quiet=False（真 _send 会拦静默）
    status, data = _call(gw, "/api/approve", {
        "contact": "好友A", "text": "hi", "confirm": True})
    assert status == 200 and called[-1]["force_quiet"] is False

    # 真实 _send（不 mock）在静默时段即使 force/manual 也拦
    real = _gw(tmp_path, quiet_hours=[0, 24])
    monkeypatch.setattr("agi.wechat_gateway.server.time.time", lambda: NOW)
    out = real._send("好友A", "hi", force=True, manual=True)
    assert out["status"] == "blocked" and out["error"] == "quiet_hours"
    out2 = real._send("好友A", "hi", force=True, manual=True, force_quiet=True)
    assert out2["ok"] is True


def test_draft_approve_requires_confirm(tmp_path):
    gw = _gw(tmp_path)
    item = drafts.add("好友A", "草稿内容", "confirm", path=gw.draft_path)
    status, data = _call(gw, "/api/draft/approve", {"id": item["id"]})
    assert status == 400 and data["error"] == "confirm_required"
    assert drafts.list_all(path=gw.draft_path)   # 未丢掉草稿
    status, data = _call(gw, "/api/draft/approve",
                         {"id": item["id"], "confirm": True})
    assert status == 200 and data["ok"] is True


# --------------------------------------------------------------------------- #
# 6. ?token= query 默认拒绝 / 弱 token 告警
# --------------------------------------------------------------------------- #
def _call_get(gw, path, *, header_token=None, peer="127.0.0.1"):
    handler_cls = make_handler(gw)
    handler = handler_cls.__new__(handler_cls)
    handler.path = path
    handler.command = "GET"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.close_connection = True
    handler.wfile = io.BytesIO()
    handler._headers_buffer = []
    handler.rfile = io.BytesIO(b"")
    handler.headers = {"Host": "127.0.0.1:801", "Content-Length": "0"}
    if header_token:
        handler.headers["X-Admin-Token"] = header_token
    handler.client_address = (peer, 5555)
    handler.do_GET()
    head, _, payload = handler.wfile.getvalue().decode("utf-8").partition("\r\n\r\n")
    status = int(head.split(" ")[1]) if head else 0
    return status, (json.loads(payload) if payload.strip() else None)


def test_query_token_rejected_by_default(tmp_path, monkeypatch):
    gw = _gw(tmp_path)
    monkeypatch.delenv("GATEWAY_ALLOW_QUERY_TOKEN", raising=False)
    status, _ = _call_get(gw, "/status?token=tok_secure_123")
    assert status == 401
    status, _ = _call_get(gw, "/status", header_token="tok_secure_123")
    assert status == 200

    # env 开关 + 回环来源才允许 query
    monkeypatch.setenv("GATEWAY_ALLOW_QUERY_TOKEN", "1")
    status, _ = _call_get(gw, "/status?token=tok_secure_123")
    assert status == 200
    status, _ = _call_get(gw, "/status?token=tok_secure_123", peer="10.0.0.5")
    assert status == 401


def test_weak_admin_token_warning(tmp_path):
    gw = _gw(tmp_path, admin_token="8888")
    assert "admin_token" in gw.admin_token_warning
    assert any(e.get("kind") == "admin_token_weak" for e in gw.recent)
    assert admin_token_warning("short") and not admin_token_warning(
        "a" * 32)
    # 默认健康检查（无 token）也带告警
    status, data = _call_get(gw, "/api/health")
    assert status == 200 and data["admin_token_weak"] is True
