# -*- coding: utf-8 -*-
"""P1-b：时间分层检索（hot ≤30d / warm 30–90d / cold >90d）+ 记忆渲染时间模糊。

目标：最近的事记得细，老的事知道大概；默认上下文不被陈年原文挤爆，且
prompt 里绝不出现具体日期/天数（"前阵子/更早以前"）。
"""
from __future__ import annotations

import json
import random
import re
import time

import pytest

from agi.selflearn import db, memory
from agi.selflearn import chat as chat_mod
from agi.selflearn.chat import SelfLearnAgent, filter_stale_hits

DAY = 86400.0
NOW = 1_800_000_000.0


def _mem(path, mid: str, *, content: str, kind: str = "fact",
         importance: float = 0.5, age_days: float = 0.0,
         status: str = "active", evidence: str | None = None,
         last_accessed: float | None = None) -> None:
    created = NOW - age_days * DAY
    con = db.connect(path)
    con.execute(
        "INSERT INTO user_memories(id,scope,kind,content,evidence,evidence_message_id,"
        "confidence,status,conflict_group,created_at,importance,access_count,"
        "last_accessed,evidence_history) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, "user", kind, content, evidence or content, None, 0.8, status, None,
         created, importance, 0,
         0.0 if last_accessed is None else last_accessed,
         json.dumps([evidence or content], ensure_ascii=False)))
    con.commit()
    con.close()


def _ids(items: list[dict]) -> list[str]:
    return [m["id"] for m in items]


# --------------------------------------------------------------------------- #
# 分层 / 预算
# --------------------------------------------------------------------------- #
def test_tier_of_boundaries():
    assert memory.tier_of(NOW - 5 * DAY, NOW) == "hot"
    assert memory.tier_of(NOW - 30 * DAY, NOW) == "hot"
    assert memory.tier_of(NOW - 31 * DAY, NOW) == "warm"
    assert memory.tier_of(NOW - 90 * DAY, NOW) == "warm"
    assert memory.tier_of(NOW - 91 * DAY, NOW) == "cold"


def test_tiered_budget_and_filters(tmp_path):
    path = tmp_path / "tiers.sqlite3"
    # 6 条 hot（query 全命中）
    for i in range(6):
        _mem(path, f"HOT-{i}", content=f"用户喜欢猫{i}", importance=0.6, age_days=5)
    # warm：2 条命中（1 条高重要、1 条低重要但有 overlap），1 条低重要且无 overlap（应排除）
    _mem(path, "WARM-high", content="用户喜欢猫和音乐", importance=0.6, age_days=45)
    _mem(path, "WARM-overlap", content="喜欢猫很可爱", importance=0.2, age_days=60)
    _mem(path, "WARM-low", content="用户常去爬山", importance=0.2, age_days=45)
    # cold 稳定事实 + 命中 → 候选；cold fact + 命中 → 不许进
    _mem(path, "COLD-stable1", content="用户喜欢猫", kind="preference",
         importance=0.7, age_days=200)
    _mem(path, "COLD-stable2", content="用户喜欢猫粮", kind="value",
         importance=0.8, age_days=300)
    _mem(path, "COLD-fact", content="用户喜欢猫毛", kind="fact",
         importance=0.9, age_days=200)

    result = memory.retrieve_tiered(path, "喜欢猫", now=NOW)
    assert result["tiers"]["hot"] == 4          # 预算 4
    assert result["tiers"]["warm"] == 2         # 2 条候选全收
    assert result["tiers"]["cold"] == 1         # 预算 1；只收稳定事实
    assert result["dropped"] == (6 - 4) + 1     # hot 超 2 + cold 超 1
    ok = _ids(result["items"])
    assert "COLD-fact" not in ok and "WARM-low" not in ok
    assert all(mid.startswith("HOT") for mid in ok if mid.startswith("HOT"))
    assert sum(1 for mid in ok if mid.startswith("HOT")) == 4


