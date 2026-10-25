# -*- coding: utf-8 -*-
"""P4-a 本地调试前端后端测试（全部 tmp_path，不碰真实库/不联网/不起端口）。"""
from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "console_server", ROOT / "tools" / "console" / "server.py")
console = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(console)


def _make_db(path: Path, *, inner_rows=2, message_rows=3) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE messages(id TEXT PRIMARY KEY, role TEXT, text TEXT, "
                "ts REAL, proactive INTEGER DEFAULT 0, archived_at REAL)")
    con.execute("CREATE TABLE inner_states(id TEXT PRIMARY KEY, turn_id TEXT, mode TEXT, "
                "state_json TEXT, block TEXT, injected INTEGER, latency_ms REAL, "
                "fallback INTEGER DEFAULT 0, source TEXT, created_at REAL)")
    con.execute("CREATE TABLE user_memories(id TEXT PRIMARY KEY, scope TEXT, kind TEXT, "
                "content TEXT, evidence TEXT, confidence REAL, status TEXT, "
                "created_at REAL, importance REAL DEFAULT 0.5)")
    now = 1_800_000_000.0
    for index in range(message_rows):
        con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                    (f"M-{index}", "user" if index % 2 else "assistant",
                     f"消息 {index}" + ("。" if index == 0 else "？"), now + index))
    for index in range(inner_rows):
        con.execute(
            "INSERT INTO inner_states(id,turn_id,mode,state_json,block,injected,"
            "latency_ms,fallback,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"IS-{index}", f"T-{index}", "model",
             json.dumps({"monologue": f"想法{index}", "stance": "平稳",
                         "behavior": {"length": "short", "ask": False}}, ensure_ascii=False),
             f"<inner_state>{index}</inner_state>", 1, 120.5, 0, "model", now + index))
    con.execute("INSERT INTO user_memories(id,scope,kind,content,evidence,confidence,"
                "status,created_at,importance) VALUES(?,?,?,?,?,?,?,?,?)",
                ("UM-1", "user", "preference", "用户喜欢猫", "我喜欢猫", 0.8,
                 "active", now, 0.5))
    con.commit()
    con.close()
    return path


# ============================================================ feed
def test_feed_parses_and_filters(tmp_path, monkeypatch):
    log = tmp_path / "gateway.log"
    events = [
        {"ts": 1.0, "kind": "gateway_started", "dry_run": True},
        {"ts": 2.0, "kind": "inbound", "friend": "好友A", "text": "你好"},
        {"ts": 3.0, "kind": "outbound", "friend": "好友A", "text": "在", "status": "sent"},
        {"ts": 4.0, "kind": "send_blocked", "friend": "好友A", "reason": "quiet_hours"},
        {"ts": 5.0, "kind": "proactive_send", "friend": "好友A", "text": "在吗"},
        {"ts": 6.0, "kind": "inbound", "friend": "好友B", "text": "早"},
    ]
    lines = [json.dumps(e, ensure_ascii=False) for e in events]
    lines.insert(3, "broken")           # 中间夹一行坏数据，解析应跳过
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(console, "GATEWAY_LOG", log)

    items = console.feed_events(limit=10)
    assert [item["kind"] for item in items] == [
        "gateway_started", "inbound", "outbound", "send_blocked", "proactive_send", "inbound"][::-1]
    assert console.feed_events(limit=2)[0]["kind"] == "inbound"
    only = console.feed_events(limit=10, kind_filter="inbound,outbound")
    # 新→旧：inbound(6) → outbound(3) → inbound(2)
    assert [item["kind"] for item in only] == ["inbound", "outbound", "inbound"]
    assert console.feed_events(limit=0)  # limit 兜底不崩
    # 缺失日志 → 空列表
    monkeypatch.setattr(console, "GATEWAY_LOG", tmp_path / "missing.log")
    assert console.feed_events() == []


# ============================================================ 联系人库只读
def test_inner_messages_and_memory_snapshot(tmp_path, monkeypatch):
    db = _make_db(tmp_path / "contact.sqlite3")
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", db)
    inner = console.read_inner(limit=10)
    assert inner["ok"] and len(inner["items"]) == 2
    first = inner["items"][0]
    assert first["state"]["monologue"] == "想法1"        # 新→旧
    assert first["injected"] is True and first["latency_ms"] == 120.5

    messages = console.read_messages(limit=10)
    assert messages["ok"] and len(messages["items"]) == 3
    assert "messages" not in [key for key in messages["items"][0] if key == "state_json"]

    snapshot = console.memory_snapshot()
    assert snapshot["ok"] is True
    assert snapshot["metrics"]["memories"]["total"] == 1
    assert snapshot["recent"][0]["content"] == "用户喜欢猫"


