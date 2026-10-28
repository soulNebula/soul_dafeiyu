# -*- coding: utf-8 -*-
"""M0 核心测试（精简版）：schema 校验、配置保存/备份/审计、端口探测。"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config_schema, config_store, services  # noqa: E402


# ---------------------------------------------------------------- schema
def test_schema_validates_types_and_ranges():
    item = config_schema.by_key("admin_token")
    assert config_schema.validate_value(item, "8888") is None
    assert "字符串" in config_schema.validate_value(item, 123)

    item = config_schema.by_key("port")
    assert config_schema.validate_value(item, 801) is None
    assert "不能大于" in config_schema.validate_value(item, 70000)
    assert "整数" in config_schema.validate_value(item, "801")

    item = config_schema.by_key("inner_mode")
    assert config_schema.validate_value(item, "off") is None
    assert "只能是" in config_schema.validate_value(item, "xxx")

    removed = config_schema.by_key("send_mode")
    assert removed is None  # 发送模式已随草稿/对话功能删除


def test_validate_all_reports_errors():
    r = config_schema.validate_all({"port": 70000, "inner_mode": "bad"})
    assert r["ok"] is False
    keys = {e["key"] for e in r["errors"]}
    assert keys == {"port", "inner_mode"}


def test_core_groups_only_model_train_base():
    assert config_schema.groups() == ["基础", "模型", "训练"]


# ---------------------------------------------------------------- config save
def isolated(tmp_path, monkeypatch):
    """把配置路径/备份目录/审计库指到 tmp，避免动真实项目。"""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"port": 801, "inner_mode": "off"}, ensure_ascii=False),
                   encoding="utf-8")
    monkeypatch.setattr(config_store.paths, "wechat_config_path", lambda: cfg)
    monkeypatch.setattr(config_store, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config_store.audit, "record", lambda *a, **k: None)
    return cfg


def test_config_save_writes_file_and_reports_restart(tmp_path, monkeypatch):
    cfg = isolated(tmp_path, monkeypatch)
    r = config_store.save({"inner_mode": "rule", "port": 802})
    assert r["ok"] is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["inner_mode"] == "rule" and data["port"] == 802
    assert "port" in r["restart_required"]


def test_config_save_rejects_invalid(tmp_path, monkeypatch):
    cfg = isolated(tmp_path, monkeypatch)
    r = config_store.save({"port": 70000})
    assert r["ok"] is False and r["error"] == "invalid"
    assert json.loads(cfg.read_text(encoding="utf-8"))["port"] == 801


def test_config_save_creates_backup(tmp_path, monkeypatch):
    isolated(tmp_path, monkeypatch)
    config_store.save({"inner_mode": "rule"})
    backups = config_store.backups()
    assert backups and backups[0].startswith("config.")


# ---------------------------------------------------------------- services
def test_port_state_detects_listener():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        running, _pid = services.port_state(port)
        assert running is True
    finally:
        srv.close()


def test_services_list_has_core_entries():
    data = services.list_services()
    names = {i["name"] for i in data["items"]}
    assert {"gateway", "console", "soul", "local3b"} <= names
