# -*- coding: utf-8 -*-
"""S06 learn_loop.py：缺口、来源 provenance、制卡、练习、verifier 与 L3 解锁。

测试只用 fake 数据：不联网（来源是假 URL、不会抓取）、不执行任何真实代码。
"""
from __future__ import annotations

import json

from agi.selflearn import db, learn_loop, skilltree

SKILL = "SK-CODE-PY"
URL = "https://example.com/python-sorting"
QUOTE = "sorted() 返回一个新的已排序列表，原列表不变。"


def _count(path, table: str) -> int:
    con = db.connect(path)
    try:
        return int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        con.close()


def _node(path, skill_id: str = SKILL) -> dict:
    return skilltree.get(path, skill_id)


def _provenance(path, skill_id: str = SKILL) -> list[dict]:
    return json.loads(_node(path, skill_id)["provenance"])


def _source(path, url: str = URL, quote: str = QUOTE, title: str = "Python 排序教程"):
    return learn_loop.record_source(path, SKILL, url, title, quote)


def _new_db(tmp_path):
    return tmp_path / "selflearn.sqlite3"


def test_open_gap_registers_task_and_locked_skill(tmp_path):
    path = _new_db(tmp_path)
    task_id = learn_loop.open_gap(path, SKILL, "写一个排序脚本")

    assert isinstance(task_id, str) and task_id.startswith("LT-")
    state = learn_loop.status(path, SKILL)
    assert state["ok"] is True
    assert state["level"] == 0
    assert state["status"] == "locked"
    assert [task["id"] for task in state["tasks"]] == [task_id]
    assert state["tasks"][0]["goal"] == "写一个排序脚本"
    assert state["tasks"][0]["status"] == "open"
    assert state["tasks"][0]["attempts"] == 0


def test_open_gap_defaults_goal_and_rejects_empty_skill(tmp_path):
    path = _new_db(tmp_path)
    task_id = learn_loop.open_gap(path, SKILL, "   ")
    state = learn_loop.status(path, SKILL)
    assert state["tasks"][0]["id"] == task_id
    assert state["tasks"][0]["goal"]

    assert learn_loop.open_gap(path, "", "无技能") == ""
    assert learn_loop.open_gap(path, "   ", "无技能") == ""
    assert _count(path, "learn_tasks") == 1


def test_open_gap_ids_are_unique_for_repeated_identical_calls(tmp_path):
    path = _new_db(tmp_path)
    ids = {learn_loop.open_gap(path, SKILL, "同一个缺口") for _ in range(20)}
    assert len(ids) == 20
    assert all(task_id.startswith("LT-") for task_id in ids)
    assert _count(path, "learn_tasks") == 20


def test_record_source_validates_url_and_quote(tmp_path):
    path = _new_db(tmp_path)

    assert learn_loop.record_source(path, "", URL, "t", QUOTE)["ok"] is False
    bad = learn_loop.record_source(path, SKILL, "not-a-url", "t", QUOTE)
    assert bad["ok"] is False and bad["code"] == "bad_url"
    ftp = learn_loop.record_source(path, SKILL, "ftp://example.com/x", "t", QUOTE)
    assert ftp["ok"] is False and ftp["code"] == "bad_url"
    no_quote = learn_loop.record_source(path, SKILL, URL, "t", "   ")
    assert no_quote["ok"] is False and no_quote["code"] == "missing_quote"

    assert _provenance(path) == []


def test_record_source_appends_provenance_json_and_is_idempotent(tmp_path):
    path = _new_db(tmp_path)
    first = learn_loop.record_source(
        path, SKILL, URL, "Python 排序教程", QUOTE, fetched_at=1700000000.0
    )
    assert first["ok"] is True and first["duplicate"] is False
    assert first["id"].startswith("SRC-")
    assert first["url"] == URL
    assert first["fetched_at"] == 1700000000.0

    second = _source(path, url="https://example.com/python-list", quote="list 是可变序列。")
    assert second["ok"] is True and second["id"] != first["id"]

    sources = _provenance(path)
    assert len(sources) == 2
    by_url = {source["url"]: source for source in sources}
    assert by_url[URL]["quote"] == QUOTE
    assert by_url[URL]["title"] == "Python 排序教程"
    assert by_url[URL]["fetched_at"] == 1700000000.0
    assert by_url["https://example.com/python-list"]["id"] == second["id"]

    duplicate = learn_loop.record_source(path, SKILL, URL, "另一个标题", QUOTE)
    assert duplicate["ok"] is True and duplicate["duplicate"] is True
    assert duplicate["id"] == first["id"]
    assert len(_provenance(path)) == 2
    assert _node(path)["level"] == 0


