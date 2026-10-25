# -*- coding: utf-8 -*-
"""P1-e：统一时间模糊层（timefuzz 唯一真源）+ prompt 绝对时间审计。

规则：任何注入 prompt 的字符串都不得出现绝对日期或精确天数；
日志/导出/DB 可以保留绝对时间。
"""
from __future__ import annotations

import json
import random
import time

import pytest

from agi.selflearn import db, memory, personality, retrieval, search, summary
from agi.selflearn import chat as chat_mod
from agi.selflearn import timefuzz
from agi.selflearn.chat import SelfLearnAgent

DAY = 86400.0
NOW = 1_800_000_000.0


# --------------------------------------------------------------------------- #
# relative() 7 档边界（含右不含左）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("age_s,label", [
    (0.0, timefuzz.LABEL_JUST_NOW),
    (timefuzz.TIER_1H_S, timefuzz.LABEL_JUST_NOW),
    (timefuzz.TIER_1H_S + 1, timefuzz.LABEL_TODAY),
    (timefuzz.TIER_1D_S, timefuzz.LABEL_TODAY),
    (timefuzz.TIER_1D_S + 1, timefuzz.LABEL_TWO_DAYS),
    (timefuzz.TIER_3D_S, timefuzz.LABEL_TWO_DAYS),
    (timefuzz.TIER_3D_S + 1, timefuzz.LABEL_FEW_DAYS),
    (timefuzz.TIER_7D_S, timefuzz.LABEL_FEW_DAYS),
    (timefuzz.TIER_7D_S + 1, timefuzz.LABEL_RECENT),
    (timefuzz.TIER_30D_S, timefuzz.LABEL_RECENT),
    (timefuzz.TIER_30D_S + 1, timefuzz.LABEL_A_WHILE),
    (timefuzz.TIER_90D_S, timefuzz.LABEL_A_WHILE),
    (timefuzz.TIER_90D_S + 1, timefuzz.LABEL_EARLIER),
    (timefuzz.TIER_365D_S, timefuzz.LABEL_EARLIER),
    (timefuzz.TIER_365D_S + 1, timefuzz.LABEL_LONG_AGO),
])
def test_relative_tier_boundaries(age_s, label):
    assert timefuzz.relative(NOW - age_s, now=NOW) == label


def test_relative_edges_do_not_crash():
    assert timefuzz.relative(NOW + 100.0, now=NOW) == timefuzz.LABEL_JUST_NOW  # 未来
    assert timefuzz.relative(None) == ""
    assert timefuzz.relative("not-a-ts") == ""
    # 同秒多次调用稳定
    assert len({timefuzz.relative(NOW - 45 * DAY, now=NOW) for _ in range(5)}) == 1


# --------------------------------------------------------------------------- #
# strip_absolute / has_absolute
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", [
    "2026年10月25日",
    "10月25日",
    "2026-10-25",
    "3 天前",
    "昨天",
])
def test_strip_absolute_covers_common_forms(raw):
    cleaned = timefuzz.strip_absolute(f"我记得{raw}说过这件事")
    assert timefuzz.has_absolute(cleaned) == []
    assert timefuzz.strip_absolute(cleaned) == cleaned   # 幂等


def test_has_absolute_detects_and_clean_text_passes():
    assert timefuzz.has_absolute("2026年10月25日") == ["2026年10月25日"]
    assert timefuzz.has_absolute("3 天前") == ["3 天前"]
    assert timefuzz.has_absolute("2026-10-25") == ["2026-10-25"]
    assert timefuzz.has_absolute("今天有点累，想聊聊工作") == []
    assert timefuzz.has_absolute("") == []


@pytest.mark.parametrize("raw,expected_label", [
    ("2026年10月25日", timefuzz.LABEL_EARLIER),
    ("2026-10-25", timefuzz.LABEL_EARLIER),
    ("10月25日", timefuzz.LABEL_A_WHILE),
    ("2 天前", timefuzz.LABEL_TWO_DAYS),
    ("昨天", timefuzz.LABEL_TWO_DAYS),
])
def test_strip_absolute_mapping(raw, expected_label):
    cleaned = timefuzz.strip_absolute(raw)
    assert expected_label in cleaned
    assert timefuzz.has_absolute(cleaned) == []


# --------------------------------------------------------------------------- #
# 渲染层审计：三个输出都不含绝对时间
# --------------------------------------------------------------------------- #
def _seed_memory(path, mid, content, *, kind="preference", importance=0.8,
                 age_days=200.0, now=None):
    now = NOW if now is None else now
    con = db.connect(path)
    con.execute(
        "INSERT INTO user_memories(id,scope,kind,content,evidence,evidence_message_id,"
        "confidence,status,conflict_group,created_at,importance,access_count,"
        "last_accessed,evidence_history) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, "user", kind, content, content, None, 0.9, "active", None,
         now - age_days * DAY, importance, 0, 0.0,
         json.dumps([content], ensure_ascii=False)))
    con.commit()
    con.close()


