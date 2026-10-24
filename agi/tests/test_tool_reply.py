"""L2「能查」入口测试：判定启发式 / 研究结果注入 / 来源落库。"""
from __future__ import annotations

import sqlite3

import pytest

from agi.selflearn import chat, tool_reply


# ---------------------------------------------------------------- 判定
def test_should_lookup_hits_explicit_and_time_sensitive():
    assert tool_reply.should_lookup("帮我查一下明天北京天气")
    assert tool_reply.should_lookup("今天有什么新闻")
    assert tool_reply.should_lookup("deepseek 最新版本是多少")


def test_should_lookup_skips_smalltalk():
    assert not tool_reply.should_lookup("今天有点累")
    assert not tool_reply.should_lookup("你吃饭了吗")
    assert not tool_reply.should_lookup("在吗")
    assert not tool_reply.should_lookup("")


def test_should_lookup_env_modes(monkeypatch):
    monkeypatch.setenv("SELFLEARN_LOOKUP", "off")
    assert tool_reply.should_lookup("帮我查一下天气") is False
    monkeypatch.setenv("SELFLEARN_LOOKUP", "always")
    assert tool_reply.should_lookup("随便说句话") is True


# ---------------------------------------------------------------- 研究块
def test_research_builds_context_with_sources(monkeypatch):
    def fake_run_agent(client, text, **kw):
        return {"ok": True, "reply": "北京今天多云 12~20 度",
                "sources": [{"title": "天气网", "url": "https://example.com/weather"}],
                "tool_calls": 2, "steps": [{"tool": "web_search", "ok": True}]}

    monkeypatch.setattr(tool_reply.agent_mod, "run_agent", fake_run_agent)

    class FakeClient:
        def available(self):
            return True

    out = tool_reply.research(FakeClient(), "帮我查一下今天北京天气")
    assert out["used"] is True
    assert "多云" in out["context"]
    assert "https://example.com/weather" in out["context"]
    assert out["tool_calls"] == 2


def test_research_fail_closed_on_error(monkeypatch):
    def boom(client, text, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(tool_reply.agent_mod, "run_agent", boom)

    class FakeClient:
        def available(self):
            return True

    out = tool_reply.research(FakeClient(), "帮我查一下今天的汇率")
    assert out["used"] is False and "network down" in out["error"]


def test_research_skips_when_not_needed(monkeypatch):
    called = {"n": 0}

    def fake_run_agent(*a, **k):
        called["n"] += 1
        return {"ok": True, "reply": "x", "sources": []}

    monkeypatch.setattr(tool_reply.agent_mod, "run_agent", fake_run_agent)
    out = tool_reply.research(object(), "今天有点累")
    assert out["used"] is False and called["n"] == 0


# ---------------------------------------------------------------- 落库
def test_record_sources_writes_rows(tmp_path):
    db = tmp_path / "src.sqlite3"
    n = tool_reply.record_sources(db, "查天气", {
        "sources": [{"title": "天气网", "url": "https://a.example/x"},
                    {"title": "重复链接", "url": "https://a.example/x"},
                    {"title": "无链接", "url": ""}]})
    assert n == 1  # 同一 URL 去重（本次调用内保留第一条）
    con = sqlite3.connect(db)
    try:
        rows = con.execute("select url, title, query from research_sources").fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    assert rows[0][0] == "https://a.example/x"
    assert rows[0][1] == "天气网"
    assert rows[0][2] == "查天气"


def test_record_sources_noop_without_db(tmp_path):
    assert tool_reply.record_sources("", "q", {"sources": [{"url": "https://x"}]}) == 0


# ---------------------------------------------------------------- chat 集成
class FakeClient:
    def available(self):
        return True

    def chat(self, messages, **kw):
        return {"ok": True, "text": "我在呢", "usage": {}}


def test_chat_injects_research_and_records(monkeypatch, tmp_path):
    captured = {}

    def fake_research(client, text, **kw):
        captured["called"] = True
        return {"used": True, "context": "【资料】答案在 42",
                "sources": [{"title": "t", "url": "https://e.example/42"}],
                "tool_calls": 1, "steps": []}

    monkeypatch.setattr(tool_reply, "research", fake_research)
    agent = chat.SelfLearnAgent(tmp_path / "c.sqlite3", client=FakeClient())
    out = agent.send("帮我查一下宇宙终极答案")
    assert captured.get("called") is True
    assert out["research"]["used"] is True
    assert out["research"]["sources"][0]["url"] == "https://e.example/42"
    # 回复仍然走人格层，不被资料直接替换
    assert out["reply"]
