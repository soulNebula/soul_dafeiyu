# -*- coding: utf-8 -*-
"""自动拆解测试。"""
from __future__ import annotations

from agi.selflearn import goals, learn_plan


class C:
    def available(self): return True
    def chat(self, messages, **kwargs):
        return {"ok": True, "text": '{"steps":["看语法","写例子","做练习"]}'}


def test_decompose_and_apply(tmp_path):
    p = tmp_path / "plan.sqlite3"
    d = learn_plan.decompose("学会 Python", C())
    assert d["ok"] and len(d["steps"]) == 3
    a = learn_plan.apply(p, "学会 Python", C())
    assert a["ok"] and len(a["children"]) == 3
    assert len(goals.list_goals(p)) == 4
