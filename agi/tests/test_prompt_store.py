# -*- coding: utf-8 -*-
"""P4-b：提示词存储（默认导出 / 热加载 / 校验 / 覆盖生效 / 回滚字节一致）。"""
from __future__ import annotations

import json
import logging
import os
import time

import pytest

from agi.selflearn import chat, proactive, prompt_store


@pytest.fixture(autouse=True)
def _store_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_store, "PROMPTS_PATH", tmp_path / "prompts.json")
    monkeypatch.setattr(prompt_store, "PROMPT_OVERRIDE_LOG_PATH", tmp_path / "prompt_override.log")
    monkeypatch.setattr(prompt_store, "_LAST_ERROR_TS", 0.0)
    yield
    prompt_store.delete_all()


def _read_payload() -> dict:
    return json.loads((prompt_store.prompts_path()).read_text(encoding="utf-8"))


def test_defaults_export_all_keys_and_match_code_constants():
    values = prompt_store.defaults()
    assert set(values) == set(prompt_store.KNOWN_KEYS)
    assert values["proactive.persona_constraints"] == proactive.PERSONA_CONSTRAINTS
    assert values["proactive.anti_fabrication"] == proactive.GROUNDING_CONSTRAINTS
    assert values["proactive.punctuation"] == proactive.RUNON_CONSTRAINTS
    assert values["proactive.continuity"] == proactive.CONTINUITY_CONSTRAINTS
    assert values["proactive.voice_fewshot"] == proactive.VOICE_FEWSHOT_SECTION
    assert values["chat.system"] == chat.SYSTEM
    for kind in ("care", "share", "light", "followup", "silence_ack"):
        assert values[f"proactive.kind.{kind}"] == proactive.KIND_INSTRUCTION[kind]


def test_set_get_delete_and_atomic_write():
    payload = prompt_store.set_many({"proactive.kind.care": "覆盖后的 care 指令"})
    assert payload["version"] >= 2
    assert prompt_store.get("proactive.kind.care") == "覆盖后的 care 指令"
    on_disk = _read_payload()
    assert on_disk["prompts"]["proactive.kind.care"] == "覆盖后的 care 指令"
    assert list(prompt_store.prompts_path().parent.glob("*.tmp.*")) == []
    # 空串 = 删除覆盖，回退默认
    prompt_store.set_many({"proactive.kind.care": ""})
    assert prompt_store.get("proactive.kind.care") == ""
    assert "proactive.kind.care" not in prompt_store.overrides()
    prompt_store.delete_all()
    assert prompt_store.overrides() == {}


def test_mtime_hot_reload():
    prompt_store.set_many({"proactive.kind.share": "第一版"})
    assert prompt_store.get("proactive.kind.share") == "第一版"
    path = prompt_store.prompts_path()
    raw = _read_payload()
    raw["version"] = int(raw.get("version") or 1) + 1
    raw["prompts"]["proactive.kind.share"] = "第二版"
    time.sleep(0.01)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    os.utime(path, None)
    assert prompt_store.get("proactive.kind.share") == "第二版"


def test_validate_rules():
    ok, errors = prompt_store.validate({"version": 1, "prompts": {
        "proactive.kind.care": "x"}})
    assert ok and errors == []
    ok, errors = prompt_store.validate({"version": 1, "prompts": {
        "proactive.kind.care": "x" * 4001}})
    assert not ok and any("4000" in error for error in errors)
    ok, errors = prompt_store.validate({"version": 1, "prompts": {"unknown": "x"}})
    assert not ok and any("未知键" in error for error in errors)
    ok, errors = prompt_store.validate({"version": 1, "prompts": {"proactive.kind.care": 1}})
    assert not ok and any("字符串" in error for error in errors)


def test_set_many_rejects_bad_input():
    with pytest.raises(ValueError):
        prompt_store.set_many({"unknown.key": "x"})
    with pytest.raises(ValueError):
        prompt_store.set_many({"proactive.kind.care": 123})
    with pytest.raises(ValueError):
        prompt_store.set_many({"proactive.kind.care": "x" * 4001})


