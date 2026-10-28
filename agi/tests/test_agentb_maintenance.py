# -*- coding: utf-8 -*-
"""task-1（Agent B）：草稿 TTL / 弱口令硬闸 / 批准接口令牌形态。

覆盖验收项：
8) drafts.prune(max_age_days=7)（配置 draft_ttl_days 覆盖）+ Gateway 启动时清一次
   + add 时顺带 prune，记录 drafts_pruned
9) 弱/空 admin_token 且服务已启动 → 管理 API 403 + admin_token_weak_blocked 审计；
   /api/approve、/api/draft/approve 保留 confirm=true + 白名单，且拒绝 ?token= query

向后兼容：未 start() 的网关（纯离线构造/历史单测）保持旧行为。
"""
from __future__ import annotations

import io
import json
import time

from agi.wechat_gateway import drafts
from agi.wechat_gateway.server import Gateway, make_handler

NOW = time.mktime((2026, 10, 29, 12, 0, 0, 0, 0, -1))


def _gw(tmp_path, **over):
    cfg = {
        "enabled": True, "port": 801, "dry_run": True, "send_mode": "auto",
        "allowed_contacts": ["好友A"], "monitored_contacts": ["好友A"],
        "admin_token": "tok_secure_123",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "quiet_hours": [0, 0], "min_interval_s": 0,
    }
    cfg.update(over)
    return Gateway(cfg, stop_path=tmp_path / "STOP",
                   config_path=tmp_path / "config.json")


def _write_drafts(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                            for row in rows), encoding="utf-8")


def _draft_row(draft_id, age_days, text="内容"):
    return {"id": draft_id, "ts": NOW - age_days * 86400.0, "contact": "好友A",
            "text": text, "reason": "confirm", "status": "pending"}


def _events(gw, kind):
    return [e for e in gw.recent if e.get("kind") == kind]


# --------------------------------------------------------------------------- #
# 8. 草稿 TTL
# --------------------------------------------------------------------------- #
def test_prune_removes_expired_keeps_fresh(tmp_path):
    path = tmp_path / "drafts.jsonl"
    _write_drafts(path, [_draft_row("D-old", 8), _draft_row("D-fresh", 1)])
    removed = drafts.prune(max_age_days=7, path=path, now=NOW)
    assert removed == 1
    assert [row["id"] for row in drafts.list_all(path=path)] == ["D-fresh"]


def test_prune_zero_days_disables_and_missing_file_is_noop(tmp_path):
    path = tmp_path / "drafts.jsonl"
    _write_drafts(path, [_draft_row("D-old", 100)])
    assert drafts.prune(max_age_days=0, path=path, now=NOW) == 0
    assert len(drafts.list_all(path=path)) == 1
    assert drafts.prune(max_age_days=7, path=tmp_path / "nope.jsonl", now=NOW) == 0


def test_add_prunes_expired_drafts(tmp_path):
    path = tmp_path / "drafts.jsonl"
    _write_drafts(path, [_draft_row("D-ancient", 30)])
    fresh = drafts.add("好友A", "新草稿", path=path)
    rows = drafts.list_all(path=path)
    assert [row["id"] for row in rows] == [fresh["id"]]


def test_gateway_startup_prunes_drafts_and_records_event(tmp_path):
    gw_path = tmp_path / "drafts.jsonl"
    _write_drafts(gw_path, [_draft_row("D-old", 9), _draft_row("D-fresh", 2)])
    gw = _gw(tmp_path)
    assert gw.drafts_pruned == 1
    assert [row["id"] for row in drafts.list_all(path=gw.draft_path)] == ["D-fresh"]
    pruned = _events(gw, "drafts_pruned")
    assert pruned and pruned[-1]["pruned"] == 1
    assert gw.status()["drafts_pruned"] == 1


def test_gateway_draft_ttl_days_config_override(tmp_path):
    gw_path = tmp_path / "drafts.jsonl"
    _write_drafts(gw_path, [_draft_row("D-old", 2)])
    # 默认 7 天：2 天前的草稿保留
    assert _gw(tmp_path).drafts_pruned == 0
    # 配置成 1 天：同一条被清掉
    assert _gw(tmp_path, draft_ttl_days=1).drafts_pruned == 1


# --------------------------------------------------------------------------- #
# 9. 弱口令硬闸（服务已启动 = ready=True；未 start 的网关保持旧行为）
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
    handler.client_address = ("127.0.0.1", 5555)
    handler.do_POST()
    head, _, payload = handler.wfile.getvalue().decode("utf-8").partition("\r\n\r\n")
    status = int(head.split(" ")[1]) if head else 0
    return status, (json.loads(payload) if payload.strip() else None)


