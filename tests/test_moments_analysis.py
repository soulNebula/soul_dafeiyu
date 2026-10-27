# -*- coding: utf-8 -*-
"""Moments-B 朋友圈 LLM 分析测试：prompt 裁剪/JSON 解析/缓存/历史/失败路径。

全部 mock `_chat`/momentsstudio/audit，不联网、不调真实模型。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import moments_analysis as ma


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(ma, "ANALYSIS_DIR", tmp_path / "moments")
    audits: list[dict] = []
    monkeypatch.setattr(ma.audit, "record",
                        lambda action, target, **kw: audits.append(
                            {"action": action, "target": target, **kw}))
    yield {"audits": audits, "tmp": tmp_path}


def _moment(index=0, content="今天去爬山，风景很好", **extra):
    data = {
        "tid": f"T{index}",
        "nickname": f"好友{index}",
        "content": content,
        "create_time": 1793000000 + index,
        "likes": index,
        "comment_count": index + 1,
        "has_image": index % 2 == 0,
    }
    data.update(extra)
    return data


GOOD_TEXT = json.dumps({
    "summary": "朋友们在分享户外与日常，整体轻松。",
    "topics": ["爬山", "日常"],
    "mood": "轻松愉快",
    "social_signals": ["周末户外活动多"],
    "notable": ["好友0去爬山"],
    "suggestions": ["可以问问爬山路线"],
}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 1. prompt 构建/裁剪
# --------------------------------------------------------------------------- #
def test_build_messages_contains_required_fields():
    built = ma.build_messages([_moment(0)], question="关注谁最近爱运动")
    messages = built["messages"]
    assert messages[0]["role"] == "system" and "JSON" in messages[0]["content"]
    user = messages[1]["content"]
    assert "好友0" in user and "今天去爬山" in user
    assert "赞 0" in user and "评论 1" in user and "图 1" in user
    assert "关注谁最近爱运动" in user
    assert built["chars"] <= ma.PROMPT_LIMIT


def test_build_messages_truncates_long_single_content():
    huge = _moment(0, content="很长的正文" * 2000)
    built = ma.build_messages([huge])
    user = built["messages"][1]["content"]
    assert built["chars"] <= ma.PROMPT_LIMIT
    assert "…" in user


def test_build_messages_respects_total_limit_many_moments():
    moments = [_moment(index, content="内容" * 200) for index in range(100)]
    built = ma.build_messages(moments, question="总结")
    assert built["chars"] <= ma.PROMPT_LIMIT
    assert len(built["messages"][1]["content"]) <= ma.PROMPT_LIMIT


def test_build_messages_empty():
    built = ma.build_messages([], question="")
    assert built["count"] == 0
    assert "暂无朋友圈" in built["messages"][1]["content"]


# --------------------------------------------------------------------------- #
# 2. 严格 JSON 解析
# --------------------------------------------------------------------------- #
def test_parse_analysis_valid_and_fenced():
    for text in (GOOD_TEXT,
                 "```json\n" + GOOD_TEXT + "\n```",
                 "先说明一下：\n" + GOOD_TEXT + "\n结束"):
        parsed = ma._parse_analysis(text)
        assert parsed["ok"] is True
        assert parsed["analysis"]["summary"].startswith("朋友们")
        assert parsed["analysis"]["topics"] == ["爬山", "日常"]


def test_parse_analysis_invalid_and_missing_fields():
    bad = ma._parse_analysis("不是 JSON")
    assert bad["ok"] is False and bad["parse_error"] == "no_json_object"
    malformed = ma._parse_analysis('{"summary": "x", }')
    assert malformed["ok"] is False
    missing = ma._parse_analysis('{"summary": "只有总结"}')
    assert missing["ok"] is False and "missing_fields" in missing["parse_error"]


def test_parse_analysis_normalizes_list_fields():
    raw = json.dumps({
        "summary": "s", "topics": "单主题", "mood": "m",
        "social_signals": None, "notable": ["a", "", " b "],
        "suggestions": ["做点什么"]})
    parsed = ma._parse_analysis(raw)
    assert parsed["ok"] is True
    assert parsed["analysis"]["topics"] == ["单主题"]
    assert parsed["analysis"]["social_signals"] == []
    assert parsed["analysis"]["notable"] == ["a", "b"]


# --------------------------------------------------------------------------- #
# 3. analyze：成功/空/LLM 失败/解析失败
# --------------------------------------------------------------------------- #
def test_analyze_success(monkeypatch):
    calls = []

    def fake_chat(messages):
        calls.append(messages)
        return {"ok": True, "text": GOOD_TEXT, "backend": "deepseek",
                "usage": {"tokens_in": 10, "tokens_out": 20}}

    monkeypatch.setattr(ma, "_chat", fake_chat)
    result = ma.analyze([_moment(0)], question="")
    assert result["ok"] is True
    assert result["analysis"]["summary"].startswith("朋友们")
    assert result["model"] == "deepseek"
    assert result["count"] == 1 and result["latency_ms"] >= 0
    assert calls and calls[0][0]["role"] == "system"


def test_analyze_no_moments_does_not_call_llm(monkeypatch):
    called = {"value": False}
    monkeypatch.setattr(ma, "_chat",
                        lambda messages: called.update(value=True) or {})
    result = ma.analyze([])
    assert result["ok"] is False and result["error"] == "no_moments"
    assert called["value"] is False


def test_analyze_llm_failure(monkeypatch):
    monkeypatch.setattr(ma, "_chat", lambda messages: {
        "ok": False, "error": "budget_exceeded", "backend": ""})
    result = ma.analyze([_moment(0)])
    assert result["ok"] is False
    assert result["error"].startswith("llm_failed")
    assert result["analysis"] is None


def test_analyze_parse_failure_returns_raw(monkeypatch):
    monkeypatch.setattr(ma, "_chat", lambda messages: {
        "ok": True, "text": "模型直接说了段话，没有 JSON", "backend": "deepseek"})
    result = ma.analyze([_moment(0)])
    assert result["ok"] is False and result["error"] == "parse_error"
    assert "模型直接说了段话" in result["raw"]
    assert result["parse_error"] == "no_json_object"


# --------------------------------------------------------------------------- #
# 4. momentsstudio 依赖
# --------------------------------------------------------------------------- #
def test_load_moments_handles_missing_backend(monkeypatch):
    monkeypatch.setattr(ma, "_moments_module",
                        lambda: (None, "moments 后端未就绪：ImportError"))
    result = ma.load_moments()
    assert result["ok"] is False and "未就绪" in result["error"]


def test_load_moments_uses_list_moments(monkeypatch):
    fake = SimpleNamespace(list_moments=lambda limit=30: {
        "ok": True, "items": [_moment(0), _moment(1)]})
    monkeypatch.setattr(ma, "_moments_module", lambda: (fake, ""))
    result = ma.load_moments(limit=1)
    assert result["ok"] is True and len(result["moments"]) >= 1


def test_load_moments_tids_uses_detail(monkeypatch):
    seen = []

    def detail(tid):
        seen.append(tid)
        return {"tid": tid, "content": "指定的一条", "nickname": "A"}

    fake = SimpleNamespace(list_moments=lambda limit=30: {"items": []},
                           detail=detail)
    monkeypatch.setattr(ma, "_moments_module", lambda: (fake, ""))
    result = ma.load_moments(tids=["T9"])
    assert seen == ["T9"] and result["ok"] is True
    assert result["moments"][0]["tid"] == "T9"


def test_analyze_without_moments_uses_backend(monkeypatch):
    fake = SimpleNamespace(list_moments=lambda limit=30: {
        "items": [_moment(0)]})
    monkeypatch.setattr(ma, "_moments_module", lambda: (fake, ""))
    monkeypatch.setattr(ma, "_chat", lambda messages: {
        "ok": True, "text": GOOD_TEXT, "backend": "local"})
    result = ma.analyze(limit=5)
    assert result["ok"] is True and result["model"] == "local"


# --------------------------------------------------------------------------- #
# 5. 缓存/历史
# --------------------------------------------------------------------------- #
def test_analyze_and_cache_writes_file_and_audit(monkeypatch, _isolate):
    monkeypatch.setattr(ma, "_chat", lambda messages: {
        "ok": True, "text": GOOD_TEXT, "backend": "deepseek"})
    result = ma.analyze_and_cache([_moment(0)], question="看看")
    assert result["ok"] is True
    path = result["cached_path"]
    assert path and ma.ANALYSIS_DIR.is_dir()
    payload = json.loads(open(path, encoding="utf-8").read())
    assert payload["analysis"]["summary"].startswith("朋友们")
    assert payload["model"] == "deepseek"
    assert payload["question"] == "看看"
    assert _isolate["audits"]
    assert _isolate["audits"][0]["action"] == "moments.analyze"


def test_analyze_and_cache_failure_writes_nothing(monkeypatch):
    monkeypatch.setattr(ma, "_chat", lambda messages: {"ok": False,
                                                       "error": "down"})
    result = ma.analyze_and_cache([_moment(0)])
    assert result["ok"] is False
    assert "cached_path" not in result
    assert not list(ma.ANALYSIS_DIR.glob("*.json")) if ma.ANALYSIS_DIR.exists() else True


def test_list_and_get_analysis(monkeypatch):
    monkeypatch.setattr(ma, "_chat", lambda messages: {
        "ok": True, "text": GOOD_TEXT, "backend": "deepseek"})
    first = ma.analyze_and_cache([_moment(0)])
    second = ma.analyze_and_cache([_moment(1)])
    listed = ma.list_analyses(limit=10)
    assert listed["ok"] is True and len(listed["items"]) >= 2
    names = {item["name"] for item in listed["items"]}
    assert first["name"] in names and second["name"] in names
    detail = ma.get_analysis(first["name"])
    assert detail["ok"] is True and detail["item"]["analysis"]["summary"]


def test_get_analysis_invalid_name_and_missing():
    assert ma.get_analysis("../secret.json")["error"] == "not_found"
    assert ma.get_analysis("not-json.txt")["error"] == "invalid_name"
    assert ma.get_analysis("analysis_missing.json")["error"] == "not_found"


def test_routes_contract():
    assert ma.ROUTES == (
        ("POST", "/api/wb/moments/analyze", "analyze_and_cache"),
        ("GET", "/api/wb/moments/analyses", "list_analyses"),
        ("GET", "/api/wb/moments/analysis/{name}", "get_analysis"),
    )
