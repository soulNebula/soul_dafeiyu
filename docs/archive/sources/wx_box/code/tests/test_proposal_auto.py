# -*- coding: utf-8 -*-
"""04 auto gray / 14 ticker 测试。"""
from __future__ import annotations

from agi.selflearn import autonomy, proposal_auto, proposals, ticker


def test_auto_gray_requires_two_evidence(tmp_path):
    p = tmp_path / "pa.sqlite3"
    a = proposals.create(p, "persona_trait", "k1", {"value": "v1"}, ["e1"])
    b = proposals.create(p, "persona_trait", "k2", {"value": "v2"}, ["e1", "e2"])
    r = proposal_auto.auto_gray(p)
    assert b["id"] in r["moved"]
    assert a["id"] not in r["moved"]
    assert proposals.get(p, b["id"])["status"] == "gray"
    st = proposal_auto.weekly_stats(p)
    assert st["by_status"]["gray"] == 1


def test_ticker_gate_blocks(monkeypatch, tmp_path):
    p = tmp_path / "t.sqlite3"
    monkeypatch.setattr(autonomy, "gate", lambda path, now=None: {"allowed": False, "reason": "off"})
    r = ticker.run_once(p)
    assert r["ran"] is False and r["reason"] == "off"
