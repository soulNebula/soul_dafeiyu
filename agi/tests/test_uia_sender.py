# -*- coding: utf-8 -*-
"""UIA 发送后端测试（不碰真实微信/窗口）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agi.wechat_gateway import uia_sender  # noqa: E402


def test_available_reflects_script(monkeypatch, tmp_path):
    monkeypatch.setattr(uia_sender, "SCRIPT", tmp_path / "nope.ps1")
    assert uia_sender.available() is False
    f = tmp_path / "q.ps1"
    f.write_text("", encoding="utf-8")
    monkeypatch.setattr(uia_sender, "SCRIPT", f)
    assert uia_sender.available() is True


def test_find_session_parses_json(monkeypatch):
    monkeypatch.setattr(uia_sender, "_query", lambda *a, **k: {
        "ok": True, "contact": {"found": True, "rect": {"x": 10, "y": 20, "w": 100, "h": 40}}})
    r = uia_sender.find_session("文件传输助手")
    assert r["contact"]["found"] is True
    assert uia_sender._rect_center(r["contact"]["rect"]) == (60, 40)


def test_open_contact_blocks_on_title_mismatch(monkeypatch):
    monkeypatch.setattr(uia_sender, "_activate_window", lambda: True)
    monkeypatch.setattr(uia_sender, "find_session", lambda c: {
        "ok": True, "contact": {"found": True, "rect": {"x": 0, "y": 0, "w": 10, "h": 10}}})
    monkeypatch.setattr(uia_sender, "_click_screen", lambda x, y: True)
    monkeypatch.setattr(uia_sender, "chat_title", lambda: "别人")
    monkeypatch.setattr(uia_sender.time, "sleep", lambda *a, **k: None)
    ok, why = uia_sender.open_contact("目标")
    assert ok is False
    assert "title_mismatch" in why


def test_open_contact_ok_when_title_matches(monkeypatch):
    monkeypatch.setattr(uia_sender, "_activate_window", lambda: True)
    monkeypatch.setattr(uia_sender, "find_session", lambda c: {
        "ok": True, "contact": {"found": True, "rect": {"x": 0, "y": 0, "w": 10, "h": 10}}})
    monkeypatch.setattr(uia_sender, "_click_screen", lambda x, y: True)
    monkeypatch.setattr(uia_sender, "chat_title", lambda: "目标")
    monkeypatch.setattr(uia_sender.time, "sleep", lambda *a, **k: None)
    assert uia_sender.open_contact("目标") == (True, "")


def test_send_to_contact_aborts_when_open_fails(monkeypatch):
    monkeypatch.setattr(uia_sender, "open_contact", lambda c, retries=2: (False, "title_mismatch"))
    called = []
    monkeypatch.setattr(uia_sender, "_paste_and_enter", lambda t: called.append(t) or True)
    out = uia_sender.send_to_contact("目标", "hi")
    assert out["ok"] is False and out["status"] == "failed"
    assert called == []  # 没打开正确会话，绝不粘贴


def test_send_to_contact_sends_when_verified(monkeypatch):
    monkeypatch.setattr(uia_sender, "open_contact", lambda c, retries=2: (True, ""))
    monkeypatch.setattr(uia_sender, "find_input", lambda: {"found": True, "rect": {"x": 0, "y": 0, "w": 10, "h": 10}})
    monkeypatch.setattr(uia_sender, "_click_screen", lambda x, y: True)
    monkeypatch.setattr(uia_sender, "_paste_and_enter", lambda t: True)
    monkeypatch.setattr(uia_sender.time, "sleep", lambda *a, **k: None)
    out = uia_sender.send_to_contact("目标", "hi")
    assert out["ok"] is True and out["method"] == "uia"
