# -*- coding: utf-8 -*-
"""W3-A A2：INNER_MODE=shadow（只记不注入）测试。

硬约束：
- shadow 模式 prompt 必须与 off 逐字节一致（sha256 锁）；
- rule 渲染块只落 inner_states（mode='shadow', injected=0），不进 prompt；
- shadow 失败（渲染失败 / 落库失败）静默降级，不影响主链；
- crisis 分支不计算、不落库 shadow；
- rule 模式仍正常注入，且不写 inner_states。
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3

from agi.selflearn import chat as chat_mod
from agi.selflearn import db
from agi.selflearn.chat import SelfLearnAgent

INNER_HEADER = "你的内在状态（只影响语气与选择，不是事实，不要说出口）："
TEXT = "今天有点累，想聊聊工作"


class FakeClient:
    def __init__(self, reply="嗯，听着呢。今天怎么个累法？"):
        self.reply = reply
        self.calls = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        self.calls.append({"messages": messages, "kwargs": kwargs, "text": text})
        return {"ok": True, "text": self.reply}


def _agent(tmp_path, name, client):
    path = tmp_path / f"{name}.sqlite3"
    return SelfLearnAgent(path, client=client, rng=random.Random(7)), path


def _generation_prompt(client) -> str:
    for call in client.calls:
        content = call["messages"][1]["content"]
        if isinstance(content, str) and "用户刚刚说" in content and "直接回复" in content:
            return content
    raise AssertionError("没有捕获到生成 prompt（可能走了 crisis 分支）")


def _inner_block(prompt: str) -> str:
    assert "<inner_state>" in prompt, prompt
    return prompt.split("<inner_state>", 1)[1].split("</inner_state>", 1)[0]


def _inner_rows(path):
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM inner_states ORDER BY rowid").fetchall()]
    finally:
        con.close()


def _send(tmp_path, monkeypatch, mode: str, text: str = TEXT):
    monkeypatch.setenv("INNER_MODE", mode)
    client = FakeClient()
    agent, path = _agent(tmp_path, mode, client)
    result = agent.send(text)
    return result, client, path


def test_shadow_prompt_is_byte_identical_to_off(monkeypatch, tmp_path):
    """shadow 与 off 的生成 prompt 逐字节一致（sha256 相等）。"""
    monkeypatch.setenv("INNER_MODE", "off")
    off_client = FakeClient()
    off_agent, _ = _agent(tmp_path, "off", off_client)
    assert off_agent.send(TEXT)["ok"] is True
    off_prompt = _generation_prompt(off_client)

    shadow_result, shadow_client, _ = _send(tmp_path, monkeypatch, "shadow")
    assert shadow_result["ok"] is True
    shadow_prompt = _generation_prompt(shadow_client)

    assert shadow_prompt == off_prompt
    assert (hashlib.sha256(shadow_prompt.encode("utf-8")).hexdigest()
            == hashlib.sha256(off_prompt.encode("utf-8")).hexdigest())
    # 明确：shadow 绝不注入
    assert "<inner_state>" not in shadow_prompt
    assert INNER_HEADER not in shadow_prompt
    # shadow 的 rule 渲染不能额外触发模型调用
    assert all(call["kwargs"].get("purpose") != "inner_state"
               for call in shadow_client.calls)


def test_shadow_records_rule_render_without_injecting(monkeypatch, tmp_path):
    result, client, path = _send(tmp_path, monkeypatch, "shadow")
    assert result["ok"] is True

    rows = _inner_rows(path)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["mode"] == "shadow"
    assert row["turn_id"] == result["turn_id"]
    assert row["injected"] == 0
    assert row["block"].startswith("<inner_state>")
    assert row["block"].endswith("</inner_state>")

    state = json.loads(row["state_json"])
    for key in ("mood", "energy", "sharing", "battery", "trust", "stage",
                "stance", "intent", "monologue", "behavior"):
        assert key in state, (key, state)
    assert state["behavior"]["length"] in ("short", "normal", "long")
    assert isinstance(state["behavior"]["ask"], bool)

    prompt = _generation_prompt(client)
    assert "<inner_state>" not in prompt
    assert INNER_HEADER not in prompt
    # 仍然完成了正常生成
    assert any(call["kwargs"].get("purpose") == "selflearn_chat"
               for call in client.calls)


def test_shadow_block_matches_what_rule_mode_would_inject(monkeypatch, tmp_path):
    """shadow 落库的 block 应等于 rule 模式实际注入的块（同状态、同文本）。"""
    _, _, shadow_path = _send(tmp_path, monkeypatch, "shadow")
    shadow_row = _inner_rows(shadow_path)[0]
    shadow_block = shadow_row["block"]

    _, rule_client, _ = _send(tmp_path, monkeypatch, "rule")
    rule_block = _inner_block(_generation_prompt(rule_client))

    assert _inner_block(shadow_block) == rule_block
    assert shadow_row["block"].startswith("<inner_state>")
    assert "语气：" in shadow_block


def test_rule_mode_still_injects_and_writes_no_inner_rows(monkeypatch, tmp_path):
    result, client, path = _send(tmp_path, monkeypatch, "rule")
    assert result["ok"] is True
    prompt = _generation_prompt(client)
    assert INNER_HEADER in prompt
    assert "<inner_state>" in prompt
    assert prompt.index("；关系：") < prompt.index("<inner_state>") < prompt.index("；教训：")
    assert _inner_rows(path) == []


def test_shadow_render_failure_is_silent(monkeypatch, tmp_path):
    """rule 渲染抛异常：主链照常，prompt 不注入，仍留一行空 block 便于观察。"""
    def boom(*args, **kwargs):
        raise RuntimeError("shadow render boom")

    monkeypatch.setattr(chat_mod.inner_inject, "build_inner_ctx", boom)
    monkeypatch.setenv("INNER_MODE", "shadow")
    client = FakeClient()
    agent, path = _agent(tmp_path, "render_fail", client)
    result = agent.send(TEXT)

    assert result["ok"] is True and result["reply"]
    prompt = _generation_prompt(client)
    assert "<inner_state>" not in prompt and INNER_HEADER not in prompt
    rows = _inner_rows(path)
    assert len(rows) == 1 and rows[0]["block"] == "" and rows[0]["injected"] == 0
    assert json.loads(rows[0]["state_json"])["behavior"]["length"] in (
        "short", "normal", "long")


def test_shadow_write_failure_does_not_break_main_chain(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("inner_states 写入失败")

    monkeypatch.setattr(chat_mod, "_record_inner_state", boom)
    monkeypatch.setenv("INNER_MODE", "shadow")
    client = FakeClient()
    agent, path = _agent(tmp_path, "write_fail", client)
    result = agent.send(TEXT)

    assert result["ok"] is True and result["reply"]
    assert "<inner_state>" not in _generation_prompt(client)
    assert _inner_rows(path) == []


def test_crisis_does_not_compute_or_record_shadow(monkeypatch, tmp_path):
    called = {"value": False}
    real = chat_mod.inner_inject.build_inner_ctx

    def spy(*args, **kwargs):
        called["value"] = True
        return real(*args, **kwargs)

    monkeypatch.setattr(chat_mod.inner_inject, "build_inner_ctx", spy)
    monkeypatch.setenv("INNER_MODE", "shadow")
    client = FakeClient()
    agent, path = _agent(tmp_path, "crisis", client)
    result = agent.send("我不想活了")

    assert result["ok"] is True and result["crisis"] is True and result["reply"]
    assert called["value"] is False
    assert _inner_rows(path) == []
