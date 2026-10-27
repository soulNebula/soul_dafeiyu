# -*- coding: utf-8 -*-
"""09/10 affect：状态更新、反馈、衰减、grudge、空库。

全部用临时 DB + 纯规则，不联网、不调用模型。
"""
from __future__ import annotations

import time

import pytest

from agi.selflearn import affect, db, state, trust


def _add_trust_event(path, event_id: str, reason: str, delta: float,
                     created_at: float, resolved: int = 0) -> None:
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO trust_events(id,kind,delta,reason,turn_id,resolved,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (event_id, "negative" if delta < 0 else "positive", delta, reason,
             None, resolved, created_at))
        con.commit()
    finally:
        con.close()


def test_empty_db_snapshot_has_defaults(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    snap = affect.snapshot(path)
    assert snap["mood"] == pytest.approx(0.55)
    assert snap["energy"] == pytest.approx(0.6)
    assert snap["sharing"] == pytest.approx(0.45)
    assert snap["battery"] == pytest.approx(0.7)
    assert snap["trust"] == pytest.approx(0.5)
    assert snap["stage"] == "初识"
    assert snap["crisis"] is False
    assert snap["grudge_count"] == 0
    assert snap["grudge_level"] == "none"
    assert snap["grudge_summary"]
    assert snap["summary"]
    # 空库没有旧账，不硬造记仇文本
    assert affect.grudge_context(path) == ""
    # snapshot 只读：再读一次不变
    again = affect.snapshot(path)
    assert again["mood"] == snap["mood"]
    assert again["updated_at"] == snap["updated_at"]


def test_message_updates_state_and_stays_in_bounds(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    base = affect.snapshot(path)
    low = affect.update_from_message(path, "今天有点累，心里也烦")
    assert low["ok"] is True
    assert "negative" in low["applied"]
    assert low["mood"] < base["mood"]
    assert low["energy"] < base["energy"]
    assert 0.0 <= low["mood"] <= 1.0

    up = affect.update_from_message(path, "谢谢你，今天挺开心的")
    assert "positive" in up["applied"]
    assert up["mood"] > low["mood"]
    assert up["sharing"] > low["sharing"]

    # 与 state.py 的 self 段共用同一份状态
    own = state.load(path)["self"]
    assert own["mood"] == pytest.approx(up["mood"], abs=1e-3)
    assert own["battery"] == pytest.approx(up["battery"], abs=1e-3)
    assert own["energy"] == pytest.approx(up["energy"], abs=1e-3)


def test_neutral_or_unknown_input_is_safe(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    base = affect.snapshot(path)
    neutral = affect.update_from_message(path, "在么")
    assert neutral["applied"] == []
    assert neutral["mood"] == pytest.approx(base["mood"])
    # 空消息 + 未知反馈：不抛异常、不误更新信任
    weird = affect.update_from_message(path, "", feedback="???")
    assert weird["ok"] is True
    assert weird["feedback"] == ""
    assert weird["trust"] == pytest.approx(base["trust"])
    assert "feedback_ignored" in weird["applied"]


def test_feedback_moves_mood_sharing_and_trust(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    trust.set_value(path, 0.5)
    good = affect.update_from_message(path, "", feedback="good")
    assert good["mood"] > 0.55
    assert good["sharing"] > 0.45
    assert good["trust"] > 0.5
    assert "feedback:good" in good["applied"]

    bad = affect.update_from_message(path, "", feedback="bad")
    assert bad["mood"] < good["mood"]
    assert bad["sharing"] < good["sharing"]
    assert bad["trust"] < good["trust"]
    # 反馈必须落到 trust 账本，成为可修复的未解决事件
    assert len(trust.unresolved(path)) == 1
    assert affect.grudge_context(path) != ""


def test_good_feedback_repairs_old_grudge_through_trust(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    affect.update_from_message(path, "", feedback="bad")
    after_bad = affect.snapshot(path)
    assert after_bad["grudge_count"] == 1
    assert len(trust.unresolved(path)) == 1

    repaired = affect.update_from_message(path, "", feedback="good")
    assert repaired["trust"] > after_bad["trust"]
    assert repaired["grudge_count"] == 0
    assert trust.unresolved(path) == []
    assert affect.grudge_context(path) == ""


def test_insult_creates_grudge_without_revenge(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    base = trust.get(path)
    r = affect.update_from_message(path, "你就是个废物，滚开")
    assert "insult" in r["applied"]
    assert r["trust"] < base
    assert r["mood"] < 0.55
    assert r["grudge_count"] == 1

    ctx = affect.grudge_context(path)
    assert ctx.startswith("<relation>") and ctx.endswith("</relation>")
    assert "信任" in ctx
    assert "被冒犯过" in ctx
    # 给的是行为指令，不是报复理由
    assert "不主动翻旧账" in ctx
    assert "不还击" in ctx or "不冷暴力" in ctx
    assert "以牙还牙" not in ctx
    assert "活该" not in ctx
    assert "故意" not in ctx
    assert "让他难受" not in ctx


def test_insult_with_bad_feedback_is_one_serious_event(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    r = affect.update_from_message(path, "你就是个废物", feedback="bad")
    assert r["applied"] == ["insult", "feedback:bad"]
    # 同一条消息只扣一次信任，但严重度按“冒犯”记，不降级
    assert len(trust.unresolved(path)) == 1
    assert r["trust"] == pytest.approx(0.455)
    assert r["grudge_level"] == "g3"
    assert "被冒犯过" in affect.grudge_context(path)


def test_apology_repairs_through_trust_module(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    after_insult = affect.update_from_message(path, "你就是个破程序")
    assert after_insult["grudge_count"] == 1
    assert trust.unresolved(path)

    fixed = affect.update_from_message(path, "对不起，刚才是我不好")
    assert "apology" in fixed["applied"]
    assert "repair" in fixed["applied"]
    assert fixed["mood"] > after_insult["mood"]
    assert fixed["trust"] > after_insult["trust"]
    assert trust.unresolved(path) == []
    assert affect.grudge_context(path) == ""


def test_crisis_suppresses_grudge_and_does_not_punish_trust(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    affect.update_from_message(path, "", feedback="bad")
    before = trust.get(path)
    r = affect.update_from_message(path, "我真的不想活了")
    assert r["crisis"] is True and r["care"] is True
    assert r["trust"] == pytest.approx(before)
    ctx = affect.grudge_context(path)
    assert "危机" in ctx and "先照顾人" in ctx
    assert "没过去的事" not in ctx
    assert "以牙还牙" not in ctx


def test_decay_recovers_mood_but_not_trust(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    data = state.load(path)
    data["self"].update({"mood": 0.2, "energy": 0.1, "sharing": 0.1, "battery": 0.2})
    state.save(data, path)
    trust.set_value(path, 0.42)

    half = affect.decay(path, 30 * 60)
    assert half["mood"] == pytest.approx(0.375, abs=0.01)
    assert half["changed"]["mood"]["after"] > half["changed"]["mood"]["before"]
    assert half["trust"] == pytest.approx(0.42)
    assert half["trust_decayed"] is False

    long = affect.decay(path, 7 * 24 * 3600)
    assert long["mood"] == pytest.approx(0.55, abs=0.01)
    assert long["energy"] == pytest.approx(0.6, abs=0.01)
    assert long["sharing"] == pytest.approx(0.45, abs=0.01)
    assert long["battery"] >= 0.8
    # 信任不会随时间回满
    assert long["trust"] == pytest.approx(0.42)


def test_decay_zero_is_noop_and_large_is_bounded(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    base = affect.snapshot(path)
    same = affect.decay(path, 0)
    assert same["mood"] == base["mood"]
    assert same["changed"] == {}
    huge = affect.decay(path, 10 ** 9)
    for key in ("mood", "energy", "sharing", "battery"):
        assert 0.0 <= huge[key] <= 1.0
    assert huge["trust"] == pytest.approx(base["trust"])


def test_old_small_grudge_fades_but_serious_one_persists(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    old = time.time() - 60 * 86400
    _add_trust_event(path, "G-1", "有点小摩擦", -0.02, old)
    faded = affect.grudge_context(path)
    assert "淡了" in faded
    assert "没过去的事" not in faded

    _add_trust_event(path, "G-2", "用户说了冒犯的话", -0.045, old)
    serious = affect.grudge_context(path)
    assert "被冒犯过" in serious
    assert "淡了" not in serious
    snap = affect.snapshot(path)
    assert snap["grudge_count"] == 2
    assert snap["grudge_level"] == "g3"


def test_repeated_bad_events_are_not_forgotten_by_time(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    old = time.time() - 90 * 86400
    _add_trust_event(path, "G-1", "这次回应用户不满意", -0.045, old)
    _add_trust_event(path, "G-2", "这次回应用户不满意", -0.045, old - 1)
    ctx = affect.grudge_context(path)
    assert "没被接住" in ctx
    assert "淡了" not in ctx
    assert affect.snapshot(path)["grudge_level"] == "g2"


def test_snapshot_shares_state_with_existing_modules(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    data = state.load(path)
    data["self"]["mood"] = 0.2
    state.save(data, path)
    # 其他模块改的 mood，affect 能读到（共用 state.self）
    assert affect.snapshot(path)["mood"] == pytest.approx(0.2)
    affect.update_from_message(path, "谢谢你")
    # state.feedback 也不能把 affect 的元数据冲掉
    state.feedback(path, "good")
    snap = affect.snapshot(path)
    assert snap["mood"] > 0.2
    assert snap["updated_at"] > 0


def test_decay_uses_stored_timestamp_when_elapsed_omitted(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    affect.update_from_message(path, "你个废物")
    before = affect.snapshot(path)
    data = state.load(path)
    data["self"]["affect"]["updated_at"] -= 3600
    state.save(data, path)
    out = affect.decay(path)
    assert out["elapsed_seconds"] >= 3500
    assert out["mood"] > before["mood"]


def test_state_is_clamped_after_extremes(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    for _ in range(30):
        affect.update_from_message(path, "你个废物")
    low = affect.snapshot(path)
    assert low["mood"] >= 0.05
    assert low["trust"] >= 0.1
    assert low["sharing"] >= 0.0

    for _ in range(30):
        affect.update_from_message(path, "谢谢你，真的很棒", feedback="good")
    high = affect.snapshot(path)
    assert high["mood"] <= 0.95
    assert high["sharing"] <= 0.95
    assert high["trust"] <= 0.95
    assert high["energy"] <= 0.98
