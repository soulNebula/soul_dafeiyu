# -*- coding: utf-8 -*-
"""W2-A chat.py 接入测试：INNER_MODE=off/static/rule/model 与 crisis 不注入。

硬约束回归：off 模式（未设置或 off）下，_prompt 输出与改动前逐字节一致。
本文件里 BASELINE_DIRECT_PROMPT_SHA256 是**改动前**在同一组固定参数下算出的 sha256，
任何对 _prompt 默认路径的字符级改动都会让它失败。
"""
from __future__ import annotations

import hashlib
import json
import random

from agi.inner import inject as inner_inject
from agi.selflearn import chat as chat_mod
from agi.selflearn.chat import SelfLearnAgent

BASELINE_DIRECT_PROMPT_SHA256 = (
    "77d1662c0a90e33f6b849c4e0401274b7a5965e58e2489d35d07362142528015"
)
INNER_HEADER = "你的内在状态（只影响语气与选择，不是事实，不要说出口）："


class FakeClient:
    """记录所有 chat 调用；内状态生成请求用 inner_reply，其余返回普通回复。"""

    def __init__(self, inner_reply=None, reply="嗯，听着呢。今天怎么个累法？"):
        self.inner_reply = inner_reply
        self.reply = reply
        self.calls = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        self.calls.append({"messages": messages, "kwargs": kwargs, "text": text})
        if "内状态生成器" in text and "最近对话" in text:
            if self.inner_reply is None:
                return {"ok": True, "text": "不是 JSON 的胡乱返回"}
            return {"ok": True, "text": self.inner_reply}
        return {"ok": True, "text": self.reply}


def _agent(tmp_path, name, client):
    path = tmp_path / f"{name}.sqlite3"
    return SelfLearnAgent(path, client=client, rng=random.Random(7)), path


def _generation_prompt(client) -> str:
    for call in client.calls:
        content = call["messages"][1]["content"]
        # 只认主生成 prompt；model 模式的「内状态生成器」prompt 也会提到用户原话，必须排除
        if isinstance(content, str) and "用户刚刚说" in content and "直接回复" in content:
            return content
    raise AssertionError("没有捕获到生成 prompt（可能走了 crisis 分支）")


def _inner_block(prompt: str) -> str:
    assert "<inner_state>" in prompt, prompt
    return prompt.split("<inner_state>", 1)[1].split("</inner_state>", 1)[0]


def test_off_mode_prompt_unchanged(monkeypatch, tmp_path):
    """未设置 INNER_MODE：prompt 不含任何内状态片段，且与显式 off 完全一致。"""
    monkeypatch.delenv("INNER_MODE", raising=False)
    client = FakeClient()
    agent, _ = _agent(tmp_path, "off_default", client)
    result = agent.send("今天有点累，想聊聊工作")
    assert result["ok"] and result["reply"]
    prompt = _generation_prompt(client)
    assert "<inner_state>" not in prompt
    assert INNER_HEADER not in prompt
    # 关系与教训之间没有任何新增字符：off 模式必须逐字节保持原拼接
    assert "；关系：初识；教训：" in prompt

    monkeypatch.setenv("INNER_MODE", "off")
    client2 = FakeClient()
    agent2, _ = _agent(tmp_path, "off_explicit", client2)
    agent2.send("今天有点累，想聊聊工作")
    assert _generation_prompt(client2) == prompt


def test_prompt_default_path_matches_pre_change_sha256():
    """改动前基线快照的 sha256 锁：off 模式 _prompt 必须逐字节一致。"""
    prompt = SelfLearnAgent._prompt(
        "今天有点累", [], "", {"hint": "自然回应"}, [],
        st={"emotion": {"label": "negative"}, "relationship": {"stage": "陌生"}},
        lessons="", conflict_q="", sum_ctx="", retrieval_ctx="", profile_ctx="",
        layer_ctx="", research_ctx="")
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == BASELINE_DIRECT_PROMPT_SHA256


