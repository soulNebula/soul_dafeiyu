# -*- coding: utf-8 -*-
"""风控：连续环境类发送失败自动熔断（跨重启保持，需人工 reset）。

适用范围：``send_timeout`` / ``sender_unavailable`` 这类"微信侧异常"失败；
内容检查/闸门拦截/普通发送失败不计入。默认阈值 2，可用
``send_breaker_threshold`` 调整，``send_breaker_enabled=false`` 可整体关闭。
"""
from __future__ import annotations

from pathlib import Path

from agi.wechat_gateway import config as gw_config
from agi.wechat_gateway.server import Gateway


def _gw(tmp_path, **overrides) -> Gateway:
    cfg = {
        "enabled": True, "allowed_contacts": ["好友A"],
        "monitored_contacts": ["好友A"], "admin_token": "tok",
        "dry_run": True, "send_mode": "auto", "quiet_hours": [0, 0],
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
    }
    cfg.update(overrides)
    return Gateway(cfg)


def test_default_timeout_is_twenty_seconds():
    assert gw_config.DEFAULTS["send_timeout_s"] == 20
    assert gw_config.DEFAULTS["send_breaker_enabled"] is True
    assert gw_config.DEFAULTS["send_breaker_threshold"] == 2


def test_two_consecutive_timeouts_open_breaker_and_block_sends(tmp_path):
    gw = _gw(tmp_path)
    gw._note_send_failure("send_timeout")
    assert gw.send_breaker["open"] is False
    gw._note_send_failure("send_timeout")
    assert gw.send_breaker["open"] is True

    out = gw._send("好友A", "熔断后不允许发")
    assert out["ok"] is False and out["status"] == "blocked"
    assert out["error"] == "send_breaker_open"
    # 人工/force 也不能绕（风控优先于一切放行开关）
    manual = gw._send("好友A", "人工也不行", force=True, manual=True)
    assert manual["error"] == "send_breaker_open"


def test_success_resets_consecutive_before_threshold(tmp_path):
    gw = _gw(tmp_path)
    gw._note_send_failure("send_timeout")
    gw._note_send_success()
    gw._note_send_failure("sender_unavailable")
    assert gw.send_breaker["open"] is False
    assert gw.send_breaker["consecutive"] == 1


def test_reset_allows_sending_again(tmp_path):
    gw = _gw(tmp_path)
    gw._note_send_failure("send_timeout")
    gw._note_send_failure("send_timeout")
    assert gw._send("好友A", "x")["error"] == "send_breaker_open"
    gw.reset_send_breaker()
    assert gw.send_breaker["open"] is False
    out = gw._send("好友A", "恢复后的一条")
    assert out["ok"] is True and out["status"] == "planned"


def test_breaker_survives_restart(tmp_path):
    first = _gw(tmp_path)
    first._note_send_failure("send_timeout")
    first._note_send_failure("send_timeout")
    assert first.send_breaker["open"] is True
    assert (tmp_path / "send_breaker.json").is_file()

    second = _gw(tmp_path)
    assert second.send_breaker["open"] is True
    assert second._send("好友A", "重启也不行")["error"] == "send_breaker_open"


def test_threshold_is_configurable(tmp_path):
    gw = _gw(tmp_path, send_breaker_threshold=1)
    assert gw._note_send_failure("send_timeout")["open"] is True


def test_breaker_can_be_disabled(tmp_path):
    gw = _gw(tmp_path, send_breaker_enabled=False)
    gw._note_send_failure("send_timeout")
    gw._note_send_failure("send_timeout")
    assert gw.send_breaker["open"] is False
    out = gw._send("好友A", "关闭熔断后照常")
    assert out["ok"] is True and out["status"] == "planned"


def test_status_exposes_breaker(tmp_path):
    gw = _gw(tmp_path)
    gw._note_send_failure("send_timeout")
    gw._note_send_failure("send_timeout")
    status = gw.status()
    assert status["send_breaker"]["open"] is True
    assert "send_timeout" in status["send_breaker"]["reason"]
