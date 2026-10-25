# -*- coding: utf-8 -*-
"""P3-b：内状态延迟优化（slim schema 4 字段 + scope 策略）测试。

覆盖：
1. `INNER_SCHEMA_SLIM` 结构（只 4 字段、无 mood 等数值字段）与 per-schema client 构造；
2. slim 输出走生产链路：数值字段用真实状态覆盖、缺字段 → 回退 rule；
3. scope 策略矩阵：all / proactive_only / off × 被动/主动 → 是否调用模型、延迟、落库；
4. 生产默认 scope=proactive_only、测试/离线默认 all、env 覆盖与非法值回退；
5. `inner_mode=off` prompt 字节锁不变（沿用 sha256 基线）；
6. `/status` 暴露 scope/schema。
"""
from __future__ import annotations

import hashlib
import json
import random

import pytest

from agi.inner import inject
from agi.selflearn import db
from agi.selflearn.chat import SelfLearnAgent
from agi.soul import prompt as soul_prompt

INNER_HEADER = "你的内在状态（只影响语气与选择，不是事实，不要说出口）："
TEXT = "今天有点累，想聊聊工作"
BASELINE_DIRECT_PROMPT_SHA256 = (
    "77d1662c0a90e33f6b849c4e0401274b7a5965e58e2489d35d07362142528015"
)

SLIM_INNER = json.dumps({
    "stance": "接住情绪",
    "intent": "先共情，不问问题",
    "monologue": "先听他说完",
    "behavior": {"length": "short", "ask": False, "advice": False, "emoji": "不用"},
}, ensure_ascii=False)

