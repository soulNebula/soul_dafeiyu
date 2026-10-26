# -*- coding: utf-8 -*-
"""P13 统一 LLM 路由层测试（全部 tmp_path + mock 后端，不联网）。

覆盖：配置解析/非法配置、purpose→backend、must_api 强制 API、fallback 链、
全失败、记账 JSONL 字段、usage_summary 聚合、日预算降级、rule 后端、
openai_local/deepseek 后端适配、调试接口（purposes/available_backends）。
"""
from __future__ import annotations

import json
import time

import pytest

from agi.llm import openai_local, router as router_mod, usage as usage_mod
from agi.llm.deepseek import DeepSeekBackend
from agi.llm.openai_local import OpenAILocalBackend
from agi.llm.rule import RuleBackend
from agi.llm.router import Router, RouterConfigError, load_config

NOW = 1_700_000_000.0


@pytest.fixture(autouse=True)
def _isolate_router(monkeypatch, tmp_path):
    """不读真实 agi/data/llm_router.json；usage 写 tmp。"""
    monkeypatch.setenv("LLM_ROUTER_CONFIG", str(tmp_path / "missing_router.json"))
    monkeypatch.setenv("LLM_USAGE_PATH", str(tmp_path / "usage.jsonl"))
    router_mod.reset_default_router()
    yield
    router_mod.reset_default_router()


# --------------------------------------------------------------------------- #
# 基础配置 / Fake 后端
# --------------------------------------------------------------------------- #
def _config(**overrides) -> dict:
    cfg = {
        "backends": {
            "local": {"kind": "openai", "base_url": "http://127.0.0.1:8125/v1",
                      "model": "local-3b", "timeout_s": 60},
            "api": {"kind": "deepseek", "model": "deepseek-flash", "timeout_s": 30},
            "rule": {"kind": "rule"},
        },
        "purposes": {
            "video_understanding": {"backend": "api", "must_api": True},
            "ingest_summary": {"backend": "local", "fallback": ["api"], "max_tokens": 400},
            "share_copy": {"backend": "api", "fallback": ["local"]},
            "chat_reply": {"backend": "api"},
            "relevance_score": {"backend": "local", "fallback": ["rule"]},
        },
        "budget": {"daily_api_tokens": 300000, "on_exceed": "local"},
        "log_path": "agi/data/llm_usage.jsonl",
    }
    cfg.update(overrides)
    return cfg


class FakeBackend:
    def __init__(self, alias: str, kind: str, *, ok: bool = True,
                 text: str = "ok-text", tokens=(11, 7), available: bool = True,
                 error: str = ""):
        self.name = alias
        self.kind = kind
        self.ok = ok
        self.text = text
        self.tokens = tokens
        self._available = available
        self.error = error
        self.calls: list[dict] = []

    def available(self):
        return self._available

    def chat(self, messages, *, purpose="chat", max_tokens=800, temperature=0.7,
             timeout=None, json_mode=False):
        self.calls.append({"messages": messages, "purpose": purpose,
                           "max_tokens": max_tokens, "temperature": temperature,
                           "json_mode": json_mode})
        if not self.ok:
            return {"ok": False, "text": "", "error": self.error or "boom"}
        return {"ok": True, "text": self.text,
                "usage": {"tokens_in": self.tokens[0], "tokens_out": self.tokens[1]}}


def _router(cfg=None, **overrides) -> Router:
    cfg = cfg or _config()
    backends = {"local": FakeBackend("local", "openai"),
                "api": FakeBackend("api", "deepseek")}
    backends.update(overrides.pop("backends", {}))
    return Router(cfg, backend_overrides=backends, usage_path=overrides.pop(
        "usage_path", None), now_fn=overrides.pop("now_fn", lambda: NOW),
        **overrides)


def _records(path) -> list[dict]:
    return usage_mod.read_records(path)