def test_corrupt_file_falls_back(caplog):
    path = prompt_store.prompts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        prompt_store.load(force=True)
    assert prompt_store.overrides() == {}
    assert any("prompt_store_error" in record.message for record in caplog.records)


def test_override_used_by_proactive_prompt(tmp_path):
    path = tmp_path / "p.sqlite3"
    from agi.selflearn import db, trust

    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", "周末去爬山了，累死", time.time() - 3600))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}), time.time()))
    con.commit()
    con.close()
    trust.set_value(path, 0.6)

    class Fake:
        def __init__(self):
            self.prompts = []

        def available(self):
            return True

        def chat(self, messages, **kwargs):
            self.prompts.append(str(messages[-1].get("content") or ""))
            return {"ok": True, "text": "资料还没理完，有点烦"}

    old = proactive.build_candidates
    proactive.build_candidates = lambda p, n=None: [{
        "kind": "care", "text": "x", "reason": "t", "base": 0.5,
        "score": 0.6, "anchor": "", "topic": "",
    }]
    try:
        prompt_store.set_many({"proactive.kind.care": "OVERRIDE_KIND_TEXT"})
        prompt_store.set_many({"proactive.anti_fabrication": "OVERRIDE_GROUNDING"})
        client = Fake()
        row = proactive.tick(path, client=client, now=time.time(), cooldown=0,
                             min_score=0, respect_state=False)
        assert row["available"] is True
        assert any("OVERRIDE_KIND_TEXT" in prompt for prompt in client.prompts)
        assert any("OVERRIDE_GROUNDING" in prompt for prompt in client.prompts)
        # 未覆盖键仍用默认
        assert any(proactive.PERSONA_CONSTRAINTS in prompt for prompt in client.prompts)
        # 回滚后与默认逐字节一致
        prompt_store.delete_all()
        before = proactive._kind_prompt("care", {"kind": "care"}, 0.55, 0.45, "用户:在吗")
        prompt_store.set_many({"proactive.persona_constraints": "TEMP"})
        prompt_store.delete_all()
        after = proactive._kind_prompt("care", {"kind": "care"}, 0.55, 0.45, "用户:在吗")
        assert before == after
    finally:
        proactive.build_candidates = old


def test_chat_system_override_and_rollback():
    assert chat._system_prompt() == chat.SYSTEM
    prompt_store.set_many({"chat.system": "OVERRIDE_CHAT_SYSTEM"})
    assert chat._system_prompt() == "OVERRIDE_CHAT_SYSTEM"
    assert "OVERRIDE_CHAT_SYSTEM" not in chat.SYSTEM
    prompt_store.delete_all()
    assert chat._system_prompt() == chat.SYSTEM


def test_snapshot_and_file_shape():
    prompt_store.set_many({"proactive.kind.light": "light-override"})
    payload = _read_payload()
    assert payload["version"] >= 2
    assert set(payload["prompts"]) == {"proactive.kind.light"}
    assert payload["prompts"]["proactive.kind.light"] == "light-override"
    snap = prompt_store.snapshot()
    assert snap["overrides"]["proactive.kind.light"] == "light-override"
    assert snap["effective"]["proactive.kind.light"] == "light-override"
    assert snap["effective"]["chat.system"] == chat.SYSTEM


def test_custom_prompt_add_and_delete_does_not_touch_builtin():
    payload = prompt_store.set_many({"custom.tone": "自定义语气"})
    assert payload["prompts"]["custom.tone"] == "自定义语气"
    snap = prompt_store.snapshot()
    assert snap["effective"]["custom.tone"] == "自定义语气"
    assert "custom.tone" in snap["custom_keys"]
    # 内置键仍在，自定义可删除
    assert "chat.system" in snap["effective"]
    prompt_store.set_many({"custom.tone": ""})
    assert "custom.tone" not in prompt_store.overrides()


def test_custom_prompt_key_validation():
    assert prompt_store.is_custom_key("custom.a_b-c.1")
    assert not prompt_store.is_custom_key("custom.")
    assert not prompt_store.is_custom_key("chat.system")
    with pytest.raises(ValueError):
        prompt_store.set_many({"unknown.key": "x"})
    with pytest.raises(ValueError):
        prompt_store.set_many({"custom.bad key": "x"})
