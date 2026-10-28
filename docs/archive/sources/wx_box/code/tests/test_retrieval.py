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
