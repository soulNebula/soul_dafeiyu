# -*- coding: utf-8 -*-
"""P7-e：记忆库 / 人格库透明路由、迁移与边界。"""
from __future__ import annotations

import sqlite3

import pytest

from agi.selflearn import db


def _files(path):
    return db.memory_path(path), db.persona_path(path), db.legacy_path(path)


def test_router_routes_by_table_and_keeps_api(tmp_path):
    path = tmp_path / "split.sqlite3"
    con = db.connect(path)
    try:
        con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                    ("M-1", "user", "hi", 1.0, 0))
        con.execute("INSERT INTO persona(id,key,value,evidence_json,version,status,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)", ("P-1", "k", "v", "[]", 1, "active", 1.0))
        con.executemany("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                        [("M-2", "user", "two", 2.0, 0), ("M-3", "assistant", "三", 3.0, 0)])
        con.commit()
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
        assert con.execute("SELECT count(*) FROM persona").fetchone()[0] == 1
        # 同域 JOIN 不跨域
        assert con.execute(
            "SELECT count(*) FROM turns t JOIN messages m ON m.id=t.user_message_id"
        ).fetchone()[0] == 0
        assert con.execute("PRAGMA table_info(user_memories)").fetchone() is not None
        # UPDATE/DELETE 路由
        con.execute("UPDATE messages SET text='changed' WHERE id='M-2'")
        con.execute("DELETE FROM persona WHERE id='P-1'")
        con.commit()
        assert con.execute("SELECT text FROM messages WHERE id='M-2'").fetchone()[0] == "changed"
        assert con.execute("SELECT count(*) FROM persona").fetchone()[0] == 0
    finally:
        con.close()
    mem, per, _legacy = _files(path)
    assert mem.exists() and per.exists()
    assert sqlite3.connect(mem).execute("SELECT count(*) FROM messages").fetchone()[0] == 3
    assert sqlite3.connect(per).execute("SELECT count(*) FROM persona").fetchone()[0] == 0


def test_cross_domain_query_raises_and_prints_sql(tmp_path, capsys):
    con = db.connect(tmp_path / "cross.sqlite3")
    try:
        with pytest.raises(db.CrossDomainQuery) as excinfo:
            con.execute("SELECT * FROM messages JOIN persona ON 1=1")
        assert "SELECT * FROM messages JOIN persona" in str(excinfo.value)
        assert "SELECT * FROM messages JOIN persona" in capsys.readouterr().out
    finally:
        con.close()


def test_transaction_context_manager_commits_both(tmp_path):
    path = tmp_path / "ctx.sqlite3"
    with db.connect(path) as con:
        con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                    ("M-1", "user", "x", 1, 0))
        con.execute("INSERT INTO state(key,value,updated_at) VALUES(?,?,?)",
                    ("k", "v", 1))
    con = db.connect(path)
    try:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM state").fetchone()[0] == 1
    finally:
        con.close()


def _make_legacy(path):
    con = sqlite3.connect(path)
    con.executescript(db.SCHEMA)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-OLD", "user", "旧消息", 1.0, 0))
    con.execute("INSERT INTO turns(id,user_message_id,assistant_message_id,reply,created_at) "
                "VALUES(?,?,?,?,?)", ("T-OLD", "M-OLD", "A-OLD", "ok", 1.0))
    con.execute("INSERT INTO persona(id,key,value,evidence_json,version,status,updated_at) "
                "VALUES(?,?,?,?,?,?,?)", ("P-OLD", "tone", "简洁", "[]", 1, "active", 1.0))
    con.execute("INSERT INTO state(key,value,updated_at) VALUES(?,?,?)",
                ("old_state", "1", 1.0))
    con.commit()
    con.close()


def test_legacy_auto_migration_and_rename(tmp_path):
    path = tmp_path / "old.sqlite3"
    _make_legacy(path)
    con = db.connect(path)
    try:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM turns").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM persona").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM state").fetchone()[0] == 1
    finally:
        con.close()
    mem, per, legacy = _files(path)
    assert mem.exists() and per.exists() and legacy.exists()
    assert not path.exists()
    assert sqlite3.connect(mem).execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    assert sqlite3.connect(per).execute("SELECT count(*) FROM persona").fetchone()[0] == 1


def test_migration_idempotent_and_does_not_duplicate(tmp_path):
    path = tmp_path / "idem.sqlite3"
    _make_legacy(path)
    db.connect(path).close()
    db.connect(path).close()
    mem, per, legacy = _files(path)
    assert sqlite3.connect(mem).execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    assert sqlite3.connect(per).execute("SELECT count(*) FROM persona").fetchone()[0] == 1
    # 再改 legacy 不应影响已迁移的新库
    raw = sqlite3.connect(legacy)
    raw.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-LEGACY", "user", "x", 2, 0))
    raw.commit()
    raw.close()
    db.connect(path).close()
    assert sqlite3.connect(mem).execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_empty_and_missing_boundaries(tmp_path):
    path = tmp_path / "empty.sqlite3"
    con = db.connect(path)
    try:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM persona").fetchone()[0] == 0
    finally:
        con.close()
    # 已经是 memory/persona 路径也能继续用
    con = db.connect(db.memory_path(path))
    try:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    finally:
        con.close()
    # 只存在一个旧库：不重复迁移旧数据，但两个新库都存在
    only = tmp_path / "only.sqlite3"
    sqlite3.connect(only).close()
    db.connect(only).close()
    assert db.memory_path(only).exists() and db.persona_path(only).exists()


def test_unknown_table_defaults_to_memory(tmp_path, caplog):
    import logging
    con = db.connect(tmp_path / "unknown.sqlite3")
    try:
        con.execute("CREATE TABLE weird_new_table(id TEXT PRIMARY KEY)")
        con.execute("INSERT INTO weird_new_table(id) VALUES('x')")
        con.commit()
        assert con.execute("SELECT count(*) FROM weird_new_table").fetchone()[0] == 1
    finally:
        con.close()
