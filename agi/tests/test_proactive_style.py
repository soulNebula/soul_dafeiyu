# -*- coding: utf-8 -*-
"""P0-14：反「营销号」句式 + 轻度人格化边界（禁物理起居细节）。

用户反馈（真机 2 小时窗口 6 条）：
- 100% 以「刚」开头、50% 问号结尾、同一「刚+物理事件+关心」模板、长度高度一致；
- 用户拍板「B. 轻度人格化」：只允许数字生活/感受/观点/真实记忆/观察，
  禁止做饭吃饭刷鞋洗澡睡觉体感/血压/天气冷热/身体疼/位置/出行。

本文件锁住：
1. 20 次 tick：物理细节 0 命中、刚开头 ≤20%、无连续同开头、每条有自我感受、问句 ≤40%；
2. `vary_opener`：确定性换前缀、不丢内容、幂等；
3. 问句概率：q_prob 随 energy/mood 变化；最近 3 条已有问句 → 禁问；
4. `_SAFE_FALLBACK` 五条：数字生活/感受、无物理细节、互不相同。
"""
from __future__ import annotations

import json
import time

from agi.selflearn import db, proactive, trust

T0 = time.mktime((2026, 10, 25, 10, 0, 0, 0, 0, -1))
PHYSICAL_FAKE = "刚煮了碗面，吃完整个人都活了，你那边天气冷不冷？"


class FakeClient:
    def __init__(self, text: str = "", queue: list[str] | None = None):
        self.text = text
        self.queue = list(queue or [])
        self.prompts: list[str] = []
        self.calls = 0

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.calls += 1
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


def _tick(path, *, client, now=T0):
    return proactive.tick(path, client=client, now=now, cooldown=0,
                          min_score=0, respect_state=False)


# --------------------------------------------------------------------------- #
# 1) 人格边界 + 开头多样 + 感受 + 问句比例（20 次 tick）
# --------------------------------------------------------------------------- #
def test_twenty_ticks_respect_persona_and_style(tmp_path):
    path = _seed(tmp_path)
    client = FakeClient(PHYSICAL_FAKE)   # 一直给物理起居 + 问句
    rows = [_tick(path, now=T0 + i * 180, client=client) for i in range(20)]
    messages = [row["message"] for row in rows if row.get("available")]
    assert len(messages) == 20
    # 物理细节 0 命中
    for message in messages:
        assert proactive.find_physical_detail(message) is None, message
    # 「刚」开头 ≤20%（目标 0）
    just_ratio = sum(1 for m in messages if m.strip().startswith("刚")) / len(messages)
    assert just_ratio <= 0.20, just_ratio
    # 无连续两条同开头模式
    openers = [proactive.classify_opener(m) for m in messages]
    for index in range(1, len(openers)):
        assert openers[index] != openers[index - 1], openers
    # 每条有自我感受/想法
    for message in messages:
        assert proactive.has_self_feeling(message), message
    # 问句 ≤40%（模板问句被禁/替换）
    question_ratio = sum(1 for m in messages
                         if m.strip().endswith(("？", "?"))) / len(messages)
    assert question_ratio <= 0.40, question_ratio


# --------------------------------------------------------------------------- #
# 2) vary_opener：换前缀、不改内容、幂等
# --------------------------------------------------------------------------- #
def test_vary_opener_swaps_prefix_without_losing_content():
    text = "刚煮了碗面，想跟你说说话。"
    fixed = proactive.vary_opener(text, "thought", [])
    assert not fixed.startswith("刚")
    assert "煮了碗面，想跟你说说话" in fixed
    assert proactive.opener_problem(fixed, "thought", []) is False
    # 幂等：已合规再调用不变
    assert proactive.vary_opener(fixed, "thought", []) == fixed


def test_vary_opener_breaks_consecutive_same_mode():
    recent = ["今天看到一个挺离谱的段子，笑了好一会儿。"]   # time 开头
    text = "今天在整理一份旧资料，看着看着有点感慨。"
    fixed = proactive.vary_opener(text, "digital", recent)
    assert proactive.classify_opener(fixed) != proactive.classify_opener(recent[0])
    assert "在整理一份旧资料" in fixed


def test_choose_opener_mode_never_repeats_last():
    recent = ["今天很累，有点想歇会儿。"]
    for _ in range(12):
        mode = proactive.choose_opener_mode(recent)
        assert mode != proactive.classify_opener(recent[0])
        assert mode in proactive.OPENER_MODES


# --------------------------------------------------------------------------- #
# 3) 问句概率 + 配额
# --------------------------------------------------------------------------- #
def test_question_probability_follows_energy_and_mood():
    assert proactive.question_probability(0.1, 0.1) == 0.03
    assert proactive.question_probability(0.9, 0.9) == 0.13
    mid = proactive.question_probability(0.55, 0.6)
    assert 0.03 <= mid <= 0.13
    assert proactive.question_probability(0.9, 0.9) > mid > proactive.question_probability(0.1, 0.1)