def test_contact_db_missing_returns_empty_lists(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", None)
    monkeypatch.setattr(console, "CONTACTS_DIR", tmp_path / "none")
    assert console.contact_db_path() is None
    assert console.read_inner()["items"] == []
    assert console.read_messages()["items"] == []
    assert console.memory_snapshot()["ok"] is False


# ============================================================ 语气
def test_tone_snapshot_uses_baseline(tmp_path, monkeypatch):
    db = _make_db(tmp_path / "tone.sqlite3", message_rows=0, inner_rows=0)
    con = sqlite3.connect(db)
    now = 1_800_000_000.0
    for index, text in enumerate(("在吗？", "真的服了。", "哈哈哈哈", "今天有点累")):
        con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                    (f"T-{index}", "assistant", text, now + index))
    con.commit()
    con.close()
    profile = tmp_path / "profile.md"
    profile.write_text("```json\n" + json.dumps({
        "length": {"median": 7, "mean": 9.0},
        "ending": {"无标点": 0.95, "问号": 0.04, "句号": 0.01, "感叹号": 0.0, "省略号": 0.0},
        "emoji_rate": 0.01, "self_rows": 467,
    }, ensure_ascii=False) + "\n```\n", encoding="utf-8")
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", db)
    monkeypatch.setattr(console, "TONE_PROFILE", profile)
    snapshot = console.tone_snapshot(limit=50)
    assert snapshot["ok"] and snapshot["sample_n"] == 4
    ai = snapshot["ai"]
    assert ai["ending"]["question"] == pytest.approx(0.25)   # 1 条问号
    assert ai["ending"]["period"] == pytest.approx(0.25)     # 1 条句号
    assert ai["filler_rate"] == pytest.approx(0.5)           # 真的服了 / 哈哈哈哈
    assert snapshot["baseline"]["length_median"] == 7
    assert "period_delta" in snapshot["gap"]


# ============================================================ 写操作 helpers
@pytest.fixture
def write_env(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"admin_token": "tok", "proactive_enabled": True},
                                 ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(console, "GATEWAY_CONFIG", config)
    monkeypatch.setattr(console, "STOP_PATH", tmp_path / "STOP")
    monkeypatch.setattr(console, "AUDIT_PATH", tmp_path / "audit.log")
    monkeypatch.setattr(console, "PROMPTS_PATH", tmp_path / "prompts.json")
    return {"config": config, "tmp": tmp_path}


def test_stop_resume_and_audit(write_env):
    assert console.set_stop(True)["ok"] is True
    assert console.STOP_PATH.exists() and console.stop_active() is True
    assert console.set_stop(True)["ok"] is True          # 幂等
    assert console.set_stop(False)["ok"] is True
    assert not console.STOP_PATH.exists()
    audits = (write_env["tmp"] / "audit.log").read_text(encoding="utf-8").splitlines()
    assert len(audits) >= 3 and json.loads(audits[-1])["action"] == "proactive_resume"


def test_apply_config_whitelist_and_validation(write_env):
    ok = console.apply_config({"proactive_enabled": False,
                               "quiet_hours": [22, 8],
                               "proactive_probability_min": 0.2,
                               "proactive_probability_max": 0.8,
                               "inner_mode": "model",
                               "inner_shadow_only": True,
                               "inner_mode_scope": "all",
                               "inner_schema": "slim"})
    assert ok["ok"] is True
    assert ok["config"]["proactive_enabled"] is False
    saved = json.loads(write_env["config"].read_text(encoding="utf-8"))
    assert saved["proactive_enabled"] is False and saved["inner_mode"] == "model"

    bad = console.apply_config({"admin_token": "hacked"})
    assert bad["ok"] is False and any("not_allowed" in error for error in bad["errors"])
    assert json.loads(write_env["config"].read_text(encoding="utf-8"))["admin_token"] == "tok"

    invalid = console.apply_config({"proactive_interval_min_s": 5000,
                                    "proactive_interval_max_s": 100})
    assert invalid["ok"] is False


def test_save_prompts_merge_and_validation(write_env):
    result = console.save_prompts({"proactive_style": "短句，不要句号"})
    assert result["ok"] is True
    saved = json.loads(write_env["tmp"].joinpath("prompts.json").read_text(encoding="utf-8"))
    assert saved == {"version": 1, "prompts": {"proactive_style": "短句，不要句号"}}
    console.save_prompts({"care": "问近况"})
    merged = json.loads(write_env["tmp"].joinpath("prompts.json").read_text(encoding="utf-8"))
    assert set(merged["prompts"]) == {"proactive_style", "care"}
    assert console.save_prompts({"../bad": "x"})["ok"] is False
    assert console.save_prompts({})["ok"] is False


def test_model_allowlist_rejects_reserved(write_env):
    result = console.start_model("qwen15")
    assert result["ok"] is False and result["error"] == "model_not_editable"
    assert console.stop_model("qwen15")["ok"] is False
    assert console.start_model("")["ok"] is False