# --------------------------------------------------------------------------- #
# 1. 配置解析 / 非法配置
# --------------------------------------------------------------------------- #
def test_load_config_from_file(tmp_path):
    cfg_path = tmp_path / "router.json"
    cfg_path.write_text(json.dumps(_config(), ensure_ascii=False), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert set(cfg["backends"]) == {"local", "api", "rule"}
    assert cfg["purposes"]["video_understanding"]["must_api"] is True
    assert cfg["purposes"]["ingest_summary"]["fallback"] == ["api"]
    assert cfg["budget"]["daily_api_tokens"] == 300000
    # env 覆盖路径
    import os
    os.environ["LLM_ROUTER_CONFIG"] = str(cfg_path)
    assert load_config()["backends"]["api"]["kind"] == "deepseek"


@pytest.mark.parametrize("raw", [
    "not-json",
    {},
    {"backends": {}, "purposes": {"x": {"backend": "local"}}},
    {"backends": {"local": {"kind": "openai"}}, "purposes": {"x": {"backend": "local"}}},
    {"backends": {"local": {"kind": "量子"}}, "purposes": {"x": {"backend": "local"}}},
    {"backends": {"local": {"kind": "rule"}}, "purposes": {"x": {}}},
])
def test_invalid_config_raises_clear_error(tmp_path, raw):
    cfg_path = tmp_path / "bad.json"
    if isinstance(raw, str):
        cfg_path.write_text(raw, encoding="utf-8")
    else:
        cfg_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RouterConfigError):
        load_config(cfg_path)


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(RouterConfigError):
        load_config(tmp_path / "nope.json")


def test_validate_config_warns_undefined_backend():
    cfg = _config()
    cfg["purposes"]["transcribe"] = {"backend": "local_whisper"}
    warnings = router_mod.validate_config(router_mod._normalize_config(cfg))
    assert any("local_whisper" in item for item in warnings)


# --------------------------------------------------------------------------- #
# 2. purpose → backend / must_api
# --------------------------------------------------------------------------- #
def test_purpose_routes_to_configured_backend():
    r = _router()
    result = r.chat("ingest_summary", [{"role": "user", "content": "x"}])
    assert result["ok"] is True and result["backend"] == "local"
    assert result["usage"] == {"tokens_in": 11, "tokens_out": 7}
    assert r._overrides["api"].calls == []
    local_call = r._overrides["local"].calls[0]
    assert local_call["purpose"] == "ingest_summary"
    assert local_call["max_tokens"] == 400          # purpose 里的 max_tokens 生效


def test_must_api_forces_api_even_if_configured_local():
    cfg = _config()
    cfg["purposes"]["video_understanding"] = {"backend": "local", "must_api": True}
    r = _router(cfg)
    result = r.chat("video_understanding", [{"role": "user", "content": "看视频"}])
    assert result["ok"] is True and result["backend"] == "api"
    assert r._overrides["local"].calls == []
    assert any("must_api_forced_api" in item for item in result["warnings"])


def test_must_api_without_api_backend_fails():
    cfg = _config()
    cfg["backends"].pop("api")
    cfg["purposes"]["video_understanding"] = {"backend": "local", "must_api": True}
    r = Router(cfg, backend_overrides={"local": FakeBackend("local", "openai")},
               usage_path=None, now_fn=lambda: NOW)
    result = r.chat("video_understanding", [{"role": "user", "content": "x"}])
    assert result["ok"] is False
    assert "must_api_no_api_backend" in result["error"]


# --------------------------------------------------------------------------- #
# 3. fallback 链 / 全失败
# --------------------------------------------------------------------------- #
def test_local_failure_falls_back_to_api():
    backends = {"local": FakeBackend("local", "openai", ok=False),
                "api": FakeBackend("api", "deepseek")}
    r = _router(backends=backends)
    result = r.chat("ingest_summary", [{"role": "user", "content": "x"}])
    assert result["ok"] is True and result["backend"] == "api"
    assert result["fallback_used"] is True
    assert result["attempts"][0]["backend"] == "local"
    assert result["attempts"][0]["ok"] is False


def test_api_failure_falls_back_to_local():
    backends = {"api": FakeBackend("api", "deepseek", ok=False),
                "local": FakeBackend("local", "openai")}
    r = _router(backends=backends)
    result = r.chat("share_copy", [{"role": "user", "content": "x"}])
    assert result["ok"] is True and result["backend"] == "local"
    assert result["fallback_used"] is True


def test_all_backends_failed_returns_error():
    backends = {"api": FakeBackend("api", "deepseek", ok=False, error="HTTPError: 500")}
    r = _router(backends=backends)
    result = r.chat("chat_reply", [{"role": "user", "content": "x"}])
    assert result["ok"] is False and result["text"] == ""
    assert "all_backends_failed" in result["error"]
    assert "HTTPError" in result["error"]
    records = _records(r.log_path)
    assert records and records[-1]["ok"] is False
    assert records[-1]["purpose"] == "chat_reply"


