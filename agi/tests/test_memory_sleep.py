# -*- coding: utf-8 -*-
"""P38 接线测试：gateway 03:00 睡眠整理、config 开关、console strength 路由。"""
from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path

import pytest

from agi.selflearn import db, memory_decay
from agi.wechat_gateway import config as gw_cfg
from agi.wechat_gateway.server import Gateway
from tools.console import server as console


def _gw(tmp_path, **overrides):
    path = tmp_path / "config.json"
    cfg = {
        "enabled": True, "port": 801, "dry_run": True,
        "allowed_contacts": [], "monitored_contacts": [],
        "log_path": str(tmp_path / "gateway.log"),
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "memory_decay_paths": [str(tmp_path / "dummy.memory.sqlite3")],
    }
    cfg.update(overrides)
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return Gateway(cfg, config_path=path)


def test_config_defaults():
    assert gw_cfg.DEFAULTS["memory_decay_enabled"] is True
    assert gw_cfg.DEFAULTS["memory_purge_enabled"] is False


def test_seconds_until_memory_sleep(tmp_path):
    gw = _gw(tmp_path)
    at_2 = datetime(2026, 10, 27, 2, 0).timestamp()
    at_4 = datetime(2026, 10, 27, 4, 0).timestamp()
    assert abs(gw._seconds_until_memory_sleep(at_2) - 3600.0) < 2
    assert abs(gw._seconds_until_memory_sleep(at_4) - 23 * 3600.0) < 2
    gw.cfg["memory_sleep_hour"] = 4
    assert abs(gw._seconds_until_memory_sleep(at_2) - 7200.0) < 2


def test_memory_sleep_runs_even_engine_stopped(tmp_path, monkeypatch):
    gw = _gw(tmp_path, memory_purge_enabled=False)
    gw.engine_enabled = False
    calls = []

    def fake_decay(path, now=None, dry_run=False):
        calls.append(("decay", str(path), dry_run))
        return {"ok": True, "checked": 10, "changed": 4,
                "stages": {"full": 6, "compressed": 2, "keyword": 1, "dormant": 1}}

    def fake_purge(path, days=200, now=None, dry_run=True):
        calls.append(("purge", str(path), dry_run))
        return {"ok": True, "count": 2, "dry_run": dry_run, "ids": []}

    monkeypatch.setattr(memory_decay, "decay_all", fake_decay)
    monkeypatch.setattr(memory_decay, "purge_dormant", fake_purge)
    result = gw._run_memory_sleep(now=datetime(2026, 10, 27, 3, 0).timestamp())
    assert result["ok"] is True
    assert result["checked"] == 10 and result["compressed"] == 2
    assert result["keyword"] == 1 and result["dormant"] == 1
    assert result["purged"] == 2 and result["purge_dry_run"] is True
    assert ("decay", str(tmp_path / "dummy.memory.sqlite3"), False) in calls
    assert ("purge", str(tmp_path / "dummy.memory.sqlite3"), True) in calls
    events = [e for e in gw.recent if e.get("kind") == "memory_sleep"]
    assert events and events[0]["checked"] == 10
    assert events[0]["engine_enabled"] is False


def test_memory_sleep_purge_real_when_enabled(tmp_path, monkeypatch):
    gw = _gw(tmp_path, memory_purge_enabled=True)
    seen = []

    def fake_decay(path, now=None, dry_run=False):
        return {"ok": True, "checked": 1, "changed": 0, "stages": {}}

    def fake_purge(path, days=200, now=None, dry_run=True):
        seen.append(dry_run)
        return {"ok": True, "count": 0, "dry_run": dry_run}

    monkeypatch.setattr(memory_decay, "decay_all", fake_decay)
    monkeypatch.setattr(memory_decay, "purge_dormant", fake_purge)
    result = gw._run_memory_sleep(now=datetime(2026, 10, 27, 3, 0).timestamp())
    assert result["purge_dry_run"] is False
    assert seen == [False]


def test_memory_sleep_disabled_skips(tmp_path, monkeypatch):
    gw = _gw(tmp_path, memory_decay_enabled=False)
    monkeypatch.setattr(memory_decay, "decay_all",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))
    assert gw._run_memory_sleep() == {"ok": True, "skipped": "disabled"}


def test_stage_text_and_console_strength(tmp_path, monkeypatch):
    path = tmp_path / "selflearn.sqlite3"
    con = db.connect(path)
    con.execute("insert into user_memories(id,kind,content,evidence,confidence,status,created_at,importance) "
                "values('UM-1','preference','喜欢咖啡','证据',0.8,'active',?,0.5)",
                (datetime(2026, 10, 27).timestamp(),))
    con.commit()
    con.close()
    memory_decay.decay_all(path, now=datetime(2026, 10, 27, 12).timestamp(), dry_run=False)
    assert memory_decay.stage_text({"forget_stage": "dormant", "content": "x"}) == ""
    assert memory_decay.stage_text({"forget_stage": "keyword", "content": "咖啡、周末"}) == "咖啡、周末"
    # console helper（不走 HTTP server，只验数据）
    monkeypatch.setattr(console, "contact_db_path", lambda: db.memory_path(path))
    data = console.memory_strength()
    assert data["ok"] is True and data["count"] == 1
    assert data["stages"].get("compressed", 0) + data["stages"].get("full", 0) == 1


def test_console_memory_strength_route(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "memory_strength",
                        lambda contact="": {"ok": True, "count": 2, "stages": {"full": 2}})
    handler = console.Handler.__new__(console.Handler)
    handler.path = "/api/memory/strength"
    handler.command = "GET"
    handler.requestline = "GET /api/memory/strength HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.close_connection = True
    handler.wfile = io.BytesIO()
    handler._headers_buffer = []
    handler.rfile = io.BytesIO(b"")
    handler.headers = {"Host": "127.0.0.1:802"}
    handler.do_GET()
    raw = handler.wfile.getvalue().decode("utf-8")
    head, _, payload = raw.partition("\r\n\r\n")
    status = int(head.split(" ")[1])
    data = json.loads(payload)
    assert status == 200 and data["count"] == 2