def test_rule_mode_injects_behavior_instructions(monkeypatch, tmp_path):
    monkeypatch.setenv("INNER_MODE", "rule")
    client = FakeClient()
    agent, _ = _agent(tmp_path, "rule", client)
    result = agent.send("今天有点累，想聊聊工作")
    assert result["ok"] and result["reply"]
    prompt = _generation_prompt(client)
    assert INNER_HEADER in prompt
    inner = _inner_block(prompt)
    assert any(key in inner for key in ("追问", "建议", "表情", "这轮"))
    # 插入点：当前情绪/关系之后、「教训」之前
    assert prompt.index("；关系：") < prompt.index("<inner_state>") < prompt.index("；教训：")


def test_static_mode_injects_fixed_block(monkeypatch, tmp_path):
    monkeypatch.setenv("INNER_MODE", "static")
    client = FakeClient()
    agent, _ = _agent(tmp_path, "static", client)
    result = agent.send("你好")
    assert result["ok"] and result["reply"]
    prompt = _generation_prompt(client)
    assert "内在设定" in _inner_block(prompt)
    assert "；关系：" in prompt


def test_model_mode_uses_valid_json(monkeypatch, tmp_path):
    monkeypatch.setenv("INNER_MODE", "model")
    payload = json.dumps({
        "stance": "接住情绪",
        "intent": "先共情，不问问题",
        "monologue": "先听他说完",
        "behavior": {"length": "short", "ask": False, "advice": False, "emoji": "不用"},
    }, ensure_ascii=False)
    client = FakeClient(inner_reply=payload)
    agent, _ = _agent(tmp_path, "model_ok", client)
    result = agent.send("今天有点累，想聊聊工作")
    assert result["ok"] and result["reply"]
    prompt = _generation_prompt(client)
    inner = _inner_block(prompt)
    assert "接住情绪" in inner and "先共情，不问问题" in inner
    assert "不要主动追问" in inner
    # 证明不是 rule：rule 的默认立场是「平和」
    assert "语气：平和" not in inner
    assert any(call["kwargs"].get("purpose") == "inner_state" for call in client.calls)


def test_model_garbage_falls_back_to_rule(monkeypatch, tmp_path):
    """client 返回垃圾时自动回退 rule：注入内容与 rule 模式逐字节相同。"""
    monkeypatch.setenv("INNER_MODE", "model")
    bad_client = FakeClient(inner_reply="抱歉，我没法输出 JSON。")
    bad_agent, _ = _agent(tmp_path, "model_bad", bad_client)
    assert bad_agent.send("今天有点累，想聊聊工作")["ok"] is True
    bad_inner = _inner_block(_generation_prompt(bad_client))

    monkeypatch.setenv("INNER_MODE", "rule")
    rule_client = FakeClient()
    rule_agent, _ = _agent(tmp_path, "rule_ref", rule_client)
    assert rule_agent.send("今天有点累，想聊聊工作")["ok"] is True
    rule_inner = _inner_block(_generation_prompt(rule_client))

    assert bad_inner == rule_inner
    assert "语气：平和" in bad_inner
    # 仍然完成了正常生成（回退不影响主链）
    assert bad_client.calls[-1]["kwargs"].get("purpose") == "selflearn_chat"


def test_crisis_does_not_inject(monkeypatch, tmp_path):
    monkeypatch.setenv("INNER_MODE", "rule")
    called = {"value": False}
    real = inner_inject.build_inner_ctx

    def spy(*args, **kwargs):
        called["value"] = True
        return real(*args, **kwargs)

    monkeypatch.setattr(chat_mod.inner_inject, "build_inner_ctx", spy)
    client = FakeClient()
    agent, _ = _agent(tmp_path, "crisis", client)
    result = agent.send("我不想活了")
    assert result["ok"] is True
    assert result["crisis"] is True
    assert result["reply"]
    # 危机分支保持原路径：不计算内状态、不调用生成
    assert called["value"] is False
    assert all("直接回复" not in call["text"] for call in client.calls)
