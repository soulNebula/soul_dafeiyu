# -*- coding: utf-8 -*-
"""P12-B：兴趣画像（加权/归一/衰减/检索词/自提/重建/接线）。"""
from __future__ import annotations

import json
import time

import pytest

from agi.selflearn import db, interests


def _topics(monkeypatch, values):
    calls = []
    def fake_chat(purpose, messages, **kwargs):
        calls.append(purpose)
        return {"ok": True, "text": json.dumps({"topics": values}, ensure_ascii=False),
                "backend": "local", "usage": {}, "latency_ms": 1.0}
    monkeypatch.setattr(interests, "llm_chat", fake_chat)
    monkeypatch.setenv("INTERESTS_USE_LLM", "1")
    return calls


def _path(tmp_path):
    return tmp_path / "db.sqlite3"


def test_observe_source_weights_and_cap(tmp_path, monkeypatch):
    path = _path(tmp_path)
    for source, expected in (("chat", 0.15), ("video", 0.09),
                             ("self", 0.06), ("recommend", 0.045)):
        _topics(monkeypatch, ["AI"])
        if db.memory_path(path).exists():
            con = db.connect(path)
            con.execute("DELETE FROM interests")
            con.execute("DELETE FROM interest_events")
            con.commit(); con.close()
        result = interests.observe(["人工智能 测试"], source=source, path=path,
                                   use_llm=True)
        rows = interests.top_topics(10, path=path)
        assert rows and abs(rows[0]["weight"] - expected) < 1e-9, (source, rows)
        assert result["delta"] <= 0.15
    # 同一 topic 两次 observe：每次增量 ≤0.15
    con = db.connect(path)
    con.execute("DELETE FROM interests")
    con.execute("DELETE FROM interest_events")
    con.commit(); con.close()
    _topics(monkeypatch, ["AI"])
    interests.observe(["AI"], source="chat", path=path, use_llm=True)
    interests.observe(["AI"], source="chat", path=path, use_llm=True)
    row = interests.top_topics(10, path=path)[0]
    assert abs(row["weight"] - 0.30) < 1e-9


def test_normalize_synonyms_and_new_limit(tmp_path, monkeypatch):
    assert interests.normalize("人工智能") == "ai"
    assert interests.normalize("AI") == "ai"
    path = _path(tmp_path)
    _topics(monkeypatch, ["人工智能", "AI", "机器学习", "写代码", "手冲咖啡",
                          "游戏", "Rust", "数据库"])
    result = interests.observe(["x"], source="chat", path=path, use_llm=True)
    rows = interests.top_topics(20, path=path)
    assert len(rows) == 5                      # 最多新增 5 个
    assert result["new"] == 5
    names = {item["topic"] for item in rows}
    assert "ai" in names and "b站" not in names


