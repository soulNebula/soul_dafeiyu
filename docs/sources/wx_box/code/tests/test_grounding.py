# -*- coding: utf-8 -*-
"""无据断言弱审计测试。"""
from __future__ import annotations

import time

from agi.selflearn import db, grounding


def test_supported_sentence():
    r = grounding.check_reply("你喜欢咖啡，那晚上写东西时也会喝吗？", ["我喜欢咖啡和晚上写东西"])
    assert r["unsupported"] == 0


def test_unsupported_detail_detected():
    r = grounding.check_reply("你家那只橘猫最近生病了。", ["我喜欢咖啡"])
    assert r["unsupported"] >= 1


def test_audit_synthetic_db(tmp_path):
    path = tmp_path / "g.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-1", "user", "我喜欢咖啡", time.time(), 0))
    con.execute("INSERT INTO turns(id,user_message_id,assistant_message_id,reply,created_at) "
                "VALUES(?,?,?,?,?)", ("T-1", "M-1", "A-1", "咖啡。", time.time()))
    con.commit(); con.close()
    a = grounding.audit(path)
    assert a["claims"] >= 1 and a["rate"] <= 0.5
