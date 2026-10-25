# -*- coding: utf-8 -*-
"""P0-25：夜间静默只约束主动消息，用户自己发消息必须回。

2026-10-26 真机事故：凌晨 4 点用户在微信里发消息，gateway 生成了回复但被
``_send`` 的 quiet_hours 硬保护全部拦掉（3 条 ``send_blocked reason=quiet_hours``），
用户看到的现象是"发了消息没回复"。静默的本意是"不主动打扰"，不是"用户找我我也不理"。
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from agi.wechat_gateway import policy, server as S


class _OkBackend:
    def send(self, contact, text):
        return {"ok": True, "status": "sent"}


def _gateway(**over):
    cfg = {
        "allowed_contacts": ["x"],
        "monitored_contacts": ["x"],
        "send_mode": "auto",
        "dry_run": True,
        "quiet_hours": [0, 24],          # 全天静默：最容易复现事故
        "stop": False,
        "admin_token": "t",
        "max_chars": 500,
    }
    cfg.update(over)
    gw = S.Gateway(cfg)
    gw.backend = _OkBackend()
    gw.draft_path = Path(tempfile.mkdtemp()) / "drafts.jsonl"
    return gw


def test_reactive_reply_not_blocked_by_quiet_hours():
    gw = _gateway()
    assert gw._quiet_blocked() is True
    out = gw._send("x", "在的，咋了", reactive=True)
    assert out.get("status") != "blocked", out
    assert out.get("error") != "quiet_hours"


def test_proactive_still_blocked_by_quiet_hours():
    gw = _gateway()
    out = gw._send("x", "主动消息", proactive=True)
    assert out.get("status") == "blocked"
    assert out.get("error") == "quiet_hours"


def test_manual_force_send_not_blocked_by_quiet_hours():
    gw = _gateway()
    out = gw._send("x", "手动补发", force=True)
    assert out.get("status") != "blocked", out


def test_stop_file_still_blocks_reactive():
    """STOP 是最高优先级：连被动回复也要停。"""
    stop = Path(tempfile.mkdtemp()) / "STOP"
    stop.write_text("", encoding="utf-8")
    cfg = {
        "allowed_contacts": ["x"], "monitored_contacts": ["x"], "send_mode": "auto",
        "dry_run": True, "quiet_hours": [0, 24], "stop": False,
        "admin_token": "t", "max_chars": 500,
    }
    gw = S.Gateway(cfg, stop_path=stop)
    gw.backend = _OkBackend()
    gw.draft_path = Path(tempfile.mkdtemp()) / "drafts.jsonl"
    out = gw._send("x", "在的", reactive=True)
    assert out.get("status") == "blocked"
    assert out.get("error") == "stop_file"


def test_policy_check_send_reactive_ignores_quiet():
    cfg = {"quiet_hours": [0, 24], "stop": False}
    out = policy.check_send(now=time.time(), hour=3, recent_60s=0, today_count=0,
                            cfg=cfg, reactive=True)
    assert out["ok"] is True, out
    out2 = policy.check_send(now=time.time(), hour=3, recent_60s=0, today_count=0,
                             cfg=cfg, reactive=False)
    assert out2["ok"] is False and out2["reason"] == "quiet_hours"
