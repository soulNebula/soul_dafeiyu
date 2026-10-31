# -*- coding: utf-8 -*-
"""联网检索兜底（2026-10-31 真机：模型没调工具却说“搜了下”，802 无记录）。

覆盖：
- 模型 0 次工具调用时，直接 web_search 兜底并产出可引用资料；
- 搜索 0 结果/失败时标记 attempted（回复不许编、802 也要有记录）；
- record_attempt 落库（research_attempts）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from agi.selflearn import tool_reply


class FakeClient:
    def available(self):
        return True


def test_model_skips_tools_then_direct_search(monkeypatch, tmp_path):
    monkeypatch.setattr(tool_reply.agent_mod, "run_agent",
                        lambda *a, **k: {"ok": True, "reply": "凭记忆瞎说",
                                         "sources": [], "notes": [],
                                         "tool_calls": 0, "steps": []})
    monkeypatch.setattr(tool_reply._tools, "web_search",
                        lambda query, count=5: {"ok": True, "query": query,
                                                "results": [
                                                    {"title": "杨国福 门店",
                                                     "url": "https://example.com/a",
                                                     "snippet": "附近有店"},
                                                    {"title": "鸡公煲 评价",
                                                     "url": "https://example.com/b",
                                                     "snippet": "口味偏重"}]})
    result = tool_reply.research(FakeClient(), "你去网上搜搜，看看哪个好吃",
                                 db_path=tmp_path / "m.sqlite3")
    assert result["used"] is True
    assert result["attempted"] is True
    assert result["fallback"] is True
    assert len(result["sources"]) == 2
    assert "杨国福" in result["context"] or "附近有店" in result["context"]


def test_zero_results_is_recorded_as_attempt(monkeypatch, tmp_path):
    monkeypatch.setattr(tool_reply.agent_mod, "run_agent",
                        lambda *a, **k: {"ok": True, "reply": "", "sources": [],
                                         "notes": [], "tool_calls": 0,
                                         "steps": []})
    monkeypatch.setattr(tool_reply._tools, "web_search",
                        lambda query, count=5: {"ok": True, "results": []})
    result = tool_reply.research(FakeClient(), "帮我搜一下这个", db_path=tmp_path / "m.sqlite3")
    assert result["used"] is False
    assert result["attempted"] is True
    assert result["results_count"] == 0


def test_record_attempt_persists(tmp_path):
    db = tmp_path / "m.sqlite3"
    n = tool_reply.record_attempt(db, "测试查询",
                                  {"attempted": True, "used": False,
                                   "query": "测试查询", "results_count": 0,
                                   "error": ""}, turn_id="T-1")
    assert n == 1
    # memory_connect 会路由到 split DB（*.memory.sqlite3）
    found = list(tmp_path.glob("*.memory.sqlite3"))
    assert found, "record_attempt 必须落到可被 802 扫描的 memory 库"
    con = sqlite3.connect(f"file:{found[0].as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT query,status,results FROM research_attempts").fetchall()
    finally:
        con.close()
    assert rows and rows[0][0] == "测试查询" and rows[0][1] == "empty"
