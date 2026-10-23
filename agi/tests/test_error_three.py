# -*- coding: utf-8 -*-
"""三次法则（case → rule → guard）测试：门槛、去重、无规则、失败不抛异常。"""
from __future__ import annotations

from agi.selflearn import db, error_three


def _count(path, sql: str, args: tuple = ()) -> int:
    con = db.connect(path)
    try:
        return int(con.execute(sql, args).fetchone()[0])
    finally:
        con.close()


def test_record_case_writes_case(tmp_path):
    p = tmp_path / "case.sqlite3"
    r = error_three.record_case(p, "T-1", "too_long", "回复 400 字，超长",
                                root_cause="没有长度检查")
    assert r["ok"] is True and r["reused"] is False
    assert r["id"].startswith("ERR-")
    assert r["category"] == "too_long" and r["root_cause"] == "没有长度检查"
    assert r["hit_count"] == 1 and r["case_count"] == 1 and r["occurrences"] == 1
    assert r["promotable"] is False
    assert _count(p, "SELECT COUNT(*) FROM errors") == 1


def test_record_case_dedupes_same_turn(tmp_path):
    p = tmp_path / "dedupe.sqlite3"
    first = error_three.record_case(p, "T-1", "pushy", "你必须马上照做")
    again = error_three.record_case(p, "T-1", "pushy", "你必须马上照做",
                                    root_cause="advice_style")
    assert again["ok"] is True and again["reused"] is True
    assert again["id"] == first["id"]
    assert again["hit_count"] == 2
    assert again["root_cause"] == "advice_style"      # 重复上报补齐归因
    assert again["case_count"] == 1                   # 同一轮仍只算一个 case
    assert again["occurrences"] == 2
    assert again["promotable"] is False
    assert _count(p, "SELECT COUNT(*) FROM errors") == 1
    # 同一事故再上报第 3 次：只累加 hit_count，不能凑够三次门槛
    third = error_three.record_case(p, "T-1", "pushy", "你必须马上照做")
    assert third["reused"] is True and third["hit_count"] == 3
    assert third["case_count"] == 1 and third["promotable"] is False
    assert error_three.promote_rule(p, "pushy", "不要说“你必须”", ["E-1"])["ok"] is False
    # 换一轮再犯同类错误 → 新 case，但不会覆盖原 case
    other = error_three.record_case(p, "T-2", "pushy", "你必须马上照做")
    assert other["reused"] is False and other["case_count"] == 2
    assert _count(p, "SELECT COUNT(*) FROM errors") == 2


def test_promote_requires_three_cases(tmp_path):
    p = tmp_path / "three.sqlite3"
    for i in range(2):
        r = error_three.record_case(p, f"T-{i}", "too_long", f"第 {i} 次超长")
        assert r["ok"] is True and r["promotable"] is False

    denied = error_three.promote_rule(p, "too_long", "回复先压到 1-3 句", ["E-1", "E-2"])
    assert denied["ok"] is False and denied["promoted"] is False
    assert denied["case_count"] == 2 and denied["threshold"] == error_three.THRESHOLD
    assert denied["error"]
    assert error_three.guard_for(p, "too_long") == ""
    assert _count(p, "SELECT COUNT(*) FROM lessons") == 0

    third = error_three.record_case(p, "T-2", "too_long", "第 2 次超长")
    assert third["promotable"] is True

    ok = error_three.promote_rule(p, "too_long", "回复先压到 1-3 句",
                                  ["E-1", "E-2", "E-3"])
    assert ok["ok"] is True and ok["promoted"] is True and ok["rule_id"].startswith("RULE-")
    assert ok["case_count"] == 3
    assert error_three.guard_for(p, "too_long") == "回复先压到 1-3 句"


def test_guard_for_empty_without_rule(tmp_path):
    p = tmp_path / "norule.sqlite3"
    assert error_three.guard_for(p, "too_long") == ""
    assert error_three.guard_for(p, "never_seen") == ""
    assert error_three.guard_for(p, "") == ""
    # 只有 case、没升级 rule 时仍然不注入
    error_three.record_case(p, "T-1", "too_long", "超长")
    assert error_three.guard_for(p, "too_long") == ""


def test_promote_rule_is_idempotent_per_category(tmp_path):
    p = tmp_path / "replace.sqlite3"
    for i in range(3):
        error_three.record_case(p, f"T-{i}", "pushy", f"你必须第 {i} 次")
    a = error_three.promote_rule(p, "pushy", "不要说“你必须”", ["E-1", "E-2", "E-3"])
    b = error_three.promote_rule(p, "PUSHY", "改成建议句", ["E-1", "E-2", "E-3"])
    assert a["ok"] is True and b["ok"] is True
    assert a["rule_id"] == b["rule_id"]
    assert b["category"] == "pushy"                  # category 大小写归一
    assert _count(p, "SELECT COUNT(*) FROM lessons WHERE id LIKE 'RULE-%'") == 1
    assert error_three.guard_for(p, "PUSHY") == "改成建议句"


def test_duplicate_raw_rows_do_not_fake_threshold(tmp_path):
    """errors.record 等路径留下的重复行，也只算一个 case。"""
    p = tmp_path / "rawdup.sqlite3"
    con = db.connect(p)
    for i in range(3):
        con.execute(
            "INSERT INTO errors(id,turn_id,category,detail,created_at,"
            "root_cause,fix_verified,hit_count) VALUES(?,?,?,?,?,?,?,?)",
            (f"E-{i}", "T-1", "too_long", "同一次超长", 1000 + i, "", 0, 1))
    con.commit()
    con.close()
    out = error_three.promote_rule(p, "too_long", "压短", [])
    assert out["ok"] is False and out["promoted"] is False
    assert out["case_count"] == 1
    assert error_three.guard_for(p, "too_long") == ""


def test_promote_rule_auto_evidence(tmp_path):
    p = tmp_path / "evidence.sqlite3"
    for i in range(3):
        error_three.record_case(p, f"T-{i}", "too_long", f"超长 {i}")
    out = error_three.promote_rule(p, "too_long", "压短", None)
    assert out["ok"] is True
    assert len(out["evidence"]) == 3
    assert all(e.startswith("ERR-") for e in out["evidence"])
    assert _count(p, "SELECT COUNT(*) FROM errors") == 3


def test_failures_return_dict_and_never_raise(tmp_path):
    p = tmp_path / "bad.sqlite3"
    empty_cat = error_three.record_case(p, "T-1", "  ", "x")
    assert empty_cat["ok"] is False and empty_cat["error"]
    empty_detail = error_three.record_case(p, "T-1", "too_long", "   ")
    assert empty_detail["ok"] is False and empty_detail["error"]
    assert error_three.promote_rule(p, "", "rule", [])["ok"] is False
    assert error_three.promote_rule(p, "too_long", "   ", [])["ok"] is False

    # 路径不可用（把目录当 DB）时也不抛异常
    broken = tmp_path / "dir_as_db"
    broken.mkdir()
    assert error_three.record_case(broken, "T-1", "too_long", "x")["ok"] is False
    assert error_three.promote_rule(broken, "too_long", "rule", [])["ok"] is False
    assert error_three.guard_for(broken, "too_long") == ""