def test_decay_half_life_and_floor(tmp_path):
    now = time.time()
    path = _path(tmp_path)
    con = db.connect(path)
    con.execute("INSERT INTO interests(id,topic,weight,source,evidence_json,hits,"
                "last_seen_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("I1", "ai", 0.5, "chat", "[]", 3, now - 7 * 86400,
                 now - 7 * 86400, now - 7 * 86400))
    con.execute("INSERT INTO interests(id,topic,weight,source,evidence_json,hits,"
                "last_seen_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("I2", "game", 0.1, "chat", "[]", 1, now, now, now))
    con.commit(); con.close()
    interests.decay(path=path, now=now)
    rows = {item["topic"]: item for item in interests.top_topics(10, path=path)}
    assert abs(rows["ai"]["weight"] - 0.3) < 1e-6
    assert rows["ai"]["hits"] == 3
    assert abs(rows["game"]["weight"] - 0.1) < 1e-6


def test_search_keywords_and_propose(tmp_path, monkeypatch):
    path = _path(tmp_path)
    now = time.time()
    con = db.connect(path)
    for index, (topic, weight, hits, last) in enumerate([
            ("ai", 0.8, 1, now),
            ("游戏", 0.6, 0, now - 2 * 86400),
            ("咖啡", 0.4, 10, now)]):
        con.execute("INSERT INTO interests(id,topic,weight,source,evidence_json,hits,"
                    "last_seen_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"I{index}", topic, weight, "chat", "[]", hits, last, last, last))
    con.commit(); con.close()
    keywords = interests.search_keywords(3, path=path, now=now)
    assert keywords and len(keywords) <= 3
    assert "咖啡" not in keywords[:1]  # 已探索多，降权

    calls = _topics(monkeypatch, ["量子计算"])
    proposed = interests.propose_new(2, path=path, now=now)
    assert calls == ["persona_reflect"]
    assert proposed == ["量子计算"]
    rows = {item["topic"]: item for item in interests.top_topics(10, path=path)}
    assert "量子计算" in rows


def test_rebuild_idempotent(tmp_path):
    path = _path(tmp_path)
    now = time.time()
    con = db.connect(path)
    con.execute("INSERT INTO bili_impressions"
                "(id,bvid,title,topics_json,created_at) VALUES(?,?,?,?,?)",
                ("BI-1", "BV1", "视频", json.dumps(["AI", "大模型"]), now - 3600))
    con.execute("INSERT INTO user_memories(id,scope,kind,content,evidence,confidence,"
                "status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("UM-1", "user", "preference", "我在学分布式系统", "原话", 0.9,
                 "active", now - 7200))
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", "最近在看电影和游戏", now - 3600))
    con.commit(); con.close()
    first = interests.rebuild_from_history(30, path=path, now=now)
    topics1 = interests.top_topics(20, path=path)
    second = interests.rebuild_from_history(30, path=path, now=now)
    topics2 = interests.top_topics(20, path=path)
    assert first["topics"] == second["topics"]
    assert [(t["topic"], round(t["weight"], 6)) for t in topics1] == \
           [(t["topic"], round(t["weight"], 6)) for t in topics2]
    assert topics1


def test_cli_smoke(tmp_path, capsys):
    path = _path(tmp_path)
    con = db.connect(path)
    con.execute("INSERT INTO interests(id,topic,weight,source,evidence_json,hits,"
                "last_seen_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("I1", "ai", 0.5, "chat", "[]", 1, time.time(), time.time(), time.time()))
    con.commit(); con.close()
    assert interests.main(["--show", "--path", str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["topics"][0]["topic"] == "ai"
    assert interests.main(["--decay", "--path", str(path)]) == 0
    assert interests.main(["--rebuild", "--path", str(path)]) == 0


def test_bili_ingest_observes_video(monkeypatch, tmp_path):
    from agi.integrations import bili_ingest

    monkeypatch.setenv("BILI_FEED_INTERESTS", "1")
    monkeypatch.setattr(bili_ingest, "fetch_video", lambda bvid, **kw: {
        "ok": True, "bvid": bvid, "title": "视频", "owner": "UP", "owner_mid": "1",
        "duration_s": 1, "cover_url": "", "description": "", "tags": [],
        "zone": "", "subtitle": "字幕", "subtitle_missing": False,
        "subtitle_reason": "", "comments": [], "danmaku": [],
        "view": 0, "like": 0, "coin": 0, "favorite": 0,
    })
    class FakeChat:
        def __call__(self, purpose, messages, **kwargs):
            data = {"keyword_extract": {"topics": ["AI"]},
                    "ingest_summary": {"summary": "s", "emotion": "neutral"},
                    "video_understanding": {"value": 0.8, "why": "w", "my_take": "我"},
                    "share_copy": {"share_line": "分享"}}[purpose]
            return {"ok": True, "text": json.dumps(data), "backend": "api",
                    "usage": {}, "latency_ms": 1}
    monkeypatch.setattr(bili_ingest, "llm_chat", FakeChat())
    called = []
    monkeypatch.setattr(interests, "observe",
                        lambda texts, **kw: called.append((list(texts), kw)) or {"ok": True})
    bili_ingest.ingest("BV1OBS", path=tmp_path / "db.sqlite3")
    assert called and called[0][0] == ["AI"]
    assert called[0][1]["source"] == "video"


def test_chat_observes_every_five_turns(monkeypatch, tmp_path):
    import json as _json
    import random
    from agi.selflearn.chat import SelfLearnAgent

    class Fake:
        def available(self):
            return True

        def chat(self, messages, **kwargs):
            text = "\n".join(str(m.get("content") or "") for m in messages)
            if "记忆抽取器" in text or "用户记忆" in text:
                return {"ok": True, "text": "[]"}
            return {"ok": True, "text": "好的，收到"}

    called = []
    monkeypatch.setattr(interests, "observe",
                        lambda texts, **kw: called.append(kw) or {"ok": True})
    agent = SelfLearnAgent(tmp_path / "chat.sqlite3", client=Fake(),
                           rng=random.Random(1))
    for index in range(5):
        agent.send(f"第{index}条消息")
    assert len(called) == 1
    assert called[0]["source"] == "chat"
