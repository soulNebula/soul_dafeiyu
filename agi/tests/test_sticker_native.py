# -*- coding: utf-8 -*-
"""sticker_native 单测：默认安全 unsupported、开关、fake backend、收藏贴纸空。"""
from __future__ import annotations

import pytest

from agi.wechat_gateway import sticker_native


class FakeBackend:
    def __init__(self, *, available=True, current="文件传输助手", emojis=("微笑",),
                 list_items=None):
        self._available = available
        self.current = current
        self.emojis = set(emojis)
        self.list_items = list_items or []
        self.calls = []

    def available(self):
        return self._available

    def current_chat(self):
        self.calls.append(("current_chat",))
        return self.current

    def open_emoji_panel(self):
        self.calls.append(("open_emoji_panel",))

    def click_system_emoji(self, name):
        self.calls.append(("click_system_emoji", name))
        if name not in self.emojis:
            raise sticker_native.EmojiNotFound(name)

    def send(self):
        self.calls.append(("send",))

    def close(self):
        self.calls.append(("close",))

    def list_collect_stickers(self):
        return list(self.list_items)


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(sticker_native, "STICKER_NATIVE_ENABLED", False)
    monkeypatch.delenv(sticker_native.ENV_FLAG, raising=False)
    sticker_native.set_backend(None)
    yield
    sticker_native.set_backend(None)


def test_default_is_safe_unsupported():
    assert sticker_native.enabled() is False
    assert sticker_native.available() is False
    result = sticker_native.send_native("文件传输助手", "微笑")
    assert result["ok"] is False
    assert result["status"] == "unsupported"
    assert result["error"] == "sticker_native_disabled"
    assert result["fallback"] == "image"
    assert sticker_native.list_collect_stickers() == []


def test_enabled_but_no_wxauto4_is_unsupported(monkeypatch):
    monkeypatch.setenv(sticker_native.ENV_FLAG, "1")
    assert sticker_native.enabled() is True
    assert sticker_native.available() is False  # venv 里没有 wxauto4/backend
    result = sticker_native.send_native("文件传输助手", "微笑")
    assert result["ok"] is False
    assert result["status"] == "unsupported"
    assert result["error"] == "backend_unavailable"


def test_fake_backend_sends_system_emoji(monkeypatch):
    monkeypatch.setattr(sticker_native, "STICKER_NATIVE_ENABLED", True)
    backend = FakeBackend()
    assert sticker_native.available(backend=backend) is True
    result = sticker_native.send_native("文件传输助手", "微笑", backend=backend)
    assert result["ok"] is True
    assert result["status"] == "sent"
    assert result["name"] == "微笑"
    assert result["kind"] == "system_emoji"
    assert ("click_system_emoji", "微笑") in backend.calls
    assert ("send",) in backend.calls
    assert ("close",) in backend.calls


def test_contact_mismatch_refused_without_click(monkeypatch):
    monkeypatch.setattr(sticker_native, "STICKER_NATIVE_ENABLED", True)
    backend = FakeBackend(current="另一个好友")
    result = sticker_native.send_native("文件传输助手", "微笑", backend=backend)
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"] == "contact_mismatch"
    assert result["fallback"] == "image"
    assert all(call[0] != "open_emoji_panel" for call in backend.calls)


def test_unknown_emoji_fails_and_closes(monkeypatch):
    monkeypatch.setattr(sticker_native, "STICKER_NATIVE_ENABLED", True)
    backend = FakeBackend(emojis=("微笑",))
    result = sticker_native.send_native("文件传输助手", "不存在的表情", backend=backend)
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"] == "emoji_not_found"
    assert result["fallback"] == "image"
    assert ("close",) in backend.calls


def test_unavailable_backend(monkeypatch):
    monkeypatch.setattr(sticker_native, "STICKER_NATIVE_ENABLED", True)
    backend = FakeBackend(available=False)
    assert sticker_native.available(backend=backend) is False
    result = sticker_native.send_native("文件传输助手", "微笑", backend=backend)
    assert result["ok"] is False
    assert result["status"] == "unsupported"


def test_list_collect_stickers_returns_empty_by_default(monkeypatch):
    monkeypatch.setattr(sticker_native, "STICKER_NATIVE_ENABLED", True)
    backend = FakeBackend(list_items=[])
    assert sticker_native.list_collect_stickers(backend=backend) == []


def test_env_flag_enables():
    assert sticker_native.enabled() is False
    import os
    os.environ[sticker_native.ENV_FLAG] = "1"
    try:
        assert sticker_native.enabled() is True
    finally:
        os.environ.pop(sticker_native.ENV_FLAG, None)
