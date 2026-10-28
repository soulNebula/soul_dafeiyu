# -*- coding: utf-8 -*-
"""人格三层测试。"""
from __future__ import annotations

from agi.selflearn import persona_layers


def test_evolving_needs_two_evidence_and_rate_limit(tmp_path):
    p = tmp_path / "pl.sqlite3"
    a = persona_layers.observe(p, "plain_style", "少寒暄", ["t1"])
    assert a["status"] == "candidate"
    b = persona_layers.observe(p, "plain_style", "少寒暄", ["t2"])
    assert b["status"] == "active"
    # 同一天最多 2 次晋级
    persona_layers.observe(p, "k2", "v2", ["a", "b"])
    blocked = persona_layers.observe(p, "k3", "v3", ["a", "b"])
    assert blocked["ok"] is False


def test_core_requires_approval(tmp_path):
    p = tmp_path / "pl2.sqlite3"
    r = persona_layers.observe(p, "honesty", "不装懂", ["a", "b"], layer="core")
    assert r["status"] == "candidate"
    oid = r["id"]
    assert persona_layers.approve(p, oid, "owner")["ok"] is True
    ctx = persona_layers.context(p)
    assert "core:" in ctx
