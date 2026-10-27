# -*- coding: utf-8 -*-
"""主动行为每日预算测试。"""
from __future__ import annotations

import time

from agi.selflearn import db, proactive


def test_proactive_daily_budget_blocks_after_limit(tmp_path):
    path = tmp_path / "p.sqlite3"
    con = db.connect(path)
    now = time.time()
    for i in range(proactive.DAILY_BUDGET):
        con.execute("INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (f"P-{i}", "check_in", "在吗", "test", 0.8, "spoken", now - 1000 - i * 60))
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-1", "user", "hi", now - 3600, 0))
    con.commit(); con.close()
    r = proactive.tick(path, client=None, now=now + 10)
    assert r["available"] is False
    assert r["reason"] == "今日主动预算已用完"


class _SayClient:
    def __init__(self, text):
        self.text = text

    def available(self):
        return True

    def chat(self, *a, **k):
        return {"ok": True, "text": self.text}


def _seed(path, *, ts_age=120):
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-1", "user", "最近在忙项目", time.time() - ts_age, 0))
    con.commit()
    con.close()


def test_llm_can_say_skip(tmp_path):
    path = tmp_path / "p.sqlite3"
    _seed(path)
    r = proactive.tick(path, client=_SayClient("SKIP"), cooldown=0,
                       respect_state=False, min_score=0)
    assert r["available"] is False and r["reason"] == "llm_skip"


def test_llm_can_speak_even_below_score_and_state(tmp_path, monkeypatch):
    # 固定随机数：完整套件里 random 状态不同会让概率闸门偶发跳过（order-dependent flake）
    import random as _random
    monkeypatch.setattr(_random, "random", lambda: 0.0)
    path = tmp_path / "p.sqlite3"
    _seed(path)
    con = db.connect(path)
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('self_state',?,?)",
                ('{"self": {"mood": 0.1, "sharing": 0.05, "energy": 0.1}}', time.time()))
    con.commit(); con.close()
    r = proactive.tick(path, client=_SayClient("今天有点想你，在忙吗？"), cooldown=0,
                       respect_state=False, min_score=0)
    assert r["available"] is True
    assert r["message"] == "今天有点想你，在忙吗？"


def test_cooldown_zero_allows_immediate_repeat(tmp_path):
    path = tmp_path / "p.sqlite3"
    _seed(path)
    r1 = proactive.tick(path, client=_SayClient("第一句"), cooldown=0,
                        respect_state=False, min_score=0)
    assert r1["available"] is True
    r2 = proactive.tick(path, client=_SayClient("第二句"), cooldown=0,
                        respect_state=False, min_score=0)
    # P0-14 风格层可能给消息加开头/感受前缀，但内容必须还是第二句
    assert r2["available"] is True and "第二句" in r2["message"]
