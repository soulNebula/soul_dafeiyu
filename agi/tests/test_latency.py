# -*- coding: utf-8 -*-
"""W3-A A1：turns.latency_ms 延迟埋点测试。

覆盖：
- 主路径：正常 send 写 turn，latency_ms 非空、≥0、1 位小数，且包含模型调用耗时；
- 早退路径：撤回 / 心情问句 / 身份 / 能力 / skill gate 等 _simple_reply 路径也写值；
- 旧库兼容：手工建的无 latency_ms 列的 turns，经 db.connect 自动 ALTER 补列，
  老数据保留为 NULL，新 turn 正常写入。
"""
from __future__ import annotations

import sqlite3
import time

from agi.selflearn import db
from agi.selflearn.chat import SelfLearnAgent

OLD_TURNS_DDL = """
CREATE TABLE turns(
  id TEXT PRIMARY KEY, user_message_id TEXT NOT NULL, assistant_message_id TEXT NOT NULL,
  reply TEXT NOT NULL, skill_id TEXT, variant_id TEXT, feedback TEXT, score REAL,
  created_at REAL NOT NULL
);
"""


class SleepClient:
    """普通 selflearn_chat 调用里 sleep，用来证明 latency 覆盖了模型调用耗时。"""

    def __init__(self, reply="晚上写东西挺安静的。你一般写什么？", sleep=0.0):
        self.reply = reply
        self.sleep = sleep
        self.calls = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.calls.append(kwargs)
        if self.sleep and kwargs.get("purpose") in (
                "selflearn_chat", "selflearn_chat_vision"):
            time.sleep(self.sleep)
        return {"ok": True, "text": self.reply}


def _agent(tmp_path, name, client):
    return SelfLearnAgent(tmp_path / f"{name}.sqlite3", client=client)


def _turns(path):
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM turns ORDER BY rowid").fetchall()]
    finally:
        con.close()


def _assert_rounded(latency_ms):
    assert latency_ms is not None, "latency_ms 不能为空"
    assert isinstance(latency_ms, (int, float)) and not isinstance(latency_ms, bool)
    assert latency_ms >= 0
    assert round(float(latency_ms), 1) == float(latency_ms), latency_ms


def test_normal_send_records_latency_including_model_call(monkeypatch, tmp_path):
    monkeypatch.delenv("INNER_MODE", raising=False)
    client = SleepClient(sleep=0.02)
    agent = _agent(tmp_path, "normal", client)
    result = agent.send("我喜欢晚上写东西")
    assert result["ok"] and result["reply"]

    rows = _turns(tmp_path / "normal.sqlite3")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == result["turn_id"]
    _assert_rounded(row["latency_ms"])
    # 主路径 latency 覆盖生成：FakeClient 在生成调用里 sleep 20ms
    assert row["latency_ms"] >= 20.0, row["latency_ms"]
    assert row["latency_ms"] < 5000.0, row["latency_ms"]


def test_early_exit_simple_replies_record_latency(monkeypatch, tmp_path):
    """早退路径（simple reply / skill gate / 身份 / 心情）也必须写 latency。"""
    monkeypatch.delenv("INNER_MODE", raising=False)
    path = tmp_path / "early.sqlite3"
    agent = SelfLearnAgent(path, client=SleepClient())
    results = [
        agent.send("[对方撤回了一条消息]"),   # 撤回早退
        agent.send("你心情怎么样"),            # MOOD_QUERY 早退
        agent.send("你是什么模型"),            # 身份早退
        agent.send("帮我写一个 HTML 页面"),    # skill gate 早退
    ]
    assert all(r.get("ok") for r in results), results
    rows = {row["id"]: row for row in _turns(path)}
    assert len(rows) == len(results)
    for result in results:
        row = rows[result["turn_id"]]
        _assert_rounded(row["latency_ms"])


def test_skip_path_does_not_write_turn(monkeypatch, tmp_path):
    """skip 路径没有 assistant 回复，本就不写 turns 行（语义保持原样）。

    说明：任务描述里「skip 也要写 latency」与现状冲突——skip 直接 return，没有 turn 行，
    为避免污染 turns 表（训练/对账会把它当成一轮回复），这里只锁住「不新增 turn」。
    """
    monkeypatch.delenv("INNER_MODE", raising=False)
    path = tmp_path / "skip.sqlite3"
    from agi.selflearn import state
    data = state.load(path)
    data["self"]["mood"] = 0.1
    state.save(data, path)
    import random
    agent = SelfLearnAgent(path, client=SleepClient(), rng=random.Random(1))
    result = agent.send("在")
    assert result["skipped"] is True
    assert result["bubbles"] == []
    assert _turns(path) == []


def test_old_db_without_latency_column_is_migrated(tmp_path):
    """旧库（turns 无 latency_ms）经 db.connect 自动 ALTER 补列，老数据保留。"""
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    try:
        con.executescript(OLD_TURNS_DDL)
        con.execute(
            "INSERT INTO turns(id,user_message_id,assistant_message_id,reply,created_at) "
            "VALUES(?,?,?,?,?)",
            ("T-old", "u-old", "a-old", "旧回复", 1.0),
        )
        con.commit()
    finally:
        con.close()

    con = db.connect(path)  # 触发迁移
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(turns)").fetchall()}
        assert "latency_ms" in cols
        old = dict(con.execute(
            "SELECT * FROM turns WHERE id='T-old'").fetchone())
        assert old["latency_ms"] is None
        assert old["reply"] == "旧回复"
    finally:
        con.close()

    # 迁移后的库可以继续正常工作，新 turn 有 latency
    agent = SelfLearnAgent(path, client=SleepClient())
    result = agent.send("你好")
    assert result["ok"] is True
    con = db.connect(path)
    try:
        new = dict(con.execute(
            "SELECT latency_ms FROM turns WHERE id=?", (result["turn_id"],)).fetchone())
    finally:
        con.close()
    _assert_rounded(new["latency_ms"])
