# -*- coding: utf-8 -*-
"""autonomy tick gate 测试。"""
from __future__ import annotations

import json
import time

import time

from agi.selflearn import autonomy, db


def test_gate_quiet_hours(tmp_path):
    p = tmp_path / "a.sqlite3"
    db.connect(p).close()
    # 强制 UTC 中午附近避开本地凌晨，用 state 覆盖 quiet_hours
    con = db.connect(p)
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES(?,?,?)",
                ("autonomy", json.dumps({"enabled": True, "quiet_hours": [0, 0]}), time.time()))
    con.commit(); con.close()
    r = autonomy.gate(p, now=time.time())
    assert r["allowed"] is True


def test_gate_disabled_and_budget(tmp_path):
    p = tmp_path / "a2.sqlite3"
    con = db.connect(p)
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES(?,?,?)",
                ("autonomy", json.dumps({"enabled": False}), time.time()))
    con.commit(); con.close()
    assert autonomy.gate(p)["reason"] == "autonomy_disabled"


def test_autonomy_five_levels(tmp_path):
    p = tmp_path / "a3.sqlite3"
    con = db.connect(p)
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES(?,?,?)",
                ("autonomy", json.dumps({"level": "off"}), time.time()))
    con.commit(); con.close()
    r = autonomy.gate(p)
    assert r["allowed"] is False and r["level"] == "off"


def test_daily_budget_zero_means_unlimited(tmp_path):
    import json
    import sqlite3
    from agi.selflearn import autonomy, db

    path = tmp_path / "c.sqlite3"
    con = db.connect(path)
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"enabled": True, "level": "assist", "quiet_hours": [],
                             "daily_budget": 0, "min_interval_seconds": 0}),
                  time.time()))
    for i in range(20):
        con.execute("INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    ("P%d" % i, "check_in", "x", "r", 0.5, "spoken", time.time() - 3600 - i))
    con.commit()
    con.close()
    gate = autonomy.gate(path)
    assert gate["allowed"] is True
