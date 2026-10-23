# -*- coding: utf-8 -*-
"""14 按需唤醒：优先级、预算、去重、wake_runs 审计，不自动发微信。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agi.selflearn import db, scheduler, wake


def _rows(path, sql: str, params=()):
    con = db.connect(path)
    try:
        return [dict(row) for row in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _count(path, table: str) -> int:
    return int(_rows(path, f"SELECT count(*) n FROM {table}")[0]["n"])


def test_add_persists_pending_intent_with_priority(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    intent_id = wake.add(
        path, "follow_up", "问问面试结果", 1000.0,
        priority=0.9, reason="用户周三面试")

    assert isinstance(intent_id, str)
    assert intent_id.startswith("WK-")
    rows = _rows(path, "SELECT * FROM intents")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == intent_id
    assert row["kind"] == "follow_up"
    assert row["text"] == "问问面试结果"
    assert row["wake_at"] == 1000.0
    assert row["status"] == "pending"
    assert row["reason"] == "用户周三面试"
    assert row["priority"] == 0.9
    assert row["created_at"] > 0


def test_add_dedupes_pending_and_merges_priority_and_earlier_time(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    first = wake.add(path, "follow_up", "追问面试", 2000.0, 0.4, "旧原因")
    again = wake.add(path, "follow_up", "追问面试", 1000.0, 0.8, "新原因")

    assert again == first
    rows = _rows(path, "SELECT * FROM intents")
    assert len(rows) == 1
    assert rows[0]["wake_at"] == 1000.0
    assert rows[0]["priority"] == 0.8
    assert rows[0]["reason"] == "旧原因"

    # 归一化空白后仍算同一条待办
    a = wake.add(path, "check", "  同一个   任务  ", 3000.0)
    b = wake.add(path, "check", "同一个 任务", 2500.0)
    assert a == b
    assert _count(path, "intents") == 2

    # 不同 kind 不合并
    other = wake.add(path, "share", "同一个 任务", 2500.0)
    assert other != a
    assert _count(path, "intents") == 3


def test_add_allows_same_text_again_after_previous_is_done(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    first = wake.add(path, "check", "回访", 1000.0)
    wake.run_due(path, now=1000.0)
    second = wake.add(path, "check", "回访", 2000.0)

    assert second != first
    rows = _rows(path, "SELECT id,status FROM intents ORDER BY created_at, id")
    assert [(row["id"], row["status"]) for row in rows] == [
        (first, "done"), (second, "pending")]


@pytest.mark.parametrize("kwargs", [
    {"kind": "", "text": "x", "wake_at": 1.0},
    {"kind": "check", "text": "   ", "wake_at": 1.0},
    {"kind": "check", "text": "x", "wake_at": "not-a-time"},
    {"kind": "check", "text": "x", "wake_at": float("nan")},
    {"kind": "check", "text": "x", "wake_at": float("inf")},
    {"kind": "check", "text": "x", "wake_at": 1.0, "priority": "high"},
])
def test_add_rejects_invalid_input(tmp_path, kwargs):
    path = tmp_path / "selflearn.sqlite3"
    with pytest.raises(ValueError):
        wake.add(path, **kwargs)
    assert _count(path, "intents") == 0


def test_add_clamps_priority_to_zero_one(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    wake.add(path, "check", "太高", 1000.0, priority=2.0)
    wake.add(path, "check", "太低", 1000.0, priority=-1.0)
    rows = _rows(path, "SELECT text,priority FROM intents ORDER BY text")
    assert [(row["text"], row["priority"]) for row in rows] == [
        ("太低", 0.0), ("太高", 1.0)]


def test_due_orders_by_priority_then_time_and_respects_budget(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    ids = {}
    for text, priority, wake_at in (
        ("低", 0.2, 900.0),
        ("最高", 0.9, 950.0),
        ("中", 0.5, 800.0),
        ("次高", 0.7, 990.0),
    ):
        ids[text] = wake.add(path, "check", text, wake_at, priority)

    result = wake.due(path, now=1000.0, budget=2)

    assert result["ok"] is True
    assert result["budget"] == 2
    assert result["total_due"] == 4
    assert result["selected_count"] == 2
    assert [item["text"] for item in result["items"]] == ["最高", "次高"]
    assert [item["id"] for item in result["items"]] == [ids["最高"], ids["次高"]]
    assert [item["id"] for item in result["deferred"]] == [ids["中"], ids["低"]]
    assert result["counts"]["due"] == 4
    assert result["counts"]["selected"] == 2
    assert result["counts"]["deferred"] == 2
    assert result["items"][0]["late_seconds"] == 50.0


def test_due_does_not_mutate_status_or_write_wake_runs(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    intent_id = wake.add(path, "check", "只看不动", 1000.0)

    result = wake.due(path, now=1000.0)

    assert [item["id"] for item in result["items"]] == [intent_id]
    assert _rows(path, "SELECT status FROM intents")[0]["status"] == "pending"
    assert _count(path, "wake_runs") == 0
    assert result["auto_send"] is False


def test_due_excludes_future_and_other_statuses(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    wake.add(path, "check", "未来", 2000.0)

    assert wake.due(path, now=1999.0)["total_due"] == 0
    assert wake.due(path, now=2000.0)["total_due"] == 1
    assert _count(path, "wake_runs") == 0


def test_run_due_writes_audit_marks_done_and_never_sends(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    intent_id = wake.add(
        path, "follow_up", "问问面试结果", 1000.0, 0.8, "承诺到期")

    result = wake.run_due(path, now=1500.0, budget=1)

    assert result["ok"] is True
    assert result["processed"] == 1
    assert result["count"] == 1
    assert result["sent"] is False
    assert result["auto_send"] is False
    assert result["ids"] == [intent_id]
    assert [row["id"] for row in result["intents"]] == [intent_id]

    runs = _rows(path, "SELECT * FROM wake_runs")
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "done"
    assert run["kind"] == "follow_up"
    assert run["started_at"] == 1500.0
    assert run["finished_at"] == 1500.0
    detail = json.loads(run["detail_json"])
    assert detail["intent_id"] == intent_id
    assert detail["text"] == "问问面试结果"
    assert detail["reason"] == "承诺到期"
    assert detail["priority"] == 0.8
    assert detail["late_seconds"] == 500.0
    assert detail["auto_send"] is False
    assert detail["merged_intent_ids"] == []

    assert _rows(path, "SELECT status FROM intents")[0]["status"] == "done"
    assert wake.due(path, now=1500.0)["total_due"] == 0
    assert wake.run_due(path, now=1500.0)["processed"] == 0


def test_run_due_budget_leaves_remainder_pending(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    ids = [wake.add(path, "check", text, 1000.0, priority)
           for text, priority in (("A", 0.9), ("B", 0.8), ("C", 0.7))]

    first = wake.run_due(path, now=1000.0, budget=2)
    assert [row["id"] for row in first["intents"]] == [ids[0], ids[1]]
    assert first["processed"] == 2
    assert [row["id"] for row in first["deferred"]] == [ids[2]]

    statuses = {row["id"]: row["status"]
                for row in _rows(path, "SELECT id,status FROM intents")}
    assert statuses == {ids[0]: "done", ids[1]: "done", ids[2]: "pending"}

    second = wake.run_due(path, now=1000.0, budget=5)
    assert second["processed"] == 1
    assert second["ids"] == [ids[2]]
    assert _count(path, "wake_runs") == 3


def test_run_due_empty_queue_is_ok(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    result = wake.run_due(path, now=1000.0)

    assert result["ok"] is True
    assert result["processed"] == 0
    assert result["runs"] == []
    assert result["sent"] is False
    assert _count(path, "wake_runs") == 0


def test_daily_budget_caps_runs_and_next_day_resumes(tmp_path, monkeypatch):
    path = tmp_path / "selflearn.sqlite3"
    monkeypatch.setattr(wake, "DAILY_WAKE_BUDGET", 1)
    wake.add(path, "check", "第一条", 1000.0, 0.9)
    wake.add(path, "check", "第二条", 1000.0, 0.8)

    first = wake.run_due(path, now=1000.0)
    assert first["processed"] == 1
    assert first["daily"]["used"] == 0
    assert first["daily"]["used_after"] == 1
    assert first["daily"]["remaining_after"] == 0

    blocked = wake.run_due(path, now=1100.0)
    assert blocked["processed"] == 0
    assert len(blocked["budget_blocked"]) == 1
    assert blocked["daily"]["remaining"] == 0

    next_day = wake.run_due(path, now=1100.0 + 24 * 3600)
    assert next_day["processed"] == 1
    assert next_day["intents"][0]["text"] == "第二条"


def test_legacy_scheduler_intent_is_visible_and_runnable(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    intent_id = scheduler.add(path, "check", "旧调度器排的", 1000.0, "旧原因")

    result = wake.due(path, now=1000.0)
    assert result["total_due"] == 1
    item = result["items"][0]
    assert item["id"] == intent_id
    assert item["priority"] == 0.5

    run = wake.run_due(path, now=1000.0)
    assert run["processed"] == 1
    assert scheduler.due(path, now=1000.0) == []
    assert _count(path, "wake_runs") == 1


def test_duplicate_legacy_rows_are_merged_and_dropped_on_run(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    wake.due(path, now=0.0)  # 先确保 priority 列存在
    con = db.connect(path)
    try:
        for intent_id, priority in (("I-1", 0.3), ("I-2", 0.9)):
            con.execute(
                "INSERT INTO intents(id,kind,text,wake_at,status,reason,created_at,priority) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (intent_id, "check", "重复待办", 1000.0, "pending", "", 900.0, priority),
            )
        con.commit()
    finally:
        con.close()

    result = wake.due(path, now=1000.0)
    assert result["total_due"] == 1
    assert result["items"][0]["id"] == "I-2"
    assert result["items"][0]["merged_ids"] == ["I-1"]
    assert result["duplicates"][0]["id"] == "I-1"
    assert result["duplicates"][0]["merged_into"] == "I-2"

    run = wake.run_due(path, now=1000.0)
    assert run["processed"] == 1
    assert run["ids"] == ["I-2"]
    assert run["dropped_duplicates"] == ["I-1"]
    assert run["runs"][0]["detail"]["merged_intent_ids"] == ["I-1"]
    statuses = {row["id"]: row["status"]
                for row in _rows(path, "SELECT id,status FROM intents")}
    assert statuses == {"I-1": "dropped", "I-2": "done"}


def test_reason_is_truncated_and_module_has_no_wechat_or_network_code(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    wake.add(path, "check", "x", 1000.0, reason="r" * 500)
    assert len(_rows(path, "SELECT reason FROM intents")[0]["reason"]) == wake.REASON_LIMIT

    source = Path(wake.__file__).read_text(encoding="utf-8")
    for token in ("wxauto", "requests", "urllib", "http://", "https://"):
        assert token not in source
