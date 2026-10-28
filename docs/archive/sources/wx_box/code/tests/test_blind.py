# -*- coding: utf-8 -*-
"""盲测/二源测试。"""
from __future__ import annotations

from agi.selflearn import blind, learn_loop


def test_blind_requires_two_sources_and_passes(tmp_path):
    p = tmp_path / "b.sqlite3"
    tid = learn_loop.open_gap(p, "SK-ADD", "加法")
    learn_loop.record_source(p, "SK-ADD", "https://a", "a", "加法用 +")
    b = blind.create(p, "SK-ADD", "加法怎么写？", ["+", "加法"])
    assert b["ok"] and b["question"]
    r = blind.verify_with_blind(p, tid, b["token"], "用 + 做加法")
    assert r["ok"] is False and "2 个来源" in r["reason"]
    learn_loop.record_source(p, "SK-ADD", "https://b", "b", "加法是 +")
    for i in range(3):
        learn_loop.record_practice(p, tid, f"a{i}", True)
    r2 = blind.verify_with_blind(p, tid, b["token"], "用 + 做加法")
    assert r2["ok"] is True and r2["unlocked"]["unlocked"] is True