def test_unknown_backend_alias_is_skipped_with_clear_error():
    cfg = _config()
    cfg["purposes"]["weird"] = {"backend": "nope_backend"}
    r = _router(cfg)
    result = r.chat("weird", [{"role": "user", "content": "x"}])
    assert result["ok"] is False
    assert "unknown_backend" in result["error"]
    assert any("nope_backend" in item for item in r.warnings)


def test_unknown_purpose_returns_explicit_error_without_silence():
    r = _router()
    result = r.chat("no_such_purpose", [{"role": "user", "content": "x"}])
    assert result["ok"] is False
    assert "unknown_purpose:no_such_purpose" in result["error"]
    assert _records(r.log_path)


def test_unavailable_backend_is_skipped():
    backends = {"local": FakeBackend("local", "openai", available=False),
                "api": FakeBackend("api", "deepseek")}
    r = _router(backends=backends)
    result = r.chat("ingest_summary", [{"role": "user", "content": "x"}])
    assert result["backend"] == "api"
    assert result["attempts"][0]["error"] == "unavailable"


# --------------------------------------------------------------------------- #
# 4. rule 后端
# --------------------------------------------------------------------------- #
def test_rule_backend_relevance_score_fallback(tmp_path):
    backends = {"local": FakeBackend("local", "openai", ok=False),
                "rule": RuleBackend("rule")}
    r = _router(backends=backends)
    result = r.chat("relevance_score",
                    [{"role": "user", "content": "我在学做菜"},
                     {"role": "user", "content": "做菜的时候火候很难掌握"}])
    assert result["ok"] is True and result["backend"] == "rule"
    payload = json.loads(result["text"])
    assert 0.0 <= payload["score"] <= 1.0
    assert result["usage"] == {"tokens_in": 0, "tokens_out": 0}


def test_rule_backend_unsupported_purpose_is_explicit():
    backend = RuleBackend("rule")
    result = backend.chat([{"role": "user", "content": "x"}], purpose="chat_reply")
    assert result["ok"] is False
    assert "rule_unsupported_purpose" in result["error"]


# --------------------------------------------------------------------------- #
# 5. 记账 / 汇总
# --------------------------------------------------------------------------- #
def test_usage_record_has_required_fields():
    r = _router()
    r.chat("chat_reply", [{"role": "user", "content": "x"}])
    records = _records(r.log_path)
    assert len(records) == 1
    record = records[0]
    for key in ("ts", "purpose", "backend", "backend_kind", "tokens_in",
                "tokens_out", "latency_ms", "ok", "fallback"):
        assert key in record, key
    assert record["purpose"] == "chat_reply"
    assert record["backend"] == "api" and record["backend_kind"] == "deepseek"
    assert record["tokens_in"] == 11 and record["tokens_out"] == 7


def test_usage_summary_by_purpose_backend_and_day():
    r = _router()
    r.chat("chat_reply", [{"role": "user", "content": "a"}])
    r.chat("ingest_summary", [{"role": "user", "content": "b"}])
    summary = usage_mod.summary(r.log_path, days=7, now=NOW)
    assert summary["total"]["calls"] == 2
    assert summary["total"]["ok"] == 2
    assert set(summary["by_purpose"]) == {"chat_reply", "ingest_summary"}
    assert summary["by_backend"]["api"]["calls"] == 1
    assert summary["by_backend"]["local"]["calls"] == 1
    day_keys = list(summary["by_day"])
    assert len(day_keys) == 1
    assert summary["by_day"][day_keys[0]]["calls"] == 2
    assert summary["total"]["tokens_in"] == 22


# --------------------------------------------------------------------------- #
# 6. 日预算
# --------------------------------------------------------------------------- #
def test_budget_exceeded_degrades_to_local():
    cfg = _config(budget={"daily_api_tokens": 5, "on_exceed": "local"})
    backends = {"local": FakeBackend("local", "openai", text="local-ok"),
                "api": FakeBackend("api", "deepseek", text="api-ok")}
    r = _router(cfg, backends=backends)
    first = r.chat("chat_reply", [{"role": "user", "content": "a"}])
    assert first["ok"] is True and first["backend"] == "api"
    assert r.daily_api_tokens() == 18            # 11 + 7

    second = r.chat("share_copy", [{"role": "user", "content": "b"}])
    assert second["ok"] is True and second["backend"] == "local"
    assert second["budget_exceeded"] is True
    assert any("budget_exceeded" in item for item in second["warnings"])
    last = _records(r.log_path)[-1]
    assert last["backend"] == "local" and last["budget_exceeded"] is True


