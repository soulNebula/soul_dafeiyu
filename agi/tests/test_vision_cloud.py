# -*- coding: utf-8 -*-
"""云端识图 provider：配置 cloud 时绕过 router 直接调 Client。"""
from __future__ import annotations

from agi.llm import vision


class FakeClient:
    def __init__(self):
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {"ok": True, "text": "cloud caption", "model": "vision-cloud"}


def test_cloud_provider_routes(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(vision, "_cloud_provider", lambda: {
        "base_url": "https://api.example.com/v1", "model": "vision-model"})
    monkeypatch.setattr(vision, "_cloud_client", lambda p: fake)
    c = vision.RouterChatClient()
    result = c.chat([{"role": "user", "content": "hi"}])
    assert result["ok"] is True and result["text"] == "cloud caption"
    assert result["model"] == "vision-cloud"
    assert len(fake.calls) == 1
