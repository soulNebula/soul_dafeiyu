# -*- coding: utf-8 -*-
"""P0-12：主动调度间隔必须是 60–7200 秒**均匀随机**，不再由 LLM 决定。

真机 2 小时干净窗口（2026-10-25 17:31–19:30）实测 13 次调度
min/mean/max = 257.7 / **577.6** / 1689.6 秒（期望均值 3600s），
发送率 2.05 条/小时（目标 ≈0.6）——根因是旧实现让 LLM 输出"目标间隔"，
LLM 倾向给几百秒，且有 0.85–1.15 抖动。本文件锁住纯随机语义。
"""
from __future__ import annotations

import statistics

import pytest

from agi.wechat_gateway import proactive_worker
from agi.wechat_gateway.server import Gateway

LO, HI = 60.0, 7200.0
EXPECTED_MEAN = (LO + HI) / 2.0          # 3600s
UNIFORM_STD = (HI - LO) / (12 ** 0.5)    # ≈2061s


class FakeGateway:
    def __init__(self, **cfg):
        self.cfg = dict(cfg)
        self.last_proactive: dict = {}


class _SchedClient:
    """旧 LLM 调度 client 替身：P0-12 后不应再被调用。"""

    def __init__(self):
        self.available_calls = 0
        self.chat_calls = 0

    def available(self):
        self.available_calls += 1
        return True

    def chat(self, *a, **k):
        self.chat_calls += 1
        return {"ok": True, "text": "120"}


def _capture_uniform(monkeypatch, value: float) -> list[tuple]:
    calls: list[tuple] = []

    def fake_uniform(lo, hi):
        calls.append((lo, hi))
        return value

    monkeypatch.setattr(proactive_worker.random, "uniform", fake_uniform)
    return calls


# --------------------------------------------------------------------------- #
# 纯随机 & 参数
# --------------------------------------------------------------------------- #
def test_uniform_called_with_configured_bounds_and_value_passed_through(monkeypatch):
    calls = _capture_uniform(monkeypatch, 1234.567)
    g = FakeGateway(proactive_interval_min_s=60, proactive_interval_max_s=7200)
    delay = proactive_worker.next_interval(g)
    assert calls == [(LO, HI)]
    assert delay == 1234.6                      # 保留一位小数
    # 旧签名参数保留兼容，但不参与计算
    assert proactive_worker.next_interval(g, client=object(),
                                          last_outcome={"sent": 2}, now=1.0) == 1234.6


def test_1000_samples_are_uniform_enough(monkeypatch):
    """统计验收：均值 3600±5%、min≥60、max≤7200、标准差 >1500（分布不退化）。"""
    g = FakeGateway(proactive_interval_min_s=60, proactive_interval_max_s=7200)
    samples = [proactive_worker.next_interval(g) for _ in range(1000)]
    mean = statistics.fmean(samples)
    std = statistics.pstdev(samples)
    assert EXPECTED_MEAN * 0.95 <= mean <= EXPECTED_MEAN * 1.05, mean
    assert min(samples) >= LO and max(samples) <= HI
    assert std > 1500, std
    # 不能退化到旧 LLM 的几百秒量级
    assert mean > 3000, mean
    # 分布应覆盖低/中/高三段（均匀随机的粗检）
    assert any(s < 2000 for s in samples)
    assert any(s > 6000 for s in samples)


def test_llm_client_is_never_called(monkeypatch):
    client = _SchedClient()
    g = FakeGateway(proactive_interval_min_s=60, proactive_interval_max_s=7200)
    for _ in range(10):
        proactive_worker.next_interval(g, client=client)
    assert client.available_calls == 0
    assert client.chat_calls == 0               # 不再为调度花一次 DeepSeek 调用


# --------------------------------------------------------------------------- #
# 非法/缺失配置兜底
# --------------------------------------------------------------------------- #
def test_missing_bounds_fall_back_to_60_7200(monkeypatch):
    calls = _capture_uniform(monkeypatch, 3600.0)
    g = FakeGateway()
    assert proactive_worker.next_interval(g) == 3600.0
    assert calls == [(60.0, 7200.0)]


@pytest.mark.parametrize("cfg,expected", [
    ({"proactive_interval_min_s": None, "proactive_interval_max_s": None}, (60.0, 7200.0)),
    ({"proactive_interval_min_s": "abc", "proactive_interval_max_s": "xyz"}, (60.0, 7200.0)),
    ({"proactive_interval_min_s": 60, "proactive_interval_max_s": -5}, (60.0, 7200.0)),
    ({"proactive_interval_min_s": 5000, "proactive_interval_max_s": 1000}, (5000.0, 7200.0)),
    ({"proactive_interval_min_s": 30, "proactive_interval_max_s": 300}, (30.0, 300.0)),
])
def test_invalid_or_custom_bounds(monkeypatch, cfg, expected):
    calls = _capture_uniform(monkeypatch, 100.0)
    g = FakeGateway(**cfg)
    proactive_worker.next_interval(g)
    assert calls == [expected]


# --------------------------------------------------------------------------- #
# 调度日志 source 字段（验收用）
# --------------------------------------------------------------------------- #
def _gw(tmp_path, **overrides) -> Gateway:
    cfg = {
        "enabled": True, "port": 801, "dry_run": True,
        "allowed_contacts": ["好友A"], "send_mode": "auto",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "proactive_enabled": True,
    }
    cfg.update(overrides)
    return Gateway(cfg)


def test_proactive_schedule_event_records_source_random(tmp_path, monkeypatch):
    g = _gw(tmp_path)
    monkeypatch.setattr(g, "_proactive_cycle", lambda: {"ok": True, "sent": 0})
    monkeypatch.setattr(proactive_worker, "next_interval", lambda *a, **k: 600.0)
    waits: list[float] = []

    def fake_wait(delay):
        waits.append(delay)
        g.stop_event.set()

    monkeypatch.setattr(g.stop_event, "wait", fake_wait)
    g._proactive_loop()
    assert waits == [600.0]
    schedules = [e for e in g.recent if e["kind"] == "proactive_schedule"]
    assert len(schedules) == 1
    assert schedules[0]["next_delay_s"] == 600.0
    assert schedules[0]["source"] == "random"
