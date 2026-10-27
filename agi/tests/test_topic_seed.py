# -*- coding: utf-8 -*-
"""P8-b：simple 链路话题种子（候选/过滤/prompt/开关）。"""
from __future__ import annotations

import json
import time

import pytest

from agi.selflearn import db, simple_tick, topic_seed


def _seed(path, *, memories=(), claims=(), messages=(), proactive=(), now=None):
    now = time.time() if now is None else float(now)
    con = db.connect(path)
    try:
        for index, content in enumerate(memories):
            con.execute(
                "INSERT OR REPLACE INTO user_memories"
                "(id,scope,kind,content,evidence,confidence,status,created_at,"
                "importance) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"UM-{index}", "user", "preference", content, "原话", 0.9,
                 "active", now - 3600 - index, 0.9))
        for index, obj in enumerate(claims):
            con.execute(
                "INSERT INTO claims(id,subject,predicate,object,polarity,time_ref,"
                "claim_type,evidence_span,confidence,source_level,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"C-{index}", "user", "在做", obj, 1, "", "fact", "原话", 0.9,
                 "A", "active", now - 7200 - index))
        for index, (role, text, ts) in enumerate(messages):
            con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                        (f"M-{index}", role, text, ts))
        for index, text in enumerate(proactive):
            con.execute("INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (f"P-{index}", "care", text, "test", 0.6, "spoken", now - 60))
        con.commit()
    finally:
        con.close()


def test_topic_seed_from_memory_claims_and_messages(tmp_path):
    now = time.time()
    path = tmp_path / "topics.sqlite3"
    _seed(path, now=now,
          memories=("喜欢喝手冲咖啡", "在整理旧资料"),
          claims=("最近在学新的项目管理方法",),
          messages=(("user", "最近在整理旧资料，越整理越乱", now - 48 * 3600),))
    seed = topic_seed.build(path, now)
    assert 2 <= len(seed["candidates"]) <= 3
    assert seed["picked"]
    assert {item["source"] for item in seed["candidates"]} & {"memory", "claim", "message"}
    block = topic_seed.prompt_block(seed)
    assert "可自然提起的旧话题" in block
    assert "最多提 1 个" in block
    assert all("我记得你说过" not in item["text"] for item in seed["candidates"])


def test_topic_seed_empty_and_claims_only(tmp_path, monkeypatch):
    # 隔离真实 B 站日报：否则空库也会从 ~/.dsh/bili-duty/out 读到当天报告，
    # 导致"空库应无候选"的断言随日期/真实数据变化（2026-10-28 踩到）。
    from agi.integrations import bili_digest
    monkeypatch.setattr(bili_digest, "DEFAULT_DIR", tmp_path / "_no_bili", raising=False)
    monkeypatch.setattr(bili_digest, "latest_topics", lambda *a, **k: [], raising=False)
    monkeypatch.setenv("PROACTIVE_TOPIC_BILI", "0")
    now = time.time()
    empty = tmp_path / "empty.sqlite3"
    db.connect(empty).close()
    seed = topic_seed.build(empty, now)
    assert seed["candidates"] == [] and seed["picked"] == ""
    assert topic_seed.prompt_block(seed) == ""

    claims = tmp_path / "claims.sqlite3"
    _seed(claims, now=now, claims=("最近在学做咖啡",))
    seed = topic_seed.build(claims, now)
    assert any(item["source"] == "claim" for item in seed["candidates"])


def test_topic_seed_filters_sensitive_physical_and_recent(tmp_path):
    now = time.time()
    path = tmp_path / "filter.sqlite3"
    _seed(path, now=now,
          memories=("我的手机号是13800138000", "密码是 abc123",
                    "我下楼买菜的时候顺便走了一圈", "喜欢喝手冲咖啡",
                    "在整理旧资料，越整理越乱"),
          proactive=("今天还在喝手冲咖啡，挺舒服",))
    seed = topic_seed.build(path, now)
    texts = [item["text"] for item in seed["candidates"]]
    assert not any("13800138000" in text for text in texts)
    assert not any("密码" in text for text in texts)
    assert not any("买菜" in text or "下楼" in text for text in texts)
    # 与最近 7 天主动消息重复（≥3 个二字符词）的“喜欢喝手冲咖啡”被过滤
    assert "喜欢喝手冲咖啡" not in texts


def test_topic_prompt_only_in_simple_and_switch(tmp_path, monkeypatch):
    now = time.time()
    path = tmp_path / "prompt.sqlite3"
    _seed(path, now=now, memories=("喜欢喝手冲咖啡",)
          )
    seed = topic_seed.build(path, now)
    ctx = topic_seed.prompt_block(seed)
    base = simple_tick._simple_prompt("care", "user:今天有点累", "")
    injected = simple_tick._simple_prompt("care", "user:今天有点累", "", topic_ctx=ctx)
    assert ctx and "可自然提起的旧话题" in injected
    assert "可自然提起的旧话题" not in base
    monkeypatch.setenv("PROACTIVE_TOPIC_SEED", "0")
    assert topic_seed.enabled() is False


def test_tick_returns_topic_seed_and_prompt():
    # 详细 tick 行为用 test_proactive_simple 的 DB seed 太重；这里验证 prompt 注入即可。
    seed = {"picked": "喜欢喝手冲咖啡", "candidates": [
        {"text": "喜欢喝手冲咖啡", "source": "memory", "label": "用户之前提过"}],
        "reason": "selected:memory"}
    ctx = topic_seed.prompt_block(seed)
    prompt = simple_tick._simple_prompt("care", "user:hi", "", topic_ctx=ctx)
    assert "喜欢喝手冲咖啡" in prompt
    assert "最多提 1 个" in prompt
