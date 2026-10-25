# -*- coding: utf-8 -*-
"""P1-f：上下文预算规则（按段落优先级整段裁剪 + token 记账 + 丢弃日志）。

硬约束：未超预算时 prompt 与直接拼接逐字节一致（chat_inner 的 sha256 基线锁不放松）。
"""
from __future__ import annotations

import json
import random
import time

import pytest

from agi.selflearn import context, db
from agi.selflearn import chat as chat_mod
from agi.selflearn.chat import SelfLearnAgent


def _sec(name, text, priority=50, *, protect=False, min_tokens=0):
    return {"name": name, "text": text, "priority": priority,
            "protect": protect, "min_tokens": min_tokens}


def _cjk(n: int) -> str:
    return "记" * n


# --------------------------------------------------------------------------- #
# assemble：整段丢弃顺序 / protect / 未超预算逐字节一致
# --------------------------------------------------------------------------- #
def test_assemble_drop_order_by_priority():
    sections = [
        _sec("user_text", "\n\n用户刚刚说：\n你好", priority=100, protect=True),
        _sec("memory_hot", _cjk(3000), priority=70),
        _sec("retrieval", _cjk(3000), priority=60),
        _sec("profile", _cjk(3000), priority=55),
        _sec("summary", _cjk(3000), priority=50),
        _sec("memory_cold", _cjk(3000), priority=30),
        _sec("closing", "\n\n直接回复。", priority=100, protect=True),
    ]
    joined = "".join(s["text"] for s in sections)
    budget = context.estimate_tokens(joined) // 2   # 需要丢掉约一半
    result = context.assemble(sections, max_tokens=budget)
    assert result["tokens"] <= budget
    # 最先丢最低优先级（memory_cold → summary → profile …）
    assert result["dropped"][0] == "memory_cold"
    assert result["dropped"] == sorted(result["dropped"],
                                       key=lambda n: {
                                           "memory_cold": 30, "summary": 50,
                                           "profile": 55, "retrieval": 60,
                                           "memory_hot": 70}[n])
    assert "user_text" not in result["dropped"]
    assert "closing" not in result["dropped"]
    assert "user_text" in result["kept"] and "closing" in result["kept"]


def test_assemble_drops_whole_section_only_when_enough():
    big = _cjk(4000)
    small = "\n\n人格层：\n简短"
    sections = [
        _sec("user_text", "\n\n用户刚刚说：\n你好", protect=True),
        _sec("memory_cold", big, priority=30),
        _sec("summary", small, priority=50),
    ]
    result = context.assemble(sections, max_tokens=context.estimate_tokens(
        "".join(s["text"] for s in sections)) - 100)
    assert result["dropped"] == ["memory_cold"]
    # 其他段完整保留（不被切半）
    assert "\n\n人格层：\n简短" in result["text"]


def test_assemble_under_budget_is_byte_identical_and_idempotent():
    sections = [
        _sec("memory_hot", "\n长期记忆：\n- 用户喜欢猫"),
        _sec("persona", "\n\n人格层：\n简短克制", protect=True),
        _sec("user_text", "\n\n用户刚刚说：\n你好", protect=True),
    ]
    joined = "".join(s["text"] for s in sections)
    fit = context.assemble(sections, max_tokens=5200)
    assert fit["text"] == joined
    assert fit["kept"] == ["memory_hot", "persona", "user_text"]
    assert fit["dropped"] == [] and fit["truncated"] == []
    again = context.assemble(sections, max_tokens=5200)
    assert again == fit                                  # 同输入稳定输出


def test_assemble_empty_and_single_huge_section():
    assert context.assemble([])["text"] == ""
    assert context.assemble([])["dropped"] == []
    huge = _sec("user_text", _cjk(6000), protect=True)
    result = context.assemble([huge], max_tokens=5200)
    assert result["dropped"] == []
    assert result["tokens"] <= 5200
    assert "user_text" in result["truncated"]
    assert result["text"]                                     # 保留 protect 的锚点


