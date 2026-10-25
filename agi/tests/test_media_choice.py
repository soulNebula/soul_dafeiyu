# -*- coding: utf-8 -*-
"""P5-b：媒体解析 + 安全上限（大模型决定；多贴纸/wish；获取管线 stub）。"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from agi.selflearn import config, media_choice, proactive


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_ENABLED", "1")
    monkeypatch.setenv("MEDIA_WISH_FILE", str(tmp_path / "wishes.jsonl"))
    monkeypatch.setattr(media_choice, "INDEXED", {"done": True})
    monkeypatch.setattr(media_choice, "_recent_sticker_names",
                        lambda path=None, now=None, days=None: set())
    media_choice.reset_history()
    yield
    media_choice.reset_history()


ASSETS = [
    {"name": "666", "path": "/tmp/666.png", "tags": ["好笑", "赞"], "extension": ".png"},
    {"name": "偷笑", "path": "/tmp/偷笑.png", "tags": ["好笑"], "extension": ".png"},
    {"name": "亲亲", "path": "/tmp/亲亲.png", "tags": ["亲昵"], "extension": ".png"},
    {"name": "OK", "path": "/tmp/OK.png", "tags": ["同意"], "extension": ".png"},
    {"name": "笑gif", "path": "/tmp/x.gif", "tags": ["好笑"], "extension": ".gif"},
]


@pytest.fixture(autouse=True)
def _fake_assets(monkeypatch):
    by_name = {item["name"]: item for item in ASSETS}
    monkeypatch.setattr(media_choice, "find_asset",
                        lambda name, path=None: by_name.get(str(name).strip()))
    monkeypatch.setattr(media_choice.emoji_archive, "record_use",
                        lambda *a, **k: {"ok": True})


def _state(text="看到一个段子，笑死了", **extra):
    return {"text": text, "mood": 0.6, **extra}


def test_parse_single_and_multi_stickers():
    clean, plan = media_choice.parse_media_directive(
        "看到一个段子，笑死了\n[[media:sticker:666]]")
    assert clean == "看到一个段子，笑死了"
    assert plan["stickers"] == ["666"]
    clean, plan = media_choice.parse_media_directive(
        "笑死了\n[[media:sticker:666]]\n[[media:sticker:偷笑]]\n[[media:sticker:亲亲]]")
    assert plan["stickers"] == ["666", "偷笑", "亲亲"]
    assert "[[media:" not in clean


def test_parse_mixed_and_invalid():
    clean, plan = media_choice.parse_media_directive(
        "太无语了\n[[media:emoji:😄]]\n[[media:sticker:666]]\n"
        "[[media:wish:想表达\"无语但好笑\"]]\n[[media:sticker]]")
    assert plan["emoji"] == "😄"
    assert plan["stickers"] == ["666"]
    assert plan["wishes"] == ['想表达"无语但好笑"']
    assert "[[media:" not in clean


def test_build_multi_stickers_and_emoji():
    directives = {"stickers": ["666", "偷笑", "亲亲"], "emoji": "😄", "wishes": []}
    plan = media_choice.build_plan(None, directives, "笑死了", state=_state(),
                                   recent_media=[], allow_sticker=True)
    assert plan is not None
    assert plan["bubbles"][0]["type"] == "emoji_text"
    assert plan["bubbles"][0]["text"].endswith("😄")
    assert [b["name"] for b in plan["bubbles"][1:]] == ["666", "偷笑", "亲亲"]


def test_fourth_sticker_is_truncated(monkeypatch):
    # 加第 4 个可用贴纸
    extra = {"name": "耶", "path": "/tmp/ye.png", "tags": ["开心"], "extension": ".png"}
    by_name = {item["name"]: item for item in ASSETS + [extra]}
    monkeypatch.setattr(media_choice, "find_asset",
                        lambda name, path=None: by_name.get(str(name).strip()))
    directives = {"stickers": ["666", "偷笑", "亲亲", "耶"], "emoji": "", "wishes": []}
    plan = media_choice.build_plan(None, directives, "笑死了", state=_state(),
                                   recent_media=[], allow_sticker=True)
    assert [b["name"] for b in plan["bubbles"] if b.get("type") == "sticker"] == [
        "666", "偷笑", "亲亲"]


def test_consecutive_media_caps_to_one():
    directives = {"stickers": ["666", "偷笑"], "emoji": "", "wishes": []}
    plan = media_choice.build_plan(None, directives, "笑死了", state=_state(),
                                   recent_media=[True, True], allow_sticker=True)
    stickers = [b for b in plan["bubbles"] if b.get("type") in ("sticker", "gif")]
    assert len(stickers) == 1


def test_crisis_and_low_mood_limits(monkeypatch):
    directives = {"stickers": ["666", "偷笑"], "emoji": "", "wishes": []}
    assert media_choice.build_plan(None, directives, "笑死了",
                                   state=_state(crisis=True),
                                   recent_media=[], allow_sticker=True) is None
    monkeypatch.setattr(media_choice, "_recent_sticker_names",
                        lambda path=None, now=None, days=None: set())
    plan = media_choice.build_plan(None, directives, "笑死了",
                                   state=_state(mood=0.2),
                                   recent_media=[], allow_sticker=True)
    assert plan is None  # 666/偷笑 都是搞笑标签，全被拦


def test_sticker_7day_dedup(monkeypatch):
    monkeypatch.setattr(media_choice, "_recent_sticker_names",
                        lambda path=None, now=None, days=None: {"666", "偷笑"})
    plan = media_choice.build_plan(None, {"stickers": ["666", "偷笑", "OK"], "emoji": "",
                                          "wishes": []},
                                   "看到一个段子，笑死了", state=_state(),
                                   recent_media=[], allow_sticker=True)
    assert plan is None or all(b.get("name") != "666" for b in plan["bubbles"])
    # OK 与文本无语义标签交集，但校验只按库/7天，不在 recent → 可以发
    plan = media_choice.build_plan(None, {"stickers": ["OK"], "emoji": "", "wishes": []},
                                   "好的", state=_state(), recent_media=[],
                                   allow_sticker=True)
    assert plan is not None and plan["bubbles"][1]["name"] == "OK"


def test_unknown_sticker_rejected_and_wish_recorded(tmp_path):
    plan = media_choice.build_plan(None, {"stickers": ["不存在"], "emoji": "", "wishes": []},
                                   "笑死了", state=_state(), recent_media=[],
                                   allow_sticker=True)
    assert plan is not None and plan["bubbles"] == []
    wishes = [json.loads(line) for line in
              (tmp_path / "wishes.jsonl").read_text(encoding="utf-8").splitlines()
              if line.strip()]
    assert wishes and "不存在" in wishes[0]["wish"]


def test_wish_recorded_but_not_sent(tmp_path):
    plan = media_choice.build_plan(
        None, {"stickers": [], "emoji": "", "wishes": ['想表达"无语但好笑"']},
        "太无语了", state=_state(), recent_media=[], allow_sticker=True)
    assert plan is not None and plan["bubbles"] == []
    assert plan["wishes"]
    entry = json.loads((tmp_path / "wishes.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert set(entry) == {"ts", "wish", "context", "source"}
    assert entry["wish"] == '想表达"无语但好笑"'


def test_prompt_materials_only_when_enabled(monkeypatch):
    fake = [{"name": "666", "path": "/tmp/666.png", "tags": ["好笑", "赞"],
             "extension": ".png"}]
    monkeypatch.setattr(media_choice, "list_assets", lambda path=None: fake)
    monkeypatch.setattr(media_choice, "ensure_indexed", lambda *a, **k: {"ok": True})
    block = media_choice.media_prompt_block(None)
    assert "666（好笑、赞）" in block
    assert "[[media:sticker:名字]]" in block
    assert "[[media:wish:" in block
    assert "最多 3 个" in block
    monkeypatch.setenv("MEDIA_ENABLED", "0")
    assert media_choice.media_prompt_block(None) == ""
    # 关闭时 proactive prompt 不含 media 段（基线锁由既有测试守护）
    prompt = proactive._kind_prompt("care", {"kind": "care"}, 0.55, 0.45, "用户:在吗")
    assert "可选媒体素材" not in prompt


def test_tick_parses_three_stickers(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ENABLED", "1")
    from agi.selflearn import db, trust
    path = tmp_path / "p.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", "周末去爬山了", time.time() - 3600))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}), time.time()))
    con.commit()
    con.close()
    trust.set_value(path, 0.6)

    class Fake:
        def available(self):
            return True

        def chat(self, messages, **kwargs):
            return {"ok": True, "text": "笑死了\n[[media:sticker:666]]\n"
                                        "[[media:sticker:偷笑]]\n[[media:sticker:亲亲]]"}

    old = proactive.build_candidates
    proactive.build_candidates = lambda p, n=None: [{
        "kind": "care", "text": "x", "reason": "t", "base": 0.5,
        "score": 0.6, "anchor": "", "topic": ""}]
    try:
        row = proactive.tick(path, client=Fake(), now=time.time(), cooldown=0,
                             min_score=0, respect_state=False)
        assert row["available"] is True
        assert row["media"] is not None
        names = [b.get("name") for b in row["media"]["bubbles"][1:] if b.get("name")]
        assert names[:3] == ["666", "偷笑", "亲亲"]
    finally:
        proactive.build_candidates = old


def test_worker_sends_multiple_media(monkeypatch):
    from agi.wechat_gateway import proactive_worker
    from agi.selflearn import ticker

    class FakeGateway:
        def __init__(self):
            self.cfg = {"proactive_enabled": True, "media_enabled": True,
                        "proactive_probability": 1.0, "allowed_contacts": ["好友A"],
                        "proactive_contacts": []}
            self.sent = []
            self.records = []
            self.last_turns = {}
            self.last_proactive = {}
            self.media_calls = []

        def _send(self, contact, text, **kw):
            self.sent.append((contact, text, kw))
            return {"ok": True, "status": "sent"}

        def send_bubbles(self, contact, items, **kw):
            self.media_calls.append((contact, items))
            return [{"ok": True, "status": "sent", "type": item.get("type")}
                    for item in items]

        def _record(self, kind, data):
            self.records.append((kind, data))

    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: {
        "ok": True, "ran": True, "gate": {"allowed": True},
        "proactive": {"ok": True, "available": True, "messages": ["笑死了"],
                      "message": "笑死了", "kind": "share", "turn_id": "T-1",
                      "media": {"bubbles": [
                          {"type": "text", "text": "笑死了"},
                          {"type": "sticker", "name": "666", "path": "/tmp/666.png"},
                          {"type": "sticker", "name": "偷笑", "path": "/tmp/偷笑.png"}],
                          "wishes": [], "reason": "model:sticker:666,model:sticker:偷笑"}}})
    g = FakeGateway()
    out = proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert out["sent"] == 1
    assert len(g.media_calls) == 1
    assert [item["name"] for item in g.media_calls[0][1]] == ["666", "偷笑"]
    assert out.get("media_sent") == 2


def test_acquisition_stub_reserved_no_network(monkeypatch, tmp_path):
    import agi.media_acquisition as acq

    def fail_socket(*args, **kwargs):
        raise AssertionError("try_acquire 不允许联网")

    monkeypatch.setattr(socket, "socket", fail_socket)
    monkeypatch.setattr(config, "MEDIA_ACQUISITION_ENABLED", True, raising=False)
    out = acq.try_acquire("无语但好笑")
    assert out["ok"] is False and out["status"] == "reserved"
    assert out["reason"] in ("manual_review_required", "acquisition_not_enabled")


def test_add_to_library_validation(monkeypatch, tmp_path):
    import agi.media_acquisition as acq

    monkeypatch.setattr(acq, "LIBRARY_DIR", tmp_path / "stickers")
    src = tmp_path / "x.txt"
    src.write_text("x", encoding="utf-8")
    assert acq.add_to_library(src, "x")["ok"] is False
    bad_size = tmp_path / "big.png"
    bad_size.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    assert acq.add_to_library(bad_size, "big")["ok"] is False
    good = tmp_path / "good.png"
    good.write_bytes(b"x")
    monkeypatch.setattr(acq.emoji_archive, "index_assets", lambda *a, **k: {"ok": True})
    out = acq.add_to_library(good, "good")
    assert out["ok"] is True and Path(out["path"]).exists()


def test_chat_parses_emoji_directive(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_ENABLED", "1")
    monkeypatch.setenv("MEDIA_STICKER_IN_REPLY", "0")
    media_choice._recent_sticker_names = lambda *a, **k: set()
    from agi.selflearn import chat as chat_module
    monkeypatch.setattr(chat_module, "_ensure_meaningful",
                        lambda reply, **kw: reply)
    from agi.selflearn.chat import SelfLearnAgent

    class Fake:
        def available(self):
            return True

        def chat(self, messages, **kwargs):
            if kwargs.get("purpose") != "selflearn_chat":
                return {"ok": True, "text": "[]"}
            return {"ok": True, "text": "晚上写东西挺安静的" + chr(10) + "[[media:emoji:😄]]"}

    agent = SelfLearnAgent(tmp_path / "chat.sqlite3", client=Fake(), rng=__import__("random").Random(1))
    row = agent.send("我喜欢晚上写东西")
    assert row["ok"] is True
    assert row["reply_media"] is not None
    assert "😄" in row["reply"]
    assert "[[media:" not in row["reply"]
