# -*- coding: utf-8 -*-
"""主动行为每日预算测试。"""
from __future__ import annotations

import time

from agi.selflearn import db, proactive


def test_proactive_daily_budget_blocks_after_limit(tmp_path):
    path = tmp_path / "p.sqlite3"
    con = db.connect(path)
    now = time.time()
    for i in range(proactive.DAILY_BUDGET):
        con.execute("INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (f"P-{i}", "check_in", "在吗", "test", 0.8, "spoken", now - 1000 - i * 60))
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-1", "user", "hi", now - 3600, 0))
    con.commit(); con.close()
    r = proactive.tick(path, client=None, now=now + 10)
    assert r["available"] is False
    assert r["reason"] == "今日主动预算已用完"
