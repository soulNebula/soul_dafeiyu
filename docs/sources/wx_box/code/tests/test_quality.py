# -*- coding: utf-8 -*-
"""质量审计离线测试。"""
from __future__ import annotations

import json
import time

from agi.selflearn import db, quality


def test_claims_quality_detects_fabricated_span(tmp_path):
    path = tmp_path / "q.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-1", "user", "我喜欢咖啡", time.time(), 0))
    con.execute("INSERT INTO claims(id,subject,predicate,object,polarity,time_ref,claim_type,"
                "evidence_span,evidence_message_id,confidence,source_level,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("C-1", "user", "喜欢", "咖啡", 1, "", "preference", "我喜欢咖啡", "M-1", 0.9, "A", "active", time.time()))
    con.execute("INSERT INTO claims(id,subject,predicate,object,polarity,time_ref,claim_type,"
                "evidence_span,evidence_message_id,confidence,source_level,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("C-2", "user", "喜欢", "茶", 1, "", "preference", "我喜欢茶", "M-1", 0.9, "A", "active", time.time()))
    con.commit(); con.close()
    c = quality.claims_quality(path)
    assert c["total"] == 2 and c["valid_evidence"] == 1 and c["fabricated"] == 1
    assert c["verdict"] == "needs_work"


def test_profile_quality_needs_two_evidence(tmp_path):
    path = tmp_path / "q2.sqlite3"
    con = db.connect(path)
    now = time.time()
    for dim in quality.profile.DIMENSIONS:
        con.execute("INSERT INTO user_profile(id,dimension,value,evidence_json,confidence,status,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    ("UP-" + dim, dim, "v", json.dumps(["UM-1", "UM-2"]), 0.8, "active", now))
    con.commit(); con.close()
    p = quality.profile_quality(path)
    assert p["verdict"] == "pass" and p["with_two_evidence"] == 10