def test_assemble_tiny_budget_does_not_crash():
    sections = [
        _sec("user_text", "\n\n用户刚刚说：\n你好，今天有点累", protect=True),
        _sec("memory_hot", _cjk(500), priority=70),
        _sec("summary", _cjk(500), priority=50),
    ]
    result = context.assemble(sections, max_tokens=10)
    assert isinstance(result["text"], str)
    assert result["tokens"] == context.estimate_tokens(result["text"])
    assert result["dropped"] and "user_text" in result["kept"]


def test_assemble_prefers_truncation_after_all_drops():
    protect = _sec("user_text", "用户原话。" * 200, protect=True)
    result = context.assemble([protect], max_tokens=300)
    assert result["truncated"] == ["user_text"]
    assert result["tokens"] <= 300


# --------------------------------------------------------------------------- #
# estimate_tokens 抽查（中英混排 3 例，误差 ≤30%）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "今天有点累，想聊聊工作和生活。真的服了",
    "hello world this is a mixed 中英文 test 123",
    "哈哈哈 笑死我了 " * 20 + "ok",
])
def test_estimate_tokens_sanity(text):
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff")
    other = len(text) - cjk
    expected = cjk * 1.6 + other / 3.5
    got = context.estimate_tokens(text)
    assert abs(got - expected) / max(1.0, expected) <= 0.3
    assert context.estimate_tokens("") == 1


# --------------------------------------------------------------------------- #
# chat 集成：正常小库不丢段；超长上下文丢段 + 记账事件
# --------------------------------------------------------------------------- #
class FakeClient:
    def available(self):
        return True

    def chat(self, messages, **kwargs):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        if "记忆系统" in text or "用户记忆抽取器" in text:
            return {"ok": True, "text": json.dumps({"items": []})}
        return {"ok": True, "text": "嗯"}


def _agent(tmp_path):
    path = tmp_path / "budget.sqlite3"
    return path, SelfLearnAgent(path, client=FakeClient(), rng=random.Random(5))


def _capture_prompt(monkeypatch) -> dict:
    captured: dict = {}

    def fake_generate(client, system, prompt, memories, **kwargs):
        captured["prompt"] = prompt
        return {"ok": True, "text": "嗯"}, {"two_stage": False, "claims": []}

    monkeypatch.setattr(chat_mod.generation, "generate", fake_generate)
    return captured


def test_small_library_prompt_unchanged_under_budget(tmp_path, monkeypatch):
    path, agent = _agent(tmp_path)
    captured = _capture_prompt(monkeypatch)
    result = agent.send("你好")
    assert result["ok"]
    budget = result["context_budget"]
    assert budget["dropped"] == [] and budget["truncated"] == []
    assert budget["tokens"] == context.estimate_tokens(captured["prompt"])
    assert "…（上下文过长，已裁剪）…" not in captured["prompt"]
    # 小库不写 context_budget 事件
    con = db.connect(path)
    try:
        n = con.execute("SELECT count(*) FROM raw_events WHERE source='context_budget'").fetchone()[0]
    finally:
        con.close()
    assert n == 0


def test_long_context_drops_sections_and_logs_event(tmp_path, monkeypatch):
    path, agent = _agent(tmp_path)
    # 让画像/摘要/检索段都超长 → 必须按优先级丢段
    monkeypatch.setattr(chat_mod.profile, "render_profile",
                        lambda p: "用户画像" * 1500)
    monkeypatch.setattr(chat_mod.summary, "context", lambda p, **k: "阶段摘要" * 1500)
    monkeypatch.setattr(chat_mod.retrieval, "hybrid",
                        lambda p, q, **k: [])
    captured = _capture_prompt(monkeypatch)
    result = agent.send("你好")
    assert result["ok"]
    budget = result["context_budget"]
    assert budget["dropped"], budget
    assert context.estimate_tokens(captured["prompt"]) <= context.MAX_PROMPT_TOKENS
    assert "用户刚刚说：\n你好" in captured["prompt"]          # 用户原话 protect
    assert "直接回复。不要提" in captured["prompt"]            # 收尾 protect
    # 丢弃事件写进 raw_events（复用现有事件表）
    con = db.connect(path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM raw_events WHERE source='context_budget'").fetchall()]
    finally:
        con.close()
    assert rows
    payload = json.loads(rows[0]["meta_json"])
    assert payload["dropped"] == budget["dropped"]
    assert "truncated" in payload
