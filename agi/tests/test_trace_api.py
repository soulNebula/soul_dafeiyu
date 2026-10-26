# -*- coding: utf-8 -*-
"""P15-A：控制台 trace_api 只读接口测试（tmp_path，不联网、不读生产配置）。

按 w3-feedback 的接线契约测：`handle_trace(suffix, query)`，
query 为 parse_qs 的 dict（值是 list）。同时覆盖 items/turns、turn/item、
stats 扁平字段、token 401、坏 JSON 行降级、limit、过滤、未知路由。
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

from agi.selflearn import db, trace

TRACE_API_PATH = Path(__file__).resolve().parents[2] / "tools" / "console" / "trace_api.py"


def _load_api():
    spec = importlib.util.spec_from_file_location("console_trace_api_test",
                                                  TRACE_API_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _isolate_trace_api(monkeypatch, tmp_path):
    cfg = tmp_path / "trace_cfg.json"
    cfg.write_text(json.dumps({"trace_enabled": True, "trace_max_rows": 5000}),
                   encoding="utf-8")
    monkeypatch.setenv("TRACE_CONFIG_PATH", str(cfg))
    monkeypatch.delenv("TRACE_ENABLED", raising=False)
    monkeypatch.delenv("TRACE_MAX_ROWS", raising=False)
    # trace_api 默认库路径固定到 tmp，避免扫描真实联系人库
    monkeypatch.setenv("TRACE_DB_PATH", str(tmp_path / "api.sqlite3"))
    yield


@pytest.fixture
def api():
    return _load_api()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "api.sqlite3"
    db.connect(path).close()
    base = time.time()
    trace.record(path, {"id": "T-1", "ts": base - 3, "mode": "chat",
                        "contact": "A", "user_text": "今天好累",
                        "final_reply": "先歇会儿", "latency_ms": 120.0,
                        "generation_json": {"reasoning": "内心推理",
                                            "raw_text": "原始", "two_stage": True}})
    trace.record(path, {"id": "T-2", "ts": base - 2, "mode": "proactive",
                        "contact": "B", "final_reply": "想你了",
                        "latency_ms": 250.0,
                        "proactive_json": {"kind": "share", "available": True}})
    trace.record(path, {"id": "T-3", "ts": base - 1, "mode": "chat",
                        "contact": "A", "skipped": 1,
                        "skip_reason": "心情不好", "latency_ms": 80.0})
    return path


# --------------------------------------------------------------------------- #
# 1. 列表 / 详情 / 流 / 统计
# --------------------------------------------------------------------------- #
def test_turns_list_contract_for_frontend(api, db_path):
    result = api.handle_trace("/turns", {"limit": ["50"], "db": [str(db_path)]})
    assert result["ok"] is True
    assert result["count"] == 3
    # w3-feedback 前端契约：items 数组 + turns 兼容
    assert isinstance(result["items"], list) and result["items"] == result["turns"]
    item = result["items"][0]
    for key in ("id", "ts", "mode", "contact", "user_text", "final_reply",
                "latency_ms", "skipped", "kind"):
        assert key in item, key
    assert item["id"] == "T-3"          # ts 最新在前


def test_turns_limit_and_mode_filter(api, db_path):
    limited = api.handle_trace("/turns", {"limit": ["1"], "db": [str(db_path)]})
    assert limited["count"] == 1
    chat = api.handle_trace("/turns", {"mode": ["chat"], "db": [str(db_path)]})
    assert {item["id"] for item in chat["items"]} == {"T-1", "T-3"}
    proactive = api.handle_trace("/turns", {"mode": ["proactive"],
                                            "db": [str(db_path)]})
    assert {item["id"] for item in proactive["items"]} == {"T-2"}


def test_turn_detail_contract(api, db_path):
    result = api.handle_trace("/turn", {"id": ["T-1"], "db": [str(db_path)]})
    assert result["ok"] is True
    turn = result["item"]
    assert turn == result["turn"]
    for key in ("user_text", "system_prompt", "prompt_user", "memories", "inner",
                "mood", "generation", "polish", "guard", "final_reply",
                "latency_ms", "skipped", "skip_reason", "crisis", "ts", "mode"):
        assert key in turn, key
    assert turn["generation"]["reasoning"] == "内心推理"
    assert turn["generation"]["raw_text"] == "原始"
    # 字符串化 JSON 也保留（前端容错）
    assert turn["raw"]["generation_json"]


def test_turn_missing_id_and_not_found(api, db_path):
    missing = api.handle_trace("/turn", {"db": [str(db_path)]})
    assert missing["ok"] is False and missing["error"] == "missing_id"
    absent = api.handle_trace("/turn", {"id": ["nope"], "db": [str(db_path)]})
    assert absent["ok"] is False and absent["error"].startswith("not_found")


def test_stream_contract(api, db_path):
    result = api.handle_trace("/stream", {"limit": ["2"], "db": [str(db_path)]})
    assert result["ok"] is True
    assert len(result["events"]) == 2
    assert result["items"] == result["events"]


def test_stats_contract_flattened_for_frontend(api, db_path):
    result = api.handle_trace("/stats", {"days": ["7"], "db": [str(db_path)]})
    assert result["ok"] is True
    assert result["total"] == 3
    assert result["avg_latency_ms"] == pytest.approx(150.0, abs=0.1)  # (120+250+80)/3
    assert 0 < result["skip_rate"] < 1
    assert result["p95_latency_ms"] > 0 and result["p95"] == result["p95_latency_ms"]
    assert "stats" in result and result["stats"]["count"] == 3


# --------------------------------------------------------------------------- #
# 2. 路由 / 兜底 / token
# --------------------------------------------------------------------------- #
def test_full_path_and_unknown_route(api, db_path):
    full = api.handle_trace("/api/trace/turns", {"db": [str(db_path)]})
    assert full["ok"] is True and full["count"] == 3
    unknown = api.handle_trace("/bogus", {"db": [str(db_path)]})
    assert unknown["ok"] is False
    assert unknown["error"].startswith("not_found:")
    assert "/turns" in unknown["routes"]


def test_optional_admin_token(api, db_path):
    denied = api.handle_trace("/turns", {}, admin_token="secret")
    assert denied["ok"] is False and denied.get("status") == 401
    wrong = api.handle_trace("/turns", {"token": ["bad"]}, admin_token="secret")
    assert wrong.get("status") == 401
    ok = api.handle_trace("/turns", {"token": ["secret"], "db": [str(db_path)]},
                          admin_token="secret")
    assert ok["ok"] is True


def test_bad_json_rows_degrade_gracefully(api, db_path):
    con = db.memory_connect(db_path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO turn_traces(id,ts,mode,generation_json,"
            "proactive_json,created_at) VALUES(?,?,?,?,?,?)",
            ("T-bad", __import__("time").time() - 0.5, "chat", "not-json", "{oops", __import__("time").time()))
        con.commit()
    finally:
        con.close()
    turns = api.handle_trace("/turns", {"db": [str(db_path)]})
    assert turns["ok"] is True and turns["count"] == 4
    detail = api.handle_trace("/turn", {"id": ["T-bad"], "db": [str(db_path)]})
    assert detail["ok"] is True
    assert detail["item"]["generation"] == {}      # 坏 JSON → 空结构，不炸
    stats = api.handle_trace("/stats", {"db": [str(db_path)]})
    assert stats["ok"] is True and stats["total"] == 4


def test_contact_filter_requires_explicit_db(api, db_path):
    filtered = api.handle_trace("/turns", {"contact": ["A"],
                                          "db": [str(db_path)]})
    assert {item["id"] for item in filtered["items"]} == {"T-1", "T-3"}


def test_resolve_route_helpers(api):
    assert api.resolve_route("/api/trace/turn") == "/turn"
    assert api.resolve_route("turns") == "/turns"
    assert api.resolve_route("/") == "/turns"
    assert api.trace_routes() == ["/api/trace/turns", "/api/trace/turn",
                                  "/api/trace/stream", "/api/trace/stats"]
