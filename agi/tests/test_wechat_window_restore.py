# -*- coding: utf-8 -*-
"""发送前窗口自检：微信最小化/收托盘时要能把主窗口恢复出来。"""
from __future__ import annotations

from types import SimpleNamespace

import wx_core.win as win


def test_ensure_wechat_window_visible_returns_existing_window(monkeypatch):
    fake = SimpleNamespace(hwnd=123, title="微信", width=1200, height=800)
    monkeypatch.setattr(win, "find_wechat_window", lambda: fake)
    monkeypatch.setattr(win.user32, "IsIconic", lambda hwnd: False)
    assert win.ensure_wechat_window_visible() is fake


def test_ensure_wechat_window_visible_restores_hidden_tray_window(monkeypatch):
    fake = SimpleNamespace(hwnd=456, title="微信", width=1200, height=800)
    calls: list[int] = []

    # 第一次找（可见窗口）→ None；恢复后第二次 → fake
    state = {"n": 0}

    def _find():
        state["n"] += 1
        return None if state["n"] == 1 else fake

    monkeypatch.setattr(win, "find_wechat_window", _find)
    monkeypatch.setattr(win, "_top_level_windows", lambda: [456])
    monkeypatch.setattr(win, "_exe_of", lambda pid: "weixin.exe")
    monkeypatch.setattr(win, "_window_title", lambda hwnd: "微信")
    monkeypatch.setattr(win, "_class_name", lambda hwnd: "Qt51514QWindowIcon")
    monkeypatch.setattr(win, "_restored_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(win.time, "sleep", lambda s: None)

    def _show(hwnd, cmd):
        calls.append(int(cmd))
        return True

    monkeypatch.setattr(win.user32, "ShowWindow", _show)

    assert win.ensure_wechat_window_visible() is fake
    assert 9 in calls  # SW_RESTORE
    assert 5 in calls  # SW_SHOW


def test_ensure_wechat_window_visible_returns_none_when_wechat_exited(monkeypatch):
    monkeypatch.setattr(win, "find_wechat_window", lambda: None)
    monkeypatch.setattr(win, "_top_level_windows", lambda: [])
    assert win.ensure_wechat_window_visible() is None
