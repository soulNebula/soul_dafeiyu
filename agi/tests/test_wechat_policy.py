# -*- coding: utf-8 -*-
"""801 发送策略测试。"""
from __future__ import annotations

from agi.wechat_gateway.policy import check_send, in_quiet_hours


def test_quiet_hours_wraps_midnight():
    assert in_quiet_hours(2, 0, 7) is True
    assert in_quiet_hours(7, 0, 7) is False
    assert in_quiet_hours(23, 22, 6) is True
    assert in_quiet_hours(12, 22, 6) is False


def test_send_policy_blocks():
    base = {"stop": False, "quiet_hours": [0, 7], "rate_per_minute": 2, "daily_limit": 10}
    assert check_send(now=0, hour=3, recent_60s=0, today_count=0, cfg=base)["reason"] == "quiet_hours"
    assert check_send(now=0, hour=12, recent_60s=2, today_count=0, cfg=base)["reason"] == "rate_limit"
    assert check_send(now=0, hour=12, recent_60s=0, today_count=10, cfg=base)["reason"] == "daily_limit"
    assert check_send(now=0, hour=12, recent_60s=0, today_count=0, cfg=base)["ok"] is True
    assert check_send(now=0, hour=12, recent_60s=0, today_count=0, cfg={**base, "stop": True})["reason"] == "stop_switch"


def test_reactive_rate_is_higher():
    cfg = {"stop": False, "quiet_hours": [], "rate_per_minute": 3,
           "reactive_rate_per_minute": 8, "daily_limit": 200}
    assert check_send(now=0, hour=12, recent_60s=3, today_count=0, cfg=cfg)["reason"] == "rate_limit"
    assert check_send(now=0, hour=12, recent_60s=3, today_count=0, cfg=cfg,
                      reactive=True)["ok"] is True
