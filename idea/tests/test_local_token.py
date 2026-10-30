# -*- coding: utf-8 -*-
"""803 本机令牌便利入口（/api/wb/local-token）：仅回环可读、可被环境变量关闭。

背景：803 默认口令已从 8888 改为随机 32 位（写 idea/config.json）。老书签/浏览器里
残留的 8888 会 401，用户不该去翻文件——浏览器本身就在回环上，所以提供与 802
`/api/token` 同口径的本机只读入口；非回环 peer 一律 403。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import idea_config  # noqa: E402
from app.main import _is_loopback_host, app  # noqa: E402


def _get(client_host: str, headers: dict | None = None):
    async def run():
        transport = httpx.ASGITransport(app=app, client=(client_host, 12345))
        async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:803") as client:
            return await client.get("/api/wb/local-token", headers=headers or {})

    return asyncio.run(run())


def test_is_loopback_host():
    assert _is_loopback_host("127.0.0.1:803")
    assert _is_loopback_host("localhost:803")
    assert _is_loopback_host("[::1]:803")
    assert not _is_loopback_host("192.168.1.5:803")
    assert not _is_loopback_host("")


def test_local_token_loopback_returns_current_token(monkeypatch):
    monkeypatch.delenv("IDEA_DISABLE_LOCAL_TOKEN", raising=False)
    resp = _get("127.0.0.1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["token"] == idea_config.admin_token()
    assert data["token"] and data["token"] not in ("8888", "123456", "admin")


def test_local_token_rejects_non_loopback_peer(monkeypatch):
    monkeypatch.delenv("IDEA_DISABLE_LOCAL_TOKEN", raising=False)
    resp = _get("10.0.0.5")
    assert resp.status_code == 403


def test_local_token_can_be_disabled(monkeypatch):
    monkeypatch.setenv("IDEA_DISABLE_LOCAL_TOKEN", "1")
    resp = _get("127.0.0.1")
    assert resp.status_code == 403
