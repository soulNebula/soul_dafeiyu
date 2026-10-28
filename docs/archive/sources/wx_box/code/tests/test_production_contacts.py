# -*- coding: utf-8 -*-
"""联系人库生产聚合测试。"""
from __future__ import annotations

import time

from agi.selflearn import db, production_contacts


def _seed(path, n_messages=12, n_turns=6, feedback=0, explicit=0):
    con = db.connect(path)
    now = time.time()
    for i in range(n_messages):
        con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                    (f"M-{path.stem}-{i}", "user", f"m{i}", now - i * 86400, 0))
    for i in range(n_turns):
        con.execute("INSERT INTO turns(id,user_message_id,assistant_message_id,reply,created_at) "
                    "VALUES(?,?,?,?,?)", (f"T-{i}", f"M-{i}", f"A-{i}", "ok", now - i))
    for i in range(feedback):
        note = "" if i < explicit else "implicit:x"
        con.execute("INSERT INTO feedback(id,turn_id,reaction,score,note,created_at) VALUES(?,?,?,?,?,?)",
                    (f"F-{i}", f"T-{i}", "good", 1.0, note, now - i))
    con.commit(); con.close()


def test_aggregate_contacts(tmp_path):
    a = tmp_path / "a.sqlite3"; b = tmp_path / "b.sqlite3"
    _seed(a, 12, 6, feedback=2, explicit=1)
    _seed(b, 12, 6, feedback=2, explicit=2)
    out = production_contacts.aggregate([a, b])
    assert out["db_count"] == 2
    assert out["raw"]["explicit_feedback"] == 3
    assert out["checks"]["explicit_feedback"] is True
