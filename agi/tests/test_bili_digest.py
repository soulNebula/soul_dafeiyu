# -*- coding: utf-8 -*-
"""P10：B站日报解析 + 话题种子接入（全部 tmp_path，不读真实目录）。"""
from __future__ import annotations

import os
import time

import pytest

from agi.integrations import bili_digest
from agi.selflearn import db, topic_seed

REPORT = """# 刷B站 · 2026-10-27

### BV1aa411c7p1
分布式系统入门
为什么值得：它把分布式系统讲得很具体，适合最近在学后端的人。
它自己的一句话：别急着上规模，先把边界想清楚。
### BV1bb411c7p2
标题在下一行
为什么值得：看完对做产品有点启发。
"""


def test_parse_report_normal_next_line_and_missing_quote():
    items = bili_digest.parse_report(REPORT, date="2026-10-27")
    assert [item["bv"] for item in items] == ["BV1aa411c7p1", "BV1bb411c7p2"]
    assert items[0]["title"].startswith("为什么") is False
    assert "分布式系统" in items[0]["why"]
    assert "边界" in items[0]["quote"]
    assert items[1]["title"] == "标题在下一行"
    assert "启发" in items[1]["why"]
    assert items[1]["quote"] == ""
    assert items[0]["date"] == "2026-10-27"


def test_parse_report_failure_is_empty():
    text = """# 刷B站 —— 这一轮没跑起来
> 退出码 1，模型提供方没注册驱动 qianwen
NO_ADAPTER: no adapter registered
"""
    assert bili_digest.parse_report(text) == []
    assert bili_digest.parse_report("") == []
    assert bili_digest.parse_report("随便一段没有视频的文本") == []


def test_clean_title_and_sensitive_filter():
    assert bili_digest.clean_title("【推广】讲分布式系统的一期") == "讲分布式系统的一期"
    assert len(bili_digest.clean_title("很长" * 40, 30)) <= 30
    text = """### BV1aa
【推广】手机号13800138000 的视频
为什么值得：不值得。
"""
    items = bili_digest.parse_report(text)
    assert items and "13800138000" in items[0]["title"]


def test_latest_topics_days_mtime_dedup_and_missing(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    now = time.time()
    (out / "2026-10-27.md").write_text(REPORT, encoding="utf-8")
    dup = REPORT.replace("BV1bb411c7p2", "BV1aa411c7p1")
    (out / "2026-10-26.md").write_text(dup, encoding="utf-8")
    (out / "2026-10-23.md").write_text(REPORT.replace("BV1aa411c7p1", "BV-old"), encoding="utf-8")
    os.utime(out / "2026-10-27.md", (now, now))
    os.utime(out / "2026-10-26.md", (now - 3600, now - 3600))
    os.utime(out / "2026-10-23.md", (now - 5 * 86400, now - 5 * 86400))
    payload = bili_digest.latest_topics(days=2, limit=3, now=now, directory=out)
    assert [item["bv"] for item in payload["topics"]] == ["BV1aa411c7p1", "BV1bb411c7p2"]
    assert payload["report_date"] == "2026-10-27"
    assert payload["report_path"].endswith("2026-10-27.md")
    assert bili_digest.latest_topics(directory=tmp_path / "missing")["topics"] == []


def test_latest_topics_bad_file_tolerated(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "bad.md").write_bytes(b"\xff\xfe\x00bad")
    payload = bili_digest.latest_topics(directory=out, now=time.time())
    assert payload["topics"] == []


def test_topic_seed_uses_bili_and_switch(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / "2026-10-27.md").write_text(REPORT, encoding="utf-8")
    monkeypatch.setenv("BILI_DUTY_OUT_DIR", str(out))
    path = tmp_path / "db.sqlite3"
    db.connect(path).close()
    seed = topic_seed.build(path, time.time())
    assert seed["sources"]["bili"] is True
    assert seed["sources"]["report_date"] == "2026-10-27"
    assert any(item["source"] == "bili" for item in seed["candidates"])
    block = topic_seed.prompt_block(seed)
    assert "最近刷到「" in block and "（视频，可聊）" in block

    monkeypatch.setenv("PROACTIVE_TOPIC_BILI", "0")
    off = topic_seed.build(path, time.time())
    assert off["sources"]["bili"] is False
    assert not any(item["source"] == "bili" for item in off["candidates"])
    assert topic_seed.prompt_block(off) == ""


def test_topic_seed_bili_title_filter_and_recent_overlap(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / "2026-10-27.md").write_text(
        "### BV-safe\n【推广】手机号13800138000\n为什么值得：x\n", encoding="utf-8")
    path = tmp_path / "db.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
                "VALUES('P-1','care','最近在聊分布式系统', 'test',0.6,'spoken',?)",
                (time.time(),))
    con.commit()
    con.close()
    monkeypatch.setenv("BILI_DUTY_OUT_DIR", str(out))
    seed = topic_seed.build(path, time.time())
    assert not any(item["source"] == "bili" for item in seed["candidates"])


def test_simple_tick_exposes_bili_sources(tmp_path, monkeypatch):
    from agi.selflearn import proactive, simple_tick, trust

    out = tmp_path / "out"
    out.mkdir()
    (out / "2026-10-27.md").write_text(REPORT, encoding="utf-8")
    monkeypatch.setenv("BILI_DUTY_OUT_DIR", str(out))
    monkeypatch.setenv("PROACTIVE_STYLE", "simple")
    path = tmp_path / "tick.sqlite3"
    now = time.time()
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES('M-1','user','今天有点累',?,0)",
                (now - 3600,))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                ('{"daily_budget": 0}', now))
    con.commit()
    con.close()
    trust.set_value(path, 0.6)
    monkeypatch.setattr(proactive, "build_candidates", lambda p, now=None: [{
        "kind": "care", "text": "x", "reason": "t", "base": 0.5,
        "score": 0.6, "anchor": "", "topic": ""}])
    monkeypatch.setattr(proactive.initiative, "decide", lambda *a, **k: {})

    class Fake:
        def available(self):
            return True

        def chat(self, messages, **kwargs):
            return {"ok": True, "text": "看到一个说法，挺有意思"}

    row = simple_tick.tick_simple(path, client=Fake(), now=now,
                                  cooldown=0, min_score=0, respect_state=False)
    assert row["available"] is True
    assert row["topic_seed"]["sources"]["bili"] is True
    assert row["topic_seed"]["sources"]["report_date"] == "2026-10-27"
