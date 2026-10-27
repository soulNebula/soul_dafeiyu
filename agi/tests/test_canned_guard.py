# -*- coding: utf-8 -*-
"""P28：金句腔/主动收尾守卫（canned + closing）。"""
from __future__ import annotations

import random

import pytest

from agi.selflearn import chat as chat_module
from agi.selflearn import guard, voice_polish


def test_canned_check_and_closing_condition():
    assert guard.canned_check("不高级但耐看")["hit"] is True
    assert guard.canned_check("这默契先存着")["hit"] is True
    assert guard.canned_check("买完就踏实了")["hit"] is True
    assert guard.canned_check("那先这样，忙你的去", "今天聊完")["hit"] is True
    assert guard.canned_check("那先这样，忙你的去", "我去忙了")["hit"] is False
    assert guard.canned_check("那你先忙，回聊", "我睡了，晚安")["hit"] is False
    assert guard.canned_check("今天吃了寿司，挺香")["hit"] is False


def test_strip_canned_and_closing():
    cleaned = guard.strip_canned_clauses("这顿还行，但耐看，那先这样吧", "继续聊")
    assert "耐看" not in cleaned
    assert "先这样" not in cleaned
    allowed = guard.strip_canned_clauses("那先这样，忙你的去", "我去忙了")
    assert "那先这样" in allowed


def test_voice_polish_canned_fallback():
    out = voice_polish.polish_reply("这番茄味挺稳的，买完就踏实了")
    assert "挺稳的" not in out["text"]
    assert "买完就踏实了" not in out["text"]


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


def test_chat_canned_rewrites(tmp_path, monkeypatch):
    agent = _agent(tmp_path, ["这个不高级但耐看", "穿久了应该挺舒服"], monkeypatch)
    row = agent.send("这件衣服怎么样")
    assert row["ok"] is True
    assert "耐看" not in row["reply"]
    assert "canned_regenerated" in row["guard"]


def test_chat_still_canned_strips_clause(tmp_path, monkeypatch):
    agent = _agent(tmp_path, ["这默契先存着", "这默契先存着"], monkeypatch)
    row = agent.send("你懂我意思吧")
    assert row["ok"] is True
    assert "默契" not in row["reply"]
    assert row["guard"]["canned_regenerated"]["method"] in ("clause_removed", "fallback")


def test_closing_allowed_when_user_leaves(tmp_path, monkeypatch):
    agent = _agent(tmp_path, ["那先这样，你去忙吧", "不会用到"], monkeypatch)
    row = agent.send("我去忙了")
    assert row["ok"] is True
    assert "那先这样" in row["reply"]
    assert "canned_regenerated" not in row.get("guard", {})


def test_switch_off_no_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv("CANNED_GUARD_ENABLED", "0")
    agent = _agent(tmp_path, ["这个不高级但耐看", "不会用到"], monkeypatch)
    row = agent.send("这件衣服怎么样")
    assert row["ok"] is True
    assert "耐看" in row["reply"]
    assert "canned_regenerated" not in row.get("guard", {})


def test_prompt_contains_canned_rules():
    from tools.persona import install_xiaoyu
    text = install_xiaoyu.build_chat_system()
    assert "金句" in text and "耐看" in text and "收下" in text
    assert "主动结束对话" in text and "元自我" in text
