# -*- coding: utf-8 -*-
"""P4-a 本地调试前端后端测试（全部 tmp_path，不碰真实库/不联网/不起端口）。"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import sqlite3
import sys
import time
import types
import urllib.error
import urllib.parse
import zipfile
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


@pytest.fixture
def prompt_env(tmp_path, monkeypatch, write_env):
    """把 prompt_store 的文件路径指到 tmp（测试绝不碰真实 agi/data/prompts.json）。"""
    path = tmp_path / "prompts.json"
    monkeypatch.setenv("PROMPTS_PATH", str(path))
    try:
        from agi.selflearn import prompt_store
        monkeypatch.setattr(prompt_store, "PROMPTS_PATH", path)
        monkeypatch.setattr(prompt_store, "_CACHE_MTIME", None)
        monkeypatch.setattr(prompt_store, "_CACHE_PAYLOAD", None)
    except Exception:  # noqa: BLE001 - 没有 prompt_store 时走降级测试
        prompt_store = None
    monkeypatch.setattr(console, "PROMPTS_PATH", path)
    return {"path": path, "store": prompt_store}


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


def test_prompts_state_uses_prompt_store(prompt_env):
    """GET 提示词：11 键 defaults 与 prompt_store.defaults() 一致，且只读不写盘。"""
    state = console.prompts_state()
    assert state["ok"] is True and state["degraded"] is False
    store = prompt_env["store"]
    expected_keys = list(store.KNOWN_KEYS)
    assert len(state["defaults"]) == 11
    assert set(state["defaults"]) == set(expected_keys)
    assert state["defaults"] == store.defaults()
    assert set(state["effective"]) == set(expected_keys)
    assert state["overrides"] == {}
    assert state["labels"]["chat.system"]["label"] == "被动对话 SYSTEM"
    assert state["config"]["max_chars"] == 4000
    assert not prompt_env["path"].exists()          # 只读不创建文件
    assert len(state["config"]["keys"]) == 11


def test_prompts_save_and_reset_via_store(prompt_env):
    result = console.save_prompts({"proactive.kind.care": "新的 care 指令"})
    assert result["ok"] is True and "proactive.kind.care" in result["changed_keys"]
    assert result["overrides"]["proactive.kind.care"] == "新的 care 指令"
    assert prompt_env["path"].exists()
    audits = console.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["action"] == "prompt_save" for line in audits)

    reset = console.reset_prompts(keys=["proactive.kind.care"])
    assert reset["ok"] is True and reset["overrides"] == {}
    assert any(json.loads(line)["action"] == "prompt_reset"
               for line in console.AUDIT_PATH.read_text(encoding="utf-8").splitlines())

    console.save_prompts({"proactive.kind.share": "share 覆盖", "chat.system": "SYSTEM 覆盖"})
    assert len(console.prompts_state()["overrides"]) == 2
    all_reset = console.reset_prompts(all=True)
    assert all_reset["ok"] is True and all_reset["overrides"] == {}
    assert not prompt_env["path"].exists()          # delete_all 删除文件


def test_prompts_unknown_key_rejected_by_store(prompt_env):
    result = console.save_prompts({"not.a.key": "x"})
    assert result["ok"] is False
    assert console.prompts_state()["overrides"] == {}
    assert console.reset_prompts(keys=["not.a.key"])["ok"] is False


def test_prompts_degraded_fallback(write_env, monkeypatch):
    """import 不到 prompt_store 时降级：直接读写 JSON，不崩。"""
    monkeypatch.setattr(console, "_prompt_store", lambda: None)
    state = console.prompts_state()
    assert state["ok"] is True and state["degraded"] is True
    assert state["defaults"] == {}
    assert len(state["labels"]) == 11
    saved = console.save_prompts({"care": "降级 care"})
    assert saved["ok"] is True and saved["degraded"] is True
    assert saved["overrides"] == {"care": "降级 care"}
    assert json.loads(console.PROMPTS_PATH.read_text(encoding="utf-8"))["prompts"] == {
        "care": "降级 care"}
    reset = console.reset_prompts(keys=["care"])
    assert reset["ok"] is True and reset["overrides"] == {}
    console.save_prompts({"care": "a", "share": "b"})
    all_reset = console.reset_prompts(all=True)
    assert all_reset["ok"] is True and all_reset["overrides"] == {}
    assert not console.PROMPTS_PATH.exists()
    assert console.save_prompts({})["ok"] is False


def test_model_allowlist_rejects_reserved(write_env):
    result = console.start_model("qwen15")
    assert result["ok"] is False and result["error"] == "model_not_editable"
    assert console.stop_model("qwen15")["ok"] is False
    assert console.start_model("")["ok"] is False


def test_model_start_missing_gguf_returns_error(write_env, tmp_path, monkeypatch):
    """完全自包含：tmp registry 里放一条 gguf 不存在的模型；不碰真实配置/端口。"""
    registry_path = tmp_path / "local_models.json"
    missing_gguf = tmp_path / "not_there.gguf"
    registry_path.write_text(json.dumps({
        "llama_bin": str(tmp_path / "llama"),
        "models": [{"name": "soul-05b", "gguf": str(missing_gguf), "port": 18081,
                    "role": "soul", "status": "ready"}],
    }, ensure_ascii=False), encoding="utf-8")
    from agi.selflearn import model_registry as real_registry   # 纯函数，不触发 spawn
    fake = type("TmpRegistry", (), {
        "list_models": staticmethod(lambda: real_registry.list_models(registry_path)),
        "health": staticmethod(lambda name: False),
        "start": staticmethod(lambda name: real_registry.start(name, path=registry_path)),
        "stop": staticmethod(lambda name: {"ok": True, "pid": 1}),
    })
    monkeypatch.setattr(console, "_model_registry", lambda: fake)
    result = console.start_model("soul-05b")
    assert result["ok"] is False
    assert result["error"] == "gguf_not_found"
    assert result["path"] == str(missing_gguf)
    assert console.stop_model("soul-05b")["ok"] is True


def test_model_registry_unavailable_returns_error(write_env, monkeypatch):
    monkeypatch.setattr(console, "_model_registry", lambda: None)
    assert console.start_model("soul-05b")["error"] == "model_registry unavailable"
    assert console.stop_model("soul-05b")["error"] == "model_registry unavailable"
    snapshot = console.models_snapshot()
    assert snapshot["ok"] is False and snapshot["models"] == []


def test_models_snapshot_with_fake_registry(write_env, monkeypatch):
    fake = type("FakeRegistry", (), {
        "list_models": staticmethod(lambda: [
            {"name": "soul-05b", "port": 8123, "role": "soul", "status": "ready"},
            {"name": "qwen15", "port": 8124, "role": "reserved", "status": "reserved"},
        ]),
        "health": staticmethod(lambda name: name == "soul-05b"),
    })
    monkeypatch.setattr(console, "_model_registry", lambda: fake)
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


def test_http_get_host_guard_and_health(write_env, tmp_path, monkeypatch):
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", None)
    monkeypatch.setattr(console, "CONTACTS_DIR", tmp_path / "empty")
    monkeypatch.setattr(console, "GATEWAY_LOG", tmp_path / "gateway.log")
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


def test_http_config_and_prompts(prompt_env):
    status, data = _call("POST", "/api/config", {"updates": {"proactive_enabled": False}},
                         token="tok")
    assert status == 200 and data["ok"] is True
    assert data["config"]["proactive_enabled"] is False
    status, data = _call("POST", "/api/config", {"updates": {"admin_token": "x"}},
                         token="tok")
    assert status == 400 and data["ok"] is False
    status, data = _call("POST", "/api/prompts",
                         {"prompts": {"proactive.kind.care": "问近况"}}, token="tok")
    assert status == 200 and data["ok"] is True
    assert data["overrides"]["proactive.kind.care"] == "问近况"
    status, data = _call("GET", "/api/prompts")
    assert status == 200 and len(data["defaults"]) == 11
    assert data["overrides"]["proactive.kind.care"] == "问近况"
    status, data = _call("POST", "/api/prompts/reset",
                         {"keys": ["proactive.kind.care"]}, token="tok")
    assert status == 200 and data["ok"] is True and data["overrides"] == {}
    status, data = _call("POST", "/api/prompts/reset", {"all": True}, token="tok")
    assert status == 200 and data["ok"] is True


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


# ============================================================ P4-c 恢复出厂
def _factory_env(tmp_path, monkeypatch, write_env):
    """把 reset 涉及的全部路径重定向到 tmp（含 data/、log/、backup/）。"""
    data_dir = tmp_path / "data"
    gateway_dir = data_dir / "wechat"
    selflearn = data_dir / "selflearn"
    contacts = selflearn / "wechat_contacts"
    log_dir = tmp_path / "log"
    backup_dir = tmp_path / "backups"
    for directory in (gateway_dir, contacts, log_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (selflearn / "media").mkdir(parents=True, exist_ok=True)
    (selflearn / "inner.log").write_text("inner log\n", encoding="utf-8")

    contact_db = contacts / "contact.sqlite3"
    con = sqlite3.connect(contact_db)
    con.execute("CREATE TABLE messages(id TEXT, role TEXT, text TEXT, ts REAL)")
    con.execute("CREATE TABLE user_memories(id TEXT, content TEXT)")
    con.execute("CREATE TABLE claims(id TEXT)")
    con.execute("CREATE TABLE inner_states(id TEXT)")
    con.executemany("INSERT INTO messages VALUES(?,?,?,?)",
                    [(f"M{i}", "user", f"消息{i}", 1.0 + i) for i in range(3)])
    con.executemany("INSERT INTO user_memories VALUES(?,?)",
                    [("UM1", "用户喜欢猫"), ("UM2", "用户住杭州")])
    con.execute("INSERT INTO claims VALUES('C1')")
    con.execute("INSERT INTO inner_states VALUES('IS1')")
    con.commit()
    con.close()
    default_db = selflearn / "selflearn.sqlite3"
    sqlite3.connect(default_db).close()

    gateway_log = gateway_dir / "gateway.log"
    gateway_log.write_text("\n".join(json.dumps({"ts": i, "kind": "inbound"})
                                     for i in range(4)) + "\n", encoding="utf-8")
    drafts = gateway_dir / "drafts.jsonl"
    drafts.write_text('{"id":"D1"}\n', encoding="utf-8")
    prompts = data_dir / "prompts.json"
    prompts.write_text(json.dumps({"version": 1, "prompts": {"care": "问近况"}},
                                  ensure_ascii=False), encoding="utf-8")
    log_messages = log_dir / "messages.jsonl"
    log_messages.write_text("line1\nline2\n", encoding="utf-8")
    log_chat = log_dir / "聊天记录.txt"
    log_chat.write_text("聊天记录\n", encoding="utf-8")

    monkeypatch.setattr(console, "DATA_DIR", data_dir)
    monkeypatch.setattr(console, "SELFLEARN_DIR", selflearn)
    monkeypatch.setattr(console, "CONTACTS_DIR", contacts)
    monkeypatch.setattr(console, "GATEWAY_LOG", gateway_log)
    monkeypatch.setattr(console, "DRAFTS_PATH", drafts)
    monkeypatch.setattr(console, "PROMPTS_PATH", prompts)
    monkeypatch.setattr(console, "LOG_DIR", log_dir)
    monkeypatch.setattr(console, "LOG_MESSAGES", log_messages)
    monkeypatch.setattr(console, "LOG_CHAT", log_chat)
    monkeypatch.setattr(console, "BACKUP_DIR", backup_dir)
    # write_env 已经把 config/STOP/audit/prompts 指到 tmp 根；proimpts 上面覆盖成 data 下
    return {
        "data": data_dir, "selflearn": selflearn, "contacts": contacts,
        "gateway_log": gateway_log, "drafts": drafts, "prompts": prompts,
        "log_messages": log_messages, "log_chat": log_chat, "backup": backup_dir,
        "config": write_env["config"],
    }


def test_factory_preview_counts_without_deleting(tmp_path, write_env, monkeypatch):
    env = _factory_env(tmp_path, monkeypatch, write_env)
    status, data = _call("POST", "/api/factory/preview", {})
    assert status == 200 and data["ok"] is True
    stats = data["stats"]
    assert stats["contacts"] == 1
    assert stats["messages"] == 3
    assert stats["memories"] == 2
    assert stats["claims"] == 1
    assert stats["inner_states"] == 1
    assert stats["prompt_overrides"] == 1
    assert stats["gateway_events"] == 4
    assert data["phrase_hint"] == console.FACTORY_PHRASE
    assert data["backup_preview"].endswith(".zip")
    # 只读：一个文件都没删、备份也没建
    assert env["gateway_log"].exists() and env["prompts"].exists()
    assert env["contacts"].exists()
    assert not env["backup"].exists()


def test_factory_reset_rejects_wrong_confirm_or_missing_token(tmp_path, write_env, monkeypatch):
    env = _factory_env(tmp_path, monkeypatch, write_env)
    status, data = _call("POST", "/api/factory/reset",
                         {"confirm": "错的短语"}, token="tok")
    assert status == 400 and data["ok"] is False
    assert env["gateway_log"].exists() and env["prompts"].exists()
    assert env["contacts"].exists()

    status, data = _call("POST", "/api/factory/reset",
                         {"confirm": console.FACTORY_PHRASE})
    assert status == 401 and data["ok"] is False
    assert env["gateway_log"].exists() and env["contacts"].exists()
    assert not list(env["backup"].glob("*.zip")) if env["backup"].exists() else True


def test_factory_reset_backup_then_clear(tmp_path, write_env, monkeypatch):
    env = _factory_env(tmp_path, monkeypatch, write_env)
    status, data = _call("POST", "/api/factory/reset",
                         {"confirm": console.FACTORY_PHRASE, "backup": True},
                         token="tok")
    assert status == 200 and data["ok"] is True, data
    backup_path = Path(data["backup_path"])
    assert backup_path.exists() and backup_path.suffix == ".zip"
    with zipfile.ZipFile(backup_path) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
    assert any(name.endswith("contact.sqlite3") for name in names)
    assert any(name.endswith("gateway.log") for name in names)

    # 数据被清空：selflearn 目录只剩空壳、日志/提示词/草稿都没了
    assert env["selflearn"].exists()
    assert list(env["selflearn"].iterdir()) == []
    assert not env["gateway_log"].exists()
    assert not env["drafts"].exists()
    assert not env["prompts"].exists()
    assert not env["log_messages"].exists()
    assert not env["log_chat"].exists()
    # 保留 config
    assert env["config"].exists()
    deleted = data["deleted"]
    assert deleted["messages"] == 3 and deleted["memories"] == 2
    assert deleted["contacts"] == 1
    assert "restart_hint" in data


def test_factory_reset_backup_failure_aborts(tmp_path, write_env, monkeypatch):
    env = _factory_env(tmp_path, monkeypatch, write_env)
    blocked = tmp_path / "blocked_backup"
    blocked.write_text("not a dir", encoding="utf-8")
    monkeypatch.setattr(console, "BACKUP_DIR", blocked)
    status, data = _call("POST", "/api/factory/reset",
                         {"confirm": console.FACTORY_PHRASE, "backup": True},
                         token="tok")
    assert status == 400 and data["ok"] is False
    assert "backup_failed" in data["error"]
    assert env["gateway_log"].exists() and env["prompts"].exists()
    assert env["contacts"].exists()


def test_factory_reset_whitelist_rejects_outside_path(tmp_path, write_env, monkeypatch):
    env = _factory_env(tmp_path, monkeypatch, write_env)
    outside = tmp_path / "outside.sqlite3"
    outside.write_text("keep me", encoding="utf-8")
    inside_root = tmp_path / "data"
    monkeypatch.setattr(console, "reset_roots", lambda: [inside_root])
    monkeypatch.setattr(console, "reset_targets", lambda: ([], [outside]))
    result = console.factory_reset(console.FACTORY_PHRASE, backup=True, client="test")
    assert result["ok"] is False and "unsafe_target" in result["error"]
    assert outside.exists()
    assert env["gateway_log"].exists()

# ============================================================ P5-d 媒体页
def _media_env(tmp_path, monkeypatch, write_env):
    emoji = tmp_path / "emoji_assets"
    stickers = tmp_path / "stickers"
    inbound = tmp_path / "inbound_media"
    for directory in (emoji, stickers, inbound):
        directory.mkdir(parents=True, exist_ok=True)
    (emoji / "cat.png").write_bytes(b"PNG-cat")
    (emoji / "dog.jpg").write_bytes(b"JPG-dog")
    (stickers / "happy.gif").write_bytes(b"GIF88a-happy")
    (stickers / "notes.txt").write_text("not an image", encoding="utf-8")
    wishes = tmp_path / "media_wishes.jsonl"
    wishes.write_text(
        json.dumps({"wish": "想发一只布偶猫的表情", "source": "model",
                    "created_at": 1800000000.0}, ensure_ascii=False) + "\n"
        + json.dumps({"wish": "想发「晚安」贴纸"}, ensure_ascii=False) + "\n"
        + "broken line\n", encoding="utf-8")
    monkeypatch.setattr(console, "MEDIA_ASSET_DIRS", (emoji, stickers, inbound))
    monkeypatch.setattr(console, "MEDIA_WISHES_PATH", wishes)
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", None)
    monkeypatch.setattr(console, "CONTACTS_DIR", tmp_path / "no_contacts")
    return {"emoji": emoji, "stickers": stickers, "wishes": wishes, "tmp": tmp_path}


def test_media_capabilities_static_matrix(tmp_path, write_env, monkeypatch):
    monkeypatch.setattr(console, "_gateway_media", lambda: None)
    monkeypatch.setattr(console, "_sticker_native", lambda: None)
    caps = console.media_capabilities()
    assert caps["ok"] is True and caps["source"] == "static"
    assert caps["test_contact"] == "文件传输助手"
    matrix = {row["key"]: row for row in caps["matrix"]}
    assert len(matrix) == 8
    assert matrix["text"]["status"] == "ok"
    assert matrix["unicode_emoji"]["status"] == "ok"
    assert matrix["image"]["status"] == "ok"
    assert matrix["gif"]["status"] == "ok"
    assert matrix["sticker_as_image"]["status"] == "degraded"
    assert matrix["sticker_native"]["status"] == "reserved"
    assert matrix["sticker_collect"]["status"] == "no"
    assert matrix["mixed"]["status"] == "ok"


def test_media_capabilities_real_media_module(write_env):
    caps = console.media_capabilities()
    assert caps["ok"] is True
    assert caps["source"] == "agi.wechat_gateway.media"
    keys = {row["key"] for row in caps["matrix"]}
    assert {"sticker_native", "sticker_collect", "mixed"} <= keys


def test_media_assets_scan_and_thumbnail_endpoint(tmp_path, write_env, monkeypatch):
    env = _media_env(tmp_path, monkeypatch, write_env)
    assets = console.media_assets(limit=50)
    assert assets["ok"] is True and assets["source"] == "scan"
    assert assets["total"] == 3
    kinds = {item["kind"] for item in assets["items"]}
    assert kinds == {"image", "gif"}
    assert all(item["thumb"].startswith("/api/media/file?path=") for item in assets["items"])
    assert all(item["file_exists"] for item in assets["items"])

    status, data = _call("GET", "/api/media/assets?limit=10")
    assert status == 200 and data["total"] == 3

    target = env["emoji"] / "cat.png"
    status, body = _call("GET", "/api/media/file?path=" + urllib.parse.quote(str(target)))
    assert status == 200 and body == "PNG-cat"

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"secret")
    status, data = _call("GET", "/api/media/file?path=" + urllib.parse.quote(str(outside)))
    assert status == 404 and data["ok"] is False


def test_media_assets_from_emoji_archive(tmp_path, write_env, monkeypatch):
    monkeypatch.setattr(console, "MEDIA_ASSET_DIRS", (tmp_path / "no_media",))
    db = tmp_path / "archive.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE emoji_archive(id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "path TEXT NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]', "
                "usage_count INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)")
    image = tmp_path / "assets" / "cat.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"PNG")
    con.execute("INSERT INTO emoji_archive VALUES(?,?,?,?,?,?)",
                ("EMO-1", "cat", str(image), '["猫","可爱"]', 7, 1800000000.0))
    con.execute("INSERT INTO emoji_archive VALUES(?,?,?,?,?,?)",
                ("EMO-2", "dog", str(tmp_path / "missing.png"), "[]", 0, 1800000001.0))
    con.commit()
    con.close()
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", db)
    assets = console.media_assets(limit=10)
    assert assets["source"] == "emoji_archive" and assets["total"] == 2
    first = assets["items"][0]
    assert first["id"] == "EMO-1" and first["usage_count"] == 7
    assert first["tags"] == ["猫", "可爱"]
    assert assets["items"][1]["file_exists"] is False
    stats = console.media_stats()
    assert stats["source"] == "emoji_archive" and stats["total"] == 2
    assert stats["used"] == 1 and stats["unused"] == 1   # cat 有 usage_count=7
    assert stats["by_ext"]["png"] == 2
    assert stats["top"] and stats["top"][0]["name"] == "cat"


def test_media_wishes_reads_jsonl_and_missing_file(tmp_path, write_env, monkeypatch):
    env = _media_env(tmp_path, monkeypatch, write_env)
    wishes = console.media_wishes()
    assert wishes["ok"] is True and wishes["count"] == 2
    assert "布偶猫" in wishes["items"][0]["wish"]
    assert wishes["note"].startswith("获取/下载管线预留")
    monkeypatch.setattr(console, "MEDIA_WISHES_PATH", tmp_path / "missing.jsonl")
    assert console.media_wishes()["count"] == 0


def test_media_send_forces_file_transfer_assistant(tmp_path, write_env, monkeypatch):
    monkeypatch.setattr(console, "_gateway_media", lambda: None)
    captured = {}

    def fake_post(payload):
        captured.update(payload)
        return {"ok": True, "status": "sent", "contact": payload["contact"]}

    monkeypatch.setattr(console, "_post_gateway_media", fake_post)
    status, data = _call("POST", "/api/media/send",
                         {"type": "text", "text": "测试 emoji 😄",
                          "contact": "真实好友A"}, token="tok")
    assert status == 200 and data["ok"] is True
    assert data["contact"] == "文件传输助手" and data["test"] is True
    assert captured["contact"] == "文件传输助手"      # body 里的联系人被强制覆盖
    assert captured["items"][0]["text"] == "测试 emoji 😄"
    audits = console.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    entry = [json.loads(line) for line in audits if "media_send" in line][-1]
    assert entry["action"] == "media_send" and entry["ok"] is True
    assert entry["payload"]["contact"] == "文件传输助手"


def test_media_send_501_when_gateway_has_no_api(write_env, monkeypatch):
    monkeypatch.setattr(console, "_gateway_media", lambda: None)

    def missing(payload):
        raise urllib.error.HTTPError("http://127.0.0.1:801/api/media/send", 404,
                                     "Not Found", {}, None)

    monkeypatch.setattr(console, "_post_gateway_media", missing)
    status, data = _call("POST", "/api/media/send",
                         {"type": "text", "text": "hi"}, token="tok")
    assert status == 501, data
    assert data["ok"] is False and data["status"] == "unsupported"
    assert data["error"] == "media_api_not_landed"
    assert "P5-a" in data["hint"]


def test_media_send_validation_and_token(write_env, monkeypatch):
    monkeypatch.setattr(console, "_gateway_media", lambda: None)
    status, _ = _call("POST", "/api/media/send", {"type": "text", "text": "hi"})
    assert status == 401
    status, data = _call("POST", "/api/media/send",
                         {"type": "video", "path": "x.mp4"}, token="tok")
    assert status == 400 and data["status"] == "invalid"
    status, data = _call("POST", "/api/media/send",
                         {"type": "text", "text": "   "}, token="tok")
    assert status == 400 and data["status"] == "invalid"
    status, data = _call("POST", "/api/media/send",
                         {"type": "image", "path": ""}, token="tok")
    assert status == 400 and data["status"] == "invalid"


def test_media_library_acquisition_and_capability_contract(tmp_path, write_env, monkeypatch):
    env = _media_env(tmp_path, monkeypatch, write_env)
    status, data = _call("GET", "/api/media/library?limit=10")
    assert status == 200 and data["ok"] is True and data["total"] == 3
    assert data["source"] == "scan"
    item = data["items"][0]
    assert {"name", "path", "ext", "usage_count", "tags", "mtime", "indexed"} <= set(item)
    assert item["indexed"] is False and item["ext"] in ("png", "jpg", "gif")
    assert item["mtime"] is not None

    status, acq = _call("GET", "/api/media/acquisition")
    assert status == 200 and acq["enabled"] is False
    assert acq["wishes"] == 2 and "预留" in acq["note"]

    caps = console.media_capabilities()
    assert len(caps["capabilities"]) == 9
    for key in ("text", "unicode_emoji", "image", "gif", "sticker_as_image",
                "sticker_native", "sticker_collect", "mixed", "media_enabled"):
        assert key in caps["capabilities"]
        assert isinstance(caps["capabilities"][key]["supported"], bool)
    assert caps["capabilities"]["sticker_collect"]["supported"] is False

    captured = {}

    def fake_post(payload):
        captured.update(payload)
        return {"ok": True, "status": "sent", "contact": payload["contact"]}

    monkeypatch.setattr(console, "_gateway_media", lambda: None)
    monkeypatch.setattr(console, "_post_gateway_media", fake_post)
    status, data = _call("POST", "/api/media/test-send",
                         {"type": "text", "text": "hi", "contact": "真实好友B"},
                         token="tok")
    assert status == 200 and data["contact"] == "文件传输助手"
    assert captured["contact"] == "文件传输助手"

    status, data = _call("POST", "/api/media/test-send",
                         {"type": "native_emoji", "name": "微笑"}, token="tok")
    assert status == 200
    assert captured["items"][0]["type"] == "native_emoji"
    assert captured["items"][0]["name"] == "微笑"


# ============================================================ P7-b 对话测试 / 模型配置 / 日志
class _FakeChatAgent:
    calls = []

    def __init__(self, path):
        self.path = path

    def send(self, text):
        _FakeChatAgent.calls.append({"path": str(self.path), "text": text})
        return {
            "ok": True, "reply": "嗯 在", "bubbles": ["嗯 在"], "crisis": False,
            "guard": {"level": "L0", "issues": []},
            "generation": {"inner": {"mode": "model", "stance": "平静"},
                           "media": {"bubbles": [{"type": "text"}]}},
            "state": {"mood": 0.6},
        }


def _chat_env(tmp_path, monkeypatch):
    selflearn = tmp_path / "selflearn"
    selflearn.mkdir()
    db = selflearn / "console_test.sqlite3"
    monkeypatch.setattr(console, "SELFLEARN_DIR", selflearn)
    monkeypatch.setattr(console, "CHAT_TEST_DB", db)
    _FakeChatAgent.calls = []
    monkeypatch.setattr(console, "_chat_agent_factory", lambda path: _FakeChatAgent(path))
    return db


def test_chat_test_requires_token_and_isolated_db(tmp_path, write_env, monkeypatch):
    db = _chat_env(tmp_path, monkeypatch)
    status, _ = _call("POST", "/api/chat-test", {"text": "在吗"})
    assert status == 401
    status, data = _call("POST", "/api/chat-test", {"text": "在吗"}, token="tok")
    assert status == 200 and data["ok"] is True
    assert data["reply"] == "嗯 在" and data["bubbles"] == ["嗯 在"]
    assert data["inner"]["mode"] == "model"
    assert data["media"]["bubbles"][0]["type"] == "text"
    assert isinstance(data["latency_ms"], float) and data["latency_ms"] >= 0
    assert data["isolation"].startswith("隔离测试库")
    # 只落在隔离库路径，agent 收到的 path 也是隔离库
    assert Path(data["db"]) == db.resolve()
    assert Path(_FakeChatAgent.calls[-1]["path"]) == db.resolve()
    assert _FakeChatAgent.calls[-1]["text"] == "在吗"
    # 除隔离库外没有创建别的 sqlite
    assert [p.name for p in db.parent.glob("*.sqlite3")
            if p.name != "console_test.sqlite3"] == []


def test_chat_test_reset_and_history(tmp_path, write_env, monkeypatch):
    db = _chat_env(tmp_path, monkeypatch)
    db.write_bytes(b"")
    Path(str(db) + "-wal").write_bytes(b"")
    status, data = _call("POST", "/api/chat-test", {"reset": True}, token="tok")
    assert status == 200 and data["reset"] is True
    assert not db.exists() and not Path(str(db) + "-wal").exists()
    assert _FakeChatAgent.calls == []            # reset-only 不生成回复

    # 造历史
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE messages(id TEXT PRIMARY KEY, role TEXT, text TEXT, ts REAL)")
    con.executemany("INSERT INTO messages VALUES(?,?,?,?)",
                    [("M1", "user", "第一句", 100.0), ("M2", "assistant", "第一答", 101.0),
                     ("M3", "user", "第二句", 102.0), ("M4", "assistant", "第二答", 103.0)])
    con.commit()
    con.close()
    history = console.chat_test_history(limit=10)
    assert history["ok"] is True and len(history["items"]) == 4
    assert [item["text"] for item in history["items"]] == ["第一句", "第一答", "第二句", "第二答"]
    status, data = _call("GET", "/api/chat-test/history?limit=2")
    assert status == 200 and len(data["items"]) == 2
    assert data["items"][-1]["text"] == "第二答"


def test_chat_test_timeout(tmp_path, write_env, monkeypatch):
    db = _chat_env(tmp_path, monkeypatch)

    class SlowAgent:
        def __init__(self, path):
            self.path = path

        def send(self, text):
            import time as _time
            _time.sleep(0.2)
            return {"ok": True, "reply": "太慢", "bubbles": ["太慢"], "generation": {}}

    monkeypatch.setattr(console, "_chat_agent_factory", lambda path: SlowAgent(path))
    monkeypatch.setattr(console, "CHAT_TEST_TIMEOUT_S", 0.05)
    status, data = _call("POST", "/api/chat-test", {"text": "hi"}, token="tok")
    assert status == 504
    assert data["ok"] is False and data["status"] == "timeout"
    assert "超时" in data["error"]


def test_model_config_whitelist_and_key_masking(tmp_path, write_env, monkeypatch):
    web_config = tmp_path / "web_config.json"
    web_config.write_text(json.dumps({
        "llm": {"api_key": "sk-SECRET-123", "model": "deepseek-chat",
                "base_url": "https://api.deepseek.com"},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(console, "WEB_CONFIG_PATH", web_config)

    status, data = _call("GET", "/api/model-config")
    assert status == 200 and data["ok"] is True
    assert data["deepseek"]["configured"] is True
    assert "web_config.json" in data["deepseek"]["source"]
    assert "sk-SECRET-123" not in json.dumps(data, ensure_ascii=False)   # 不回传明文

    status, _ = _call("POST", "/api/model-config", {"updates": {"llm_model": "x"}})
    assert status == 401

    updates = {"llm_model": "deepseek-flash", "llm_temperature": 0.3,
               "llm_max_tokens": 1024, "soul_server_url": "http://127.0.0.1:8123/",
               "soul_timeout_s": 3.5, "inner_schema": "full"}
    status, data = _call("POST", "/api/model-config", {"updates": updates}, token="tok")
    assert status == 200 and data["ok"] is True
    status, data = _call("GET", "/api/model-config")
    saved = data["config"]
    assert saved["llm_temperature"] == 0.3
    assert saved["llm_max_tokens"] == 1024
    assert saved["soul_server_url"] == "http://127.0.0.1:8123"
    assert saved["soul_timeout_s"] == 3.5
    assert saved["inner_schema"] == "full"

    status, data = _call("POST", "/api/model-config",
                         {"updates": {"admin_token": "hacked"}}, token="tok")
    assert status == 400 and data["ok"] is False
    assert json.loads(write_env["config"].read_text(encoding="utf-8"))["admin_token"] == "tok"
    status, data = _call("POST", "/api/model-config",
                         {"updates": {"llm_temperature": 9}}, token="tok")
    assert status == 400 and data["ok"] is False
    audits = console.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["action"] == "model_config_update" for line in audits)


def _logs_env(tmp_path, monkeypatch):
    gateway = tmp_path / "gateway.log"
    gateway.write_text("\n".join([
        json.dumps({"ts": 100.0, "kind": "inbound", "text": "你好"}, ensure_ascii=False),
        "broken line",
        json.dumps({"ts": 300.0, "kind": "send_blocked", "reason": "quiet_hours"},
                   ensure_ascii=False),
        json.dumps({"ts": 200.0, "kind": "outbound", "text": "好的"}, ensure_ascii=False),
    ]) + "\n", encoding="utf-8")
    inner = tmp_path / "inner.log"
    inner.write_text(json.dumps({"ts": 400.0, "kind": "inner_fallback",
                                 "reason": "bad_json"}, ensure_ascii=False) + "\n",
                     encoding="utf-8")
    prompt = tmp_path / "prompt_override.log"
    prompt.write_text(json.dumps({"ts": 500.0, "kind": "prompt_override",
                                  "version": 2}, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    monkeypatch.setattr(console, "LOG_SOURCES", {
        "gateway": [gateway], "inner": [inner], "prompt": [prompt]})
    return {"gateway": gateway, "inner": inner, "prompt": prompt}


def test_logs_filters_and_missing_sources(tmp_path, write_env, monkeypatch):
    env = _logs_env(tmp_path, monkeypatch)
    status, data = _call("GET", "/api/logs?source=gateway&limit=2")
    assert status == 200 and data["total"] == 3           # broken 行被跳过
    assert [item["kind"] for item in data["items"]] == ["send_blocked", "outbound"]
    assert data["items"][0]["error"] is True              # 错误行可标红
    assert data["items"][1]["error"] is False

    status, data = _call("GET", "/api/logs?source=gateway&q=quiet")
    assert status == 200 and data["total"] == 1
    assert data["items"][0]["kind"] == "send_blocked"

    status, data = _call("GET", "/api/logs?source=gateway&kind=outbound")
    assert status == 200 and [item["kind"] for item in data["items"]] == ["outbound"]

    status, data = _call("GET", "/api/logs?source=inner")
    assert status == 200 and data["items"][0]["kind"] == "inner_fallback"
    status, data = _call("GET", "/api/logs?source=prompt")
    assert status == 200 and data["items"][0]["kind"] == "prompt_override"

    status, data = _call("GET", "/api/logs?source=nope")
    assert status == 200 and data["items"] == []
    monkeypatch.setattr(console, "LOG_SOURCES", {"gateway": [tmp_path / "missing.log"]})
    status, data = _call("GET", "/api/logs?source=gateway")
    assert status == 200 and data["items"] == [] and data["note"].startswith("日志文件不存在")


def test_logs_regen_source(write_env, monkeypatch):
    monkeypatch.setattr(console, "_regen_entries", lambda: [
        {"ts": None, "kind": "regen_failed", "key": "regen:too_long", "count": 2,
         "error": True, "_source": "regen", "_file": "media_choice", "_line": 0}])
    status, data = _call("GET", "/api/logs?source=regen")
    assert status == 200 and data["total"] == 1
    assert data["items"][0]["kind"] == "regen_failed"


# ============================================================ P7-c 数据管理 / 记忆编辑
def _make_memory_db(path, *, messages=3, memories=2, claims=1):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE messages(id TEXT PRIMARY KEY, role TEXT, text TEXT, ts REAL, "
                "proactive INTEGER DEFAULT 0)")
    con.execute("CREATE TABLE user_memories(id TEXT PRIMARY KEY, scope TEXT, kind TEXT, "
                "content TEXT, evidence TEXT, evidence_history TEXT, confidence REAL, "
                "status TEXT, created_at REAL, importance REAL)")
    con.execute("CREATE TABLE claims(id TEXT PRIMARY KEY, subject TEXT, predicate TEXT, "
                "object TEXT, confidence REAL, status TEXT, created_at REAL)")
    for index in range(messages):
        con.execute("INSERT INTO messages VALUES(?,?,?,?,0)",
                    (f"M{index}", "user" if index % 2 else "assistant",
                     f"消息{index}", 1000.0 + index))
    for index in range(memories):
        con.execute("INSERT INTO user_memories VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"UM{index}", "user", "preference" if index % 2 else "fact",
                     f"记忆内容{index}", f"证据{index}", "[]", 0.8, "active",
                     2000.0 + index, 0.5 + index * 0.1))
    for index in range(claims):
        con.execute("INSERT INTO claims VALUES(?,?,?,?,?,?,?)",
                    (f"C{index}", "用户", "喜欢", f"东西{index}", 0.7, "active", 3000.0))
    con.commit()
    con.close()
    return path


def _data_env(tmp_path, monkeypatch, write_env):
    data_dir = tmp_path / "data"
    selflearn = data_dir / "selflearn"
    contacts = selflearn / "wechat_contacts"
    gateway = data_dir / "wechat"
    log_dir = tmp_path / "log"
    backups = tmp_path / "backups"
    for directory in (contacts, gateway, log_dir, backups):
        directory.mkdir(parents=True, exist_ok=True)
    _make_memory_db(contacts / "alice.sqlite3", messages=3, memories=2, claims=1)
    _make_memory_db(contacts / "bob.sqlite3", messages=1, memories=0, claims=0)
    (gateway / "gateway.log").write_text(
        json.dumps({"ts": 1.0, "kind": "inbound", "text": "hi"}) + "\n", encoding="utf-8")
    (gateway / "drafts.jsonl").write_text("{}\n", encoding="utf-8")
    (data_dir / "prompts.json").write_text(json.dumps({"version": 1, "prompts": {}}),
                                           encoding="utf-8")
    (log_dir / "messages.jsonl").write_text("x\n", encoding="utf-8")
    (log_dir / "聊天记录.txt").write_text("x\n", encoding="utf-8")
    (backups / "manual_seed.zip").write_bytes(b"PK-seed")
    monkeypatch.setattr(console, "DATA_DIR", data_dir)
    monkeypatch.setattr(console, "SELFLEARN_DIR", selflearn)
    monkeypatch.setattr(console, "CONTACTS_DIR", contacts)
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", contacts / "alice.sqlite3")
    monkeypatch.setattr(console, "GATEWAY_LOG", gateway / "gateway.log")
    monkeypatch.setattr(console, "DRAFTS_PATH", gateway / "drafts.jsonl")
    monkeypatch.setattr(console, "PROMPTS_PATH", data_dir / "prompts.json")
    monkeypatch.setattr(console, "LOG_DIR", log_dir)
    monkeypatch.setattr(console, "LOG_MESSAGES", log_dir / "messages.jsonl")
    monkeypatch.setattr(console, "LOG_CHAT", log_dir / "聊天记录.txt")
    monkeypatch.setattr(console, "BACKUP_DIR", backups)
    monkeypatch.setattr(console, "MEDIA_ASSET_DIRS", (tmp_path / "no_media",))
    return {"data": data_dir, "contacts": contacts, "backups": backups,
            "gateway_log": gateway / "gateway.log", "tmp": tmp_path}


def test_data_info_counts_and_backups(tmp_path, write_env, monkeypatch):
    env = _data_env(tmp_path, monkeypatch, write_env)
    status, _ = _call("GET", "/api/data/info")
    assert status == 401
    status, data = _call("GET", "/api/data/info", token="tok")
    assert status == 200 and data["ok"] is True
    assert data["contacts"] == 2
    assert data["messages"] == 4 and data["memories"] == 2 and data["claims"] == 1
    assert data["gateway_log_lines"] == 1
    assert any(row["name"] == "manual_seed.zip" for row in data["backups"])


def test_data_backup_creates_zip_without_changing_data(tmp_path, write_env, monkeypatch):
    env = _data_env(tmp_path, monkeypatch, write_env)
    before = (env["contacts"] / "alice.sqlite3").read_bytes()
    status, data = _call("POST", "/api/data/backup", {}, token="tok")
    assert status == 200 and data["ok"] is True
    backup = Path(data["backup_path"])
    assert backup.exists() and backup.parent == env["backups"].resolve()
    assert backup.name.startswith("manual_")
    with zipfile.ZipFile(backup) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
    assert any(name.endswith("alice.sqlite3") for name in names)
    assert (env["contacts"] / "alice.sqlite3").read_bytes() == before
    assert console.data_info()["messages"] == 4


def test_data_export_jsonl_md_and_memories(tmp_path, write_env, monkeypatch):
    _data_env(tmp_path, monkeypatch, write_env)
    status, body = _call("GET", "/api/data/export?format=jsonl&limit=10", token="tok")
    assert status == 200
    rows = [json.loads(line) for line in body.splitlines() if line.strip()]
    assert len(rows) == 4
    assert {"contact", "ts", "role", "text", "proactive"} <= set(rows[0])
    assert any(row["contact"] == "alice" for row in rows)

    status, body = _call("GET", "/api/data/export?contact=alice&format=jsonl", token="tok")
    assert status == 200
    rows = [json.loads(line) for line in body.splitlines() if line.strip()]
    assert len(rows) == 3 and all(row["contact"] == "alice" for row in rows)

    status, body = _call("GET", "/api/data/export?contact=alice&format=md", token="tok")
    assert status == 200 and "# 聊天记录导出" in body and "**我**" in body or "对方" in body
    status, _ = _call("GET", "/api/data/export?format=jsonl")
    assert status == 401

    status, body = _call("GET", "/api/data/export-memories?contact=alice", token="tok")
    assert status == 200
    memories = body if isinstance(body, list) else json.loads(body)
    assert len(memories) == 2 and memories[0]["contact"] == "alice"
    assert "content" in memories[0] and "importance" in memories[0]


def _make_import_zip(path, *, entry="selflearn/wechat_contacts/new.sqlite3",
                     payload=b"new-db"):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(entry, payload)


def test_data_import_merge_and_guards(tmp_path, write_env, monkeypatch):
    env = _data_env(tmp_path, monkeypatch, write_env)
    zip_path = env["backups"] / "manual_import.zip"
    _make_import_zip(zip_path)
    status, data = _call("POST", "/api/data/import",
                         {"zip_path": "manual_import.zip", "confirm": "导入备份",
                          "mode": "merge"}, token="tok")
    assert status == 200 and data["ok"] is True
    assert data["imported"] == ["new.sqlite3"]
    assert (env["contacts"] / "new.sqlite3").exists()

    status, data = _call("POST", "/api/data/import",
                         {"zip_path": "manual_import.zip", "confirm": "导入备份",
                          "mode": "merge"}, token="tok")
    assert status == 200 and data["skipped"] == ["new.sqlite3"] and data["imported"] == []

    outside = tmp_path / "outside.zip"
    _make_import_zip(outside)
    status, data = _call("POST", "/api/data/import",
                         {"zip_path": str(outside), "confirm": "导入备份",
                          "mode": "merge"}, token="tok")
    assert status == 400 and data["error"] == "unsafe_or_missing_zip"
    status, data = _call("POST", "/api/data/import",
                         {"zip_path": "manual_import.zip", "mode": "merge"}, token="tok")
    assert status == 400 and data["error"] == "confirm_phrase_mismatch"

    unsafe = env["backups"] / "manual_unsafe.zip"
    _make_import_zip(unsafe, entry="../evil.sqlite3")
    status, data = _call("POST", "/api/data/import",
                         {"zip_path": "manual_unsafe.zip", "confirm": "导入备份",
                          "mode": "merge"}, token="tok")
    assert status == 400 and "unsafe_zip_entry" in data["error"]

    status, _ = _call("POST", "/api/data/import",
                      {"zip_path": "manual_import.zip", "confirm": "导入备份",
                       "mode": "bogus"}, token="tok")
    assert status == 400


def test_data_import_overwrite_requires_double_confirm(tmp_path, write_env, monkeypatch):
    env = _data_env(tmp_path, monkeypatch, write_env)
    zip_path = env["backups"] / "manual_overwrite.zip"
    _make_import_zip(zip_path, entry="selflearn/wechat_contacts/alice.sqlite3",
                     payload=b"overwritten-db")
    status, data = _call("POST", "/api/data/import",
                         {"zip_path": "manual_overwrite.zip", "confirm": "导入备份",
                          "mode": "overwrite"}, token="tok")
    assert status == 400 and data["error"] == "overwrite_confirm_required"
    assert (env["contacts"] / "alice.sqlite3").read_bytes().startswith(b"SQLite")

    status, data = _call("POST", "/api/data/import",
                         {"zip_path": "manual_overwrite.zip", "confirm": "导入备份",
                          "mode": "overwrite", "overwrite_confirm": "覆盖现有库"},
                         token="tok")
    assert status == 200 and data["ok"] is True
    assert (env["contacts"] / "alice.sqlite3").read_bytes() == b"overwritten-db"


def test_memory_list_search_status_and_claims(tmp_path, write_env, monkeypatch):
    _data_env(tmp_path, monkeypatch, write_env)
    status, _ = _call("GET", "/api/memory/list")
    assert status == 401
    status, data = _call("GET", "/api/memory/list?limit=1&offset=0", token="tok")
    assert status == 200 and data["total"] == 2 and len(data["items"]) == 1
    status, data = _call("GET", "/api/memory/list?q=记忆内容1", token="tok")
    assert status == 200 and data["total"] == 1
    status, data = _call("GET", "/api/memory/list?status=invalid", token="tok")
    assert status == 200 and data["total"] == 0
    status, data = _call("GET", "/api/memory/list?status=bogus", token="tok")
    assert status == 400

    status, data = _call("GET", "/api/memory/claims", token="tok")
    assert status == 200 and len(data["items"]) == 1
    assert data["items"][0]["subject"] == "用户"
    status, _ = _call("GET", "/api/memory/claims")
    assert status == 401


def test_memory_update_validation_and_audit(tmp_path, write_env, monkeypatch):
    _data_env(tmp_path, monkeypatch, write_env)
    status, data = _call("POST", "/api/memory/update",
                         {"id": "UM0", "content": "改过的内容", "importance": 0.9,
                          "status": "dormant"}, token="tok")
    assert status == 200 and data["ok"] is True
    assert data["item"]["content"] == "改过的内容"
    assert data["item"]["importance"] == 0.9
    assert data["item"]["status"] == "dormant"
    assert "manual_edit" in str(data["item"].get("evidence") or "")
    audits = console.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["action"] == "memory_edit" for line in audits)

    for body in ({"id": "UM0", "content": ""},
                 {"id": "UM0", "content": "x" * 501},
                 {"id": "UM0", "importance": 1.5},
                 {"id": "UM0", "status": "deleted"},
                 {"id": "nope", "content": "x"},
                 {}):
        status, data = _call("POST", "/api/memory/update", body, token="tok")
        assert status == 400 and data["ok"] is False


def test_memory_delete_is_soft_and_mark(tmp_path, write_env, monkeypatch):
    _data_env(tmp_path, monkeypatch, write_env)
    db = console.DEFAULT_CONTACT_DB
    status, data = _call("POST", "/api/memory/delete",
                         {"id": "UM1", "confirm": "错误"}, token="tok")
    assert status == 400
    con = sqlite3.connect(db)
    assert con.execute("SELECT status FROM user_memories WHERE id='UM1'").fetchone()[0] == "active"
    con.close()

    status, data = _call("POST", "/api/memory/delete",
                         {"id": "UM1", "confirm": "删除记忆"}, token="tok")
    assert status == 200 and data["ok"] is True
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM user_memories WHERE id='UM1'").fetchone()[0] == 1
    assert con.execute("SELECT status FROM user_memories WHERE id='UM1'").fetchone()[0] == "invalid"
    con.close()

    status, data = _call("POST", "/api/memory/mark",
                         {"id": "UM0", "important": True}, token="tok")
    assert status == 200 and data["item"]["importance"] == 1.0
    status, data = _call("POST", "/api/memory/mark",
                         {"id": "UM0", "important": False}, token="tok")
    assert status == 200 and data["item"]["importance"] == 0.5


def test_memory_merge_keeps_earlier_and_invalidates_other(tmp_path, write_env, monkeypatch):
    _data_env(tmp_path, monkeypatch, write_env)
    status, data = _call("POST", "/api/memory/merge", {"ids": ["UM1", "UM0"]}, token="tok")
    assert status == 200 and data["ok"] is True
    assert data["kept"] == "UM0" and data["invalidated"] == "UM1"
    con = sqlite3.connect(console.DEFAULT_CONTACT_DB)
    rows = {row[0]: row for row in con.execute(
        "SELECT id, status, evidence FROM user_memories").fetchall()}
    con.close()
    assert rows["UM0"][1] == "active" and rows["UM1"][1] == "invalid"
    assert "证据0" in rows["UM0"][2] and "证据1" in rows["UM0"][2]
    status, data = _call("POST", "/api/memory/merge", {"ids": ["UM0"]}, token="tok")
    assert status == 400
    status, data = _call("POST", "/api/memory/merge", {"ids": ["UM0", "nope"]}, token="tok")
    assert status == 400


def test_data_split_memory_persona_layout(tmp_path, write_env, monkeypatch):
    env = _data_env(tmp_path, monkeypatch, write_env)
    contacts = env["contacts"]
    _make_memory_db(contacts / "abc.memory.sqlite3", messages=2, memories=1, claims=1)
    con = sqlite3.connect(contacts / "abc.persona.sqlite3")
    con.execute("CREATE TABLE persona(id TEXT PRIMARY KEY, name TEXT)")
    con.execute("CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO persona VALUES(?,?)", [("P1", "a"), ("P2", "b"), ("P3", "c")])
    con.execute("INSERT INTO state VALUES('trust','0.8')")
    con.commit()
    con.close()
    (contacts / "abc.legacy.sqlite3").write_bytes(b"legacy-old")
    _make_memory_db(contacts / "def.sqlite3", messages=1, memories=0, claims=0)

    info = console.data_info()
    assert info["contacts"] == 4                       # abc/alice/bob/def
    assert info["memory_dbs"] == 1 and info["persona_dbs"] == 1
    assert info["legacy_dbs"] == 1
    assert info["split_layout"] is True
    assert info["messages"] == 7 and info["memories"] == 3 and info["claims"] == 2
    detail = {row["stem"]: row for row in info["contacts_detail"]}
    abc = detail["abc"]
    assert abc["migrated"] is True
    assert abc["memory"]["messages"] == 2 and abc["memory"]["memories"] == 1
    assert abc["persona"]["rows"] == 4                 # 3 persona + 1 state
    assert abc["legacy"]["name"] == "abc.legacy.sqlite3"
    assert detail["def"]["single"] is True and detail["def"]["persona"] is None

    exported = console.data_export(contact="abc", fmt="jsonl")
    assert exported["ok"] is True and exported["count"] == 2
    listed = console.memory_list(contact="abc")
    assert listed["ok"] is True and listed["total"] == 1

    # 备份 zip 包含拆分后的两种库
    backup = console.data_backup()
    assert backup["ok"] is True
    with zipfile.ZipFile(backup["backup_path"]) as archive:
        names = archive.namelist()
    assert any(name.endswith("abc.memory.sqlite3") for name in names)
    assert any(name.endswith("abc.persona.sqlite3") for name in names)

    # 导入新版拆分 zip（memory/persona 两个文件都复制）
    zip_path = env["backups"] / "manual_split.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("selflearn/wechat_contacts/xyz.memory.sqlite3", b"mem")
        archive.writestr("selflearn/wechat_contacts/xyz.persona.sqlite3", b"per")
    result = console.data_import("manual_split.zip", "导入备份", "merge")
    assert result["ok"] is True
    assert set(result["imported"]) == {"xyz.memory.sqlite3", "xyz.persona.sqlite3"}
    assert (contacts / "xyz.memory.sqlite3").exists()
    assert (contacts / "xyz.persona.sqlite3").exists()


# ============================================================ P8-a 统计
def _stats_env(tmp_path, monkeypatch, write_env):
    # 锚定在「今天零点」而不是 now：避免测试跨越午夜时 days=1 窗口抖动
    anchor = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, 0))
    events = [
        {"ts": anchor + 3600, "kind": "outbound", "friend": "alice", "text": "回复", "status": "sent"},
        {"ts": anchor - 2 * 86400, "kind": "outbound", "friend": "alice", "text": "前天", "status": "sent"},
        {"ts": anchor + 1800, "kind": "proactive_send", "friend": "alice", "text": "在吗",
         "status": "sent", "latency_ms": 100,
         "inner": {"latency_ms": 200},
         "text_media_check": {"regen_failed": ["too_long", "too_long", "emoji"]}},
        {"ts": anchor + 600, "kind": "proactive_send", "friend": "bob", "text": "失败",
         "status": "failed"},
        {"ts": anchor + 500, "kind": "send_blocked", "friend": "alice",
         "reason": "quiet_hours", "text": "被拦文本"},
        {"ts": anchor + 400, "kind": "send_blocked", "friend": "alice", "reason": "stop_file"},
        {"ts": anchor + 300, "kind": "proactive_discarded", "friend": "alice",
         "reason": "banned_phrase", "text": "丢弃文本"},
        {"ts": anchor + 200, "kind": "proactive_skipped", "reason": "probability"},
        {"ts": anchor + 100, "kind": "proactive_schedule", "next_delay_s": 300},
        {"ts": anchor + 90, "kind": "proactive_schedule", "next_delay_s": 500},
        {"ts": anchor + 80, "kind": "send_media", "type": "sticker", "ok": True},
        {"ts": anchor + 70, "kind": "send_media", "type": "sticker", "status": "failed"},
        {"ts": anchor + 60, "kind": "send_media", "type": "image", "status": "sent"},
        {"ts": anchor - 10 * 86400, "kind": "outbound", "friend": "alice", "status": "sent"},
    ]
    lines = [json.dumps(event, ensure_ascii=False) for event in events]
    lines.insert(4, "broken json")
    gateway_log = tmp_path / "gateway.log"
    gateway_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    db = _make_db(tmp_path / "stats.sqlite3")
    monkeypatch.setattr(console, "GATEWAY_LOG", gateway_log)
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", db)
    monkeypatch.setattr(console, "CONTACTS_DIR", tmp_path / "contacts")
    return {"log": gateway_log, "db": db, "tmp": tmp_path}


def test_stats_window_counts_and_rates(tmp_path, write_env, monkeypatch):
    _stats_env(tmp_path, monkeypatch, write_env)
    data = console.stats_snapshot(days=7)
    assert data["ok"] is True
    assert data["window"]["days"] == 7
    assert data["window"]["log_lines_scanned"] == 15
    assert len(data["sent_by_day"]) == 7
    assert sum(row["total"] for row in data["sent_by_day"]) == 3
    assert sum(data["by_hour"]) == 3
    assert data["counts"] == {"sent": 3, "failed": 1, "blocked": 2}
    assert data["success_rate"] == pytest.approx(0.5)
    assert data["blocked"] == {"quiet_hours": 1, "stop_file": 1}
    assert data["discarded"] == {"banned_phrase": 1}
    assert data["proactive"]["skipped"] == {"probability": 1}
    assert data["proactive"]["schedule"] == 2
    assert data["proactive"]["avg_interval_s"] == pytest.approx(400.0)
    assert data["regen_reasons"] == {"too_long": 2, "emoji": 1}
    assert data["media"]["sticker"] == {"sent": 1, "failed": 1}
    assert data["media"]["image"] == {"sent": 1, "failed": 0}
    # 事件延迟 p50/p95（100 与 200 两条）
    assert data["latency"]["p50"] == pytest.approx(150.0)
    assert data["latency"]["p95"] == pytest.approx(195.0)
    assert [row["kind"] for row in data["recent_blocked"]][:2] == [
        "send_blocked", "send_blocked"]
    assert data["recent_blocked"][0]["text"] == "被拦文本"


def test_stats_inner_messages_and_db_errors(tmp_path, write_env, monkeypatch):
    env = _stats_env(tmp_path, monkeypatch, write_env)
    data = console.stats_snapshot(days=7)
    inner = data["inner"]
    assert inner["count"] == 2
    assert inner["by_mode"] == {"model": 2}
    assert inner["by_stance"] == {"平稳": 2}
    assert inner["latency"]["p50"] == pytest.approx(120.5)
    assert inner["latency"]["max"] == pytest.approx(120.5)
    messages = data["messages"]
    assert messages["total"] == 3
    assert messages["mine"] == 2 and messages["theirs"] == 1
    assert messages["active_24h"] == 3

    monkeypatch.setattr(console, "GATEWAY_LOG", tmp_path / "missing.log")
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", tmp_path / "missing.sqlite3")
    empty = console.stats_snapshot(days=7)
    assert empty["ok"] is True
    assert empty["sent_by_day"] and all(row["total"] == 0 for row in empty["sent_by_day"])
    assert empty["inner"]["count"] == 0 and empty["messages"]["total"] == 0
    assert empty["success_rate"] is None
    assert "gateway_log_missing" in empty["errors"]
    assert any(error.startswith("contact_db_not_found") for error in empty["errors"])


def test_stats_days_filter_and_contact_filter(tmp_path, write_env, monkeypatch):
    _stats_env(tmp_path, monkeypatch, write_env)
    one_day = console.stats_snapshot(days=1)
    assert sum(row["total"] for row in one_day["sent_by_day"]) == 2
    assert one_day["counts"]["sent"] == 2

    alice = console.stats_snapshot(days=7, contact="alice")
    assert alice["counts"]["sent"] == 3          # alice：1 条 proactive + 2 条 outbound（含 2 天前，在 7 天窗口内）
    bob = console.stats_snapshot(days=7, contact="bob")
    assert bob["counts"]["sent"] == 0 and bob["counts"]["failed"] == 1
    status, data = _call("GET", "/api/stats?days=7", token="tok")
    assert status == 200 and data["ok"] is True
    status, _ = _call("GET", "/api/stats?days=7")
    assert status == 401


# ============================================================ P9-c 心情卡片
def _mood_env(tmp_path, monkeypatch, write_env):
    contacts = tmp_path / "wechat_contacts"
    contacts.mkdir(parents=True, exist_ok=True)
    db_path = contacts / "alice.memory.sqlite3"
    mood_json = tmp_path / "mood.json"
    mood_json.write_text(json.dumps({
        "judge_enabled": False, "inject": True, "confidence_min": 60,
        "other_key": "keep-me",
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(console, "CONTACTS_DIR", contacts)
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", db_path)
    monkeypatch.setattr(console, "MOOD_SETTINGS_PATH", mood_json)
    monkeypatch.setenv("MOOD_CONFIG_PATH", str(mood_json))
    from agi.selflearn import mood_judge
    now = time.time()
    mood_judge.db.connect(db_path).close()
    for index, delta in enumerate((
            {"moodDelta": 3, "dims": {"happy": 5, "sweet": 2},
             "reason": "你说事情办成了，我跟着高兴起来", "confidence": 80},
            {"moodDelta": -2, "dims": {"worry": 4}, "reason": "你说没睡好，我有点挂心",
             "confidence": 65},
            {"moodDelta": 1, "dims": {"calm": 2}, "reason": "你报了平安，我心里稳下来",
             "confidence": 75},
    )):
        mood_judge.apply(db_path, delta, now=now - 300 + index * 60, mode="user",
                         user_text="测试消息", reply_text="测试回复")
    return {"contacts": contacts, "db": db_path, "mood_json": mood_json,
            "now": now, "mood_judge": mood_judge}


def test_mood_snapshot_timeline_and_limit(tmp_path, write_env, monkeypatch):
    env = _mood_env(tmp_path, monkeypatch, write_env)
    data = console.mood_snapshot(limit=2)
    assert data["ok"] is True
    assert isinstance(data["mood"], (int, float))
    assert set(data["dims"]) == {"happy", "calm", "sweet", "heartache",
                                 "worry", "sad", "jealous"}
    assert len(data["timeline"]) == 2                 # limit 生效
    assert data["settings"]["inject"] is True
    assert data["settings"]["judge_enabled"] is False
    assert data["pending_inject"] is True
    assert data["injected_preview"]                       # inject=true 且有变化
    assert data["db"] == str(env["db"].resolve())

    by_contact = console.mood_snapshot(contact="alice", limit=1)
    assert by_contact["ok"] is True and len(by_contact["timeline"]) == 1

    status, http = _call("GET", "/api/mood?limit=2", token="tok")
    assert status == 200 and http["ok"] is True
    status, _ = _call("GET", "/api/mood?limit=2")
    assert status == 401


def test_mood_unavailable_when_module_or_db_missing(tmp_path, write_env, monkeypatch):
    _mood_env(tmp_path, monkeypatch, write_env)
    monkeypatch.setattr(console, "_mood_store", lambda: None)
    data = console.mood_snapshot()
    assert data["ok"] is False and data["error"] == "mood_unavailable"
    status, http = _call("GET", "/api/mood")
    assert status == 401                               # 无 token 优先 401
    status, http = _call("GET", "/api/mood", token="tok")
    assert status == 200 and http["ok"] is False

    monkeypatch.setattr(console, "_mood_store",
                        lambda: __import__("agi.selflearn.mood_judge",
                                           fromlist=["mood_judge"]))
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", tmp_path / "missing.sqlite3")
    monkeypatch.setattr(console, "CONTACTS_DIR", tmp_path / "empty_contacts")
    data = console.mood_snapshot()
    assert data["ok"] is False and data["error"] == "mood_unavailable"
    assert "找不到联系人库" in data["hint"]


def test_mood_toggles_write_whitelist_and_audit(tmp_path, write_env, monkeypatch):
    env = _mood_env(tmp_path, monkeypatch, write_env)
    mood_json = env["mood_json"]

    status, data = _call("POST", "/api/mood/inject", {"inject": False}, token="tok")
    assert status == 200 and data["ok"] is True
    saved = json.loads(mood_json.read_text(encoding="utf-8"))
    assert saved["inject"] is False
    assert saved["judge_enabled"] is False            # 未动的键保留
    assert saved["other_key"] == "keep-me"            # 其它键保留
    audits = console.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["action"] == "mood_inject_toggle" for line in audits)

    status, data = _call("POST", "/api/mood/enabled", {"enabled": True}, token="tok")
    assert status == 200 and data["ok"] is True
    saved = json.loads(mood_json.read_text(encoding="utf-8"))
    assert saved["judge_enabled"] is True and saved["inject"] is False
    assert saved["other_key"] == "keep-me"

    status, _ = _call("POST", "/api/mood/inject", {"inject": True})
    assert status == 401
    status, data = _call("POST", "/api/mood/inject", {}, token="tok")
    assert status == 400
    status, data = _call("POST", "/api/mood/enabled", {}, token="tok")
    assert status == 400

    illegal = console.mood_update({"mood": 50}, client="test")
    assert illegal["ok"] is False
    assert any("not_allowed:mood" in error for error in illegal["errors"])
    assert json.loads(mood_json.read_text(encoding="utf-8"))["other_key"] == "keep-me"


# ============================================================ P5-e-B 媒体本地导入
def _tiny_png(color=(180, 60, 60)) -> bytes:
    from io import BytesIO
    from PIL import Image
    buffer = BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _import_env(tmp_path, monkeypatch, write_env):
    from agi import media_acquisition as acq
    media = tmp_path / "media"
    desktop = tmp_path / "desktop"
    repo_media = tmp_path / "repo_media"
    for directory in (media, desktop, repo_media):
        directory.mkdir(parents=True, exist_ok=True)
    wishes = tmp_path / "media_wishes.jsonl"
    wishes.write_text(
        json.dumps({"wish": "想发一只布偶猫的表情", "source": "model",
                    "created_at": 1800000000.0}, ensure_ascii=False) + "\n"
        + json.dumps({"wish": "想发「晚安」贴纸"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(acq, "IMPORT_DIR", media)
    monkeypatch.setattr(acq, "MEDIA_SOURCES_PATH", tmp_path / "media_sources.json")
    monkeypatch.setattr(acq, "WISH_STATUS_FILE", tmp_path / "media_wishes_status.json")
    monkeypatch.setattr(acq, "IMPORT_INDEX_DB", tmp_path / "index.sqlite3")
    monkeypatch.setattr(acq, "IMPORT_PATH_ROOTS", (media, desktop, repo_media))
    monkeypatch.setattr(acq, "wishes_path", lambda: wishes)
    monkeypatch.setattr(console, "MEDIA_WISHES_PATH", wishes)
    contacts = tmp_path / "contacts"
    contacts.mkdir()
    contact_db = _make_db(contacts / "alice.sqlite3", message_rows=1, inner_rows=0)
    monkeypatch.setattr(console, "CONTACTS_DIR", contacts)
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", contact_db)
    monkeypatch.setattr(console, "MEDIA_ASSET_DIRS", (media,))
    return {"media": media, "desktop": desktop, "wishes": wishes, "tmp": tmp_path,
            "acq": acq, "contacts": contacts}


def test_console_media_import_upload_duplicate_and_audit(tmp_path, write_env, monkeypatch):
    env = _import_env(tmp_path, monkeypatch, write_env)
    payload = base64.b64encode(_tiny_png()).decode("ascii")
    body = {"files": [{"filename": "cat.png", "data": "data:image/png;base64," + payload,
                       "name": "猫猫", "tags": ["可爱"]}]}
    status, _ = _call("POST", "/api/media/import", body)
    assert status == 401
    status, data = _call("POST", "/api/media/import", body, token="tok")
    assert status == 200 and data["ok"] is True
    assert data["imported"] == 1 and data["results"][0]["status"] == "imported"
    target = Path(data["results"][0]["path"])
    assert target.is_file() and target.parent == env["media"].resolve()
    audits = console.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["action"] == "media_import" for line in audits)
    status, library = _call("GET", "/api/media/library?limit=1000")
    assert status == 200
    imported = [item for item in library["items"] if str(item.get("path")) == str(target)]
    assert imported and imported[0]["name"] == "猫猫"
    assert imported[0]["source"] == "local_import"

    status, data = _call("POST", "/api/media/import", body, token="tok")
    assert status == 200 and data["duplicates"] == 1
    assert len(list(env["media"].glob("*.png"))) == 1

    bad = {"files": [{"filename": "evil.svg", "data": base64.b64encode(b"<svg/>").decode()},
                     {"filename": "page.png", "data": base64.b64encode(b"<html/>").decode()}]}
    status, data = _call("POST", "/api/media/import", bad, token="tok")
    assert status == 200 and data["ok"] is False and data["failed"] == 2
    reasons = {item["filename"]: item.get("reason") for item in data["results"]}
    assert reasons["evil.svg"] == "suffix_not_allowed"
    assert reasons["page.png"] == "not_an_image"


def test_console_media_import_path_whitelist(tmp_path, write_env, monkeypatch):
    env = _import_env(tmp_path, monkeypatch, write_env)
    source = env["desktop"] / "cat.png"
    source.write_bytes(_tiny_png((5, 6, 7)))
    status, data = _call("POST", "/api/media/import-path",
                         {"path": str(source), "name": "桌面猫"}, token="tok")
    assert status == 200 and data["ok"] is True and data["status"] == "imported"

    outside = tmp_path / "outside.png"
    outside.write_bytes(_tiny_png((8, 9, 10)))
    status, data = _call("POST", "/api/media/import-path",
                         {"path": str(outside)}, token="tok")
    assert status == 400 and data["reason"] == "path_not_allowed"
    status, _ = _call("POST", "/api/media/import-path", {"path": str(source)})
    assert status == 401


def test_console_media_wishes_status_and_fulfill(tmp_path, write_env, monkeypatch):
    env = _import_env(tmp_path, monkeypatch, write_env)
    status, data = _call("GET", "/api/media/wishes")
    assert status == 200 and data["count"] == 2
    assert all(item["status"] == "pending" for item in data["items"])
    wish_id = data["items"][0]["wish_id"]
    original = env["wishes"].read_text(encoding="utf-8")

    status, data = _call("POST", "/api/media/wish/fulfill",
                         {"wish_id": wish_id,
                          "data": base64.b64encode(_tiny_png((30, 40, 50))).decode()},
                         token="tok")
    assert status == 200 and data["ok"] is True and data["wish"]["status"] == "fulfilled"
    status, data = _call("GET", "/api/media/wishes")
    items = {item["wish_id"]: item for item in data["items"]}
    assert items[wish_id]["status"] == "fulfilled"
    assert items[wish_id]["fulfilled_asset"]
    assert env["wishes"].read_text(encoding="utf-8") == original

    status, data = _call("POST", "/api/media/wish/fulfill",
                         {"wish_id": wish_id,
                          "data": base64.b64encode(_tiny_png()).decode()}, token="tok")
    assert status == 400 and data["reason"] == "already_fulfilled"
    status, _ = _call("POST", "/api/media/wish/fulfill", {"wish_id": wish_id})
    assert status == 401


# ============================================================ P11 B站页 HTTP
def _bili_http_fake(url, headers=None):
    if "qrcode/generate" in url:
        return {"code": 0, "data": {
            "url": "https://passport.bilibili.com/login?SESSDATA=s1&bili_jct=j1"
                   "&DedeUserID=42&buvid3=b1",
            "qrcode_key": "KEY-HTTP"}}
    if "qrcode/poll" in url:
        code = _bili_http_fake.poll_code
        data = {"code": code}
        if code == 0:
            data["url"] = ("https://passport.bilibili.com/login?SESSDATA=s1"
                           "&bili_jct=j1&DedeUserID=42&buvid3=b1")
        return {"code": 0, "data": data}
    if "web-interface/nav" in url:
        return {"code": 0, "data": {"isLogin": True, "uname": "UP主", "mid": 42}}
    raise AssertionError("unexpected url " + url)


def _bili_console_env(tmp_path, monkeypatch, write_env):
    home = tmp_path / "bili-duty"
    (home / "out").mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({"perDay": 2, "extra": "keep"},
                                                 ensure_ascii=False), encoding="utf-8")
    (home / "out" / (time.strftime("%Y-%m-%d") + ".md")).write_text("# report\n", encoding="utf-8")
    monkeypatch.setenv("BILI_DUTY_HOME", str(home))
    monkeypatch.setattr(console, "BILI_QR_PNG", tmp_path / "qr.png")
    # 隔离（P0-24）：扫码登录会写 `DATA_DIR/selflearn/bili_login_debug.log` 与
    # `bili_profile.json`；不重定向就会落到 worktree 真实 agi/data（守卫会报生产写盘）。
    monkeypatch.setattr(console, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(console, "SELFLEARN_DIR", tmp_path / "data" / "selflearn")
    monkeypatch.setattr(console, "_bili_http_json", _bili_http_fake)
    _bili_http_fake.poll_code = 86101
    console._BILI_NAV_CACHE.update(ts=0.0, data=None)
    return home


def test_console_bili_qr_poll_status_config_http(tmp_path, write_env, monkeypatch):
    home = _bili_console_env(tmp_path, monkeypatch, write_env)
    status, _ = _call("POST", "/api/bili/login/qr", {})
    assert status == 401
    status, data = _call("POST", "/api/bili/login/qr", {}, token="tok")
    assert status == 200 and data["ok"] is True and data["key"] == "KEY-HTTP"
    qr_png = Path(console.BILI_QR_PNG)
    assert qr_png.is_file() and qr_png.read_bytes().startswith(b"\x89PNG")
    # poll waiting → scanned → ok
    status, data = _call("GET", "/api/bili/login/poll?key=KEY-HTTP", token="tok")
    assert status == 200 and data["status"] == "waiting"
    _bili_http_fake.poll_code = 86090
    assert _call("GET", "/api/bili/login/poll?key=KEY-HTTP", token="tok")[1]["status"] == "scanned"
    _bili_http_fake.poll_code = 0
    status, data = _call("GET", "/api/bili/login/poll?key=KEY-HTTP", token="tok")
    assert status == 200 and data["status"] == "ok" and data["uname"] == "UP主"
    assert (home / "bili_credential.json").is_file()

    status, data = _call("GET", "/api/bili/status", token="tok")
    assert status == 200 and data["logged_in"] is True and data["uname"] == "UP主"
    status, _ = _call("GET", "/api/bili/status")
    assert status == 401
    status, data = _call("GET", "/api/bili/report", token="tok")
    assert status == 200 and data["ok"] is True
    status, data = _call("GET", "/api/bili/report?date=2020-01-01", token="tok")
    assert status == 200 and data["error"] == "no_report"


def test_console_bili_config_http_whitelist(tmp_path, write_env, monkeypatch):
    home = _bili_console_env(tmp_path, monkeypatch, write_env)
    status, data = _call("GET", "/api/bili/config", token="tok")
    assert status == 200 and data["ok"] is True
    status, data = _call("POST", "/api/bili/config",
                         {"updates": {"perDay": 9, "night": False}}, token="tok")
    assert status == 200 and data["ok"] is True and data["config"]["perDay"] == 9
    saved = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert saved["extra"] == "keep" and saved["perDay"] == 9
    status, data = _call("POST", "/api/bili/config",
                         {"updates": {"nope": 1}}, token="tok")
    assert status == 400 and data["ok"] is False
    status, _ = _call("POST", "/api/bili/config", {"updates": {"perDay": 1}})
    assert status == 401


# ============================================================ P15-B 推理过程
class _FakeTraceApi:
    calls = []

    @staticmethod
    def handle_trace(path, query):
        _FakeTraceApi.calls.append((path, query))
        if path == "/turns":
            return {"ok": True, "items": [
                {"id": "T1", "ts": 1800000000.0, "mode": "chat",
                 "user_text": "你好", "final_reply": "在", "latency_ms": 120.0,
                 "skipped": False, "kind": "chat"}],
                "limit": query.get("limit")}
        if path == "/turn":
            return {"ok": True, "item": {
                "id": (query.get("id") or [""])[0], "user_text": "你好",
                "final_reply": "在", "generation": {"reasoning": "先接住"},
                "memories": [], "inner": {}, "polish": {}, "guard": {}}}
        if path == "/stats":
            return {"ok": True, "total": 1, "avg_latency_ms": 120.0,
                    "skip_rate": 0.0, "p95_latency_ms": 120.0}
        return {"ok": False, "error": "not_found:" + str(path)}


def test_console_trace_routes_and_static_assets(tmp_path, write_env, monkeypatch):
    _FakeTraceApi.calls = []
    monkeypatch.setattr(console, "_load_trace_api", lambda: _FakeTraceApi)
    status, _ = _call("GET", "/api/trace/turns?limit=5")
    assert status == 401
    status, data = _call("GET", "/api/trace/turns?limit=5", token="tok")
    assert status == 200 and data["ok"] is True
    assert data["items"][0]["id"] == "T1"
    assert _FakeTraceApi.calls[-1][0] == "/turns"
    assert _FakeTraceApi.calls[-1][1]["limit"] == ["5"]

    status, data = _call("GET", "/api/trace/turn?id=T1", token="tok")
    assert status == 200 and data["item"]["id"] == "T1"
    status, data = _call("GET", "/api/trace/stats?days=7", token="tok")
    assert status == 200 and data["total"] == 1

    static_dir = Path(console.STATIC_DIR)
    index = (static_dir / "index.html").read_text(encoding="utf-8")
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    style = (static_dir / "style.css").read_text(encoding="utf-8")
    assert 'data-page="trace"' in index
    assert "renderTrace" in app_js and "traceDetailHtml" in app_js
    assert ".trace-step" in style and ".trace-pre" in style
    assert "推理过程" in app_js


def test_console_trace_backend_unavailable_or_error(write_env, monkeypatch):
    monkeypatch.setattr(console, "_load_trace_api", lambda: None)
    status, data = _call("GET", "/api/trace/turns", token="tok")
    assert status == 501 and data["error"] == "trace_backend_unavailable"
    assert "trace_api.py" in data["hint"]

    class BoomApi:
        @staticmethod
        def handle_trace(path, query):
            raise RuntimeError("db locked")

    monkeypatch.setattr(console, "_load_trace_api", lambda: BoomApi)
    status, data = _call("GET", "/api/trace/turn?id=T1", token="tok")
    assert status == 500 and data["error"] == "trace_api_error"
    assert "db locked" in data["hint"]


# ============================================================ P18-B 引擎启停
def test_console_engine_get_and_unreachable(tmp_path, write_env, monkeypatch):
    original_engine = console._engine_gateway
    monkeypatch.setattr(console, "_engine_gateway",
                        lambda path, method="GET": {"ok": True, "enabled": True,
                                                    "since": 1800000000.0,
                                                    "reason": "startup", "manual": False,
                                                    "available": True})
    status, _ = _call("GET", "/api/engine")
    assert status == 401
    status, data = _call("GET", "/api/engine", token="tok")
    assert status == 200 and data["enabled"] is True and data["available"] is True

    monkeypatch.setattr(console, "_engine_gateway", original_engine)
    monkeypatch.setattr(console, "GATEWAY_STATUS_URL", "http://127.0.0.1:1/status")
    status, data = _call("GET", "/api/engine", token="tok")
    assert status == 200 and data["ok"] is False
    assert data["error"] == "gateway_unreachable" and data["available"] is False


def test_console_engine_toggle_proxy_and_audit(tmp_path, write_env, monkeypatch):
    calls = []

    def fake(path, method="GET"):
        calls.append((path, method))
        enabled = path.endswith("/start")
        return {"ok": True, "enabled": enabled, "since": 1800000000.0,
                "reason": "manual_start" if enabled else "manual_stop",
                "manual": not enabled, "available": True, "changed": True}

    monkeypatch.setattr(console, "_engine_gateway", fake)
    status, data = _call("POST", "/api/engine/stop", {}, token="tok")
    assert status == 200 and data["enabled"] is False and data["changed"] is True
    status, data = _call("POST", "/api/engine/start", {}, token="tok")
    assert status == 200 and data["enabled"] is True
    assert calls == [("/api/engine/stop", "POST"), ("/api/engine/start", "POST")]
    audits = console.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    actions = [json.loads(line) for line in audits if "engine_toggle" in line]
    assert len(actions) == 2 and all(entry["ok"] is True for entry in actions)
    status, _ = _call("POST", "/api/engine/start", {})
    assert status == 401

    monkeypatch.setattr(console, "_engine_gateway",
                        lambda path, method="GET": {"ok": False, "available": True,
                                                    "error": "gateway_http_401"})
    status, data = _call("POST", "/api/engine/stop", {}, token="tok")
    assert status == 200 and data["ok"] is False and data["error"] == "gateway_http_401"

    static_dir = Path(console.STATIC_DIR)
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    assert "renderEngineCard" in app_js and "engine-start" in app_js and "engine-stop" in app_js
    assert "bindEngineActions" in app_js and "toggleEngine" in app_js


# ============================================================ P17 素材库 HTTP
class _FakeLibraryStore:
    calls = []

    @staticmethod
    def list_assets(type_="", tag="", q="", enabled="", limit=100, offset=0):
        _FakeLibraryStore.calls.append(("list", type_, tag, q, enabled, limit, offset))
        return {"ok": True, "items": [{"id": "MA-1", "name": "猫猫", "type": "sticker"}],
                "total": 1}

    @staticmethod
    def get_asset(asset_id):
        return {"ok": asset_id == "MA-1", "asset": {"id": asset_id}}

    @staticmethod
    def stats():
        return {"ok": True, "total": 1, "enabled": 1, "soft_deleted": 0,
                "by_type": {"sticker": 1, "image": 0, "gif": 0}}

    @staticmethod
    def create_asset(**kwargs):
        _FakeLibraryStore.calls.append(("create", kwargs))
        return {"ok": True, "duplicate": False, "asset": {"id": "MA-2"}}

    @staticmethod
    def update_asset(asset_id, updates):
        _FakeLibraryStore.calls.append(("update", asset_id, updates))
        return {"ok": True, "asset": {"id": asset_id}}

    @staticmethod
    def delete_asset(asset_id, hard=False, confirm=""):
        _FakeLibraryStore.calls.append(("delete", asset_id, hard, confirm))
        return {"ok": True, "mode": "hard" if hard else "soft"}

    @staticmethod
    def restore_asset(asset_id):
        return {"ok": True, "asset": {"id": asset_id, "enabled": True}}


def test_console_library_routes_and_static(tmp_path, write_env, monkeypatch):
    _FakeLibraryStore.calls = []
    monkeypatch.setattr(console, "_load_media_store", lambda: _FakeLibraryStore)
    status, _ = _call("GET", "/api/library/list")
    assert status == 401
    status, data = _call("GET", "/api/library/list?type=sticker&q=猫", token="tok")
    assert status == 200 and data["total"] == 1
    assert _FakeLibraryStore.calls[-1] == ("list", "sticker", "", "猫", "", 100, 0)
    status, data = _call("GET", "/api/library/asset?id=MA-1", token="tok")
    assert status == 200 and data["asset"]["id"] == "MA-1"
    status, data = _call("GET", "/api/library/stats", token="tok")
    assert status == 200 and data["by_type"]["sticker"] == 1

    status, data = _call("POST", "/api/library/create",
                         {"data": "aGVsbG8=", "type": "sticker", "name": "新",
                          "caption": "含义", "tags": ["a"]}, token="tok")
    assert status == 200 and data["asset"]["id"] == "MA-2"
    status, data = _call("POST", "/api/library/update",
                         {"id": "MA-1", "name": "改名", "enabled": False}, token="tok")
    assert status == 200
    status, data = _call("POST", "/api/library/delete",
                         {"id": "MA-1", "confirm": "删除素材"}, token="tok")
    assert status == 200 and data["mode"] == "soft"
    status, data = _call("POST", "/api/library/restore", {"id": "MA-1"}, token="tok")
    assert status == 200 and data["asset"]["enabled"] is True
    status, _ = _call("POST", "/api/library/create", {"name": "x"})
    assert status == 401

    audits = console.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    actions = [json.loads(line)["action"] for line in audits]
    for action in ("media_asset_create", "media_asset_update",
                   "media_asset_delete", "media_asset_restore"):
        assert action in actions

    static_dir = Path(console.STATIC_DIR)
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    index = (static_dir / "index.html").read_text(encoding="utf-8")
    style = (static_dir / "style.css").read_text(encoding="utf-8")
    assert 'data-page="library"' in index
    assert "renderLibrary" in app_js and "openLibraryEdit" in app_js
    assert ".library-grid" in style and ".library-card" in style


def test_every_nav_page_has_renderer():
    """每个侧栏页面都必须在 RENDERERS 里注册，否则前端报
    「RENDERERS[page] is not a function」导致整页空白（素材库页踩过）。"""
    import re

    static_dir = Path(console.__file__).parent / "static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    js = (static_dir / "app.js").read_text(encoding="utf-8")
    navs = set(re.findall(r'data-page="([^"]+)"', html))
    match = re.search(r"const RENDERERS = \{(.*?)\};", js, re.S)
    assert match, "app.js 里找不到 RENDERERS 表"
    have = {key for key, _ in re.findall(r"(\w+):\s*(\w+)", match.group(1))}
    missing = sorted(page for page in navs if page not in have)
    assert not missing, f"这些页面没有注册渲染器：{missing}"


# ============================================================ P26 媒体页去素材区块
def test_media_page_no_asset_block_and_library_selector(write_env):
    from pathlib import Path as _Path
    static_dir = _Path(console.STATIC_DIR)
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    style = (static_dir / "style.css").read_text(encoding="utf-8")
    assert "素材库（完整管理请到" not in app_js
    assert "asset-missing" not in app_js
    assert "无预览" not in app_js
    assert "media-filter" not in app_js
    assert "data-asset-path" not in app_js
    assert "/api/library/list?enabled=1&limit=300" in app_js
    assert "btn-media-send" in app_js
    assert "media-import-submit" in app_js
    assert "本页只负责发送与导入" in app_js
    assert "library-missing" in app_js          # 素材库页自身的文件缺失兜底仍保留
    assert ".library-grid" in style


# ============================================================ P35 growth routes
import types  # noqa: E402


def test_growth_routes_401_and_200(write_env, monkeypatch):
    fake = types.SimpleNamespace(
        growth_narrative=lambda: {"ok": True, "text": "叙事", "history": []},
        growth_status=lambda enabled=True: {"ok": True, "enabled": bool(enabled),
                                            "last_week": "2026-W43"})
    monkeypatch.setattr(console, "_load_growth_api", lambda: fake)

    for path in ("/api/growth/narrative", "/api/growth/status"):
        status, data = _call("GET", path)
        assert status == 401 and data["ok"] is False
        status, data = _call("GET", path, token="tok")
        assert status == 200 and data["ok"] is True

    monkeypatch.setattr(console, "_load_growth_api", lambda: None)
    status, data = _call("GET", "/api/growth/narrative", token="tok")
    assert status == 501 and data["ok"] is False


# ============================================================ 遗忘度（task-163）
def _forget_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE user_memories(id TEXT PRIMARY KEY, scope TEXT, kind TEXT, "
                "content TEXT, evidence TEXT, confidence REAL, status TEXT, "
                "created_at REAL, importance REAL, strength REAL, last_strength_at REAL, "
                "forget_stage TEXT, access_count INTEGER)")
    rows = [
        ("UM-F1", "user", "preference", "喜欢猫", "喜欢猫", 0.9, "active", 100.0, 0.5,
         0.8, 90.0, "full", 3),
        ("UM-F2", "user", "fact", "住在杭州", "住杭州", 0.8, "active", 200.0, 0.9,
         0.1, 80.0, "keyword", 1),
    ]
    con.executemany("INSERT INTO user_memories(id,scope,kind,content,evidence,confidence,"
                    "status,created_at,importance,strength,last_strength_at,forget_stage,"
                    "access_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def test_memory_list_exposes_forget_fields(tmp_path, monkeypatch):
    db = _forget_db(tmp_path / "forget.memory.sqlite3")
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", db)
    data = console.memory_list(limit=10)
    assert data["ok"] is True and data["total"] == 2
    by_id = {item["id"]: item for item in data["items"]}
    first = by_id["UM-F1"]
    assert first["strength"] == 0.8
    assert first["forget_degree"] == 0.2 and first["forget_percent"] == 20
    assert first["forget_stage_zh"] == "完整"
    assert first["half_life_days"] and first["half_life_days"] > 0
    assert first["last_strength_at"] == 90.0
    second = by_id["UM-F2"]
    assert second["forget_degree"] == 0.9 and second["forget_percent"] == 90
    assert second["forget_stage_zh"] == "关键词"


def test_memory_list_old_db_without_forget_columns(tmp_path, monkeypatch):
    db = _make_db(tmp_path / "old.memory.sqlite3")
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", db)
    data = console.memory_list(limit=10)
    assert data["ok"] is True and data["total"] == 1
    item = data["items"][0]
    assert item["strength"] == 1.0
    assert item["forget_degree"] == 0.0 and item["forget_percent"] == 0
    assert item["forget_stage_zh"] == "完整"
    assert item["half_life_days"] is not None


# ============================================================ 日记（task-165）
def test_diary_list_and_get(tmp_path, monkeypatch):
    diary = tmp_path / "diary"
    diary.mkdir()
    (diary / "2026-10-28.txt").write_text("第一行预览\n正文第二行。\n", encoding="utf-8")
    (diary / "2026-10-27.txt").write_text("昨天的一篇日记\n", encoding="utf-8")
    (diary / "notes.md").write_text("不是日记", encoding="utf-8")
    monkeypatch.setattr(console, "DIARY_DIR", diary)

    listing = console.diary_list()
    assert listing["ok"] is True and listing["count"] == 2
    assert [item["date"] for item in listing["items"]] == ["2026-10-28", "2026-10-27"]
    assert listing["items"][0]["preview"] == "第一行预览"
    assert listing["items"][0]["chars"] > 0

    detail = console.diary_get("2026-10-28")
    assert detail["ok"] is True
    assert "正文第二行" in detail["text"]
    assert detail["chars"] > 0


def test_diary_date_validation_and_missing(tmp_path, monkeypatch):
    diary = tmp_path / "diary"
    diary.mkdir()
    (diary / "2026-10-28.txt").write_text("日记", encoding="utf-8")
    monkeypatch.setattr(console, "DIARY_DIR", diary)
    for bad in ("../secret", "2026-10-28/../../x", "2026-13-99", "20261028", ""):
        result = console.diary_get(bad)
        assert result["ok"] is False and result["error"] == "invalid_date"
    missing = console.diary_get("2026-10-29")
    assert missing["ok"] is False and missing["error"] == "not_found"


# ============================================================ 上网/B站档案（task-166）
def _web_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE research_sources(id TEXT PRIMARY KEY, ts REAL, query TEXT, "
                "url TEXT, title TEXT, snippet TEXT, turn_id TEXT)")
    con.executemany("INSERT INTO research_sources VALUES(?,?,?,?,?,?,?)", [
        ("RS-1", 100.0, "今天天气", "https://weather.example/a", "天气", "晴", "T-1"),
        ("RS-2", 200.0, "最近新闻", "https://news.example/b", "新闻", "头条", "T-2"),
    ])
    con.commit()
    con.close()
    return path


def test_web_records_order_and_search(tmp_path, monkeypatch):
    contacts = tmp_path / "contacts"
    contacts.mkdir()
    _web_db(contacts / "a.memory.sqlite3")
    monkeypatch.setattr(console, "CONTACTS_DIR", contacts)
    monkeypatch.setattr(console, "SELFLEARN_DIR", tmp_path / "missing")
    data = console.web_records(limit=10)
    assert data["ok"] is True and data["count"] == 2
    assert [item["id"] for item in data["items"]] == ["RS-2", "RS-1"]
    searched = console.web_records(q="天气")
    assert searched["count"] == 1 and searched["items"][0]["title"] == "天气"


def test_bili_impressions_aggregate_and_filter(tmp_path, monkeypatch):
    contacts = tmp_path / "contacts"
    contacts.mkdir()
    db = contacts / "a.memory.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE bili_impressions(bvid TEXT PRIMARY KEY, title TEXT, owner TEXT, "
                "duration_s REAL, cover_url TEXT, summary TEXT, topics_json TEXT, emotion TEXT, "
                "value REAL, why TEXT, my_take TEXT, created_at REAL)")
    con.execute("INSERT INTO bili_impressions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("BV1", "猫猫视频", "UP主", 60, "", "一只猫", '["猫"]', "开心", 0.8,
                 "喜欢猫", "可爱", 100.0))
    con.commit()
    con.close()
    monkeypatch.setattr(console, "CONTACTS_DIR", contacts)
    monkeypatch.setattr(console, "SELFLEARN_DIR", tmp_path / "missing")
    data = console._bili_impressions(10)
    assert data["ok"] is True and data["count"] == 1
    item = data["items"][0]
    assert item["bvid"] == "BV1" and item["topics"] == ["猫"]
    assert console._bili_impressions(q="猫")["count"] == 1
    assert console._bili_impressions(q="不存在")["count"] == 0


def _bili_home(tmp_path: Path) -> Path:
    home = tmp_path / "bili"
    (home / "digest").mkdir(parents=True, exist_ok=True)
    (home / "reports").mkdir(exist_ok=True)
    (home / "digest" / "2026-10.md").write_text("# 十月摘要\n今天看了猫。", encoding="utf-8")
    (home / "reports" / "BV1-2026-10-28.md").write_text("# 视频报告\n内容不错。", encoding="utf-8")
    (home / "duty.log").write_text("line1\nline2\nline3\n", encoding="utf-8")
    return home


def test_bili_digests_list_and_read(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "bili_home", lambda: _bili_home(tmp_path))
    listing = console.bili_digests()
    assert listing["ok"] is True and listing["count"] == 1
    assert listing["items"][0]["name"] == "2026-10.md"
    detail = console.bili_digests("2026-10.md")
    assert detail["ok"] is True and "今天看了猫" in detail["text"]
    assert console.bili_digests("../duty.log")["error"] == "invalid_name"


def test_bili_reports_list_read_and_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "bili_home", lambda: _bili_home(tmp_path))
    listing = console.bili_reports()
    assert listing["count"] == 1
    assert listing["items"][0]["name"] == "BV1-2026-10-28.md"
    assert "内容不错" in console.bili_reports("BV1-2026-10-28.md")["text"]
    assert console.bili_reports("missing.md")["error"] == "not_found"


def test_bili_duty_log_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "bili_home", lambda: _bili_home(tmp_path))
    data = console.bili_duty_log(lines=2)
    assert data["ok"] is True and data["lines"] == ["line2", "line3"]
    assert data["count"] == 2


def test_web_research_contract(tmp_path, monkeypatch):
    contacts = tmp_path / "contacts"
    contacts.mkdir()
    _web_db(contacts / "a.memory.sqlite3")
    monkeypatch.setattr(console, "CONTACTS_DIR", contacts)
    monkeypatch.setattr(console, "SELFLEARN_DIR", tmp_path / "missing")
    data = console.web_research(limit=10)
    assert data["ok"] is True and data["total"] == 2
    item = data["items"][0]
    assert item["domain"] == "news.example"
    assert item["time"] and item["contact"]


def test_bili_feed_filter_contract(tmp_path, monkeypatch):
    contacts = tmp_path / "contacts"
    contacts.mkdir()
    con = sqlite3.connect(contacts / "a.memory.sqlite3")
    con.execute("CREATE TABLE bili_impressions(bvid TEXT PRIMARY KEY, title TEXT, owner TEXT, "
                "duration_s REAL, cover_url TEXT, summary TEXT, topics_json TEXT, emotion TEXT, "
                "value REAL, why TEXT, my_take TEXT, share_line TEXT, created_at REAL, "
                "shared INTEGER DEFAULT 0, shared_at REAL)")
    con.executemany("INSERT INTO bili_impressions(bvid,title,topics_json,created_at,shared) "
                    "VALUES(?,?,?,?,?)", [
        ("BV-A", "已分享视频", '["猫"]', 300.0, 1),
        ("BV-B", "未分享视频", '[]', 200.0, 0),
    ])
    con.commit()
    con.close()
    monkeypatch.setattr(console, "CONTACTS_DIR", contacts)
    monkeypatch.setattr(console, "SELFLEARN_DIR", tmp_path / "missing")
    all_data = console.bili_feed(10, "all")
    assert all_data["total"] == 2 and all_data["items"][0]["shared"] is True
    shared = console.bili_feed(10, "shared")
    assert shared["total"] == 1 and shared["items"][0]["bvid"] == "BV-A"
    skipped = console.bili_feed(10, "skipped")
    assert skipped["total"] == 1 and skipped["items"][0]["bvid"] == "BV-B"


def test_bili_digest_contract(tmp_path, monkeypatch):
    home = _bili_home(tmp_path)
    (home / "digest" / "2026-10.md").write_text(
        "# 摘要\n\n- 2026-10-27 · BV1abc · 一个视频 · UP主 · 挑了：还行 · https://www.bilibili.com/video/BV1abc\n"
        "- 无法解析的一行\n", encoding="utf-8")
    monkeypatch.setattr(console, "bili_home", lambda: home)
    data = console.bili_digest()
    assert data["ok"] is True and data["count"] == 2
    parsed = next(item for item in data["items"] if item["bvid"] == "BV1abc")
    assert parsed["title"] == "一个视频" and parsed["owner"] == "UP主"
    assert parsed["url"].endswith("BV1abc")
    report = console.bili_reports_index()
    assert report["ok"] is True and report["items"][0]["bvid"] == "BV1"

def test_deepseek_status_reads_secrets_store(tmp_path, monkeypatch):
    import json as _json
    secrets = tmp_path / "secrets.json"
    secrets.write_text(_json.dumps({"version": 1, "llm": {"api_key": "sk-secret-xyz"}}),
                       encoding="utf-8")
    monkeypatch.setenv("SOULAGENT_SECRETS", str(secrets))
    from agi import secrets_store
    secrets_store.reset_cache()
    web = tmp_path / "web_config.json"
    web.write_text(_json.dumps({"llm": {"api_key": ""}}), encoding="utf-8")
    monkeypatch.setattr(console, "WEB_CONFIG_PATH", web)
    status = console._deepseek_status()
    assert status["configured"] is True
    assert "secrets.json" in status["source"]
    assert "sk-secret-xyz" not in _json.dumps(status)
    secrets_store.reset_cache()


def test_save_deepseek_key_endpoint(tmp_path, write_env, monkeypatch):
    import json as _json
    secrets = tmp_path / "secrets.json"
    monkeypatch.setenv("SOULAGENT_SECRETS", str(secrets))
    from agi import secrets_store
    secrets_store.reset_cache()
    web = tmp_path / "web_config.json"
    web.write_text(_json.dumps({"llm": {"api_key": "sk-old-plaintext"}}), encoding="utf-8")
    monkeypatch.setattr(console, "WEB_CONFIG_PATH", web)

    status, data = _call("POST", "/api/model-config/deepseek-key",
                         {"api_key": "sk-new-key-123456"}, token="tok")
    assert status == 200 and data["ok"] is True
    assert data["deepseek"]["configured"] is True and "secrets" in data["deepseek"]["source"]
    assert secrets_store.llm_api_key() == "sk-new-key-123456"
    saved_web = _json.loads(web.read_text(encoding="utf-8"))
    assert saved_web["llm"]["api_key"] == ""          # 明文不留在 web_config
    assert "sk-new-key-123456" not in _json.dumps(data)

    status, data = _call("POST", "/api/model-config/deepseek-key",
                         {"clear": True}, token="tok")
    assert status == 200 and data["ok"] is True
    assert secrets_store.llm_api_key() == ""

    status, _data = _call("POST", "/api/model-config/deepseek-key",
                          {"api_key": "sk-new-key-123456"})
    assert status == 401
    secrets_store.reset_cache()

def test_chat_vision_mode_config_roundtrip(tmp_path, write_env, monkeypatch):
    status, data = _call("POST", "/api/model-config",
                         {"updates": {"chat_vision_mode": "api"}}, token="tok")
    assert status == 200 and data["ok"] is True
    assert data["config"]["chat_vision_mode"] == "api"
    status, data = _call("GET", "/api/model-config")
    assert status == 200 and data["config"]["chat_vision_mode"] == "api"

    status, data = _call("POST", "/api/model-config",
                         {"updates": {"chat_vision_mode": "bogus"}}, token="tok")
    assert status == 400 and data["ok"] is False
    assert any("chat_vision_mode" in item for item in data["errors"])

    status, data = _call("POST", "/api/model-config",
                         {"updates": {"chat_vision_mode": "local_caption"}}, token="tok")
    assert status == 200 and data["config"]["chat_vision_mode"] == "local_caption"

def test_contacts_overview_and_messages_by_contact(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "abc.memory.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE messages(id TEXT, role TEXT, text TEXT, ts REAL)")
    con.execute("INSERT INTO messages VALUES('m1','user','你好',100.0)")
    con.execute("CREATE TABLE attachments(id TEXT, message_id TEXT, kind TEXT,"
                " mime TEXT, path TEXT)")
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"png")
    con.execute("INSERT INTO attachments VALUES('ATT-1','m1','image','image/png',?)",
                (str(pic),))
    con.commit(); con.close()

    monkeypatch.setattr(console, "_contact_aliases",
                        lambda: [{"alias": "甲", "db": db.name, "exists": True}])
    monkeypatch.setattr(console, "_contact_paths", lambda alias: {"memory": db})
    monkeypatch.setattr(console, "contact_db_path", lambda: db)
    ov = console.contacts_overview()
    assert ov["ok"] and ov["items"][0]["alias"] == "甲"
    assert ov["items"][0]["messages"] == 1
    assert ov["items"][0]["current"] is True

    monkeypatch.setattr(console, "_contact_db_by_alias", lambda alias: db)
    got = console.read_messages(10, "甲")
    assert got["ok"] and len(got["items"]) == 1
    assert got["items"][0]["text"] == "你好"
    attachment = got["items"][0]["attachment"]
    assert attachment["kind"] == "image" and attachment["url"].startswith("/api/media/file?path=")


def test_memory_contact_missing_returns_hint(monkeypatch):
    monkeypatch.setattr(console, "_contact_db_by_alias", lambda alias: None)
    result = console.memory_snapshot("不存在的联系人")
    assert result["ok"] is False
    assert result["error"] == "contact_db_missing"
    assert "803" in result["hint"]


def test_manual_send_requires_contact_and_text():
    assert console.manual_send("", "hi")["ok"] is False
    assert console.manual_send("某人", "")["ok"] is False
    assert console.manual_send("某人", "x" * 2001)["ok"] is False


# ============================================================ 训练 / 成长 / 内驱力（2c）
def test_http_train_status_and_daily_readonly(tmp_path, monkeypatch):
    """训练页只读契约：/api/train/status 带 daily 日期与门禁，日报缺失返回 404。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import tools.train.train_gate as gate_mod
    monkeypatch.setattr(gate_mod, "check_ready",
                        lambda **kwargs: {"ready": False, "samples": 1,
                                          "reasons": ["samples<300"],
                                          "next_ready": "2026-11-01",
                                          "last_training": None})
    daily = tmp_path / "train" / "daily"
    daily.mkdir(parents=True)
    day = time.strftime("%Y-%m-%d")
    (daily / f"{day}.jsonl").write_text(
        json.dumps({"meta": {"quality": "positive"}}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (daily / f"{day}.md").write_text("# 今日日报\n样本 1 条\n", encoding="utf-8")
    monkeypatch.setattr(console, "TRAIN_ROOT", tmp_path / "train")
    monkeypatch.setattr(console, "TRAIN_DAILY_DIR", daily)
    monkeypatch.setattr(console, "TRAIN_HISTORY_PATH", tmp_path / "train" / "history.jsonl")

    status, data = _call("GET", "/api/train/status")
    assert status == 200 and data["ok"] is True
    assert data["today"]["samples"] == 1 and data["today"]["usable"] == 1
    assert data["daily"]["dates"] == [day]
    assert data["min_samples"] == gate_mod.MIN_SAMPLES
    assert data["gate"]["ready"] is False

    status, detail = _call("GET", "/api/train/daily?date=" + day)
    assert status == 200 and "今日日报" in detail["report"]
    status, detail = _call("GET", "/api/train/daily?date=1999-01-01")
    assert status == 404 and detail["ok"] is False


def test_http_train_status_empty_is_readonly(tmp_path, monkeypatch):
    """没有任何语料/训练记录时也要 200 + 空态字段，不伪造数据。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import tools.train.train_gate as gate_mod
    monkeypatch.setattr(gate_mod, "check_ready",
                        lambda **kwargs: {"ready": False, "samples": 0,
                                          "reasons": ["samples<300"],
                                          "next_ready": "", "last_training": None})
    monkeypatch.setattr(console, "TRAIN_DAILY_DIR", tmp_path / "no-daily")
    monkeypatch.setattr(console, "TRAIN_HISTORY_PATH", tmp_path / "no-history.jsonl")
    status, data = _call("GET", "/api/train/status")
    assert status == 200 and data["ok"] is True
    assert data["today"]["samples"] == 0
    assert data["cumulative"]["samples"] == 0
    assert data["daily"]["dates"] == []
    assert data["last_training"] is None


def test_http_initiative_status_proxies_gateway(monkeypatch):
    """802 只做代理：/api/initiative/status 透传 contact 并原样回 801 字段。"""
    seen = {}

    def fake_proxy(path, payload=None, *, method="GET", timeout=30.0):
        seen["path"] = path
        return {"ok": True, "contact": "甲", "drive": 0.62,
                "drive_level": "warm", "quadrant": "warm"}

    monkeypatch.setattr(console, "_gateway_proxy", fake_proxy)
    status, data = _call("GET", "/api/initiative/status?contact="
                         + urllib.parse.quote("甲"))
    assert status == 200 and data["ok"] is True
    assert "contact=" in seen["path"] and "%E7%94%B2" in seen["path"]
    assert data["drive_level"] == "warm"


def test_http_media_acquisition_lists_wishes_readonly(tmp_path, write_env, monkeypatch):
    """素材获取只读契约：列出愿望 + 明确 enabled=False/下载未实现（不假装）。"""
    _media_env(tmp_path, monkeypatch, write_env)
    status, acq = _call("GET", "/api/media/acquisition")
    assert status == 200 and acq["enabled"] is False
    assert acq["mode"] == "read_only"
    assert acq["wishes"] == 2
    assert [item["wish"] for item in acq["items"]] == [
        "想发一只布偶猫的表情", "想发「晚安」贴纸"]
    caps = acq["capabilities"]
    assert caps["download"] is False and caps["search_online"] is False
    assert caps["manual_import"] is True
    assert "下载" in acq["note"] or "下载管线" in acq["note"]
    assert acq["library"]["path"].endswith("stickers")


def test_http_bili_status_reports_missing_plugin(tmp_path, write_env, monkeypatch):
    """插件缺失时 /api/bili/status 回 bili_home_missing（前端据此显示安装空态）。"""
    monkeypatch.setenv("BILI_DUTY_HOME", str(tmp_path / "no-bili-duty"))
    status, data = _call("GET", "/api/bili/status", token="tok")
    assert status == 200 and data["ok"] is False
    assert data["error"] == "bili_home_missing"
    assert "插件家目录不存在" in data["hint"]


# ============================================================ C4/C5/C7（802 成长页 / 心情口径）
def test_http_growth_weak_due_unavailable_is_not_501(tmp_path, write_env, monkeypatch):
    """weak_validation 模块不可用：401→200(ok:false)，错误码 weak_due_unavailable，绝不是 501。"""
    monkeypatch.setattr(console, "_weak_validation_module", lambda: None)
    status, data = _call("GET", "/api/growth/weak-due")
    assert status == 401
    status, data = _call("GET", "/api/growth/weak-due", token="tok")
    assert status == 200, data
    assert data["ok"] is False and data["error"] == "weak_due_unavailable"
    assert data["count"] == 0 and data["items"] == []
    assert data.get("hint")


def test_http_growth_weak_due_ok_normalizes_items(tmp_path, write_env, monkeypatch):
    """正常路径：due_rechecks 的 items 归一成 {skill_id,next_at,reason,variant,level,overdue_s}。"""
    db = tmp_path / "contact.sqlite3"
    db.write_bytes(b"")
    monkeypatch.setattr(console, "contact_db_path", lambda: db)
    fake = types.SimpleNamespace(due_rechecks=lambda path: {"items": [
        {"skill_id": "SK-1", "next_at": 1800000000.0, "reason": "连续失败",
         "variant": "v2", "level": 2, "overdue_s": 120.5},
    ]})
    monkeypatch.setattr(console, "_weak_validation_module", lambda: fake)
    status, data = _call("GET", "/api/growth/weak-due", token="tok")
    assert status == 200 and data["ok"] is True
    assert data["count"] == 1
    row = data["items"][0]
    assert row["skill_id"] == "SK-1" and row["level"] == 2
    assert row["overdue_s"] == 120.5 and row["variant"] == "v2"


def test_http_growth_weak_due_missing_db(tmp_path, write_env, monkeypatch):
    """库不可用：同样 ok:false + weak_due_unavailable，不 501、不抛异常。"""
    class _NeverCalled:
        @staticmethod
        def due_rechecks(path):
            raise AssertionError("库不存在时不应调用 due_rechecks")

    monkeypatch.setattr(console, "_weak_validation_module", lambda: _NeverCalled)
    monkeypatch.setattr(console, "contact_db_path", lambda: None)
    status, data = _call("GET", "/api/growth/weak-due", token="tok")
    assert status == 200 and data["ok"] is False
    assert data["error"] == "weak_due_unavailable"


def test_http_wake_pending_empty(tmp_path, write_env, monkeypatch):
    """无候选：ok:true,count:0（页面显示空态，不是错误）。"""
    db = tmp_path / "contact.sqlite3"
    db.write_bytes(b"")
    monkeypatch.setattr(console, "contact_db_path", lambda: db)
    fake = types.SimpleNamespace(list_candidates=lambda path, limit=20: [])
    monkeypatch.setattr(console, "_share_store_module", lambda: fake)
    status, data = _call("GET", "/api/wake/pending", token="tok")
    assert status == 200 and data["ok"] is True
    assert data["count"] == 0 and data["items"] == []


def test_http_wake_pending_filters_wake_kind(tmp_path, write_env, monkeypatch):
    """只保留 kind=="wake" 的候选；其它 kind 不混进「内驱力到期候选」。"""
    db = tmp_path / "contact.sqlite3"
    db.write_bytes(b"")
    rows = [
        {"id": "C-1", "kind": "wake", "text": "想问你周末有空吗", "level": "L1",
         "score": 0.7, "created_at": 1800000000.0},
        {"id": "C-2", "kind": "share", "text": "不该出现", "level": "L1",
         "score": 0.3, "created_at": 1800000001.0},
    ]
    monkeypatch.setattr(console, "contact_db_path", lambda: db)
    fake = types.SimpleNamespace(list_candidates=lambda path, limit=20: rows)
    monkeypatch.setattr(console, "_share_store_module", lambda: fake)
    status, data = _call("GET", "/api/wake/pending?limit=10", token="tok")
    assert status == 200 and data["ok"] is True
    assert data["count"] == 1
    assert data["items"][0]["kind"] == "wake"
    assert data["items"][0]["text"] == "想问你周末有空吗"


def test_http_mood_adds_source_and_affect_note(tmp_path, write_env, monkeypatch):
    """C7：/api/mood 明确 source=mood_judge，并带 affect(0–1) 口径说明。"""
    db = tmp_path / "contact.sqlite3"
    db.write_bytes(b"")
    monkeypatch.setattr(console, "contact_db_path", lambda: db)
    fake = types.SimpleNamespace(
        snapshot=lambda path, timeline_limit=20: {
            "mood": 72, "mood_label": "平静", "dims": {}, "settings": {},
            "timeline": [], "last_reason": "", "pending_inject": False})
    monkeypatch.setattr(console, "_mood_store", lambda: fake)
    status, data = _call("GET", "/api/mood", token="tok")
    assert status == 200 and data["ok"] is True
    assert data["source"] == "mood_judge"
    assert "affect" in data["affect_note"] and "0–1" in data["affect_note"]
    assert data["mood"] == 72
