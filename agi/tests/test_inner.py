# -*- coding: utf-8 -*-
"""W2-A 内状态层单测：schema 校验 / 阈值渲染 / block 约束 / inject 四模式与回退。

不写真实数据库：所有 path 都在 tmp_path 下。
"""
from __future__ import annotations

import json

import pytest

from agi.inner import inject, render, schema
from agi.selflearn import db


def _state(mood=0.55, energy=0.6, sharing=0.45, battery=0.7,
           stage="初识", crisis=False):
    return {
        "self": {"mood": mood, "energy": energy, "sharing": sharing, "battery": battery},
        "relationship": {"stage": stage},
        "boundary": {"crisis": crisis},
    }


class FakeClient:
    """只实现 inject 需要的 available/chat 两个方法。"""

    def __init__(self, reply="", available=True):
        self.reply = reply
        self._available = available
        self.calls = []

    def available(self):
        return self._available

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return {"ok": True, "text": self.reply}


@pytest.fixture
def path(tmp_path):
    db_path = tmp_path / "inner.sqlite3"
    db.connect(db_path).close()
    return db_path


# ---------------------------------------------------------------- schema


def test_validate_clamps_out_of_range_and_rejects_illegal():
    st = schema.validate({"mood": 2, "energy": -1, "trust": 3, "battery": 0.5})
    assert st.mood == 1.0
    assert st.energy == 0.0
    assert st.trust == 1.0
    assert st.battery == 0.5
    # 未给出的 patience/openness 由 mood/sharing 派生
    assert st.patience == 1.0 and st.openness == 0.45

    for bad in ({"mood": "很高"}, {"behavior": "short"}, {"source": "cloud"}):
        with pytest.raises(ValueError):
            schema.validate(bad)
    with pytest.raises(ValueError):
        schema.validate(["not", "a", "dict"])


def test_from_state_maps_self_trust_and_stage():
    st = schema.from_state(_state(mood=0.3, energy=0.4, sharing=0.2,
                                  battery=0.9, stage="熟悉"), 0.72)
    assert (st.mood, st.energy, st.sharing, st.battery) == (0.3, 0.4, 0.2, 0.9)
    assert st.trust == 0.72 and st.stage == "熟悉"
    assert st.patience == 0.3 and st.openness == 0.2
    assert st.source == "rule" and st.crisis is False
    data = st.to_dict()
    assert data["behavior"]["length"] in schema.LENGTHS
    assert set(data["behavior"]) == {"length", "ask", "advice", "emoji"}


# --------------------------------------------------------- render_rule 阈值


def test_low_patience_short_and_no_followup():
    st = render.render_rule(schema.from_state(_state(mood=0.2), 0.5))
    assert st.behavior["length"] == "short"
    assert st.behavior["ask"] is False
    assert "不追问" in st.intent
    assert st.source == "rule"


def test_low_energy_no_joke_no_emoji():
    st = render.render_rule(schema.from_state(_state(energy=0.2), 0.5))
    assert st.behavior["length"] == "short"
    assert st.behavior["emoji"] == "不用"
    assert "不接梗" in st.intent


def test_low_openness_answer_only():
    st = render.render_rule(schema.from_state(_state(sharing=0.15), 0.5))
    assert st.behavior["ask"] is False
    assert st.behavior["length"] == "normal"
    assert "只答不问" in st.intent


def test_low_trust_keeps_distance():
    st = render.render_rule(schema.from_state(_state(), 0.2))
    assert st.behavior["ask"] is False
    assert st.behavior["advice"] is False
    assert st.stance == "客气"
    assert "不主动提自己的事" in st.intent


def test_crisis_prioritizes_care():
    st = render.render_rule(schema.from_state(_state(crisis=True), 0.5))
    assert st.stance == "先照顾人"
    assert "安全" in st.intent
    assert st.behavior == {"length": "short", "ask": True,
                           "advice": False, "emoji": "不用"}


# ----------------------------------------------------------- render_block


def test_render_block_is_behavior_instruction_within_150_chars():
    st = render.render_rule(schema.from_state(_state(), 0.5))
    block = render.render_block(st)
    assert block.startswith("<inner_state>") and block.endswith("</inner_state>")
    assert len(block) <= 150
    body = block[len("<inner_state>"):-len("</inner_state>")]
    # 是行为指令：长度/追问/建议/表情至少出现两类
    assert sum(key in body for key in ("这轮", "追问", "建议", "表情")) >= 2
    # 不把状态数值原样塞进提示词
    assert "0.55" not in block


