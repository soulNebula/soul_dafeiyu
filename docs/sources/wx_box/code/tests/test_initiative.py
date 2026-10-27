# -*- coding: utf-8 -*-
"""11/12 initiative.py：五因子算分、候选与气泡数量、只生成不发送。"""
from __future__ import annotations

import pytest

from agi.selflearn import db, initiative, memory, scheduler, state, trust

NOW = 1_900_000_000.0


def _seed_self(path, *, mood=0.55, sharing=0.45, energy=0.6, battery=0.7):
    data = state.load(path)
    data["self"].update({"mood": mood, "sharing": sharing,
                         "energy": energy, "battery": battery})
    state.save(data, path)


def _seed_dialog(path, *, idle_hours=3.0, now=NOW):
    ts = now - idle_hours * 3600.0
    memory.store_message(path, "user", "在吗", now=ts)
    memory.store_message(path, "assistant", "在", now=ts + 1)


def _count(path, table):
    con = db.connect(path)
    try:
        return int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        con.close()


def _add_unresolved(path, reason="上次说话有点冲", now=NOW):
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO trust_events(id,kind,delta,reason,turn_id,resolved,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("TE-test", "negative", 0.0, reason, None, 0, now - 60))
        con.commit()
    finally:
        con.close()
    return "TE-test"


def _add_proactive(path, created_at):
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("PRO-test", "check_in", "在吗", "test", 0.6, "proposed", created_at))
        con.commit()
    finally:
        con.close()


