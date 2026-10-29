# -*- coding: utf-8 -*-
"""把当前 802 配置导出成 Bot 包（不含明文 secrets）。"""
from __future__ import annotations

import json
from pathlib import Path

from agi import paths as agi_paths, secrets_store
from tools import bot_profile, export_bot


def test_export_current_config(tmp_path, monkeypatch):
    source = tmp_path / "data"
    (source / "wechat").mkdir(parents=True)
    (source / "selflearn" / "wechat_contacts").mkdir(parents=True)
    (source / "diary").mkdir()
    (source / "persona" / "xiaoyu").mkdir(parents=True)
    (source / "wechat" / "config.json").write_text(json.dumps({
        "enabled": True, "engine_enabled": True, "allowed_contacts": ["小鱼"],
        "monitored_contacts": ["小鱼"], "inner_mode": "model",
        "send_mode": "confirm", "llm_model": "deepseek-flash",
    }), encoding="utf-8")
    (source / "selflearn" / "wechat_contacts" / "a.memory.sqlite3").write_bytes(b"x")
    (source / "diary" / "d.txt").write_text("d", encoding="utf-8")
    (source / "persona" / "xiaoyu" / "p.txt").write_text("p", encoding="utf-8")
    (source / "prompts.json").write_text("{}", encoding="utf-8")
    web = tmp_path / "web_config.json"
    web.write_text(json.dumps({"llm": {"base_url": "https://api.example.com",
                                       "model": "m"}}), encoding="utf-8")
    monkeypatch.setattr(agi_paths, "data_dir", lambda: source)
    monkeypatch.setattr(agi_paths, "web_config_path", lambda: web)
    monkeypatch.setattr(secrets_store, "llm_api_key", lambda *a, **k: "sk-test-123456")

    result = export_bot.export(path=tmp_path / "bot", name="测试", password="secret123")
    assert result["ok"] is True
    bot = bot_profile.load_bot(tmp_path / "bot")
    assert bot["name"] == "测试"
    assert bot["settings"]["inner_mode"] == "model"
    assert (tmp_path / "bot" / "data" / "wechat" / "config.json").is_file()
    cfg = json.loads((tmp_path / "bot" / "data" / "wechat" / "config.json").read_text(encoding="utf-8"))
    assert cfg["enabled"] is False and cfg["allowed_contacts"] == ["小鱼"]
    assert (tmp_path / "bot" / "data" / "selflearn" / "wechat_contacts" / "a.memory.sqlite3").is_file()
    assert not (tmp_path / "bot" / "data" / "secrets.json").exists()
    secrets = bot_profile.load_secrets(tmp_path / "bot")
    assert secrets["providers"]["chat"]["api_key"] == "sk-test-123456"
