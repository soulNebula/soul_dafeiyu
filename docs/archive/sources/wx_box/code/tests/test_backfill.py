# -*- coding: utf-8 -*-
"""历史回填的离线测试。"""
from __future__ import annotations

import json
import time

from agi.selflearn import backfill, db


class ClaimClient:
    def available(self):
        return True

    def chat(self, messages, **kwargs):
        return {"ok": True, "text": json.dumps([
            {"subject": "user", "predicate": "喜欢", "object": "咖啡",
             "polarity": 1, "claim_type": "preference",
             "evidence_span": "我喜欢咖啡", "confidence": 0.9}] if "咖啡" in str(messages) else [])}


def test_claims_from_messages_is_idempotent(tmp_path):
    path = tmp_path / "pilot.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-1", "user", "我喜欢咖啡", time.time(), 0))
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-2", "assistant", "咖啡不错", time.time(), 0))
    con.commit(); con.close()
    first = backfill.claims_from_messages(path, ClaimClient(), limit=10)
    second = backfill.claims_from_messages(path, ClaimClient(), limit=10)
    assert first["messages"] == 1 and first["accepted"] >= 1
    assert second["extracted"] >= 1