def test_render_layers_have_no_absolute_time(tmp_path):
    path = tmp_path / "render.sqlite3"
    _seed_memory(path, "UM-old", "用户2026年10月25日说过喜欢猫")
    _seed_memory(path, "UM-warm", "用户10月25日提到喜欢音乐", age_days=45.0)
    result = memory.retrieve_tiered(path, "还记得以前的事吗", now=NOW)
    lines = memory.render_memories(result["items"], now=NOW)
    text = "\n".join(lines)
    assert timefuzz.has_absolute(text) == []

    # retrieval.context：归档消息带 timefuzz 前缀
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-old", "user", "以前说过喜欢咖啡", NOW - 200 * DAY))
    con.commit()
    con.close()
    search.index_texts(path, [{"source_type": "message", "source_id": "M-old",
                               "text": "以前说过喜欢咖啡"}])
    memory.archive_cold(path, now=NOW, days=90)
    ctx = retrieval.context(path, "还记得以前的咖啡吗", k=5, now=NOW)
    assert ctx and timefuzz.has_absolute(ctx) == []
    assert "[更早以前]" in ctx

    # summary.context：月 gist 标题含绝对月份，注入前被统一模糊
    con = db.connect(path)
    con.execute("INSERT INTO summaries(id,content,evidence_json,turns_count,created_at,"
                "kind,year_month,source) VALUES(?,?,?,?,?,?,?,?)",
                ("SUM-m", "【2026年8月要点】- 用户当时在准备面试", "{}", 5,
                 NOW - 200 * DAY, "monthly", "2026-08", "rule"))
    con.commit()
    con.close()
    sum_ctx = summary.context(path)
    assert "2026" not in sum_ctx and timefuzz.has_absolute(sum_ctx) == []


def test_personality_context_strips_absolute(tmp_path):
    path = tmp_path / "persona.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO persona(id,key,value,evidence_json,version,status,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("P1", "tone", "2026年10月25日之后变简短", "{}", 1, "active", 1.0))
    con.commit()
    con.close()
    ctx = personality.context(path)
    assert timefuzz.has_absolute(ctx) == []


# --------------------------------------------------------------------------- #
# prompt 审计：真跑一次 chat.send，最终 prompt 不得含绝对日期/精确天数
# --------------------------------------------------------------------------- #
class FakeClient:
    def available(self):
        return True

    def chat(self, messages, **kwargs):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        if "记忆系统" in text or "用户记忆抽取器" in text:
            return {"ok": True, "text": json.dumps({"items": []})}
        return {"ok": True, "text": "嗯"}


def test_chat_prompt_has_no_absolute_time(tmp_path, monkeypatch):
    path = tmp_path / "prompt.sqlite3"
    now = time.time()
    # 极老记忆（cold）+ 月 gist（绝对月份）+ 归档消息（检索命中）
    _seed_memory(path, "UM-old", "用户喜欢猫", age_days=400.0, now=now)
    con = db.connect(path)
    con.execute("INSERT INTO summaries(id,content,evidence_json,turns_count,created_at,"
                "kind,year_month,source) VALUES(?,?,?,?,?,?,?,?)",
                ("SUM-m", "【2026年8月要点】- 用户说 2026年8月 在准备面试", "{}", 5,
                 now - 200 * DAY, "monthly", "2026-08", "rule"))
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-old", "user", "2026年7月25日 以前说过喜欢咖啡", now - 120 * DAY))
    con.commit()
    con.close()
    search.index_texts(path, [{"source_type": "message", "source_id": "M-old",
                               "text": "2026年7月25日 以前说过喜欢咖啡"}])
    memory.archive_cold(path, now=now, days=90)

    captured: dict = {}

    def fake_generate(client, system, prompt, memories, **kwargs):
        captured["prompt"] = prompt
        return {"ok": True, "text": "嗯"}, {"two_stage": False, "claims": []}

    monkeypatch.setattr(chat_mod.generation, "generate", fake_generate)
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(3))
    result = agent.send("还记得以前的事吗")
    assert result["ok"] and captured.get("prompt")
    prompt = captured["prompt"]
    assert timefuzz.has_absolute(prompt) == []
    assert "2026" not in prompt
    assert "更早以前" in prompt     # 老记忆/月 gist 被模糊


# --------------------------------------------------------------------------- #
# 源码防线：注入路径里不许直接用 strftime / %Y-%m-%d（除非白名单注释）
# --------------------------------------------------------------------------- #
def test_no_strftime_in_prompt_renderers():
    from pathlib import Path
    base = Path(__file__).resolve().parents[1] / "selflearn"
    guarded = ["memory.py", "summary.py", "retrieval.py", "personality.py",
               "context.py", "chat.py"]
    offenders = []
    for name in guarded:
        text = (base / name).read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            if ("strftime" in line or "%Y-%m-%d" in line) and "# timefuzz-allowed" not in line:
                offenders.append(f"{name}:{line_no}: {line.strip()[:100]}")
    assert offenders == [], offenders
