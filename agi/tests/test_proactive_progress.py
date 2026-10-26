# -*- coding: utf-8 -*-
"""P0-15：用户待办/进度追问（find_user_todos + progress_check）。"""
from __future__ import annotations

import json
import time

import pytest

from agi.selflearn import db, proactive, simple_tick, trust

T0 = time.time()


class FakeClient:
    def __init__(self, queue=None, text=""):
        self.queue = list(queue or [])
        self.text = text
        self.prompts = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.prompts.append(str(messages[-1].get("content") or ""))
        if self.queue:
            return {"ok": True, "text": self.queue.pop(0)}
        return {"ok": True, "text": self.text}


def _seed(tmp_path, *, messages=(), memories=(), proactive_rows=(), name="p.sqlite3"):
    path = tmp_path / name
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES('M-base','user','今天有点累',?,0)",
                (T0 - 3600,))
    for index, (text, ts) in enumerate(messages):
        con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                    (f"M-{index}", "user", text, ts))
    for index, text in enumerate(memories):
        con.execute(
            "INSERT INTO user_memories(id,scope,kind,content,evidence,confidence,status,"
            "created_at,importance) VALUES(?,?,?,?,?,?,?,?,?)",
            (f"UM-{index}", "user", "plan", text, "原话", 0.9, "active", T0 - 7200, 0.9))
    for index, (kind, text, created) in enumerate(proactive_rows):
        con.execute("INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (f"P-{index}", kind, text, "test", 0.6, "spoken", created))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}), T0))
    con.commit()
    con.close()
    trust.set_value(path, 0.6)
    return path


def test_find_user_todos_interview_and_move(tmp_path):
    path = _seed(tmp_path, messages=[
        ("下周三要去面试", T0 - 2 * 3600),
        ("这周末要搬家", T0 - 3 * 3600),
    ], memories=["最近在准备考研"])
    todos = proactive.find_user_todos(path, T0)
    anchors = [item["anchor"] for item in todos]
    assert "面试" in anchors
    assert "搬家" in anchors
    assert "考研" in anchors
    assert all(item["msg_id"] for item in todos)
    assert all(len(item["anchor"]) <= 12 for item in todos)
    assert {item["source"] for item in todos} <= {"memory", "message"}


def test_find_user_todos_skips_asked_anchor(tmp_path):
    path = _seed(tmp_path, messages=[("下周三要去面试", T0 - 3600),
                                     ("这周末要搬家", T0 - 7200)],
                 proactive_rows=[("progress_check", "面试准备得怎么样了", T0 - 86400)])
    todos = proactive.find_user_todos(path, T0)
    anchors = [item["anchor"] for item in todos]
    assert "面试" not in anchors       # 7 天内问过
    assert "搬家" in anchors


def test_progress_check_selected_and_anchor_required(tmp_path, monkeypatch):
    path = _seed(tmp_path, messages=[("下周三要去面试", T0 - 3600)])
    monkeypatch.setattr(proactive, "PROGRESS_CHECK_PROB", 1.0)
    monkeypatch.setenv("INNER_MODE", "off")
    monkeypatch.setenv("PROACTIVE_INNER", "off")

    def choose_progress(candidates, path=None, now=None):
        return next(c for c in candidates if c["kind"] == "progress_check")

    monkeypatch.setattr(proactive, "choose_candidate", choose_progress)
    client = FakeClient(queue=["最近怎么样", "面试准备得怎么样"])
    row = proactive.tick(path, client=client, now=T0, cooldown=0, min_score=0,
                         respect_state=False)
    assert row["available"] is True
    assert row["kind"] == "progress_check"
    assert "面试" in row["message"]
    assert row["progress_check"]["anchor"] == "面试"
    assert len(client.prompts) >= 2  # 第一次无 anchor，被重生成


def test_no_todo_means_no_progress_candidate(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    monkeypatch.setattr(proactive, "PROGRESS_CHECK_PROB", 1.0)
    candidates = proactive.build_candidates(path, T0)
    assert not any(item["kind"] == "progress_check" for item in candidates)
    assert proactive.find_user_todos(path, T0) == []


def test_simple_chain_progress_anchor(tmp_path, monkeypatch):
    path = _seed(tmp_path, messages=[("这周末要搬家", T0 - 3600)])
    monkeypatch.setenv("PROACTIVE_STYLE", "simple")
    monkeypatch.setenv("INNER_MODE", "off")
    monkeypatch.setenv("PROACTIVE_INNER", "off")
    monkeypatch.setattr(proactive, "PROGRESS_CHECK_PROB", 1.0)

    def choose_progress(candidates, path=None, now=None):
        return next(c for c in candidates if c["kind"] == "progress_check")

    monkeypatch.setattr(proactive, "choose_candidate", choose_progress)
    client = FakeClient(queue=["最近怎么样", "搬家那事顺利吗"])
    row = simple_tick.tick_simple(path, client=client, now=T0, cooldown=0,
                                 min_score=0, respect_state=False)
    assert row["available"] is True
    assert row["kind"] == "progress_check"
    assert "搬家" in row["message"]
    assert row["progress_check"]["anchor"] == "搬家"


def test_same_todo_not_asked_twice_within_7_days(tmp_path, monkeypatch):
    path = _seed(tmp_path, messages=[("下周三要去面试", T0 - 3600)])
    monkeypatch.setattr(proactive, "PROGRESS_CHECK_PROB", 1.0)
    monkeypatch.setenv("INNER_MODE", "off")
    monkeypatch.setenv("PROACTIVE_INNER", "off")

    original = proactive.choose_candidate

    def choose_progress(candidates, path=None, now=None):
        return next((c for c in candidates if c["kind"] == "progress_check"),
                    original(candidates, path, now))

    monkeypatch.setattr(proactive, "choose_candidate", choose_progress)
    first = proactive.tick(path, client=FakeClient(text="面试准备得怎么样"), now=T0,
                           cooldown=0, min_score=0, respect_state=False)
    assert first["available"] is True
    second = proactive.tick(path, client=FakeClient(text="面试准备得怎么样"), now=T0 + 60,
                            cooldown=0, min_score=0, respect_state=False)
    assert "progress_check" not in str(second.get("kind") or "")

    def choose_missing(candidates, path=None, now=None):
        return next((c for c in candidates if c["kind"] == "progress_check"), None)

    monkeypatch.setattr(proactive, "choose_candidate", choose_missing)
    # 7 天内同一 anchor 已问过 → build_candidates 里不再有 progress_check
    assert not any(item["kind"] == "progress_check"
                   for item in proactive.build_candidates(path, T0 + 60))
