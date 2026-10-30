# -*- coding: utf-8 -*-
"""回归：用户要表情包时，模型只输出 [[media:sticker:x]] 不能变成"我没接住"。

2026-10-31 真机事故：用户说"给我发个表情包"，模型回 `[[media:sticker:666]]`；
build_plan 对空正文直接返回 None → 媒体被丢，_ensure_meaningful 把空回复换成罐头句，
用户收到的是"这次我没接住，你再说一次？"。
"""
from __future__ import annotations

from pathlib import Path

from agi.selflearn import chat as chat_mod
from agi.selflearn import media_choice
from agi.selflearn.chat import MEANINGFUL_FALLBACKS, SelfLearnAgent


class FakeClient:
    model = "fake"

    def __init__(self, text: str) -> None:
        self.text = text

    def available(self) -> bool:
        return True

    def chat(self, messages, **kwargs):
        return {"ok": True, "text": self.text, "model": "fake",
                "usage": {}, "finish_reason": "stop"}


def _patch_media(monkeypatch):
    monkeypatch.setattr(media_choice, "media_settings",
                        lambda: {"enabled": True, "sticker_in_reply": True,
                                 "max_stickers": 3, "max_consecutive": 2})
    monkeypatch.setattr(media_choice, "recent_media", lambda path: [])
    monkeypatch.setattr(media_choice, "record_media", lambda path, sent: None)

    def fake_build_plan(path, directives, text, **kwargs):
        # 复刻真实约束：正文为空 → 计划为 None（表情包被丢）
        if not str(text or "").strip():
            return None
        return {"bubbles": [{"type": "sticker", "name": "666"}],
                "wishes": [], "reason": "model:sticker:666",
                "dropped": [], "stickers": ["666"]}

    monkeypatch.setattr(media_choice, "build_plan", fake_build_plan)


def _send(tmp_path: Path, monkeypatch, reply: str, user_text: str) -> dict:
    _patch_media(monkeypatch)
    agent = SelfLearnAgent(tmp_path / "selflearn.sqlite3",
                           client=FakeClient(reply))
    return agent.send(user_text)


def test_sticker_only_reply_keeps_media(tmp_path, monkeypatch):
    result = _send(tmp_path, monkeypatch,
                   "[[media:sticker:666]]", "我想让你给我发个表情包，可以吗？")
    assert result["reply"] not in MEANINGFUL_FALLBACKS
    assert result["reply"], "不能返回空正文"
    assert result["media"] and result["media"]["bubbles"]


def test_short_text_plus_sticker_is_not_replaced(tmp_path, monkeypatch):
    result = _send(tmp_path, monkeypatch,
                   "好好[[media:sticker:666]]", "给我发个表情包")
    assert result["reply"] not in MEANINGFUL_FALLBACKS
    assert "好好" in str(result["reply"])
    assert result["media"] and result["media"]["bubbles"]


def test_explicit_sticker_survives_strict_plan_rejection(tmp_path, monkeypatch):
    """build_plan 被冷却/情绪/校验拦掉时，用户明确要贴纸仍应发出去。"""
    _patch_media(monkeypatch)
    monkeypatch.setattr(media_choice, "build_plan",
                        lambda *args, **kwargs: None)   # 模拟严格校验全拒
    monkeypatch.setattr(media_choice, "find_asset",
                        lambda name, path=None: {"name": name,
                                                 "path": str(tmp_path / "666.png")})
    agent = SelfLearnAgent(tmp_path / "selflearn.sqlite3",
                           client=FakeClient("[[media:sticker:666]]"))
    result = agent.send("给我发个表情包")
    assert result["reply"] not in MEANINGFUL_FALLBACKS
    assert result["media"] and result["media"]["bubbles"]
    assert result["media"]["bubbles"][0]["type"] == "sticker"