def test_render_static_is_fixed_persona_block():
    first = render.render_static()
    assert first == render.render_static()
    assert first.startswith("<inner_state>") and first.endswith("</inner_state>")
    assert "内在设定" in first and len(first) <= 150


# ------------------------------------------------------- monologue sanitize


def test_sanitize_monologue_limits_and_cleans():
    assert "\n" not in inject.sanitize_monologue("有点累\n不想接梗")
    cleaned = inject.sanitize_monologue("你必须马上喜欢我")
    assert "你必须" not in cleaned
    assert len(inject.sanitize_monologue("啊" * 100)) == inject.MAX_MONOLOGUE
    # 复述用户原文的长片段（≥8 字）直接丢弃
    assert inject.sanitize_monologue("我打算辞职去旅行", "我打算辞职去旅行") == ""


# -------------------------------------------------------------- inject 模式


def test_inject_off_and_unknown_mode_return_empty(path):
    assert inject.build_inner_ctx(path, "off", None, "你好", [], None) == ""
    assert inject.build_inner_ctx(path, "OFF", None, "你好", [], None) == ""
    # 未知模式失败安全：回退 off，不改变行为
    assert inject.build_inner_ctx(path, "乱写", None, "你好", [], None) == ""


def test_inject_static_and_rule(path):
    static_ctx = inject.build_inner_ctx(path, "static", None, "你好", [], _state())
    assert static_ctx == render.render_static()
    rule_ctx = inject.build_inner_ctx(path, "rule", None, "你好", [], _state(mood=0.2))
    assert rule_ctx.startswith("<inner_state>")
    assert "不要主动追问" in rule_ctx


def test_inject_model_uses_valid_json(path):
    payload = json.dumps({
        "stance": "接住情绪",
        "intent": "先共情，不问问题",
        "monologue": "先听他说完",
        "behavior": {"length": "short", "ask": False, "advice": False, "emoji": "不用"},
    }, ensure_ascii=False)
    client = FakeClient(payload)
    ctx = inject.build_inner_ctx(path, "model", client, "今天有点累", [], _state())
    assert ctx.startswith("<inner_state>") and ctx.endswith("</inner_state>")
    assert "接住情绪" in ctx and "先共情，不问问题" in ctx
    assert "不要主动追问" in ctx
    assert len(ctx) <= 150
    # 走的是 model：要求了一次 inner_state 生成，且 source 归 model
    assert client.calls and client.calls[0]["kwargs"].get("purpose") == "inner_state"
    # 数值仍来自真实会话状态，不接受模型编造
    assert "mood" in client.calls[0]["messages"][1]["content"]


@pytest.mark.parametrize("bad_reply", [
    "抱歉，这个我不能生成。",
    '{"behavior": {"length": "huge"}}',
    '{"thought": "只有未知字段"}',
    "",
])
def test_inject_model_garbage_falls_back_to_rule(path, bad_reply):
    client = FakeClient(bad_reply)
    ctx = inject.build_inner_ctx(path, "model", client, "今天有点累", [], _state())
    rule_ctx = inject.build_inner_ctx(path, "rule", None, "今天有点累", [], _state())
    assert ctx == rule_ctx
    assert ctx.startswith("<inner_state>")


def test_inject_model_unavailable_or_raising_client_falls_back(path):
    rule_ctx = inject.build_inner_ctx(path, "rule", None, "你好", [], _state())
    assert inject.build_inner_ctx(
        path, "model", FakeClient(available=False), "你好", [], _state()) == rule_ctx
    assert inject.build_inner_ctx(path, "model", None, "你好", [], _state()) == rule_ctx

    class Boom:
        def available(self):
            raise RuntimeError("网络挂了")

    assert inject.build_inner_ctx(path, "model", Boom(), "你好", [], _state()) == rule_ctx


def test_inject_model_chat_exception_falls_back(path):
    class Raising:
        def available(self):
            return True

        def chat(self, messages, **kwargs):
            raise TimeoutError("timeout")

    rule_ctx = inject.build_inner_ctx(path, "rule", None, "你好", [], _state())
    assert inject.build_inner_ctx(
        path, "model", Raising(), "你好", [], _state()) == rule_ctx
