# -*- coding: utf-8 -*-
"""Moments Studio 后端测试：fetch 解析/缓存去重、列表过滤、详情缺失、媒体、失败不崩。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import momentsstudio


@pytest.fixture
def env(tmp_path, monkeypatch):
    soul = tmp_path / "SoulAgent"
    (soul / "tools").mkdir(parents=True)
    (soul / "tools" / "moments_fetch.py").write_text("# stub\n", encoding="utf-8")
    data = tmp_path / "idea" / ".data"
    data.mkdir(parents=True)
    monkeypatch.setattr(momentsstudio.paths, "soulagent_home", lambda: soul)
    monkeypatch.setattr(momentsstudio.paths, "DATA_DIR", data)
    monkeypatch.setattr(momentsstudio, "_media_roots", lambda: [])
    audits = []
    monkeypatch.setattr(momentsstudio.audit, "record",
                        lambda action, target, **kw: audits.append(
                            {"action": action, "target": target, **kw}))
    return {"soul": soul, "data": data, "audits": audits}


class Proc:
    def __init__(self, stdout="[]", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _rows():
    return [
        {"id": "m1", "tid": "u1", "username": "u1", "nickname": "小鱼",
         "text": "今天撸猫", "create_time": 100, "images": [], "likes": ["a"],
         "comments": [], "location": {"name": "家"}},
        {"id": "m2", "tid": "u2", "username": "u2", "nickname": "朋友",
         "text": "下雨了", "create_time": 200, "images": [], "likes": [],
         "comments": [{"text": "带伞"}], "location": {}},
    ]


def test_fetch_parse_cache_and_dedupe(env, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Proc(json.dumps(_rows(), ensure_ascii=False))

    monkeypatch.setattr(momentsstudio.subprocess, "run", fake_run)
    first = momentsstudio.fetch(limit=5)
    assert first["ok"] is True and first["count"] == 2
    cache = env["data"] / "moments" / f"{__import__('time').strftime('%Y-%m-%d')}.jsonl"
    assert cache.is_file()
    assert len(cache.read_text(encoding="utf-8").splitlines()) == 2
    second = momentsstudio.fetch(limit=5)
    assert second["ok"] is True and second["count"] == 2
    assert len(cache.read_text(encoding="utf-8").splitlines()) == 2
    assert any(item["action"] == "moments.fetch" for item in env["audits"])


def test_list_filter_and_limit(env, monkeypatch):
    monkeypatch.setattr(momentsstudio.subprocess, "run",
                        lambda cmd, **kw: Proc(json.dumps(_rows(), ensure_ascii=False)))
    momentsstudio.fetch(5)
    all_items = momentsstudio.list_moments(limit=10)
    assert all_items["ok"] is True and all_items["total"] == 2
    assert all_items["items"][0]["id"] == "m2"
    cat = momentsstudio.list_moments(keyword="猫")
    assert cat["count"] == 1 and cat["items"][0]["id"] == "m1"
    since = momentsstudio.list_moments(since=150)
    assert since["count"] == 1 and since["items"][0]["id"] == "m2"
    limited = momentsstudio.list_moments(limit=1)
    assert limited["count"] == 1


def test_detail_found_and_missing(env, monkeypatch):
    monkeypatch.setattr(momentsstudio.subprocess, "run",
                        lambda cmd, **kw: Proc(json.dumps(_rows(), ensure_ascii=False)))
    momentsstudio.fetch(5)
    found = momentsstudio.detail("m1")
    assert found["ok"] is True and found["item"]["likes"] == ["a"]
    missing = momentsstudio.detail("nope")
    assert missing["ok"] is False and missing["error"] == "moment_not_found"
    assert momentsstudio.detail("")["error"] == "tid_required"


def test_fetch_failure_does_not_crash(env, monkeypatch):
    monkeypatch.setattr(momentsstudio.subprocess, "run",
                        lambda cmd, **kw: Proc("", 2, "boom"))
    failed = momentsstudio.fetch(3)
    assert failed["ok"] is False and failed["count"] == 0
    assert failed["error"] == "fetch_command_failed"
    # timeout
    def timeout_run(cmd, **kw):
        raise momentsstudio.subprocess.TimeoutExpired(cmd, 180)
    monkeypatch.setattr(momentsstudio.subprocess, "run", timeout_run)
    timed = momentsstudio.fetch(3)
    assert timed["ok"] is False and timed["error"] == "fetch_timeout_180s"


def test_media_path_finds_image(env, monkeypatch, tmp_path):
    md5 = "4bf149cf0f73b0ede33a779af18ad8b6"
    root = tmp_path / "xwechat_files"
    img = root / "acc" / "cache" / "2026-10" / "Sns" / "Img" / "ab"
    img.mkdir(parents=True)
    target = img / f"{md5}.jpg"
    target.write_bytes(b"JPEG")
    (img / f"{md5}.dat").write_bytes(b"encrypted")
    monkeypatch.setattr(momentsstudio, "_media_roots", lambda: [root])
    found = momentsstudio.media_path(md5)
    assert found == target.resolve()
    assert momentsstudio.media_path("not-md5") is None
    assert momentsstudio.media_path("0" * 32) is None
