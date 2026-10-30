# -*- coding: utf-8 -*-
"""夜间 runner 测试：不联网，只验证编排。"""
from __future__ import annotations

import time

from agi.selflearn import db, nightly, skills, weak_validation


class FakeClient:
    def available(self):
        return True

    def chat(self, messages, **kwargs):
        return {"ok": True, "text": "{}"}


def test_nightly_no_client_still_reports(tmp_path):
    path = tmp_path / "n.sqlite3"
    db.connect(path).close()
    r = nightly.run(path, None)
    assert r["ok"] is True and r["auto_send"] is False
    assert r["steps"]["summary"]["skipped"] == "no_client"


def test_nightly_with_fake_client_does_not_raise(tmp_path):
    path = tmp_path / "n2.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-1", "user", "我喜欢咖啡", time.time(), 0))
    con.commit(); con.close()
    r = nightly.run(path, FakeClient(), force=True)
    assert r["auto_send"] is False
    assert "steps" in r and "reflect" in r["steps"]


def test_nightly_rebuilds_sessions(tmp_path):
    path = tmp_path / "n3.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO raw_events(id,source,source_id,friend,sender,kind,text,ts,meta_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("RE-1", "live", "M-1", "selflearn", "user", "text", "你好", time.time(), "{}"))
    con.commit(); con.close()
    r = nightly.run(path, None)
    assert r["steps"]["sessions"]["sessions"] >= 1
    assert r["steps"]["sessions"]["segments"] >= 1


# --------------------------------------------------------------------------- #
# C4：nightly 输出 weak_due（只提醒/报表，不 run_recheck、不降级）
# --------------------------------------------------------------------------- #
def test_nightly_reports_weak_due_without_changing_level(tmp_path):
    path = tmp_path / "n4.sqlite3"
    skills.ensure_defaults(path)
    con = db.connect(path)
    con.execute("UPDATE skills SET level=? WHERE id=?", (3, skills.SKILL_ID))
    con.commit(); con.close()
    # now=0 排的计划在真实 now（nightly 内部用 time.time()）已经到期
    weak_validation.schedule_recheck(path, skills.SKILL_ID, "到期复测", now=0.0)

    result = nightly.run(path, None)

    assert result["steps"]["weak_due"]["ok"] is True
    assert result["steps"]["weak_due"]["count"] == 1
    due_item = result["steps"]["weak_due"]["items"][0]
    assert due_item["skill_id"] == skills.SKILL_ID
    assert due_item["level"] == 3
    assert due_item["reason"] == "到期复测"

    # 只提醒：不写复测记录、不改技能等级
    con = db.connect(path)
    try:
        rechecks = int(con.execute(
            "SELECT count(*) FROM weak_rechecks").fetchone()[0])
        level = int(con.execute(
            "SELECT level FROM skills WHERE id=?", (skills.SKILL_ID,)).fetchone()[0])
    finally:
        con.close()
    assert rechecks == 0
    assert level == 3
