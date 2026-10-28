# -*- coding: utf-8 -*-
"""04 scoring.py：60 情境桶 + 七维加权 sigmoid 评分 + 同桶 baseline。

全部离线：纯函数 + 临时 SQLite，不读活动库、不联网。
"""
from __future__ import annotations

import itertools
import json
import math

import pytest

from agi.selflearn import db, scoring


def _db(tmp_path):
    return tmp_path / "scoring.sqlite3"


def _seed(path, *rows):
    """直接写 score_records，模拟历史评分。"""
    con = db.connect(path)
    try:
        for index, item in enumerate(rows):
            con.execute(
                "INSERT OR REPLACE INTO score_records"
                "(id,turn_id,bucket,raw,score,breakdown_json,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    item.get("id", f"SEED-{index}"),
                    item.get("turn_id"),
                    item["bucket"],
                    item.get("raw", 0.0),
                    item["score"],
                    "{}",
                    item.get("created_at", float(index)),
                ),
            )
        con.commit()
    finally:
        con.close()


def _rows(path):
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute("SELECT * FROM score_records ORDER BY created_at")]
    finally:
        con.close()


def test_bucket_of_defaults_to_stranger_neutral_life():
    assert scoring.bucket_of() == "陌生|中性|生活"


def test_bucket_of_is_keyword_only():
    with pytest.raises(TypeError):
        scoring.bucket_of("陌生")  # type: ignore[misc]


def test_buckets_cover_exactly_sixty_combinations():
    names = scoring.buckets()
    expected = {
        "|".join(parts)
        for parts in itertools.product(scoring.STAGES, scoring.MOODS, scoring.TOPICS)
    }
    assert len(names) == 60
    assert len(set(names)) == 60
    assert set(names) == expected


def test_bucket_of_normalizes_aliases():
    assert scoring.bucket_of(stage="熟悉", mood="negative", topic="work") == "熟|负|工作"
    assert scoring.bucket_of(stage="信任", mood="pos", topic="恋爱") == "亲近|正|感情"
    assert scoring.bucket_of(stage="初识", mood="中性") == "陌生|中性|生活"


def test_mood_accepts_valance_number():
    assert scoring.bucket_of(mood=0.8) == "陌生|正|生活"
    assert scoring.bucket_of(mood=-0.4) == "陌生|负|生活"
    assert scoring.bucket_of(mood=0.0) == "陌生|中性|生活"


def test_bucket_of_unknown_values_fall_back_without_raising():
    assert scoring.bucket_of(stage="???", mood="", topic=None) == "陌生|中性|生活"
    assert scoring.stage_of("市场部") == "陌生"
    assert scoring.mood_of("说不上来") == "中性"
    assert scoring.topic_of("八卦") == "生活"


def test_parse_bucket_roundtrip():
    name = scoring.bucket_of(stage="亲近", mood="正", topic="兴趣")
    assert scoring.parse_bucket(name) == ("亲近", "正", "兴趣")
    assert scoring.parse_bucket("a|b") is None
    assert scoring.parse_bucket("") is None
    assert scoring.parse_bucket(None) is None


def test_weights_match_the_contract():
    assert scoring.WEIGHTS == {
        "explicit": 0.30,
        "behavior": 0.25,
        "length": 0.10,
        "emotion": 0.15,
        "retention": 0.15,
        "recurrence": -0.10,
        "judge": 0.20,
    }
    assert scoring.WEIGHTS["recurrence"] < 0
    assert abs(sum(abs(w) for w in scoring.WEIGHTS.values())) == pytest.approx(1.25)


def test_zero_signals_score_half():
    result = scoring.score_turn({name: 0 for name in scoring.WEIGHTS})
    assert result["raw"] == pytest.approx(0.0)
    assert result["score"] == pytest.approx(0.5)
    assert result["bucket"] == "陌生|中性|生活"
    assert result["score_delta"] is None
    assert result["breakdown"]["missing"] == []


