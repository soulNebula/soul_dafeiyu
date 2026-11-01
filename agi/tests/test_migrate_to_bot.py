# -*- coding: utf-8 -*-
"""V1 PR1：旧全局数据 → bots/<id>/ 迁移脚本（只复制、不删除）。"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "migrate_to_bot.py"
    spec = importlib.util.spec_from_file_location("migrate_to_bot", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _legacy(tmp_path: Path) -> Path:
    legacy = tmp_path / "legacy"
    (legacy / "wechat").mkdir(parents=True)
    (legacy / "wechat" / "config.json").write_text('{"enabled": false}', encoding="utf-8")
    (legacy / "selflearn" / "wechat_contacts").mkdir(parents=True)
    (legacy / "selflearn" / "selflearn.sqlite3").write_bytes(b"SQLite legacy")
    (legacy / "selflearn" / "wechat_contacts" / "a.memory.sqlite3").write_bytes(b"SQLite contact")
    return legacy


def test_build_plan_lists_existing_sources(tmp_path, monkeypatch):
    mod = _load_script()
    monkeypatch.setattr(mod, "_repo_web_config", lambda: None)
    legacy = _legacy(tmp_path)
    target = tmp_path / "bots" / "demo"
    plan = mod.build_plan(legacy, target)
    kinds = {item["kind"] for item in plan}
    assert {"wechat_config", "selflearn_db", "contact_db"} <= kinds
    assert all(Path(item["src"]).is_file() for item in plan)


def test_dry_run_does_not_write(tmp_path, monkeypatch):
    mod = _load_script()
    monkeypatch.setattr(mod, "_repo_web_config", lambda: None)
    legacy = _legacy(tmp_path)
    target = tmp_path / "bots" / "demo"
    plan = mod.build_plan(legacy, target)
    result = mod.execute(plan, apply=False)
    assert result["copied"] and not result["skipped"]
    assert not (target / "config" / "wechat.json").exists()


def test_apply_copies_and_is_idempotent(tmp_path, monkeypatch):
    mod = _load_script()
    monkeypatch.setattr(mod, "_repo_web_config", lambda: None)
    legacy = _legacy(tmp_path)
    target = tmp_path / "bots" / "demo"
    plan = mod.build_plan(legacy, target)
    first = mod.execute(plan, apply=True)
    assert first["copied"]
    assert (target / "config" / "wechat.json").read_text(encoding="utf-8") == '{"enabled": false}'
    assert (target / "db" / "selflearn" / "selflearn.sqlite3").is_file()
    assert (target / "db" / "selflearn" / "wechat_contacts" / "a.memory.sqlite3").is_file()
    meta = mod.write_bot_meta(target)
    assert meta.is_file() and meta.name == "bot.json"

    second = mod.execute(plan, apply=True)
    assert second["copied"] == [] and len(second["skipped"]) == len(plan)
