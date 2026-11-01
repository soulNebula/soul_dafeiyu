# -*- coding: utf-8 -*-
"""V1 PR2：bootstrap 环境检测 / bot 初始化 / 安装编排（不联网、不启服务）。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[2] / "scripts" / "bootstrap.py"
    spec = importlib.util.spec_from_file_location("bootstrap", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_ensure_bot_creates_safe_layout(tmp_path):
    boot = _load()
    bot = tmp_path / "bots" / "demo"
    result = boot.ensure_bot(bot)
    assert result["ok"] is True
    for sub in ("config", "db/selflearn/wechat_contacts", "models", "prompts",
                "media", "logs", "secrets"):
        assert (bot / sub).is_dir(), sub
    cfg = json.loads((bot / "config" / "wechat.json").read_text(encoding="utf-8"))
    assert cfg["enabled"] is False and cfg["dry_run"] is True
    assert cfg["send_mode"] == "off" and cfg["stop"] is True
    assert (bot / "bot.json").is_file()


def test_ensure_bot_is_idempotent_and_keeps_user_config(tmp_path):
    boot = _load()
    bot = tmp_path / "bots" / "demo"
    boot.ensure_bot(bot)
    cfg_path = bot / "config" / "wechat.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["allowed_contacts"] = ["文件传输助手"]
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    second = boot.ensure_bot(bot)
    assert second["created"] == []
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["allowed_contacts"] == ["文件传输助手"]


def test_build_report_has_expected_sections():
    boot = _load()
    report = boot.build_report()
    assert set(report) >= {"python", "disk", "wechat", "gpu", "main_venv",
                           "idea_venv", "port_803"}
    assert report["python"]["ok"] is True
    assert "free_gb" in report["disk"]


def test_python_version_gate():
    boot = _load()
    info = boot.python_info()
    assert info["ok"] is True and info["version"]
