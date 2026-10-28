# -*- coding: utf-8 -*-
"""Learn Flow 主链测试：不联网，monkeypatch research。"""
from __future__ import annotations

from agi.selflearn import learn_flow


def test_learn_from_url_writes_source_and_card(tmp_path, monkeypatch):
    monkeypatch.setattr(learn_flow.research, "fetch_text",
                        lambda url: {"ok": True, "url": url, "title": "加法",
                                     "text": "加法用 + 运算符。", "error": ""})
    monkeypatch.setattr(learn_flow.research, "extract_quote",
                        lambda text, keyword: "加法用 + 运算符。")
    class C:
        def available(self): return True
        def chat(self, *a, **k):
            return {"ok": True, "text": '{"content":"用 + 做加法","example":"1+2"}'}
    p = tmp_path / "l.sqlite3"
    r = learn_flow.learn_from_url(p, "SK-TEST-ADD", "https://x", client=C())
    assert r["ok"] and r["source"]["ok"] and r["card"]["ok"]
    assert r["task_id"].startswith("LT-")