def test_record_source_auto_registers_unknown_skill_without_level(tmp_path):
    path = _new_db(tmp_path)
    result = learn_loop.record_source(path, "SK-CUSTOM", URL, "t", QUOTE)
    assert result["ok"] is True
    node = skilltree.get(path, "SK-CUSTOM")
    assert node is not None
    assert node["level"] == 0
    assert node["status"] == "locked"
    assert len(json.loads(node["provenance"])) == 1


def test_write_card_rejects_without_provenance(tmp_path):
    path = _new_db(tmp_path)
    learn_loop.open_gap(path, SKILL, "写排序")

    no_source = learn_loop.write_card(path, SKILL, "sorted 返回新列表", "sorted([3, 1])")
    assert no_source["ok"] is False
    assert no_source["code"] == "missing_provenance"
    empty_ids = learn_loop.write_card(
        path, SKILL, "sorted 返回新列表", "sorted([3, 1])", provenance_ids=[]
    )
    assert empty_ids["ok"] is False
    assert _count(path, "skill_cards") == 0

    source = _source(path)
    unknown = learn_loop.write_card(
        path, SKILL, "sorted 返回新列表", "", provenance_ids=["SRC-not-exist"]
    )
    assert unknown["ok"] is False and unknown["code"] == "unknown_provenance"
    assert unknown["missing"] == ["SRC-not-exist"]
    assert _count(path, "skill_cards") == 0

    bad_source = learn_loop.record_source(path, SKILL, "https://example.com/x", "t", "")
    assert bad_source["ok"] is False
    assert _count(path, "skill_cards") == 0
    assert source["ok"] is True


def test_write_card_stores_provenance_snapshot(tmp_path):
    path = _new_db(tmp_path)
    first = _source(path)
    second = _source(path, url="https://example.com/python-list", quote="list 是可变序列。")

    card = learn_loop.write_card(
        path,
        SKILL,
        "sorted 返回新的已排序列表",
        "sorted([3, 1, 2]) -> [1, 2, 3]",
        provenance_ids=[first["id"]],
    )
    assert card["ok"] is True and card["id"].startswith("SC-")
    assert card["skill_id"] == SKILL
    assert card["example"] == "sorted([3, 1, 2]) -> [1, 2, 3]"
    assert card["provenance_ids"] == [first["id"]]

    con = db.connect(path)
    try:
        row = con.execute(
            "SELECT * FROM skill_cards WHERE id=?", (card["id"],)
        ).fetchone()
    finally:
        con.close()
    stored = json.loads(row["provenance_json"])
    assert len(stored) == 1
    assert stored[0]["url"] == URL
    assert stored[0]["quote"] == QUOTE
    assert row["status"] == "active"

    # 不传 provenance_ids：默认使用该 skill 的全部来源
    all_sources = learn_loop.write_card(path, SKILL, "第二张卡", "")
    assert all_sources["ok"] is True
    assert len(all_sources["provenance"]) == 2
    assert set(all_sources["provenance_ids"]) == {first["id"], second["id"]}

    empty = learn_loop.write_card(path, SKILL, "   ", "例子")
    assert empty["ok"] is False and empty["code"] == "empty_content"
    assert _count(path, "skill_cards") == 2


def test_record_practice_writes_run_and_failure_counts(tmp_path):
    path = _new_db(tmp_path)
    task_id = learn_loop.open_gap(path, SKILL, "写排序")

    failed = learn_loop.record_practice(
        path, task_id, attempt=1, passed=False, error="SyntaxError: bad input"
    )
    assert failed["ok"] is True
    assert failed["attempt"] == 1
    assert failed["stored_attempt"] == "1"
    assert failed["task_status"] == "practicing"
    assert failed["last_error"] == "SyntaxError: bad input"
    assert failed["fail_count"] == 1
    assert failed["practices"] == 1

    con = db.connect(path)
    try:
        row = dict(
            con.execute("SELECT * FROM practice_runs WHERE task_id=?", (task_id,)).fetchone()
        )
    finally:
        con.close()
    assert row["attempt"] == "1"
    assert row["passed"] == 0
    assert row["error"] == "SyntaxError: bad input"
    assert row["id"] == failed["id"]

    state = learn_loop.status(path, SKILL)
    assert state["practice"]["count"] == 1
    assert state["practice"]["failed"] == 1
    assert state["practice"]["last_passed"] is False
    assert state["fail_count"] == 1
    assert state["last_error"] == "SyntaxError: bad input"
    assert state["level"] == 0 and state["status"] == "locked"

    passed = learn_loop.record_practice(path, task_id, attempt=2, passed=True)
    assert passed["ok"] is True
    assert passed["task_status"] == "passed"
    assert passed["last_error"] == ""
    assert passed["fail_count"] == 1  # 失败计数是历史账本，不因一次通过清零
    assert passed["practices"] == 2

    state = learn_loop.status(path, SKILL)
    assert state["last_error"] == ""
    assert state["level"] == 0  # 练习本身绝不改等级
    assert state["tasks"][0]["attempts"] == 2
    assert state["tasks"][0]["practices"] == 2
    assert state["tasks"][0]["passed_practices"] == 1


