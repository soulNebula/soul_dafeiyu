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
        "proactive": {"ok": True, "available": True,
                      "messages": ["看到一个段子，笑死了"],
                      "message": "看到一个段子，笑死了", "kind": "share", "turn_id": "T-1",
                      "media": {"bubbles": [
                          {"type": "text", "text": "看到一个段子，笑死了"},
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


def test_text_media_quality_helpers():
    assert "meta_topic" in media_choice.text_media_issues("翻到一堆旧表情包，有点想笑", None)
    assert "sticker_ref_without_media" in media_choice.text_media_issues(
        "那个好了听着比表情包舒服", None)
    plan = {"bubbles": [{"type": "text", "text": "x"},
                        {"type": "sticker", "name": "666", "path": "/tmp/666.png"}]}
    assert "media_description_without_need" in media_choice.text_media_issues(
        "我要发个表情包了", plan)
    assert "polarity_conflict" in media_choice.text_media_issues(
        "我是不是特别容易被逗啊，有点烦，哈哈哈", None)
    clean = media_choice.sanitize_text_media("翻到一堆旧表情包，有点想笑", None)
    assert "表情包" not in clean
    clean = media_choice.sanitize_text_media("那个好了听着比表情包舒服", None)
    assert "表情包" not in clean
    clean = media_choice.sanitize_text_media("今天有点烦，哈哈哈", None)
    assert "哈哈" not in clean and "烦" in clean
    # 第一条没有具体事由 → 收口返回空，交给 tick 的兜底
    assert media_choice.sanitize_text_media("心里一下子轻了", None) == ""


def test_prompt_meta_topic_ban_only_when_enabled(monkeypatch):
    monkeypatch.setenv("MEDIA_ENABLED", "1")
    block = media_choice.media_prompt_block(None)
    assert "正文绝对不能出现" in block
    monkeypatch.setenv("MEDIA_ENABLED", "0")
    assert media_choice.media_prompt_block(None) == ""


def _tick_once(path, client, now):
    old = proactive.build_candidates
    proactive.build_candidates = lambda p, n=None: [{
        "kind": "care", "text": "x", "reason": "t", "base": 0.5,
        "score": 0.6, "anchor": "", "topic": ""}]
    try:
        return proactive.tick(path, client=client, now=now, cooldown=0,
                              min_score=0, respect_state=False)
    finally:
        proactive.build_candidates = old


def test_real_machine_four_cases_are_fixed(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ENABLED", "1")
    from agi.selflearn import db, trust
    path = tmp_path / "p6.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", "今天有点累", time.time() - 3600))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}), time.time()))
    con.commit()
    con.close()
    trust.set_value(path, 0.6)

    cases = [
        "我是不是特别容易被逗啊，有点烦，哈哈哈",
        "心里一下子轻了",
        "那个好了听着比表情包舒服",
        "翻到一堆旧表情包，有点想笑",
    ]

    class FixClient:
        def __init__(self):
            self.current = ""
            self.calls = 0

        def available(self):
            return True

        def chat(self, messages, **kwargs):
            self.calls += 1
            prompt = "\n".join(str(m.get("content") or "") for m in messages)
            if "【重生成】" in prompt:
                return {"ok": True, "text": "今天先这样，你也早点休息"}
            return {"ok": True, "text": self.current}

    client = FixClient()
    for index, raw in enumerate(cases):
        client.current = raw
        row = _tick_once(path, client, time.time())
        assert row["available"] is True
        final = str(row.get("message") or "")
        assert media_choice.text_media_issues(final, row["media"]) == []
        assert row["text_media_check"]["final_issues"] == []


def test_twenty_ticks_stats_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ENABLED", "1")
    from agi.selflearn import db, trust
    path = tmp_path / "p6b.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", "今天有点累", time.time() - 3600))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}), time.time()))
    con.commit()
    con.close()
    trust.set_value(path, 0.6)

    bad = ["我是不是特别容易被逗啊，有点烦，哈哈哈",
           "那个好了听着比表情包舒服", "翻到一堆旧表情包，有点想笑"]

    class Client:
        def __init__(self):
            self.count = 0

        def available(self):
            return True

        def chat(self, messages, **kwargs):
            prompt = "\n".join(str(m.get("content") or "") for m in messages)
            if "【重生成】" in prompt:
                return {"ok": True, "text": "今天先这样，你也早点休息"}
            return {"ok": True, "text": bad[self.count % len(bad)]}

    client = Client()
    stats = {"meta": 0, "polarity": 0, "inconsistent": 0, "inner_monologue": 0}
    for index in range(20):
        client.count = index
        row = _tick_once(path, client, time.time())
        text = str(row.get("message") or "")
        issues = media_choice.text_media_issues(text, row["media"])
        stats["meta"] += int("meta_topic" in issues)
        stats["polarity"] += int("polarity_conflict" in issues)
        stats["inconsistent"] += int("sticker_ref_without_media" in issues)
        stats["inner_monologue"] += int("inner_monologue" in issues)
    assert stats == {"meta": 0, "polarity": 0, "inconsistent": 0,
                     "inner_monologue": 0}


def test_inner_monologue_blacklist_and_replacements(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ENABLED", "1")
    from agi.selflearn import db, trust
    path = tmp_path / "p6c.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", "今天有点累", time.time() - 3600))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}), time.time()))
    con.commit()
    con.close()
    trust.set_value(path, 0.6)

    bad_cases = [
        "心里一下子轻了",
        "心里暖暖的",
        "悬着的心终于放下了",
        "心头一热，差点没绷住",
        "眼眶有点热，说不出什么滋味",
        "百感交集，有点想哭",
    ]
    for text in bad_cases:
        assert "inner_monologue" in media_choice.text_media_issues(text, None)

    class FixClient:
        def __init__(self):
            self.current = ""
            self.calls = 0

        def available(self):
            return True

        def chat(self, messages, **kwargs):
            self.calls += 1
            prompt = "\n".join(str(m.get("content") or "") for m in messages)
            if "【重生成】" in prompt:
                return {"ok": True, "text": "终于弄完了，舒服"}
            return {"ok": True, "text": self.current}

    client = FixClient()
    for text in bad_cases:
        client.current = text
        row = _tick_once(path, client, time.time())
        final = str(row.get("message") or "")
        assert not media_choice.INNER_MONOLOGUE_RE.search(final), (text, final)
        assert row["text_media_check"]["final_issues"] == []


def test_text_media_consistency_media_off(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ENABLED", "0")
    from agi.selflearn import db, trust
    path = tmp_path / "p6off.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", "今天有点累", time.time() - 3600))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}), time.time()))
    con.commit()
    con.close()
    trust.set_value(path, 0.6)

    class Client:
        def __init__(self):
            self.calls = 0

        def available(self):
            return True

        def chat(self, messages, **kwargs):
            self.calls += 1
            prompt = "\n".join(str(m.get("content") or "") for m in messages)
            if "【重生成】" in prompt:
                return {"ok": True, "text": "今天先这样，你也早点休息"}
            assert "可选媒体素材" not in prompt  # 关闭时不注入素材
            return {"ok": True, "text": "那个好了听着比表情包舒服"}

    row = _tick_once(path, Client(), time.time())
    final = str(row.get("message") or "")
    assert "表情包" not in final and "那个好了" not in final
    assert row["text_media_check"]["final_issues"] == []
