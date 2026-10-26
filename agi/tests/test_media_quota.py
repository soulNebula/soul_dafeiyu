# -*- coding: utf-8 -*-
"""P19：回复媒体配额（≥2/10；force/鼓励/软冷却；chat 接入）。"""
from __future__ import annotations

import random

import pytest

from agi.selflearn import chat as chat_module
from agi.selflearn import media_quota


def _path(tmp_path):
    return tmp_path / "db.sqlite3"


def test_record_and_recent_usage_emoji_and_media(tmp_path):
    path = _path(tmp_path)
    media_quota.record(path, "t1", None, "好呀😄")
    media_quota.record(path, "t2", {"bubbles": [
        {"type": "text", "text": "笑死"}, {"type": "sticker", "name": "666"}]}, "笑死")
    media_quota.record(path, "t3", None, "普通一句")
    rows = media_quota.recent_usage(path, 10)
    assert len(rows) == 3
    assert rows[0]["used"] is False          # 最新一条无媒体
    assert rows[1]["used"] is True
    assert rows[1]["types"] == ["sticker"]
    assert rows[2]["used"] is True and "emoji" in rows[2]["types"]


def test_need_media_thresholds(tmp_path):
    path = _path(tmp_path)
    assert media_quota.need_media(path)["force"] is True
    media_quota.record(path, "t1", None, "😄")
    assert media_quota.need_media(path)["force"] is True  # 1/10
    media_quota.record(path, "t2", None, "😄")
    info = media_quota.need_media(path)                    # 2/10
    assert info["force"] is False and info["reason"] == "quota_encourage"
    media_quota.record(path, "t3", None, "😄")
    assert media_quota.need_media(path)["force"] is False  # 3/10
    media_quota.record(path, "t4", None, "😄")
    info = media_quota.need_media(path)                    # 4/10
    assert info["force"] is False and info["reason"] == "quota_ok"
    assert info["soft_cool"] is True                       # 最近连续 3 条都带


def test_only_last_ten_count(tmp_path):
    path = _path(tmp_path)
    for i in range(2):
        media_quota.record(path, f"old{i}", None, "😄", now=100 + i)
    for i in range(9):
        media_quota.record(path, f"new{i}", None, "普通", now=200 + i)
    info = media_quota.need_media(path)   # 最近 10 条只有 1 条带媒体
    assert info["force"] is True


def test_prompt_block_text():
    assert "这一条请带一个表情" in media_quota.prompt_block({"force": True})
    assert "10 条里 2–3 条" in media_quota.prompt_block(
        {"force": False, "reason": "quota_encourage"})
    assert "优先用正文 emoji" in media_quota.prompt_block(
        {"force": False, "reason": "quota_ok", "soft_cool": True})
    assert media_quota.prompt_block({"force": False, "reason": "quota_ok"}) == ""


class Fake:
    def __init__(self):
        self.prompts = []

    def available(self):
        return True


def _fake(monkeypatch, text):
    client = Fake()

    def call(messages, **kwargs):
        client.prompts.append("\n".join(str(m.get("content") or "") for m in messages))
        if kwargs.get("purpose") == "selflearn_chat":
            return {"ok": True, "text": text}
        return {"ok": True, "text": "[]"}

    client.chat = call
    monkeypatch.setattr(chat_module, "_ensure_meaningful", lambda reply, **kw: reply)
    return client


def test_chat_force_prompt_and_record(tmp_path, monkeypatch):
    path = _path(tmp_path)
    monkeypatch.setenv("MEDIA_ENABLED", "1")
    client = _fake(monkeypatch, "收到😄")
    agent = chat_module.SelfLearnAgent(path, client=client,
                                       rng=random.Random(1))
    row = agent.send("今天怎么样")
    assert row["ok"] is True
    assert any("这一条请带一个表情" in p for p in client.prompts)
    usage = media_quota.recent_usage(path)
    assert usage and usage[0]["used"] is True and "emoji" in usage[0]["types"]


def test_chat_no_force_prompt_when_quota_met(tmp_path, monkeypatch):
    path = _path(tmp_path)
    media_quota.record(path, "a", None, "😄")
    media_quota.record(path, "b", None, "😄")
    monkeypatch.setenv("MEDIA_ENABLED", "1")
    client = _fake(monkeypatch, "收到")
    agent = chat_module.SelfLearnAgent(path, client=client,
                                       rng=random.Random(1))
    agent.send("今天怎么样")
    assert not any("这一条请带一个表情" in p for p in client.prompts)
    assert any("10 条里 2–3 条" in p for p in client.prompts)


def test_record_failure_does_not_raise(tmp_path, monkeypatch):
    def boom(path=None):
        raise OSError("disk full")
    monkeypatch.setattr(media_quota, "_connect", boom)
    assert media_quota.record(tmp_path / "x.sqlite3", "t", None, "😄")["ok"] is False
    assert media_quota.need_media(tmp_path / "x.sqlite3")["force"] is False
