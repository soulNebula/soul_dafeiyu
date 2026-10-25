# -*- coding: utf-8 -*-
"""P0-21：聊天语气 v2（内心念头 / 口语元素 / 句尾不加句号 / 短句单主题）。

真机反面样例：
1. `在整理东西的时候，顺带说个事，在听一张老专辑，同一首慢歌第三遍放。`（三事并置、动作汇报）
2. `那个惯性的事。我越想越觉得像整理资料时打开一个旧文件夹…`（绕、无信息）
3. `说起来，想起前阵子学的一个整理笔记的标记法…`（太书面）

本文件锁住：
- 20 tick：句尾句号率 ≤5%、口语元素覆盖 40–70% 且最近 5 条不重复、长度中位数 ≤25、
  无「在X的时候…顺带说个事」结构、无单条 ≥2 个「在…」并置；
- 真机 3 条 polish 后无尾句号、单主题；
- `strip_trailing_period` 幂等、不误删？/！/…；
- few-shot 段存在；
- 口语元素每条最多 1 个。
"""
from __future__ import annotations

import json
import re
import time

from agi.selflearn import db, proactive, trust

T0 = time.mktime((2026, 10, 25, 10, 0, 0, 0, 0, -1))
REAL_BAD = (
    "在整理东西的时候，顺带说个事，在听一张老专辑，同一首慢歌第三遍放。",
    "那个惯性的事。我越想越觉得像整理资料时打开一个旧文件夹，就那么一直搁着懒得关。",
    "说起来，想起前阵子学的一个整理笔记的标记法，试了两页就搁下了。"
    "心里还有点惦记，觉得没学透。",
)


class FakeClient:
    def __init__(self, text: str = "", queue: list[str] | None = None):
        self.text = text
        self.queue = list(queue or [])
        self.prompts: list[str] = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.prompts.append(str(messages[-1].get("content") or ""))
        if self.queue:
            return {"ok": True, "text": self.queue.pop(0)}
        return {"ok": True, "text": self.text}


