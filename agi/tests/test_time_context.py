# -*- coding: utf-8 -*-
"""P16-A：时间上下文注入（被动 system + simple 主动）。"""
from __future__ import annotations

import time

import pytest

from agi.selflearn import chat, config


def _ts(monkeypatch, text):
    # 用本地时区解析固定日期；CI 为 Asia/Shanghai 时稳定
    import datetime
    dt = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
    return dt.timestamp()


def test_time_context_format_and_periods(monkeypatch):
    monkeypatch.setenv("INJECT_TIME_CONTEXT", "1")
    assert config.time_context(_ts(monkeypatch, "2026-10-27 03:24")).startswith(
        "现在是 2026-10-27 03:24（周二）凌晨。")
    cases = {
        "2026-10-27 08:00": "早上",
        "2026-10-27 10:30": "上午",
        "2026-10-27 13:00": "中午",
        "2026-10-27 17:00": "下午",
        "2026-10-27 20:00": "晚上",
        "2026-10-27 23:30": "深夜",
    }
    for stamp, period in cases.items():
        assert period in config.time_context(_ts(monkeypatch, stamp))
    assert "你知道当前时间" in config.time_context(_ts(monkeypatch, "2026-10-27 03:24"))


def test_system_prompt_time_switch(tmp_path, monkeypatch):
    path = tmp_path / "x.sqlite3"
    monkeypatch.setenv("INJECT_TIME_CONTEXT", "0")
    base = chat._system_prompt(path)
    assert "现在是 " not in base
    monkeypatch.setenv("INJECT_TIME_CONTEXT", "1")
    value = chat._system_prompt(path)
    assert value.startswith(base)
    assert "现在是 " in value and "你知道当前时间" in value
    assert "完整代码/脚本交付还没解锁" not in chat.SYSTEM


def test_simple_prompt_time_switch(monkeypatch):
    from agi.selflearn import simple_tick
    monkeypatch.setenv("INJECT_TIME_CONTEXT", "0")
    base = simple_tick._simple_prompt("care", "用户：在吗", "")
    assert "现在是 " not in base
    monkeypatch.setenv("INJECT_TIME_CONTEXT", "1")
    value = simple_tick._simple_prompt("care", "用户：在吗", "")
    assert "现在是 " in value and value.endswith("最近对话：用户：在吗")
