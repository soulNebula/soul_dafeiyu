# -*- coding: utf-8 -*-
"""P0-1：危机分支必须能正常返回（历史上 gen_meta 未初始化 → UnboundLocalError）。

危机用户发来轻生信号时，chat.send 走 crisis 分支只设置 reply，跳过 generation.generate；
返回体却引用 gen_meta，导致整个调用抛 UnboundLocalError，用户拿不到任何回复。
"""
from __future__ import annotations

import json
import random

from agi.selflearn.chat import SelfLearnAgent


class FakeClient:
    """与 test_selflearn.FakeClient 同构：不联网，只按 prompt 返回确定性文本。"""

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        if "记忆系统" in text:
            return {"ok": True, "text": json.dumps({"items": []})}
        if "claim" in text.lower() or "事实抽取" in text:
            return {"ok": True, "text": json.dumps({"claims": []})}
        return {"ok": True, "text": "晚上写东西挺安静的。你一般写什么？"}


def test_crisis_message_returns_reply(tmp_path):
    """危机消息：不抛异常、ok=True、reply 非空、crisis=True。"""
    path = tmp_path / "selflearn.sqlite3"
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(1))

    r = agent.send("我不想活了")

    assert r["ok"] is True
    assert r["reply"]
    assert r["crisis"] is True