def test_all_positive_signals_raw_is_weight_sum():
    signals = {
        "explicit": 1,
        "behavior": 1,
        "length": 1,
        "emotion": 1,
        "retention": 1,
        "recurrence": 0,
        "judge": 1,
    }
    result = scoring.score_turn(signals)
    assert result["raw"] == pytest.approx(1.15)
    assert result["score"] == pytest.approx(1.0 / (1.0 + math.exp(-1.15)))
    assert 0.5 < result["score"] < 1.0


def test_recurrence_lowers_score():
    baseline = scoring.score_turn({"explicit": 1.0})
    repeated = scoring.score_turn({"explicit": 1.0, "recurrence": True})
    assert repeated["raw"] == pytest.approx(baseline["raw"] - 0.10)
    assert repeated["score"] < baseline["score"]
    component = repeated["breakdown"]["components"]["recurrence"]
    assert component["value"] == pytest.approx(1.0)
    assert component["contribution"] == pytest.approx(-0.10)


def test_out_of_range_signals_are_clipped():
    result = scoring.score_turn({"explicit": 99, "behavior": -99, "judge": 5})
    components = result["breakdown"]["components"]
    assert components["explicit"]["value"] == 1.0
    assert components["behavior"]["value"] == -1.0
    assert components["judge"]["value"] == 1.0
    assert result["raw"] == pytest.approx(0.30 - 0.25 + 0.20)


def test_missing_signals_are_neutral_zero():
    result = scoring.score_turn({})
    assert result["raw"] == pytest.approx(0.0)
    assert result["score"] == pytest.approx(0.5)
    assert result["breakdown"]["missing"] == list(scoring.SIGNAL_ORDER)
    assert scoring.score_turn(None)["score"] == pytest.approx(0.5)


def test_nan_and_inf_do_not_raise():
    result = scoring.score_turn(
        {
            "explicit": float("nan"),
            "behavior": float("inf"),
            "length": float("-inf"),
            "judge": "--",
        }
    )
    # 显式传了但解析不出来的信号，和没传的信号一样按 0 处理并记入 missing
    assert set(result["breakdown"]["missing"]) == set(scoring.SIGNAL_ORDER)
    assert result["raw"] == pytest.approx(0.0)
    assert result["score"] == pytest.approx(0.5)


def test_explicit_accepts_feedback_words():
    assert scoring.score_turn({"explicit": "good"})["raw"] == pytest.approx(0.30)
    assert scoring.score_turn({"explicit": "bad"})["raw"] == pytest.approx(-0.30)
    assert scoring.score_turn({"explicit": "ok"})["raw"] == pytest.approx(0.0)
    # 也接受 turns.feedback 的键名
    assert scoring.score_turn({"feedback": "good"})["raw"] == pytest.approx(0.30)


def test_behavior_accepts_words():
    assert scoring.score_turn({"behavior": "深聊"})["raw"] == pytest.approx(0.25 * 0.9)
    assert scoring.score_turn({"behavior": "消失"})["raw"] == pytest.approx(-0.25)


def test_retention_bool_maps_to_signed_signal():
    returned = scoring.score_turn({"retention": True})
    gone = scoring.score_turn({"retention": False})
    assert returned["raw"] == pytest.approx(0.15)
    assert gone["raw"] == pytest.approx(-0.15)


def test_judge_accepts_dict_with_reason():
    result = scoring.score_turn({"judge": {"score": 1.0, "reason": "用户说出了新信息"}})
    component = result["breakdown"]["components"]["judge"]
    assert component["contribution"] == pytest.approx(0.20)
    assert "新信息" in component["note"]
    assert json.loads(json.dumps(result["breakdown"], ensure_ascii=False))["bucket"] == result["bucket"]