# 完整 schema 的 13 字段输出（P3-a 口径，做 before/after 对照的 fake）
FULL_INNER = json.dumps({
    "mood": 0.45, "energy": 0.4, "sharing": 0.3, "battery": 0.5, "trust": 0.5,
    "stage": "初识", "stance": "接住情绪", "intent": "先共情，不问问题",
    "monologue": "先听他说完",
    "behavior": {"length": "short", "ask": False, "advice": False, "emoji": "不用"},
}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _isolate_inner(monkeypatch, tmp_path):
    inject.reset_stats()
    for name in ("INNER_MODE", "INNER_MODE_SCOPE", "INNER_SCHEMA", "INNER_SHADOW_ONLY",
                 "SOUL_SERVER_URL", "SOUL_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INNER_HEALTH_CACHE_S", "0")
    monkeypatch.setattr(inject, "INNER_LOG_PATH", tmp_path / "inner.log")
    monkeypatch.setattr(inject, "_read_gateway_config", lambda: {})
    yield
    inject.reset_stats()


class FakeMainClient:
    def __init__(self, reply="嗯，听着呢。今天怎么个累法？"):
        self.reply = reply
        self.calls: list[dict] = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return {"ok": True, "text": self.reply}


class FakeLocalClient:
    local_soul = True

    def __init__(self, reply: str = SLIM_INNER, *, available: bool = True,
                 error: str = ""):
        self.reply = reply
        self._available = available
        self.error = error
        self.calls: list[dict] = []

    def available(self):
        return self._available

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.error:
            return {"ok": False, "text": "", "error": self.error}
        return {"ok": True, "text": self.reply}


def _agent(tmp_path, name, *, main=None, inner_client=None):
    path = tmp_path / f"{name}.sqlite3"
    return (SelfLearnAgent(path, client=main or FakeMainClient(),
                           inner_client=inner_client, rng=random.Random(7)), path)


def _generation_prompt(client) -> str:
    for call in client.calls:
        content = call["messages"][1]["content"]
        if isinstance(content, str) and "用户刚刚说" in content and "直接回复" in content:
            return content
    raise AssertionError("没有捕获到生成 prompt")


def _inner_rows(path):
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM inner_states ORDER BY rowid").fetchall()]
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# 1. slim schema
# --------------------------------------------------------------------------- #
def test_slim_schema_only_has_four_fields():
    slim = soul_prompt.INNER_SCHEMA_SLIM
    assert set(slim["required"]) == {"stance", "intent", "monologue", "behavior"}
    assert set(slim["properties"]) == {"stance", "intent", "monologue", "behavior"}
    for numeric in ("mood", "energy", "sharing", "battery", "trust", "stage"):
        assert numeric not in slim["properties"]
    behavior = slim["properties"]["behavior"]
    assert set(behavior["required"]) == {"length", "ask", "advice", "emoji"}
    assert soul_prompt.INNER_OUTPUT_FIELDS == (
        "stance", "intent", "monologue", "behavior")
    # system 输入仍保留完整状态行（训练同源，不因 slim 输出而变化）
    system = soul_prompt.render_system(soul_prompt.profile_state("default"))
    for field in ("mood=", "energy=", "sharing=", "battery=", "trust=", "stage="):
        assert field in system


def test_client_schema_selection_and_cache_key():
    """slim/full 选择走纯净函数（不在测试里 import 真实 llama_client，避免影响
    test_soul_eval 的 sys.modules 缺失模块用例）。"""
    slim_settings = {"server_url": "http://127.0.0.1:8123", "timeout_s": 2.0,
                     "schema": "slim"}
    full_settings = dict(slim_settings, schema="full")
    assert inject.client_schema(slim_settings) == soul_prompt.INNER_SCHEMA_SLIM
    assert inject.client_schema(full_settings) is None
    # 缓存键包含 schema：不同 schema 不会互相复用同一个 client 实例
    sentinel_slim, sentinel_full = object(), object()
    inject._LOCAL_CLIENTS[("http://127.0.0.1:8123", 2.0, "slim")] = sentinel_slim
    inject._LOCAL_CLIENTS[("http://127.0.0.1:8123", 2.0, "full")] = sentinel_full
    assert inject.make_local_client(slim_settings) is sentinel_slim
    assert inject.make_local_client(full_settings) is sentinel_full


def test_slim_output_injects_and_uses_real_state(monkeypatch, tmp_path):
    monkeypatch.setenv("INNER_MODE", "model")
    monkeypatch.setenv("INNER_MODE_SCOPE", "all")
    inner = FakeLocalClient()
    main = FakeMainClient()
    agent, path = _agent(tmp_path, "slim_ok", main=main, inner_client=inner)
    result = agent.send(TEXT)

    assert result["ok"] is True
    prompt = _generation_prompt(main)
    assert INNER_HEADER in prompt and "接住情绪" in prompt
    rows = _inner_rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["mode"] == "model" and row["source"] == "local"
    assert row["injected"] == 1 and row["fallback"] == ""
    assert row["latency_ms"] is not None
    state = json.loads(row["state_json"])
    # slim 输出没有数值字段，落库 state 用真实会话状态覆盖（不是模型默认值）
    assert state["stance"] == "接住情绪"
    assert {"mood", "energy", "sharing", "battery", "trust", "stage"} <= set(state)
    assert state["behavior"]["length"] == "short"
    # 模型请求用的 schema 是 slim（若 client 是注入的 fake 则看 fake 收到的 messages 即可）
    assert inner.calls and inner.calls[0]["kwargs"]["json_mode"] is True


def test_missing_slim_field_falls_back_to_rule(monkeypatch, tmp_path):
    monkeypatch.setenv("INNER_MODE", "model")
    monkeypatch.setenv("INNER_MODE_SCOPE", "all")
    partial = json.dumps({"stance": "接住情绪", "intent": "先共情",
                          "monologue": "先听"}, ensure_ascii=False)  # 缺 behavior
    inner = FakeLocalClient(partial)
    main = FakeMainClient()
    agent, path = _agent(tmp_path, "slim_missing", main=main, inner_client=inner)
    result = agent.send(TEXT)

    assert result["ok"] is True
    prompt = _generation_prompt(main)
    assert INNER_HEADER in prompt and "语气：" in prompt      # rule 回退
    row = _inner_rows(path)[0]
    assert row["mode"] == "model" and row["source"] == "rule"
    assert row["fallback"] == "missing_fields"


# --------------------------------------------------------------------------- #
# 2. scope 策略矩阵
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scope,proactive,expected_model", [
    ("all", False, True),
    ("all", True, True),
    ("proactive_only", False, False),
    ("proactive_only", True, True),
    ("off", False, False),
    ("off", True, False),
])
def test_scope_matrix(monkeypatch, tmp_path, scope, proactive, expected_model):
    monkeypatch.setenv("INNER_MODE", "model")
    monkeypatch.setenv("INNER_MODE_SCOPE", scope)
    inner = FakeLocalClient()
    main = FakeMainClient()
    agent, path = _agent(tmp_path, f"scope_{scope}_{proactive}",
                         main=main, inner_client=inner)
    result = agent.send(TEXT, proactive=proactive)
    assert result["ok"] is True

    meta = result["generation"]["inner"]
    assert meta["scope"] == scope
    prompt = _generation_prompt(main)
    if expected_model:
        assert inner.calls, "scope 允许时应该调用本地模型"
        assert meta["mode"] == "model" and meta["source"] == "local"
        assert meta["latency_ms"] is not None
        assert "接住情绪" in prompt
        rows = _inner_rows(path)
        assert len(rows) == 1 and rows[0]["mode"] == "model"
    else:
        assert inner.calls == [], "scope 不允许时不能调模型（0 延迟）"
        assert meta["mode"] == "rule" and meta["source"] == "rule"
        assert meta["latency_ms"] is None
        assert "语气：" in prompt                      # rule 行为指令仍注入
        assert _inner_rows(path) == [], "scope 拦截不落 inner_states"