def test_cold_requires_query_hit_or_lookback_cue(tmp_path):
    path = tmp_path / "cold.sqlite3"
    _mem(path, "COLD-1", content="用户喜欢听音乐", kind="preference",
         importance=0.8, age_days=200)
    # 普通 query 无命中 → 老事不主动翻
    quiet = memory.retrieve_tiered(path, "今天好累", now=NOW)
    assert quiet["tiers"]["cold"] == 0
    # 明确翻旧账 → 放开（仍限稳定事实）
    look = memory.retrieve_tiered(path, "还记得以前的事吗", now=NOW)
    assert look["tiers"]["cold"] == 1
    assert _ids(look["items"]) == ["COLD-1"]


def test_warm_low_importance_without_overlap_excluded(tmp_path):
    path = tmp_path / "warm.sqlite3"
    _mem(path, "WARM-low", content="用户喜欢爬山", importance=0.2, age_days=45)
    _mem(path, "WARM-high", content="用户喜欢猫", importance=0.5, age_days=45)
    result = memory.retrieve_tiered(path, "完全无关", now=NOW)
    assert result["tiers"]["warm"] == 1
    assert _ids(result["items"]) == ["WARM-high"]


def test_dormant_excluded_and_access_updated(tmp_path):
    path = tmp_path / "dormant.sqlite3"
    _mem(path, "HOT-a", content="用户喜欢猫", age_days=5)
    _mem(path, "HOT-dormant", content="用户喜欢狗", age_days=5, status="dormant")
    result = memory.retrieve_tiered(path, "猫", now=NOW)
    assert _ids(result["items"]) == ["HOT-a"]
    rows = {r["id"]: r for r in memory.list_user_memories(path)}
    assert rows["HOT-a"]["access_count"] == 1
    assert rows["HOT-a"]["last_accessed"] == NOW
    assert rows["HOT-dormant"]["access_count"] == 0


# --------------------------------------------------------------------------- #
# 渲染：时间模糊，绝不输出日期/天数
# --------------------------------------------------------------------------- #
def test_render_memories_fuzzy_time_no_dates(tmp_path):
    path = tmp_path / "render.sqlite3"
    _mem(path, "HOT", content="用户喜欢猫", age_days=5)
    _mem(path, "WARM", content="用户喜欢音乐", age_days=45)
    _mem(path, "COLD", content="用户喜欢旅行", kind="preference",
         importance=0.8, age_days=200)
    result = memory.retrieve_tiered(path, "喜欢", now=NOW)
    lines = memory.render_memories(result["items"], now=NOW)
    text = "\n".join(lines)
    assert "[前阵子]" in text
    assert "[更早以前]" in text
    assert not re.search(r"\d+\s*(?:天|日|年|月)", text)
    assert "2026" not in text
    # hot 行保持旧格式（不带时间前缀）
    hot_line = [ln for ln in lines if "用户喜欢猫" in ln][0]
    assert hot_line.startswith("- [user/fact] 用户喜欢猫（依据：")


def test_render_accepts_plain_rows_without_tier():
    now = NOW
    rows = [{"scope": "user", "kind": "fact", "content": "用户喜欢猫",
             "evidence": "我喜欢猫", "created_at": now - 45 * DAY}]
    line = memory.render_memories(rows, now=now)[0]
    assert "前阵子" in line and "天" not in line


# --------------------------------------------------------------------------- #
# 兼容：retrieve 旧签名（只取 hot 池 + 旧的重叠过滤）
# --------------------------------------------------------------------------- #
def test_retrieve_legacy_signature_and_semantics(tmp_path):
    path = tmp_path / "compat.sqlite3"
    _mem(path, "HOT", content="用户喜欢猫", age_days=5)
    _mem(path, "COLD", content="用户喜欢音乐", kind="preference",
         importance=0.8, age_days=200)
    mems = memory.retrieve(path, "猫", k=3)
    assert [m["id"] for m in mems] == ["HOT"]        # 只取 hot 池
    assert memory.retrieve(path, "")                  # 无 query 也能返回 hot
    # 旧签名位置参数仍然可用
    assert memory.retrieve(path, "猫", 3)[0]["id"] == "HOT"


