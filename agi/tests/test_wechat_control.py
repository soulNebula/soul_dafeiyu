# -*- coding: utf-8 -*-
"""发送控制面测试：鉴权 fail-closed、Host 校验、按联系人策略。"""
from __future__ import annotations

from agi.wechat_gateway.control import (auth_ok, ensure_admin_token, host_ok,
                                        is_complaint, send_mode)


def test_control_helpers():
    assert is_complaint("我要投诉你") is True
    assert is_complaint("在吗") is False
    assert send_mode({"send_mode": "confirm"}) == "confirm"
    assert send_mode({"send_mode": "bad"}) == "confirm"  # 非法值 fail-safe 到 confirm
    assert send_mode({}) == "confirm"                     # 未配置默认 confirm
    assert send_mode({"send_mode": "off"}) == "off"


def test_contact_mode_overrides_global():
    cfg = {"send_mode": "confirm", "contact_modes": {"好友A": "auto", "好友B": "坏值"}}
    assert send_mode(cfg, "好友A") == "auto"
    assert send_mode(cfg, "好友B") == "confirm"
    assert send_mode(cfg, "没配的联系人") == "confirm"


def test_auth_is_fail_closed_without_token():
    # 空 token 不再等于"放行"（2026-10-24 M0 修复）
    assert auth_ok({}, {}) is False
    assert auth_ok({}, {"admin_token": ""}) is False
    assert auth_ok({"X-Admin-Token": "x"}, {"admin_token": "x"}) is True
    assert auth_ok({"X-Admin-Token": "y"}, {"admin_token": "x"}) is False
    assert auth_ok({}, {"admin_token": "x"}) is False
    assert auth_ok({}, {"admin_token": "x"}, "x") is True      # ?token=
    assert auth_ok({}, {"admin_token": "x"}, "y") is False


def test_ensure_admin_token_generates_once():
    cfg: dict = {}
    token, generated = ensure_admin_token(cfg)
    assert generated is True and len(token) == 32
    again, generated_again = ensure_admin_token(cfg)
    assert again == token and generated_again is False


def test_host_must_be_loopback_with_matching_port():
    assert host_ok("127.0.0.1:801", 801) is True
    assert host_ok("localhost:801", 801) is True
    assert host_ok("[::1]:801", 801) is True
    assert host_ok("127.0.0.1:802", 801) is False
    assert host_ok("evil.example:801", 801) is False
    assert host_ok("", 801) is False
    assert host_ok("127.0.0.1", 801) is False
