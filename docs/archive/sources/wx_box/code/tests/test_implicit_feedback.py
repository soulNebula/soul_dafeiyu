# -*- coding: utf-8 -*-
"""隐式反馈离线测试。"""
from __future__ import annotations

import time

from agi.selflearn import db, implicit_feedback


def _turn(con, tid, mid, msg_id, text, ts):
    con.execute("INSERT INTO turns(id,user_message_id,assistant_message_id,reply,created_at) VALUES(?,?,?,?,?)",
                (tid, mid, mid, "reply", ts))
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                (msg_id, "user", text, ts, 0))


def test_implicit_feedback_classifies_next_message(tmp_path):
    path = tmp_path / "pilot.sqlite3"
    con = db.connect(path)
    now = time.time()
    _turn(con, "T-1", "M-1", "M-1", "你好", now)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-2", "user", "谢谢，懂了", now + 10, 0))
    _turn(con, "T-2", "M-3", "M-3", "你写错了", now + 20)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-4", "user", "不对，重来", now + 30, 0))
    _turn(con, "T-3", "M-5", "M-5", "随便说说", now + 40)
    con.commit(); con.close()
    r = implicit_feedback.infer(path)
    assert r["inserted"] == 2
    assert r["counts"] == {"good": 1, "ok": 0, "bad": 1}
    # 幂等：已写 feedback 的 turn 不再重复
    r2 = implicit_feedback.infer(path)
    assert r2["inserted"] == 0
