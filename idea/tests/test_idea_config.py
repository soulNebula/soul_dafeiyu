# -*- coding: utf-8 -*-
"""803 自身配置（idea/config.json）测试：admin token 弱口令收口 + 路径设置优先级。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import idea_config  # noqa: E402


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """把配置路径指到 tmp，并清掉相关环境变量/缓存，避免动真实 idea/config.json。"""
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(idea_config, "CONFIG_PATH", cfg)
    monkeypatch.delenv("IDEA_TOKEN", raising=False)
    monkeypatch.delenv("IDEA_TRAIN_PYTHON", raising=False)
    idea_config.reset_cache()
    yield cfg
    idea_config.reset_cache()


def test_weak_default_is_replaced_by_random_token(isolated):
    isolated.write_text(json.dumps({"admin_token": "8888"}), encoding="utf-8")
    token = idea_config.admin_token()
    assert token != "8888"
    assert len(token) == 32, "应为 32 位十六进制随机 token"
    saved = json.loads(isolated.read_text(encoding="utf-8"))
    assert saved["admin_token"] == token, "新 token 必须写回 idea/config.json"


def test_missing_config_creates_file(isolated):
    assert not isolated.exists()
    token = idea_config.admin_token()
    assert isolated.is_file()
    assert json.loads(isolated.read_text(encoding="utf-8"))["admin_token"] == token


def test_existing_strong_token_is_reused(isolated):
    strong = "a" * 32
    isolated.write_text(json.dumps({"admin_token": strong}), encoding="utf-8")
    assert idea_config.admin_token() == strong


def test_env_token_wins_even_if_weak(isolated, monkeypatch):
    """显式设置 IDEA_TOKEN 视为用户确认（包括弱口令），不再强制轮换。"""
    monkeypatch.setenv("IDEA_TOKEN", "8888")
    idea_config.reset_cache()
    assert idea_config.admin_token() == "8888"


def test_token_is_cached(isolated):
    first = idea_config.admin_token()
    assert idea_config.admin_token() == first


def test_get_setting_priority(isolated, monkeypatch):
    assert idea_config.get_setting("train_python", "IDEA_TRAIN_PYTHON", "D") == "D"
    isolated.write_text(json.dumps({"train_python": "cfg"}), encoding="utf-8")
    assert idea_config.get_setting("train_python", "IDEA_TRAIN_PYTHON", "D") == "cfg"
    monkeypatch.setenv("IDEA_TRAIN_PYTHON", "env")
    assert idea_config.get_setting("train_python", "IDEA_TRAIN_PYTHON", "D") == "env"


def test_is_weak_token():
    assert idea_config.is_weak_token("") is True
    assert idea_config.is_weak_token("8888") is True
    assert idea_config.is_weak_token("Zx9" * 8) is False
