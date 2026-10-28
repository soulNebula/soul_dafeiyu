# -*- coding: utf-8 -*-
"""P0-8：反「安静」内容塌缩（禁元话题 + 主题去重 + 问句配额 + 后处理兜底）。

对应真机验收问题（178 条主动消息）：
- 22% 是「安静/没动静/忙吗/好久没/冒个泡」这类**元话题**；28% 带「犯困/发呆/窗户/声音」主题词；
- 84% 以问号结尾（连发像审讯）；kind 轮换了，内容还是在聊「你不说话」。

本文件锁住：
1. 每个 kind 的 prompt 都带全局负向约束（禁元话题、禁泛问、必须具体）；
2. 主题去重：与最近 10 条 2-gram Jaccard ≥0.35 → 同 kind 重生成 1 次 → 仍高换低重叠兜底；
3. 问句配额：最近 3 条 ≥2 条问号结尾 → 本次强制陈述；
4. 发送前 sanitize_topic：命中禁词一律换该 kind 兜底句，线上绝不出现；
5. 五条兜底句审查：无禁词、不评论沉默、彼此主题不同。
"""
from __future__ import annotations

import json
import time

import pytest

from agi.selflearn import db, proactive, ticker, trust
from agi.wechat_gateway import proactive_worker

T0 = time.mktime((2026, 10, 25, 10, 0, 0, 0, 0, -1))


class FakeClient:
    """记录 prompt / 调用次数，返回固定或按队列的文本。"""

    def __init__(self, text: str = "嗯，最近还好。", queue: list[str] | None = None):
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


def _force_kind(monkeypatch, kind: str = "care") -> None:
    """把 kind 固定住，隔离测试内容层（避免 followup 锚点/轮换干扰断言）。"""
    monkeypatch.setattr(proactive, "build_candidates", lambda p, now=None: [{
        "kind": kind, "text": "兜底", "reason": "test", "base": 0.55,
        "score": 0.6, "anchor": "", "topic": "",
    }])


# --------------------------------------------------------------------------- #
# 1) 禁词 / 兜底 / prompt 约束
# --------------------------------------------------------------------------- #
def test_prompt_every_kind_has_negative_constraints():
    for kind in proactive.KINDS:
        prompt = proactive._kind_prompt(
            kind, {"kind": kind, "anchor": "周末去爬山了", "topic": "喜欢喝手冲咖啡"},
            0.55, 0.45, "用户:在吗")
        assert proactive.TOPIC_CONSTRAINTS in prompt, kind
        for word in ("安静", "没动静", "忙吗", "冒个泡"):
            assert word in prompt, (kind, word)
        assert "禁止" in prompt and "具体" in prompt


@pytest.mark.parametrize("banned_text", [
    "安静这么久，我都有点犯困了",
    "你那边怎么一点声音都没有",
    "好久没聊了，最近在干嘛",
    "冒个泡，别介意打扰了",
    "没人理我，是不是没消息了",
])
def test_sanitize_topic_replaces_every_banned_variant(banned_text):
    out = proactive.sanitize_topic(banned_text, "care")
    assert out == proactive._SAFE_FALLBACK["care"]
    for word in proactive.BANNED_TOPIC_WORDS:
        assert word not in out


def test_sanitize_topic_keeps_clean_text_and_handles_empty():
    clean = "刚泡了杯手冲，酸得我皱了下眉。"
    assert proactive.sanitize_topic(clean, "share") == clean
    assert proactive.sanitize_topic("", "light") == proactive._SAFE_FALLBACK["light"]
    assert proactive.sanitize_topic("   ", "care") == proactive._SAFE_FALLBACK["care"]


def test_five_fallbacks_are_reviewed():
    banned = proactive.BANNED_TOPIC_WORDS
    silence_words = ("不回", "不说话", "冷清", "沉默", "没人理", "不理我")
    for name, mapping in (("KIND_FALLBACK", proactive._KIND_FALLBACK),
                          ("SAFE_FALLBACK", proactive._SAFE_FALLBACK)):
        texts = []
        for kind in proactive.KINDS:
            text = mapping[kind].format(anchor="周末去爬山了", topic="喜欢喝手冲咖啡")
            assert not any(word in text for word in banned), (name, kind, text)
            assert not any(word in text for word in silence_words), (name, kind, text)
            texts.append(text)
        assert len(set(texts)) == len(proactive.KINDS), (name, texts)


def test_kind_fallback_followup_keeps_anchor():
    text = proactive._safe_fallback("followup", anchor="周末去爬山了")
    assert "爬山" in text
    # 空锚点不能拼出「上次你说「」」这种破句
    assert "「」" not in proactive._safe_fallback("followup")


