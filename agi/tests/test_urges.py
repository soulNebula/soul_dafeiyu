# -*- coding: utf-8 -*-
"""P34：内驱力队列（urges）测试。全 tmp_path、纯逻辑、不联网。

覆盖：惰性建表 / push 去重·上限·同日同类 / 半衰期 decay / top 阈值与可用性 /
spend·satisfy·cleanup / 触发 helper（冲浪·兴趣·未来事件·心情）/ 坏数据容错 / 开关。
"""
from __future__ import annotations

import json
import time

import pytest

from agi.selflearn import db, urges


@pytest.fixture
def path(tmp_path):
    return tmp_path / "urge_contact.sqlite3"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("URGES_ENABLED", raising=False)


def _active(path) -> list[dict]:
    con = urges._conn(path)
    try:
        return [dict(row) for row in con.execute(
            "SELECT * FROM urges WHERE status='active' ORDER BY intensity DESC"
        ).fetchall()]
    finally:
        con.close()


def _row(path, urge_id):
    con = urges._conn(path)
    try:
        row = con.execute("SELECT * FROM urges WHERE id=?",
                          (urge_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _count(path, status="active") -> int:
    con = urges._conn(path)
    try:
        return int(con.execute("SELECT count(*) FROM urges WHERE status=?",
                               (status,)).fetchone()[0])
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# 1. 惰性建表 / push 基本
# --------------------------------------------------------------------------- #
def test_table_is_created_lazily(path):
    db.memory_connect(path).close()
    con = db.memory_connect(path)
    try:
        names = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        con.close()
    assert "urges" not in names          # 没调用前不存在
    urge_id = urges.push(path, "share_find", "AI", {"summary": "发现"})
    assert urge_id
    assert _count(path) == 1


def test_push_new_and_clamp_intensity(path):
    urge_id = urges.push(path, "light", "想你了", intensity=2.0, now=1000.0)
    row = _row(path, urge_id)
    assert row["status"] == "active"
    assert row["intensity"] == 1.0       # clamp 上限
    assert row["contact"] == "urge_contact"
    low = urges.push(path, "light", "在吗", intensity=-1, now=1000.0)
    assert _row(path, low)["intensity"] == 0.0


def test_push_rejects_unknown_kind_and_bad_payload(path):
    assert urges.push(path, "unknown", "x") is None
    urge_id = urges.push(path, "share_find", "t", None, intensity="bad")
    assert _row(path, urge_id)["intensity"] == 0.0   # 坏强度按 0 处理


def test_push_dedupes_and_raises_intensity(path):
    first = urges.push(path, "share_find", "AI 新闻", intensity=0.5, now=1000.0)
    second = urges.push(path, "share_find", "AI 新闻", intensity=0.6, now=1000.0 + 3600)
    assert second == first                        # 不新增
    assert _count(path) == 1
    assert _row(path, first)["intensity"] == pytest.approx(0.7)  # max+0.1


def test_push_same_kind_daily_limit(path):
    base = 1000.0
    ids = [urges.push(path, "share_thought", f"主题{i}", intensity=0.5,
                      now=base) for i in range(4)]
    assert all(ids[:3]) and ids[3] is None       # 同日同类最多 3
    assert _count(path) == 3
    next_day = urges.push(path, "share_thought", "第二天", intensity=0.5,
                          now=base + 86400)
    assert next_day is not None


def test_push_queue_limit_drops_weakest(path):
    base = 2000.0
    kinds = ["share_find", "share_thought", "ask_about", "light"]
    made = []
    for index in range(8):
        kind = kinds[index % len(kinds)]
        urge_id = urges.push(path, kind, f"topic-{index}",
                             intensity=0.55 + index * 0.01, now=base)
        assert urge_id
        made.append(urge_id)
    assert _count(path) == 8
    # 第 9 条更强 → 丢最弱；仍 8 条
    strong = urges.push(path, "share_find", "更强的", intensity=0.99, now=base)
    assert strong is not None
    assert _count(path) == 8
    weakest = made[0]
    assert _row(path, weakest)["status"] == "dropped"
    # 新欲望更弱 → 不进队
    weak = urges.push(path, "share_find", "太弱了", intensity=0.1, now=base)
    assert weak is None
    assert _count(path) == 8


# --------------------------------------------------------------------------- #
# 2. 衰减 / top
# --------------------------------------------------------------------------- #
def test_decay_uses_kind_half_life(tmp_path):
    base = 1000.0
    for kind, half_life in (("share_find", 3.0), ("share_thought", 1.5),
                            ("light", 0.75), ("progress_check", 12.0)):
        sub = tmp_path / f"{kind}.sqlite3"
        urge_id = urges.push(sub, kind, "topic", intensity=0.8, now=base)
        # progress_check 先过 24h 可用期才开始衰减
        at = base + int((24 + half_life) * 3600) if kind == "progress_check"             else base + int(half_life * 3600)
        urges.decay(sub, at)
        expected = 0.4 if kind != "progress_check" else 0.4
        assert _row(sub, urge_id)["intensity"] == pytest.approx(expected, abs=0.02), kind


def test_decay_is_incremental_not_double(path):
    base = 1000.0
    urge_id = urges.push(path, "share_find", "x", intensity=0.8, now=base)
    urges.decay(path, base + 3600)
    once = _row(path, urge_id)["intensity"]
    urges.decay(path, base + 3600)        # 同一时刻再 decay 不应再降
    assert _row(path, urge_id)["intensity"] == pytest.approx(once)


def test_top_threshold_and_strongest(path):
    base = 1000.0
    low = urges.push(path, "light", "low", intensity=0.2, now=base)
    assert urges.top(path, now=base) is None
    urges.push(path, "share_find", "strong", intensity=0.9, now=base)
    urges.push(path, "share_thought", "medium", intensity=0.7, now=base)
    item = urges.top(path, now=base)
    assert item is not None and item["kind"] == "share_find"
    assert item["intensity"] >= 0.9
    # min_intensity 可调
    assert urges.top(path, now=base, min_intensity=0.95) is None
    assert _row(path, low)["status"] == "active"


def test_top_changes_after_decay(path):
    base = 1000.0
    find = urges.push(path, "share_find", "快凉", intensity=0.8, now=base)
    thought = urges.push(path, "share_thought", "也凉", intensity=0.7, now=base)
    urges.push(path, "progress_check", "长跑", intensity=0.65, now=base)
    # 1.5h 后 share_thought 掉到 0.35，share_find 掉到 0.566 → find 仍强
    assert urges.top(path, now=base + int(1.5 * 3600))["kind"] == "share_find"
    # 9h 后 find=0.8*0.5^3=0.1（低于阈值），progress_check 还没到 24h → None
    assert urges.top(path, now=base + 9 * 3600) is None


def test_progress_check_waits_24h(path):
    base = 1000.0
    urge_id = urges.push(path, "progress_check", "面试", {"event": "面试"},
                         intensity=0.6, now=base)
    assert _row(path, urge_id)["intensity"] == pytest.approx(0.6)
    urges.decay(path, base + 6 * 3600)     # 未到 available_at，不衰减
    assert _row(path, urge_id)["intensity"] == pytest.approx(0.6)
    assert urges.top(path, now=base + 6 * 3600) is None     # 24h 内不可用
    assert urges.top(path, now=base + 25 * 3600) is not None  # 到点可用
    assert urges.top(path, now=base + 25 * 3600)["kind"] == "progress_check"


def test_list_active_and_status(path):
    base = 1000.0
    urges.push(path, "share_find", "a", intensity=0.8, now=base)
    urges.push(path, "light", "b", intensity=0.4, now=base)
    for index in range(7):
        urges.push(path, "share_thought", f"c{index}", intensity=0.3, now=base)
    items = urges.list_active(path, now=base, limit=5)
    assert len(items) == 5
    assert items[0]["kind"] == "share_find"
    assert all("age_hours" in item for item in items)
    snapshot = urges.status(path, now=base)
    assert snapshot["enabled"] is True
    assert snapshot["count"] == 5 and len(snapshot["items"]) == 5
    assert snapshot["contact"] == "urge_contact"


# --------------------------------------------------------------------------- #
# 3. spend / satisfy / cleanup
# --------------------------------------------------------------------------- #
def test_spend_marks_spent_and_counts_attempts(path):
    base = 1000.0
    urge_id = urges.push(path, "share_find", "x", intensity=0.8, now=base)
    assert urges.spend(path, urge_id, now=base + 10) is True
    row = _row(path, urge_id)
    assert row["status"] == "spent" and row["attempts"] == 1
    assert urges.spend(path, urge_id) is False       # 不能重复 spend
    assert urges.spend(path, "不存在") is False


def test_satisfy_marks_contact_active_as_spent(path):
    base = 1000.0
    a = urges.push(path, "share_find", "A", intensity=0.8, contact="好友A", now=base)
    b = urges.push(path, "light", "B", intensity=0.6, contact="好友B", now=base)
    assert urges.satisfy(path, "好友A", now=base + 10) == 1
    assert _row(path, a)["status"] == "spent"
    assert _row(path, a)["intensity"] == pytest.approx(0.24)   # ×0.3
    assert _row(path, b)["status"] == "active"
    assert urges.satisfy(path, "好友A") == 0       # 没有 active 了


def test_cleanup_drops_faint_or_old_urges(path):
    base = 1000.0
    faint = urges.push(path, "light", "faint", intensity=0.02, now=base)
    stale = urges.push(path, "share_find", "stale", intensity=0.9, now=base)
    fresh = urges.push(path, "share_find", "fresh", intensity=0.9, now=base + 80000)
    con = urges._conn(path)
    try:
        con.execute("UPDATE urges SET created_at=? WHERE id=?",
                    (base - 2 * 86400, stale))
        con.commit()
    finally:
        con.close()
    dropped = urges.cleanup(path, now=base + 90000)
    assert dropped >= 2
    assert _row(path, faint)["status"] == "dropped"
    assert _row(path, stale)["status"] == "dropped"
    assert _row(path, fresh)["status"] == "active"


def test_cleanup_keeps_progress_check_before_available(path):
    base = 1000.0
    urge_id = urges.push(path, "progress_check", "面试", intensity=0.6, now=base)
    dropped = urges.cleanup(path, now=base + 12 * 3600)
    assert dropped == 0
    assert _row(path, urge_id)["status"] == "active"


# --------------------------------------------------------------------------- #
# 4. 触发 helper
# --------------------------------------------------------------------------- #
def test_observe_surf_signal_and_score(path):
    assert urges.observe_surf(path, summary="今天看了个普通技术贴", now=1000.0) is None
    urge_id = urges.observe_surf(
        path, summary="这个发现挺有意思，值得说一嘴",
        keywords=["AI"], items=[{"title": "发现"}], now=1000.0)
    assert urge_id
    row = _row(path, urge_id)
    assert row["kind"] == "share_find"
    assert row["topic"] == "AI"
    payload = json.loads(row["payload_json"])
    assert "有意思" in payload["summary"]
    # score 高无需关键词信号
    scored = urges.observe_surf(path, summary="普通内容", score=0.9, now=1000.0)
    assert scored is not None


def test_observe_interest_threshold(path):
    assert urges.observe_interest(path, "摄影", delta=0.05) is None
    urge_id = urges.observe_interest(path, "摄影", delta=0.2, now=1000.0)
    assert urge_id is not None
    row = _row(path, urge_id)
    assert row["kind"] == "share_thought" and row["topic"] == "摄影"


def test_observe_future_event_progress_check(path):
    assert urges.observe_future_event(path, "今天吃了火锅") is None
    urge_id = urges.observe_future_event(path, "下周要面试了，好紧张", now=1000.0)
    assert urge_id is not None
    row = _row(path, urge_id)
    assert row["kind"] == "progress_check"
    assert row["topic"] == "面试"                # 具体事件优先（面试 > 下周）
    payload = json.loads(row["payload_json"])
    assert payload["available_at"] == pytest.approx(1000.0 + 86400)
    assert "面试" in payload["source_text"]
    # 只有「下周」没有具体事件时，topic 用「下周的安排」
    weekly = urges.observe_future_event(path, "下周找个时间聚一下", now=1000.0)
    assert _row(path, weekly)["topic"] == "下周的安排"


def test_observe_mood_extremes(path):
    assert urges.observe_mood(path, 0.55, now=1000.0) is None
    high = urges.observe_mood(path, 0.85, now=1000.0)
    low = urges.observe_mood(path, 0.2, now=1000.0)
    assert high and low
    assert _row(path, high)["kind"] == "light"
    assert _row(path, low)["kind"] == "light"


# --------------------------------------------------------------------------- #
# 5. 容错 / 开关 / 联系人隔离
# --------------------------------------------------------------------------- #
def test_corrupt_rows_do_not_break_reads(path):
    urges.push(path, "share_find", "ok", intensity=0.8, now=1000.0)
    con = urges._conn(path)
    try:
        con.execute(
            "INSERT INTO urges(id,contact,kind,topic,payload_json,intensity,"
            "created_at,updated_at,status,ref_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("U-bad", "urge_contact", "share_find", "坏数据", "not-json", 0.9,
             1000.0, 1000.0, "active", "{bad"))
        con.commit()
    finally:
        con.close()
    assert urges.top(path, now=1000.0) is not None     # 不抛异常
    assert urges.list_active(path, now=1000.0, limit=5)  # 同样不炸
    urges.decay(path, now=1000.0)
    urges.cleanup(path, now=1000.0)


def test_urges_enabled_switch(monkeypatch):
    assert urges.enabled() is True
    monkeypatch.setenv("URGES_ENABLED", "0")
    assert urges.enabled() is False
    monkeypatch.setenv("URGES_ENABLED", "1")
    assert urges.enabled() is True


def test_contact_isolation(path):
    base = 1000.0
    a = urges.push(path, "share_find", "A", intensity=0.8, contact="A", now=base)
    b = urges.push(path, "light", "B", intensity=0.6, contact="B", now=base)
    items = urges.list_active(path, now=base, limit=5, contact="A")
    assert [item["id"] for item in items] == [a]
    urges.satisfy(path, "A")
    assert _row(path, a)["status"] == "spent"
    assert _row(path, b)["status"] == "active"


def test_failure_does_not_raise_on_bad_path(tmp_path):
    bad = tmp_path / "not_a_dir"
    bad.mkdir()
    # 目录当 DB 路径：所有入口都应静默返回，不抛异常
    assert urges.push(bad, "light", "x") is None
    assert urges.decay(bad) == 0
    assert urges.top(bad) is None
    assert urges.cleanup(bad) == 0
    assert urges.satisfy(bad, "x") == 0
    assert urges.list_active(bad) == []


def test_surf_session_end_pushes_share_find(tmp_path):
    from agi.selflearn import surf

    db_path = tmp_path / "surf_urge.sqlite3"

    def fetch_stub(keyword, limit):
        return {"ok": True, "source": "test",
                "items": [{"title": f"{keyword} 新发现", "summary": "一条发现",
                          "url": "", "source": "test"}]}

    session = surf.run_session(
        {"dice": 6, "factor": 0.9, "duration_min": 5, "items": 3,
         "source": "scheduled", "started_at": time.time() - 60},
        now=time.time(), db_path=db_path, fetch_fn=fetch_stub,
        observe_fn=lambda texts, **kw: None, keywords=["手冲咖啡"])
    assert session["ok"] is True and session["items_seen"] >= 1
    items = urges.list_active(db_path, now=time.time(), limit=5)
    assert any(item["kind"] == "share_find" for item in items), items


def test_surf_session_without_signal_does_not_push(tmp_path, monkeypatch):
    from agi.selflearn import surf

    monkeypatch.setattr(surf, "summarize", lambda items, kws: "这轮没看到什么")
    db_path = tmp_path / "surf_nosignal.sqlite3"

    def fetch_stub(keyword, limit):
        return {"ok": True, "source": "test",
                "items": [{"title": "普通", "summary": "没什么",
                          "url": "", "source": "test"}]}

    surf.run_session(
        {"dice": 3, "factor": 0.7, "duration_min": 3, "items": 1,
         "source": "scheduled", "started_at": time.time() - 60},
        now=time.time(), db_path=db_path, fetch_fn=fetch_stub,
        observe_fn=lambda texts, **kw: None, keywords=["杂项"])
    assert urges.list_active(db_path, now=time.time(), limit=5) == []
