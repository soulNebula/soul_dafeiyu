# -*- coding: utf-8 -*-
"""P1-c：月度 gist（90 天～1 年区间，从原文重建，禁止摘要的摘要）。

- 只读该月原始 messages，绝不读 summaries 表；
- 分批总结（≤120 条/批）再合并成 ≤12 行 / ≤400 字；
- 当月高重要稳定事实（value/preference/plan & importance≥0.6）原样进「确定的事」；
- 幂等：同 year_month UPDATE 同一行；nightly 每月最多一次、最多补最近 3 个月。
"""
from __future__ import annotations

import datetime
import json

import pytest

from agi.selflearn import db, nightly, summary

DAY = 86400.0


def _ts(*args) -> float:
    return datetime.datetime(*args).timestamp()


def _seed_messages(path, year: int, month: int, count: int, *, prefix: str = "消息"):
    con = db.connect(path)
    start = _ts(year, month, 1, 0, 0)
    for i in range(count):
        con.execute(
            "INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
            (f"M-{year}{month:02d}-{i}", "user" if i % 2 == 0 else "assistant",
             f"{prefix}{i} 今天聊了面试和搬家的事", start + i * 60))
    con.commit()
    con.close()


def _seed_memory(path, mid: str, content: str, *, kind: str, importance: float,
                 created: float):
    con = db.connect(path)
    con.execute(
        "INSERT INTO user_memories(id,scope,kind,content,evidence,evidence_message_id,"
        "confidence,status,conflict_group,created_at,importance,access_count,"
        "last_accessed,evidence_history) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, "user", kind, content, content, None, 0.9, "active", None,
         created, importance, 0, 0.0, json.dumps([content], ensure_ascii=False)))
    con.commit()
    con.close()


class BatchClient:
    """假 LLM：batch 返回要点，merge 返回合并要点；可配置失败。"""

    def __init__(self, *, fail: bool = False, fail_times: int = 0,
                 available: bool = True):
        self.fail = fail
        self.fail_times = fail_times
        self.available_flag = available
        self.calls: list[dict] = []

    def available(self):
        return self.available_flag

    def chat(self, messages, **kwargs):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        self.calls.append({"purpose": kwargs.get("purpose"), "text": text})
        if self.fail or self.fail_times > 0:
            if self.fail_times > 0:
                self.fail_times -= 1
            return {"ok": False, "error": "boom"}
        if "合并" in text or "月度要点" in text:
            return {"ok": True, "text": json.dumps({"points": ["合并要点A", "合并要点B"]})}
        n = max(1, text.count("用户："))
        return {"ok": True,
                "text": json.dumps({"points": [f"要点{i}" for i in range(min(3, n))]})}


# --------------------------------------------------------------------------- #
# 分批 / 长度 / 稳定事实 / 幂等
# --------------------------------------------------------------------------- #
def test_build_monthly_batches_caps_length_and_preserves_stable(tmp_path):
    path = tmp_path / "monthly.sqlite3"
    _seed_messages(path, 2026, 9, 130)
    start = _ts(2026, 9, 1)
    _seed_memory(path, "UM-pref", "用户喜欢猫", kind="preference",
                 importance=0.8, created=start + 100)
    _seed_memory(path, "UM-low", "用户随口提过一家店", kind="preference",
                 importance=0.3, created=start + 200)
    _seed_memory(path, "UM-fact", "用户某天说过天气", kind="fact",
                 importance=0.9, created=start + 300)

    client = BatchClient()
    result = summary.build_monthly(path, "2026-09", client)
    assert result["ok"] and result["created"]
    assert result["source"] == "llm"
    assert result["batches"] == 2                 # 130 条 / 每批 120 → 2 批
    assert result["llm_calls"] == 3               # 2 次 batch + 1 次 merge
    assert len(result["content"]) <= 400
    assert "【2026年9月要点】" in result["content"]
    assert "【确定的事】" in result["content"]
    assert "用户喜欢猫" in result["content"]        # 稳定事实原样保留
    assert "用户随口提过一家店" not in result["content"]  # importance 不足
    assert "用户某天说过天气" not in result["content"]    # fact 不是稳定事实

    # 幂等：同月二次生成 → UPDATE 同一行，不新增
    rows = _monthly_rows(path)
    assert len(rows) == 1
    again = summary.build_monthly(path, "2026-09", client)
    assert again["created"] is False
    assert len(_monthly_rows(path)) == 1


def _monthly_rows(path) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM summaries WHERE kind='monthly'").fetchall()]
    finally:
        con.close()


def test_build_monthly_reads_only_raw_messages(tmp_path):
    """红线：输入只来自 messages，不读 summaries（禁止摘要的摘要）。"""
    path = tmp_path / "no_summary_of_summary.sqlite3"
    _seed_messages(path, 2026, 9, 10, prefix="原始对话")
    con = db.connect(path)
    summary._ensure_monthly_schema(con)
    con.execute("INSERT INTO summaries(id,content,evidence_json,turns_count,created_at,"
                "kind,year_month,source) VALUES(?,?,?,?,?,?,?,?)",
                ("SUM-DIRTY", "DIRTY_SUMMARY_MARKER", "{}", 1, _ts(2026, 9, 5),
                 "stage", "", "llm"))
    con.commit()
    con.close()

    client = BatchClient()
    summary.build_monthly(path, "2026-09", client)
    assert client.calls
    assert all("DIRTY_SUMMARY_MARKER" not in c["text"] for c in client.calls)
    assert any("原始对话0" in c["text"] for c in client.calls)  # 用的是原文