def test_record_practice_auto_numbers_attempt_and_defaults_error(tmp_path):
    path = _new_db(tmp_path)
    task_id = learn_loop.open_gap(path, SKILL, "写排序")

    first = learn_loop.record_practice(path, task_id, None, False)
    assert first["attempt"] == "1"
    assert first["error"] == "练习未通过"
    assert first["last_error"] == "练习未通过"

    second = learn_loop.record_practice(path, task_id, "", True)
    assert second["attempt"] == "2"
    assert second["error"] == ""


def test_record_practice_and_verify_fail_closed_on_unknown_task(tmp_path):
    path = _new_db(tmp_path)
    practice = learn_loop.record_practice(path, "LT-nope", 1, True)
    assert practice["ok"] is False and practice["code"] == "unknown_task"
    verify = learn_loop.verify_and_unlock(path, "LT-nope", True)
    assert verify["ok"] is False and verify["code"] == "unknown_task"
    assert _count(path, "practice_runs") == 0
    assert _count(path, "learn_tasks") == 0


def test_verify_requires_three_practices_before_unlock(tmp_path):
    path = _new_db(tmp_path)
    task_id = learn_loop.open_gap(path, SKILL, "写排序")
    for attempt in (1, 2):
        assert learn_loop.record_practice(path, task_id, attempt, True)["ok"] is True

    blocked = learn_loop.verify_and_unlock(path, task_id, True, note="自测通过")
    assert blocked["ok"] is False
    assert blocked["unlocked"] is False
    assert blocked["reason"] == "requirements_not_met"
    assert blocked["missing"] == ["practice 2/3"]
    assert blocked["level"] == 0
    assert blocked["practices"] == 2

    node = _node(path)
    assert node["level"] == 0 and node["status"] == "locked"
    state = learn_loop.status(path, SKILL)
    assert state["fail_count"] == 0  # 条件不满足不是学习失败
    assert state["tasks"][0]["last_error"]
    assert state["can_unlock"] is False
    assert "practice 2/3" in state["missing"]

    assert learn_loop.record_practice(path, task_id, 3, True)["ok"] is True
    unlocked = learn_loop.verify_and_unlock(path, task_id, True, note="自测通过")
    assert unlocked["ok"] is True
    assert unlocked["unlocked"] is True
    assert unlocked["level"] == 3
    assert unlocked["previous_level"] == 0
    assert unlocked["practices"] == 3
    assert unlocked["reason"] == "unlocked"

    node = _node(path)
    assert node["level"] == 3
    assert node["status"] == "unlocked"
    state = learn_loop.status(path, SKILL)
    assert state["level"] == 3 and state["unlocked"] is True
    assert state["tasks"][0]["status"] == "done"
    assert state["tasks"][0]["last_error"] == ""
    assert state["verified"] is True
    assert state["practice_ready"] is True
    assert state["can_unlock"] is False
    assert state["missing"] == []


def test_verifier_failure_blocks_unlock_and_records_error(tmp_path):
    path = _new_db(tmp_path)
    task_id = learn_loop.open_gap(path, SKILL, "写排序")
    for attempt in (1, 2, 3):
        learn_loop.record_practice(path, task_id, attempt, True)

    failed = learn_loop.verify_and_unlock(path, task_id, False, note="assert 1 == 2 失败")
    assert failed["ok"] is False
    assert failed["unlocked"] is False
    assert failed["reason"] == "verifier_failed"
    assert failed["error"] == "assert 1 == 2 失败"
    assert failed["fail_count"] == 1

    node = _node(path)
    assert node["level"] == 0 and node["status"] == "locked"
    require = _count(path, "practice_runs")
    assert require == 3  # verifier 不写 practice_runs
    state = learn_loop.status(path, SKILL)
    assert state["fail_count"] == 1
    assert state["last_error"] == "assert 1 == 2 失败"
    assert "verifier" in state["missing"]
    assert state["can_unlock"] is False

    ok = learn_loop.verify_and_unlock(path, task_id, True, note="重跑通过")
    assert ok["ok"] is True and ok["level"] == 3
    assert _node(path)["level"] == 3


