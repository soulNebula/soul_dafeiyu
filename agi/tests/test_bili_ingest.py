# -*- coding: utf-8 -*-
"""P12-A：B站素材摄取 + 印象库（全部 mock，不联网）。"""
from __future__ import annotations

import json
import time

import pytest

from agi.integrations import bili_ingest


class FakeVideo:
    def __init__(self, *, subtitle=True, title="测试视频"):
        self.subtitle = subtitle
        self.title = title

    async def get_info(self):
        return {
            "aid": 123, "cid": 456, "title": self.title,
            "owner": {"name": "测试UP", "mid": 999},
            "duration": 300, "pic": "https://cover", "desc": "这是一个测试简介",
            "tname": "知识", "stat": {"view": 1000, "like": 100, "coin": 10,
                                      "favorite": 20},
        }

    async def get_subtitle(self, cid=None):
        if not self.subtitle:
            return {"subtitles": []}
        return {"subtitles": [{"lan": "zh-CN",
                               "subtitle_url": "//example.com/sub.json"}]}

    async def get_danmakus(self):
        class D:
            def __init__(self, text):
                self.text = text
        return [D("弹幕一"), D("弹幕二"), D("弹幕一")]

    async def get_tags(self):
        return [{"tag_name": "AI"}, {"tag_name": "测试"}]


class FakeComment:
    async def get_comments(self, *args, **kwargs):
        return {"replies": [
            {"content": {"message": "这条评论很好"}, "like": 10,
             "member": {"uname": "甲"}},
            {"content": {"message": "另一条评论"}, "like": 50,
             "member": {"uname": "乙"}},
        ]}


def _fake_chat_factory(overrides=None, calls=None):
    overrides = overrides or {}
    calls = calls if calls is not None else []

    def fake_chat(purpose, messages, **kwargs):
        calls.append(purpose)
        if purpose in overrides:
            value = overrides[purpose]
            if callable(value):
                value = value(len([c for c in calls if c == purpose]))
            if isinstance(value, dict) and not value.get("ok", True):
                return value
            if isinstance(value, str):
                return {"ok": True, "text": value, "backend": "api",
                        "usage": {}, "latency_ms": 1.0}
            return {"ok": True, "text": json.dumps(value, ensure_ascii=False),
                    "backend": "api", "usage": {"total_tokens": 10},
                    "latency_ms": 2.0}
        defaults = {
            "keyword_extract": {"topics": ["AI", "测试"]},
            "ingest_summary": {"summary": "一个测试视频的摘要", "emotion": "positive"},
            "video_understanding": {"value": 0.8, "why": "讲得具体",
                                    "my_take": "我看了觉得挺有意思"},
            "share_copy": {"share_line": "刷到一个讲得挺清楚的视频"},
        }
        data = defaults.get(purpose, {})
        return {"ok": True, "text": json.dumps(data, ensure_ascii=False),
                "backend": "api", "usage": {"total_tokens": 10},
                "latency_ms": 2.0}

    return fake_chat, calls


@pytest.fixture(autouse=True)
def _mock_bili(monkeypatch, tmp_path):
    monkeypatch.setattr(bili_ingest, "load_credential", lambda path=None: object())
    monkeypatch.setattr(bili_ingest, "_video_factory",
                        lambda bvid, credential: FakeVideo())
    monkeypatch.setattr(bili_ingest, "bili_comment", FakeComment())
    async def fake_subtitle(url):
        return "这是字幕第一句。\n这是字幕第二句。"
    monkeypatch.setattr(bili_ingest, "_download_subtitle", fake_subtitle)
    yield


