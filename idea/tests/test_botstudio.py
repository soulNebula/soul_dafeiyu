# -*- coding: utf-8 -*-
"""803 Bot 管理：创建/登记/列出/移除登记（不删文件）。"""
from __future__ import annotations

from pathlib import Path


def test_create_list_and_remove(tmp_path, monkeypatch):
    from app import botstudio

    monkeypatch.setattr(botstudio, "REGISTRY", tmp_path / "bots.json")
    bot_dir = tmp_path / "mybot"
    result = botstudio.create_bot({
        "path": str(bot_dir), "name": "小鱼", "password": "secret123",
        "base_url": "https://api.example.com", "model": "any-model",
        "api_key": "sk-test-123456",
    })
    assert result["ok"] is True
    assert (bot_dir / "bot.json").is_file()
    assert (bot_dir / "secrets.json").is_file()

    listed = botstudio.list_bots()
    assert listed["ok"] is True and len(listed["items"]) == 1
    assert listed["items"][0]["valid"] is True

    inspected = botstudio.inspect_bot(str(bot_dir))
    assert inspected["ok"] is True
    assert "api_key" not in str(inspected["bot"]) or inspected["bot"]["providers"]

    removed = botstudio.unregister(str(bot_dir))
    assert removed["ok"] is True
    assert botstudio.list_bots()["items"] == []
    assert (bot_dir / "bot.json").is_file()  # 只移除登记，不删文件


def test_create_requires_path_and_name(tmp_path, monkeypatch):
    from app import botstudio

    monkeypatch.setattr(botstudio, "REGISTRY", tmp_path / "bots.json")
    assert botstudio.create_bot({"path": "", "name": "x"})["ok"] is False
    assert botstudio.create_bot({"path": str(tmp_path / "x"), "name": ""})["ok"] is False


def test_add_existing_validates(tmp_path, monkeypatch):
    from app import botstudio

    monkeypatch.setattr(botstudio, "REGISTRY", tmp_path / "bots.json")
    bot_dir = tmp_path / "b"
    botstudio.create_bot({"path": str(bot_dir), "name": "b", "password": "secret123",
                          "api_key": "sk-test-123456"})
    botstudio.unregister(str(bot_dir))
    added = botstudio.add_existing(str(bot_dir))
    assert added["ok"] is True
    assert len(botstudio.list_bots()["items"]) == 1
    bad = botstudio.add_existing(str(tmp_path / "nope"))
    assert bad["ok"] is False
