# -*- coding: utf-8 -*-
"""M2-A ChatStudio 后端测试：提示词/人格备份审计、开关白名单、护栏只读、trace、dry 试跑。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app import chatstudio


@pytest.fixture
def env(tmp_path, monkeypatch):
    soul = tmp_path / "SoulAgent"
    data = soul / "agi" / "data"
    persona = soul / "agi" / "persona" / "xiaoyu"
    data.mkdir(parents=True)
    persona.mkdir(parents=True)
    (data / "prompts.json").write_text(json.dumps(
        {"version": 1, "prompts": {"chat.system": "旧提示词"}}), encoding="utf-8")
    (data / "wechat").mkdir()
    (data / "wechat" / "config.json").write_text(json.dumps(
        {"inject_time_context": True, "unknown_key": 1}), encoding="utf-8")
    (persona / "values.md").write_text("旧价值观", encoding="utf-8")
    (persona / "profile.json").write_text('{"name": "小鱼"}', encoding="utf-8")
    idea_data = tmp_path / "idea" / ".data"
    idea_data.mkdir(parents=True)
    monkeypatch.setattr(chatstudio.paths, "soulagent_home", lambda: soul)
    monkeypatch.setattr(chatstudio.paths, "DATA_DIR", idea_data)
    audits = []
    monkeypatch.setattr(chatstudio.audit, "record",
                        lambda action, target, **kw: audits.append(
                            {"action": action, "target": target, **kw}))
    return {"soul": soul, "data": data, "persona": persona,
            "idea_data": idea_data, "audits": audits}


def test_prompts_view_and_update_backup_audit(env):
    view = chatstudio.prompts_view()
    assert view["ok"] is True
    assert view["overrides"]["chat.system"] == "旧提示词"
    result = chatstudio.update_prompts({"chat.system": "新提示词"})
    assert result["ok"] is True
    assert Path(result["backup"]).is_file()
    assert json.loads((env["data"] / "prompts.json").read_text(encoding="utf-8"))["prompts"]["chat.system"] == "新提示词"
    assert any(item["action"] == "chat.prompts_update" for item in env["audits"])
    assert chatstudio.update_prompts({"not.a.key": "x"})["error"].startswith("unknown_prompt_key")
    assert chatstudio.update_prompt("chat.system", "单键版本")["ok"] is True


def test_persona_update_json_validation_and_backup(env):
    result = chatstudio.update_persona("xiaoyu", "values.md", "新价值观")
    assert result["ok"] is True
    assert Path(result["backup"]).is_file()
    assert (env["persona"] / "values.md").read_text(encoding="utf-8") == "新价值观"
    bad = chatstudio.update_persona("xiaoyu", "profile.json", "{broken")
    assert bad["ok"] is False and bad["error"].startswith("invalid_json")
    assert chatstudio.update_persona("xiaoyu", "other.txt", "x")["error"] == "persona_file_not_allowed"
    assert any(item["action"] == "chat.persona_update" for item in env["audits"])


def test_switches_whitelist_and_backup(env):
    view = chatstudio.switches_view()
    assert view["ok"] is True and view["items"]["inject_time_context"]["value"] is True
    edited = chatstudio.update_switches({"trace_enabled": False, "proactive_style": "simple"})
    assert edited["ok"] is True
    saved = json.loads((env["data"] / "wechat" / "config.json").read_text(encoding="utf-8"))
    assert saved["trace_enabled"] is False and saved["proactive_style"] == "simple"
    assert saved["unknown_key"] == 1  # 不认识的旧键原样保留
    assert Path(edited["backup"]).is_file()
    assert chatstudio.update_switches({"engine_enabled": False})["error"].startswith("switch_not_allowed")
    assert chatstudio.update_switches({"trace_enabled": "no"})["error"].startswith("switch_not_bool")
    pipeline = chatstudio.pipeline_view()
    assert pipeline["ok"] is True
    assert [stage["key"] for stage in pipeline["stages"]] == [
        "time", "context", "memory", "mood", "inner", "guard",
        "media", "media_choice", "voice_polish", "trace"]


def test_guard_rules_readonly(env, monkeypatch):
    monkeypatch.setattr(chatstudio, "_soul_module", lambda name: (_ for _ in ()).throw(ImportError("no soul")))
    rules = chatstudio.guard_rules()
    assert rules["readonly"] is True
    assert "error" in rules
    citation = chatstudio.citation_rules()
    assert citation["readonly"] is True


def test_trace_list_and_detail_fallback(env, monkeypatch):
    db = env["idea_data"] / "trial.memory.sqlite3"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE turn_traces (id TEXT PRIMARY KEY, ts REAL, mode TEXT, contact TEXT, "
        "user_text TEXT, final_reply TEXT, latency_ms REAL, skipped INTEGER, "
        "skip_reason TEXT, crisis INTEGER, created_at REAL, system_prompt TEXT, "
        "prompt_user TEXT, memories_json TEXT, inner_json TEXT, mood_json TEXT, "
        "generation_json TEXT, polish_json TEXT, guard_json TEXT, proactive_json TEXT)")
    con.execute("INSERT INTO turn_traces(id,ts,mode,contact,user_text,final_reply,latency_ms,"
                "skipped,skip_reason,crisis,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("t1", 1.0, "chat", "c", "你好", "嗨", 12.5, 0, "", 0, 1.0))
    con.commit()
    con.close()
    monkeypatch.setattr(chatstudio, "_soul_module", lambda name: (_ for _ in ()).throw(ImportError("no soul")))
    listing = chatstudio.traces(str(db), limit=10)
    assert listing["ok"] is True and listing["count"] == 1
    assert listing["items"][0]["id"] == "t1"
    detail = chatstudio.trace_detail("t1", str(db))
    assert detail["ok"] is True and detail["trace"]["user_text"] == "你好"
    assert chatstudio.trace_detail("missing", str(db))["error"] == "trace_not_found"


def test_run_trial_dry_with_fake_agent(env):
    class FakeAgent:
        def __init__(self, path):
            self.path = path

        def send(self, text):
            return {"ok": True, "reply": "试跑回复：" + text}

    result = chatstudio.run_trial("你好", agent_factory=FakeAgent)
    assert result["ok"] is True and result["dry_run"] is True
    assert result["reply"] == "试跑回复：你好"
    assert result["elapsed_ms"] >= 0
    # 临时目录已清理（DB 路径不再存在）
    assert not Path(result["db"]).exists()
