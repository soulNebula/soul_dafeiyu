# -*- coding: utf-8 -*-
"""P36：禁止编造网络故障（prompt/guard/net_probe/主动状态）。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agi.selflearn import guard, simple_tick


def test_prompt_capability_and_failure_ban():
    from tools.persona import install_xiaoyu
    text = install_xiaoyu.build_chat_system()
    assert "联网查天气" in text and "B站是后台每天刷的" in text
    assert "不能即时点开视频" in text
    assert "不要编" in text and "连不上网" in text
    assert "实时新闻/天气/股价/网络信息拿不到" not in text


def test_false_failure_guard_and_probe(monkeypatch):
    monkeypatch.setattr(guard, "net_probe", lambda *a, **k: False)
    hit = guard.false_failure_check("我连不上网，页面一直转圈")
    assert hit["hit"] is True and hit["hits"]
    cleaned = guard.strip_false_failure_clauses("今天没看到什么，我连不上网，页面转圈")
    assert "连不上网" not in cleaned and "没看到什么" in cleaned
    monkeypatch.setattr(guard, "net_probe", lambda *a, **k: True)
    assert guard.false_failure_check("我连不上网")["hit"] is False


def test_guard_check_cleans_when_network_ok(monkeypatch):
    monkeypatch.setattr(guard, "net_probe", lambda *a, **k: False)
    out = guard.check("我连不上网，页面转圈")
    assert "连不上网" not in out["reply"]


def test_activity_context_injected(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"day": time.strftime("%Y-%m-%d"), "count": 2,
                                 "picked": 0}), encoding="utf-8")
    monkeypatch.setenv("BILI_DUTY_STATE", str(state))
    from agi.selflearn import surf
    monkeypatch.setattr(surf, "status", lambda *a, **k: {
        "today": {"executed": 3}, "recent_sessions": [{"started_at": time.time(),
                                                       "items_seen": 7}]})
    ctx = simple_tick.today_activity_context()
    assert "bili_today" in ctx and "ran=2" in ctx and "picked=0" in ctx
    assert "surf_today" in ctx and "sessions=3" in ctx and "items=7" in ctx
    prompt = simple_tick._simple_prompt("care", "用户:在吗", "", activity_ctx=ctx)
    assert "bili_today" in prompt and "不要说网络坏了" in prompt


def test_simple_violation_false_failure(monkeypatch):
    monkeypatch.setattr(guard, "net_probe", lambda *a, **k: False)
    reasons = simple_tick._violations("我连不上网", None, [], [])
    assert "false_failure" in reasons
    monkeypatch.setattr(guard, "net_probe", lambda *a, **k: True)
    assert "false_failure" not in simple_tick._violations("我连不上网", None, [], [])
