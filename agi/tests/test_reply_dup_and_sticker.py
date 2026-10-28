# -*- coding: utf-8 -*-
"""2026-10-28 线上事故回归：回复重复（媒体计划里的文本气泡重复发）+ 贴纸按名字发不出去。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agi.wechat_gateway.sender import WxAutoSender
from agi.wechat_gateway.server import Gateway


def _gw(tmp_path, **over):
    cfg = {
        "enabled": True, "port": 801, "dry_run": False, "send_mode": "auto",
        "allowed_contacts": ["好友A"], "monitored_contacts": ["好友A"],
        "admin_token": "tok_secure_123",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "quiet_hours": [0, 0], "min_interval_s": 0, "media_enabled": True,
    }
    cfg.update(over)
    return Gateway(cfg, stop_path=tmp_path / "STOP",
                   config_path=tmp_path / "config.json")


class _FakeAgent:
    media: dict | None = None

    def __init__(self, db_path):
        self.db_path = db_path

    def send(self, text, image=None):
        return {"turn_id": "t1", "bubbles": ["在呢"], "media": _FakeAgent.media}


# --------------------------------------------------------------------------- #
# ① 媒体计划里的文本气泡不得再发一遍（复读事故根因）
# --------------------------------------------------------------------------- #
def test_reply_media_skips_text_bubbles(tmp_path, monkeypatch):
    import agi.selflearn.chat as chat_mod
    _FakeAgent.media = {"bubbles": [
        {"type": "text", "text": "在呢"},
        {"type": "sticker", "name": "囧", "path": r"C:\tmp\x.png"},
    ]}
    monkeypatch.setattr(chat_mod, "SelfLearnAgent", _FakeAgent)
    gw = _gw(tmp_path)
    calls = []
    monkeypatch.setattr(gw, "send_media_batch",
                        lambda contact, items, **kw: calls.append(items) or {"ok": True})
    gw._reply({"friend": "好友A", "text": "在吗", "kind": "text"})
    assert len(calls) == 1
    items = calls[0]
    assert [i["type"] for i in items] == ["sticker"], items


def test_reply_media_only_text_means_no_batch(tmp_path, monkeypatch):
    import agi.selflearn.chat as chat_mod
    _FakeAgent.media = {"bubbles": [{"type": "text", "text": "在呢"}]}
    monkeypatch.setattr(chat_mod, "SelfLearnAgent", _FakeAgent)
    gw = _gw(tmp_path)
    calls = []
    monkeypatch.setattr(gw, "send_media_batch",
                        lambda *a, **k: calls.append(a) or {"ok": True})
    gw._reply({"friend": "好友A", "text": "在吗", "kind": "text"})
    assert calls == []


# --------------------------------------------------------------------------- #
# ② 贴纸按「名字」也要能找到素材（库里是 名字→路径，文件名是哈希）
# --------------------------------------------------------------------------- #
class _FakeWx:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.files = []
        self.texts = []

    def SendFiles(self, path, who=None):  # noqa: N802 - 兼容 wxauto 命名
        self.files.append(str(path))
        return {"status": "成功"}

    def SendMsg(self, text, who=None):  # noqa: N802
        self.texts.append(str(text))
        return {"status": "成功"}


def test_sticker_name_resolved_from_library(tmp_path, monkeypatch):
    from agi.selflearn import media_choice as mc
    from agi.wechat_gateway import sender as sender_mod

    sticker = tmp_path / "jiong.png"
    sticker.write_bytes(b"png")
    cfg = {"media_enabled": True, "media_dirs": [str(tmp_path)], "media_max_mb": 10}
    monkeypatch.setattr(mc, "find_asset",
                        lambda name, path=None: {"name": "囧", "path": str(sticker)})
    wx = _FakeWx(tmp_path)
    sender = WxAutoSender(cfg=cfg)
    ok, error, used, tries = sender._sticker_bubbles(
        wx, {"type": "sticker", "name": "囧"}, cfg)
    assert ok is True and error == "" and used == "image"
    assert wx.files == [str(sticker)]
    assert tries and tries[0] == "image"


def test_sticker_missing_falls_back_to_emoji(tmp_path, monkeypatch):
    from agi.selflearn import media_choice as mc

    monkeypatch.setattr(mc, "find_asset", lambda name, path=None: None)
    cfg = {"media_enabled": True, "media_dirs": [str(tmp_path)],
           "media_max_mb": 10, "sticker_fallback_emoji": "😄"}
    wx = _FakeWx(tmp_path)
    sender = WxAutoSender(cfg=cfg)
    ok, error, used, tries = sender._sticker_bubbles(
        wx, {"type": "sticker", "name": "不存在的贴纸"}, cfg)
    assert ok is True and used == "emoji_text"
    assert wx.texts and wx.texts[0] == "😄"
