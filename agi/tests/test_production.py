# -*- coding: utf-8 -*-
"""生产门槛的离线测试，使用合成库，不读活动数据。"""
from __future__ import annotations

import time

from agi.selflearn import db, production


def _insert(con, sql, rows):
    con.executemany(sql, rows)
    con.commit()


def test_production_empty_db_is_not_ready(tmp_path):
    path = tmp_path / "empty.sqlite3"
    db.connect(path).close()
    s = production.snapshot(path)
    assert s["ok"] is True and s["verdict"] in ("not_ready", "needs_work")
    assert s["checks"]["feedback"]["ok"] is False
    assert "7 天" in s["note"]


def test_production_ready_synthetic(tmp_path):
    path = tmp_path / "ready.sqlite3"
    con = db.connect(path)
    now = time.time()
    base = now - 3 * 86400
    ts = [base + (i % 3) * 86400 + i * 60 for i in range(30)]
    _insert(con, "INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
            [(f"M-{i}", "user" if i % 2 else "assistant", f"m{i}", ts[i], 0)
             for i in range(30)])
    _insert(con, "INSERT INTO turns(id,user_message_id,assistant_message_id,reply,created_at) "
                 "VALUES(?,?,?,?,?)",
            [(f"T-{i}", f"M-{i}", f"M-{i}", "ok", ts[i]) for i in range(10)])
    _insert(con, "INSERT INTO feedback(id,turn_id,reaction,score,created_at) VALUES(?,?,?,?,?)",
            [(f"F-{i}", f"T-{i}", "good", 1.0, base + (i % 2) * 86400 + i * 60) for i in range(3)])
    _insert(con, "INSERT INTO user_memories(id,scope,kind,content,evidence,confidence,status,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?)",
            [(f"UM-{i}", "user", "fact", f"memory {i}", "原话", 0.9, "active", now - i)
             for i in range(6)])
    _insert(con, "INSERT INTO persona(id,key,value,evidence_json,version,status,updated_at) "
                 "VALUES(?,?,?,?,?,?,?)",
            [(f"P-{i}", f"trait{i}", f"value{i}", "[]", 1, "active", now - i) for i in range(3)])
    _insert(con, "INSERT INTO claims(id,subject,predicate,object,polarity,time_ref,claim_type,"
                 "evidence_span,evidence_message_id,confidence,source_level,status,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(f"C-{i}", "user", "p", f"o{i}", 1, "", "fact", "原话", f"M-{i}", 0.9, "A", "active", now - i)
             for i in range(5)])
    con.close()
    s = production.snapshot(path)
    assert s["verdict"] == "pilot_ready", s
    assert s["passed"] == s["total"]
    md = production.markdown(path)
    assert "PASS" in md and "pilot_ready" in md
