# -*- coding: utf-8 -*-
"""P1-3 记忆引用闭环测试：prompt 注入 [M-] id → 回复引用 → guard 校验/审计/开关。"""
from __future__ import annotations

import json
import random

import pytest

from agi.selflearn import citation, db, guard, memory
from agi.selflearn.chat import SelfLearnAgent


class FakeClient:
    def __init__(self):
        self.calls: list[list[dict]] = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        text = "\n".join(str(m.get("content") or "") for m in messages)
        if "用户记忆抽取器" in text:
            return {"ok": True, "text": json.dumps([
                {"kind": "preference", "content": "用户喜欢晚上写东西",
                 "evidence": "我喜欢晚上写东西", "confidence": 0.9}])}
        return {"ok": True, "text": "晚上写东西挺安静的。你一般写什么？"}


def _agent(tmp_path, name="memcite"):
    path = tmp_path / f"{name}.sqlite3"
    client = FakeClient()
    return SelfLearnAgent(path, client=client, rng=random.Random(7)), path, client


def _generation_prompt(client) -> str:
    for messages in client.calls:
        content = messages[-1].get("content")
        if isinstance(content, str) and "用户刚刚说" in content:
            return content
    raise AssertionError("没捕获到生成 prompt")


# --------------------------------------------------------------------------- #
# 1. 短引用生成 / 注入
# --------------------------------------------------------------------------- #
def test_short_citation_id_is_stable_and_short():
    mem = {"id": "UM-abcdef1234567890", "content": "喜欢猫"}
    first = citation.short_citation_id(mem)
    assert first == citation.short_citation_id(dict(mem))
    assert first.startswith("M-") and len(first) == 10   # M- + 8 hex
    assert citation.short_citation_id({"id": ""}) == ""
    assert citation.short_citation_id({"evidence_message_id": "M-1"}).startswith("M-")


def test_injected_ids_caps_at_ten():
    mems = [{"id": f"UM-{index}"} for index in range(20)]
    ids = citation.injected_ids(mems, limit=10)
    assert len(ids) == 10
    assert citation.injected_ids(mems, limit=0) == set()


def test_inject_lines_adds_ids_after_dash():
    mems = [{"id": "UM-1", "content": "喜欢猫"}]
    lines = ["- [user/preference] 喜欢猫（依据：我喜欢猫）"]
    allowed = citation.injected_ids(mems)
    out = citation.inject_lines(lines, mems, allowed)
    assert out[0].startswith("- [M-")
    assert "喜欢猫" in out[0]
    # 不在 allowed / 没有 id → 原样
    assert citation.inject_lines(lines, mems, set()) == lines
    assert citation.inject_lines(lines, [{"content": "无 id"}], allowed) == lines


# --------------------------------------------------------------------------- #
# 2. validate_citations 支持 allowed 短 id
# --------------------------------------------------------------------------- #
def test_validate_allowed_short_id(tmp_path):
    path = tmp_path / "cite.sqlite3"
    db.connect(path).close()
    token = citation.short_citation_id({"id": "UM-1"})
    result = citation.validate_citations(
        f"你提过。[{token}] 还有个假的。[M-deadbeef]",
        path, allowed={token})
    assert result["ok"] is False
    assert token in result["reply"]            # 有效引用保留
    assert "M-deadbeef" not in result["reply"] # 坏引用删除
    assert result["removed"] == ["M-deadbeef"]


# --------------------------------------------------------------------------- #
# 3. guard 校验 + 审计
# --------------------------------------------------------------------------- #
def test_guard_valid_citation_passes(tmp_path):
    path = tmp_path / "g1.sqlite3"
    db.connect(path).close()
    token = citation.short_citation_id({"id": "UM-1"})
    out = guard.check(f"你提过这个。[{token}]", path, citation_ids={token})
    assert "bad_citation" not in (out.get("issues") or [])
    assert token in out["reply"]


def test_guard_invalid_citation_removed_and_audited(tmp_path):
    path = tmp_path / "g2.sqlite3"
    db.connect(path).close()
    out = guard.check("你提过这个。[M-deadbeef] 真的。", path,
                      citation_ids={"M-abc12345"})
    assert "bad_citation" in (out.get("issues") or [])
    assert "[M-deadbeef]" not in out["reply"]
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT kind, meta_json FROM raw_events WHERE kind='citation_invalid'"
        ).fetchall()
    finally:
        con.close()
    assert rows
    meta = json.loads(rows[0]["meta_json"])
    assert "M-deadbeef" in meta["removed"]


def test_guard_switch_off_no_new_audit(tmp_path, monkeypatch):
    path = tmp_path / "g3.sqlite3"
    db.connect(path).close()
    monkeypatch.setenv("MEMORY_CITATION_ENABLED", "0")
    out = guard.check("你提过这个。[M-deadbeef]", path,
                      citation_ids={"M-abc12345"})
    assert "bad_citation" in (out.get("issues") or [])   # 原有策略仍在
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT count(*) FROM raw_events WHERE kind='citation_invalid'"
        ).fetchone()[0]
    finally:
        con.close()
    assert rows == 0


# --------------------------------------------------------------------------- #
# 4. chat prompt 注入 + 开关
# --------------------------------------------------------------------------- #
def test_prompt_injects_memory_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_CITATION_ENABLED", "1")
    agent, path, client = _agent(tmp_path, "prompt_on")
    result = agent.send("我喜欢晚上写东西")
    assert result["ok"] is True
    prompt = _generation_prompt(client)
    assert "[M-" in prompt
    mems = memory.list_user_memories(path)
    assert mems
    expected = citation.short_citation_id(mems[0])
    assert expected in prompt
    # 同一份记忆 id 稳定
    assert citation.short_citation_id(mems[0]) == expected


def test_prompt_switch_off_has_no_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_CITATION_ENABLED", "0")
    agent, path, client = _agent(tmp_path, "prompt_off")
    result = agent.send("我喜欢晚上写东西")
    assert result["ok"] is True
    prompt = _generation_prompt(client)
    assert "[M-" not in prompt
    assert "用户喜欢晚上写东西" in prompt or "长期记忆" in prompt


def test_prompt_switch_off_does_not_call_inject(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_CITATION_ENABLED", "0")

    def _boom(*args, **kwargs):
        raise AssertionError("关闭引用时不应调用 inject_lines")

    monkeypatch.setattr(citation, "inject_lines", _boom)
    agent, path, client = _agent(tmp_path, "prompt_off2")
    result = agent.send("我喜欢晚上写东西")
    assert result["ok"] is True


def test_disabled_env_priority(monkeypatch):
    monkeypatch.setenv("MEMORY_CITATION_ENABLED", "0")
    assert citation.enabled() is False
    monkeypatch.setenv("MEMORY_CITATION_ENABLED", "true")
    assert citation.enabled() is True