def test_scope_block_does_not_call_model_or_record_latency(monkeypatch, tmp_path):
    """proactive_only 下被动消息必须零模型调用（结构性断言，不用墙钟时间）。

    断言链（全部来自可观测结构，不受机器负载影响）：
    1. fake 内状态 client 的 calls 为空（没调模型）；
    2. gen_meta.inner: mode=rule / source=rule / latency_ms=None / injected=True；
    3. inject.stats() 的 last_latency_ms 仍为 None（没有任何模型延迟被记录）；
    4. inner_states 不落库（scope 拦截不算一次 model 尝试）；
    5. turns.latency_ms 正常写入（主链延迟埋点本身不受影响）。
    """
    monkeypatch.setenv("INNER_MODE", "model")
    monkeypatch.setenv("INNER_MODE_SCOPE", "proactive_only")
    inner = FakeLocalClient()
    main = FakeMainClient()
    agent, path = _agent(tmp_path, "zero_latency", main=main, inner_client=inner)
    agent.send("先热一下库")                       # 首次调用有建库/FTS 一次性开销
    result = agent.send(TEXT)                      # 被动

    assert result["ok"] is True
    assert inner.calls == [], "scope 拦截时不得调用本地模型"
    meta = result["generation"]["inner"]
    assert meta["mode"] == "rule" and meta["source"] == "rule"
    assert meta["latency_ms"] is None
    assert meta["injected"] is True                 # rule 行为指令仍注入
    assert meta["scope"] == "proactive_only"
    assert inject.stats()["last_latency_ms"] is None
    assert _inner_rows(path) == [], "scope 拦截不落 inner_states"

    con = db.connect(path)
    try:
        turn_latency = con.execute(
            "SELECT latency_ms FROM turns ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    finally:
        con.close()
    assert turn_latency is not None


# --------------------------------------------------------------------------- #
# 3. 配置解析默认值 / env 覆盖
# --------------------------------------------------------------------------- #
def test_scope_defaults(monkeypatch):
    # 测试/离线上下文（显式注入 client）默认 all，保持 W2/W4 的 model 语义
    assert inject.resolve_settings(production=False)["scope"] == "all"
    # 生产默认 proactive_only（被动 0 延迟）
    assert inject.resolve_settings(production=True)["scope"] == "proactive_only"
    # env 优先
    monkeypatch.setenv("INNER_MODE_SCOPE", "off")
    assert inject.resolve_settings(production=True)["scope"] == "off"
    # 非法值回退默认
    monkeypatch.setenv("INNER_MODE_SCOPE", "乱写")
    assert inject.resolve_settings(production=True)["scope"] == "proactive_only"
    assert inject.resolve_settings(production=False)["scope"] == "all"


def test_config_scope_switch(monkeypatch):
    monkeypatch.setattr(inject, "_read_gateway_config",
                        lambda: {"inner_mode": "model",
                                 "inner_mode_scope": "all"})
    settings = inject.resolve_settings(production=True)
    assert settings["mode"] == "model" and settings["scope"] == "all"


def test_schema_setting_defaults_to_slim(monkeypatch):
    assert inject.resolve_settings(production=True)["schema"] == "slim"
    monkeypatch.setenv("INNER_SCHEMA", "full")
    assert inject.resolve_settings(production=True)["schema"] == "full"
    monkeypatch.setenv("INNER_SCHEMA", "乱写")
    assert inject.resolve_settings(production=True)["schema"] == "slim"


# --------------------------------------------------------------------------- #
# 4. 直接调用 build_inner_ctx_ex：scope / slim / 回退
# --------------------------------------------------------------------------- #
def _state():
    return {"self": {"mood": 0.55, "energy": 0.6, "sharing": 0.45,
                     "battery": 0.7},
            "relationship": {"stage": "初识"}, "boundary": {"crisis": False}}


def test_build_ex_scope_block_returns_rule_without_model_call(tmp_path):
    out = inject.build_inner_ctx_ex(
        tmp_path / "db.sqlite3", "model", FakeLocalClient(), TEXT, [], _state(),
        proactive=False, scope="proactive_only")
    assert out["mode"] == "rule" and out["model_attempted"] is False
    assert out["source"] == "rule" and out["latency_ms"] is None
    assert out["ctx"].startswith("<inner_state>") and "语气：" in out["ctx"]
    out_all = inject.build_inner_ctx_ex(
        tmp_path / "db2.sqlite3", "model", FakeLocalClient(), TEXT, [], _state(),
        proactive=False, scope="all")
    assert out_all["mode"] == "model" and out_all["model_attempted"] is True
    assert out_all["source"] == "local" and out_all["latency_ms"] is not None


def test_build_ex_slim_state_overrides_numeric_from_base(tmp_path):
    out = inject.build_inner_ctx_ex(
        tmp_path / "db3.sqlite3", "model", FakeLocalClient(SLIM_INNER), TEXT, [],
        _state(), proactive=True, scope="proactive_only")
    assert out["model_attempted"] is True
    state = out["state"]
    assert state["mood"] == 0.55 and state["energy"] == 0.6
    assert state["stage"] == "初识"
    assert state["behavior"]["emoji"] == "不用"


# --------------------------------------------------------------------------- #
# 5. off 字节锁 + /status
# --------------------------------------------------------------------------- #
def test_off_prompt_matches_pre_change_sha256():
    prompt = SelfLearnAgent._prompt(
        "今天有点累", [], "", {"hint": "自然回应"}, [],
        st={"emotion": {"label": "negative"}, "relationship": {"stage": "陌生"}},
        lessons="", conflict_q="", sum_ctx="", retrieval_ctx="", profile_ctx="",
        layer_ctx="", research_ctx="")
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == BASELINE_DIRECT_PROMPT_SHA256


def test_off_mode_prompt_unchanged_with_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("INNER_MODE", "off")
    monkeypatch.setenv("INNER_MODE_SCOPE", "all")
    client = FakeMainClient()
    agent, path = _agent(tmp_path, "off_scope", main=client)
    assert agent.send(TEXT)["ok"] is True
    prompt = _generation_prompt(client)
    assert "<inner_state>" not in prompt and INNER_HEADER not in prompt
    assert "inner" not in agent.send(TEXT)["generation"]
    assert _inner_rows(path) == []


def test_status_exposes_scope_and_schema(monkeypatch):
    from agi.wechat_gateway.server import Gateway

    monkeypatch.setenv("INNER_MODE", "model")
    monkeypatch.setenv("INNER_MODE_SCOPE", "proactive_only")
    monkeypatch.setenv("INNER_SCHEMA", "slim")
    status = Gateway({}).status()
    assert status["inner"]["scope"] == "proactive_only"
    assert status["inner"]["schema"] == "slim"
    assert status["inner"]["mode"] == "model"
