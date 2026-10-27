# -*- coding: utf-8 -*-
"""P37：B站/网上看看意图 → 真实搜索或务实回答。"""
from __future__ import annotations

import random

import pytest

from agi.integrations import web_lookup as wl
from agi.selflearn import chat as chat_module


def test_detect_bili_intents():
    for text in ("去B站看看", "刷刷B站", "看看人家怎么说", "去网上查查",
                 "帮我找点AI新闻", "网上有什么好玩的"):
        intent = wl.detect_intent(text)
        assert intent and intent["kind"] == "bili_request", text
    assert wl.detect_intent("帮我找点AI新闻")["query"].startswith("AI")
    assert wl.detect_intent("今天天气怎么样")["kind"] == "weather"


def test_lookup_search_results(monkeypatch):
    monkeypatch.setattr(wl, "search", lambda q, limit=3: {
        "ok": True, "items": [{"title": "AI 新闻", "summary": "一条摘要",
                              "domain": "example.com"}],
        "count": 1, "source": "test", "query": q, "error": ""})
    out = wl.lookup("帮我找点AI新闻", now=1000.0)
    assert out["kind"] == "bili_request" and out["ok"] is True
    assert "AI 新闻" in out["summary"]


def test_lookup_no_result_and_no_topic(monkeypatch):
    monkeypatch.setattr(wl, "search", lambda q, limit=3: {
        "ok": False, "items": [], "count": 0, "source": "", "query": q,
        "error": "no result"})
    no_result = wl.lookup("帮我找点不存在的东西", now=2000.0)
    assert no_result["ok"] is False and "B站是后台每天刷的" in no_result["summary"]
    no_topic = wl.lookup("去B站看看", now=2000.0)
    assert no_topic["ok"] is False and "没法即时点开视频" in no_topic["summary"]


class Fake:
    def __init__(self, reply="好呀"):
        self.reply = reply
        self.prompts = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.prompts.append("\n".join(str(m.get("content") or "") for m in messages))
        if kwargs.get("purpose") == "selflearn_chat":
            return {"ok": True, "text": self.reply}
        return {"ok": True, "text": "[]"}


def _agent(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(chat_module, "_ensure_meaningful",
                        lambda reply, **kw: reply)
    return chat_module.SelfLearnAgent(tmp_path / "chat.sqlite3", client=fake,
                                      rng=random.Random(1))


def test_chat_bili_result_injected(tmp_path, monkeypatch):
    monkeypatch.setattr(wl, "lookup", lambda text, **kw: {
        "ok": True, "kind": "bili_request", "summary": "我帮你翻了下网页：AI 新闻",
        "items": [], "source": "test", "title": "", "hint": "", "error": ""})
    monkeypatch.setenv("WEB_LOOKUP_ENABLED", "1")
    fake = Fake("看到一条 AI 新闻")
    row = _agent(tmp_path, fake, monkeypatch).send("帮我找点AI新闻")
    assert row["ok"] is True
    assert row["web_lookup"]["kind"] == "bili_request"
    assert any("AI 新闻" in p for p in fake.prompts)


def test_chat_bili_no_topic_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(wl, "lookup", lambda text, **kw: {
        "ok": False, "kind": "bili_request", "summary": "",
        "items": [], "source": "", "title": "",
        "hint": "B站是后台每天刷的，我这边没法即时点开视频；我帮你查网页看看",
        "error": "no_topic"})
    monkeypatch.setenv("WEB_LOOKUP_ENABLED", "1")
    fake = Fake("B站是后台刷的，我这边点不开视频")
    row = _agent(tmp_path, fake, monkeypatch).send("去B站看看")
    assert row["ok"] is True
    assert "B站是后台每天刷的" in "".join(fake.prompts)