# --------------------------------------------------------------------------- #
# chat 集成：hybrid 老片段过滤 + prompt 时间模糊 + 预算截断
# --------------------------------------------------------------------------- #
class FakeClient:
    def available(self):
        return True

    def chat(self, messages, **kwargs):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        if "记忆系统" in text or "用户记忆抽取器" in text:
            return {"ok": True, "text": json.dumps({"items": []})}
        return {"ok": True, "text": "嗯"}


def _fake_agent(tmp_path):
    path = tmp_path / "agent.sqlite3"
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(7))
    return path, agent


def test_filter_stale_hits_drops_old_memory_and_message(tmp_path):
    path = tmp_path / "hybrid.sqlite3"
    now = time.time()
    _mem(path, "MEM-old", content="用户喜欢猫", age_days=200)
    _mem(path, "MEM-new", content="用户喜欢狗", age_days=5)
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-old", "user", "以前很喜欢猫", now - 200 * DAY))
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-new", "user", "今天聊猫", now - 1 * DAY))
    con.commit()
    con.close()
    hits = [{"source_type": "memory", "source_id": "MEM-old", "text": "老记忆"},
            {"source_type": "memory", "source_id": "MEM-new", "text": "新记忆"},
            {"source_type": "message", "source_id": "M-old", "text": "老消息"},
            {"source_type": "claim", "source_id": "C-1", "text": "claim 不参与过滤"}]
    kept, dropped = filter_stale_hits(path, hits, "你好", now=now)
    assert dropped == 2
    assert {h["source_id"] for h in kept} == {"MEM-new", "C-1"}
    # 用户明确翻旧账 → 全保留
    kept2, dropped2 = filter_stale_hits(path, hits, "还记得以前的事吗", now=now)
    assert dropped2 == 0 and len(kept2) == 4


def test_chat_prompt_fuzzy_time_and_budget(tmp_path, monkeypatch):
    path, agent = _fake_agent(tmp_path)
    near = time.time()
    for i in range(6):
        _mem(path, f"HOT-{i}", content=f"用户喜欢猫{i}", age_days=5,
             last_accessed=0.0)
        # 修正 created_at 用真实 now 基准（_mem 用固定 NOW，这里重写）
        con = db.connect(path)
        con.execute("UPDATE user_memories SET created_at=? WHERE id=?",
                    (near - 5 * DAY, f"HOT-{i}"))
        con.commit(); con.close()
    _mem(path, "WARM-1", content="用户喜欢音乐", age_days=45)
    _mem(path, "COLD-1", content="用户喜欢旅行", kind="preference",
         importance=0.8, age_days=200)
    con = db.connect(path)
    for mid, age in (("WARM-1", 45), ("COLD-1", 200)):
        con.execute("UPDATE user_memories SET created_at=? WHERE id=?",
                    (near - age * DAY, mid))
    con.commit(); con.close()

    captured: dict = {}

    def fake_generate(client, system, prompt, memories, **kwargs):
        captured["prompt"] = prompt
        captured["mems"] = list(memories)
        return {"ok": True, "text": "嗯"}, {"two_stage": False, "claims": []}

    monkeypatch.setattr(chat_mod.generation, "generate", fake_generate)
    result = agent.send("还记得以前的事吗")
    assert result["ok"] and captured.get("prompt")

    prompt = captured["prompt"]
    section = prompt.split("长期记忆", 1)[1].split("用户画像", 1)[0]
    assert "[更早以前]" in section          # cold 被模糊成"更早以前"
    assert "[前阵子]" in section            # warm
    assert not re.search(r"\d+\s*(?:天|日|年|月)", section)
    mem_lines = [ln for ln in section.splitlines() if ln.startswith("- [")]
    assert len(mem_lines) <= 7             # hot4 + warm2 + cold1 总预算
    assert len([ln for ln in mem_lines if "[前阵子]" not in ln
                and "[更早以前]" not in ln]) <= 4   # hot 预算 4
    assert "retrieval_dropped_stale" in result
