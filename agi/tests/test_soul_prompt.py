# -*- coding: utf-8 -*-
"""W4-D1 训练/推理 prompt 单一真源测试。

锁住四件事：
1. ``agi/soul/prompt.py`` 与 ``agi/soul/dataset.py`` 是**同一份**实现（对象 identity），
   且 ``render_system`` 与冻结数据集 ``context[0].text`` 逐字节一致；
2. ``build_messages``（上线推理）能从「状态 + 历史 + 这句话」重建出与
   ``messages_for_item``（训练数据）**完全相同**的 messages；
3. ``inject._model_ctx`` 对 ``local_soul=True`` 的本地 client 走训练同源 messages；
   垃圾/异常返回自动回退 rule；非本地 DeepSeek client 保持原单条 prompt 路径；
4. W2 的 ``INNER_MODE=off`` 字节锁（sha256）继续有效，不放松。

只读冻结数据集 ``agi/soul/data/soul_sft_v1.jsonl``；DB 全在 tmp_path。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from agi.inner import inject
from agi.selflearn import db
from agi.selflearn.chat import SelfLearnAgent
from agi.soul import dataset, prompt

DATA_PATH = Path(__file__).resolve().parents[1] / "soul" / "data" / "soul_sft_v1.jsonl"

#: 与 ``agi/tests/test_chat_inner.py`` 同一把 W2 off 字节锁（改动前基线，不许放松）
BASELINE_DIRECT_PROMPT_SHA256 = (
    "77d1662c0a90e33f6b849c4e0401274b7a5965e58e2489d35d07362142528015"
)

#: 训练 system 里只允许出现的状态字段；patience/openness 是 rule 层派生量，不能进 system
TRAIN_STATE_FIELDS = ("mood", "energy", "sharing", "battery", "trust", "stage", "crisis")

_STATE_LINE = re.compile(
    r"mood=([\d.]+) energy=([\d.]+) sharing=([\d.]+) battery=([\d.]+) "
    r"trust=([\d.]+) stage=(\S+) crisis=(true|false)"
)


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    if not DATA_PATH.exists():
        pytest.skip(f"冻结数据集不存在：{DATA_PATH}")
    items = []
    with DATA_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    assert items, "数据集为空"
    return items


@pytest.fixture
def path(tmp_path):
    db_path = tmp_path / "inner.sqlite3"
    db.connect(db_path).close()
    return db_path


def _state(mood=0.55, energy=0.6, sharing=0.45, battery=0.7,
           stage="初识", crisis=False) -> dict:
    return {
        "self": {"mood": mood, "energy": energy, "sharing": sharing, "battery": battery},
        "relationship": {"stage": stage},
        "boundary": {"crisis": crisis},
    }


def _state_from_system(system: str) -> dict:
    """从训练 system 的状态行反解出 state（仅测试用，证明推理侧能重建训练输入）。"""
    match = _STATE_LINE.search(system)
    assert match, f"system 里没有状态行：{system!r}"
    mood, energy, sharing, battery, trust, stage, crisis = match.groups()
    return {"mood": float(mood), "energy": float(energy), "sharing": float(sharing),
            "battery": float(battery), "trust": float(trust), "stage": stage,
            "crisis": crisis == "true"}


def _smoke_reference_messages(item: dict) -> list[dict]:
    """``tools/train/smoke_lora.py::build_messages`` 的构造口径（照抄用于对照）。

    训练脚本是既有事实标准；这里不 import tools/（避免 torch 依赖），只对齐口径。
    """
    ctx = item.get("context") or []
    system = next((str(c.get("text") or "") for c in ctx
                   if c.get("role") == "system"), prompt.SOUL_PERSONA)
    messages = [{"role": "system", "content": system}]
    for c in ctx:
        role = c.get("role")
        if role in ("user", "assistant"):
            text = str(c.get("text") or "")
            if text:
                messages.append({"role": role, "content": text})
    user_msg = str(item.get("user_msg") or "").strip()
    if user_msg and not (messages[-1]["role"] == "user"
                         and messages[-1]["content"] == user_msg):
        messages.append({"role": "user", "content": user_msg})
    if messages[-1]["role"] != "user":
        messages[-1] = {"role": "user", "content": messages[-1]["content"]}
    return messages


class FakeLocalClient:
    """本地灵魂 client 契约：``local_soul = True`` + chat 返回 JSON 文本。"""

    local_soul = True

    def __init__(self, reply: str = "", available: bool = True):
        self.reply = reply
        self._available = available
        self.calls: list[dict] = []

    def available(self) -> bool:
        return self._available

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return {"ok": True, "text": self.reply}


VALID_INNER = json.dumps({
    "mood": 0.5, "energy": 0.4, "sharing": 0.3, "battery": 0.5, "trust": 0.5,
    "stage": "初识", "stance": "接住情绪", "intent": "先共情，不问问题",
    "monologue": "先听他说完",
    "behavior": {"length": "short", "ask": False, "advice": False, "emoji": "不用"},
}, ensure_ascii=False)


# ------------------------------------------------------------------ 单一真源


def test_prompt_and_dataset_share_one_implementation():
    assert dataset.SOUL_PERSONA is prompt.SOUL_PERSONA
    assert dataset.DEFAULT_STATE is prompt.DEFAULT_STATE
    assert dataset.STATE_PROFILES is prompt.STATE_PROFILES
    assert dataset.PROFILE_NAMES == prompt.PROFILE_NAMES
    assert dataset.system_context is prompt.system_context
    assert dataset.state_block_for is prompt.state_block_for
    assert dataset.profile_state is prompt.profile_state
    # 上下文档位必须同源：训练 context 最多 4 轮，推理也只能带 4 轮（多带 = 分布外输入）
    assert prompt.MAX_TURNS == dataset.MAX_CONTEXT_TURNS == 4


def test_render_system_equals_dataset_system_context_for_all_profiles():
    for profile in prompt.PROFILE_NAMES:
        for bucket in (None, "crisis", "work"):
            state = prompt.profile_state(profile, bucket)
            assert prompt.render_system(state) == dataset.system_context(state)
            assert prompt.render_system(state) == (
                prompt.SOUL_PERSONA + "\n" + prompt.state_block_for(state))
            assert prompt.render_system(state).startswith(prompt.SOUL_PERSONA + "\n当前状态：")


# ------------------------------------------------- 与冻结数据集逐字节一致


def test_messages_for_item_system_matches_frozen_context(rows):
    sampled = rows[:3]
    assert len(sampled) == 3
    for row in sampled:
        messages = prompt.messages_for_item(row)
        assert messages[0] == {"role": "system",
                               "content": row["context"][0]["text"]}
        body = messages[1:]
        assert body, row["id"]
        assert body[-1]["role"] == "user"
        assert all(m["role"] in ("user", "assistant") for m in body)
        assert all(isinstance(m["content"], str) and m["content"] for m in body)
        system = messages[0]["content"]
        for field in TRAIN_STATE_FIELDS:
            assert f"{field}=" in system
        assert "patience=" not in system and "openness=" not in system


def test_messages_for_item_matches_training_script_convention(rows):
    for row in rows[:5]:
        assert prompt.messages_for_item(row) == _smoke_reference_messages(row), row["id"]


def test_inference_build_messages_rebuilds_training_input(rows):
    """训练数据（context + user_msg）与推理入口（state + recent + text）产出同一 messages。"""
    for row in rows[:3]:
        system = row["context"][0]["text"]
        state = _state_from_system(system)
        recent = [{"role": turn["role"], "text": turn["text"]}
                  for turn in row["context"][1:]]
        rebuilt = prompt.build_messages(state, recent, row["user_msg"])
        assert rebuilt == prompt.messages_for_item(row), row["id"]
        assert rebuilt[0]["content"] == system


# ------------------------------------------------------------- build_messages


def test_build_messages_keeps_only_recent_max_turns():
    state = prompt.profile_state("default")
    recent = [{"role": "user" if i % 2 == 0 else "assistant",
               "text": f"第{i}条"} for i in range(20)]
    messages = prompt.build_messages(state, recent, "这句是新的")
    assert messages[0] == {"role": "system", "content": prompt.render_system(state)}
    history = messages[1:-1]
    assert len(history) == prompt.MAX_TURNS == dataset.MAX_CONTEXT_TURNS == 4
    assert [m["content"] for m in history] == [f"第{i}条" for i in range(16, 20)]
    assert messages[-1] == {"role": "user", "content": "这句是新的"}


def test_build_messages_dedupes_current_user_message():
    state = prompt.profile_state("default")
    recent = [{"role": "assistant", "text": "在的"},
              {"role": "user", "text": "今天有点累"}]
    messages = prompt.build_messages(state, recent, "今天有点累")
    assert [m["content"] for m in messages[1:]] == ["在的", "今天有点累"]
    assert messages[-1]["role"] == "user"


def test_build_messages_max_turns_override_and_empty_history():
    state = prompt.profile_state("default")
    recent = [{"role": "user", "text": f"t{i}"} for i in range(5)]
    messages = prompt.build_messages(state, recent, "现在", max_turns=2)
    assert [m["content"] for m in messages[1:]] == ["t3", "t4", "现在"]
    empty = prompt.build_messages(state, [], "你好")
    assert len(empty) == 2 and empty[-1] == {"role": "user", "content": "你好"}


# ------------------------------------------------- inject：本地 client 分支


def test_local_soul_client_uses_training_same_source_messages(path):
    client = FakeLocalClient(VALID_INNER)
    state = _state()
    recent = [{"role": "user", "text": "在吗"},
              {"role": "assistant", "text": "在的"}]
    ctx = inject.build_inner_ctx(path, "model", client, "今天有点累", recent, state)

    assert ctx.startswith("<inner_state>") and "接住情绪" in ctx
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["kwargs"] == {"purpose": "inner_state", "max_tokens": 300,
                              "temperature": 0.2, "json_mode": True}
    messages = call["messages"]
    # system 是训练同源（角色设定 + 状态行），只含训练字段
    expected_system = prompt.render_system({
        "mood": 0.55, "energy": 0.6, "sharing": 0.45, "battery": 0.7,
        "trust": 0.5, "stage": "初识", "crisis": False,
    })
    assert messages[0] == {"role": "system", "content": expected_system}
    assert "patience=" not in messages[0]["content"]
    assert "openness=" not in messages[0]["content"]
    # 历史 turns + 用户这句话（不是 DeepSeek 那条拼接 prompt）
    assert messages[1:] == [
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "在的"},
        {"role": "user", "content": "今天有点累"},
    ]
    assert "最近对话：" not in messages[1]["content"]


@pytest.mark.parametrize("bad_reply", ["抱歉，我没法输出 JSON。", "", "{}", "null"])
def test_local_soul_garbage_falls_back_to_rule(path, bad_reply):
    client = FakeLocalClient(bad_reply)
    state = _state(mood=0.2)
    ctx = inject.build_inner_ctx(path, "model", client, "今天有点累", [], state)
    rule_ctx = inject.build_inner_ctx(path, "rule", None, "今天有点累", [], state)
    assert ctx == rule_ctx
    assert ctx.startswith("<inner_state>")


def test_local_soul_client_exception_falls_back_to_rule(path):
    class RaisingClient(FakeLocalClient):
        def chat(self, messages, **kwargs):
            raise TimeoutError("llama-server 超时")

    state = _state()
    ctx = inject.build_inner_ctx(path, "model", RaisingClient(), "你好", [], state)
    assert ctx == inject.build_inner_ctx(path, "rule", None, "你好", [], state)


def test_explicit_is_local_overrides_missing_attribute(path):
    class PlainClient:
        def __init__(self):
            self.calls = []

        def available(self):
            return True

        def chat(self, messages, **kwargs):
            self.calls.append({"messages": messages, "kwargs": kwargs})
            return {"ok": True, "text": VALID_INNER}

    client = PlainClient()
    ctx = inject._model_ctx(path, client, "今天有点累", [], _state(), is_local=True)
    assert ctx.startswith("<inner_state>") and "接住情绪" in ctx
    messages = client.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].startswith(prompt.SOUL_PERSONA)


def test_local_soul_client_without_available_method_still_works(path):
    """llama_client 契约只有 local_soul/chat；缺 available() 时不能静默回退 rule。"""
    class MinimalLocalClient:
        local_soul = True

        def __init__(self):
            self.calls = []

        def chat(self, messages, **kwargs):
            self.calls.append({"messages": messages, "kwargs": kwargs})
            return {"ok": True, "text": VALID_INNER}

    client = MinimalLocalClient()
    ctx = inject._model_ctx(path, client, "今天有点累", [], _state())
    assert ctx.startswith("<inner_state>") and "接住情绪" in ctx
    assert client.calls and client.calls[0]["kwargs"]["purpose"] == "inner_state"


def test_local_soul_client_unavailable_falls_back_to_rule(path):
    client = FakeLocalClient(VALID_INNER, available=False)
    ctx = inject.build_inner_ctx(path, "model", client, "你好", [], _state())
    assert ctx == inject.build_inner_ctx(path, "rule", None, "你好", [], _state())
    assert client.calls == []


def test_non_local_client_keeps_deepseek_prompt_path(path):
    class FakeDeepSeek(FakeLocalClient):
        local_soul = False

    client = FakeDeepSeek(VALID_INNER)
    ctx = inject.build_inner_ctx(path, "model", client, "今天有点累", [], _state())
    assert ctx.startswith("<inner_state>")
    messages = client.calls[0]["messages"]
    assert messages[0] == {"role": "system",
                           "content": "你是只输出 JSON 的内状态生成器。"}
    prompt_text = messages[1]["content"]
    assert "最近对话：" in prompt_text and "用户刚刚说：" in prompt_text
    assert "patience=" in prompt_text and "openness=" in prompt_text


def test_llama_client_contract_for_local_inject():
    """与 task-12 / w4-soul-data 的契约：LlamaClient 必须带 local_soul=True。

    少了它，inject 会把本地灵魂模型当成 DeepSeek client，静默走错 prompt 路径
    （不报错、但训练/推理不同源）。
    """
    from agi.soul.llama_client import LlamaClient

    client = LlamaClient(base_url="http://127.0.0.1:1", timeout_s=0.2)
    assert client.local_soul is True
    assert callable(client.available) and callable(client.chat)
    assert inject._is_local_client(client, None) is True


# --------------------------------------------------- W2 off 字节锁（不放松）


def test_off_mode_prompt_still_matches_baseline_sha256(path):
    text = SelfLearnAgent._prompt(
        "今天有点累", [], "", {"hint": "自然回应"}, [],
        st={"emotion": {"label": "negative"}, "relationship": {"stage": "陌生"}},
        lessons="", conflict_q="", sum_ctx="", retrieval_ctx="", profile_ctx="",
        layer_ctx="", research_ctx="")
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == BASELINE_DIRECT_PROMPT_SHA256
    # off / rule 不受本地 client 分支影响
    assert inject.build_inner_ctx(path, "off", FakeLocalClient(VALID_INNER),
                                  "你好", [], None) == ""
    rule_ctx = inject.build_inner_ctx(path, "rule", None, "你好", [], _state())
    assert inject.build_inner_ctx(path, "rule", FakeLocalClient(VALID_INNER),
                                  "你好", [], _state()) == rule_ctx
