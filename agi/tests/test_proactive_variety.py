# -*- coding: utf-8 -*-
"""P0-3：主动消息模板多样化（4 类意图）+ 出站文本质检。

对应故障（gateway.log 审计）：
- 177 条主动消息只有一种类型（check_in），95 条成功里 29 条（31%）都在说「安静」；
- 出站文本 95 条里问号 0 条（句尾却大量「吗/呢」）、11 条粘连长句。

本文件锁住：
1. 4 类 kind（care/share/followup/light）+ 低频 silence_ack 的触发条件；
2. 轮换：连续 20 次 tick 出现 ≥4 种 kind、单一 kind 占比 ≤50%、light 夜间不出、
   silence_ack 至少间隔 7 天且不连续；
3. followup 必须引用最近对话里的具体内容；
4. polish_outbound：补句末标点 / 合并空白 / 超长按标点切分不漏字 / 已合规逐字节不变；
5. worker 发送前质检 + kind 写进 proactive_send 日志与 proactive_events 表。
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from agi.selflearn import db, memory, proactive, ticker, trust
from agi.wechat_gateway import proactive_worker

#: 本地 2026-10-25 10:00（白天）的固定时间戳，避免测试依赖真实时钟
T0 = time.mktime((2026, 10, 25, 10, 0, 0, 0, 0, -1))
NIGHT_T = time.mktime((2026, 10, 25, 23, 0, 0, 0, 0, -1))


class FakeClient:
    """可用 client：记录 prompt，返回固定文本（可指定）。"""

    def __init__(self, text: str = "嗯，最近还好。"):
        self.text = text
        self.prompts: list[str] = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.prompts.append(str(messages[-1].get("content") or ""))
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
    # 日预算置 0 = 不限，避免 20 次 tick 被上限挡住
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}, ensure_ascii=False), now))
    con.commit()
    con.close()
    trust.set_value(path, trust_value)
    return path


def _tick(path, *, now=T0, client=None):
    return proactive.tick(path, client=client if client is not None else FakeClient(),
                          now=now, cooldown=0, min_score=0, respect_state=False)


# --------------------------------------------------------------------------- #
# 4 类 kind 的触发条件
# --------------------------------------------------------------------------- #
def test_build_candidates_cover_four_kinds_when_eligible(tmp_path):
    path = _seed(tmp_path)
    kinds = [c["kind"] for c in proactive.build_candidates(path, T0)]
    assert {"care", "share", "followup", "light"} <= set(kinds)
    # 每类有自己的兜底句，且不共用同一句
    fallbacks = {proactive.kind_fallback(k, anchor="周末去爬山了", topic="喜欢喝手冲咖啡")
                 for k in proactive.KINDS}
    assert len(fallbacks) == len(proactive.KINDS)
    # 每类有自己的 prompt 指令
    instructions = {proactive.KIND_INSTRUCTION[k] for k in proactive.KINDS}
    assert len(instructions) == len(proactive.KINDS)


def test_care_only_when_low_activity(tmp_path):
    low = _seed(tmp_path, idle_h=6.0, name="low")
    assert "care" in {c["kind"] for c in proactive.build_candidates(low, T0)}
    active = _seed(tmp_path, idle_h=0.1, name="active")
    assert "care" not in {c["kind"] for c in proactive.build_candidates(active, T0)}


def test_light_only_daytime_and_not_stranger(tmp_path):
    day = _seed(tmp_path, trust_value=0.6, name="day")
    assert "light" in {c["kind"] for c in proactive.build_candidates(day, T0)}
    night = _seed(tmp_path, idle_h=6.0, trust_value=0.6, name="night")
    assert "light" not in {c["kind"] for c in proactive.build_candidates(night, NIGHT_T)}
    # 关系仍停在「初识」时不轻佻
    stranger = _seed(tmp_path, trust_value=0.5, name="stranger")
    assert "light" not in {c["kind"] for c in proactive.build_candidates(stranger, T0)}


def test_silence_ack_needs_long_silence_and_seven_day_gap(tmp_path):
    short = _seed(tmp_path, idle_h=6.0, name="short")
    assert "silence_ack" not in {
        c["kind"] for c in proactive.build_candidates(short, T0)}
    long_idle = _seed(tmp_path, idle_h=72.0, name="long")
    assert "silence_ack" in {
        c["kind"] for c in proactive.build_candidates(long_idle, T0)}


# --------------------------------------------------------------------------- #
# 20 次 tick：种类覆盖 + 占比 + light 夜间不出
# --------------------------------------------------------------------------- #
def test_twenty_ticks_cover_four_kinds_and_never_exceed_half(tmp_path):
    path = _seed(tmp_path)
    client = FakeClient()
    rows = [_tick(path, now=T0 + i * 180, client=client) for i in range(20)]
    assert all(row["available"] for row in rows), rows[:3]
    kinds = [row["kind"] for row in rows]
    assert {"care", "share", "followup", "light"} <= set(kinds), kinds
    assert len(set(kinds)) >= 4
    for kind in set(kinds):
        assert kinds.count(kind) / len(kinds) <= 0.5, (kind, kinds)
    counts = proactive._kind_counts(path)
    assert sum(counts.values()) == 20
    assert max(counts.values()) / sum(counts.values()) <= 0.5, counts
    # 每个 kind 的 prompt 带上自己的指令（不是同一句通用模板）
    prompts = dict(zip(kinds, client.prompts))
    for kind, prompt in prompts.items():
        assert proactive.KIND_INSTRUCTION[kind] in prompt, kind


def test_light_never_at_night_in_ticks(tmp_path):
    path = _seed(tmp_path, idle_h=6.0)
    rows = [_tick(path, now=NIGHT_T + i * 180,
                  client=FakeClient()) for i in range(6)]
    assert rows and all(row["available"] for row in rows)
    assert "light" not in {row["kind"] for row in rows}


def test_followup_quotes_concrete_recent_content(tmp_path):
    path = _seed(tmp_path, user_text="周末去爬山了，累死")
    rows = [_tick(path, now=T0 + i * 180, client=FakeClient()) for i in range(20)]
    texts = [row["message"] for row in rows if row["kind"] == "followup"]
    assert texts, [row["kind"] for row in rows]
    for text in texts:
        assert "爬山" in text, text          # 引用最近对话里的实词，不是空泛「最近怎么样」
        assert "最近怎么样" not in text


def test_followup_falls_back_when_llm_goes_offtopic(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    now = T0 + 60
    followup = next(c for c in proactive.build_candidates(path, now)
                    if c["kind"] == "followup")
    monkeypatch.setattr(proactive, "build_candidates",
                        lambda p, n=None: [followup])
    client = FakeClient("最近还好吧，你那边呢？")   # 不含锚点 → 触发兜底
    row = proactive.tick(path, client=client, now=now, cooldown=0,
                         min_score=0, respect_state=False)
    assert row["kind"] == "followup"
    assert "爬山" in row["message"], row["message"]


def test_followup_keeps_llm_text_when_anchor_present(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    now = T0 + 60
    followup = next(c for c in proactive.build_candidates(path, now)
                    if c["kind"] == "followup")
    monkeypatch.setattr(proactive, "build_candidates",
                        lambda p, n=None: [followup])
    client = FakeClient("你说的爬山那事后来咋样了")
    row = proactive.tick(path, client=client, now=now, cooldown=0,
                         min_score=0, respect_state=False)
    assert row["message"] == "你说的爬山那事后来咋样了"


def test_silence_ack_at_most_once_per_seven_days(tmp_path):
    path = _seed(tmp_path, idle_h=72.0)
    kinds = [_tick(path, now=T0 + day * 86400, client=FakeClient())["kind"]
             for day in range(15)]
    assert "silence_ack" in kinds
    hit_days = [i for i, kind in enumerate(kinds) if kind == "silence_ack"]
    for earlier, later in zip(hit_days, hit_days[1:]):
        assert later - earlier >= 7, kinds      # 7 天间隔（一天一 tick）
    for i in hit_days:
        assert kinds[i + 1] != "silence_ack"    # 不连续出现


def test_choose_candidate_hard_caps_dominant_kind(tmp_path):
    path = _seed(tmp_path, idle_h=6.0)
    con = db.connect(path)
    for i in range(12):   # 窗口内 care 已占 12/13
        con.execute("INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (f"P-{i}", "care", "x", "r", 0.6, "spoken", T0 - 100 - i))
    con.commit()
    con.close()
    candidates = proactive.build_candidates(path, T0)
    picked = proactive.choose_candidate(candidates, path, T0)
    assert picked is not None and picked["kind"] != "care"


# --------------------------------------------------------------------------- #
# polish_outbound（纯函数）
# --------------------------------------------------------------------------- #
def test_polish_adds_terminal_punctuation_and_collapses_spaces():
    assert proactive.polish_outbound("今天有点累") == "今天有点累。"
    assert proactive.polish_outbound("你在忙吗") == "你在忙吗？"
    assert proactive.polish_outbound("  今天   有点累 ") == "今天 有点累。"
    assert proactive.polish_outbound("还没睡？") == "还没睡？"
    assert proactive.polish_outbound("") == ""
    assert proactive.polish_outbound("   ") == ""


def test_polish_compliant_text_is_byte_identical():
    for text in ("今天有点累，先睡了。", "明天出去吃饭吗？", "好呀！", "嗯，在的。"):
        assert proactive.polish_outbound(text) == text


def test_polish_splits_overlong_text_without_losing_chars():
    text = ("今天上班遇到一件特别离谱的事。同事把咖啡洒在键盘上了。"
            "后来我们一起擦了半天，笑死了。"
            "结果老板进来第一句话问的是这个键盘还能不能保修，我们都不知道该笑还是该慌。")
    assert len(text) > proactive.LONG_TEXT_CHARS
    out = proactive.polish_outbound(text)
    pieces = out.split("\n")
    assert len(pieces) >= 2
    assert "".join(pieces) == text           # 只加分隔符，不漏字
    assert "".join(pieces) != out            # 确有切分
    assert all(piece.endswith("。") for piece in pieces)


# --------------------------------------------------------------------------- #
# worker：发送前质检 + kind 透传
# --------------------------------------------------------------------------- #
class FakeGateway:
    def __init__(self, **cfg):
        self.cfg = {
            "proactive_enabled": True,
            "proactive_probability": 1.0,   # 概率闸门恒开
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


def _fake_tick(kind: str | None, texts: list[str], turn_id: str = "T-PRO-1") -> dict:
    pro = {"ok": True, "available": True, "messages": list(texts),
           "message": texts[0], "turn_id": turn_id}
    if kind is not None:
        pro["kind"] = kind
    return {"ok": True, "ran": True, "gate": {"allowed": True}, "proactive": pro}


@pytest.fixture
def contact_db(tmp_path, monkeypatch):
    path = tmp_path / "contact.sqlite3"
    monkeypatch.setattr("agi.wechat_gateway.config.contact_db", lambda contact: path)
    return path


def test_worker_polishes_before_send_and_logs_kind(monkeypatch, contact_db):
    monkeypatch.setattr(ticker, "run_once",
                        lambda *a, **k: _fake_tick("care", ["在吗   今天有点累"]))
    g = FakeGateway()
    out = proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert out["sent"] == 1
    assert g.sent[0][1] == "在吗 今天有点累。"      # 合并空白 + 补句号
    events = [data for kind, data in g.records if kind == "proactive_send"]
    assert events and events[0]["kind"] == "care"
    assert events[0]["text"] == "在吗 今天有点累。"
    con = sqlite3.connect(contact_db)
    try:
        row = con.execute("SELECT kind, text, status FROM proactive_events").fetchone()
    finally:
        con.close()
    assert row == ("care", "在吗 今天有点累。", "sent")


def test_worker_splits_overlong_bubble_into_separate_sends(monkeypatch, contact_db):
    text = ("今天上班遇到一件特别离谱的事。同事把咖啡洒在键盘上了。"
            "后来我们一起擦了半天，笑死了。"
            "结果老板进来第一句话问的是这个键盘还能不能保修，我们都不知道该笑还是该慌。")
    monkeypatch.setattr(ticker, "run_once",
                        lambda *a, **k: _fake_tick("share", [text]))
    g = FakeGateway()
    out = proactive_worker.run_cycle(g, client=object(), now=100.0)
    sent_texts = [item[1] for item in g.sent]
    assert len(sent_texts) >= 2 and out["sent"] == len(sent_texts)
    assert "".join(sent_texts) == text
    assert all(t.endswith("。") for t in sent_texts)
    events = [data for kind, data in g.records if kind == "proactive_send"]
    assert len(events) == len(sent_texts)
    assert all(event["kind"] == "share" for event in events)


def test_worker_legacy_payload_without_kind_is_untouched(monkeypatch, contact_db):
    """无 kind 的旧 payload 原样透传（P0-2 的既有测试/旧调用不回归）。"""
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _fake_tick(None, ["在干嘛"]))
    g = FakeGateway()
    proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert [item[1] for item in g.sent] == ["在干嘛"]


def test_record_event_persists_kind_and_migrates_old_table(tmp_path):
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE proactive_events(id TEXT PRIMARY KEY, contact TEXT NOT NULL,"
        " ts REAL NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sent',"
        " response TEXT, response_ts REAL);")
    con.commit()
    con.close()
    proactive_worker.record_event(path, "好友A", "在吗", "sent", now=1.0, kind="followup")
    con = sqlite3.connect(path)
    try:
        row = con.execute("SELECT kind, text FROM proactive_events").fetchone()
    finally:
        con.close()
    assert row == ("followup", "在吗")


# --------------------------------------------------------------------------- #
# kind 写进 proactive（selflearn）表，便于统计模板分布
# --------------------------------------------------------------------------- #
def test_tick_records_kind_in_proactive_table(tmp_path):
    path = _seed(tmp_path)
    row = _tick(path, client=FakeClient())
    assert row["available"] is True
    con = db.connect(path)
    try:
        stored = con.execute(
            "SELECT kind FROM proactive ORDER BY created_at DESC LIMIT 1").fetchone()
    finally:
        con.close()
    assert stored["kind"] == row["kind"].strip()
    assert row["kind"] in proactive.KINDS
