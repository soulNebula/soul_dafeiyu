# -*- coding: utf-8 -*-
"""Bot 激活（不重启服务）：数据根/环境变量/活跃指针。"""
from __future__ import annotations

from pathlib import Path

from agi import bot_runtime
from tools import bot_activate, bot_profile


def _prepare(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_runtime, "GLOBAL_DIR", tmp_path / "global")
    monkeypatch.setattr(bot_runtime, "ACTIVE_PATH", tmp_path / "global" / "active_bot.json")
    monkeypatch.setattr(bot_runtime, "SWITCH_STATUS_PATH", tmp_path / "global" / "switch.json")
    bot_path = tmp_path / "bot"
    bot_profile.create_bot(bot_path, "测试", password="secret123",
                           chat={"base_url": "https://api.example.com",
                                 "model": "m", "api_key": "sk-test-123456"})
    return bot_path


def test_activate_only_prepares_data_and_pointer(tmp_path, monkeypatch):
    bot_path = _prepare(tmp_path, monkeypatch)
    result = bot_activate.activate(str(bot_path), restart=False)
    assert result["ok"] is True
    runtime = result["runtime"]
    data = Path(runtime["data"])
    assert (data / "wechat" / "config.json").is_file()
    assert (data / "web_config.json").is_file()
    env = runtime["env"]
    assert env["SOULAGENT_DATA_DIR"] == str(data)
    assert env["SOULAGENT_SECRETS"].endswith("secrets.json")
    active = bot_runtime.read_active()
    assert active and active["path"] == str(bot_path.resolve())


def test_activate_rejects_invalid_bot(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    result = bot_activate.activate(str(tmp_path / "nope"), restart=False)
    assert result["ok"] is False
    assert result["error"] == "invalid_bot"
