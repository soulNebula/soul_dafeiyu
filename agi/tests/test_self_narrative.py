# -*- coding: utf-8 -*-
"""P32 自我叙事测试：周记→叙事→core 层、归档、幂等、注入空串。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from agi.selflearn import db, persona_layers, self_narrative
from tools.console import growth_api


class FakeClient:
    """模拟 DeepSeek：周记 300+ 字，叙事 ≤120 字。"""

    def __init__(self):
        self.calls = []

    def chat(self, messages, **kwargs):
        purpose = kwargs.get("purpose", "")
        self.calls.append(purpose)
        if purpose == "self_narrative_weekly":
            text = ("这周我一直在想，怎么把话说得像自己一点。"
                    "有人跟我聊到深夜，我接住了几句，也有几句太生硬被指出来。"
                    "我开始记住对方喜欢什么、不喜欢什么，也记住自己哪里容易急。"
                    "下周我想先听完再回，不急着解释，也不把每句话都说得像结论。"
                    "我还是在学，但这是我自己的选择，不是照着谁的样子演。")
            return {"ok": True, "text": text + text[:80]}
        return {"ok": True, "text": "我是一个还在学着接住别人的 AI，记得住对方的语气，也在改自己生硬的地方。"}


class FailingClient:
    def chat(self, messages, **kwargs):
        raise RuntimeError("offline")


def _base(tmp_path):
    base = tmp_path / "selflearn.sqlite3"
    con = db.connect(base)
    con.close()
    return base


def _run(tmp_path, base, client):
    return self_narrative.weekly_review(
        db_path=base, now=datetime(2026, 10, 27, 23, 30), client=client,
        weekly_dir=tmp_path / "weekly", history_path=tmp_path / "history.jsonl",
        diary_dir=tmp_path / "diary", daily_dir=tmp_path / "daily")


def test_weekly_review_writes_core_and_is_idempotent(tmp_path):
    base = _base(tmp_path)
    old = persona_layers.observe(base, "self_narrative", "旧叙事", layer="core")
    persona_layers.approve(base, old["id"], "old_test")
    client = FakeClient()
    result = _run(tmp_path, base, client)
    assert result["ok"] is True and result["skipped"] is False
    assert result["changed"] is True
    assert len(result["narrative"]) <= 120
    assert Path(result["diary_path"]).is_file()
    assert "## 自我叙事" in Path(result["diary_path"]).read_text(encoding="utf-8")
    # 归档旧值
    rows = [json.loads(line) for line in
            (tmp_path / "history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows and rows[0]["old"] == "旧叙事"
    # core 层已 active、approved_by=weekly_review
    current = self_narrative.current(db_path=base)
    assert current["found"] is True
    assert current["text"] == result["narrative"]
    assert current["approved_by"] == "weekly_review"
    assert self_narrative.inject_line(db_path=base) == result["narrative"]
    # 幂等：同周再次调用跳过，不重复写历史
    again = _run(tmp_path, base, client)
    assert again["ok"] is True and again["skipped"] is True
    assert len((tmp_path / "history.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_inject_line_empty_and_history(tmp_path):
    base = _base(tmp_path)
    assert self_narrative.inject_line(db_path=base) == ""
    assert self_narrative.current(db_path=base)["found"] is False
    assert self_narrative.history(path=tmp_path / "none.jsonl") == []


def test_llm_failure_falls_back_and_still_writes(tmp_path):
    base = _base(tmp_path)
    result = _run(tmp_path, base, FailingClient())
    assert result["ok"] is True
    assert result["llm"] == "fallback"
    assert result["narrative"]
    assert result["weekly"]
    assert Path(result["diary_path"]).is_file()


def test_maybe_weekly_due_window_and_idempotent(tmp_path):
    weekly = tmp_path / "weekly"
    before = self_narrative.maybe_weekly(
        now=datetime(2026, 11, 1, 23, 29), weekly_dir=weekly)
    assert before["should_run"] is False and before["reason"] == "not_due"
    due = self_narrative.maybe_weekly(
        now=datetime(2026, 11, 1, 23, 30), weekly_dir=weekly)
    assert due["should_run"] is True and due["reason"] == "due"
    # 文件已存在 → 本周不再跑
    weekly.mkdir(exist_ok=True)
    (weekly / f"{due['week']}.md").write_text("# 周记\n", encoding="utf-8")
    again = self_narrative.maybe_weekly(
        now=datetime(2026, 11, 1, 23, 40), weekly_dir=weekly)
    assert again["should_run"] is False and again["reason"] == "already_ran"
    disabled = self_narrative.maybe_weekly(
        now=datetime(2026, 11, 1, 23, 40), weekly_dir=weekly, enabled=False)
    assert disabled["should_run"] is False and disabled["reason"] == "disabled"


def test_growth_api_helpers(tmp_path):
    base = _base(tmp_path)
    result = _run(tmp_path, base, FakeClient())
    assert result["ok"] is True
    narrative = growth_api.growth_narrative(
        db_path=base, history_path=tmp_path / "history.jsonl")
    assert narrative["ok"] is True and narrative["text"] == result["narrative"]
    assert narrative["approved_by"] == "weekly_review"
    status = growth_api.growth_status(
        enabled=False, now=datetime(2026, 10, 27, 23, 30),
        db_path=base, weekly_dir=tmp_path / "weekly",
        history_path=tmp_path / "history.jsonl")
    assert status["ok"] is True and status["narrative_available"] is True
    assert status["weeks"] == 1 and status["last_week"]
    assert status["schedule"]["should_run"] is False


def test_collect_week_includes_interests_and_negative(tmp_path):
    base = _base(tmp_path)
    con = db.connect(base)
    con.execute("insert into interests values (?,?,?,?,?,?,?,?,?)",
                ("I-1", "ai", 0.8, "chat", "[]", 3, 1.0, 1.0, 1.0))
    con.execute("insert into inner_states values (?,?,?,?,?,?,?,?,?,?)",
                ("S-1", "T-1", "model",
                 json.dumps({"mood": 0.7, "stage": "熟悉"}), "{}", 0, 100,
                 0, "local", datetime(2026, 10, 27, 12).timestamp()))
    con.commit()
    con.close()
    daily = tmp_path / "daily"
    daily.mkdir()
    day = datetime(2026, 10, 27).strftime("%Y-%m-%d")
    (daily / f"{day}.jsonl").write_text(json.dumps({
        "messages": [{"role": "system", "content": "s"},
                     {"role": "user", "content": "好生硬"}],
        "response": "好的", "meta": {"quality": "negative", "reason": "user_negative"}},
        ensure_ascii=False) + "\n", encoding="utf-8")
    stats = self_narrative.collect_week(
        db_path=base, now=datetime(2026, 10, 27, 23, 30),
        diary_dir=tmp_path / "diary", daily_dir=daily)
    assert stats["interests"][0]["topic"] == "ai"
    assert stats["mood_avg"] == 0.7
    assert stats["negative"]["count"] == 1