def test_client_failure_falls_back_to_rule_gist(tmp_path):
    path = tmp_path / "rule.sqlite3"
    _seed_messages(path, 2026, 8, 25)
    _seed_memory(path, "UM-pref", "用户喜欢猫", kind="preference",
                 importance=0.7, created=_ts(2026, 8, 2))
    client = BatchClient(fail=True)
    result = summary.build_monthly(path, "2026-08", client)
    assert result["created"] and result["source"] == "rule"
    assert "【2026年8月要点】" in result["content"]
    assert "共 25 条消息" in result["content"]
    assert "用户喜欢猫" in result["content"]
    row = _monthly_rows(path)[0]
    assert row["source"] == "rule"

    # client 不可用 → 同样规则式
    path2 = tmp_path / "rule2.sqlite3"
    _seed_messages(path2, 2026, 8, 5)
    result2 = summary.build_monthly(path2, "2026-08", BatchClient(available=False))
    assert result2["source"] == "rule" and result2["content"]


def test_llm_retry_once_then_success(tmp_path):
    path = tmp_path / "retry.sqlite3"
    _seed_messages(path, 2026, 9, 5)
    client = BatchClient(fail_times=1)   # 第一次失败，重试成功
    result = summary.build_monthly(path, "2026-09", client)
    assert result["source"] == "llm" and result["created"]


# --------------------------------------------------------------------------- #
# nightly 调度：每月一次 + 最多补 3 个月
# --------------------------------------------------------------------------- #
def test_maintenance_same_month_no_repeat(tmp_path):
    path = tmp_path / "nightly.sqlite3"
    _seed_messages(path, 2026, 9, 10)
    now = _ts(2026, 10, 15)
    client = BatchClient()
    first = summary.maintenance_monthly(path, client, now=now)
    assert first["created"] == 1
    assert first["generated"][0]["year_month"] == "2026-09"
    second = summary.maintenance_monthly(path, client, now=now)
    assert second["created"] == 0 and second["targets"] == []


def test_maintenance_backfills_at_most_three_months(tmp_path):
    path = tmp_path / "backfill.sqlite3"
    for month in (6, 7, 8, 9):
        _seed_messages(path, 2026, month, 3)
    now = _ts(2026, 10, 15)
    client = BatchClient()
    result = summary.maintenance_monthly(path, client, now=now)
    assert result["created"] == 3
    assert [g["year_month"] for g in result["generated"]] == ["2026-07", "2026-08", "2026-09"]
    assert summary.latest_monthly_month(path) == "2026-09"


def test_nightly_runs_monthly_step(tmp_path):
    path = tmp_path / "nightly_run.sqlite3"
    _seed_messages(path, 2026, 9, 6)
    result = nightly.run(path, None, force=True)
    assert "monthly_gist" in result["steps"]
    step = result["steps"]["monthly_gist"]
    assert step["ok"] is True
    # nightly 用真实当前时间 → 目标月不是 2026-09，但步骤必须不报错且结构完整
    assert "generated" in step and "targets" in step


# --------------------------------------------------------------------------- #
# 读取侧：context 拼最近 N 份月 gist，总长 ≤900，最新摘要优先
# --------------------------------------------------------------------------- #
def test_context_includes_recent_month_gists_within_limit(tmp_path):
    path = tmp_path / "context.sqlite3"
    con = db.connect(path)
    summary._ensure_monthly_schema(con)
    stage = "阶段摘要" * 40
    con.execute("INSERT INTO summaries(id,content,evidence_json,turns_count,created_at,"
                "kind,year_month,source) VALUES(?,?,?,?,?,?,?,?)",
                ("SUM-stage", stage, "{}", 3, 1000.0, "stage", "", "llm"))
    for i, month in enumerate(("2026-06", "2026-07", "2026-08")):
        con.execute("INSERT INTO summaries(id,content,evidence_json,turns_count,created_at,"
                    "kind,year_month,source) VALUES(?,?,?,?,?,?,?,?)",
                    (f"SUM-m-{month}", f"{month}要点" + "内容" * 60, "{}", 5,
                     2000.0 + i, "monthly", month, "rule"))
    con.commit()
    con.close()

    text = summary.context(path, max_chars=900, include_months=2)
    assert text.startswith("阶段摘要")          # 最新摘要优先
    assert "2026-08要点" in text and "2026-07要点" in text
    assert "2026-06要点" not in text            # 只取最近 2 份
    assert len(text) <= 900
    # latest() 不会被月度 gist 顶掉
    assert summary.latest(path)["id"] == "SUM-stage"


def test_monthly_schema_lazy_migration_idempotent(tmp_path):
    path = tmp_path / "schema.sqlite3"
    _seed_messages(path, 2026, 9, 3)
    summary.build_monthly(path, "2026-09", BatchClient(available=False))
    con = db.connect(path)
    cols1 = {r[1] for r in con.execute("PRAGMA table_info(summaries)").fetchall()}
    con.close()
    assert {"kind", "year_month", "source"} <= cols1
    summary.build_monthly(path, "2026-09", BatchClient(available=False))
    con = db.connect(path)
    cols2 = {r[1] for r in con.execute("PRAGMA table_info(summaries)").fetchall()}
    con.close()
    assert cols1 == cols2
