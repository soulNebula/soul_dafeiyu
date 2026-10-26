# -*- coding: utf-8 -*-
"""P16-A：被动回复贴纸放行（media_sticker_in_reply=true）。"""
from __future__ import annotations

import json
import random

import pytest

from agi.selflearn import chat as chat_module
from agi.selflearn import media_choice


ASSETS = [
    {"name": "666", "path": "/tmp/666.png", "tags": ["好笑"], "extension": ".png"},
    {"name": "偷笑", "path": "/tmp/偷笑.png", "tags": ["好笑"], "extension": ".png"},
    {"name": "亲亲", "path": "/tmp/亲亲.png", "tags": ["亲昵"], "extension": ".png"},
    {"name": "OK", "path": "/tmp/OK.png", "tags": ["同意"], "extension": ".png"},
]


@pytest.fixture(autouse=True)
def _media(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_ENABLED", "1")
    monkeypatch.setenv("MEDIA_STICKER_IN_REPLY", "1")
    by_name = {item["name"]: item for item in ASSETS}
    monkeypatch.setattr(media_choice, "find_asset",
                        lambda name, path=None: by_name.get(str(name).strip()))
    monkeypatch.setattr(media_choice, "_recent_sticker_names",
                        lambda path=None, now=None, days=None: set())
    monkeypatch.setattr(media_choice.emoji_archive, "record_use",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(media_choice, "INDEXED", {"done": True})
    media_choice.reset_history()
    yield
    media_choice.reset_history()


class Fake:
    def available(self):
        return True

    def chat(self, messages, **kwargs):
        if kwargs.get("purpose") == "selflearn_chat":
            return {"ok": True, "text": "今天有点累啊\n[[media:sticker:666]]"}
        return {"ok": True, "text": "[]"}


def test_chat_reply_applies_sticker(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_module, "_ensure_meaningful",
                        lambda reply, **kw: reply)
    agent = chat_module.SelfLearnAgent(tmp_path / "chat.sqlite3", client=Fake(),
                                       rng=random.Random(1))
    row = agent.send("今天有点累")
    assert row["ok"] is True
    assert row["reply_media"] is not None
    assert row["media"] is row["reply_media"]
    types = [b.get("type") for b in row["reply_media"]["bubbles"]]
    assert "sticker" in types
    assert "666" in row["reply_media"].get("stickers", [])


def test_sticker_in_reply_switch_off(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_STICKER_IN_REPLY", "0")
    monkeypatch.setattr(chat_module, "_ensure_meaningful",
                        lambda reply, **kw: reply)
    agent = chat_module.SelfLearnAgent(tmp_path / "chat2.sqlite3", client=Fake(),
                                       rng=random.Random(1))
    row = agent.send("今天有点累")
    assert row["ok"] is True
    assert row["reply_media"] is None


def test_fourth_sticker_truncated_and_mood_blocks_funny():
    directives = {"stickers": ["666", "偷笑", "亲亲", "OK"], "emoji": "", "wishes": []}
    plan = media_choice.build_plan(None, directives, "笑死", state={"mood": 0.6},
                                   recent_media=[], allow_sticker=True)
    names = [b["name"] for b in plan["bubbles"] if b.get("type") in ("sticker", "gif")]
    assert names == ["666", "偷笑", "亲亲"]       # 最多 3 个
    blocked = media_choice.build_plan(None, {"stickers": ["666"], "emoji": ""}, "烦",
                                      state={"mood": 0.2}, recent_media=[],
                                      allow_sticker=True)
    assert blocked is None or all(b.get("type") != "sticker" for b in blocked["bubbles"])


def test_media_disabled_prompt_empty(monkeypatch):
    monkeypatch.setenv("MEDIA_ENABLED", "0")
    assert media_choice.media_prompt_block(None) == ""
