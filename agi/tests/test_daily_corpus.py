# -*- coding: utf-8 -*-
"""P30 每日语料流水线测试：tmp 库、规则标注、过滤、门禁（不联网/不训练）。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from tools.train import daily_corpus, daily_summary, train_gate

DATE = "2026-01-02"


def _ts(hour: int) -> float:
    base = datetime.strptime(DATE, "%Y-%m-%d").timestamp()
    return base + hour * 3600


@pytest.fixture
def corpus_env(tmp_path, monkeypatch):
    selflearn = tmp_path / "selflearn"
    contacts = selflearn / "wechat_contacts"
    contacts.mkdir(parents=True)
    db_path = contacts / "abc123.memory.sqlite3"
    con = sqlite3.connect(db_path)
    con.executescript("""
        create table messages (id text primary key, role text, text text, ts real);
        create table turns (id text primary key, user_message_id text,
            assistant_message_id text, reply text, created_at real);
        create table turn_traces (id text primary key, ts real, contact text,
            user_text text, system_prompt text, final_reply text,
            skipped integer, crisis integer, inner_json text, proactive_json text);
        create table proactive_events (id text primary key, ts real, text text, status text);
    """)
    rows = [
        # id, user, system, reply, crisis, skipped, inner
        ("T-pos", "今天好累", "SYS", "累就早点歇着", 0, 0, '{"mood":0.4}'),
        ("T-neg", "嗯 我准备洗个澡睡了", "SYS", "好的", 0, 0, '{"mood":0.5}'),
        ("T-cor", "好生硬，别这样", "SYS", "那我换个说法", 0, 0, '{"mood":0.5}'),
        ("T-cor-good", "不是，我是说想吃你做的面", "SYS", "我哪会做面啊 你教我", 0, 0, '{"mood":0.6}'),
        ("T-crisis", "我想死", "SYS", "别这样想", 1, 0, '{"mood":0.1}'),
        ("T-sticker", "发个表情", "SYS", "[[media:sticker:666]]", 0, 0, '{"mood":0.5}'),
        ("T-test", "文件传输助手", "SYS", "收到", 0, 0, '{"mood":0.5}'),
    ]
    for index, (turn_id, user, system, reply, crisis, skipped, inner) in enumerate(rows):
        con.execute("insert into turn_traces values (?,?,?,?,?,?,?,?,?,?)",
                    (turn_id, _ts(9 + index), "abc123", user, system, reply,
                     skipped, crisis, inner, "{}"))
        con.execute("insert into turns values (?,?,?,?,?)",
                    (turn_id, "", "", reply, _ts(9 + index)))
    con.commit()
    con.close()

    monkeypatch.setattr(daily_corpus, "DATA_ROOT", selflearn)
    monkeypatch.setattr(daily_corpus, "CONTACTS_DIR", contacts)
    monkeypatch.setattr(daily_corpus, "DAILY_DIR", tmp_path / "train" / "daily")
    monkeypatch.setattr(daily_corpus, "DPO_DIR", tmp_path / "train" / "dpo")
    monkeypatch.setattr(daily_summary, "DAILY_DIR", tmp_path / "train" / "daily")
    monkeypatch.setattr(train_gate, "DAILY_DIR", tmp_path / "train" / "daily")
    monkeypatch.setattr(train_gate, "STAGED_DIR", tmp_path / "train" / "staged")
    monkeypatch.setattr(train_gate, "HISTORY_PATH", tmp_path / "train" / "history.jsonl")
    return {"tmp": tmp_path, "db": db_path}


def test_daily_corpus_labels_filters_and_outputs(corpus_env):
    result = daily_corpus.run(DATE)
    assert result["sft"] >= 3
    assert result["dpo"] >= 1
    rows = [json.loads(line) for line in
            Path(result["sft_path"]).read_text(encoding="utf-8").splitlines()]
    qualities = {row["meta"]["quality"] for row in rows}
    assert "positive" in qualities
    assert "negative" in qualities
    assert "correction" in qualities
    assert all(set(row) >= {"messages", "response", "meta"} for row in rows)
    assert all(isinstance(row["messages"], list) and len(row["messages"]) == 2 for row in rows)
    # 敏感/纯贴纸/测试联系人被过滤
    users = [row["messages"][1]["content"] for row in rows]
    assert not any("我想死" in user or "文件传输助手" in user for user in users)
    assert not any("[[media:sticker" in row["response"] for row in rows)
    report = Path(result["report_path"]).read_text(encoding="utf-8")
    assert "每日语料日报" in report and "可训练正/修正" in report
    dpo = [json.loads(line) for line in
           Path(result["dpo_path"]).read_text(encoding="utf-8").splitlines()]
    assert dpo and dpo[0]["chosen"] == "我哪会做面啊 你教我"
    assert dpo[0]["rejected"] == "那我换个说法"


def test_summary_rule_fallback(corpus_env):
    daily_corpus.run(DATE)
    out = daily_summary.run(DATE, use_llm=False)
    assert out["used"] == "rule"
    text = Path(out["path"]).read_text(encoding="utf-8")
    assert "每日语气总结" in text
    assert "像人" in text and "出戏" in text


def test_gate_not_ready_then_ready(corpus_env):
    ready = train_gate.check_ready(now=datetime(2026, 1, 2))
    assert ready["ready"] is False and "samples<300" in ready["reasons"]
    # 造 300 条增量
    daily = corpus_env["tmp"] / "train" / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    rows = [{"messages": [{"role": "system", "content": "S"},
                          {"role": "user", "content": "u"}],
             "response": "r", "meta": {"quality": "positive", "date": DATE}}
            for _ in range(300)]
    (daily / f"{DATE}.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8")
    ready = train_gate.check_ready(now=datetime(2026, 1, 10))
    assert ready["ready"] is True and ready["samples"] == 300
    # 最近刚训练过 → 间隔不足
    history = corpus_env["tmp"] / "train" / "history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(json.dumps({"date": "2026-01-09", "version": "v1",
                                   "status": "trained", "score": 80}) + "\n",
                       encoding="utf-8")
    ready = train_gate.check_ready(now=datetime(2026, 1, 10))
    assert ready["ready"] is False and any("days_since" in reason for reason in ready["reasons"])


def test_gate_candidate_and_no_auto_switch(corpus_env):
    history = corpus_env["tmp"] / "train" / "history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(json.dumps({"date": "2026-01-01", "version": "v1",
                                   "status": "trained", "score": 90}) + "\n",
                       encoding="utf-8")
    good = train_gate.gate("v2", score=89, history_path=history)
    assert good["candidate"] is True and good["switched"] is False
    bad = train_gate.gate("v3", score=87, history_path=history)
    assert bad["candidate"] is False and bad["switched"] is False
    entries = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
    assert entries[-1]["status"] == "rejected"


def test_train_once_not_ready_does_not_train(corpus_env):
    plan = train_gate.train_once(dry_run=True)
    assert plan["ok"] is False and plan["status"] == "not_ready"
    assert "command" not in plan
