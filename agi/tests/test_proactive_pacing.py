# -*- coding: utf-8 -*-
"""P0-1 主动消息节奏（2026-10-25 用户拍板的新语义）：

- 间隔：60–7200 秒（1–120 分钟）随机；
- 每轮发送概率：在 [0.3, 0.9] 内随机采（替代固定 0.3）；
- 夜间静音：23:00–08:00（跨零点）。

对应故障（gateway.log 审计）：177 次 proactive_send，8.1 小时 95 条成功，
中位间隔 3.0 分钟、峰值 20 条/小时；根因是旧配置 30–300 秒 + 每轮必发。
"""
from __future__ import annotations

import pytest

from agi.selflearn import ticker
from agi.wechat_gateway import config as gw_config
from agi.wechat_gateway import policy, proactive_worker

OLD_MIN_S = 30
OLD_MAX_S = 300
NEW_MIN_S = 60      # 1 分钟
NEW_MAX_S = 7200    # 120 分钟
P_MIN = 0.3
P_MAX = 0.9
QUIET = [23, 8]     # 23:00–08:00（跨零点）


class FakeGateway:
    """proactive_worker.run_cycle 需要的最小网关替身。"""

    def __init__(self, **cfg):
        self.cfg = {
            "proactive_enabled": True,
            "proactive_probability_min": P_MIN,
            "proactive_probability_max": P_MAX,
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


def _tick_one(turn_id="T-PRO-1"):
    """一次只有一个气泡，方便精确数「发送调用次数」。"""
    return {"ok": True, "ran": True, "gate": {"allowed": True},
            "proactive": {"ok": True, "available": True,
                          "messages": ["在吗"], "message": "在吗",
                          "turn_id": turn_id}}


class _NoLLMClient:
    def available(self):
        return False


class _SchedClient:
    def __init__(self, text):
        self.text = text

    def available(self):
        return True

    def chat(self, *a, **k):
        return {"ok": True, "text": self.text}


# ------------------------------------------------------------------ 间隔默认值
def test_interval_defaults_are_1_to_120_minutes():
    lo = gw_config.DEFAULTS["proactive_interval_min_s"]
    hi = gw_config.DEFAULTS["proactive_interval_max_s"]
    assert (lo, hi) == (NEW_MIN_S, NEW_MAX_S)
    assert lo != OLD_MIN_S and hi != OLD_MAX_S
    assert lo < hi


def test_next_interval_stays_in_new_bounds():
    g = FakeGateway(**{k: gw_config.DEFAULTS[k] for k in
                       ("proactive_interval_min_s", "proactive_interval_max_s")})
    for _ in range(25):
        delay = proactive_worker.next_interval(g, client=_NoLLMClient())
        assert NEW_MIN_S <= delay <= NEW_MAX_S, delay
        assert delay not in (OLD_MIN_S, OLD_MAX_S), delay


def test_next_interval_clamps_llm_idea_into_new_bounds():
    g = FakeGateway(**{k: gw_config.DEFAULTS[k] for k in
                       ("proactive_interval_min_s", "proactive_interval_max_s")})
    assert proactive_worker.next_interval(g, client=_SchedClient("99999")) <= NEW_MAX_S
    assert proactive_worker.next_interval(g, client=_SchedClient("1")) >= NEW_MIN_S


# ------------------------------------------------------------------ 概率区间
def test_probability_defaults_are_range():
    assert gw_config.DEFAULTS["proactive_probability_min"] == pytest.approx(P_MIN)
    assert gw_config.DEFAULTS["proactive_probability_max"] == pytest.approx(P_MAX)
    # 旧固定键不再出现在默认配置里（仅作为旧配置兼容读取）
    assert "proactive_probability" not in gw_config.DEFAULTS


def test_probability_uses_uniform_range(monkeypatch):
    calls: list[tuple] = []

    def fake_uniform(lo, hi):
        calls.append((lo, hi))
        return 0.5

    monkeypatch.setattr(proactive_worker.random, "uniform", fake_uniform)
    monkeypatch.setattr(proactive_worker.random, "random", lambda: 0.0)
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick_one())
    g = FakeGateway()
    proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert calls == [(P_MIN, P_MAX)]
    assert len(g.sent) == 1


def test_probability_varies_across_rounds():
    g = FakeGateway()
    samples = [proactive_worker._probability(g) for _ in range(80)]
    assert all(P_MIN <= p <= P_MAX for p in samples)
    assert len({round(p, 3) for p in samples}) > 10   # 不是固定值


