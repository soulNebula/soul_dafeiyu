# -*- coding: utf-8 -*-
"""P7-f：gateway 主动链必须走 db 层，不能打开基名 .sqlite3。"""
from __future__ import annotations

import inspect
import sqlite3

from agi.selflearn import db
from agi.wechat_gateway import proactive_worker


def test_gateway_source_has_no_direct_sqlite_connect():
    source = inspect.getsource(proactive_worker)
    assert "sqlite3.connect" not in source
    assert "import sqlite3" not in source


def test_gateway_events_route_to_memory_and_never_create_base(tmp_path):
    base = tmp_path / "contact.sqlite3"
    eid = proactive_worker.record_event(base, "好友A", "主动消息", "sent", now=100.0,
                                        kind="care")
    assert eid
    proactive_worker.mark_response(base, "哈哈", now=101.0)
    assert proactive_worker.feedback_cooldown(base, now=102.0) == ""
    rows = proactive_worker.recent_sent_texts(base, "好友A", since=0)
    assert rows and rows[0][1] == "主动消息"

    # 不允许生成/打开基名库
    assert not base.exists()
    mem = db.memory_path(base)
    assert mem.exists()
    con = sqlite3.connect(mem)
    try:
        tables = {str(r[0]) for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "proactive_events" in tables
        row = con.execute(
            "SELECT contact,text,status,kind,response FROM proactive_events").fetchone()
        assert row == ("好友A", "主动消息", "sent", "care", "positive")
    finally:
        con.close()


def test_gateway_accepts_already_migrated_base(tmp_path):
    base = tmp_path / "migrated.sqlite3"
    db.connect(base).close()
    proactive_worker.record_event(base, "好友B", "hello", "sent", now=1.0)
    assert not base.exists()
    assert len(proactive_worker.recent_sent_texts(base, "好友B", since=0)) == 1
