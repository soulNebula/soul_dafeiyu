# -*- coding: utf-8 -*-
"""Batch2 集成：Learn Loop 能否从缺口走到 L3。"""
from __future__ import annotations

from agi.selflearn import db, learn_loop, skilltree


def test_learn_loop_reaches_l3_with_provenance_and_practice(tmp_path):
    path = tmp_path / "batch2.sqlite3"
    task = learn_loop.open_gap(path, "SK-TEST-ADD", "学会写一个加法函数")
    assert task.startswith("LT-")

    source = learn_loop.record_source(
        path, "SK-TEST-ADD", "https://example.com/python-add",
        "Python addition", "加法用 + 运算符")
    assert source["ok"] is True

    card = learn_loop.write_card(path, "SK-TEST-ADD",
                                 "用 + 可以做加法", "def add(a,b): return a+b")
    assert card["ok"] is True

    for i in range(3):
        p = learn_loop.record_practice(path, task, f"attempt-{i}", True)
        assert p["ok"] is True

    result = learn_loop.verify_and_unlock(path, task, True, "测试通过")
    assert result["ok"] is True and result["unlocked"] is True
    node = skilltree.get(path, "SK-TEST-ADD")
    assert node["level"] >= 3 and node["status"] == "unlocked"

    st = learn_loop.status(path, "SK-TEST-ADD")
    assert st["source_count"] >= 1 and st["card_count"] >= 1
    assert st["practice"]["count"] >= 3