def test_probability_hit_sends(monkeypatch):
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick_one())
    monkeypatch.setattr(proactive_worker.random, "uniform", lambda lo, hi: 0.6)
    monkeypatch.setattr(proactive_worker.random, "random", lambda: 0.0)  # 0.0 < 0.6
    g = FakeGateway()
    out = proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert len(g.sent) == 1
    assert out["sent"] == 1
    assert not any(k == "proactive_skipped" for k, _ in g.records)


def test_probability_miss_skips_and_logs(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(ticker, "run_once",
                        lambda *a, **k: calls.append(1) or _tick_one())
    monkeypatch.setattr(proactive_worker.random, "uniform", lambda lo, hi: 0.6)
    monkeypatch.setattr(proactive_worker.random, "random", lambda: 0.99)  # 0.99 >= 0.6
    g = FakeGateway()
    out = proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert calls == []                  # 跳过时不该再去跑 LLM
    assert g.sent == []                 # 发送调用次数 0
    assert out["sent"] == 0 and out["ran"] is False
    assert out["reason"] == "probability_skip"
    skipped = [data for kind, data in g.records if kind == "proactive_skipped"]
    assert len(skipped) == 1, g.records
    assert skipped[0]["probability"] == pytest.approx(0.6)   # 带本次采样的 p
    assert skipped[0]["probability_range"] == [P_MIN, P_MAX]
    assert skipped[0]["roll"] == pytest.approx(0.99)


@pytest.mark.parametrize("probability,roll,sends", [
    (0.0, 0.0, False),      # 概率 0：再低的随机数也不发
    (1.0, 0.999, True),     # 概率 1：永远发
])
def test_probability_extremes(monkeypatch, probability, roll, sends):
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick_one())
    monkeypatch.setattr(proactive_worker.random, "random", lambda: roll)
    g = FakeGateway(proactive_probability_min=probability,
                    proactive_probability_max=probability)
    proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert len(g.sent) == (1 if sends else 0)


def test_legacy_fixed_probability_is_still_honored():
    """旧配置只有 proactive_probability：当固定值用（不随机）。"""
    g = FakeGateway()
    g.cfg.pop("proactive_probability_min")
    g.cfg.pop("proactive_probability_max")
    g.cfg["proactive_probability"] = 0.5
    assert proactive_worker.probability_range(g) == (0.5, 0.5)
    assert proactive_worker._probability(g) == pytest.approx(0.5)


def test_missing_probability_keeps_old_always_send(monkeypatch):
    """旧配置/旧测试三个概率键都没有：不引入随机闸门（向下兼容）。"""
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick_one())
    monkeypatch.setattr(proactive_worker.random, "random", lambda: 0.999)
    g = FakeGateway()
    for key in ("proactive_probability", "proactive_probability_min",
                "proactive_probability_max"):
        g.cfg.pop(key, None)
    out = proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert out["sent"] == 1 and len(g.sent) == 1


# ------------------------------------------------------------------ 夜间静音
def test_quiet_hours_default_is_23_to_8():
    assert gw_config.DEFAULTS["quiet_hours"] == QUIET


@pytest.mark.parametrize("hour,expected", [
    (23, True),   # 静音起点
    (0, True),    # 跨零点后
    (7, True),    # 静音末尾（不含 8）
    (8, False),   # 边界：解除静音
    (12, False),
    (22, False),  # 边界：尚未静音
])
def test_in_quiet_hours_cross_midnight(hour, expected):
    assert policy.in_quiet_hours(hour, *QUIET) is expected
    # 旧的 int 小时语义 / 同值区间保持不变
    assert policy.in_quiet_hours(hour, 0, 0) is False


@pytest.mark.parametrize("hour,allowed", [
    (23, False), (0, False), (7, False), (8, True), (12, True), (22, True),
])
def test_check_send_blocks_on_cross_midnight_quiet(hour, allowed):
    cfg = {"stop": False, "quiet_hours": list(QUIET), "rate_per_minute": 3,
           "reactive_rate_per_minute": 8, "daily_limit": 200, "hourly_limit": 40,
           "min_interval_s": 0}
    verdict = policy.check_send(now=0.0, hour=hour, recent_60s=0,
                                today_count=0, cfg=cfg)
    assert verdict["ok"] is allowed
    if not allowed:
        assert verdict["reason"] == "quiet_hours"