def test_desire_weights_all_five_signals(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_self(path, mood=0.45, sharing=0.40)
    _seed_dialog(path, idle_hours=2)
    trust.set_value(path, 0.5)

    base = initiative.desire(path, NOW)
    assert base["ok"] is True
    assert set(base["breakdown"]) == {"mood", "sharing", "trust", "topics", "idle"}
    assert sum(base["weights"].values()) == pytest.approx(1.0)
    assert sum(base["breakdown"].values()) == pytest.approx(base["score"], abs=0.001)
    assert base["damped"] is False
    assert base["raw_score"] == pytest.approx(base["score"], abs=0.001)
    assert base["idle_hours"] == pytest.approx(2.0)
    assert base["breakdown"]["idle"] > 0
    assert base["breakdown"]["topics"] == 0.0

    # 只把心情调高，分数就要变高（不是只看空闲）
    _seed_self(path, mood=0.70, sharing=0.40)
    higher_mood = initiative.desire(path, NOW)
    assert higher_mood["breakdown"]["mood"] > base["breakdown"]["mood"]
    assert higher_mood["score"] > base["score"]

    # 只把分享欲调高
    _seed_self(path, mood=0.45, sharing=0.70)
    higher_sharing = initiative.desire(path, NOW)
    assert higher_sharing["breakdown"]["sharing"] > base["breakdown"]["sharing"]
    assert higher_sharing["score"] > base["score"]

    # 只把信任调高
    _seed_self(path, mood=0.45, sharing=0.40)
    trust.set_value(path, 0.85)
    higher_trust = initiative.desire(path, NOW)
    assert higher_trust["breakdown"]["trust"] > base["breakdown"]["trust"]
    assert higher_trust["score"] > base["score"]

    # 未解决话题（没解决的负面事件 + 到期 intent）
    trust.set_value(path, 0.5)
    before_topic = initiative.desire(path, NOW)
    assert before_topic["score"] == pytest.approx(base["score"])
    _add_unresolved(path)
    scheduler.add(path, "reminder", "记得晚上早点睡", NOW - 30)
    with_topic = initiative.desire(path, NOW)
    assert with_topic["unresolved"] == 1
    assert with_topic["due_intents"] == 1
    assert with_topic["topic_pressure"] > 0
    assert with_topic["breakdown"]["topics"] > 0
    assert with_topic["score"] > before_topic["score"]

    # 空闲更久，分数也要变高
    later = initiative.desire(path, NOW + 4 * 3600)
    assert later["idle_hours"] > with_topic["idle_hours"]
    assert later["breakdown"]["idle"] > with_topic["breakdown"]["idle"]
    assert later["score"] > with_topic["score"]


def test_pending_intent_counts_as_open_topic(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_self(path, mood=0.55, sharing=0.45)
    _seed_dialog(path, idle_hours=3)
    trust.set_value(path, 0.5)
    before = initiative.desire(path, NOW)

    scheduler.add(path, "reminder", "下周问问他考试", NOW + 3600)
    after = initiative.desire(path, NOW)
    assert after["waiting_intents"] == 1
    assert after["due_intents"] == 0
    assert after["topic_pressure"] > 0
    assert after["score"] > before["score"]


def test_low_mood_cannot_be_overridden_by_idle(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_self(path, mood=0.18, sharing=0.15)
    _seed_dialog(path, idle_hours=99)
    trust.set_value(path, 0.9)

    d = initiative.desire(path, NOW)
    assert d["available"] is False
    assert d["damped"] is True
    assert d["score"] < d["raw_score"]
    assert d["bubble_quota"] == 0
    low = initiative.decide(path, NOW)
    assert low["available"] is False
    assert low["max_bubbles"] == 0

    _seed_self(path, mood=0.85, sharing=0.80)
    high = initiative.decide(path, NOW)
    assert high["available"] is True
    assert high["max_bubbles"] == 3
    assert high["score"] > low["score"]
    assert high["desire_score"] > low["desire_score"]


@pytest.mark.parametrize("mood,sharing,expected", [
    (0.90, 0.90, 3),
    (0.78, 0.40, 3),
    (0.65, 0.55, 2),
    (0.55, 0.62, 2),
    (0.55, 0.45, 1),
    (0.20, 0.20, 0),
])
def test_decide_bubble_quota(tmp_path, mood, sharing, expected):
    path = tmp_path / "selflearn.sqlite3"
    _seed_self(path, mood=mood, sharing=sharing)
    _seed_dialog(path, idle_hours=24)
    trust.set_value(path, 0.5)

    d = initiative.desire(path, NOW)
    assert d["bubble_quota"] == expected
    r = initiative.decide(path, NOW)
    if expected == 0:
        assert r["available"] is False
        assert r["max_bubbles"] == 0
    else:
        assert r["available"] is True
        assert r["max_bubbles"] == expected
        assert r["bubble_count"] == expected


def test_just_talked_blocks_even_with_high_desire(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_self(path, mood=0.90, sharing=0.85)
    _seed_dialog(path, idle_hours=0.01)  # 36 秒
    trust.set_value(path, 0.8)

    d = initiative.desire(path, NOW)
    assert d["just_talked"] is True
    assert d["available"] is False
    assert "刚聊过" in d["reason"]

    r = initiative.decide(path, NOW)
    assert r["available"] is False
    assert "刚聊过" in r["reason"]
    assert r["max_bubbles"] == 0


def test_desire_below_threshold_keeps_quiet(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_self(path, mood=0.50, sharing=0.50)
    _seed_dialog(path, idle_hours=1.0)
    trust.set_value(path, 0.5)

    d = initiative.desire(path, NOW)
    assert d["just_talked"] is False
    assert d["available"] is False
    assert d["score"] < initiative.MIN_SCORE

    r = initiative.decide(path, NOW)
    assert r["available"] is False
    assert "不够" in r["reason"]
    assert r["max_bubbles"] == 0


def test_decide_respects_proactive_cooldown(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_self(path, mood=0.85, sharing=0.80)
    _seed_dialog(path, idle_hours=5)
    trust.set_value(path, 0.7)
    _add_proactive(path, NOW - 600)

    cooled = initiative.decide(path, NOW)
    assert cooled["available"] is False
    assert "冷却" in cooled["reason"]
    assert cooled["max_bubbles"] == 0
    # 冷却只管“发不发”，不否定它此刻想不想说
    assert initiative.desire(path, NOW)["available"] is True

    later = initiative.decide(path, NOW + 3 * 3600)
    assert later["available"] is True
    assert later["max_bubbles"] == 3


def test_candidates_are_scored_sorted_and_topic_driven(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_self(path, mood=0.82, sharing=0.75)
    _seed_dialog(path, idle_hours=6)
    trust.set_value(path, 0.75)
    _add_unresolved(path, "上次聊到一半不欢而散")
    scheduler.add(path, "reminder", "记得晚上早点睡", NOW - 60)

    cands = initiative.candidates(path, NOW)
    assert cands
    scores = [c["score"] for c in cands]
    assert scores == sorted(scores, reverse=True)
    for c in cands:
        assert {"kind", "text", "reason", "base", "score", "factor"} <= set(c)
        assert c["text"] and c["reason"]
        assert 0.0 <= c["base"] <= 1.0
        assert 0.0 < c["score"] <= 0.95
    kinds = {c["kind"] for c in cands}
    assert {"care", "follow_up", "check_in"} <= kinds
    follow_up = [c for c in cands if c["kind"] == "follow_up"][0]
    assert "早点睡" in follow_up["text"]
    assert follow_up["factor"] == "topics"
    # 只读：重复调用结果一致
    assert initiative.candidates(path, NOW) == cands


def test_candidates_include_memory_recall_when_trust_enough(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_self(path, mood=0.70, sharing=0.60)
    _seed_dialog(path, idle_hours=2)
    trust.set_value(path, 0.7)
    memory.extract_user_memories(path, None, "我最近在准备考试", "m1")

    cands = initiative.candidates(path, NOW)
    recall = [c for c in cands if c["kind"] == "share_memory"]
    assert len(recall) == 1
    assert "准备考试" in recall[0]["text"]

    trust.set_value(path, 0.3)
    lowered = initiative.candidates(path, NOW)
    assert all(c["kind"] != "share_memory" for c in lowered)


def test_decide_only_generates_candidates_and_never_writes(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_self(path, mood=0.82, sharing=0.75)
    _seed_dialog(path, idle_hours=6)
    trust.set_value(path, 0.7)
    tables = ("messages", "proactive", "intents", "trust_events")
    before = {t: _count(path, t) for t in tables}

    desire = initiative.desire(path, NOW)
    cands = initiative.candidates(path, NOW)
    r = initiative.decide(path, NOW)

    after = {t: _count(path, t) for t in tables}
    assert before == after
    assert {"available", "candidates", "score", "reason", "max_bubbles"} <= set(r)
    assert desire["available"] is True
    assert r["available"] is True
    assert r["candidates"] == cands
    assert r["text"] and r["kind"] in {c["kind"] for c in cands}
    assert "不会自动发送" in r["note"]


def test_crisis_never_offers_casual_proactive_chat(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_dialog(path, idle_hours=8)
    state.update(path, "我不想活了")

    d = initiative.desire(path, NOW)
    assert d["crisis"] is True
    assert d["available"] is False
    assert "状态不好" in d["reason"]

    r = initiative.decide(path, NOW)
    assert r["available"] is False
    assert r["max_bubbles"] == 0
    assert [c["kind"] for c in r["candidates"]] == ["crisis_care"]


def test_cold_start_has_fallback_candidate_but_stays_quiet(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    d = initiative.desire(path, NOW)
    assert d["idle_hours"] is None
    assert d["available"] is False

    r = initiative.decide(path, NOW)
    assert r["available"] is False
    assert r["max_bubbles"] == 0
    assert r["candidates"]
    assert r["candidates"][0]["kind"] == "check_in"
    assert _count(path, "messages") == 0
    assert _count(path, "proactive") == 0


def test_resolved_unresolved_topic_lowers_desire(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_self(path, mood=0.55, sharing=0.50)
    _seed_dialog(path, idle_hours=4)
    trust.set_value(path, 0.55)

    event_id = _add_unresolved(path)
    before = initiative.desire(path, NOW)
    assert before["unresolved"] == 1
    assert before["topic_pressure"] > 0

    assert trust.resolve(path, event_id) is True
    after = initiative.desire(path, NOW)
    assert after["unresolved"] == 0
    assert after["score"] < before["score"]
