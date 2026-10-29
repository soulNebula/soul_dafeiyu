# -*- coding: utf-8 -*-
"""云端灵魂路由：soul_provider_mode=cloud 时用 OpenAI 兼容 Client，不再打 8123。"""
from __future__ import annotations

from agi import secrets_store
from agi.inner import inject


def test_resolve_settings_cloud_provider(monkeypatch):
    monkeypatch.setattr(inject, "_read_gateway_config", lambda: {
        "inner_mode": "model",
        "soul_provider_mode": "cloud",
        "soul_provider_base_url": "https://api.example.com/v1",
        "soul_provider_model": "soul-model",
    })
    settings = inject.resolve_settings(production=True)
    assert settings["provider"] == "cloud"
    assert settings["provider_base_url"] == "https://api.example.com/v1"
    assert settings["provider_model"] == "soul-model"


def test_make_client_cloud_uses_llm_client(monkeypatch):
    monkeypatch.setattr(secrets_store, "provider_api_key", lambda name: "sk-soul-123456")
    monkeypatch.setattr(secrets_store, "llm_api_key", lambda: "sk-chat-123456")
    settings = {"provider": "cloud", "provider_base_url": "https://api.example.com/v1",
                "provider_model": "soul-model", "timeout_s": 12}
    client = inject.make_local_client(settings, force_new=True)
    assert client.base_url == "https://api.example.com/v1"
    assert client.model == "soul-model"
    assert client.api_key == "sk-soul-123456"
    assert getattr(client, "local_soul", False) is False
