# -*- coding: utf-8 -*-
"""2026-10-28 用户反馈回归：不要「」方括号 / 不要「想起你说」/ 不要「账」类比喻。"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agi import textguard
from agi.selflearn import guard, proactive
from agi.selflearn.chat import SelfLearnAgent


# --------------------------------------------------------------------------- #
# ① 「」不出现
# --------------------------------------------------------------------------- #
def test_textguard_strip_corner_brackets():
    assert textguard.strip_corner_brackets("想起你说「晚点回来修」，现在呢？") == \
        "想起你说晚点回来修，现在呢？"
    assert textguard.strip_corner_brackets("『好』") == "好"


def test_proactive_polish_no_brackets_no_phrase():
    out = proactive.polish_outbound("想起你说「晚点回来帮忙修某个东西」，现在进展怎么样了？")
    assert "「" not in out and "」" not in out
    assert "想起你说" not in out


# --------------------------------------------------------------------------- #
# ② 「账」类比喻
# --------------------------------------------------------------------------- #
def test_guard_ledger_check_and_strip():
    hit = guard.ledger_check("三个“行”就想把账揭过去啊")
    assert hit["hit"] is True and any("ledger" in i for i in hit["issues"])
    cleaned = guard.strip_ledger_clauses("行吧，是我冤枉你了。我先把这句冤枉人的账算清楚。")
    assert "账" not in cleaned and "冤枉你了" in cleaned
    assert guard.ledger_check("今天挺累的")["hit"] is False


class _FakeClient:
    def __init__(self, text):
        self.text = text

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        if kwargs.get("purpose") != "selflearn_chat":
            return {"ok": True, "text": "[]"}
        return {"ok": True, "text": self.text}


def test_chat_reply_strips_ledger_and_brackets(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ENABLED", "0")
    monkeypatch.setattr(guard, "canned_check",
                        lambda reply, user="": {"hit": False, "issues": []})
    agent = SelfLearnAgent(tmp_path / "chat.sqlite3",
                           client=_FakeClient("「行吧是我冤枉你了」。我先把这句冤枉人的账算清楚。"),
                           rng=random.Random(2))
    row = agent.send("我只引用了一次啊")
    assert row["ok"] is True
    reply = row["reply"]
    assert "账" not in reply
    assert "「" not in reply and "」" not in reply
    assert reply.strip()
