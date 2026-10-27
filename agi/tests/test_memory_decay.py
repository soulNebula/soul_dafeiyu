# -*- coding: utf-8 -*-
"""P38 记忆衰减测试：半衰期/阶段/唤醒/检索续命/休眠清理（tmp，不联网）。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from agi.selflearn import db, memory_decay


def _base(tmp_path):
    base = tmp_path / "selflearn.sqlite3"
    con = db.connect(base)
    con.close()
    return base


def _insert(base, *, mid, kind, content, importance=0.5, created_at, status="active"):
    con = db.connect(base)
    con.execute(
        "INSERT INTO user_memories(id,kind,content,evidence,confidence,status,created_at,importance) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (mid, kind, content, "证据", 0.8, status, created_at, importance))
    con.commit()
    con.close()


def _now() -> float:
    return datetime(2026, 10, 27, 12).timestamp()


def _columns(base):
    con = sqlite3.connect(db.memory_path(base))
    cols = {row[1] for row in con.execute("PRAGMA table_info(user_memories)")}
    con.close()
    return cols


def test_dry_run_does_not_add_columns_or_write(tmp_path):
    base = _base(tmp_path)
    now = _now()
    _insert(base, mid="UM-1", kind="preference", content="用户喜欢喝咖啡",
            importance=0.5, created_at=now - 28 * 86400)
    result = memory_decay.decay_all(base, now=now, dry_run=True)
    assert result["checked"] == 1
    assert "strength" not in _columns(base)
    assert result["changed"] >= 1


def test_lazy_columns_and_half_life_stage(tmp_path):
    base = _base(tmp_path)
    now = _now()
    _insert(base, mid="UM-1", kind="preference", content="用户喜欢喝咖啡",
            importance=0.5, created_at=now - 28 * 86400)
    result = memory_decay.decay_all(base, now=now, dry_run=False)
    assert result["changed"] >= 1
    cols = _columns(base)
    for col in ("strength", "last_strength_at", "forget_stage", "revive_count", "content_full"):
        assert col in cols
    con = sqlite3.connect(db.memory_path(base))
    con.row_factory = sqlite3.Row
    row = dict(con.execute("SELECT * FROM user_memories WHERE id='UM-1'").fetchone())
    con.close()
    assert abs(row["strength"] - 0.25) < 1e-6     # 14d 半衰期 × 28d → 0.25
    assert row["forget_stage"] == "compressed"
    assert len(row["content"]) <= 40
    assert row["content_full"] == "用户喜欢喝咖啡"


def test_dormant_and_revive_restores_content(tmp_path):
    base = _base(tmp_path)
    now = _now()
    _insert(base, mid="UM-2", kind="smalltalk", content="用户随口说今天风很大",
            importance=0.5, created_at=now - 12 * 86400)
    memory_decay.decay_all(base, now=now, dry_run=False)
    con = sqlite3.connect(db.memory_path(base))
    con.row_factory = sqlite3.Row
    row = dict(con.execute("SELECT * FROM user_memories WHERE id='UM-2'").fetchone())
    con.close()
    assert row["forget_stage"] == "dormant"
    assert row["status"] == "dormant"
    revived = memory_decay.revive(base, "UM-2", now=now)
    assert revived["ok"] is True and revived["strength"] >= 0.6
    con = sqlite3.connect(db.memory_path(base))
    con.row_factory = sqlite3.Row
    row = dict(con.execute("SELECT * FROM user_memories WHERE id='UM-2'").fetchone())
    con.close()
    assert row["forget_stage"] == "full"
    assert row["status"] == "active"
    assert row["content"] == "用户随口说今天风很大"
    assert row["revive_count"] == 1


def test_search_with_revive_fallback_and_purge(tmp_path):
    base = _base(tmp_path)
    now = _now()
    _insert(base, mid="UM-3", kind="event", content="用户说喜欢喝咖啡",
            importance=0.5, created_at=now - 20 * 86400)
    memory_decay.decay_all(base, now=now, dry_run=False)
    hits = memory_decay.search_with_revive(base, "咖啡", now=now)
    assert hits and hits[0]["source_type"] == "memory"
    assert hits[0].get("revived") is True
    # 清理：把 last_strength_at 调到 201 天前
    con = db.connect(base)
    con.execute("UPDATE user_memories SET forget_stage='dormant', "
                "last_strength_at=? WHERE id='UM-3'", (now - 201 * 86400,))
    con.commit()
    con.close()
    dry = memory_decay.purge_dormant(base, now=now, dry_run=True)
    assert dry["count"] == 1
    real = memory_decay.purge_dormant(base, now=now, dry_run=False)
    assert real["count"] == 1
    con = db.connect(base)
    assert con.execute("SELECT count(*) FROM user_memories WHERE id='UM-3'").fetchone()[0] == 0
    con.close()


def test_keyword_stage_and_long_content_compression(tmp_path):
    base = _base(tmp_path)
    now = _now()
    long_text = "用户之前提过很喜欢在周末下午去江边散步，顺便买一杯少糖的拿铁，再慢慢走回家，这段记忆需要被压缩保存。"
    _insert(base, mid="UM-4", kind="preference", content=long_text,
            importance=0.5, created_at=now - 49 * 86400)  # 14d × 3.5 → 0.088
    memory_decay.decay_all(base, now=now, dry_run=False)
    con = sqlite3.connect(db.memory_path(base))
    con.row_factory = sqlite3.Row
    row = dict(con.execute("SELECT * FROM user_memories WHERE id='UM-4'").fetchone())
    con.close()
    assert row["forget_stage"] == "keyword"
    assert 2 <= len(row["content"].split("、")) <= 4
    assert row["content_full"] == long_text


def test_half_life_importance_multiplier():
    assert memory_decay.half_life_days("event", 0.5) == 7.0
    assert memory_decay.half_life_days("profile", 0.5) == 30.0
    assert memory_decay.half_life_days("event", 0.9) == 14.0
    assert memory_decay.half_life_days("event", 0.2) == 3.5
