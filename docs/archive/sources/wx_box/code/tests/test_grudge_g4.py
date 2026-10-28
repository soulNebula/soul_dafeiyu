# -*- coding: utf-8 -*-
"""G4 不可原谅层测试。"""
from agi.selflearn import affect, db, trust


def test_g4_permanent_and_context(tmp_path):
    p = tmp_path / "g4.sqlite3"
    db.connect(p).close()
    trust.update(p, "bad", None, "背叛了我的信任，利用我最脆弱的时候")
    st = affect.snapshot(p)
    assert st["grudge_level"] == "g4"
    ctx = affect.grudge_context(p)
    assert "保持距离" in ctx
