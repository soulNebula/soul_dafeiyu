# -*- coding: utf-8 -*-
"""发送控制面测试。"""
from __future__ import annotations

from agi.wechat_gateway.control import auth_ok, is_complaint, send_mode


def test_control_helpers():
    assert is_complaint("我要投诉你") is True
    assert is_complaint("在吗") is False
    assert send_mode({"send_mode": "confirm"}) == "confirm"
    assert send_mode({"send_mode": "bad"}) == "auto"
    assert auth_ok({}, {}) is True
    assert auth_ok({"X-Admin-Token": "x"}, {"admin_token": "x"}) is True
    assert auth_ok({}, {"admin_token": "x"}) is False
