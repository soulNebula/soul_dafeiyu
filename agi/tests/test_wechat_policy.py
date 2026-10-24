# -*- coding: utf-8 -*-
"""801 发送策略测试（频率窗口 + 内容闸门）。"""
from __future__ import annotations

from agi.wechat_gateway.policy import check_content, check_send, in_quiet_hours


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


def test_hourly_limit():
    cfg = {"stop": False, "quiet_hours": [], "rate_per_minute": 99,
           "hourly_limit": 5, "daily_limit": 200}
    assert check_send(now=100, hour=12, recent_60s=0, today_count=0, cfg=cfg,
                      hourly_count=5)["reason"] == "hourly_limit"
    assert check_send(now=100, hour=12, recent_60s=0, today_count=0, cfg=cfg,
                      hourly_count=4)["ok"] is True


def test_min_interval_only_for_proactive():
    cfg = {"stop": False, "quiet_hours": [], "rate_per_minute": 99,
           "min_interval_s": 2.0, "daily_limit": 200}
    # 主动发送：距上次 1 秒 → 拦
    assert check_send(now=100, hour=12, recent_60s=0, today_count=0, cfg=cfg,
                      last_sent_at=99)["reason"] == "min_interval"
    # 被动回复：多气泡连续发，不能被卡
    assert check_send(now=100, hour=12, recent_60s=0, today_count=0, cfg=cfg,
                      last_sent_at=99, reactive=True)["ok"] is True
    # 距上次 3 秒 → 放行
    assert check_send(now=100, hour=12, recent_60s=0, today_count=0, cfg=cfg,
                      last_sent_at=97)["ok"] is True


def test_content_gate():
    cfg = {"max_chars": 20}
    big = {"max_chars": 500}
    assert check_content("", cfg)["reason"] == "empty_text"
    assert check_content("x" * 21, cfg)["reason"] == "too_long"
    assert check_content("C:\\Users\\a\\secret.txt", big)["reason"] == "sensitive"
    assert check_content("API key: abc123", big)["reason"] == "sensitive"
    assert check_content("wxid_abcdef123456", big)["reason"] == "sensitive"
    assert check_content("sk-" + "a" * 20, big)["reason"] == "sensitive"
    # 正常聊天内容
    assert check_content("今天天气不错", cfg)["ok"] is True
    assert check_content("帮你写了个小脚本，看看行不行", cfg)["ok"] is True
