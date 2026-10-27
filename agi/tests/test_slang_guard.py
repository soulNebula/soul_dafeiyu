# -*- coding: utf-8 -*-
"""P27：去网感腔调（guard slang + voice_polish + chat 重写）。"""
from __future__ import annotations

import json
import random

import pytest

from agi.selflearn import chat as chat_module
from agi.selflearn import guard, voice_polish


def test_slang_check_and_strip():
    for text in ("这顿饭算是赢了", "这波操作绝了", "笑死，666", "血压飙升"):
        assert guard.slang_check(text)["hit"] is True
    assert guard.slang_check("今天吃了寿司，挺香")["hit"] is False
    # 媒体指令中的贴纸名 666 不算正文网感词
    assert guard.slang_check("今天吃了寿司\n[[media:sticker:666]]")["hit"] is False
    cleaned = guard.strip_slang_clauses("今天吃了寿司，这顿饭算是赢了，你请客")
    assert "赢了" not in cleaned
    assert "寿司" in cleaned
    assert guard.slang_check(cleaned)["hit"] is False


def test_voice_polish_slang_fallback():
    out = voice_polish.polish_reply("天天拿寿司馋我，你中午这顿饭算是赢了，笑死")
    assert "算是赢了" not in out["text"]
    assert "笑死" not in out["text"]
    assert "确实不错" in out["text"]
    assert voice_polish.polish_reply(out["text"])["text"] == out["text"]


class Fake:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.calls.append(kwargs.get("purpose"))
        if kwargs.get("purpose") == "selflearn_chat":
            return {"ok": True, "text": self.replies.pop(0) if self.replies else "好"}
        return {"ok": True, "text": "[]"}


def _agent(tmp_path, replies, monkeypatch):
    monkeypatch.setattr(chat_module, "_ensure_meaningful", lambda reply, **kw: reply)
    return chat_module.SelfLearnAgent(tmp_path / "chat.sqlite3", client=Fake(replies),
                                      rng=random.Random(1))


def test_chat_rewrites_slang_once(tmp_path, monkeypatch):
    agent = _agent(tmp_path, ["这顿饭算是赢了", "寿司看着确实不错"], monkeypatch)
    row = agent.send("我中午吃了寿司")
    assert row["ok"] is True
    assert "赢了" not in row["reply"]
    assert row["guard"]["slang_regenerated"]["method"] == "regenerated"
    assert "这顿饭算是赢了" in row["guard"]["slang_regenerated"]["before"]


def test_chat_still_dirty_strips_clause(tmp_path, monkeypatch):
    agent = _agent(tmp_path, ["这波绝了", "还是这波绝了"], monkeypatch)
    row = agent.send("你看这个")
    assert row["ok"] is True
    assert guard.slang_check(row["reply"])["hit"] is False
    assert row["guard"]["slang_regenerated"]["method"] in ("clause_removed", "fallback")


def test_switch_off_no_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv("SLANG_GUARD_ENABLED", "0")
    agent = _agent(tmp_path, ["今天你赢了", "不会用到"], monkeypatch)
    row = agent.send("随便聊聊")
    assert row["ok"] is True
    assert "赢了" in row["reply"]
    assert "slang_regenerated" not in (row.get("guard") or {})


def test_prompt_contains_slang_ban(tmp_path, monkeypatch):
    from tools.persona import install_xiaoyu
    text = install_xiaoyu.build_chat_system()
    assert "网络梗" in text and "赢了" in text and "算是" in text
    assert "生活化打趣" in text and "输赢评判" in text
