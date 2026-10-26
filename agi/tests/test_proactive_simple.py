# -*- coding: utf-8 -*-
"""P6-e：simple 主动链路（B 方案）与 legacy 可回滚。"""
from __future__ import annotations

import json
import time

import pytest

from agi.selflearn import db, proactive, simple_tick, trust

T0 = time.mktime((2026, 10, 26, 10, 0, 0, 0, 0, -1))


class FakeClient:
    def __init__(self, queue: list[str] | None = None, text: str = ""):
        self.queue = list(queue or [])
        self.text = text
        self.prompts: list[str] = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.prompts.append(str(messages[-1].get("content") or ""))
        if self.queue:
            return {"ok": True, "text": self.queue.pop(0)}
        return {"ok": True, "text": self.text}


def _seed(tmp_path, name="s.sqlite3", recent_proactive: str = ""):
    path = tmp_path / name
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", "今天有点累", T0 - 3600))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}), T0))
    if recent_proactive:
        con.execute("INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    ("P-prev", "care", recent_proactive, "test", 0.6, "spoken", T0 - 120))
    con.commit()
    con.close()
    trust.set_value(path, 0.6)
    return path


@pytest.fixture(autouse=True)
def _simple_env(monkeypatch):
    monkeypatch.setenv("PROACTIVE_STYLE", "simple")
    monkeypatch.setattr(proactive, "build_candidates", lambda p, now=None: [{
        "kind": "care", "text": "兜底", "reason": "test", "base": 0.55,
        "score": 0.6, "anchor": "", "topic": "喜欢喝手冲咖啡",
    }])
    monkeypatch.setattr(proactive.initiative, "decide", lambda *a, **k: {})
    yield


def _tick(path, client, now=T0):
    return proactive.tick(path, client=client, now=now, cooldown=0,
                          min_score=0, respect_state=False)


def test_simple_valid_output_is_byte_identical(tmp_path):
    path = _seed(tmp_path)
    raw = "看到一个说法，挺有意思"
    row = _tick(path, FakeClient(text=raw))
    assert row["available"] is True
    assert row["style"] == "simple"
    assert row["message"] == raw
    assert row["messages"] == [raw]
    assert row["discard_reason"] == ""


def test_simple_100_chars_is_regenerated_then_used(tmp_path):
    path = _seed(tmp_path)
    long_text = "看到一个说法" + "很" * 100
    row = _tick(path, FakeClient(queue=[long_text, "看到一个说法，挺有意思"]))
    assert row["available"] is True
    assert row["message"] == "看到一个说法，挺有意思"
    assert "too_long" in row["regen_reason"]


@pytest.mark.parametrize("bad,reason", [
    ("我刚看到一个表情包", "meta_topic"),
    ("我下楼买菜，顺便走了一圈", "physical_detail"),
    ("上次你说你喜欢我，我一直记得", "fabrication"),
    ("心里一下子轻了", "inner_monologue"),
    ("今天有点烦，哈哈哈", "polarity_conflict"),
    ("嗯", "empty_or_too_short"),
    ("那个贴纸挺好", "sticker_ref_without_media"),
])
def test_simple_violation_regenerates_then_recovers(tmp_path, bad, reason):
    path = _seed(tmp_path)
    row = _tick(path, FakeClient(queue=[bad, "看到一个说法，挺有意思"]))
    assert row["available"] is True
    assert reason in row["regen_reason"], row
    assert row["message"] == "看到一个说法，挺有意思"


def test_simple_still_violation_discards(tmp_path):
    path = _seed(tmp_path)
    row = _tick(path, FakeClient(queue=["我刚看到一个表情包",
                                        "我又看到一个素材清单"]))
    assert row["available"] is False
    assert row["reason"] == "discarded"
    assert "meta_topic" in row["discard_reason"]
    assert row["media"] is None


def test_simple_duplicate_recent_is_discarded(tmp_path):
    path = _seed(tmp_path, recent_proactive="看到一个说法，挺有意思")
    row = _tick(path, FakeClient(queue=["看到一个说法，挺有意思",
                                        "看到一个说法，挺有意思"]))
    assert row["available"] is False
    assert "duplicate_recent" in row["discard_reason"]


def test_simple_does_not_call_legacy_postprocessors(tmp_path, monkeypatch):
    path = _seed(tmp_path)

    def boom(*args, **kwargs):
        raise AssertionError("simple 不应调用 legacy 后处理")

    for name in ("choose_opener_mode", "vary_opener", "apply_oral_particle",
                 "limit_voice_length", "ensure_concrete_subject",
                 "clean_reaction_text", "_continuity_postprocess",
                 "dedupe_opener_prefixes", "collapse_leading_prefixes"):
        monkeypatch.setattr(proactive, name, boom)
    monkeypatch.setattr(proactive.style_profile, "tune_reply", boom)
    row = _tick(path, FakeClient(text="看到一个说法，挺有意思"))
    assert row["available"] is True
    assert row["message"] == "看到一个说法，挺有意思"


def test_legacy_style_remains_default_and_dispatch(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    monkeypatch.setattr(simple_tick, "WECHAT_CONFIG", tmp_path / "missing.json")
    monkeypatch.delenv("PROACTIVE_STYLE", raising=False)
    assert simple_tick.proactive_style(path) == "legacy"
    row = proactive.tick(path, client=FakeClient(text="看到一个说法，挺有意思"),
                         now=T0, cooldown=0, min_score=0, respect_state=False)
    assert row["available"] in (True, False)
    assert row.get("style") != "simple"


def test_config_file_simple_switch(tmp_path, monkeypatch):
    cfg = tmp_path / "wechat.json"
    cfg.write_text(json.dumps({"proactive_style": "simple"}), encoding="utf-8")
    monkeypatch.setattr(simple_tick, "WECHAT_CONFIG", cfg)
    monkeypatch.delenv("PROACTIVE_STYLE", raising=False)
    assert simple_tick.proactive_style(None) == "simple"


def test_twenty_tick_stats(tmp_path):
    path = _seed(tmp_path)
    texts = [
        "看到一个说法，琢磨了半天觉得挺有意思",
        "刚把一段代码跑通，花了半天时间挺有成就感",
        "今天效率一般，对着屏幕有点着急",
        "翻到以前存的一篇文章，看完有点感慨",
        "那批资料终于归档了，整理完轻松一点",
        "收藏夹里还有一堆没看的，慢慢来吧",
        "刚才试着换了个方法，居然一次就成了",
        "今天学到一个小技巧，感觉还挺实用的",
        "看到一个很好笑的段子，忍不住笑了一下",
        "刚整理完一批旧笔记，脑子有点累",
        "想起以前记下的一个笔记，现在看有点感慨",
        "这会儿在等文件跑完，坐着有点无聊",
        "今天状态还行，想多聊两句最近的事",
        "刚看到一个挺有意思的观点，想了一会儿",
        "整理完资料，脑子终于清楚了一点",
        "今天有点忙，但整体还算顺利",
        "看到一个挺实用的小方法，记了下来",
        "刚把收藏夹清了一半，感觉挺爽的",
        "想起一个旧问题，今天想明白了一点",
        "今天节奏比较慢，倒也还好",
    ]
    client = FakeClient(queue=texts)
    rows = []
    for index in range(20):
        row = _tick(path, client, now=T0 + index * 180)
        if row["available"]:
            rows.append(row)
    assert len(rows) >= 17
    messages = [row["message"] for row in rows]
    assert len(set(messages)) == len(messages)
    avg = sum(len(text) for text in messages) / len(messages)
    assert 15 <= avg <= 30, avg
    assert not any(len(text) < 6 for text in messages)
    assert not any("表情包" in text or "素材" in text for text in messages)


def test_worker_records_proactive_discarded(monkeypatch):
    from agi.wechat_gateway import proactive_worker
    from agi.selflearn import ticker

    class FakeGateway:
        def __init__(self):
            self.cfg = {"proactive_enabled": True, "proactive_probability": 1.0,
                        "allowed_contacts": ["好友A"]}
            self.records = []
            self.last_turns = {}
            self.last_proactive = {}

        def _record(self, kind, data):
            self.records.append((kind, data))

    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: {
        "ok": True, "ran": True, "gate": {"allowed": True},
        "proactive": {"ok": True, "available": False, "reason": "discarded",
                      "style": "simple", "discard_reason": "meta_topic",
                      "regen_reason": "meta_topic"}})
    gateway = FakeGateway()
    proactive_worker.run_cycle(gateway, client=object(), now=100.0)
    assert any(kind == "proactive_discarded" for kind, _ in gateway.records)


def test_light_polish_idempotent():
    assert simple_tick._light_polish("第一句。第二句。") == "第一句，第二句"
    assert simple_tick._light_polish("连续，，标点。") == "连续，标点"
    assert simple_tick._light_polish("甲。乙。丙。") == "甲，乙，丙"
    for raw in ("第一句。第二句。", "连续，，标点。", "没有标点"):
        once = simple_tick._light_polish(raw)
        assert simple_tick._light_polish(once) == once


def test_real_bracket_sentence_is_regenerated(tmp_path):
    path = _seed(tmp_path)
    raw = "（放下手里在看的资料）控制饮食确实不容易 饿的时候我就想 忍过这阵子身体会轻快些"
    row = _tick(path, FakeClient(queue=[raw, "控制饮食确实不容易，忍过这阵子就好"]))
    assert row["available"] is True
    assert "bracket_action" in row["regen_reason"]
    assert not simple_tick.BRACKET_RE.search(row["message"])


def test_real_silence_comment_is_regenerated(tmp_path):
    path = _seed(tmp_path)
    raw = ("刚在整理几段笔记，忽然在想你刚才没接话，是不是觉得我说得太绕了。"
           "不是抱怨，就是有点在意")
    row = _tick(path, FakeClient(queue=[raw, "刚整理完笔记，有点在意刚才那两句"]))
    assert row["available"] is True
    assert "silence_comment" in row["regen_reason"]
    assert not simple_tick.SILENCE_RE.search(row["message"])


@pytest.mark.parametrize("bad", [
    "（放下手里的资料）控制饮食不容易",
    "【旁白】今天有点累",
    "*笑* 今天有点累",
    "他放下杯子，说今天有点累",
    "（他抬头）今天有点累",
])
def test_bracket_forms_are_blocked(tmp_path, bad):
    path = _seed(tmp_path)
    row = _tick(path, FakeClient(queue=[bad, "今天有点累，想早点休息"]))
    assert row["available"] is True
    assert "bracket_action" in row["regen_reason"]


@pytest.mark.parametrize("bad", [
    "你刚才没接话",
    "你都不理我",
    "怎么不回",
    "在忙吗",
    "冒个泡",
    "好久没消息",
])
def test_silence_forms_are_blocked(tmp_path, bad):
    path = _seed(tmp_path)
    row = _tick(path, FakeClient(queue=[bad, "今天有点累，想早点休息"]))
    assert row["available"] is True
    assert "silence_comment" in row["regen_reason"]


def test_bracket_still_bad_after_regen_is_discarded(tmp_path):
    path = _seed(tmp_path)
    row = _tick(path, FakeClient(queue=["（笑）今天有点累", "【旁白】今天有点累"]))
    assert row["available"] is False
    assert "bracket_action" in row["discard_reason"]


def test_tick_returns_topic_seed_and_switch_off(tmp_path, monkeypatch):
    path = _seed(tmp_path, name="topic.sqlite3")
    con = db.connect(path)
    con.execute(
        "INSERT OR REPLACE INTO user_memories"
        "(id,scope,kind,content,evidence,confidence,status,created_at,importance) "
        "VALUES('UM-T','user','preference','喜欢喝手冲咖啡','原话',0.9,'active',?,0.9)",
        (T0 - 3600,))
    con.commit()
    con.close()
    client = FakeClient(text="看到一个说法，挺有意思")
    row = _tick(path, client)
    assert row["available"] is True
    assert row["topic_seed"]["candidates"]
    assert any("可自然提起的旧话题" in prompt for prompt in client.prompts)

    monkeypatch.setenv("PROACTIVE_TOPIC_SEED", "0")
    path2 = _seed(tmp_path, name="topic2.sqlite3")
    con = db.connect(path2)
    con.execute(
        "INSERT OR REPLACE INTO user_memories"
        "(id,scope,kind,content,evidence,confidence,status,created_at,importance) "
        "VALUES('UM-T2','user','preference','喜欢喝手冲咖啡','原话',0.9,'active',?,0.9)",
        (T0 - 3600,))
    con.commit()
    con.close()
    client2 = FakeClient(text="看到一个说法，挺有意思")
    row2 = _tick(path2, client2)
    assert row2["available"] is True
    assert row2["topic_seed"]["reason"] == "disabled"
    assert not any("可自然提起的旧话题" in prompt for prompt in client2.prompts)