def test_length_signal_is_bounded_saturating_and_capped():
    assert scoring.length_signal(100, 100) == pytest.approx(0.0)
    assert scoring.length_signal(200, 100) == pytest.approx(1.0 / 3.0)
    assert scoring.length_signal(50, 100) == pytest.approx(-1.0 / 3.0)
    # 上限 4 倍：再长也不加分
    assert scoring.length_signal(10**6, 1) == pytest.approx(0.6)
    assert scoring.length_signal(0, 100) == pytest.approx(-1.0)
    assert scoring.length_signal(10, 0) == pytest.approx(0.6)
    assert scoring.length_signal(0, 0) == pytest.approx(0.0)
    assert scoring.length_signal("x", 100) == pytest.approx(0.0)


def test_length_ratio_key_is_saturated_in_score_turn():
    same = scoring.score_turn({"length_ratio": 1.0})["raw"]
    longer = scoring.score_turn({"length_ratio": 3.0})["raw"]
    capped = scoring.score_turn({"length_ratio": 4.0})["raw"]
    huge = scoring.score_turn({"length_ratio": 500})["raw"]
    assert same == pytest.approx(0.0)
    assert longer > same
    assert huge == pytest.approx(0.6 * scoring.WEIGHTS["length"])
    # 到 4 倍封顶后，再长也不加分
    assert capped == pytest.approx(huge)


def test_breakdown_contributions_sum_to_raw():
    result = scoring.score_turn(
        {
            "explicit": 1,
            "behavior": -0.5,
            "emotion": 0.4,
            "retention": True,
            "judge": 0.5,
            "length": 0.2,
        }
    )
    breakdown = result["breakdown"]
    assert set(breakdown["components"]) == set(scoring.WEIGHTS)
    total = sum(item["contribution"] for item in breakdown["components"].values())
    assert total == pytest.approx(result["raw"], abs=1e-5)
    assert breakdown["weights"] == dict(scoring.WEIGHTS)
    assert {"raw", "score", "bucket", "breakdown", "score_delta"} <= set(result)


def test_score_is_monotonic_in_explicit_signal():
    scores = [scoring.score_turn({"explicit": v})["score"] for v in (-1, -0.5, 0, 0.5, 1)]
    assert scores == sorted(scores)
    assert len(set(scores)) == len(scores)


def test_score_stays_strictly_inside_zero_one():
    best = scoring.score_turn({name: 1 for name in scoring.WEIGHTS})
    worst = scoring.score_turn({name: -1 for name in scoring.WEIGHTS})
    assert 0.0 < worst["score"] < 0.5 < best["score"] < 1.0


def test_score_delta_is_none_without_baseline(tmp_path):
    result = scoring.score_turn({"explicit": 1.0}, path=_db(tmp_path))
    assert result["score_delta"] is None
    assert result["baseline"] is None
    assert result["baseline_n"] == 0


def test_score_turn_accepts_explicit_baseline():
    result = scoring.score_turn({"explicit": 1.0}, baseline=0.5)
    assert result["baseline"] == pytest.approx(0.5)
    assert result["score_delta"] == pytest.approx(result["score"] - 0.5)


def test_score_delta_compares_only_same_bucket(tmp_path):
    path = _db(tmp_path)
    target = scoring.bucket_of(stage="亲近", mood="负", topic="工作")
    other = scoring.bucket_of(stage="陌生", mood="正", topic="兴趣")
    _seed(
        path,
        {"id": "S1", "bucket": target, "score": 0.2},
        {"id": "S2", "bucket": target, "score": 0.4},
        {"id": "S3", "bucket": other, "score": 0.9},
    )
    result = scoring.score_turn({"stage": "亲近", "mood": "负", "topic": "工作"}, path=path)
    assert result["baseline"] == pytest.approx(0.3)
    assert result["baseline_n"] == 2
    assert result["score"] == pytest.approx(0.5)
    assert result["score_delta"] == pytest.approx(0.2)


