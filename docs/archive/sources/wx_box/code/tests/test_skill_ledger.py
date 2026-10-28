# -*- coding: utf-8 -*-
"""审计回归：反馈/弱验证不能绕过 verifier 解锁技能节点。"""
from __future__ import annotations

from agi.selflearn import chat, db, skilltree
from agi.tests.test_selflearn import FakeClient


def test_feedback_cannot_unlock_code_skill(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    agent = chat.SelfLearnAgent(path, client=FakeClient())
    # 大量 good 反馈只会影响对话技能/变体账本
    for i in range(6):
        r = agent.send(f"今天聊点别的 {i}")
        if r.get("turn_id"):
            agent.feedback(r["turn_id"], "good")
    for sid in ("SK-CODE-HTML", "SK-CODE-PY"):
        node = skilltree.get(path, sid)
        assert int(node["level"]) < 3
        assert node["status"] == "locked"
