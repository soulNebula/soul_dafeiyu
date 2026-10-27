# -*- coding: utf-8 -*-
"""M1-B：Memory Studio 后端（tmp SoulAgent 库，不联网）。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config_store, memstudio  # noqa: E402


def _soul_home(tmp_path):
    home = tmp_path / "SoulAgent"
    (home / "agi" / "data" / "selflearn" / "wechat_contacts").mkdir(parents=True)
    return home


def _seed(tmp_path):
    home = _soul_home(tmp_path)
    db, md = memstudio._soul()
    base = home / "agi" / "data" / "selflearn" / "wechat_contacts" / "aaa.sqlite3"
    con = db.connect(base)
    md.ensure_columns(con)
    now = time.time()
    rows = [
        ("M1", "plan", "准备考研", "full", 0.9, 0.9, "active",
         "准备考研的完整内容，包含具体安排", now - 10 * 86400),
        ("M2", "event", "周末搬家", "compressed", 0.35, 0.6, "active",
         "搬家", now - 3 * 86400),
        ("M3", "smalltalk", "今天有点累", "keyword", 0.1, 0.3, "active",
         "累", now - 2 * 86400),
        ("M4", "event", "旧事", "dormant", 0.01, 0.5, "dormant",
         "旧事", now - 300 * 86400),
    ]
    for ident, kind, content, stage, strength, importance, status, full, created in rows:
        con.execute(
            "INSERT INTO user_memories(id,kind,content,evidence,confidence,status,"
            "created_at,importance,last_strength_at,forget_stage,content_full,strength) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (ident, kind, content, "证据", 0.9, status, created, importance,
             created, stage, full, strength))
    con.commit(); con.close()
    return home, base


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home, base = _seed(tmp_path)
    monkeypatch.setattr(memstudio.paths, "soulagent_home", lambda: home)
    cfg = home / "agi" / "data" / "wechat" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({
        "memory_decay_enabled": True, "memory_purge_enabled": False,
        "memory_sleep_hour": 3, "trace_enabled": True, "trace_max_rows": 5000,
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config_store, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config_store.audit, "record", lambda *a, **k: None)
    return home, base, cfg


def test_overview_stats_and_recent(env):
    data = memstudio.overview()
    assert data["ok"] is True and data["total"] == 4
    assert data["by_kind"]["event"] == 2
    assert data["by_stage"]["full"] == 1
    assert data["by_stage"]["dormant"] == 1
    assert data["strength"]["0-0.2"] == 2  # 0.1 和 0.01
    assert len(data["recent"]) == 4
    assert data["recent"][0]["id"] in {"M2", "M3"}


def test_list_filter_and_detail(env):
    data = memstudio.list_memories(kind="event")
    assert data["total"] == 2 and {i["kind"] for i in data["items"]} == {"event"}
    data = memstudio.list_memories(stage="dormant")
    assert data["total"] == 1 and data["items"][0]["id"] == "M4"
    data = memstudio.list_memories(q="考研")
    assert data["total"] == 1
    detail = memstudio.get_memory("M1")
    assert detail["ok"] is True
    assert "完整内容" in detail["memory"]["content_full"]


def test_rules_get_save_and_permissions(env):
    data = memstudio.rules()
    assert data["ok"] is True and "memory_decay_enabled" in data["keys"]
    result = memstudio.save_rules({"memory_sleep_hour": 4})
    assert result["ok"] is True
    assert json.loads(env[2].read_text(encoding="utf-8"))["memory_sleep_hour"] == 4
    denied = memstudio.save_rules({"port": 9000})
    assert denied["ok"] is False and denied["error"] == "not_allowed"
    invalid = memstudio.save_rules({"memory_sleep_hour": 99})
    assert invalid["ok"] is False and invalid["error"] == "invalid"


def test_decay_params(env):
    data = memstudio.decay_params()
    assert data["ok"] is True
    assert data["half_life_days"]["event"] == 7.0
    assert data["stage_thresholds"]["full"] == 0.5
    assert "memory_decay_enabled" in data["writable"]
    assert "half_life_days" in data["readonly"]


def test_revive_and_purge_dry_run(env):
    revived = memstudio.revive_memory("M4")
    assert revived["ok"] is True
    detail = memstudio.get_memory("M4")["memory"]
    assert detail["status"] == "active" and detail["strength"] >= 0.6
    dry = memstudio.purge_dry_run()
    assert dry["ok"] is True and dry["dry_run"] is True
    assert dry["count"] == 0  # 刚 revive 的 M4 已不再是 dormant
    denied = memstudio.purge(confirm="随便")
    assert denied["ok"] is False and denied["error"] == "confirm_required"


def test_timeline(env):
    data = memstudio.timeline(days=30)
    assert data["ok"] is True
    assert data["items"]
    assert all("count" in item and "avg_strength" in item for item in data["items"])