def test_budget_exceeded_must_api_fails_instead_of_silent_downgrade():
    cfg = _config(budget={"daily_api_tokens": 5, "on_exceed": "local"})
    r = _router(cfg)
    r.chat("chat_reply", [{"role": "user", "content": "a"}])   # 先花掉预算
    result = r.chat("video_understanding", [{"role": "user", "content": "视频"}])
    assert result["ok"] is False
    assert result["error"] == "budget_exceeded:must_api"
    assert result["budget_exceeded"] is True


def test_budget_not_counted_for_local_calls():
    cfg = _config(budget={"daily_api_tokens": 1, "on_exceed": "local"})
    r = _router(cfg)
    result = r.chat("ingest_summary", [{"role": "user", "content": "x"}])
    assert result["backend"] == "local"
    assert r.daily_api_tokens() == 0
    # 本地调用不计预算，API 用途仍可正常走
    second = r.chat("chat_reply", [{"role": "user", "content": "y"}])
    assert second["ok"] is True and second["backend"] == "api"


# --------------------------------------------------------------------------- #
# 7. 后端适配（openai_local / deepseek）
# --------------------------------------------------------------------------- #
def test_openai_local_backend_with_mock_transport():
    captured = {}

    def transport(messages, body, timeout):
        captured.update(body)
        return {"choices": [{"message": {"content": "本地回复"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                "model": "local-3b"}

    backend = OpenAILocalBackend(base_url="http://127.0.0.1:8125/v1",
                                 model="local-3b", transport=transport)
    result = backend.chat([{"role": "user", "content": "hi"}], json_mode=True)
    assert result["ok"] is True and result["text"] == "本地回复"
    assert result["usage"] == {"tokens_in": 12, "tokens_out": 3}
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["model"] == "local-3b"


def test_openai_local_backend_failure_is_explicit():
    def transport(messages, body, timeout):
        raise TimeoutError("timed out")

    backend = OpenAILocalBackend(transport=transport)
    result = backend.chat([{"role": "user", "content": "hi"}])
    assert result["ok"] is False and "TimeoutError" in result["error"]


class _FakeLLMClient:
    def __init__(self, available=True, result=None):
        self._available = available
        self.result = result or {"ok": True, "text": "api-text",
                                 "usage": {"prompt_tokens": 21,
                                           "completion_tokens": 8},
                                 "model": "deepseek-flash"}
        self.calls: list[dict] = []

    def available(self):
        return self._available

    def chat(self, messages, **kwargs):
        self.calls.append(kwargs)
        return self.result


def test_deepseek_backend_wraps_selflearn_client():
    client = _FakeLLMClient()
    backend = DeepSeekBackend(client=client)
    result = backend.chat([{"role": "user", "content": "hi"}], purpose="mood_judge",
                          max_tokens=200, temperature=0.2, json_mode=True)
    assert result["ok"] is True and result["text"] == "api-text"
    assert result["usage"] == {"tokens_in": 21, "tokens_out": 8}
    call = client.calls[0]
    assert call["purpose"] == "mood_judge"
    assert call["max_tokens"] == 200 and call["json_mode"] is True


def test_deepseek_backend_handles_unavailable_and_failure():
    unavailable = DeepSeekBackend(client=_FakeLLMClient(available=False))
    assert unavailable.available() is False
    failing = DeepSeekBackend(client=_FakeLLMClient(
        result={"ok": False, "text": "", "error": "HTTPError: 401"}))
    result = failing.chat([{"role": "user", "content": "hi"}])
    assert result["ok"] is False
    assert "401" in result["error"]


# --------------------------------------------------------------------------- #
# 8. 调试接口
# --------------------------------------------------------------------------- #
def test_purposes_and_available_backends(tmp_path, monkeypatch):
    cfg_path = tmp_path / "router.json"
    cfg = _config()
    cfg["backends"] = {"rule": {"kind": "rule"}}
    cfg["purposes"] = {"relevance_score": {"backend": "rule", "fallback": []}}
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("LLM_ROUTER_CONFIG", str(cfg_path))
    router_mod.reset_default_router()

    got = router_mod.purposes()
    assert got["relevance_score"]["backend"] == "rule"
    backends = router_mod.available_backends()
    assert backends == {"rule": {"kind": "rule", "available": True}}
