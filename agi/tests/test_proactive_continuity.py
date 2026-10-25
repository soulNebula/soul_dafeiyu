# -*- coding: utf-8 -*-
"""P0-17：主动消息逻辑/情绪连续性（相邻两条不能像两条独立朋友圈）。

真机问题：A「刚煮了碗面，想起你说胃不舒服」→ B「刚把窗台绿萝剪了剪」；
A「睡得有点晚脑袋发沉」→ B「煮了小米粥胃里暖乎乎」——话题/情绪都硬拼。

本文件锁住：
1. prompt 强制二选一 `action: continuation|new_topic`；new_topic 必须有转折词；
2. `continuity_ok`：continuation 要 ≥2 字实词重叠或指代；new_topic 要转折词 + 与上条 2-gram Jaccard <0.2；
3. 断裂 → 重生成 1 次 → 仍断 → 加承接模板/转折词（确定性）；
4. 情绪连贯：上条低落时不允许突然“巨开心”；
5. 多气泡第二段必须与第一段共享主题词，否则合并为一条；
6. 20 次 tick 相邻连续性 ≥80%，且物理/禁词/grounding/标点约束仍生效。
"""
from __future__ import annotations

import json
import time

from agi.selflearn import db, proactive, trust

T0 = time.mktime((2026, 10, 25, 10, 0, 0, 0, 0, -1))


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