# --------------------------------------------------------------------------- #
# 2) 主题去重（2-gram Jaccard）
# --------------------------------------------------------------------------- #
def test_similar_theme_is_regenerated_once_then_replaced(tmp_path, monkeypatch):
    _force_kind(monkeypatch, "care")
    path = _seed(tmp_path)
    same = "在听一张老专辑，听着听着有点走神。"
    client = FakeClient(same)
    first = _tick(path, now=T0 + 60, client=client)
    assert first["available"] is True
    assert client.calls == 1
    second = _tick(path, now=T0 + 120, client=client)
    assert second["available"] is True
    # 同主题：同一 kind 重生成 1 次 → 仍超标 → 换低重叠兜底句
    assert client.calls >= 3
    assert second["message"] != first["message"]
    assert proactive.max_topic_overlap(second["message"], [first["message"]]) < (
        proactive.TOPIC_SIMILARITY_MAX)


def test_twenty_ticks_adjacent_overlap_below_threshold_ratio(tmp_path):
    path = _seed(tmp_path)
    client = FakeClient("听见窗户那边轻响了一下，我猜是风。")
    rows = [_tick(path, now=T0 + i * 180, client=client) for i in range(20)]
    assert all(row["available"] for row in rows)
    texts = [row["message"] for row in rows]
    overlaps = [proactive.topic_overlap(texts[i], texts[i + 1])
                for i in range(len(texts) - 1)]
    low_ratio = sum(1 for value in overlaps if value < proactive.TOPIC_SIMILARITY_MAX)
    assert low_ratio / len(overlaps) >= 0.8, (low_ratio, len(overlaps), overlaps)


# --------------------------------------------------------------------------- #
# 3) 问句配额
# --------------------------------------------------------------------------- #
def test_question_quota_forces_statement_after_two_questions(tmp_path, monkeypatch):
    _force_kind(monkeypatch, "care")
    path = _seed(tmp_path)
    client = FakeClient(queue=[
        "今天中午吃的什么？", "晚上打算几点睡？",
        "周末一般怎么安排？", "你喜欢下雨天吗？",
    ])
    rows = [_tick(path, now=T0 + i * 120, client=client) for i in range(4)]
    assert all(row["available"] for row in rows)
    endings = [str(row["message"]).strip().endswith(("？", "?")) for row in rows]
    # P0-14：最近 3 条只要已有 1 条问句 → 本次禁问；首条允许问
    # （fake queue 可能被重生成多消费一条，允许最多 2 条问句，但必须受配额限制）
    assert endings[0] is True
    assert sum(endings) <= 2, endings


def test_force_statement_prompt_says_no_question(tmp_path, monkeypatch):
    _force_kind(monkeypatch, "care")
    path = _seed(tmp_path)
    client = FakeClient(queue=["今天中午吃的什么？", "晚上打算几点睡？",
                               "周末一般怎么安排？", "你喜欢下雨天吗？"])
    _tick(path, now=T0 + 60, client=client)
    _tick(path, now=T0 + 120, client=client)
    third = _tick(path, now=T0 + 180, client=client)
    assert third["available"] is True
    assert any("不要问对方任何问题" in prompt for prompt in client.prompts)


# --------------------------------------------------------------------------- #
# 4) 发送前最后一道闸：worker 里命中禁词 → 换兜底句
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


class BannedClient(FakeClient):
    def chat(self, messages, **kwargs):
        self.calls += 1
        return {"ok": True, "text": "安静这么久，你那边真的一点声音都没有吗"}


def test_worker_rewrites_banned_text_before_send(tmp_path, monkeypatch):
    path = _seed(tmp_path, now=T0)
    monkeypatch.setattr("agi.wechat_gateway.config.contact_db", lambda contact: path)
    g = FakeGateway()
    client = BannedClient()
    out = proactive_worker.run_cycle(g, client=client, now=T0)
    assert out["sent"] >= 1, out
    for _, text, _ in g.sent:
        for word in proactive.BANNED_TOPIC_WORDS:
            assert word not in text, (word, text)
        assert text, text


def test_worker_legacy_payload_without_kind_is_polished(tmp_path, monkeypatch):
    """2026-10-29 起：无 kind 的 legacy payload 也过出站质检（去「」/账、补标点）。"""
    path = _seed(tmp_path)
    monkeypatch.setattr("agi.wechat_gateway.config.contact_db", lambda contact: path)

    def fake_tick(*args, **kwargs):
        return {"ok": True, "ran": True, "gate": {"allowed": True},
                "proactive": {"ok": True, "available": True,
                              "messages": ["在干嘛"], "message": "在干嘛"}}

    monkeypatch.setattr(ticker, "run_once", fake_tick)
    g = FakeGateway()
    proactive_worker.run_cycle(g, client=object(), now=time.time())
    from agi.selflearn import proactive as _pro
    assert [item[1] for item in g.sent] == [_pro.polish_outbound("在干嘛")]
