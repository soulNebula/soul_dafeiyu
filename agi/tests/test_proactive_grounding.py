# -*- coding: utf-8 -*-
"""P0-9：禁止编造共同经历（真机首条消息「便当盒」杜撰）。

真机实证：P0-8 要求「必须具体」，模型为满足「具体」杜撰了共同经历：
> [care] 刚翻冰箱看见角落那个旧便当盒，是你上回放这儿的吧。
联系人 SQLite（messages/proactive/share_candidates）里「便当盒」只出现在刚发出的
proactive 记录里，memory 里没有任何依据。

本文件锁住：
1. `grounding_ok(text, anchors)`：共同经历断言必须有真实锚点 ≥2 字重叠，否则判编造；
2. prompt 全局不编造约束（所有 kind），followup 有真实锚点时才允许引用；
3. 编造文本 → 重生成 1 次（只讲自己的事）→ 仍不过 → `_SAFE_FALLBACK`；
4. 20 次 tick 随机/固定返回编造句，发送文本命中共同经历断言 = 0；
5. `_SAFE_FALLBACK` 五条：只讲自己的事/中性事实、无禁词、无共同经历断言、互不相同。
"""
from __future__ import annotations

import json
import time

from agi.selflearn import db, proactive, ticker, trust
from agi.wechat_gateway import proactive_worker

T0 = time.mktime((2026, 10, 25, 10, 0, 0, 0, 0, -1))

FABRICATED = "刚翻冰箱看见角落那个旧便当盒，是你上回放这儿的吧。"
FABRICATED_POOL = [
    FABRICATED,
    "上次你说要给我带那家店的蛋糕，我可记着呢。",
    "还记得你放在我这的那把伞吗，今天下雨正好用上。",
]


class FakeClient:
    """可按队列/轮转返回文本，并记录 prompt 与调用次数。"""

    def __init__(self, text: str | None = None, pool: list[str] | None = None):
        self.text = text
        self.pool = list(pool or [])
        self.index = 0
        self.prompts: list[str] = []
        self.calls = 0

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.calls += 1
        self.prompts.append(str(messages[-1].get("content") or ""))
        if self.pool:
            text = self.pool[self.index % len(self.pool)]
            self.index += 1
            return {"ok": True, "text": text}
        return {"ok": True, "text": self.text or ""}


