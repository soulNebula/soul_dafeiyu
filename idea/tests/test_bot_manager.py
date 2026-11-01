# -*- coding: utf-8 -*-
"""V1 PR3：803 Bot Manager（创建/列表/保存/校验/密钥/密码）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import bot_manager


@pytest.fixture()
def bots_root(tmp_path, monkeypatch):
    root = tmp_path / "bots"
    monkeypatch.setenv("SOULAGENT_BOTS_DIR", str(root))
    return root


def test_create_list_validate(bots_root):
    created = bot_manager.create_bot(
        "xiaoyu", name="小鱼", plan="cloud", soul_provider="cloud",
        llm_key="sk-test-123", soul_base="https://example.com/v1",
        soul_model="soul-x", soul_key="soul-key", password="pw123456")
    assert created["ok"] is True
    bot = bot_manager.get_bot("xiaoyu")
    assert bot["name"] == "小鱼" and bot["soul_provider"] == "cloud"
    assert bot["has_password"] is True
    assert bot_manager.list_bots()[0]["id"] == "xiaoyu"

    checks = bot_manager.validate_bot("xiaoyu")
    assert checks["ok"] is True
    assert all(item["ok"] for item in checks["checks"])

    keys = bot_manager.decode_secrets(
        json.loads((bots_root / "xiaoyu" / "secrets" / "keys.json").read_text("utf-8")))
    assert keys["llm_api_key"] == "sk-test-123"
    assert keys["soul_model"] == "soul-x"


def test_password_hash_roundtrip():
    record = bot_manager.hash_password("pw123456")
    assert bot_manager.verify_password("pw123456", record) is True
    assert bot_manager.verify_password("wrong", record) is False


def test_duplicate_and_bad_id(bots_root):
    bot_manager.create_bot("demo")
    with pytest.raises(bot_manager.BotError):
        bot_manager.create_bot("demo")
    with pytest.raises(bot_manager.BotError):
        bot_manager.create_bot("Bad_ID")


def test_save_updates_settings_and_keys(bots_root):
    bot_manager.create_bot("demo", llm_key="old")
    saved = bot_manager.save_bot("demo", {
        "soul_provider": "local", "soul_model": "soul-local",
        "llm_api_key": "new-key", "settings": {"reply_max_bubbles": 2},
        "password": "newpass",
    })
    assert saved["bot"]["soul_provider"] == "local"
    assert saved["bot"]["settings"]["reply_max_bubbles"] == 2
    keys = bot_manager.decode_secrets(
        json.loads((bots_root / "demo" / "secrets" / "keys.json").read_text("utf-8")))
    assert keys["llm_api_key"] == "new-key"
    auth = json.loads((bots_root / "demo" / "secrets" / "auth.json").read_text("utf-8"))
    assert bot_manager.verify_password("newpass", auth) is True


def test_api_roundtrip(bots_root, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("IDEA_TOKEN", "tok-test")
    client = TestClient(app)
    headers = {"X-Admin-Token": "tok-test"}
    res = client.post("/api/bots", headers=headers, json={
        "bot_id": "api-bot", "name": "API", "soul_provider": "cloud",
        "llm_api_key": "sk-api"})
    assert res.status_code == 200, res.text
    listed = client.get("/api/bots", headers=headers).json()
    assert any(item["id"] == "api-bot" for item in listed["items"])
    assert client.get("/api/bots/api-bot", headers=headers).json()["bot"]["name"] == "API"
    assert client.post("/api/bots/api-bot/validate", headers=headers).json()["ok"] is True


def test_load_bot_reports_stages(bots_root):
    bot_manager.create_bot("loader", llm_key="sk-x")
    result = bot_manager.load_bot("loader")
    assert result["bot_id"] == "loader"
    assert result["stages"] and result["progress"] >= 0
    names = [item["stage"] for item in result["stages"]]
    assert "目录" in names and "密钥文件" in names


def test_router_config_maps_soul_provider(bots_root):
    bot_manager.create_bot("r-local", soul_provider="local", soul_model="soul-x",
                           soul_base="http://127.0.0.1:9000/v1")
    cfg = bot_manager.router_config("r-local")
    assert cfg["purposes"]["soul"]["backend"] == "local"
    assert cfg["backends"]["local"]["base_url"].endswith(":9000/v1")
    bot_manager.create_bot("r-cloud", soul_provider="cloud")
    cfg2 = bot_manager.router_config("r-cloud")
    assert cfg2["purposes"]["soul"]["backend"] == "api"


def test_selfcheck_ports_and_bot(bots_root, monkeypatch):
    bot_manager.create_bot("self", llm_key="sk-x")
    monkeypatch.setattr(bot_manager, "_port_open", lambda port: port == 801)
    result = bot_manager.selfcheck("self")
    assert result["checks"][0]["name"] == "803"
    assert any(item["name"] == "port_801" and item["ok"] for item in result["checks"])
    assert any(item["name"] == "port_802" and not item["ok"] for item in result["checks"])
    assert any(item["name"] == "bot:self" and item["ok"] for item in result["checks"])


def test_api_selfcheck(bots_root, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    bot_manager.create_bot("sc", llm_key="sk-x")
    monkeypatch.setenv("IDEA_TOKEN", "tok-sc")
    client = TestClient(app)
    res = client.get("/api/selfcheck?bot_id=sc", headers={"X-Admin-Token": "tok-sc"})
    assert res.status_code == 200, res.text
    assert res.json()["checks"][0]["name"] == "803"
