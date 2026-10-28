# -*- coding: utf-8 -*-
"""M1-A Data Studio 后端测试：发现/浏览/分页/只读 SQL/写保护/行编辑/备份。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app import datastudio


@pytest.fixture
def env(tmp_path, monkeypatch):
    soul = tmp_path / "SoulAgent"
    dbdir = soul / "agi" / "data"
    dbdir.mkdir(parents=True)
    data_dir = tmp_path / "idea" / ".data"
    data_dir.mkdir(parents=True)
    backups = data_dir / "db_backups"
    monkeypatch.setattr(datastudio.paths, "soulagent_home", lambda: soul)
    monkeypatch.setattr(datastudio.paths, "DATA_DIR", data_dir)
    monkeypatch.setattr(datastudio, "BACKUP_DIR", backups)
    audits: list[dict] = []
    monkeypatch.setattr(datastudio.audit, "record",
                        lambda action, target, **kw: audits.append(
                            {"action": action, "target": target, **kw}))
    db = dbdir / "test.sqlite3"
    con = sqlite3.connect(db)
    con.executescript("""
        create table users (id integer primary key, name text, age integer);
        insert into users(id,name,age) values (1,'甲',20),(2,'乙',30),(3,'丙',40);
        create table nopk (x text);
        insert into nopk values ('a'),('b');
        create table logs (id integer primary key, msg text);
        insert into logs(id,msg) values (1,'hi');
    """)
    con.commit()
    con.close()
    other = data_dir / "other.sqlite3"
    con = sqlite3.connect(other)
    con.execute("create table t (id integer primary key, v text)")
    con.commit()
    con.close()
    outside = tmp_path / "outside.sqlite3"
    con = sqlite3.connect(outside)
    con.execute("create table secret (x text)")
    con.commit()
    con.close()
    return {"soul": soul, "data": data_dir, "db": db, "other": other,
            "outside": outside, "audits": audits, "backups": backups}


def test_list_databases_finds_allowed_roots_only(env):
    result = datastudio.list_databases()
    assert result["ok"] is True
    paths_found = {item["id"] for item in result["items"]}
    assert str(env["db"].resolve()) in paths_found
    assert str(env["other"].resolve()) in paths_found
    assert str(env["outside"].resolve()) not in paths_found
    db_item = next(item for item in result["items"] if item["id"] == str(env["db"].resolve()))
    assert db_item["domain"] in ("other", "memory")


def test_resolve_rejects_outside_and_bad_extension(env):
    assert datastudio.resolve_database(str(env["outside"]))[1] == "path_out_of_allowed_roots"
    assert datastudio.resolve_database(str(env["db"].with_suffix(".txt")))[1] == "not_a_sqlite_file"
    assert datastudio.resolve_database("agi/data/test.sqlite3")[0] == env["db"].resolve()


def test_schema_and_pagination(env):
    db = str(env["db"])
    schema = datastudio.schema(db)
    assert schema["ok"] is True
    users = next(t for t in schema["tables"] if t["name"] == "users")
    assert users["rows"] == 3 and users["primary_key"] == ["id"]
    page = datastudio.read_rows(db, "users", page=1, page_size=2,
                                order_by="age", direction="desc")
    assert page["total"] == 3 and page["pages"] == 2
    assert [row["id"] for row in page["rows"]] == [3, 2]
    filtered = datastudio.read_rows(db, "users", filters={"id": 2})
    assert filtered["total"] == 1 and filtered["rows"][0]["name"] == "乙"
    searched = datastudio.read_rows(db, "users", q="甲")
    assert searched["total"] == 1 and searched["rows"][0]["id"] == 1
    bad = datastudio.read_rows(db, "users", order_by="no_such")
    assert bad["ok"] is False and "unknown_order_by" == bad["error"]


def test_read_sql_and_statement_guards(env):
    db = str(env["db"])
    result = datastudio.run_sql(db, "SELECT id,name FROM users ORDER BY id")
    assert result["ok"] is True and result["kind"] == "read"
    assert result["columns"] == ["id", "name"] and len(result["rows"]) == 3
    assert datastudio.run_sql(db, "ATTACH DATABASE 'x' AS y")["error"] == "attach_not_allowed"
    assert datastudio.run_sql(db, "SELECT 1; SELECT 2")["error"] == "multiple_statements_not_allowed"
    assert datastudio.run_sql(db, "x" * (datastudio.SQL_MAX_BYTES + 1))["error"] == "sql_too_large"
    assert datastudio.run_sql(db, "BEGIN")["error"] == "unsupported_statement"


def test_write_requires_confirm_and_backs_up(env):
    db = str(env["db"])
    before = datastudio.run_sql(db, "SELECT count(*) AS n FROM users")["rows"][0]["n"]
    blocked = datastudio.run_sql(db, "INSERT INTO users(id,name,age) VALUES(4,'丁',50)")
    assert blocked["ok"] is False and blocked["error"] == "write_not_allowed"
    confirm_blocked = datastudio.run_sql(
        db, "INSERT INTO users(id,name,age) VALUES(4,'丁',50)", write=True)
    assert confirm_blocked["ok"] is False and confirm_blocked["error"] == "confirm_required"
    assert datastudio.run_sql(db, "SELECT count(*) AS n FROM users")["rows"][0]["n"] == before
    written = datastudio.run_sql(
        db, "INSERT INTO users(id,name,age) VALUES(4,'丁',50)", write=True,
        confirm=datastudio.CONFIRM_WRITE)
    assert written["ok"] is True and written["kind"] == "write"
    assert written["backup"] and Path(written["backup"]).is_file()
    assert datastudio.run_sql(db, "SELECT count(*) AS n FROM users")["rows"][0]["n"] == before + 1
    assert any(item["action"] == "database.sql_write" for item in env["audits"])
    assert any(item["action"] == "database.backup" for item in env["audits"])


def test_update_row_requires_primary_key_and_confirm(env):
    db = str(env["db"])
    assert datastudio.update_row(db, "users", {"id": 1}, {"name": "A"},
                                 confirm="wrong")["error"] == "confirm_required"
    assert datastudio.update_row(db, "nopk", {"x": "a"}, {"x": "b"},
                                 confirm=datastudio.CONFIRM_WRITE)["error"] == "table_has_no_primary_key"
    assert datastudio.update_row(db, "users", {"name": "甲"}, {"name": "A"},
                                 confirm=datastudio.CONFIRM_WRITE)["error"] == "primary_key_required"
    result = datastudio.update_row(db, "users", {"id": 1}, {"name": "A"},
                                   confirm=datastudio.CONFIRM_WRITE)
    assert result["ok"] is True and result["after"]["name"] == "A"
    assert Path(result["backup"]).is_file()
    row = datastudio.run_sql(db, "SELECT name FROM users WHERE id=1")["rows"][0]
    assert row["name"] == "A"
    assert any(item["action"] == "db.update" for item in env["audits"])
    too_many = datastudio.update_row(db, "users", {"id": 99}, {"name": "X"},
                                     confirm=datastudio.CONFIRM_WRITE)
    assert too_many["ok"] is False and too_many["error"] == "row_not_unique_or_not_found"


def test_backup_list_and_manual_backup(env):
    db = str(env["db"])
    made = datastudio.backup_database(db, reason="manual")
    assert made["ok"] is True and Path(made["backup"]).is_file()
    listing = datastudio.list_backups()
    assert listing["ok"] is True and listing["count"] >= 1
    assert any(item["name"] == Path(made["backup"]).name for item in listing["items"])