def _seed(tmp_path, *, idle_h: float = 6.0, trust_value: float = 0.6,
          user_text: str = "周末去爬山了，累死", topic: str = "喜欢喝手冲咖啡",
          now: float = T0, name: str = "p"):
    path = tmp_path / f"{name}.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", user_text, now - idle_h * 3600))
    if topic:
        con.execute(
            "INSERT INTO user_memories(id,scope,kind,content,evidence,confidence,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("UM-1", "user", "preference", topic, user_text, 0.9, now - 7200))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}, ensure_ascii=False), now))
    con.commit()
    con.close()
    trust.set_value(path, trust_value)
    return path


def _tick(path, *, now=T0, client=None):
    return proactive.tick(path, client=client if client is not None else FakeClient(),
                          now=now, cooldown=0, min_score=0, respect_state=False)


def _force_candidate(monkeypatch, kind: str = "care", *, anchor: str = "",
                     topic: str = "") -> None:
    monkeypatch.setattr(proactive, "build_candidates", lambda p, now=None: [{
        "kind": kind, "text": "兜底", "reason": "test", "base": 0.55,
        "score": 0.6, "anchor": anchor, "topic": topic,
    }])


# --------------------------------------------------------------------------- #
# 1) grounding_ok 判定
# --------------------------------------------------------------------------- #
def test_grounding_ok_flags_fabricated_shared_experience():
    anchors = ["周末去爬山了，累死", "喜欢喝手冲咖啡"]
    # 真机原句：anchors 里没有便当盒 → 编造
    assert not proactive.grounding_ok(
        "刚翻冰箱看见角落那个旧便当盒，是你上回放这儿的吧", anchors)
    assert not proactive.grounding_ok("你上回说要一起去看海", anchors)
    assert not proactive.grounding_ok("上次你说「买了个新键盘」，我记着呢", anchors)
    assert not proactive.grounding_ok("还记得你带我去吃的那家店", [])
    # 只讲自己的事 / 中性事实 → 不拦
    assert proactive.grounding_ok("刚煮了碗面，吃完整个人都活了", anchors)
    assert proactive.grounding_ok("今天降温了，出门得穿外套", anchors)


def test_grounding_ok_allows_real_anchors():
    anchors = ["周末去爬山了，累死", "喜欢喝手冲咖啡"]
    # 引用真实锚点（≥2 字重叠）→ 允许
    assert proactive.grounding_ok(
        "上次你说「周末去爬山了」，我这两天还一直想着这事", anchors)
    assert proactive.grounding_ok(
        "你之前说的手冲咖啡，我今天也试了一杯", anchors)
    # anchors 里确实有「便当盒」→ 允许（不能误杀真实依据）
    assert proactive.grounding_ok(
        "刚翻冰箱看见那个便当盒，是你上回放这儿的吧", ["便当盒"])


# --------------------------------------------------------------------------- #
# 2) prompt 不编造约束
# --------------------------------------------------------------------------- #
def test_every_kind_prompt_has_grounding_constraints():
    for kind in proactive.KINDS:
        prompt = proactive._kind_prompt(
            kind, {"kind": kind, "anchor": "周末去爬山了", "topic": "喜欢喝手冲咖啡"},
            0.55, 0.45, "用户:在吗")
        assert proactive.GROUNDING_CONSTRAINTS in prompt, kind
        assert "编造" in prompt and "只讲你自己的事" in prompt, kind
    # followup 有真实锚点时允许引用；own_only 重生成明确只讲自己
    followup = proactive._kind_prompt(
        "followup", {"kind": "followup", "anchor": "周末去爬山了"}, 0.55, 0.45, "")
    assert "例外" in followup and "周末去爬山了" in followup
    own = proactive._kind_prompt(
        "care", {"kind": "care"}, 0.55, 0.45, "", own_only=True)
    assert "只说你自己的事" in own


# --------------------------------------------------------------------------- #
# 3) 编造 → 重生成 → 兜底
# --------------------------------------------------------------------------- #
def test_fabricated_text_is_rewritten_before_return(tmp_path, monkeypatch):
    _force_candidate(monkeypatch, "care")
    path = _seed(tmp_path)
    client = FakeClient(FABRICATED)
    row = _tick(path, client=client)
    assert row["available"] is True
    assert "便当盒" not in row["message"]
    assert not proactive._has_shared_experience_claim(row["message"])
    assert client.calls >= 2                    # 至少重生成过 1 次
    safe = {text.rstrip("。") for text in proactive._SAFE_FALLBACK.values()}
    assert row["message"].rstrip("。") in safe


def test_regeneration_prompt_says_only_your_own(tmp_path, monkeypatch):
    _force_candidate(monkeypatch, "care")
    path = _seed(tmp_path)
    client = FakeClient(FABRICATED)
    _tick(path, client=client)
    assert any("只说你自己的事" in prompt for prompt in client.prompts), client.prompts


def test_real_anchor_not_falsely_rewritten(tmp_path, monkeypatch):
    _force_candidate(monkeypatch, "followup", anchor="周末去爬山了")
    path = _seed(tmp_path)
    client = FakeClient("上次你说「周末去爬山了」，我这两天还一直想着这事。")
    row = _tick(path, client=client)
    assert row["available"] is True
    assert "爬山" in row["message"] and "上次你说" in row["message"]
    assert proactive.grounding_ok(
        row["message"], proactive._grounding_anchors(path))


# --------------------------------------------------------------------------- #
# 4) 20 次 tick 编造命中 = 0
# --------------------------------------------------------------------------- #
def test_twenty_ticks_never_send_fabricated_shared_experience(tmp_path, monkeypatch):
    _force_candidate(monkeypatch, "care")
    path = _seed(tmp_path)
    client = FakeClient(pool=FABRICATED_POOL)
    messages = []
    for index in range(20):
        row = _tick(path, now=T0 + index * 180, client=client)
        assert row["available"] is True
        messages.append(row["message"])
    anchors = proactive._grounding_anchors(path)
    for message in messages:
        assert not proactive._has_shared_experience_claim(message), message
        assert proactive.grounding_ok(message, anchors), message


# --------------------------------------------------------------------------- #
# 5) _SAFE_FALLBACK 审查
# --------------------------------------------------------------------------- #
def test_safe_fallbacks_are_self_only():
    banned = proactive.BANNED_TOPIC_WORDS
    texts = []
    for kind in proactive.KINDS:
        text = proactive._SAFE_FALLBACK[kind]
        assert not any(word in text for word in banned), (kind, text)
        assert not proactive._has_shared_experience_claim(text), (kind, text)
        assert proactive.grounding_ok(text, []), (kind, text)
        for pattern in ("你上回", "上次你", "还记得你", "你说过", "你放在我这"):
            assert pattern not in text, (kind, pattern)
        texts.append(text)
    assert len(set(texts)) == len(proactive.KINDS), texts


# --------------------------------------------------------------------------- #
# 6) worker 链路回归：P0-8 禁词兜底仍生效（且新兜底句不含共同经历）
# --------------------------------------------------------------------------- #
class FakeGateway:
    def __init__(self, **cfg):
        self.cfg = {
            "proactive_enabled": True,
            "proactive_probability": 1.0,
            "allowed_contacts": ["好友A"],
            "proactive_contacts": [],
            **cfg,
        }
        self.sent: list[tuple] = []
        self.records: list[tuple] = []
        self.last_turns: dict = {}
        self.last_proactive: dict = {}

    def _send(self, contact, text, **kw):
        self.sent.append((contact, text, kw))
        return {"ok": True, "status": "sent"}

    def _record(self, kind, data):
        self.records.append((kind, data))


def test_worker_safe_fallback_has_no_shared_claim(tmp_path, monkeypatch):
    path = _seed(tmp_path, now=time.time())
    monkeypatch.setattr("agi.wechat_gateway.config.contact_db", lambda contact: path)

    def fake_tick(*args, **kwargs):
        return {"ok": True, "ran": True, "gate": {"allowed": True},
                "proactive": {"ok": True, "available": True, "kind": "care",
                              "messages": ["安静这么久，你那边真的一点声音都没有吗"],
                              "message": "安静这么久，你那边真的一点声音都没有吗"}}

    monkeypatch.setattr(ticker, "run_once", fake_tick)
    g = FakeGateway()
    proactive_worker.run_cycle(g, client=object(), now=time.time())
    assert g.sent
    for _, text, _ in g.sent:
        assert text in proactive._SAFE_FALLBACK.values(), text
        assert not proactive._has_shared_experience_claim(text), text
