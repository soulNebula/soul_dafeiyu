# -*- coding: utf-8 -*-
"""Bot 配置包：创建/校验/密码/密钥隔离。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import bot_profile  # noqa: E402


def test_create_cloud_bot_and_validate(tmp_path):
    bot = bot_profile.create_bot(
        tmp_path / "mybot", "测试小鱼", password="secret123",
        chat={"base_url": "https://api.example.com", "model": "any-model",
              "api_key": "sk-test-123456"},
        soul={"base_url": "https://api.example.com", "model": "soul-model",
              "api_key": "sk-soul-123456"})
    assert bot["mode"] == "cloud" and bot["schema"] == 1
    result = bot_profile.validate_bot(tmp_path / "mybot")
    assert result["ok"] is True
    assert not result["errors"]
    assert bot_profile.verify_password("secret123", bot["auth"]["password_hash"])
    assert not bot_profile.verify_password("wrong", bot["auth"]["password_hash"])


def test_secrets_kept_out_of_bot_json(tmp_path):
    bot_profile.create_bot(tmp_path / "b", "x", password="secret123",
                           chat={"api_key": "sk-SECRET-999999"})
    bot_json = (tmp_path / "b" / "bot.json").read_text(encoding="utf-8")
    assert "sk-SECRET-999999" not in bot_json
    secrets = bot_profile.load_secrets(tmp_path / "b")
    assert secrets["providers"]["chat"]["api_key"] == "sk-SECRET-999999"


def test_validate_rejects_missing_and_bad_schema(tmp_path):
    assert bot_profile.validate_bot(tmp_path / "nope")["ok"] is False
    root = tmp_path / "bad"
    root.mkdir()
    (root / "bot.json").write_text(json.dumps({"schema": 99}), encoding="utf-8")
    result = bot_profile.validate_bot(root)
    assert result["ok"] is False
    assert any("schema" in item for item in result["errors"])


def test_validate_warns_missing_key(tmp_path):
    bot_profile.create_bot(tmp_path / "b", "x", password="secret123", chat={"api_key": ""})
    result = bot_profile.validate_bot(tmp_path / "b")
    assert result["ok"] is True
    assert any("api_key" in item for item in result["warnings"])


def test_password_min_length(tmp_path):
    with pytest.raises(ValueError):
        bot_profile.hash_password("123")
