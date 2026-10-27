# -*- coding: utf-8 -*-
"""实时入库测试。"""
from __future__ import annotations

from agi.selflearn import db, live_ingest


def test_record_message_idempotent(tmp_path):
    path = tmp_path / "live.sqlite3"
    rid = live_ingest.record_message(path, "user", "我喜欢咖啡", ts=100.0, message_id="M-1")
    assert rid.startswith("RE-")
    live_ingest.record_message(path, "user", "我喜欢咖啡", ts=100.0, message_id="M-1")
    con = db.connect(path)
    try:
        rows = [dict(r) for r in con.execute("SELECT source,sender,text,ts FROM raw_events")]
    finally:
        con.close()
    assert len(rows) == 1
    assert rows[0]["source"] == "live" and rows[0]["sender"] == "user"


def test_empty_text_is_ignored(tmp_path):
    path = tmp_path / "live2.sqlite3"
    assert live_ingest.record_message(path, "user", "   ") == ""
