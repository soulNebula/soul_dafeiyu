# -*- coding: utf-8 -*-
"""反思→提案桥测试。"""
from __future__ import annotations

from agi.selflearn import proposal_bridge, proposals


def test_reflection_to_draft_proposal(tmp_path):
    path = tmp_path / "p.sqlite3"
    reflected = {"ok": True, "activated": [
        {"key": "plain_style", "value": "少寒暄，直接说结论", "evidence_turns": ["T-1"]},
        {"key": "plain_style", "value": "少寒暄，直接说结论", "evidence_turns": ["T-1"]},
    ]}
    r = proposal_bridge.from_reflection(path, reflected)
    assert r["ok"] and r["created"] == 1 and r["skipped"] == 1
    items = proposals.list_proposals(path, status="draft")
    assert len(items) == 1 and items[0]["kind"] == "persona_trait"


def test_reflection_bad_input_does_not_create(tmp_path):
    path = tmp_path / "p2.sqlite3"
    assert proposal_bridge.from_reflection(path, {})["ok"] is False
    assert proposals.list_proposals(path) == []
