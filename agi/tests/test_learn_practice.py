# -*- coding: utf-8 -*-
"""Learn Loop 后半测试。"""
from __future__ import annotations

from agi.selflearn import learn_loop, learn_practice


class C:
    def available(self): return True
    def chat(self, messages, **kwargs):
        return {"ok": True, "text": '{"question":"加法怎么写？","expected_terms":["+","加法"]}'}


def test_make_exercise_and_practice(tmp_path):
    p = tmp_path / "lp.sqlite3"
    ex = learn_practice.make_exercise({"content": "用 + 做加法"}, C())
    assert ex["ok"] and "+" in ex["expected_terms"]
    tid = learn_loop.open_gap(p, "SK-ADD", "练习加法")
    learn_loop.record_source(p, "SK-ADD", "https://a", "a", "加法用 +")
    r = learn_practice.answer_practice(p, tid, "用 + 做加法", ex["expected_terms"])
    assert r["graded"]["passed"] is True
    assert learn_practice.source_count(p, "SK-ADD") == 1
    assert learn_practice.has_two_sources(p, "SK-ADD") is False
