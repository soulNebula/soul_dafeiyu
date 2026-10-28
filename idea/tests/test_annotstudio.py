# -*- coding: utf-8 -*-
"""用户标注后端测试：预填建议 / 校验收敛 / corpus & JSONL 数据源 / soul JSONL 导出。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import annotstudio


@pytest.fixture
def env(tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    soul = tmp_path / "SoulAgent"
    data = tmp_path / "idea" / ".data"
    data.mkdir(parents=True)
    monkeypatch.setattr(annotstudio.paths, "agent_home", lambda: agent)
    monkeypatch.setattr(annotstudio.paths, "soulagent_home", lambda: soul)
    monkeypatch.setattr(annotstudio.paths, "DATA_DIR", data)
    audits: list[dict] = []
    monkeypatch.setattr(
        annotstudio.audit, "record",
        lambda action, target, **kw: audits.append(
            {"action": action, "target": target, **kw}))
    annotstudio._SCHEMA_CACHE.clear()
    return {"agent": agent, "soul": soul, "data": data, "audits": audits}


def _items():
    return [
        {"id": "i001", "role": "you", "text": "今天好累啊，工作一堆"},
        {"id": "i002", "role": "me", "text": "嗯，听着就累。先歇会儿？"},
        {"id": "i003", "role": "you", "text": "你说我该不该换工作"},
        {"id": "i004", "role": "me", "text": "先别急着做决定，你可以试试把现在最烦的三件事写下来，"
                                     "看看哪些是能立刻改的。"},
        {"id": "i005", "role": "you", "text": "行，我试试"},
        {"id": "i006", "role": "me", "text": "好，写完可以发我看看。"},
    ]


def _target(**overrides):
    target = {
        "mood": 0.55, "energy": 0.6, "sharing": 0.45, "battery": 0.7,
        "trust": 0.5, "stage": "初识", "stance": "平和", "intent": "自然接话",
        "monologue": "正常聊就好",
        "behavior": {"length": "normal", "ask": True,
                     "advice": False, "emoji": "不用"},
    }
    target.update(overrides)
    return target


def _annotations(ids_scores=None):
    out = []
    for item_id, score in (ids_scores or [("i002", 5), ("i004", 4), ("i006", 1)]):
        out.append({"item_id": item_id, "target": _target(),
                    "score": score, "tags": ["验收"], "note": f"note-{item_id}"})
    return out


# --------------------------------------------------------------------------- #
# suggest_annotation
# --------------------------------------------------------------------------- #
def test_suggest_short_question_low_mood():
    result = annotstudio.suggest_annotation("在吗？", prev_user_text="今天好累啊")
    target = result["target"]
    assert set(target) == set(annotstudio.TARGET_FIELDS)
    assert target["behavior"]["length"] == "short"      # 4 字
    assert target["behavior"]["ask"] is True            # 结尾问号
    assert target["behavior"]["emoji"] == "不用"
    assert target["mood"] < 0.55                        # 上一句负面词
    assert target["stance"] in annotstudio.STANCES
    assert result["reasons"]                             # UI 要展示依据


def test_suggest_long_emoji_and_crisis():
    long_reply = ("谢谢你跟我说这些，我觉得你已经做得很好了😊。"
                  "如果现在实在不想动，就先把手边最急的一件小事做完，"
                  "剩下的明天再说，别一个人硬扛着。")
    target = annotstudio.suggest_annotation(long_reply)["target"]
    assert target["behavior"]["length"] == "long"       # >45 字
    assert target["behavior"]["emoji"] == "偶尔"        # 有 emoji
    assert target["energy"] > 0.6

    crisis = annotstudio.suggest_annotation("我不想活了，真的撑不住")["target"]
    assert crisis["stance"] == "先照顾人"
    assert crisis["intent"] == "先确认对方安全"
    assert crisis["behavior"] == {"length": "short", "ask": False,
                                  "advice": False, "emoji": "不用"}
    assert crisis["mood"] <= 0.3


# --------------------------------------------------------------------------- #
# validate_annotation
# --------------------------------------------------------------------------- #
def test_validate_clamps_and_converges_enums():
    checked = annotstudio.validate_annotation(_target(
        mood=1.8, energy=-0.4, stage="朋友", stance="x" * 40,
        intent="y" * 80, monologue="z" * 60,
        behavior={"length": "medium", "ask": "yes", "advice": 1, "emoji": "rare",
                  "extra": 1}))
    assert checked["ok"] is True
    target = checked["target"]
    assert target["mood"] == 1.0 and target["energy"] == 0.0
    assert target["stage"] == "初识"
    assert target["behavior"]["length"] == "normal"
    assert target["behavior"]["emoji"] == "不用"
    assert target["behavior"]["ask"] is True and target["behavior"]["advice"] is True
    assert len(target["stance"]) <= 30
    assert len(target["intent"]) <= 60
    assert len(target["monologue"]) <= 40
    assert checked["warnings"]


def test_validate_rejects_unknown_and_bad_types():
    bad = annotstudio.validate_annotation(_target(foo="bar"))
    assert bad["ok"] is False and any("未知字段" in e for e in bad["errors"])

    missing = _target()
    missing.pop("behavior")
    bad2 = annotstudio.validate_annotation(missing)
    assert bad2["ok"] is False and any("behavior" in e for e in bad2["errors"])
    assert bad2["target"]["behavior"]["length"] in ("short", "normal", "long")

    bad3 = annotstudio.validate_annotation(_target(mood="high"))
    assert bad3["ok"] is False and any("mood" in e for e in bad3["errors"])

    not_dict = annotstudio.validate_annotation("nope")
    assert not_dict["ok"] is False


def test_validate_uses_soul_schema_enums_when_available(env):
    schema = {
        "properties": {
            "stage": {"enum": ["甲", "乙"]},
            "behavior": {"properties": {
                "length": {"enum": ["short", "long"]},
                "emoji": {"enum": ["偶尔", "不用"]},
            }},
        }
    }
    schema_path = env["soul"] / annotstudio.SCHEMA_REL
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    annotstudio._SCHEMA_CACHE.clear()

    ok = annotstudio.validate_annotation(_target(stage="甲"))
    assert ok["ok"] is True and ok["target"]["stage"] == "甲"
    caught = annotstudio.validate_annotation(_target(stage="初识"))
    assert caught["target"]["stage"] == "初识" or caught["warnings"]  # 不在自定义枚举
    annotstudio._SCHEMA_CACHE.clear()


# --------------------------------------------------------------------------- #
# save：内存 items / JSONL / corpus
# --------------------------------------------------------------------------- #
def test_save_from_items_builds_soul_rows(env):
    result = annotstudio.save_annotations(_items(), _annotations(), window=2,
                                          out_name="soul_demo", min_score=3, split=0.5)
    assert result["ok"] is True
    assert result["rows"] == 1 and result["val_rows"] == 1
    assert result["skipped"] == 1                       # i006 score=1 < 3
    out = Path(result["out"])
    val = Path(result["val_out"])
    assert out.parent == env["agent"] / "datasets"
    assert out.is_file() and val.is_file()

    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line]
    val_rows = [json.loads(line) for line in val.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) + len(val_rows) == 2
    row = rows[0]
    assert row["user_msg"] == "今天好累啊，工作一堆"
    assert 0 < len(row["context"]) <= 2
    assert all(turn["role"] in ("user", "assistant") for turn in row["context"])
    assert row["target"]["behavior"]["length"] == "normal"  # 标注里给的 target
    assert row["meta"]["score"] == 5 and row["meta"]["tags"] == ["验收"]
    assert env["audits"] and env["audits"][-1]["action"] == "annot.save"


def test_save_window_and_system_context(env):
    result = annotstudio.save_annotations(
        _items(), [{"item_id": "i006", "target": _target(), "score": 5}],
        window=3, out_name="soul_window", split=0.0, system="你是小鱼")
    assert result["ok"] is True and result["rows"] == 1
    row = json.loads(Path(result["out"]).read_text(encoding="utf-8").splitlines()[0])
    assert row["context"][0] == {"role": "system", "text": "你是小鱼"}
    # i006 前 3 轮：i003(you) i004(me) i005(you)
    assert [turn["role"] for turn in row["context"][1:]] == ["user", "assistant", "user"]
    assert row["user_msg"] == "行，我试试"


def test_save_from_jsonl_source(env, tmp_path):
    path = tmp_path / "chat.jsonl"
    path.write_text("\n".join([
        json.dumps({"messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "在，怎么了？"},
            {"role": "user", "content": "想聊聊"},
            {"role": "assistant", "content": "嗯，我在听。"}]}, ensure_ascii=False),
    ]), encoding="utf-8")
    result = annotstudio.save_annotations(
        str(path), [{"item_id": "i002", "target": _target(), "score": 4},
                    {"item_id": "i004", "target": _target(mood=0.4), "score": 2}],
        out_name="soul_jsonl", min_score=2, split=0.0)
    assert result["ok"] is True
    assert result["rows"] == 2
    rows = [json.loads(line) for line in Path(result["out"])
            .read_text(encoding="utf-8").splitlines() if line]
    assert rows[0]["user_msg"] == "你好"
    assert rows[1]["user_msg"] == "想聊聊"


def test_save_empty_and_score_filtered(env):
    empty = annotstudio.save_annotations(_items(), [], out_name="none")
    assert empty["ok"] is False and empty["error"] == "empty_annotations"
    assert not (env["agent"] / "datasets" / "none.jsonl").exists()

    filtered = annotstudio.save_annotations(_items(), _annotations(), min_score=9)
    assert filtered["ok"] is False and filtered["error"] == "empty_annotations"
    assert filtered["skipped"] == 3


def test_save_tiny_split_keeps_one_val_row(env):
    """2 行 + split=0.1 时 round(0.2)=0，验证集不能为空。"""
    result = annotstudio.save_annotations(
        _items(), [{"item_id": "i002", "target": _target(), "score": 5},
                   {"item_id": "i004", "target": _target(), "score": 5}],
        out_name="soul_tiny", min_score=1, split=0.1)
    assert result["ok"] is True
    assert result["rows"] == 1 and result["val_rows"] == 1
    assert Path(result["val_out"]).read_text(encoding="utf-8").strip()


def test_save_invalid_out_name(env):
    result = annotstudio.save_annotations(
        _items(), [{"item_id": "i002", "target": _target(), "score": 5}],
        out_name="../evil")
    assert result["ok"] is False and result["error"] == "invalid_out_name"


def test_save_corpus_backend_not_ready(env):
    result = annotstudio.save_annotations("no_such_job", _annotations())
    assert result["ok"] is False and result["error"] == "corpus_backend_not_ready"
    view = annotstudio.source_view("no_such_job")
    assert view["ok"] is False and view["error"] == "corpus_backend_not_ready"
    bad = annotstudio.load_corpus_items("../../etc")
    assert bad[0] is None and bad[1] == "invalid_job_id"


# --------------------------------------------------------------------------- #
# source_view（GET /api/wb/annot/source/{job_id} 的返回契约）
# --------------------------------------------------------------------------- #
def test_source_view_from_items_cache(env):
    job_dir = env["data"] / "corpus" / "job-1"
    job_dir.mkdir(parents=True)
    (job_dir / "items.json").write_text(json.dumps(
        {"items": _items()}, ensure_ascii=False), encoding="utf-8")

    view = annotstudio.source_view("job-1", window=2)
    assert view["ok"] is True and view["total"] == 6
    assert len(view["items"]) == 3                     # 只有 me 回复需要标注
    first = view["items"][0]
    assert first["id"] == "i002" and first["role"] == "me"
    assert first["prev_user_text"] == "今天好累啊，工作一堆"
    assert first["suggestion"]["target"]["behavior"]["length"] == "short"
    assert first["context"][0]["role"] == "user"
    assert all(item["suggestion"]["reasons"] for item in view["items"])


def test_source_view_uses_corpusstudio_when_available(env, monkeypatch):
    class FakeCorpus:
        @staticmethod
        def load_items(job_id):
            assert job_id == "job-x"
            return _items()

    monkeypatch.setattr(annotstudio, "corpusstudio", None, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "app.corpusstudio", FakeCorpus)
    view = annotstudio.source_view("job-x")
    assert view["ok"] is True and len(view["items"]) == 3
