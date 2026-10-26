# -*- coding: utf-8 -*-
"""P16-B：主动节奏对等（pacing）单元测试（tmp 库 + mock 日志，不联网）。

覆盖：
- balance 统计（messages role=user / proactive_events，7 天窗口，联系人隔离）；
- ratio/deficit/suggested；
- adjust 纯函数：低/高/容差内、概率与间隔夹紧、静默/STOP/日上限、连续无回应；
- worker 接线：本轮用建议概率、记 pacing_adjusted、send=False 时跳过；
- next_interval 使用 pacing_suggested 的建议区间。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agi.selflearn import db
from agi.selflearn import pacing


@pytest.fixture(autouse=True)
def _enable_pacing(monkeypatch):
    """本项目其它测试默认关闭 pacing（见 conftest），pacing 专项测试显式打开。"""
    monkeypatch.setenv("PACING_ENABLED", "1")
    yield

from agi.selflearn import ticker
from agi.wechat_gateway import proactive_worker

DAY = 86400.0
NOW = 1_000_000.0


def _day(hour: int) -> float:
    return time.mktime((2026, 10, 27, hour, 0, 0, 0, 0, -1))


def _make_db(path: Path):
    """建一个 selflearn 联系人库（memory 域，含 proactive_events），返回连接。"""
    db.connect(path)
    proactive_worker._ensure_events(path)   # 网关真实流程会建；测试补上
    return db.memory_connect(path)


def _add_user_message(con, ts: float = NOW - 3600, role: str = "user") -> None:
    con.execute("INSERT INTO messages(id, role, text, ts, proactive) VALUES(?,?,?,?,0)",
                (f"M-{ts}-{role}-{time.time_ns()}", role, "在吗", ts))


def _add_event(con, ts: float = NOW - 3600, *, status: str = "sent",
               kind: str = "check_in", response: str | None = None) -> None:
    con.execute(
        "INSERT INTO proactive_events(id, contact, ts, text, status, kind, response) "
        "VALUES(?,?,?,?,?,?,?)",
        (f"PE-{ts}-{time.time_ns()}", "好友A", ts, "主动一句", status, kind, response))


def _balance_db(tmp_path: Path, *, user: int = 0, agent: int = 0,
                today_agent: int = 0, responded: bool = True):
    path = tmp_path / "contact.sqlite3"
    con = _make_db(path)
    for i in range(user):
        _add_user_message(con, NOW - 3600 - i)
    for i in range(agent):
        _add_event(con, NOW - 3600 - i, response="positive" if responded else None)
    for i in range(today_agent):
        _add_event(con, pacing.day_start(NOW) + 60 + i)
    con.commit()
    con.close()
    return path


# --------------------------------------------------------------------------- #
# 1. balance
# --------------------------------------------------------------------------- #
def test_balance_counts_user_and_agent_proactive(tmp_path):
    path = _balance_db(tmp_path, user=20, agent=5)
    result = pacing.balance(path, NOW, contact="好友A")
    assert result["user_initiated"] == 20
    assert result["agent_proactive"] == 5
    assert result["ratio"] == pytest.approx(0.25)
    assert result["deficit"] == 15
    assert result["window_days"] == 7
    assert result["suggested"]["send"] is True
    assert result["suggested"]["probability"] > 0.55      # 发少了 → 提高概率


def test_balance_isolated_per_contact(tmp_path):
    a = _balance_db(tmp_path / "a", user=10, agent=10)
    b = _balance_db(tmp_path / "b", user=2, agent=8)
    ra = pacing.balance(a, NOW)
    rb = pacing.balance(b, NOW)
    assert ra["ratio"] == pytest.approx(1.0)
    assert rb["ratio"] == pytest.approx(4.0)
    assert ra["deficit"] == 0 and rb["deficit"] == -6


def test_balance_window_and_kind_filter(tmp_path):
    path = tmp_path / "contact.sqlite3"
    con = _make_db(path)
    _add_user_message(con, NOW - 2 * DAY)          # 窗口内
    _add_user_message(con, NOW - 30 * DAY)         # 窗口外
    _add_event(con, NOW - 3600, status="sent", kind="check_in")
    _add_event(con, NOW - 7200, status="sent", kind="reply")   # reply 不算主动
    _add_event(con, NOW - 10800, status="failed", kind="check_in")  # 失败不算
    con.commit()
    con.close()
    result = pacing.balance(path, NOW)
    assert result["user_initiated"] == 1
    assert result["agent_proactive"] == 1
    assert result["ratio"] == pytest.approx(1.0)


def test_balance_today_and_unanswered_streak(tmp_path):
    path = tmp_path / "contact.sqlite3"
    con = _make_db(path)
    for i in range(3):
        _add_event(con, NOW - 100 - i)              # 最近 3 条都没回应
    _add_event(con, NOW - 1000, response="positive")  # 更早的有回应 → 不连续
    con.commit()
    con.close()
    result = pacing.balance(path, NOW)
    assert result["unanswered_streak"] == 3
    assert result["today_agent_proactive"] >= 3


def test_balance_missing_db_and_zero_activity(tmp_path):
    result = pacing.balance(tmp_path / "nope.sqlite3", NOW)
    assert result["user_initiated"] == 0 and result["agent_proactive"] == 0
    assert result["ratio"] == 1.0                       # 双方都没说 → 视为对等
    assert result["suggested"]["send"] is True


def test_balance_user_zero_agent_positive_is_over(tmp_path):
    path = _balance_db(tmp_path, user=0, agent=4, responded=False)
    result = pacing.balance(path, NOW)
    assert result["ratio"] == pytest.approx(4.0)
    assert result["deficit"] == -4
    assert result["suggested"]["probability"] <= 0.55     # 用户没开口，保守


def test_balance_falls_back_to_gateway_log(tmp_path):
    path = tmp_path / "contact.sqlite3"
    con = _make_db(path)
    con.execute("DROP TABLE proactive_events")          # 模拟旧库没有事件表
    _add_user_message(con, NOW - 3600)
    con.commit()
    con.close()
    log = tmp_path / "gateway.log"
    rows = [
        {"ts": NOW - 100, "kind": "proactive_send", "status": "sent",
         "intent_kind": "check_in", "friend": "好友A"},
        {"ts": NOW - 200, "kind": "proactive_send", "status": "sent",
         "intent_kind": "reply", "friend": "好友A"},          # reply 不算
        {"ts": NOW - 300, "kind": "proactive_send", "status": "sent",
         "intent_kind": "check_in", "friend": "别人"},        # 别的联系人
        {"ts": NOW - 20 * DAY, "kind": "proactive_send", "status": "sent",
         "intent_kind": "check_in", "friend": "好友A"},       # 窗口外
    ]
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    result = pacing.balance(path, NOW, contact="好友A", log_path=log)
    assert result["source"] == "gateway_log"
    assert result["agent_proactive"] == 1
    assert result["ratio"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# 2. adjust（纯函数）
# --------------------------------------------------------------------------- #
BASE_CFG = {
    "proactive_probability_min": 0.5,
    "proactive_probability_max": 0.6,
    "proactive_interval_min_s": 3600,
    "proactive_interval_max_s": 7200,
    "quiet_hours": [23, 8],
}


def _b(ratio: float, *, today: int = 0, streak: int = 0) -> dict:
    return {"ratio": ratio, "deficit": 0, "today_agent_proactive": today,
            "unanswered_streak": streak}


def test_adjust_under_raises_probability_shortens_interval():
    out = pacing.adjust(BASE_CFG, _b(0.3), now=_day(12))
    assert out["send"] is True and out["reason"] == "under"
    assert out["probability"] == pytest.approx(0.7)
    assert out["interval_min_s"] == 2160 and out["interval_max_s"] == 4320


def test_adjust_over_lowers_probability_extends_interval():
    out = pacing.adjust(BASE_CFG, _b(2.5), now=_day(12))
    assert out["send"] is True and out["reason"] == "over"
    assert out["probability"] == pytest.approx(0.4)
    assert out["interval_min_s"] == 5400 and out["interval_max_s"] == 10800


def test_adjust_within_tolerance_keeps_base():
    out = pacing.adjust(BASE_CFG, _b(1.0), now=_day(12))
    assert out["reason"] == "within"
    assert out["probability"] == pytest.approx(0.55)
    assert out["interval_min_s"] == 3600 and out["interval_max_s"] == 7200


def test_adjust_probability_and_interval_clamps():
    low = dict(BASE_CFG, proactive_probability_min=0.85, proactive_probability_max=0.9)
    out = pacing.adjust(low, _b(0.1), now=_day(12))
    assert out["probability"] <= 0.9
    high = dict(BASE_CFG, proactive_probability_min=0.2, proactive_probability_max=0.3)
    out = pacing.adjust(high, _b(9.0), now=_day(12))
    assert out["probability"] >= 0.2
    floor = dict(BASE_CFG, pacing_min_interval_s=1500)
    out = pacing.adjust(floor, _b(0.1), now=_day(12))
    assert out["interval_min_s"] >= 1500


def test_adjust_quiet_hours_stop_daily_cap_block_send():
    quiet = pacing.adjust(BASE_CFG, _b(0.3), now=_day(23))
    assert quiet["send"] is False and quiet["reason"] == "quiet_hours"
    stop = pacing.adjust(BASE_CFG, _b(0.3), now=_day(12), stopped=True)
    assert stop["send"] is False and stop["reason"] == "stopped"
    capped = pacing.adjust(BASE_CFG, _b(0.3, today=12), now=_day(12))
    assert capped["send"] is False and capped["reason"] == "daily_cap"
    under_cap = pacing.adjust(BASE_CFG, _b(0.3, today=11), now=_day(12))
    assert under_cap["send"] is True


def test_adjust_no_response_falls_back_conservative():
    out = pacing.adjust(BASE_CFG, _b(0.3, streak=3), now=_day(12))
    assert out["conservative"] is True
    assert out["reason"] == "no_response"
    assert out["probability"] <= 0.2
    assert out["interval_min_s"] >= 3600
    assert out["send"] is True


def test_adjust_custom_target_and_tolerance():
    cfg = dict(BASE_CFG, pacing_target_ratio=0.5, pacing_tolerance=0.1)
    # ratio 0.45 = target 0.5 的 ±10% 内 → 不调整
    within = pacing.adjust(cfg, _b(0.45), now=_day(12))
    assert within["reason"] == "within"
    under = pacing.adjust(cfg, _b(0.2), now=_day(12))
    assert under["reason"] == "under"
    assert under["target_ratio"] == 0.5 and under["tolerance"] == 0.1


def test_adjust_disabled_flag(monkeypatch):
    monkeypatch.setenv("PACING_ENABLED", "0")   # env 优先级最高，这里显式关掉
    out = pacing.adjust(dict(BASE_CFG, pacing_enabled=False), _b(0.3), now=_day(12))
    assert out["applied"] is False
    assert out["probability"] == pytest.approx(0.55)       # 不改
    assert out["interval_min_s"] == 3600


# --------------------------------------------------------------------------- #
# 3. worker 接线
# --------------------------------------------------------------------------- #
class FakeGateway:
    def __init__(self, **cfg):
        self.cfg = {
            "proactive_enabled": True,
            "pacing_enabled": True,
            "allowed_contacts": ["好友A"],
            "proactive_contacts": [],
            "proactive_probability_min": 0.5,
            "proactive_probability_max": 0.6,
            "proactive_interval_min_s": 3600,
            "proactive_interval_max_s": 7200,
            **cfg,
        }
        self.sent: list[tuple] = []
        self.records: list[tuple] = []
        self.last_turns: dict = {}
        self.last_proactive: dict = {}
        self.log_path = None

    def _send(self, contact, text, **kw):
        self.sent.append((contact, text, kw))
        return {"ok": True, "status": "sent"}

    def _record(self, kind, data):
        self.records.append((kind, data))


def _tick(text: str = "在吗") -> dict:
    return {"ok": True, "ran": True, "gate": {"allowed": True},
            "proactive": {"ok": True, "available": True,
                          "messages": [text], "message": text,
                          "turn_id": "T-PRO-1"}}


def _patch_balance(monkeypatch, **kwargs):
    base = {"ratio": 1.0, "deficit": 0, "user_initiated": 10,
            "agent_proactive": 10, "today_agent_proactive": 0,
            "unanswered_streak": 0, "source": "proactive_events"}
    base.update(kwargs)
    monkeypatch.setattr(pacing, "balance", lambda *a, **k: dict(base))


def test_worker_uses_suggested_probability_and_logs(monkeypatch):
    _patch_balance(monkeypatch, ratio=0.3)
    monkeypatch.setattr(pacing, "adjust",
                        lambda cfg, b, **k: {"applied": True, "send": True,
                                             "reason": "under", "probability": 0.9,
                                             "interval_min_s": 1200,
                                             "interval_max_s": 2400,
                                             "target_ratio": 1.0,
                                             "deficit": 0, "ratio": 0.3,
                                             "today_agent_proactive": 0,
                                             "unanswered_streak": 0})
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick())
    monkeypatch.setattr(proactive_worker.random, "random", lambda: 0.5)
    g = FakeGateway()
    out = proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert out["sent"] == 1
    assert out["pacing"]["ratio"] == 0.3
    assert out["pacing"]["probability"] == 0.9
    adjusted = [d for k, d in g.records if k == "pacing_adjusted"]
    assert adjusted and adjusted[0]["ratio"] == 0.3
    assert adjusted[0]["probability"] == 0.9


def test_worker_hold_when_pacing_says_no_send(monkeypatch):
    _patch_balance(monkeypatch, ratio=0.3)
    monkeypatch.setattr(pacing, "adjust",
                        lambda cfg, b, **k: {"applied": True, "send": False,
                                             "reason": "quiet_hours",
                                             "probability": 0.55,
                                             "interval_min_s": 3600,
                                             "interval_max_s": 7200,
                                             "target_ratio": 1.0,
                                             "deficit": 0, "ratio": 0.3,
                                             "today_agent_proactive": 0,
                                             "unanswered_streak": 0})
    called: list[str] = []
    monkeypatch.setattr(ticker, "run_once",
                        lambda *a, **k: called.append("tick") or _tick())
    g = FakeGateway()
    out = proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert out["sent"] == 0 and out["skipped"][0]["reason"] == "pacing:quiet_hours"
    assert called == []                                 # 不查库、不调 LLM
    assert out["pacing"]["reason"] == "quiet_hours"


def test_worker_pacing_disabled_uses_legacy_probability(monkeypatch):
    monkeypatch.setenv("PACING_ENABLED", "0")   # env 优先级最高，这里显式关掉
    called: list[str] = []

    def fail_balance(*a, **k):
        called.append("balance")
        raise AssertionError("pacing 关掉时不该算 balance")

    monkeypatch.setattr(pacing, "balance", fail_balance)
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick())
    monkeypatch.setattr(proactive_worker.random, "random", lambda: 0.0)
    g = FakeGateway(pacing_enabled=False)
    out = proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert called == [] and out["sent"] == 1


def test_worker_pacing_error_falls_back(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db broken")

    monkeypatch.setattr(pacing, "balance", boom)
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick())
    monkeypatch.setattr(proactive_worker.random, "random", lambda: 0.0)
    g = FakeGateway()
    out = proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert out["sent"] == 1                              # 回退旧概率，不炸


# --------------------------------------------------------------------------- #
# 4. next_interval 用 pacing 建议区间
# --------------------------------------------------------------------------- #
def test_next_interval_uses_pacing_suggestion(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(proactive_worker.random, "uniform",
                        lambda lo, hi: calls.append((lo, hi)) or lo)
    g = FakeGateway()
    delay = proactive_worker.next_interval(
        g, last_outcome={"pacing_suggested": {"interval_min_s": 1200,
                                              "interval_max_s": 2400}})
    assert calls == [(1200.0, 2400.0)]
    assert delay == 1200.0


def test_next_interval_without_suggestion_uses_config_bounds(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(proactive_worker.random, "uniform",
                        lambda lo, hi: calls.append((lo, hi)) or lo)
    g = FakeGateway()
    proactive_worker.next_interval(g)                  # 没有 last_outcome
    assert calls == [(3600.0, 7200.0)]
