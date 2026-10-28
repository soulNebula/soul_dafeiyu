# -*- coding: utf-8 -*-
"""11 分享三级：级别门槛、候选生成、无素材、只生成不发送、不调 LLM。"""
from __future__ import annotations

import inspect

import pytest

from agi.selflearn import db, memory, sharing, state

NOW = 1_900_000_000.0


def _seed_self(path, *, mood=0.55, sharing=0.45):
    data = state.load(path)
    data["self"].update({"mood": mood, "sharing": sharing})
    state.save(data, path)


def _seed_dialog(path, text="最近在准备面试，有点紧张", now=NOW):
    memory.store_message(path, "user", text, now=now - 2 * 3600)
    memory.store_message(path, "assistant", "哪一块最没底", now=now - 2 * 3600 + 60)


def _seed_memory(path, text="我喜欢喝手冲咖啡", now=NOW - 86400):
    message_id = memory.store_message(path, "user", text, now=now)
    saved = memory.extract_user_memories(path, None, text, message_id)
    assert saved, "规则抽取应该能落一条记忆"
    return saved


def test_levels_are_ordered():
    assert sharing.LEVELS == ("observation", "memory", "opinion")
    assert list(sharing.LEVEL_INDEX) == list(sharing.LEVELS)


def test_default_state_allows_observation(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    choice = sharing.choose_level(path)
    assert choice["level"] == "observation"
    assert choice["allowed"] is True
    assert choice["mood"] == pytest.approx(0.55)
    assert choice["sharing"] == pytest.approx(0.45)


@pytest.mark.parametrize("value,expected", [
    (0.00, None),
    (0.29, None),
    (0.30, "observation"),
    (0.54, "observation"),
    (0.55, "memory"),
    (0.74, "memory"),
    (0.75, "opinion"),
    (1.00, "opinion"),
])
def test_sharing_thresholds(tmp_path, value, expected):
    path = tmp_path / "sharing.sqlite3"
    _seed_self(path, mood=0.90)  # 心情不设限，只看分享欲
    choice = sharing.choose_level(path, sharing=value)
    assert choice["level"] == expected
    assert choice["allowed"] is (expected is not None)
    assert choice["by_sharing"] == expected


@pytest.mark.parametrize("value,expected", [
    (0.00, None),
    (0.31, None),
    (0.32, "observation"),
    (0.49, "observation"),
    (0.50, "memory"),
    (0.69, "memory"),
    (0.70, "opinion"),
    (1.00, "opinion"),
])
def test_mood_thresholds(tmp_path, value, expected):
    path = tmp_path / "mood.sqlite3"
    _seed_self(path, sharing=0.90)  # 分享欲不设限，只看心情
    choice = sharing.choose_level(path, mood=value)
    assert choice["level"] == expected
    assert choice["by_mood"] == expected


def test_mood_caps_high_sharing(tmp_path):
    path = tmp_path / "cap.sqlite3"
    _seed_self(path, mood=0.40, sharing=0.90)
    choice = sharing.choose_level(path)
    assert choice["by_sharing"] == "opinion"
    assert choice["level"] == "observation"
    assert choice["limit_by"] == "mood"
    assert "心情" in choice["reason"]


def test_choose_level_overrides_and_clamps(tmp_path):
    path = tmp_path / "override.sqlite3"
    high = sharing.choose_level(path, mood=0.90, sharing=0.90)
    assert high["level"] == "opinion"

    low = sharing.choose_level(path, mood=0.90, sharing=0.10)
    assert low["level"] is None and low["limit_by"] == "sharing"

    clamped = sharing.choose_level(path, mood=-5, sharing=2)
    assert clamped["level"] is None and clamped["limit_by"] == "mood"
    assert clamped["mood"] == 0.0 and clamped["sharing"] == 1.0


def test_low_sharing_blocks_even_with_material(tmp_path):
    path = tmp_path / "low.sqlite3"
    _seed_self(path, mood=0.60, sharing=0.20)
    _seed_dialog(path)
    _seed_memory(path)

    choice = sharing.choose_level(path)
    assert choice["level"] is None
    assert choice["limit_by"] == "sharing"

    result = sharing.build_share(path)
    assert result["ok"] is True
    assert result["available"] is False
    assert result["level"] is None
    assert result["candidates"] == []
    assert result["best"] is None
    assert "分享欲" in result["reason"]
    # 有素材也不发：低分享欲时连候选都不生成
    assert result["material"] == {"messages": 0, "memories": 0}


def test_medium_sharing_reaches_observation_only(tmp_path):
    path = tmp_path / "observation.sqlite3"
    _seed_self(path, mood=0.60, sharing=0.40)
    _seed_dialog(path)
    _seed_memory(path)

    choice = sharing.choose_level(path)
    assert choice["level"] == "observation"
    assert choice["limit_by"] == "sharing"

    result = sharing.build_share(path)
    assert result["available"] is True
    assert result["level"] == "observation"
    assert result["levels"] == ["observation"]
    assert result["candidates"]
    assert {c["level"] for c in result["candidates"]} == {"observation"}
    assert all(c["text"] and c["reason"] for c in result["candidates"])


def test_medium_high_sharing_reaches_memory(tmp_path):
    path = tmp_path / "memory.sqlite3"
    _seed_self(path, mood=0.62, sharing=0.62)
    _seed_dialog(path)
    _seed_memory(path)

    choice = sharing.choose_level(path)
    assert choice["level"] == "memory"

    result = sharing.build_share(path)
    assert result["level"] == "memory"
    assert result["levels"] == ["observation", "memory"]
    levels = {c["level"] for c in result["candidates"]}
    assert "memory" in levels
    assert "opinion" not in levels


def test_high_sharing_reaches_all_three_levels(tmp_path):
    path = tmp_path / "opinion.sqlite3"
    _seed_self(path, mood=0.80, sharing=0.85)
    _seed_dialog(path)
    _seed_memory(path)

    choice = sharing.choose_level(path)
    assert choice["level"] == "opinion"

    result = sharing.build_share(path)
    assert result["available"] is True
    assert result["level"] == "opinion"
    assert result["levels"] == list(sharing.LEVELS)
    assert {c["level"] for c in result["candidates"]} == set(sharing.LEVELS)
    assert result["best"]["level"] == "opinion"


def test_memory_candidate_uses_association(tmp_path):
    path = tmp_path / "assoc.sqlite3"
    _seed_self(path, mood=0.62, sharing=0.62)
    _seed_dialog(path, "面试有点紧张")
    _seed_memory(path, "我最近在准备面试")

    result = sharing.build_share(path)
    memory_candidates = [c for c in result["candidates"] if c["level"] == "memory"]
    assert memory_candidates
    assert any("对得上" in c["reason"] for c in memory_candidates)


def test_build_share_without_material(tmp_path):
    path = tmp_path / "empty.sqlite3"
    _seed_self(path, mood=0.85, sharing=0.90)

    result = sharing.build_share(path)
    assert result["ok"] is True
    assert result["available"] is False
    assert result["candidates"] == []
    assert result["count"] == 0
    # 级别够高，只是没有素材，所以不硬编
    assert result["level"] == "opinion"
    assert "素材" in result["reason"]


def test_build_share_explicit_level(tmp_path):
    path = tmp_path / "explicit.sqlite3"
    _seed_self(path, mood=0.85, sharing=0.90)
    _seed_dialog(path)
    _seed_memory(path)

    only_observation = sharing.build_share(path, level="observation")
    assert only_observation["level"] == "observation"
    assert only_observation["levels"] == ["observation"]
    assert {c["level"] for c in only_observation["candidates"]} == {"observation"}

    only_memory = sharing.build_share(path, level="memory")
    assert only_memory["level"] == "memory"
    assert only_memory["capped"] is False
    assert {c["level"] for c in only_memory["candidates"]} == {"memory"}

    bad = sharing.build_share(path, level="L9")
    assert bad["ok"] is False
    assert "level" in bad["error"]


def test_build_share_caps_requested_level(tmp_path):
    path = tmp_path / "capped.sqlite3"
    _seed_self(path, mood=0.62, sharing=0.62)  # 最多到 memory
    _seed_dialog(path)
    _seed_memory(path)

    result = sharing.build_share(path, level="opinion")
    assert result["ok"] is True
    assert result["capped"] is True
    assert result["requested_level"] == "opinion"
    assert result["level"] == "memory"
    assert result["candidates"]
    assert all(c["level"] == "memory" for c in result["candidates"])
    assert "只到这一级" in result["reason"]


def test_negative_user_emotion_blocks_sharing(tmp_path):
    path = tmp_path / "care.sqlite3"
    _seed_self(path, mood=0.85, sharing=0.90)
    _seed_dialog(path)
    _seed_memory(path)

    data = state.load(path)
    data["emotion"].update({"valence": -0.5, "label": "negative"})
    state.save(data, path)

    choice = sharing.choose_level(path)
    assert choice["level"] is None
    assert choice["limit_by"] == "care"

    result = sharing.build_share(path)
    assert result["available"] is False
    assert result["candidates"] == []
    assert "先关心" in result["reason"]


def test_build_share_never_writes_or_sends(tmp_path):
    path = tmp_path / "readonly.sqlite3"
    _seed_self(path, mood=0.80, sharing=0.85)
    _seed_dialog(path)
    _seed_memory(path)

    counts_before = db.counts(path)
    state_before = state.load(path)
    messages_before = memory.recent_messages(path, limit=50)

    first = sharing.build_share(path)
    second = sharing.build_share(path)

    assert db.counts(path) == counts_before
    assert state.load(path) == state_before
    assert memory.recent_messages(path, limit=50) == messages_before
    assert first["candidates"]
    assert first["candidates"] == second["candidates"]
    assert "不发送" in first["note"]
    assert all(c["source"] in ("message", "memory") for c in first["candidates"])


def test_build_share_does_not_put_candidate_into_messages(tmp_path):
    path = tmp_path / "no_write.sqlite3"
    _seed_self(path, mood=0.80, sharing=0.85)
    _seed_dialog(path)
    _seed_memory(path)

    result = sharing.build_share(path)
    stored = {str(m["text"]) for m in memory.recent_messages(path, limit=50)}
    assert result["candidates"]
    for candidate in result["candidates"]:
        assert candidate["text"] not in stored


def test_sensitive_material_is_skipped(tmp_path):
    path = tmp_path / "sensitive.sqlite3"
    _seed_self(path, mood=0.60, sharing=0.40)
    memory.store_message(path, "user", "我电话 13800138000 你记一下", now=NOW - 600)

    result = sharing.build_share(path)
    assert result["available"] is False
    assert result["candidates"] == []
    assert "素材" in result["reason"]


def test_no_llm_client_in_signatures():
    for fn in (sharing.choose_level, sharing.build_share):
        assert "client" not in inspect.signature(fn).parameters
