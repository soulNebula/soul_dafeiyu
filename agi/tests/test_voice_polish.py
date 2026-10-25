# -*- coding: utf-8 -*-
"""voice_polish 单测：规则、幂等、实体/数字零丢失。"""
from __future__ import annotations

import random

from agi.selflearn.voice_polish import (
    PARTICLES,
    polish_reply,
    protected_tokens,
)


class AlwaysAddRng:
    """确定性地触发加语气词。"""

    def random(self) -> float:
        return 0.0

    def choice(self, seq):
        return seq[0]


class NeverAddRng:
    def random(self) -> float:
        return 1.0

    def choice(self, seq):
        return seq[0]


def test_strip_trailing_period_keeps_question():
    assert polish_reply("好的。", rng=NeverAddRng())["text"] == "好的"
    assert polish_reply("真的吗？", rng=NeverAddRng())["text"] == "真的吗？"
    assert "？" in polish_reply("真的吗？", rng=AlwaysAddRng())["text"]


def test_connective_mapping():
    result = polish_reply(
        "然而我非常累，因此我认为要休息，此外并且以及即使如此。",
        rng=NeverAddRng(), max_chars=100)
    text = result["text"]
    for written in ("然而", "因此", "非常", "我认为", "此外", "并且", "以及", "即使"):
        assert written not in text
    for spoken in ("但是", "所以", "挺", "我觉得", "另外", "而且", "还有", "就算"):
        assert spoken in text
    assert result["changed"] is True
    assert any(reason.startswith("connective:") for reason in result["reasons"])


def test_question_mark_preserved_and_no_double_particle():
    result = polish_reply("你吃了吗？", rng=AlwaysAddRng())
    assert result["text"].endswith("？")
    # 已带语气词的不再追加
    for particle in PARTICLES:
        text = "好的" + particle
        assert polish_reply(text, rng=AlwaysAddRng())["text"] == text


def test_truncate_only_over_limit():
    short = "今天天气不错，出去走走"
    assert polish_reply(short, rng=NeverAddRng(), max_chars=40)["text"] == short
    long_text = "哈哈哈哈，哈哈哈哈，哈哈哈哈，哈哈哈哈"
    result = polish_reply(long_text, rng=NeverAddRng(), max_chars=15)
    assert result["text"] != long_text
    assert "truncate" in result["reasons"]


def test_truncate_skips_when_protected_token_would_be_lost():
    text = "先说说今天的情况，再聊聊明天安排，最后关键数字是200帧和50块"
    result = polish_reply(text, rng=NeverAddRng(), max_chars=20)
    assert "truncate" not in result["reasons"]
    assert "truncate_skipped_protected" in result["reasons"]
    for token in ("200帧", "50块"):
        assert token in result["text"]


def test_protected_tokens_extraction():
    text = "明天下午3点去汕头吃泡面，大概50块，occ 200帧，Mary 也来"
    tokens = protected_tokens(text)
    assert "3点" in tokens
    assert "200帧" in tokens
    assert "50块" in tokens
    assert "occ" in tokens
    assert "Mary" in tokens
    for entity in ("汕头", "泡面"):
        assert entity in tokens


def test_no_entity_or_number_loss_on_polish():
    text = "然而明天下午3点去汕头吃泡面，大概50块，occ 200帧，Mary 也来。"
    result = polish_reply(text, rng=NeverAddRng(), max_chars=40)
    missing = [token for token in protected_tokens(text) if token not in result["text"]]
    assert missing == [], f"丢失实体/数字: {missing}"


def test_idempotent():
    text = "然而我今天非常累，因此我认为要早点休息，出去玩的事明天再说吧。"
    first = polish_reply(text, rng=random.Random(42), max_chars=25)
    second = polish_reply(first["text"], rng=random.Random(7), max_chars=25)
    assert second["text"] == first["text"]
    assert second["changed"] is False


def test_empty_and_whitespace():
    for value in ("", "   ", "\n\t"):
        result = polish_reply(value)
        assert result["text"] == ""
        assert result["changed"] is False
        assert result["reasons"] == []


def test_mixed_english_numbers_preserved():
    text = "我用 RTX4090 跑了 3 次，occ 大概 200 帧，WiFi5 也还行。"
    result = polish_reply(text, rng=NeverAddRng())
    for token in ("RTX4090", "3", "200", "occ", "WiFi5"):
        assert token in result["text"]
    assert "。" not in result["text"]


def test_particle_probability_range():
    text = "今天有点累"
    added = sum(1 for _ in range(200)
                if polish_reply(text, rng=random.Random(_))["text"] != text)
    # 5–10% 区间（允许随机波动）
    assert 0 < added < 40


def test_reasons_recorded():
    result = polish_reply("然而好的。", rng=NeverAddRng())
    assert result["changed"] is True
    assert "strip_period" in result["reasons"]
    assert any(reason.startswith("connective:") for reason in result["reasons"])
