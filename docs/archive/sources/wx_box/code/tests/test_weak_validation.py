# -*- coding: utf-8 -*-
"""13 弱验证复测：计划、审计、失败标记回退，但不直接改技能等级。"""
from __future__ import annotations

from agi.selflearn import db, skills, weak_validation as wv


def _rows(path, sql: str, params=()):
    con = db.connect(path)
    try:
        return [dict(row) for row in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _count(path, table: str) -> int:
    return int(_rows(path, f"SELECT count(*) n FROM {table}")[0]["n"])


def _seed_skill(path, level: int = 3, skill_id: str = skills.SKILL_ID) -> None:
    """先建默认技能，再把等级调到指定值（ensure_defaults 会把空样本库清成 0）。"""
    skills.ensure_defaults(path)
    con = db.connect(path)
    try:
        con.execute("UPDATE skills SET level=? WHERE id=?", (level, skill_id))
        con.commit()
    finally:
        con.close()


def test_schedule_recheck_persists_and_does_not_duplicate(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    first = wv.schedule_recheck(path, "S-CONV-001", "灰度样本还少", now=1000.0)
    assert first["ok"] is True
    assert first["created"] is True
    assert first["already_scheduled"] is False
    assert first["due"] is False
    assert first["due_at"] == 1000.0 + wv.RECHECK_INTERVAL_SECONDS
    assert first["schedule_id"].startswith("WRS-")
    assert first["schedule"]["status"] == "pending"
    assert first["level"] == 0 and first["checks"] == 0

    again = wv.schedule_recheck(path, "S-CONV-001", "重复排", now=1010.0)
    assert again["ok"] is True
    assert again["created"] is False
    assert again["already_scheduled"] is True
    assert again["schedule_id"] == first["schedule_id"]
    assert again["due_at"] == first["due_at"]
    assert again["reason"] == "灰度样本还少"

    st = wv.status(path, now=1020.0)
    assert st["ok"] is True
    assert st["scheduled"] == 1
    assert st["counts"]["scheduled"] == 1
    assert st["schedules"][0]["skill_id"] == "S-CONV-001"
    assert st["schedules"][0]["due"] is False
    assert st["skills"][0]["recommended_action"] == "wait"


def test_schedule_recheck_becomes_due_and_reports_overdue(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    wv.schedule_recheck(path, "S-1", "跨时间复测", now=1000.0)

    due_at = 1000.0 + wv.RECHECK_INTERVAL_SECONDS
    st = wv.status(path, now=due_at + 7.5)
    assert st["due_count"] == 1
    assert st["due"][0]["due"] is True
    assert st["due"][0]["overdue_seconds"] == 7.5
    assert st["skills"][0]["due"] is True
    assert st["skills"][0]["recommended_action"] == "recheck"

    before = wv.status(path, now=due_at - 0.5)
    assert before["due_count"] == 0


def test_schedule_and_run_reject_empty_skill_id(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    assert wv.schedule_recheck(path, "   ")["ok"] is False
    assert wv.run_recheck(path, "", True)["ok"] is False
    assert _count(path, "weak_rechecks") == 0
    assert wv.status(path)["counts"]["scheduled"] == 0


def test_run_recheck_writes_weak_rechecks_without_touching_level(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_skill(path, level=3)

    result = wv.run_recheck(path, skills.SKILL_ID, True, "复测通过", now=2000.0)

    assert result["ok"] is True
    assert result["recheck_id"].startswith("WR-")
    assert result["passed"] is True
    assert result["level_before"] == 3
    assert result["level_after"] == 3
    assert result["level_changed"] is False
    assert result["needs_rollback"] is False
    assert result["created_at"] == 2000.0

    rows = _rows(path, "SELECT * FROM weak_rechecks")
    assert len(rows) == 1
    assert rows[0]["id"] == result["recheck_id"]
    assert rows[0]["skill_id"] == skills.SKILL_ID
    assert rows[0]["passed"] == 1
    assert rows[0]["note"] == "复测通过"
    assert rows[0]["created_at"] == 2000.0

    level = _rows(path, "SELECT level FROM skills WHERE id=?", (skills.SKILL_ID,))
    assert level[0]["level"] == 3
    # 复测不写 weak_checks，也不挪动现有训练样本
    assert _count(path, "weak_checks") == 0


def test_failed_recheck_marks_rollback_but_keeps_level(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_skill(path, level=4)

    result = wv.run_recheck(path, skills.SKILL_ID, False, "用户反馈变差", now=3000.0)

    assert result["ok"] is True
    assert result["passed"] is False
    assert result["needs_rollback"] is True
    assert result["level_before"] == 4
    assert result["level_after"] == 4
    assert result["level_changed"] is False
    assert result["rollback"]["needed"] is True
    assert result["rollback"]["recheck_id"] == result["recheck_id"]
    assert result["consecutive_failures"] == 1
    assert "未直接改 level" in result["note_for_review"]

    stored = _rows(path, "SELECT level FROM skills WHERE id=?", (skills.SKILL_ID,))
    assert stored[0]["level"] == 4

    st = wv.status(path, now=3000.0)
    assert st["needs_rollback"] is True
    assert st["counts"]["rollback"] == 1
    assert st["rollback_needed"][0]["skill_id"] == skills.SKILL_ID
    assert st["rollback_needed"][0]["level"] == 4
    assert st["rollback_needed"][0]["recommended_action"] == "rollback_review"
    assert st["rollback_needed"][0]["last_passed"] is False


def test_passing_recheck_clears_rollback_flag(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_skill(path, level=2)

    wv.run_recheck(path, skills.SKILL_ID, False, "第一次失败", now=1000.0)
    cleared = wv.run_recheck(path, skills.SKILL_ID, True, "复测正常", now=2000.0)

    assert cleared["rollback_cleared"] is True
    assert cleared["needs_rollback"] is False
    assert cleared["consecutive_failures"] == 0

    st = wv.status(path, now=2000.0)
    assert st["needs_rollback"] is False
    assert st["rollback_needed"] == []
    assert st["skills"][0]["rechecks"] == 2
    assert st["skills"][0]["rechecks_passed"] == 1
    assert st["skills"][0]["pass_rate"] == 0.5


def test_consecutive_failures_and_pass_rate(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_skill(path, level=1)

    for index, passed in enumerate((False, False, True, False)):
        result = wv.run_recheck(
            path, skills.SKILL_ID, passed, f"第 {index} 次", now=1000.0 + index)

    assert result["consecutive_failures"] == 1
    assert result["rechecks"] == 4
    assert result["rechecks_passed"] == 1
    assert result["pass_rate"] == 0.25
    assert result["needs_rollback"] is True

    st = wv.status(path, now=1000.0)
    assert st["skills"][0]["consecutive_failures"] == 1


def test_recheck_uses_existing_variant_when_available(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    skills.ensure_defaults(path)
    created = skills.add_dynamic_variant(path, "先接住情绪再给建议")
    assert created["ok"] is True

    auto = wv.run_recheck(path, skills.SKILL_ID, True, now=1000.0)
    assert auto["variant_id"] == created["id"]
    assert _rows(path, "SELECT variant_id FROM weak_rechecks")[0]["variant_id"] == created["id"]

    explicit = wv.run_recheck(
        path, skills.SKILL_ID, False, "指定变体", variant_id="V-CUSTOM", now=2000.0)
    assert explicit["variant_id"] == "V-CUSTOM"
    rows = _rows(path, "SELECT variant_id FROM weak_rechecks ORDER BY created_at")
    assert [row["variant_id"] for row in rows] == [created["id"], "V-CUSTOM"]


def test_run_recheck_after_schedule_closes_schedule(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    scheduled = wv.schedule_recheck(path, "S-1", "复测原因", now=1000.0)

    result = wv.run_recheck(path, "S-1", True, "通过", now=1500.0)
    assert result["scheduled"] is True
    assert result["schedule"]["status"] == "done"
    assert result["schedule"]["recheck_id"] == result["recheck_id"]
    assert result["schedule"]["completed_at"] == 1500.0
    assert result["schedule"]["due"] is False

    nxt = wv.schedule_recheck(path, "S-1", "再排一次", now=2000.0)
    assert nxt["created"] is True
    assert nxt["schedule_id"] != scheduled["schedule_id"]
    assert nxt["due_at"] == 2000.0 + wv.RECHECK_INTERVAL_SECONDS
    assert nxt["due"] is False


def test_status_blank_db_and_custom_skill_without_skills_row(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    blank = wv.status(path)
    assert blank["ok"] is True
    assert blank["skills"] == []
    assert blank["schedules"] == []
    assert blank["needs_rollback"] is False
    assert blank["counts"] == {
        "skills": 0, "rechecks": 0, "scheduled": 0,
        "due": 0, "rollback": 0, "weak_checks": 0,
    }
    assert blank["last_recheck_at"] is None

    wv.run_recheck(path, "S-NOT-IN-SKILLS", False, "没有技能行也要能审计", now=500.0)
    st = wv.status(path, now=500.0)
    assert len(st["skills"]) == 1
    item = st["skills"][0]
    assert item["skill_id"] == "S-NOT-IN-SKILLS"
    assert item["level"] == 0
    assert item["rechecks"] == 1
    assert item["needs_rollback"] is True
    assert st["total_rechecks"] == 1
    assert st["last_recheck_at"] == 500.0


def test_string_passed_values_and_note_is_truncated(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    yes = wv.run_recheck(path, "S-1", "yes", now=1000.0)
    no = wv.run_recheck(path, "S-1", "0", now=1001.0)
    assert yes["passed"] is True
    assert no["passed"] is False

    wv.run_recheck(path, "S-1", True, "x" * 500, now=1002.0)
    last = _rows(
        path, "SELECT note FROM weak_rechecks ORDER BY created_at DESC, rowid DESC LIMIT 1")
    assert len(last[0]["note"]) == wv.NOTE_LIMIT


def test_rechecks_do_not_modify_variants_or_weak_checks(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_skill(path, level=2)
    variants_before = _rows(path, "SELECT * FROM variants ORDER BY id")
    weak_checks_before = _count(path, "weak_checks")

    wv.run_recheck(path, skills.SKILL_ID, True, now=1000.0)
    wv.run_recheck(path, skills.SKILL_ID, False, now=2000.0)

    assert _rows(path, "SELECT * FROM variants ORDER BY id") == variants_before
    assert _count(path, "weak_checks") == weak_checks_before
    assert _rows(path, "SELECT level FROM skills WHERE id=?", (skills.SKILL_ID,))[0]["level"] == 2
