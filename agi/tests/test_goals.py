# -*- coding: utf-8 -*-
"""目标树测试。"""
from __future__ import annotations

from agi.selflearn import goals


def test_goal_tree_add_update(tmp_path):
    p = tmp_path / "goals.sqlite3"
    root = goals.add(p, "把 AGI 跑通", priority=0.9)
    child = goals.add(p, "补 S5 表情包", parent_id=root["id"], priority=0.7)
    assert root["ok"] and child["ok"]
    tree = goals.tree(p)
    assert tree[0]["title"] == "把 AGI 跑通"
    assert tree[0]["children"][0]["title"] == "补 S5 表情包"
    out = goals.update(p, child["id"], status="done", progress=1.0)
    assert out["ok"] and out["status"] == "done"
    assert goals.list_goals(p, status="done")[0]["id"] == child["id"]


def test_goal_rejects_empty_and_bad_status(tmp_path):
    p = tmp_path / "goals2.sqlite3"
    assert goals.add(p, "  ")["ok"] is False
    g = goals.add(p, "x")
    assert goals.update(p, g["id"], status="weird")["ok"] is False