def test_failed_last_practice_blocks_unlock_even_with_verifier(tmp_path):
    path = _new_db(tmp_path)
    task_id = learn_loop.open_gap(path, SKILL, "写排序")
    learn_loop.record_practice(path, task_id, 1, True)
    learn_loop.record_practice(path, task_id, 2, True)
    learn_loop.record_practice(path, task_id, 3, False, error="IndexError")

    blocked = learn_loop.verify_and_unlock(path, task_id, True)
    assert blocked["ok"] is False
    assert blocked["reason"] == "requirements_not_met"
    assert blocked["missing"] == ["最后一次或累计练习未通过"]
    assert _node(path)["level"] == 0

    learn_loop.record_practice(path, task_id, 4, True)
    unlocked = learn_loop.verify_and_unlock(path, task_id, True)
    assert unlocked["ok"] is True and unlocked["level"] == 3


def test_cumulative_three_passes_allow_unlock(tmp_path):
    path = _new_db(tmp_path)
    task_id = learn_loop.open_gap(path, SKILL, "写排序")
    for attempt, passed in ((1, True), (2, True), (3, True), (4, False)):
        learn_loop.record_practice(path, task_id, attempt, passed, error="")

    state = learn_loop.status(path, SKILL)
    assert state["practice"]["count"] == 4
    assert state["practice"]["passed"] == 3
    assert state["practice"]["last_passed"] is False

    result = learn_loop.verify_and_unlock(path, task_id, True)
    assert result["ok"] is True
    assert result["level"] == 3
    assert _node(path)["level"] == 3


def test_verify_only_raises_level_never_drops_it(tmp_path):
    path = _new_db(tmp_path)
    task_id = learn_loop.open_gap(path, SKILL, "写排序")
    setup = skilltree.unlock(path, SKILL, 4)
    assert setup["ok"] is True and _node(path)["level"] == 4

    for attempt in (1, 2, 3):
        learn_loop.record_practice(path, task_id, attempt, True)
    result = learn_loop.verify_and_unlock(path, task_id, True)
    assert result["ok"] is True
    assert result["previous_level"] == 4
    assert result["level"] == 4
    assert _node(path)["level"] == 4
    assert _node(path)["status"] == "unlocked"


def test_verifier_attempts_are_not_counted_as_practices(tmp_path):
    path = _new_db(tmp_path)
    task_id = learn_loop.open_gap(path, SKILL, "写排序")

    for _ in range(3):
        result = learn_loop.verify_and_unlock(path, task_id, False, note="未通过")
        assert result["ok"] is False

    state = learn_loop.status(path, SKILL)
    assert state["practice"]["count"] == 0
    assert state["fail_count"] == 3
    assert state["level"] == 0
    assert _count(path, "practice_runs") == 0


def test_status_aggregate_and_unknown_skill(tmp_path):
    path = _new_db(tmp_path)
    empty = learn_loop.status(path)
    assert empty["ok"] is True
    assert [item["skill_id"] for item in empty["skills"]] == ["SK-CODE-HTML", "SK-CODE-PY"]
    assert all(item["level"] == 0 and item["status"] == "locked" for item in empty["skills"])
    assert empty["totals"]["unlocked"] == 0
    assert empty["totals"]["cards"] == 0
    assert empty["totals"]["practices"] == 0

    unknown = learn_loop.status(path, "SK-NOPE")
    assert unknown["ok"] is False and unknown["code"] == "unknown_skill"

    task_id = learn_loop.open_gap(path, SKILL, "写排序")
    _source(path)
    learn_loop.write_card(path, SKILL, "sorted 返回新列表", "sorted([3, 1])")
    for attempt in (1, 2, 3):
        learn_loop.record_practice(path, task_id, attempt, True)
    learn_loop.verify_and_unlock(path, task_id, True)

    filled = learn_loop.status(path)
    assert filled["totals"]["unlocked"] == 1
    assert filled["totals"]["sources"] == 1
    assert filled["totals"]["cards"] == 1
    assert filled["totals"]["practices"] == 3
    assert filled["totals"]["tasks"] == 1
    by_id = {item["skill_id"]: item for item in filled["skills"]}
    assert by_id[SKILL]["level"] == 3
    assert by_id["SK-CODE-HTML"]["level"] == 0