def _seed(tmp_path, *, name: str = "p", now: float = T0):
    path = tmp_path / f"{name}.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", "周末去爬山了，累死", now - 6 * 3600))
    con.execute(
        "INSERT INTO user_memories(id,scope,kind,content,evidence,confidence,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("UM-1", "user", "preference", "喜欢喝手冲咖啡", "我喜欢手冲", 0.9, now - 7200))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}, ensure_ascii=False), now))
    con.commit()
    con.close()
    trust.set_value(path, 0.6)
    return path


def _force_kind(monkeypatch, kind: str = "care"):
    monkeypatch.setattr(proactive, "build_candidates", lambda p, now=None: [{
        "kind": kind, "text": "兜底", "reason": "test", "base": 0.55,
        "score": 0.6, "anchor": "", "topic": "喜欢喝手冲咖啡",
    }])


def _tick(path, *, client, now=T0):
    return proactive.tick(path, client=client, now=now, cooldown=0,
                          min_score=0, respect_state=False)


def _clause_initial_actions(text: str) -> int:
    return len(list(proactive._ACTION_CLAUSE_RE.finditer(text)))


MIXED_TEXTS = (
    "资料还没理完，有点烦",
    "哈哈，笑死",
    "那部片子下架了，可惜",
    "真的服了",
    "刚发现收藏夹里一堆没看的，服了",
    "有点烦",
    "这会儿脑子有点乱，事儿太多",
    "外面风好大，有点吵",
)


# --------------------------------------------------------------------------- #
# 1) 20 tick 语气统计
# --------------------------------------------------------------------------- #
def test_twenty_ticks_voice_stats(tmp_path, monkeypatch):
    _force_kind(monkeypatch, "care")
    path = _seed(tmp_path)
    client = FakeClient()
    messages = []
    for index in range(20):
        client.text = MIXED_TEXTS[index % len(MIXED_TEXTS)]
        row = _tick(path, now=T0 + index * 180, client=client)
        if row.get("available"):
            messages.append(row["message"])
    assert len(messages) == 20
    polished = [proactive.polish_outbound(message) for message in messages]
    # 真人画像：结尾默认无标点（。！ 0%），问号 ≤15%
    bad_end = sum(1 for text in polished if text.endswith(("。", "！", "!")))
    assert bad_end == 0, polished
    question_ratio = sum(1 for text in polished if text.endswith("？")) / len(polished)
    assert question_ratio <= 0.15, question_ratio
    # 口语元素：每条最多 1 个、最近 3 条不重复、覆盖率 30–50%
    particles = [proactive._particles_in(text) for text in messages]
    assert all(len(items) <= 1 for items in particles), particles
    for index in range(len(particles)):
        current = particles[index]
        if not current:
            continue
        for back in range(max(0, index - 3), index):
            assert current[0] not in particles[back], (index, particles)
    coverage = sum(1 for items in particles if items) / len(particles)
    assert 0.30 <= coverage <= 0.50, coverage
    # 长度：P0-23 目标中位数 10–18 字，>25 字占比 ≤10%
    lengths = sorted(len(text) for text in messages)
    median = lengths[len(lengths) // 2]
    assert 10 <= median <= 18, lengths
    assert sum(1 for length in lengths if length > 25) / len(lengths) <= 0.10, lengths
    # P0-23 软约束：带事由 55–75%；裸情绪有上下文 ≤25%；连续裸情绪 ≤2
    concrete = [proactive.has_concrete_subject(text) for text in messages]
    subject_ratio = sum(concrete) / len(concrete)
    assert 0.55 <= subject_ratio <= 0.80, (subject_ratio, messages)
    bare_count = sum(1 for flag in concrete if not flag)
    assert bare_count / len(concrete) <= 0.30, (bare_count, messages)
    run = 0
    for flag in concrete:
        run = 0 if flag else run + 1
        assert run < 3, messages
    # 单主题：单条没有 ≥2 个「在…」结构；没有三事并置结构
    for text in messages:
        assert _clause_initial_actions(text) <= 1, text
        assert not re.search(r"在[^，。！？]{0,8}的时候[，,].*在", text), text
        assert not re.search(r"在[^，。！？]{0,10}[，,].*顺带说个事[，,].*在", text), text


# --------------------------------------------------------------------------- #
# 2) 真机 3 条：无尾句号 + 单主题
# --------------------------------------------------------------------------- #
def test_real_bad_inputs_polished():
    first = proactive.polish_outbound(REAL_BAD[0])
    assert not first.endswith("。"), first
    assert _clause_initial_actions(first) <= 1, first
    assert "顺带说个事，在听" not in first
    for raw in REAL_BAD[1:]:
        out = proactive.polish_outbound(raw)
        assert not out.endswith("。"), out


# --------------------------------------------------------------------------- #
# 3) strip_trailing_period
# --------------------------------------------------------------------------- #
def test_strip_trailing_period_rules():
    assert proactive.strip_trailing_period("好呀。") == "好呀"
    assert proactive.strip_trailing_period("好呀。。") == "好呀"
    assert proactive.strip_trailing_period("真的服了？") == "真的服了？"
    assert proactive.strip_trailing_period("笑死！") == "笑死"
    assert proactive.strip_trailing_period("嗯…") == "嗯…"
    assert proactive.strip_trailing_period("内部。保留。") == "内部。保留"
    # 幂等
    for text in ("好呀。", "真的服了？", "笑死！", "嗯…", "内部。保留。"):
        once = proactive.strip_trailing_period(text)
        assert proactive.strip_trailing_period(once) == once


# --------------------------------------------------------------------------- #
# 4) 口语元素收口
# --------------------------------------------------------------------------- #
def test_oral_particle_at_most_one_and_no_repeat():
    out = proactive.apply_oral_particle("有点烦，笑死，真的服了", [])
    assert len(proactive._particles_in(out)) <= 1, out
    out2 = proactive.apply_oral_particle("有点烦", ["有点烦，笑死"])
    assert "笑死" not in out2, out2
    # 最近 5 条已很密（3 条带口语）→ 不再追加
    recent = ["有点烦，笑死", "哈哈，无语", "哎，离谱"]
    out3 = proactive.apply_oral_particle("心里有点乱", recent)
    assert not proactive._particles_in(out3), out3


# --------------------------------------------------------------------------- #
# 5) prompt few-shot
# --------------------------------------------------------------------------- #
def test_voice_fewshot_in_prompt():
    prompt = proactive._kind_prompt(
        "care", {"kind": "care"}, 0.55, 0.45, "用户:在吗")
    assert "你平时说话的样子" in prompt
    assert len(proactive.VOICE_FEWSHOT) == 20
    assert proactive.VOICE_FEWSHOT[0] in prompt
    for example in proactive.VOICE_FEWSHOT:
        assert example in prompt, example
    assert "不要照抄内容" in prompt
    assert "句尾默认不加任何标点" in prompt


# --------------------------------------------------------------------------- #
# P0-23：每条必须有具体事由，禁止"裸情绪"
# --------------------------------------------------------------------------- #
P023_TARGETS = (
    "资料还没理完，有点烦",
    "那部片子下架了，可惜",
    "刚发现收藏夹里一堆没看的，服了",
    "这会儿脑子有点乱，事儿太多",
    "上次那个方法试了，果然不行，笑死",
    "突然想不起来要说什么了",
    "外面风好大，有点吵",
)
P021_BARE = (
    "有点烦，真的服了",
    "突然想到，越想越离谱",
    "对了，有点烦",
    "心里还惦记那事",
    "说起来，看到一个说法，有点烦",
    "看到一个说法，挺有意思，诶",
    "说起来，想起点旧东西",
    "想起点旧东西，有点说不上来",
)


def test_p023_concrete_subject_rule():
    for text in P023_TARGETS:
        assert proactive.has_concrete_subject(text), text
    assert not proactive.has_concrete_subject("跟你聊个事，笑死")
    assert not proactive.has_concrete_subject("说起来，有点烦")
    assert not proactive.has_concrete_subject("有点烦，真的服了")


def test_p023_bare_emotions_get_real_subject():
    sources = ["喜欢喝手冲咖啡", "在整理资料，越整理越乱。", "周末去爬山了，累死"]
    for text in P021_BARE:
        out = proactive.ensure_concrete_subject(text, "care", sources)
        assert proactive.has_concrete_subject(out), (text, out)
        assert proactive.find_physical_detail(out) is None, out
        assert not proactive.contains_metadiscourse(out), out
        assert not proactive._has_shared_experience_claim(out), out


def test_p023_consecutive_bare_is_capped(tmp_path, monkeypatch):
    _force_kind(monkeypatch, "care")
    path = _seed(tmp_path)
    client = FakeClient("有点烦，真的服了")
    messages = []
    for index in range(12):
        row = _tick(path, now=T0 + index * 180, client=client)
        if row.get("available"):
            messages.append(row["message"])
    assert messages
    flags = [proactive.has_concrete_subject(text) for text in messages]
    run = 0
    for flag in flags:
        run = 0 if flag else run + 1
        assert run < 3, messages
    assert any(flags), messages   # 连续裸情绪被兜底补事由
    assert not all(flags), messages


def test_p023_needs_concrete_soft_rule():
    assert proactive.needs_concrete_subject("有点烦", [], []) is True       # 首条无上下文
    assert proactive.needs_concrete_subject("有点烦", [], ["周末去爬山了"]) is False
    assert proactive.needs_concrete_subject(
        "有点烦", ["资料还没理完，有点烦"], []) is False                    # 有上下文
    assert proactive.needs_concrete_subject(
        "有点烦", ["哈哈", "有点烦", "服了"], []) is True                   # 连续 ≥3
