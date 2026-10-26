# -*- coding: utf-8 -*-
"""P15-A：推理过程采集（turn_traces + 开关/截断/裁剪 + proactive 挂点 + reasoning）。

全部 tmp_path；不联网；mock 后端。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agi.selflearn import db, proactive, trace
from agi.selflearn.llm import Client
from agi.selflearn.trace import (PROMPT_FIELD_LIMIT, RECORD_LIMIT, get_turn,
                                 list_turns, normalize_payload, record, stats)


@pytest.fixture(autouse=True)
def _isolate_trace(monkeypatch, tmp_path):
    """默认开关 on，但 trace 配置用 tmp（不读生产 wechat config）；env 清理。"""
    cfg_path = tmp_path / "trace_cfg.json"
    cfg_path.write_text(json.dumps({"trace_enabled": True, "trace_max_rows": 5000}),
                        encoding="utf-8")
    monkeypatch.setenv("TRACE_CONFIG_PATH", str(cfg_path))
    for name in ("TRACE_ENABLED", "TRACE_MAX_ROWS", "TRACE_DB_PATH"):
        monkeypatch.delenv(name, raising=False)
    yield


def _path(tmp_path, name="trace"):
    path = tmp_path / f"{name}.sqlite3"
    db.connect(path).close()
    return path


def _count(path) -> int:
    con = db.memory_connect(path)
    try:
        return int(con.execute("SELECT count(*) FROM turn_traces").fetchone()[0])
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# 1. 落库 / 字段 / 解析
# --------------------------------------------------------------------------- #
def test_turn_traces_table_lives_in_memory_db(tmp_path):
    path = _path(tmp_path)
    memory = db.memory_connect(path)
    try:
        tables = {row[0] for row in memory.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        cols = {row[1] for row in memory.execute(
            "PRAGMA table_info(turn_traces)").fetchall()}
    finally:
        memory.close()
    assert "turn_traces" in tables
    assert {"id", "ts", "contact", "user_text", "mode", "system_prompt",
            "prompt_user", "memories_json", "inner_json", "mood_json",
            "generation_json", "polish_json", "guard_json", "final_reply",
            "skipped", "skip_reason", "crisis", "latency_ms",
            "proactive_json", "created_at"} <= cols


def test_record_then_get_turn_roundtrip(tmp_path):
    path = _path(tmp_path)
    trace_id = record(path, {
        "id": "T-1", "contact": "好友A", "user_text": "今天好累",
        "mode": "chat", "system_prompt": "你是 AI", "prompt_user": "用户：今天好累",
        "memories_json": [{"id": "UM-1", "kind": "preference", "content": "喜欢晚上写代码"}],
        "inner_json": {"mode": "rule", "source": "rule", "injected": True},
        "mood_json": {"delta": {"moodDelta": -2}, "reason": "用户说累"},
        "generation_json": {"raw_text": "原始输出", "reasoning": "内心推理",
                            "two_stage": True, "usage": {"tokens_in": 10}},
        "polish_json": {"before": "然而累。", "after": "但是累"},
        "guard_json": {"level": "L0", "issues": []},
        "final_reply": "先歇会儿", "latency_ms": 321.5,
    })
    assert trace_id == "T-1"
    item = get_turn(path, "T-1")
    assert item is not None
    assert item["user_text"] == "今天好累"
    assert item["system_prompt"] == "你是 AI"
    assert item["final_reply"] == "先歇会儿"
    assert item["latency_ms"] == 321.5
    # JSON 字段被解析成对象；memories 兼容 {"items": [...]} → 数组
    assert isinstance(item["inner"], dict) and item["inner"]["mode"] == "rule"
    assert isinstance(item["generation"], dict)
    assert item["generation"]["reasoning"] == "内心推理"
    assert isinstance(item["polish"], dict)
    assert isinstance(item["guard"], dict)
    assert item["raw"]["generation_json"]


def test_memories_from_items_dict_is_flattened(tmp_path):
    path = _path(tmp_path)
    record(path, {"id": "T-mem", "memories_json": {"items": [{"id": "UM-1"}]}})
    item = get_turn(path, "T-mem")
    assert item["memories"] == [{"id": "UM-1"}]


# --------------------------------------------------------------------------- #
# 2. 截断 / 总大小 / 裁剪
# --------------------------------------------------------------------------- #
def test_prompt_fields_truncated_with_head_and_tail(tmp_path):
    path = _path(tmp_path)
    head = "头" * 5000
    middle = "中" * 30000
    tail = "尾" * 5000
    record(path, {"id": "T-big", "system_prompt": head + middle + tail,
                  "prompt_user": "P" * 50000})
    item = get_turn(path, "T-big")
    assert len(item["system_prompt"].encode("utf-8")) <= PROMPT_FIELD_LIMIT
    assert item["system_prompt"].startswith("头")
    assert "trace 已截断" in item["system_prompt"]
    assert len(item["prompt_user"].encode("utf-8")) <= PROMPT_FIELD_LIMIT


def test_record_total_size_limit(tmp_path):
    path = _path(tmp_path)
    record(path, {
        "id": "T-total",
        "system_prompt": "A" * 19000,
        "prompt_user": "B" * 19000,
        "generation_json": {"raw_text": "R" * 19000, "reasoning": "Q" * 19000},
        "guard_json": {"violations": ["V" * 19000]},
        "memories_json": [{"content": "M" * 19000}],
        "final_reply": "F" * 19000,
    })
    con = db.memory_connect(path)
    try:
        row = con.execute("SELECT * FROM turn_traces WHERE id='T-total'").fetchone()
    finally:
        con.close()
    total = sum(len(str(value or "").encode("utf-8")) for value in dict(row).values())
    assert total <= RECORD_LIMIT, total


def test_trace_max_rows_prunes_oldest(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACE_MAX_ROWS", "3")
    path = _path(tmp_path)
    for index in range(5):
        record(path, {"id": f"T-{index}", "ts": 1000.0 + index,
                      "final_reply": f"回复{index}"})
    con = db.memory_connect(path)
    try:
        ids = [row[0] for row in con.execute(
            "SELECT id FROM turn_traces ORDER BY ts").fetchall()]
    finally:
        con.close()
    assert len(ids) == 3
    assert ids == ["T-2", "T-3", "T-4"]


# --------------------------------------------------------------------------- #
# 3. 开关 / 失败不影响主链
# --------------------------------------------------------------------------- #
def test_disabled_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACE_ENABLED", "0")
    path = _path(tmp_path)
    assert record(path, {"id": "T-off", "final_reply": "x"}) is None
    assert _count(path) == 0
    assert trace.enabled() is False


def test_record_failure_is_silent(monkeypatch, tmp_path):
    path = _path(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(trace.db, "connect", boom)
    assert record(path, {"id": "T-boom", "final_reply": "x"}) is None


def test_settings_reads_wechat_config_and_env(monkeypatch, tmp_path):
    monkeypatch.setattr(trace, "_gateway_config",
                        lambda: {"trace_enabled": False, "trace_max_rows": 12})
    assert trace.settings() == {"enabled": False, "max_rows": 12}
    monkeypatch.setenv("TRACE_ENABLED", "1")
    monkeypatch.setenv("TRACE_MAX_ROWS", "7")
    assert trace.settings() == {"enabled": True, "max_rows": 7}


# --------------------------------------------------------------------------- #
# 4. proactive tick 挂点
# --------------------------------------------------------------------------- #
class _SayClient:
    def __init__(self, text="今天想起你说的事"):
        self.text = text

    def available(self):
        return True

    def chat(self, *args, **kwargs):
        return {"ok": True, "text": self.text}


def _seed_proactive(path, *, ts_age=120, mood=0.8, sharing=0.8):
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-1", "user", "最近在忙项目", time.time() - ts_age, 0))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('self_state',?,?)",
                (json.dumps({"self": {"mood": mood, "sharing": sharing}}), time.time()))
    con.commit()
    con.close()


def test_proactive_tick_records_mode_proactive(tmp_path):
    path = _path(tmp_path, "proactive")
    _seed_proactive(path)
    result = proactive.tick(path, client=_SayClient("今天想起你说的事"),
                            respect_state=False, cooldown=0, min_score=0)
    assert result["available"] is True
    rows = list_turns(path, mode="proactive")
    assert rows, "proactive tick 应该记一条 mode=proactive 的 trace"
    assert rows[0]["mode"] == "proactive"
    item = get_turn(path, rows[0]["id"])
    assert item["proactive"]["available"] is True
    assert item["proactive"]["kind"] or item["proactive"]["messages"]


def test_silent_proactive_tick_does_not_record(tmp_path):
    path = _path(tmp_path, "proactive_silent")
    _seed_proactive(path, ts_age=0)   # 「刚聊过」→ 静默轮询
    result = proactive.tick(path, client=_SayClient(), respect_state=False,
                            cooldown=900, min_score=0)
    assert result.get("available") is not True
    assert list_turns(path, mode="proactive") == []


# --------------------------------------------------------------------------- #
# 5. llm.Client reasoning_content
# --------------------------------------------------------------------------- #
def test_llm_client_returns_reasoning_content():
    def transport(messages, body):
        return {"choices": [{"message": {
            "content": '{"ok": true}',
            "reasoning_content": "我先想了一下：用户只是问好。"},
            "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    client = Client(config={"api_key": "x"}, transport=transport, timeout=5)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result["ok"] is True
    assert result["reasoning"].startswith("我先想了一下")
    assert result["text"] == '{"ok": true}'


def test_llm_client_reasoning_empty_when_absent():
    def transport(messages, body):
        return {"choices": [{"message": {"content": "普通回复"}}]}

    client = Client(config={"api_key": "x"}, transport=transport, timeout=5)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result["reasoning"] == ""
    assert result["text"] == "普通回复"


# --------------------------------------------------------------------------- #
# 6. 列表 / 统计辅助
# --------------------------------------------------------------------------- #
def test_list_turns_filters_and_truncates(tmp_path):
    path = _path(tmp_path)
    record(path, {"id": "T-a", "ts": 1000.0, "mode": "chat",
                  "user_text": "长文本" * 200, "final_reply": "回复" * 200})
    record(path, {"id": "T-b", "ts": 1001.0, "mode": "proactive",
                  "proactive_json": {"kind": "share"}})
    assert [item["id"] for item in list_turns(path)] == ["T-b", "T-a"]
    assert [item["id"] for item in list_turns(path, mode="chat")] == ["T-a"]
    chat_item = list_turns(path, mode="chat")[0]
    assert len(chat_item["user_text"]) <= 160


def test_stats_basic_numbers(tmp_path):
    path = _path(tmp_path)
    for index, latency in enumerate((100.0, 200.0, 300.0, None)):
        record(path, {"id": f"T-{index}", "ts": time.time() - index,
                      "latency_ms": latency,
                      "skipped": index == 3,
                      "generation_json": {"two_stage": index == 0}})
    result = stats(path, days=7)
    assert result["count"] == 4
    assert result["avg_latency_ms"] == 200.0
    assert result["p95_latency_ms"] >= 200.0
    assert result["skip_rate"] == 0.25
    assert result["two_stage_rate"] == 0.25


# --------------------------------------------------------------------------- #
# 7. chat.send 挂点（P15-A Phase 2）
# --------------------------------------------------------------------------- #
class _ChatClient:
    """主生成 client：chat 返回 reasoning_content；记忆抽取等给非 JSON。"""

    def __init__(self, reply="晚上写东西挺安静的。你一般写什么？",
                 reasoning="我先想了一下：对方在聊写东西。", model="deepseek-flash"):
        self.reply = reply
        self.reasoning = reasoning
        self.model = model
        self.calls: list[dict] = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        self.calls.append({"messages": messages, "kwargs": kwargs, "text": text})
        if "用户记忆抽取器" in text or "抽取器" in text:
            return {"ok": True, "text": "不是 JSON"}
        return {"ok": True, "text": self.reply, "reasoning": self.reasoning,
                "model": self.model, "finish_reason": "stop",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}}

    def submit(self, query):
        return {"ok": True, "text": self.reply, "reasoning": self.reasoning}


def _chat_agent(tmp_path, monkeypatch, name="chat_trace", client=None,
                reply="晚上写东西挺安静的。你一般写什么？"):
    import random
    from agi.selflearn import chat as chat_mod
    from agi.selflearn.chat import SelfLearnAgent

    memory_item = {"id": "UM-1", "scope": "user", "kind": "preference",
                   "content": "喜欢晚上写代码", "evidence": "我喜欢晚上写东西",
                   "created_at": time.time()}
    monkeypatch.setattr(chat_mod.memory, "retrieve_tiered",
                        lambda path, text, **kwargs: {"items": [memory_item]})
    path = tmp_path / f"{name}.sqlite3"
    agent = SelfLearnAgent(path, client=client or _ChatClient(reply=reply),
                           rng=random.Random(7))
    return agent, path


def test_chat_send_records_full_trace(tmp_path, monkeypatch):
    agent, path = _chat_agent(tmp_path, monkeypatch)
    result = agent.send("我喜欢晚上写东西")
    assert result["ok"] is True and result["reply"]

    rows = list_turns(path, mode="chat")
    assert rows, "chat.send 后应该有一条 trace"
    item = get_turn(path, rows[0]["id"])
    assert item["user_text"] == "我喜欢晚上写东西"
    assert item["system_prompt"]            # 最终 system（含 P16-A 时间行也正常）
    assert item["prompt_user"] and "用户刚刚说" in item["prompt_user"]
    assert item["memories"] and item["memories"][0]["id"] == "UM-1"
    assert isinstance(item["inner"], dict)
    generation = item["generation"]
    assert generation["raw_text"].startswith("晚上写东西挺安静的")
    assert generation["reasoning"] == "我先想了一下：对方在聊写东西。"
    assert generation["finish_reason"] == "stop"
    assert generation["usage"]["tokens_in"] == 100
    assert generation["purpose"] == "selflearn_chat"
    assert isinstance(item["polish"], dict)
    assert item["guard"]["level"] == "L0"
    assert item["final_reply"] and item["final_reply"] != "？"
    assert item["latency_ms"] is not None and item["latency_ms"] >= 0
    assert item["skipped"] == 0 and item["crisis"] == 0


def test_chat_trace_reasoning_empty_when_client_absent(tmp_path, monkeypatch):
    client = _ChatClient(reasoning="")
    agent, path = _chat_agent(tmp_path, monkeypatch, name="chat_noreason",
                              client=client)
    agent.send("周末去哪玩")
    item = get_turn(path, list_turns(path)[0]["id"])
    assert item["generation"]["reasoning"] == ""


def test_chat_skip_path_records_skipped_trace(tmp_path, monkeypatch):
    import random
    from agi.selflearn import state
    agent, path = _chat_agent(tmp_path, monkeypatch, name="chat_skip")
    data = state.load(path)
    data["self"]["mood"] = 0.1
    state.save(data, path)
    agent.rng = random.Random(1)
    result = agent.send("在")
    assert result.get("skipped") is True
    rows = list_turns(path, mode="chat")
    assert rows and rows[0]["skipped"] == 1
    assert "心情不太好" in rows[0]["skip_reason"]


def test_chat_crisis_path_records_crisis_trace(tmp_path, monkeypatch):
    agent, path = _chat_agent(tmp_path, monkeypatch, name="chat_crisis")
    result = agent.send("我不想活了")
    assert result["crisis"] is True
    rows = list_turns(path, mode="chat")
    assert rows and rows[0]["crisis"] == 1
    item = get_turn(path, rows[0]["id"])
    assert item["final_reply"]
    assert item["mode"] == "chat"


def test_chat_trace_disabled_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACE_ENABLED", "0")
    agent, path = _chat_agent(tmp_path, monkeypatch, name="chat_off")
    assert agent.send("今天有点累")["ok"] is True
    assert list_turns(path) == []
