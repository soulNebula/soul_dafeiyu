# -*- coding: utf-8 -*-
"""聊天图片本地视觉路由：模式选择 + caption 成功/失败。"""
from __future__ import annotations

import pytest

from agi.selflearn import vision_chat


class _Client:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.result


def test_mode_env_override(monkeypatch):
    monkeypatch.setenv("CHAT_VISION_MODE", "api")
    assert vision_chat.mode() == "api"
    monkeypatch.setenv("CHAT_VISION_MODE", "local_caption")
    assert vision_chat.mode() == "local_caption"
    monkeypatch.setenv("CHAT_VISION_MODE", "bogus")
    # 非法值回落到配置/默认，不抛错
    assert vision_chat.mode() in vision_chat.VALID_MODES


def test_caption_success_uses_router_client():
    client = _Client({"ok": True, "text": "一盘寿司和仙人掌", "usage": {}})
    cap, err = vision_chat.caption("data:image/png;base64,xx", client=client)
    assert cap == "一盘寿司和仙人掌" and err == ""
    messages, kwargs = client.calls[0]
    assert kwargs["max_tokens"] == 200
    content = messages[0]["content"]
    assert content[0]["type"] == "text" and content[1]["type"] == "image_url"


def test_caption_failure_returns_error():
    client = _Client({"ok": False, "text": "", "error": "local_down"})
    cap, err = vision_chat.caption("data:image/png;base64,xx", client=client)
    assert cap == "" and "local_down" in err


def test_caption_empty_image():
    cap, err = vision_chat.caption("")
    assert cap == "" and err == "empty_image"