def test_weak_token_blocks_admin_api_once_started(tmp_path):
    gw = _gw(tmp_path, admin_token="8888")
    gw.ready = True                      # start() 成功后会置 ready（这里直接模拟）
    status, data = _call_get(gw, "/status", header_token="8888")
    assert status == 403 and data["error"] == "admin_token_weak_blocked"
    assert _events(gw, "admin_token_weak_blocked")
    # 健康检查仍然免鉴权（用于发现"我被弱口令闸拦住了"）
    status, data = _call_get(gw, "/api/health")
    assert status == 200 and data["admin_token_weak"] is True
    assert gw.status()["admin_token_weak_blocked"] is True


def test_empty_token_blocks_admin_api_once_started(tmp_path):
    gw = _gw(tmp_path, admin_token="")
    gw.ready = True
    # 旧行为是 401（token required）；弱口令硬闸下必须是 403（配置问题，不是凭据错）
    status, data = _call_get(gw, "/status")
    assert status == 403 and data["error"] == "admin_token_weak_blocked"


def test_short_token_blocked_even_if_not_in_blocklist(tmp_path):
    gw = _gw(tmp_path, admin_token="tok")   # 长度 3 < 8
    gw.ready = True
    status, data = _call_get(gw, "/status", header_token="tok")
    assert status == 403 and data["error"] == "admin_token_weak_blocked"


def test_weak_token_does_not_block_before_start(tmp_path):
    """未 start() 的离线网关保持旧行为（历史单测用短 token 调 handler）。"""
    gw = _gw(tmp_path, admin_token="tok")
    status, _ = _call_get(gw, "/status", header_token="tok")
    assert status == 200


def test_strong_token_plus_confirm_approves(tmp_path, monkeypatch):
    gw = _gw(tmp_path, admin_token="a" * 32)
    gw.ready = True
    called: list[dict] = []
    monkeypatch.setattr(gw, "_send", lambda contact, text, **kw: (
        called.append({"contact": contact, "text": text, **kw})
        or {"ok": True, "status": "planned"}))

    status, data = _call(gw, "/api/approve", {"contact": "好友A", "text": "hi"},
                         token="a" * 32)
    assert status == 400 and data["error"] == "confirm_required"

    status, data = _call(gw, "/api/approve",
                         {"contact": "好友A", "text": "hi", "confirm": True},
                         token="a" * 32)
    assert status == 200 and called
    assert called[-1]["force"] is True and called[-1]["manual"] is True


def test_approve_still_respects_allowlist(tmp_path):
    gw = _gw(tmp_path, admin_token="a" * 32, allowed_contacts=["好友B"])
    gw.ready = True
    # handler 层仍是"contact 必须显式在白名单里"，approve 不能给非白名单开口子
    status, data = _call(gw, "/api/approve",
                         {"contact": "好友A", "text": "hi", "confirm": True},
                         token="a" * 32)
    assert status == 400 and "白名单" in data["error"]
    # 白名单内的联系人照常进入 _send（再走 STOP/静默/限流等闸门）
    status, data = _call(gw, "/api/approve",
                         {"contact": "好友B", "text": "hi", "confirm": True},
                         token="a" * 32)
    assert status == 200 and data.get("ok") is True


# --------------------------------------------------------------------------- #
# 9b. 批准类接口拒绝 ?token= query（即使 env 允许 query token）
# --------------------------------------------------------------------------- #
def test_approve_rejects_query_token_even_when_env_allows(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_ALLOW_QUERY_TOKEN", "1")
    gw = _gw(tmp_path)
    status, data = _call(gw, "/api/approve?token=tok_secure_123",
                         {"contact": "好友A", "text": "hi", "confirm": True},
                         token=None)
    assert status == 403 and data["error"] == "token_query_forbidden"
    assert _events(gw, "admin_token_query_rejected")

    item = drafts.add("好友A", "草稿内容", path=gw.draft_path)
    status, data = _call(gw, "/api/draft/approve?token=tok_secure_123",
                         {"id": item["id"], "confirm": True}, token=None)
    assert status == 403 and data["error"] == "token_query_forbidden"
    # 草稿没被批准/丢掉
    assert drafts.get(item["id"], path=gw.draft_path) is not None


def test_approve_header_token_still_works_without_query(tmp_path):
    gw = _gw(tmp_path)
    item = drafts.add("好友A", "草稿内容", path=gw.draft_path)
    status, data = _call(gw, "/api/draft/approve",
                         {"id": item["id"], "confirm": True})
    assert status == 200 and data["ok"] is True