def _seed(tmp_path, *, prev_text: str = "", prev_kind: str = "care",
          mood: float = 0.55, energy: float = 0.6, sharing: float = 0.45,
          name: str = "p"):
    path = tmp_path / f"{name}.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", "周末去爬山了，累死", T0 - 6 * 3600))
    con.execute(
        "INSERT INTO user_memories(id,scope,kind,content,evidence,confidence,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("UM-1", "user", "preference", "喜欢喝手冲咖啡", "我喜欢手冲", 0.9, T0 - 7200))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}, ensure_ascii=False), T0))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('self_state',?,?)",
                (json.dumps({"self": {"mood": mood, "energy": energy,
                                      "sharing": sharing}}, ensure_ascii=False), T0))
    if prev_text:
        con.execute("INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    ("P-prev", prev_kind, prev_text, "test", 0.6, "spoken", T0 - 300))
    con.commit()
    con.close()
    trust.set_value(path, 0.6)
    return path


def _force_kind(monkeypatch, kind: str = "care") -> None:
    monkeypatch.setattr(proactive, "build_candidates", lambda p, now=None: [{
        "kind": kind, "text": "兜底", "reason": "test", "base": 0.55,
        "score": 0.6, "anchor": "", "topic": "喜欢喝手冲咖啡",
    }])


def _no_quadrant_cap(monkeypatch):
    monkeypatch.setattr(proactive.initiative, "decide", lambda p, now=None: {})


def _tick(path, *, client, now=T0):
    return proactive.tick(path, client=client, now=now, cooldown=0,
                          min_score=0, respect_state=False)


# --------------------------------------------------------------------------- #
# 1) action 解析 / continuity_ok 单元
# --------------------------------------------------------------------------- #
def test_parse_action_and_infer():
    action, body = proactive.parse_action(
        "action: continuation\n在整理资料，越整理越乱。")
    assert action == "continuation" and body == "在整理资料，越整理越乱。"

    assert proactive.infer_action("对了，最近在听一张老专辑。", "在整理资料。") == "new_topic"
    assert proactive.infer_action("资料整理到一半，越整理越乱。", "在整理资料。") == "continuation"
    assert proactive.infer_action("完全无关的一句话。", "在整理资料。") == "new_topic"


def test_continuity_ok_rules():
    prev = "在整理资料，越整理越乱。"
    # continuation：共享实词 / 指代都算连贯
    assert proactive.continuity_ok("资料越堆越多，我有点烦。", "continuation", [prev])
    assert proactive.continuity_ok("接着刚才说的，越整理越乱。", "continuation", [prev])
    assert not proactive.continuity_ok("在听一张新专辑，有点走神。", "continuation", [prev])
    # new_topic：必须转折 + 与上条 Jaccard <0.2
    assert proactive.continuity_ok("对了，在看一部老电影，节奏很慢。", "new_topic", [prev])
    assert not proactive.continuity_ok("在看一部老电影，节奏很慢。", "new_topic", [prev])
    assert not proactive.continuity_ok("资料越堆越多，我有点烦。", "new_topic", [prev])


# --------------------------------------------------------------------------- #
# 2) 断裂 → 重生成 → 确定性承接/转折
# --------------------------------------------------------------------------- #
def test_prompt_includes_recent_own_and_user_messages(tmp_path, monkeypatch):
    _force_kind(monkeypatch, "care")
    _no_quadrant_cap(monkeypatch)
    path = _seed(tmp_path, prev_text="最近有点累，在整理资料，越整理越乱。")
    client = FakeClient("资料整理到一半，越整理越乱，有点烦。")
    _tick(path, client=client)
    prompt = client.prompts[0]
    assert "在整理资料，越整理越乱" in prompt      # 最近自己发的主动消息
    assert "周末去爬山了" in prompt                # 用户最近消息
    assert "action: continuation" in prompt and "action: new_topic" in prompt
    assert "情绪" in prompt and "negative" in prompt


def test_unrelated_continuation_gets_rewritten(tmp_path, monkeypatch):
    _force_kind(monkeypatch, "care")
    _no_quadrant_cap(monkeypatch)
    path = _seed(tmp_path, prev_text="在整理资料，越整理越乱。")
    client = FakeClient("在听一张新专辑，有点走神。")   # 与上条无关，无 action
    row = _tick(path, client=client)
    assert row["available"] is True
    assert client.calls >= 2                        # 断裂 → 重生成 1 次
    text = row["message"]
    history = ["在整理资料，越整理越乱。"]
    assert row["action"] in ("continuation", "new_topic")
    # 最终必须有承接/转折，且通过 continuity_ok
    assert proactive.has_transition(text) or proactive.has_continuation_reference(text), text
    assert proactive.continuity_ok(
        text, "continuation" if not proactive.has_transition(text) else "new_topic", history)


def test_continuity_regen_uses_related_second_response(tmp_path, monkeypatch):
    _force_kind(monkeypatch, "care")
    _no_quadrant_cap(monkeypatch)
    path = _seed(tmp_path, prev_text="在整理资料，越整理越乱。")
    client = FakeClient(queue=[
        "在听一张新专辑，有点走神。",
        "资料整理到一半，越整理越乱，有点烦。",
    ])
    row = _tick(path, client=client)
    assert row["available"] is True
    assert "整理" in row["message"]
    assert proactive._shares_content(row["message"], "在整理资料，越整理越乱。")


def test_new_topic_without_transition_gets_marker(tmp_path, monkeypatch):
    _force_kind(monkeypatch, "care")
    _no_quadrant_cap(monkeypatch)
    path = _seed(tmp_path, prev_text="在整理资料，越整理越乱。")
    client = FakeClient("action: new_topic\n在看一部老电影，节奏很慢。")
    row = _tick(path, client=client)
    assert row["available"] is True
    assert proactive.has_transition(row["message"]), row["message"]
    assert row["action"] == "new_topic"


# --------------------------------------------------------------------------- #
# 3) 情绪连续性
# --------------------------------------------------------------------------- #
def test_emotion_jump_is_softened(tmp_path, monkeypatch):
    _force_kind(monkeypatch, "care")
    _no_quadrant_cap(monkeypatch)
    path = _seed(tmp_path, prev_text="最近有点累，脑子发沉。", mood=0.5)
    client = FakeClient("今天心情巨开心，超级开心！")
    row = _tick(path, client=client)
    assert row["available"] is True
    assert not any(word in row["message"] for word in proactive._STRONG_POSITIVE_EMOTION)
    assert not proactive.emotion_conflict(row["message"], "negative", 0.5)


def test_emotion_allowed_when_mood_high(tmp_path, monkeypatch):
    _force_kind(monkeypatch, "care")
    _no_quadrant_cap(monkeypatch)
    path = _seed(tmp_path, prev_text="最近有点累，脑子发沉。", mood=0.85)
    client = FakeClient("今天心情特别开心，状态挺好。")
    row = _tick(path, client=client)
    assert row["available"] is True
    # mood 高（≥0.65）时允许正向，不强制软化
    assert "特别开心" in row["message"] or "开心" in row["message"]


# --------------------------------------------------------------------------- #
# 4) 20 次 tick：相邻连续性 ≥80%
# --------------------------------------------------------------------------- #
def test_twenty_ticks_have_continuity_ratio(tmp_path, monkeypatch):
    _force_kind(monkeypatch, "care")
    _no_quadrant_cap(monkeypatch)
    path = _seed(tmp_path, prev_text="在整理资料，越整理越乱。")
    client = FakeClient("在听一张新专辑，有点走神。")
    messages = []
    for index in range(20):
        row = _tick(path, now=T0 + index * 180, client=client)
        if row.get("available"):
            messages.append(row["message"])
    assert len(messages) == 20
    pairs_ok = 0
    for previous, current in zip(messages, messages[1:]):
        if (proactive._shares_content(previous, current)
                or proactive.has_transition(current)
                or proactive.has_continuation_reference(current)):
            pairs_ok += 1
    ratio = pairs_ok / (len(messages) - 1)
    assert ratio >= 0.80, ratio
    # 同时不牺牲 P0-14/P0-8 约束
    for message in messages:
        assert proactive.find_physical_detail(message) is None, message
        assert not any(word in message for word in proactive.BANNED_TOPIC_WORDS), message


# --------------------------------------------------------------------------- #
# 5) 多气泡：第二段必须延续第一段
# --------------------------------------------------------------------------- #
def test_multi_bubble_second_bubble_continues_first(tmp_path, monkeypatch):
    _force_kind(monkeypatch, "care")
    _no_quadrant_cap(monkeypatch)
    path = _seed(tmp_path, mood=0.9, energy=0.8, sharing=0.85)
    client = FakeClient("在整理一份旧资料，越整理越乱。整理这件事真挺磨人的。")
    row = _tick(path, client=client)
    assert row["available"] is True
    bubbles = row["messages"]
    assert bubbles
    if len(bubbles) > 1:
        assert proactive._bubbles_coherent(bubbles), bubbles
        assert proactive._shares_content(bubbles[0], bubbles[1])
    else:
        # 不连贯时允许合并成一条，但不能丢内容
        assert "整理" in bubbles[0]