def test_record_source_keeps_string_fetched_at(tmp_path):
    path = _new_db(tmp_path)
    result = learn_loop.record_source(
        path, SKILL, URL, "t", QUOTE, fetched_at="2024-01-01T00:00:00Z"
    )
    assert result["ok"] is True
    assert result["fetched_at"] == "2024-01-01T00:00:00Z"
    assert _provenance(path)[0]["fetched_at"] == "2024-01-01T00:00:00Z"


def test_status_handles_legacy_provenance_and_corrupt_json(tmp_path):
    path = _new_db(tmp_path)
    learn_loop.open_gap(path, SKILL, "写排序")

    # 早期来源没有 id：status 必须能读，write_card 自动补确定性 id
    con = db.connect(path)
    try:
        con.execute(
            "UPDATE skill_nodes SET provenance=? WHERE id=?",
            (json.dumps([{"url": URL, "title": "老来源", "quote": QUOTE}]), SKILL),
        )
        con.commit()
    finally:
        con.close()

    state = learn_loop.status(path, SKILL)
    assert state["source_count"] == 1
    legacy_id = state["sources"][0]["id"]
    assert legacy_id.startswith("SRC-")
    card = learn_loop.write_card(path, SKILL, "旧卡", "", provenance_ids=legacy_id)
    assert card["ok"] is True
    assert card["provenance_ids"] == [legacy_id]

    # 坏 JSON 读取按空处理：制卡 fail-closed，再 record_source 可恢复
    con = db.connect(path)
    try:
        con.execute(
            "UPDATE skill_nodes SET provenance=? WHERE id=?", ("not-json", SKILL)
        )
        con.commit()
    finally:
        con.close()
    assert learn_loop.status(path, SKILL)["source_count"] == 0
    blocked = learn_loop.write_card(path, SKILL, "无来源卡", "")
    assert blocked["ok"] is False and blocked["code"] == "missing_provenance"
    recovered = _source(path)
    assert recovered["ok"] is True
    assert learn_loop.status(path, SKILL)["source_count"] == 1


def test_status_is_json_serializable(tmp_path):
    path = _new_db(tmp_path)
    task_id = learn_loop.open_gap(path, SKILL, "写排序")
    _source(path)
    learn_loop.write_card(path, SKILL, "sorted 返回新列表", "sorted([3, 1])")
    for attempt in (1, 2, 3):
        learn_loop.record_practice(path, task_id, attempt, True)
    learn_loop.verify_and_unlock(path, task_id, True)

    single = learn_loop.status(path, SKILL)
    aggregate = learn_loop.status(path)
    assert json.loads(json.dumps(single, ensure_ascii=False))["level"] == 3
    assert json.loads(json.dumps(aggregate, ensure_ascii=False))["totals"]["cards"] == 1


def test_full_loop_end_to_end(tmp_path):
    path = _new_db(tmp_path)
    task_id = learn_loop.open_gap(path, SKILL, "写一个排序脚本")
    first = _source(path)
    second = _source(path, url="https://example.com/python-list", quote="list 是可变序列。")
    card = learn_loop.write_card(
        path,
        SKILL,
        "sorted() 不改原列表",
        "sorted([3, 1])",
        provenance_ids=[first["id"], second["id"]],
    )
    assert card["ok"] is True

    learn_loop.record_practice(path, task_id, 1, False, error="NameError: sorted")
    learn_loop.record_practice(path, task_id, 2, True)
    learn_loop.record_practice(path, task_id, 3, True)

    result = learn_loop.verify_and_unlock(path, task_id, True, note="本地 fake 断言通过")
    assert result["ok"] is True
    assert result["unlocked"] is True
    assert result["level"] == 3
    assert result["fail_count"] == 1
    assert result["practices"] == 3
    assert result["passed_practices"] == 2

    state = learn_loop.status(path, SKILL)
    assert state["source_count"] == 2
    assert state["card_count"] == 1
    assert state["cards"][0]["provenance_ids"] == [first["id"], second["id"]]
    assert state["practice"]["count"] == 3
    assert state["practice"]["passed"] == 2
    assert state["practice"]["failed"] == 1
    assert state["last_error"] == ""
    assert state["level"] == 3 and state["status"] == "unlocked"
    assert state["fail_count"] == 1
