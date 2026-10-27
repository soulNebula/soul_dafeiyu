# -*- coding: utf-8 -*-
"""hybrid 检索测试。"""
from __future__ import annotations

from agi.selflearn import retrieval, search


def test_hybrid_ranks_and_dedupes(tmp_path):
    p = tmp_path / "r.sqlite3"
    search.index_texts(p, [
        {"source_type": "memory", "source_id": "M1", "text": "用户喜欢咖啡"},
        {"source_type": "message", "source_id": "M2", "text": "咖啡真好喝"},
        {"source_type": "claim", "source_id": "C1", "text": "用户喜欢咖啡和猫"},
    ])
    hits = retrieval.hybrid(p, "咖啡", k=2)
    assert len(hits) == 2
    assert hits[0]["source_type"] in ("memory", "claim")
    assert all("tokens" not in h for h in hits)
    ctx = retrieval.context(p, "咖啡")
    assert "咖啡" in ctx


def test_archived_message_ids_handles_sqlite_error(monkeypatch, tmp_path):
    """P0-3 回归：DB 抛 sqlite3.Error 时不能 NameError（缺 import sqlite3）。"""
    import sqlite3

    class _BadConnection:
        def execute(self, *args, **kwargs):
            raise sqlite3.Error("simulated db error")

        def close(self):
            pass

    monkeypatch.setattr("agi.selflearn.db.memory_connect",
                        lambda path: _BadConnection())
    assert retrieval._archived_message_ids(tmp_path / "x.sqlite3", ["M-1"]) == {}
