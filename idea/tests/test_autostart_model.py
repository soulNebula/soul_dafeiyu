# -*- coding: utf-8 -*-
"""导出后自动启动 + 切换灵魂服务。"""
from __future__ import annotations

import json
from pathlib import Path


def test_auto_start_switches_soul_config(tmp_path, monkeypatch):
    from app import trainstudio
    bot = tmp_path / "bot"
    (bot / "data" / "wechat").mkdir(parents=True)
    (bot / "data" / "wechat" / "config.json").write_text(json.dumps({
        "admin_token": "tok", "inner_mode": "off"}), encoding="utf-8")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(trainstudio, "_post_console", lambda *a, **k: {"ok": True})
    result = trainstudio.auto_start_and_switch(
        {"name": "soul_v2", "port": 8126}, bot_root=bot)
    assert result["ok"] is True and result["switched"] is True
    cfg = json.loads((bot / "data" / "wechat" / "config.json").read_text(encoding="utf-8"))
    assert cfg["soul_provider_mode"] == "local"
    assert cfg["soul_server_url"] == "http://127.0.0.1:8126"
    assert cfg["inner_mode"] == "model"