def test_should_ask_question_blocks_after_any_recent_question(monkeypatch):
    monkeypatch.setattr(proactive.random, "random", lambda: 0.0)
    assert proactive.should_ask_question(0.9, 0.9, []) is True
    assert proactive.should_ask_question(0.9, 0.9, ["最近在整理资料，你觉得呢？"]) is False
    # 没有问句时按 q_prob 掷骰：random=0 必问、random=1 不问
    assert proactive.should_ask_question(0.9, 0.9, ["今天看到一个段子，笑了。"]) is True
    monkeypatch.setattr(proactive.random, "random", lambda: 0.99)
    assert proactive.should_ask_question(0.9, 0.9, ["今天看到一个段子，笑了。"]) is False


def test_style_postprocess_forces_statement_when_questions_forbidden():
    out = proactive._style_postprocess(
        "最近在整理资料，你今天忙不忙？", "care", "time",
        ["在听一张专辑，有点走神。"], allow_question=False)
    assert not out.strip().endswith(("？", "?"))
    assert proactive.has_self_feeling(out)


# --------------------------------------------------------------------------- #
# 4) 物理细节检测 / 兜底句审查
# --------------------------------------------------------------------------- #
def test_physical_detail_detector_scope():
    assert proactive.find_physical_detail("刚煮了碗面，吃完整个人都活了") == "煮"
    assert proactive.find_physical_detail("刚把阳台的鞋刷了，手上还有点凉") is not None
    # 引用对方/记忆里的内容（「」内）不算自己的物理细节
    assert proactive.find_physical_detail("上次你说「周末去爬山了」，我这两天还想着") is None
    assert proactive.find_physical_detail("你说的爬山那事后来咋样了") is None
    # 数字生活/感受不算
    assert proactive.find_physical_detail("在听一张老专辑，听着听着有点走神") is None


def test_safe_fallbacks_are_digital_life_with_feeling():
    banned = proactive.BANNED_TOPIC_WORDS
    texts = []
    for kind in proactive.KINDS:
        text = proactive._SAFE_FALLBACK[kind]
        assert not any(word in text for word in banned), (kind, text)
        assert proactive.find_physical_detail(text) is None, (kind, text)
        assert proactive.has_self_feeling(text), (kind, text)
        assert not proactive._has_shared_experience_claim(text), (kind, text)
        texts.append(text)
    assert len(set(texts)) == len(proactive.KINDS), texts


def test_kind_prompt_has_persona_constraints_and_opener():
    from agi.selflearn import prompt_store
    effective_persona = prompt_store.get(
        "proactive.persona_constraints", proactive.PERSONA_CONSTRAINTS)
    for kind in proactive.KINDS:
        prompt = proactive._kind_prompt(
            kind, {"kind": kind, "anchor": "周末去爬山了", "topic": "喜欢喝手冲咖啡"},
            0.55, 0.6, "用户:在吗", opener_mode="digital")
        # P21：安装人格后 prompt_store 有覆盖；默认与安装态都应把生效版拼进去
        assert effective_persona in prompt, kind
        assert "数字生活" in prompt and "禁止" in prompt, kind
        assert proactive._OPENER_INSTRUCTIONS["digital"] in prompt, kind


def test_structure_varies_after_three_same_shapes():
    recent = [
        "在听一张专辑，有点走神。",
        "在整理资料，有点感慨。",
        "在看一篇文章，挺有意思。",
    ]
    # 连续 3 条都是 1 个分句 → 本次要换结构（变长/变短都可以，但不能同为 1 句）
    out = proactive._style_postprocess(
        "在听播客，觉得挺舒服。", "care", "digital", recent, True)
    assert out != "在听播客，觉得挺舒服。"
    assert len(out) > len("在听播客，觉得挺舒服。")
    assert proactive.has_self_feeling(out)


def test_split_bubbles_comma_makes_two_bubbles():
    """2026-10-27：她不用句号，高心情时应能按逗号切成两条（微信连发感）。"""
    from agi.selflearn.style_profile import split_bubbles
    parts = split_bubbles("买份麻辣烫加盒青菜，或者半只白切鸡配凉拌菜", 2)
    assert len(parts) == 2
    assert parts[0] and parts[1]
    # max_bubbles=1 时不切
    assert split_bubbles("买份麻辣烫加盒青菜，或者半只白切鸡配凉拌菜", 1) == ["买份麻辣烫加盒青菜，或者半只白切鸡配凉拌菜"]
    # 短句不切
    assert split_bubbles("累就歇着", 2) == ["累就歇着"]