def test_score_turn_accepts_bucket_string_and_ignores_extra_keys():
    result = scoring.score_turn({"bucket": "亲近|正|兴趣", "explicit": 1.0, "reply": "...", "x": 1})
    assert result["bucket"] == "亲近|正|兴趣"
    assert (result["stage"], result["mood"], result["topic"]) == ("亲近", "正", "兴趣")
    defaulted = scoring.score_turn({"explicit": 0.5})
    assert defaulted["bucket"] == "陌生|中性|生活"


def test_baseline_of_empty_and_unknown_bucket(tmp_path):
    path = _db(tmp_path)
    assert scoring.baseline_of(path, "陌生|中性|生活") is None
    assert scoring.baseline_of(path, "") is None
    _seed(path, {"id": "S1", "bucket": "陌生|中性|生活", "score": 0.7})
    info = scoring.baseline_of(path, "陌生|中性|生活")
    assert info is not None
    assert info["n"] == 1
    assert info["mean"] == pytest.approx(0.7)


def test_record_score_writes_row_with_breakdown(tmp_path):
    path = _db(tmp_path)
    result = scoring.record_score(
        path,
        "T-1",
        {"explicit": 1.0, "stage": "熟", "mood": "正", "topic": "兴趣", "judge": {"score": 1.0, "reason": "有推进"}},
    )
    assert result["ok"] is True
    assert result["id"].startswith("SR-")
    assert result["turn_id"] == "T-1"
    assert result["score_delta"] is None

    rows = _rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == result["id"]
    assert row["turn_id"] == "T-1"
    assert row["bucket"] == "熟|正|兴趣"
    assert row["raw"] == pytest.approx(0.30 + 0.20)
    assert row["score"] == pytest.approx(result["score"])
    assert row["created_at"] > 0
    breakdown = json.loads(row["breakdown_json"])
    assert breakdown["bucket"] == "熟|正|兴趣"
    assert breakdown["components"]["explicit"]["contribution"] == pytest.approx(0.30)
    assert breakdown["components"]["judge"]["note"] == "有推进"


def test_record_score_is_idempotent_per_turn(tmp_path):
    path = _db(tmp_path)
    first = scoring.record_score(path, "T-1", {"explicit": "bad"})
    second = scoring.record_score(path, "T-1", {"explicit": "good"})
    assert first["id"] == second["id"]
    assert len(_rows(path)) == 1
    # 自己上一版的分不能当自己的 baseline
    assert second["score_delta"] is None


def test_record_score_second_turn_uses_first_as_baseline(tmp_path):
    path = _db(tmp_path)
    first = scoring.record_score(path, "T-1", {"explicit": 1.0})
    second = scoring.record_score(path, "T-2", {"explicit": -1.0})
    assert second["baseline"] == pytest.approx(first["score"])
    assert second["baseline_n"] == 1
    assert second["score_delta"] == pytest.approx(second["score"] - first["score"])
    assert second["score_delta"] < 0


def test_record_score_without_turn_id_keeps_rows(tmp_path):
    path = _db(tmp_path)
    first = scoring.record_score(path, None, {"explicit": 0.0})
    second = scoring.record_score(path, "", {"explicit": 0.0})
    assert first["id"] != second["id"]
    assert len(_rows(path)) == 2


def test_record_score_leaves_turns_row_untouched(tmp_path):
    path = _db(tmp_path)
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO turns(id,user_message_id,assistant_message_id,reply,created_at) "
            "VALUES(?,?,?,?,?)",
            ("T-9", "M-1", "M-2", "回复", 1.0),
        )
        con.commit()
    finally:
        con.close()

    scoring.record_score(path, "T-9", {"explicit": 1.0})

    con = db.connect(path)
    try:
        row = con.execute("SELECT score, feedback FROM turns WHERE id='T-9'").fetchone()
    finally:
        con.close()
    assert row["score"] is None
    assert row["feedback"] is None
