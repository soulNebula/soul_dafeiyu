# -*- coding: utf-8 -*-
"""S8 检索：中文命中、幂等索引、空查询、FTS 降级与 limit。"""
from __future__ import annotations

import sqlite3

from agi.selflearn import db, search as search_mod

ITEMS = [
    {"source_type": "message", "source_id": "M-1",
     "text": "用户喜欢晚上写代码，最近常用 Python 写小工具"},
    {"source_type": "memory", "source_id": "UM-1",
     "text": "用户住在杭州，周末常去西湖边散步"},
    {"source_type": "message", "source_id": "M-2",
     "text": "我最近在学硬件，顺便做了个 FTS5 小项目"},
]


def _count(path, table):
    con = sqlite3.connect(db.memory_path(path))
    try:
        return int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        con.close()


def _tables(path):
    con = sqlite3.connect(db.memory_path(path))
    try:
        return {str(r[0]) for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_chinese_and_ascii_query_hit(tmp_path):
    path = tmp_path / "search.sqlite3"
    assert search_mod.index_texts(path, ITEMS) == 3

    hits = search_mod.search(path, "西湖")
    assert hits and hits[0]["source_id"] == "UM-1"
    assert hits[0]["source_type"] == "memory"
    assert "西湖" in hits[0]["text"]
    assert set(hits[0]) == {"source_type", "source_id", "text", "score"}
    assert isinstance(hits[0]["score"], (int, float))

    # ASCII 词整体保留，可以做关键词命中
    assert [h["source_id"] for h in search_mod.search(path, "FTS5")] == ["M-2"]

    # 多关键词按 AND 命中同一条
    both = search_mod.search(path, "杭州 西湖")
    assert both and both[0]["source_id"] == "UM-1"
    assert [h["score"] for h in both] == sorted(
        [h["score"] for h in both], reverse=True)


def test_reindex_is_idempotent_and_updates_text(tmp_path):
    path = tmp_path / "search.sqlite3"
    search_mod.index_texts(path, ITEMS)
    search_mod.index_texts(path, ITEMS)
    assert _count(path, "search_documents") == 3
    assert _count(path, "search_fts") == 3
    assert len(search_mod.search(path, "杭州")) == 1

    # 同一批里重复 source 只写一次
    assert search_mod.index_texts(path, [ITEMS[1], ITEMS[1]]) == 1
    assert _count(path, "search_documents") == 3
    assert len(search_mod.search(path, "西湖")) == 1

    # 原文更新：旧词失效、新词可查，行数不增长
    search_mod.index_texts(path, [{
        "source_type": "memory", "source_id": "UM-1",
        "text": "用户搬到了上海，周末常去外滩"}])
    assert _count(path, "search_documents") == 3
    assert _count(path, "search_fts") == 3
    assert search_mod.search(path, "杭州") == []
    assert search_mod.search(path, "西湖") == []
    assert search_mod.search(path, "上海")[0]["source_id"] == "UM-1"


def test_empty_query_and_missing_index(tmp_path):
    path = tmp_path / "search.sqlite3"
    assert search_mod.index_texts(path, []) == 0
    assert search_mod.search(path, "") == []
    assert search_mod.search(path, "   ") == []
    assert search_mod.search(path, "？？？") == []
    assert search_mod.search(path, None) == []

    # 库不存在 / 库存在但没建过索引：返回空，不抛异常
    assert search_mod.search(tmp_path / "missing.sqlite3", "杭州") == []
    plain = tmp_path / "plain.sqlite3"
    sqlite3.connect(plain).close()
    assert search_mod.search(plain, "杭州") == []

    search_mod.index_texts(path, ITEMS)
    assert search_mod.search(path, "杭州", limit=0) == []
    assert search_mod.search(path, "杭州", limit=-1) == []


def test_fallback_when_fts_table_missing(tmp_path):
    path = tmp_path / "search.sqlite3"
    search_mod.index_texts(path, ITEMS)
    con = sqlite3.connect(db.memory_path(path))
    con.execute("DROP TABLE search_fts")
    con.commit()
    con.close()

    hits = search_mod.search(path, "西湖")
    assert hits and hits[0]["source_id"] == "UM-1"
    assert hits[0]["score"] > 0

    # 同名但不是 FTS5 虚表：识别后继续走扫描
    con = sqlite3.connect(db.memory_path(path))
    con.execute("CREATE TABLE search_fts(x TEXT)")
    con.commit()
    con.close()
    assert search_mod.search(path, "杭州")[0]["source_id"] == "UM-1"


def test_fallback_when_fts5_unavailable(tmp_path, monkeypatch):
    path = tmp_path / "search.sqlite3"
    monkeypatch.setattr(search_mod, "_try_create_fts", lambda con: False)
    assert search_mod.index_texts(path, ITEMS) == 3
    assert "search_fts" not in _tables(path)
    con = sqlite3.connect(db.memory_path(path))
    try:
        meta = con.execute(
            "SELECT value FROM search_meta WHERE key='fts_ok'").fetchone()[0]
    finally:
        con.close()
    assert meta == "0"

    hits = search_mod.search(path, "西湖")
    assert hits and hits[0]["source_id"] == "UM-1"
    assert search_mod.search(path, "Python")[0]["source_id"] == "M-1"

    # FTS 恢复后再次索引：自动建表并回填历史文档
    monkeypatch.undo()
    assert search_mod.index_texts(path, ITEMS) == 3
    assert "search_fts" in _tables(path)
    assert _count(path, "search_fts") == 3
    assert search_mod.search(path, "西湖")[0]["source_id"] == "UM-1"


def test_limit_and_default(tmp_path):
    path = tmp_path / "search.sqlite3"
    items = [
        {"source_type": "message", "source_id": f"M-{i}",
         "text": f"第{i}条记录：周末去咖啡店写代码"}
        for i in range(10)
    ]
    assert search_mod.index_texts(path, items) == 10
    assert len(search_mod.search(path, "咖啡店")) == 8   # 默认 limit=8
    assert len(search_mod.search(path, "咖啡店", limit=3)) == 3
    assert len(search_mod.search(path, "咖啡店", limit=1)) == 1
    assert len(search_mod.search(path, "咖啡店", limit=100)) == 10


def test_missing_source_id_is_stable(tmp_path):
    path = tmp_path / "search.sqlite3"
    assert search_mod.index_texts(path, [{"text": "重复内容也应只留一份"}]) == 1
    assert search_mod.index_texts(path, [{"text": "重复内容也应只留一份"}]) == 1
    assert _count(path, "search_documents") == 1
    hits = search_mod.search(path, "重复内容")
    assert len(hits) == 1
    assert hits[0]["source_type"] == "text"
    assert hits[0]["source_id"].startswith("auto-")


def test_delete_removes_document_from_search(tmp_path):
    from agi.selflearn import search
    p = tmp_path / "s.sqlite3"
    search.index_texts(p, [{"source_type": "memory", "source_id": "UM-1", "text": "喜欢咖啡"}])
    assert search.search(p, "咖啡")
    assert search.delete(p, "memory", "UM-1") == 1
    assert search.search(p, "咖啡") == []
