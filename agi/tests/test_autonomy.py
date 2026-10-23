# -*- coding: utf-8 -*-
"""autonomy tick gate 测试。"""
from __future__ import annotations

import json
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
