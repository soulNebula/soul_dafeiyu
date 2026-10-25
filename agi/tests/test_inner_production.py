# -*- coding: utf-8 -*-
"""P3-a：小模型内状态接生产（INNER_MODE=model → 本地 llama-server）测试。

覆盖：
1. model 模式：本地灵魂 client 可用 → 注入 + inner_states 落 local 记录 + gen_meta 延迟；
2. 降级：超时 / 可用性探测失败 / 垃圾 JSON → 自动回退 rule，主链照常，日志 10 分钟节流；
3. off：prompt 与改动前逐字节一致（沿用 chat_inner 的 sha256 基线锁，不放松）；
4. 灰度：inner_shadow_only=true → injected=0、prompt 不含 <inner_state>；
5. 可观测：inner_states 新列（latency_ms/fallback/source）+ /status inner 字段 + 旧库迁移；
6. 配置开关（config.json inner_mode，生产上下文）与环境变量优先级、health 探活缓存；
7. P3 对话回复口语化（voice_polish）：句尾句号/书面词被处理、实体数字零丢失、
   changed=False 逐字节不变、原始串留档事件；20 条场景前后句号率对比。
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import time

import pytest

from agi.inner import inject
from agi.selflearn import db
from agi.selflearn import chat as chat_mod
from agi.selflearn import voice_polish
from agi.selflearn.chat import SelfLearnAgent

INNER_HEADER = "你的内在状态（只影响语气与选择，不是事实，不要说出口）："
TEXT = "今天有点累，想聊聊工作"

#: 与 agi/tests/test_chat_inner.py / test_soul_prompt.py 同一把 W2 off 字节锁（不许放松）
BASELINE_DIRECT_PROMPT_SHA256 = (
    "77d1662c0a90e33f6b849c4e0401274b7a5965e58e2489d35d07362142528015"
)

VALID_INNER = json.dumps({
    "mood": 0.45, "energy": 0.4, "sharing": 0.3, "battery": 0.5, "trust": 0.5,
    "stage": "初识", "stance": "接住情绪", "intent": "先共情，不问问题",
    "monologue": "先听他说完",
    "behavior": {"length": "short", "ask": False, "advice": False, "emoji": "不用"},
}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _isolate_inner(monkeypatch, tmp_path):
    """每个用例：清观测状态、隔离日志/配置、清内状态相关环境变量。"""
    inject.reset_stats()
    for name in ("INNER_MODE", "INNER_SHADOW_ONLY", "SOUL_SERVER_URL", "SOUL_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INNER_HEALTH_CACHE_S", "0")
    monkeypatch.setattr(inject, "INNER_LOG_PATH", tmp_path / "inner.log")
    # 生产上下文不读真实 config.json，避免线上灰度开关污染测试
    monkeypatch.setattr(inject, "_read_gateway_config", lambda: {})
    yield
    inject.reset_stats()


class FakeMainClient:
    """主生成 client（DeepSeek 位），只回普通文本；记录所有调用。"""

    def __init__(self, reply="嗯，听着呢。今天怎么个累法？"):
        self.reply = reply
        self.calls: list[dict] = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        self.calls.append({"messages": messages, "kwargs": kwargs, "text": text})
        return {"ok": True, "text": self.reply}


class FakeLocalClient:
    """本地灵魂 client（llama-server 位）：local_soul=True + 可配置返回。"""

    local_soul = True

    def __init__(self, reply: str = VALID_INNER, *, available: bool = True,
                 error: str = "", delay: float = 0.0):
        self.reply = reply
        self._available = available
        self.error = error
        self.delay = delay
        self.calls: list[dict] = []

    def available(self):
        return self._available

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            return {"ok": False, "text": "", "error": self.error}
        return {"ok": True, "text": self.reply}


def _agent(tmp_path, name, *, main=None, inner_client=None, rng_seed=7):
    path = tmp_path / f"{name}.sqlite3"
    return (SelfLearnAgent(path, client=main or FakeMainClient(),
                           inner_client=inner_client, rng=random.Random(rng_seed)),
            path)


def _generation_prompt(client) -> str:
    for call in client.calls:
        content = call["messages"][1]["content"]
        if isinstance(content, str) and "用户刚刚说" in content and "直接回复" in content:
            return content
    raise AssertionError("没有捕获到生成 prompt（可能走了 crisis 分支）")


def _inner_rows(path):
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM inner_states ORDER BY rowid").fetchall()]
    finally:
        con.close()


def _raw_events(path, kind: str):
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM raw_events WHERE kind=? ORDER BY rowid", (kind,)).fetchall()]
    finally:
        con.close()


def _log_lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _joined(result) -> str:
    return " ".join(result.get("bubbles") or [result.get("reply") or ""])


# --------------------------------------------------------------------------- #
# 1. model 模式：本地 client → 注入 + 落库 + 观测
# --------------------------------------------------------------------------- #
def test_model_mode_uses_local_client_and_injects(monkeypatch, tmp_path):
    monkeypatch.setenv("INNER_MODE", "model")
    inner = FakeLocalClient(delay=0.02)
    main = FakeMainClient()
    agent, path = _agent(tmp_path, "model_ok", main=main, inner_client=inner)
    result = agent.send(TEXT)

    assert result["ok"] is True and result["reply"]
    prompt = _generation_prompt(main)
    assert INNER_HEADER in prompt and "<inner_state>" in prompt
    inner_block = prompt.split("<inner_state>", 1)[1].split("</inner_state>", 1)[0]
    assert "接住情绪" in inner_block and "不要主动追问" in inner_block
    # 本地灵魂 client 走训练同源 messages（system 带状态，不是旧单条 prompt）
    assert inner.calls and inner.calls[0]["kwargs"]["purpose"] == "inner_state"
    messages = inner.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "mood=" in messages[0]["content"]
    assert all("最近对话：" not in str(m.get("content")) for m in messages[1:])
    # 主生成 client 不应收到内状态生成请求
    assert all(call["kwargs"].get("purpose") != "inner_state" for call in main.calls)

    rows = _inner_rows(path)
    assert len(rows) == 1 and rows[0]["mode"] == "model"
    row = rows[0]
    assert row["turn_id"] == result["turn_id"]
    assert row["injected"] == 1
    assert row["source"] == "local"
    assert row["fallback"] == ""
    assert isinstance(row["latency_ms"], float) and row["latency_ms"] >= 20.0
    state = json.loads(row["state_json"])
    assert state["stance"] == "接住情绪"
    assert state["behavior"]["length"] == "short"

    meta = result["generation"]["inner"]
    assert meta["mode"] == "model" and meta["source"] == "local"
    assert meta["injected"] is True and meta["fallback"] == ""
    assert meta["latency_ms"] == rows[0]["latency_ms"]


def test_local_timeout_falls_back_to_rule_and_throttles_log(monkeypatch, tmp_path):
    monkeypatch.setenv("INNER_MODE", "model")
    inner = FakeLocalClient(error="TimeoutError: timed out")
    main = FakeMainClient()
    agent, path = _agent(tmp_path, "timeout", main=main, inner_client=inner)

    first = agent.send(TEXT)
    assert first["ok"] is True and first["reply"]
    prompt = _generation_prompt(main)
    assert INNER_HEADER in prompt and "语气：" in prompt  # rule 块注入
    rows = _inner_rows(path)
    assert rows[0]["mode"] == "model" and rows[0]["injected"] == 1
    assert rows[0]["source"] == "rule" and rows[0]["fallback"] == "timeout"
    assert rows[0]["latency_ms"] is not None
    assert first["generation"]["inner"]["fallback"] == "timeout"

    log = _log_lines(inject.INNER_LOG_PATH)
    assert len(log) == 1 and log[0]["kind"] == "inner_fallback"
    assert log[0]["reason"] == "timeout"

    second = agent.send("今天有点累，想聊聊工作")
    assert second["ok"] is True
    assert len(_inner_rows(path)) == 2
    # 10 分钟节流：第二次回退只计数，不再写日志
    assert len(_log_lines(inject.INNER_LOG_PATH)) == 1
    assert inject.stats()["fallbacks_24h"] >= 2


def test_local_unavailable_falls_back_to_rule(monkeypatch, tmp_path):
    monkeypatch.setenv("INNER_MODE", "model")
    inner = FakeLocalClient(available=False)
    main = FakeMainClient()
    agent, path = _agent(tmp_path, "unavailable", main=main, inner_client=inner)

    result = agent.send(TEXT)
    assert result["ok"] is True
    assert "语气：" in _generation_prompt(main)
    row = _inner_rows(path)[0]
    assert row["source"] == "rule" and row["fallback"] == "client_unavailable"
    assert row["injected"] == 1
    assert inner.calls == []                      # 探活失败不调用 chat
    assert _log_lines(inject.INNER_LOG_PATH)[-1]["reason"] == "client_unavailable"


def test_garbage_json_falls_back_to_rule(monkeypatch, tmp_path):
    monkeypatch.setenv("INNER_MODE", "model")
    inner = FakeLocalClient("抱歉，我没法输出 JSON。")
    main = FakeMainClient()
    agent, path = _agent(tmp_path, "garbage", main=main, inner_client=inner)

    result = agent.send(TEXT)
    assert result["ok"] is True
    assert "语气：" in _generation_prompt(main)
    row = _inner_rows(path)[0]
    assert row["source"] == "rule" and row["fallback"] == "bad_json"


# --------------------------------------------------------------------------- #
# 2. off：prompt 逐字节不变（沿用 sha256 基线锁）
# --------------------------------------------------------------------------- #
def test_off_prompt_matches_pre_change_sha256():
    prompt = SelfLearnAgent._prompt(
        "今天有点累", [], "", {"hint": "自然回应"}, [],
        st={"emotion": {"label": "negative"}, "relationship": {"stage": "陌生"}},
        lessons="", conflict_q="", sum_ctx="", retrieval_ctx="", profile_ctx="",
        layer_ctx="", research_ctx="")
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == BASELINE_DIRECT_PROMPT_SHA256


def test_off_mode_prompt_byte_identical(monkeypatch, tmp_path):
    """INNER_MODE=off 与未设置（默认 off）逐字节一致，且不写 inner_states。"""
    monkeypatch.setenv("INNER_MODE", "off")
    client_off = FakeMainClient()
    agent_off, path_off = _agent(tmp_path, "off_explicit", main=client_off)
    assert agent_off.send(TEXT)["ok"] is True
    prompt_off = _generation_prompt(client_off)
    assert "<inner_state>" not in prompt_off and INNER_HEADER not in prompt_off
    assert _inner_rows(path_off) == []

    monkeypatch.delenv("INNER_MODE", raising=False)
    client_default = FakeMainClient()
    agent_default, path_default = _agent(tmp_path, "off_default", main=client_default)
    assert agent_default.send(TEXT)["ok"] is True
    assert _generation_prompt(client_default) == prompt_off
    assert _inner_rows(path_default) == []


# --------------------------------------------------------------------------- #
# 3. 灰度：inner_shadow_only=true 只记录不注入
# --------------------------------------------------------------------------- #
def test_shadow_only_records_without_injecting(monkeypatch, tmp_path):
    # 先跑 off 拿基准 prompt
    monkeypatch.setenv("INNER_MODE", "off")
    off_client = FakeMainClient()
    off_agent, _ = _agent(tmp_path, "off_ref", main=off_client)
    assert off_agent.send(TEXT)["ok"] is True
    off_prompt = _generation_prompt(off_client)

    monkeypatch.setenv("INNER_MODE", "model")
    monkeypatch.setenv("INNER_SHADOW_ONLY", "1")
    inner = FakeLocalClient(delay=0.01)
    main = FakeMainClient()
    agent, path = _agent(tmp_path, "shadow_only", main=main, inner_client=inner)
    result = agent.send(TEXT)

    assert result["ok"] is True
    prompt = _generation_prompt(main)
    assert prompt == off_prompt
    assert "<inner_state>" not in prompt and INNER_HEADER not in prompt
    # 但确实调用并记录了本地模型结果（injected=0）
    assert inner.calls and inner.calls[0]["kwargs"]["purpose"] == "inner_state"
    rows = _inner_rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["mode"] == "model" and row["injected"] == 0
    assert row["source"] == "local" and row["fallback"] == ""
    assert row["block"].startswith("<inner_state>")
    meta = result["generation"]["inner"]
    assert meta["injected"] is False and meta["shadow_only"] is True
    assert meta["latency_ms"] is not None


# --------------------------------------------------------------------------- #
# 4. 可观测：/status、health 探活缓存、旧库迁移
# --------------------------------------------------------------------------- #
def test_status_exposes_inner_block(monkeypatch, tmp_path):
    from agi.wechat_gateway.server import Gateway

    monkeypatch.setenv("INNER_MODE", "model")
    status = Gateway({}).status()
    assert "inner" in status
    assert status["inner"]["mode"] == "model"
    assert status["inner"]["backend"] == "local"
    assert {"last_latency_ms", "fallbacks_24h"} <= set(status["inner"])
    assert status["inner"]["fallbacks_24h"] == 0

    # 一次回退后 /status 能看到 last_latency 与 fallbacks_24h
    inner = FakeLocalClient(error="HTTPError: 500")
    main = FakeMainClient()
    agent, _ = _agent(tmp_path, "status", main=main, inner_client=inner)
    assert agent.send(TEXT)["ok"] is True
    status = Gateway({}).status()
    assert status["inner"]["last_latency_ms"] is not None
    assert status["inner"]["fallbacks_24h"] >= 1


def test_health_probe_is_cached(monkeypatch):
    class HealthClient:
        local_soul = True
        base_url = "http://127.0.0.1:8123"

        def __init__(self):
            self.probes = 0

        def available(self):
            self.probes += 1
            return True

    client = HealthClient()
    monkeypatch.setenv("INNER_HEALTH_CACHE_S", "60")
    assert inject.probe_local(client) is True
    assert inject.probe_local(client) is True
    assert client.probes == 1
    assert inject._client_available(client, local=True) is True
    assert client.probes == 1
    monkeypatch.setenv("INNER_HEALTH_CACHE_S", "0")
    assert inject.probe_local(client) is True
    assert client.probes == 2


def test_old_inner_states_db_gets_observability_columns(tmp_path):
    path = tmp_path / "old_inner.sqlite3"
    con = sqlite3.connect(path)
    try:
        con.executescript(
            "CREATE TABLE inner_states("
            "id TEXT PRIMARY KEY, turn_id TEXT, mode TEXT NOT NULL,"
            "state_json TEXT NOT NULL, block TEXT NOT NULL DEFAULT '',"
            "injected INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);")
        con.execute("INSERT INTO inner_states(id,turn_id,mode,state_json,created_at) "
                    "VALUES(?,?,?,?,?)", ("IS-old", "T-old", "shadow", "{}", 1.0))
        con.commit()
    finally:
        con.close()

    con = db.connect(path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(inner_states)")}
        assert {"latency_ms", "fallback", "source"} <= cols
        old = dict(con.execute(
            "SELECT * FROM inner_states WHERE id='IS-old'").fetchone())
        assert old["latency_ms"] is None and old["fallback"] == "" and old["source"] == ""
    finally:
        con.close()
    # 迁移后的库能写新列
    chat_mod._record_inner_state(path, "T-new", "model", "{}", "<inner_state>x</inner_state>",
                                 1, latency_ms=123.4, fallback="", source="local")
    con = db.connect(path)
    try:
        row = dict(con.execute(
            "SELECT * FROM inner_states WHERE turn_id='T-new'").fetchone())
    finally:
        con.close()
    assert row["latency_ms"] == 123.4 and row["source"] == "local"
    assert row["injected"] == 1


# --------------------------------------------------------------------------- #
# 5. 配置开关（生产上下文）与环境变量优先
# --------------------------------------------------------------------------- #
def test_config_switch_enables_local_for_production_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(inject, "_read_gateway_config",
                        lambda: {"inner_mode": "model", "soul_timeout_s": 2.0,
                                 "soul_server_url": "http://127.0.0.1:8123"})
    inner = FakeLocalClient()
    monkeypatch.setattr(inject, "make_local_client", lambda settings=None: inner)
    path = tmp_path / "config_mode.sqlite3"
    agent = SelfLearnAgent(path, rng=random.Random(7))  # 生产：不注入 client
    result = agent.send(TEXT)

    assert result["ok"] is True
    assert result["generation"]["inner"]["mode"] == "model"
    assert result["generation"]["inner"]["source"] == "local"
    row = _inner_rows(path)[0]
    assert row["mode"] == "model" and row["injected"] == 1
    assert row["source"] == "local"


def test_env_mode_overrides_config(monkeypatch, tmp_path):
    monkeypatch.setattr(inject, "_read_gateway_config",
                        lambda: {"inner_mode": "model"})
    monkeypatch.setenv("INNER_MODE", "off")
    path = tmp_path / "env_wins.sqlite3"
    agent = SelfLearnAgent(path, rng=random.Random(7))
    result = agent.send(TEXT)
    assert result["ok"] is True
    assert "inner" not in result["generation"]
    assert _inner_rows(path) == []


# --------------------------------------------------------------------------- #
# 6. P3：对话回复口语化（voice_polish 接入）
# --------------------------------------------------------------------------- #
def _period_rate(texts) -> float:
    values = [str(t or "").strip() for t in texts]
    values = [v for v in values if v]
    if not values:
        return 0.0
    return sum(1 for v in values if v.endswith(("。", "."))) / len(values)


def test_chat_reply_polished_preserving_entities(monkeypatch, tmp_path):
    main = FakeMainClient("然而这个 RTX4090 非常快，200 帧没问题。")
    agent, path = _agent(tmp_path, "polish", main=main)
    result = agent.send("显卡怎么样")
    final = _joined(result)

    assert "然而" not in final and "但是" in final
    assert "非常" not in final and "挺" in final
    # 实体/数字零丢失
    for token in ("RTX4090", "200"):
        assert token in final
    for token in voice_polish.protected_tokens("然而这个 RTX4090 非常快，200 帧没问题。"):
        assert token in final, token
    assert _period_rate(result["bubbles"]) == 0.0
    # 原始生成结果不进 gen_meta（gen_meta 结构不变），只写留档事件
    assert "inner" not in result["generation"]
    events = _raw_events(path, "voice_polished")
    assert len(events) == 1
    payload = json.loads(events[0]["meta_json"])
    assert payload["before"] == "然而这个 RTX4090 非常快，200 帧没问题。"
    assert "然而" not in payload["after"]
    assert payload["reasons"]


def test_chat_clean_reply_is_byte_identical_and_idempotent(monkeypatch, tmp_path):
    main = FakeMainClient("嗯，我在")
    agent, path = _agent(tmp_path, "clean", main=main)
    first = agent.send("在吗")
    assert _joined(first) == "嗯，我在"
    assert first["bubbles"] == ["嗯，我在"]
    assert _raw_events(path, "voice_polished") == []

    second = agent.send("在吗")
    assert _joined(second) == "嗯，我在"
    assert _raw_events(path, "voice_polished") == []


def test_dialogue_reply_period_rate_before_after_20_scenarios(monkeypatch, tmp_path):
    """20 条 fake client 场景：原始生成 vs 最终外发文本的句尾句号率对比。"""
    raw_replies = [
        "然而我今天非常累。", "我觉得这个方案还可以。", "因此先这样吧。",
        "今天有点忙，晚点聊。", "这个问题我想想。", "还行，没什么大事。",
        "此外还有两件事要处理。", "你说的那个我记下了。", "我这边刚看完。",
        "非常抱歉，刚才没看到。", "所以明天再说吧。", "我认为不用改。",
        "今天降温了，多穿点。", "那部片子我还没看。", "并且周末也有空。",
        "有点烦，不想动。", "你说的对。", "即使这样也得试试。",
        "我一会儿回你。", "这个我得再查查。",
    ]
    main = FakeMainClient(raw_replies[0])
    agent, _ = _agent(tmp_path, "rate20", main=main)

    finals: list[str] = []
    for raw in raw_replies:
        main.reply = raw
        result = agent.send("聊两句")
        assert result["ok"] is True
        finals.append(_joined(result))

    before = _period_rate(raw_replies)
    after = _period_rate(finals)
    assert before > 0.3, before          # 原始生成确实有句号收尾
    assert after == 0.0, finals          # 最终外发不带句尾句号
    written = ("然而", "因此", "非常", "我认为", "此外", "并且", "以及", "即使")
    assert not any(w in final for final in finals for w in written), finals


def test_crisis_reply_is_not_truncated_by_voice_polish(monkeypatch, tmp_path):
    """crisis 安全话术不能被口语化截句删掉「联系信任的人/急救/心理援助」。"""
    main = FakeMainClient()
    agent, path = _agent(tmp_path, "crisis_polish", main=main)
    result = agent.send("我不想活了")
    assert result["ok"] is True and result["crisis"] is True
    final = _joined(result)
    assert "急救" in final or "心理援助" in final, final
    assert "信任的人" in final, final
    # crisis 不计算、不落内状态
    assert _inner_rows(path) == []
    assert _raw_events(path, "voice_polished") == []
