# -*- coding: utf-8 -*-
"""窗口级兜底发送（win_send）测试：默认不盲发、剪贴板协助、auto 需显式坐标。"""
from __future__ import annotations

from agi.wechat_gateway import win_send


class FakeClipboard:
    def __init__(self):
        self.texts: list[str] = []

    def set_text(self, value, retries=8):
        self.texts.append(value)
        return True


class FakeWin:
    def __init__(self, *, foreground=True, found=True):
        self.foreground = foreground
        self.found = found
        self.clicks: list[tuple[int, int]] = []
        self.keys: list[int] = []

    def find_wechat_window(self):
        return object() if self.found else None

    def activate(self, info):
        return True

    def is_foreground(self, info):
        return self.foreground

    def post_click(self, info, x, y, double=False):
        self.clicks.append((x, y))
        return True

    def press_key(self, vk):
        self.keys.append(vk)


def _patch(monkeypatch, *, win=None, clipboard=None):
    fake_clip = clipboard or FakeClipboard()
    fake_win = win or FakeWin()
    monkeypatch.setattr(win_send, "_load", lambda: ((fake_win, fake_clip), ""))
    monkeypatch.setattr(win_send, "_ctrl_v", lambda: None)
    return fake_win, fake_clip


def test_disabled_by_default(monkeypatch):
    _patch(monkeypatch)
    out = win_send.send_via_window("你好", {})
    assert out["ok"] is False and out["status"] == "disabled"


def test_manual_mode_only_copies_to_clipboard(monkeypatch):
    win, clip = _patch(monkeypatch)
    out = win_send.send_via_window("你好呀", {"fallback_enabled": True,
                                             "fallback_mode": "manual"})
    assert out["ok"] is True and out["status"] == "clipboard"
    assert out["verified"] is False
    assert clip.texts == ["你好呀"]
    assert win.clicks == [] and win.keys == []  # 没有模拟任何输入


def test_auto_requires_input_box(monkeypatch):
    _patch(monkeypatch)
    out = win_send.send_via_window("hi", {"fallback_enabled": True,
                                          "fallback_mode": "auto"})
    assert out["ok"] is False and "fallback_input_box" in out["error"]


def test_auto_clicks_pastes_and_enters(monkeypatch):
    win, clip = _patch(monkeypatch)
    out = win_send.send_via_window("hi", {"fallback_enabled": True,
                                          "fallback_mode": "auto",
                                          "fallback_input_box": [400, 600]})
    assert out["ok"] is True and out["status"] == "sent"
    assert out["verified"] is False
    assert win.clicks == [(400, 600)]
    assert clip.texts == ["hi"]
    assert win.keys == [win_send.VK_RETURN]


def test_auto_refuses_when_window_not_foreground(monkeypatch):
    _patch(monkeypatch, win=FakeWin(foreground=False))
    out = win_send.send_via_window("hi", {"fallback_enabled": True,
                                          "fallback_mode": "auto",
                                          "fallback_input_box": [1, 2]})
    assert out["ok"] is False and "前台" in out["error"]


def test_auto_refuses_when_window_missing(monkeypatch):
    _patch(monkeypatch, win=FakeWin(found=False))
    out = win_send.send_via_window("hi", {"fallback_enabled": True,
                                          "fallback_mode": "auto",
                                          "fallback_input_box": [1, 2]})
    assert out["ok"] is False and "微信窗口" in out["error"]


def test_empty_text_refused(monkeypatch):
    _patch(monkeypatch)
    out = win_send.send_via_window("  ", {"fallback_enabled": True})
    assert out["ok"] is False and "为空" in out["error"]
