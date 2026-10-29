# -*- coding: utf-8 -*-
"""P18-A 网关引擎启停：软停监听/自动回复/主动，人工调试发送仍允许。"""
from __future__ import annotations

import io
import json

from agi.wechat_gateway import config as gw_config
from agi.wechat_gateway.server import Gateway, make_handler


def _gw(tmp_path, **overrides) -> Gateway:
    config_path = tmp_path / "wechat" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = {
        "enabled": True,
        "engine_enabled": True,
        "port": 801,
        "dry_run": True,
        "allowed_contacts": ["好友A"],
        "monitored_contacts": ["好友A"],
        "auto_reply": True,
        "send_mode": "auto",
        "quiet_hours": [0, 0],
        "admin_token": "tok",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
    }
    cfg.update(overrides)
    config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return Gateway(cfg, config_path=config_path)


def _events(gateway: Gateway, kind: str) -> list[dict]:
    return [e for e in gateway.recent if e.get("kind") == kind]


def _call(gateway: Gateway, method: str, path: str, *, token="tok", body=b"{}"):
    handler_cls = make_handler(gateway)
    handler = handler_cls.__new__(handler_cls)
    handler.path = path
    handler.command = method
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.close_connection = True
    handler.wfile = io.BytesIO()
    handler._headers_buffer = []
    handler.rfile = io.BytesIO(body)
    handler.headers = {"Content-Length": str(len(body)), "Host": "127.0.0.1:801"}
    if token is not None:
        handler.headers["X-Admin-Token"] = token
    if method == "POST":
        handler.do_POST()
    else:
        handler.do_GET()
    raw = handler.wfile.getvalue().decode("utf-8")
    head, _, payload = raw.partition("\r\n\r\n")
    status = int(head.split(" ")[1]) if head else 0
    return status, json.loads(payload) if payload.strip() else None


def test_default_engine_enabled_and_status(tmp_path):
    gw = _gw(tmp_path)
    assert gw.engine_enabled is True
    assert gw.engine_state()["enabled"] is True
    status = gw.status()
    assert status["engine"]["enabled"] is True
    assert "since" in status["engine"] and "reason" in status["engine"]
    assert gw.on_inbound({"friend": "好友A", "text": "在吗"}) is None
    assert gw.queue.qsize() == 1


def test_stop_is_idempotent_audited_and_persisted(tmp_path):
    gw = _gw(tmp_path)
    result = gw.set_engine(False)
    assert result["ok"] is True and result["changed"] is True
    assert result["enabled"] is False
    assert len(_events(gw, "engine_stopped")) == 1

    again = gw.set_engine(False)
    assert again["ok"] is True and again["changed"] is False
    assert len(_events(gw, "engine_stopped")) == 1  # 幂等：不重复审计

    disk = json.loads(gw.config_path.read_text(encoding="utf-8"))
    assert disk["engine_enabled"] is False
    assert disk["admin_token"] == "tok"             # 其它键保留
    assert not list(gw.config_path.parent.glob("*.engine.tmp"))
    # 重启（从磁盘配置重建）后状态保持
    restarted = Gateway(disk, config_path=gw.config_path)
    assert restarted.engine_enabled is False
    assert restarted.status()["engine"]["enabled"] is False


def test_stop_drops_inbound_and_clears_queue(tmp_path):
    gw = _gw(tmp_path)
    gw.set_engine(False)
    gw.on_inbound({"friend": "好友A", "text": "这条不该回"})
    gw.on_inbound({"friend": "好友A", "text": "这条也不该回"})
    assert gw.queue.qsize() == 0
    assert not any(e["kind"] == "inbound" for e in gw.recent)
    paused = _events(gw, "engine_paused_skip")
    assert paused and paused[0]["skip_kind"] == "inbound"