def test_model_start_missing_gguf_returns_error(write_env, tmp_path, monkeypatch):
    fake = type("FakeRegistry", (), {
        "start": staticmethod(lambda name: {"ok": False, "error": "gguf_not_found",
                                           "path": "missing.gguf"}),
        "stop": staticmethod(lambda name: {"ok": True, "pid": 1}),
    })
    monkeypatch.setitem(sys.modules, "agi.selflearn.model_registry", fake)
    result = console.start_model("soul-05b")
    assert result["ok"] is False and result["error"] == "gguf_not_found"
    assert console.stop_model("soul-05b")["ok"] is True


def test_models_snapshot_with_fake_registry(write_env, monkeypatch):
    fake = type("FakeRegistry", (), {
        "list_models": staticmethod(lambda: [
            {"name": "soul-05b", "port": 8123, "role": "soul", "status": "ready"},
            {"name": "qwen15", "port": 8124, "role": "reserved", "status": "reserved"},
        ]),
        "health": staticmethod(lambda name: name == "soul-05b"),
    })
    monkeypatch.setitem(sys.modules, "agi.selflearn.model_registry", fake)
    snapshot = console.models_snapshot()
    assert snapshot["ok"] is True
    by_name = {item["name"]: item for item in snapshot["models"]}
    assert by_name["soul-05b"]["healthy"] is True and by_name["soul-05b"]["editable"]
    assert by_name["qwen15"]["reserved"] and not by_name["qwen15"]["editable"]


# ============================================================ HTTP Handler
def _call(method: str, path: str, body=None, *, token=None,
          host="127.0.0.1:802", static_dir=None, monkeypatch=None):
    raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler = console.Handler.__new__(console.Handler)
    handler.path = path
    handler.command = method
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.close_connection = True
    handler.wfile = io.BytesIO()
    handler._headers_buffer = []
    handler.rfile = io.BytesIO(raw)
    handler.headers = {"Host": host, "Content-Length": str(len(raw))}
    if token:
        handler.headers["X-Admin-Token"] = token
    getattr(handler, "do_" + method)()
    head, _, payload = handler.wfile.getvalue().decode("utf-8").partition("\r\n\r\n")
    status = int(head.split(" ")[1]) if head else 0
    if not payload.strip():
        return status, None
    try:
        return status, json.loads(payload)
    except ValueError:
        return status, payload


def test_http_get_host_guard_and_health(write_env):
    status, data = _call("GET", "/api/health")
    assert status == 200 and data["ok"] is True
    status, data = _call("GET", "/api/health", host="evil.example.com:802")
    assert status == 403 and data["ok"] is False
    status, data = _call("GET", "/api/config")
    assert status == 200 and data["config"]["proactive_enabled"] is True
    status, data = _call("GET", "/api/feed")
    assert status == 200 and isinstance(data["items"], list)


def test_http_static_serving_and_traversal(tmp_path, write_env, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>console</html>", encoding="utf-8")
    monkeypatch.setattr(console, "STATIC_DIR", static)
    status, _ = _call("GET", "/")
    assert status == 200
    status, _ = _call("GET", "/static/../server.py")
    assert status == 404


def test_http_write_requires_token(write_env):
    status, data = _call("POST", "/api/proactive/stop", {})
    assert status == 401 and data["ok"] is False
    status, data = _call("POST", "/api/proactive/stop", {}, token="wrong")
    assert status == 401
    status, data = _call("POST", "/api/proactive/stop", {}, token="tok")
    assert status == 200 and data["stopped"] is True
    assert console.STOP_PATH.exists()
    status, data = _call("POST", "/api/proactive/resume", {}, token="tok")
    assert status == 200 and data["stopped"] is False
    assert not console.STOP_PATH.exists()


def test_http_config_and_prompts(write_env):
    status, data = _call("POST", "/api/config", {"updates": {"proactive_enabled": False}},
                         token="tok")
    assert status == 200 and data["ok"] is True
    assert data["config"]["proactive_enabled"] is False
    status, data = _call("POST", "/api/config", {"updates": {"admin_token": "x"}},
                         token="tok")
    assert status == 400 and data["ok"] is False
    status, data = _call("POST", "/api/prompts",
                         {"prompts": {"care": "问近况"}}, token="tok")
    assert status == 200 and data["ok"] is True
    status, data = _call("GET", "/api/prompts")
    assert status == 200 and "care" in data["prompts"]


def test_http_models_route_rejects_reserved(write_env, monkeypatch):
    monkeypatch.setattr(console, "models_snapshot",
                        lambda: {"ok": True, "models": [], "allow_start": ["soul-05b"]})
    status, data = _call("POST", "/api/models/start", {"name": "qwen15"}, token="tok")
    assert status == 400 and data["error"] == "model_not_editable"


def test_http_missing_data_does_not_crash(tmp_path, write_env, monkeypatch):
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", None)
    monkeypatch.setattr(console, "CONTACTS_DIR", tmp_path / "empty")
    for path in ("/api/inner", "/api/messages", "/api/memory", "/api/tone"):
        status, data = _call("GET", path)
        assert status == 200
        assert data is not None
