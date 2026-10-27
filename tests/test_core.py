# -*- coding: utf-8 -*-
"""M0 核心测试：schema 校验、配置保存/备份/审计、git 提交、端口探测。"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config_schema, config_store, gitstudio, services  # noqa: E402


# ---------------------------------------------------------------- schema
def test_schema_validates_types_and_ranges():
    item = config_schema.by_key("admin_token")
    assert config_schema.validate_value(item, "8888") is None
    assert "字符串" in config_schema.validate_value(item, 123)

    item = config_schema.by_key("port")
    assert config_schema.validate_value(item, 801) is None
    assert "不能大于" in config_schema.validate_value(item, 70000)
    assert "整数" in config_schema.validate_value(item, "801")

    item = config_schema.by_key("quiet_hours")
    assert config_schema.validate_value(item, [22, 9]) is None
    assert "起始小时" in config_schema.validate_value(item, [25, 9])
    assert "起始小时" in config_schema.validate_value(item, [22])

    item = config_schema.by_key("send_mode")
    assert config_schema.validate_value(item, "auto") is None
    assert "只能是" in config_schema.validate_value(item, "xxx")

    item = config_schema.by_key("allowed_contacts")
    assert config_schema.validate_value(item, ["a", "b"]) is None
    assert "字符串列表" in config_schema.validate_value(item, "a")


def test_validate_all_reports_errors():
    r = config_schema.validate_all({"port": 70000, "send_mode": "bad"})
    assert r["ok"] is False
    keys = {e["key"] for e in r["errors"]}
    assert keys == {"port", "send_mode"}


# ---------------------------------------------------------------- config save
@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """把配置路径/备份目录/审计库指到 tmp，避免动真实项目。"""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"port": 801, "send_mode": "auto"}, ensure_ascii=False),
                   encoding="utf-8")
    monkeypatch.setattr(config_store.paths, "wechat_config_path", lambda: cfg)
    monkeypatch.setattr(config_store, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config_store.audit, "record",
                        lambda *a, **k: None)
    monkeypatch.setattr(config_store.gitstudio, "auto_commit",
                        lambda *a, **k: {"ok": True, "sha": "abc123"})
    monkeypatch.setattr(config_store.gitstudio, "push_default", lambda: {"ok": True})
    return cfg


def test_config_save_writes_file_and_reports_restart(isolated):
    r = config_store.save({"send_mode": "confirm", "port": 802})
    assert r["ok"] is True
    data = json.loads(isolated.read_text(encoding="utf-8"))
    assert data["send_mode"] == "confirm" and data["port"] == 802
    assert "port" in r["restart_required"]          # 端口需重启
    assert r["git"]["sha"] == "abc123"


def test_config_save_rejects_invalid(isolated):
    r = config_store.save({"port": 70000})
    assert r["ok"] is False and r["error"] == "invalid"
    # 文件没被改
    assert json.loads(isolated.read_text(encoding="utf-8"))["port"] == 801


def test_config_save_creates_backup(isolated):
    config_store.save({"send_mode": "confirm"})
    backups = config_store.backups()
    assert backups and backups[0].startswith("config.")


# ---------------------------------------------------------------- git
def test_git_commit_in_temp_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.txt").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(gitstudio, "repo_path", lambda: repo)
    st = gitstudio.status()
    assert st["ok"] and st["dirty"]
    r = gitstudio.commit("test commit")
    assert r["ok"] and r["sha"]
    st2 = gitstudio.status()
    assert not st2["dirty"]
    log = gitstudio.log(5)
    assert log["items"][0]["subject"] == "test commit"


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


# ---------------------------------------------------------------- 编码回退
def test_read_write_gbk_file(tmp_path, monkeypatch):
    from app import files_api
    (tmp_path / "run.bat").write_bytes("echo 你好\r\n".encode("gbk"))
    monkeypatch.setattr(files_api, "roots", lambda: {"tmp": str(tmp_path)})
    r = files_api.read_file("tmp", "run.bat")
    assert r["ok"] and r["encoding"] in ("gb18030", "cp936")
    assert "你好" in r["text"]
    # 按原编码写回，不破坏 bat 的 GBK
    w = files_api.write_file("tmp", "run.bat", r["text"], encoding=r["encoding"])
    assert w["ok"]
    raw = (tmp_path / "run.bat").read_bytes()
    assert "你好" in raw.decode("gbk")


def test_read_utf8_file(tmp_path, monkeypatch):
    from app import files_api
    (tmp_path / "a.md").write_text("# 中文\n", encoding="utf-8")
    monkeypatch.setattr(files_api, "roots", lambda: {"tmp": str(tmp_path)})
    r = files_api.read_file("tmp", "a.md")
    assert r["ok"] and r["encoding"] == "utf-8" and "中文" in r["text"]


# ---------------------------------------------------------------- 微信草稿代理
def test_wechat_proxy_maps_drafts_and_actions(monkeypatch):
    from app import wechat_proxy
    calls = []

    def fake_call(method, path, payload=None):
        calls.append((method, path, payload))
        if path == "/api/drafts":
            return {"ok": True, "drafts": [{"id": "D1", "text": "hi"}]}
        return {"ok": True, "status": "planned"}

    monkeypatch.setattr(wechat_proxy, "_call", fake_call)
    d = wechat_proxy.drafts()
    assert d["ok"] and d["count"] == 1 and d["items"][0]["id"] == "D1"
    assert wechat_proxy.approve("D1", force_quiet=True)["status"] == "planned"
    assert calls[-1][2] == {"id": "D1", "confirm": True, "force_quiet": True}
    wechat_proxy.reject("D1")
    assert calls[-1] == ("POST", "/api/draft/reject", {"id": "D1"})


# ---------------------------------------------------------------- 发送模式开关
def test_send_mode_switch_writes_all_contacts(tmp_path, monkeypatch):
    import json
    from app import wechat_proxy
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"send_mode": "confirm",
                               "allowed_contacts": ["A", "B"],
                               "contact_modes": {"A": "confirm", "B": "confirm"}},
                              ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(wechat_proxy.paths, "wechat_config_path", lambda: cfg)
    monkeypatch.setattr("app.config_store.paths.wechat_config_path", lambda: cfg)
    monkeypatch.setattr("app.config_store.BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("app.config_store.audit.record", lambda *a, **k: None)
    monkeypatch.setattr("app.config_store.gitstudio.auto_commit",
                        lambda *a, **k: {"ok": True})
    reloads = []
    monkeypatch.setattr(wechat_proxy, "reload_config",
                        lambda: reloads.append(1) or {"ok": True, "reloaded": True})

    out = wechat_proxy.set_send_mode("auto")
    assert out["ok"] is True and out["mode"] == "auto"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["send_mode"] == "auto"
    assert data["contact_modes"] == {"A": "auto", "B": "auto"}
    assert reloads, "切换后应触发网关 reload"

    assert wechat_proxy.set_send_mode("nonsense")["ok"] is False
    assert wechat_proxy.get_send_mode()["mode"] == "auto"