def test_ingest_mock_full_fields(tmp_path):
    calls = []
    fake_chat, calls = _fake_chat_factory(calls=calls)
    bili_ingest.llm_chat = fake_chat
    result = bili_ingest.ingest("BV1TEST", path=tmp_path / "db.sqlite3")
    assert result["ok"] is True
    imp = result["impression"]
    assert imp["bvid"] == "BV1TEST"
    assert imp["owner"] == "测试UP"
    assert imp["summary"]
    assert imp["topics"] == ["AI", "测试"]
    assert imp["my_take"].startswith("我看了")
    assert imp["share_line"]
    assert imp["subtitle_len"] > 0 and imp["subtitle_missing"] is False
    assert imp["comments"][0]["like"] == 50
    assert set(imp["danmaku"]) == {"弹幕一", "弹幕二"}
    assert calls == ["keyword_extract", "ingest_summary",
                     "video_understanding", "share_copy"]
    assert result["steps"]["video_understanding"]["backend"] == "api"
    rows = bili_ingest.recent_impressions(10, path=tmp_path / "db.sqlite3")
    assert len(rows) == 1 and rows[0]["bvid"] == "BV1TEST"


def test_subtitle_missing_still_summarizes(tmp_path, monkeypatch):
    monkeypatch.setattr(bili_ingest, "_video_factory",
                        lambda bvid, credential: FakeVideo(subtitle=False))
    prompts = []
    fake_chat, _ = _fake_chat_factory()
    def tracking(purpose, messages, **kwargs):
        prompts.append(messages[-1]["content"])
        return fake_chat(purpose, messages, **kwargs)
    bili_ingest.llm_chat = tracking
    result = bili_ingest.ingest("BV1NOSUB", path=tmp_path / "db.sqlite3")
    assert result["ok"] is True
    assert result["impression"]["subtitle_missing"] is True
    assert result["subtitle_len"] == 0
    assert any("标题" in prompt for prompt in prompts)


def test_bad_json_retries_once_then_fails_without_partial(tmp_path):
    bad_then_good, calls = _fake_chat_factory(overrides={
        "keyword_extract": lambda n: "not json" if n == 1 else {"topics": ["AI"]},
    })
    bili_ingest.llm_chat = bad_then_good
    result = bili_ingest.ingest("BV1RETRY", path=tmp_path / "db.sqlite3")
    assert result["ok"] is True
    assert calls.count("keyword_extract") == 2

    always_bad, _ = _fake_chat_factory(overrides={
        "ingest_summary": "never json",
    })
    bili_ingest.llm_chat = always_bad
    failed = bili_ingest.ingest("BV1BAD", path=tmp_path / "db.sqlite3")
    assert failed["ok"] is False
    assert failed["error_code"] == "bili_ingest_failed"
    assert all(row["bvid"] != "BV1BAD" for row in bili_ingest.recent_impressions(
        10, path=tmp_path / "db.sqlite3"))


def test_not_logged_in(tmp_path, monkeypatch):
    def boom(path=None):
        raise bili_ingest.BiliNotLoggedIn("bili_not_logged_in: missing")
    monkeypatch.setattr(bili_ingest, "load_credential", boom)
    fetched = bili_ingest.fetch_video("BV1NOLOGIN")
    assert fetched["ok"] is False
    assert fetched["error_code"] == "bili_not_logged_in"


def test_upsert_and_queries(tmp_path):
    fake_chat, _ = _fake_chat_factory()
    bili_ingest.llm_chat = fake_chat
    path = tmp_path / "db.sqlite3"
    first = bili_ingest.ingest("BV1DUP", path=path)
    second = bili_ingest.ingest("BV1DUP", path=path)
    assert first["ok"] and second["ok"]
    rows = bili_ingest.recent_impressions(10, path=path)
    assert len(rows) == 1
    got = bili_ingest.get_impression("BV1DUP", path=path)
    assert got and got["bvid"] == "BV1DUP"
    assert bili_ingest.mark_shared("BV1DUP", path=path) is True
    assert bili_ingest.get_impression("BV1DUP", path=path)["shared"] == 1
    assert bili_ingest.get_impression("BV-NOPE", path=path) is None


def test_dry_does_not_call_llm(tmp_path):
    calls = []
    fake_chat, calls = _fake_chat_factory(calls=calls)
    bili_ingest.llm_chat = fake_chat
    result = bili_ingest.ingest("BV1DRY", path=tmp_path / "db.sqlite3", dry=True)
    assert result["ok"] is True and result["dry"] is True
    assert calls == []
    assert bili_ingest.recent_impressions(10, path=tmp_path / "db.sqlite3") == []
