# -*- coding: utf-8 -*-
"""guard 引用/数字校验测试。"""
from __future__ import annotations

import time

from agi.selflearn import db, guard


def test_number_must_be_in_context(tmp_path):
    p = tmp_path / "g.sqlite3"
    bad = guard.check("我昨天买了 3 个苹果", p, context="我买了苹果")
    assert bad["level"] == "L1" and any(i.startswith("number_not_in_context") for i in bad["issues"])
    good = guard.check("我昨天买了 3 个苹果", p, context="买了 3 个苹果")
    assert good["level"] == "L0"


def test_bad_citation_removed(tmp_path):
    p = tmp_path / "g2.sqlite3"
    db.connect(p).close()
    out = guard.check("这是资料。[M-nope]", p)
    assert out["level"] == "L1" and "bad_citation" in out["issues"]
    assert "[M-nope]" not in out["reply"]


def test_valid_citation_kept(tmp_path):
    p = tmp_path / "g3.sqlite3"
    con = db.connect(p)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-1", "user", "你好", time.time(), 0))
    con.commit(); con.close()
    out = guard.check("这是资料。[M-1]", p)
    assert out["level"] == "L0" and "[M-1]" in out["reply"]