def test_auto_reply_blocked_without_draft(tmp_path):
    gw = _gw(tmp_path)
    gw.set_engine(False)
    out = gw._reply({"friend": "好友A", "text": "在吗"})
    assert out == {"ok": False, "status": "engine_stopped", "error": "engine_stopped"}
    from pathlib import Path
    draft_path = Path(gw.draft_path)
    assert (not draft_path.exists()) or not draft_path.read_text(encoding="utf-8").strip()
    assert gw.queue.qsize() == 0


def test_send_auto_blocked_but_manual_allowed(tmp_path):
    gw = _gw(tmp_path)
    gw.set_engine(False)
    blocked = gw._send("好友A", "自动回一句")
    assert blocked["ok"] is False and blocked["status"] == "engine_stopped"
    manual = gw._send("好友A", "人工调试一句", force=True, manual=True)
    assert manual.get("status") != "blocked"
    assert manual.get("ok") is True


def test_proactive_cycle_paused(tmp_path):
    gw = _gw(tmp_path, proactive_enabled=True)
    gw.set_engine(False)
    outcome = gw._proactive_cycle()
    assert outcome["ran"] is False
    assert outcome["reason"] == "engine_stopped"


def test_start_resumes_and_is_idempotent(tmp_path):
    gw = _gw(tmp_path)
    gw.set_engine(False)
    started = gw.set_engine(True)
    assert started["ok"] is True and started["enabled"] is True
    assert len(_events(gw, "engine_started")) == 1
    assert gw._send("好友A", "恢复后自动回一句", reactive=True).get("status") != "engine_stopped"
    again = gw.set_engine(True)
    assert again["changed"] is False
    assert len(_events(gw, "engine_started")) == 1


def test_media_manual_allowed_auto_blocked(tmp_path):
    gw = _gw(tmp_path, media_enabled=True)
    gw.set_engine(False)
    auto = gw.send_media_batch("好友A", [{"type": "text", "text": "自动媒体"}])
    assert auto["ok"] is False
    assert auto["error"] == "engine_stopped"
    manual = gw.send_media_batch("好友A", [{"type": "text", "text": "人工媒体"}],
                                 manual=True)
    assert manual.get("ok") is True or manual.get("status") == "planned"


def test_api_engine_get_start_stop_auth_and_malformed(tmp_path):
    gw = _gw(tmp_path)
    status, data = _call(gw, "GET", "/api/engine")
    assert status == 200 and data["enabled"] is True

    status, data = _call(gw, "POST", "/api/engine/stop", token=None)
    assert status == 401 and gw.engine_enabled is True

    status, data = _call(gw, "POST", "/api/engine/stop")
    assert status == 200 and data["enabled"] is False
    status, data = _call(gw, "POST", "/api/engine/stop")  # 幂等
    assert status == 200 and data["enabled"] is False

    status, data = _call(gw, "GET", "/api/engine")
    assert status == 200 and data["enabled"] is False and data["manual"] is True

    status, data = _call(gw, "POST", "/api/engine/start")
    assert status == 200 and data["enabled"] is True

    status, data = _call(gw, "POST", "/api/engine/start", body=b"not json")
    assert status == 400 and data["ok"] is False


def test_status_has_engine_after_toggle(tmp_path):
    gw = _gw(tmp_path)
    gw.set_engine(False)
    status = gw.status()
    assert status["engine"]["enabled"] is False
    assert status["engine"]["reason"] in ("manual_stop", "config_disabled")
    assert status["ok"] is True


def test_engine_vs_stop_file(tmp_path):
    gw = _gw(tmp_path)
    gw.set_engine(False)
    # engine 软停：人工发送允许
    assert gw._send("好友A", "人工", force=True, manual=True).get("status") != "blocked"
    # STOP 紧急全停：人工 force 也拦
    gw.set_stop(True)
    out = gw._send("好友A", "STOP 后人工", force=True, manual=True)
    assert out["status"] == "blocked" and out["error"] == "stop_file"
