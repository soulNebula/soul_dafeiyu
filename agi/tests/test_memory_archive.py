# -*- coding: utf-8 -*-
"""P1-d：>90 天原文冷归档（不删不改写、默认检索不到、翻旧账可查）。

红线：messages.content/ts 逐字节不变；归档只写 archived_at。
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from agi.selflearn import db, memory, nightly, retrieval, search, summary

DAY = 86400.0


def _insert_messages(path, rows: list[tuple[str, str, str, float, int]]):
    con = db.connect(path)
    for mid, role, text, ts, proactive in rows:
        con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                    (mid, role, text, ts, proactive))
    con.commit()
    con.close()


def _message_rows(path) -> dict[str, dict]:
    con = db.connect(path)
    try:
        return {str(r["id"]): dict(r) for r in con.execute(
            "SELECT * FROM messages ORDER BY id").fetchall()}
    finally:
        con.close()


def _seed(path, now: float):
    """3 条 100 天前 + 2 条 10 天前。"""
    rows = []
    for i in range(3):
        rows.append((f"M-old-{i}", "user", f"老消息{i}：以前喜欢咖啡", now - 100 * DAY, 0))
    for i in range(2):
        rows.append((f"M-new-{i}", "user", f"新消息{i}：今天喝了咖啡", now - 10 * DAY, 0))
    _insert_messages(path, rows)
    return rows


# --------------------------------------------------------------------------- #
# archive_cold：只归档老的、内容不变、幂等
# --------------------------------------------------------------------------- #
def test_archive_cold_only_old_and_immutable(tmp_path):
    path = tmp_path / "archive.sqlite3"
    now = 1_800_000_000.0
    _seed(path, now)
    before = _message_rows(path)

    result = memory.archive_cold(path, now=now, days=90)
    assert result["ok"] is True
    assert result["archived"] == 3 and result["scanned"] == 3
    assert result["remaining"] == 0

    after = _message_rows(path)
    for mid, row in before.items():
        assert after[mid]["text"] == row["text"]      # content 逐字节不变
        assert after[mid]["ts"] == row["ts"]          # ts 不变
        if mid.startswith("M-old"):
            assert after[mid]["archived_at"] == now
        else:
            assert after[mid]["archived_at"] is None  # 新消息不归档

    again = memory.archive_cold(path, now=now, days=90)
    assert again["archived"] == 0 and again["remaining"] == 0   # 幂等


def test_archive_cold_batches(tmp_path):
    path = tmp_path / "batch.sqlite3"
    now = 1_800_000_000.0
    rows = [(f"M-{i}", "user", f"老消息{i}", now - 120 * DAY, 0) for i in range(7)]
    _insert_messages(path, rows)
    result = memory.archive_cold(path, now=now, days=90, batch=2)
    assert result["archived"] == 7
    assert memory.archive_stats(path, now=now)["archived"] == 7


def test_archive_does_not_touch_memories_or_claims(tmp_path):
    path = tmp_path / "layers.sqlite3"
    now = 1_800_000_000.0
    _seed(path, now)
    con = db.connect(path)
    con.execute("INSERT INTO user_memories(id,scope,kind,content,evidence,confidence,"
                "status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("UM-old", "user", "preference", "用户喜欢咖啡", "我喜欢咖啡", 0.8,
                 "active", now - 120 * DAY))
    con.execute("INSERT INTO claims(id,subject,predicate,object,polarity,time_ref,"
                "claim_type,evidence_span,confidence,source_level,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("C-old", "user", "喜欢", "咖啡", 1, "", "preference",
                 "我喜欢咖啡", 0.8, "A", "active", now - 120 * DAY))
    con.commit()
    con.close()

    memory.archive_cold(path, now=now, days=90)
    con = db.connect(path)
    try:
        mem = con.execute("SELECT count(*) FROM user_memories").fetchone()[0]
        claim = con.execute("SELECT count(*) FROM claims").fetchone()[0]
    finally:
        con.close()
    assert mem == 1 and claim == 1       # 蒸馏层不归档


# --------------------------------------------------------------------------- #
# 检索侧：默认排除归档，翻旧账可查且标 archived=True
# --------------------------------------------------------------------------- #
def test_hybrid_excludes_archived_by_default(tmp_path):
    path = tmp_path / "hybrid.sqlite3"
    now = 1_800_000_000.0
    _seed(path, now)
    search.index_texts(path, [
        {"source_type": "message", "source_id": f"M-old-{i}",
         "text": f"老消息{i}：以前喜欢咖啡"} for i in range(3)
    ] + [
        {"source_type": "message", "source_id": f"M-new-{i}",
         "text": f"新消息{i}：今天喝了咖啡"} for i in range(2)
    ])
    memory.archive_cold(path, now=now, days=90)

    hits = retrieval.hybrid(path, "咖啡", k=10, now=now)
    ids = {h["source_id"] for h in hits}
    assert ids == {"M-new-0", "M-new-1"}          # 归档默认检索不到
    assert all(h["archived"] is False for h in hits)

    look = retrieval.hybrid(path, "还记得以前喜欢的咖啡吗", k=10, now=now)
    look_ids = {h["source_id"]: h for h in look}
    assert {"M-old-0", "M-old-1", "M-old-2"} <= set(look_ids)
    assert all(look_ids[mid]["archived"] is True for mid in
               ("M-old-0", "M-old-1", "M-old-2"))
    # 未归档的 message 在翻旧账时 archived=False
    assert look_ids["M-new-0"]["archived"] is False


def test_retrieval_context_prefixes_earlier(tmp_path):
    path = tmp_path / "context.sqlite3"
    now = 1_800_000_000.0
    _seed(path, now)
    search.index_texts(path, [
        {"source_type": "message", "source_id": "M-old-0", "text": "老消息0：以前喜欢咖啡"},
    ])
    memory.archive_cold(path, now=now, days=90)
    text = retrieval.context(path, "还记得以前的咖啡吗", k=5, now=now)
    assert "[更早以前]" in text
    # 默认 query 不含翻旧账线索 → 归档内容不出现
    assert "老消息0" not in retrieval.context(path, "咖啡", k=5, now=now)


# --------------------------------------------------------------------------- #
# 导出 / 指标
# --------------------------------------------------------------------------- #
def test_export_archive_rows_and_overwrite(tmp_path):
    path = tmp_path / "export.sqlite3"
    now = 1_800_000_000.0
    _seed(path, now)
    memory.archive_cold(path, now=now, days=90)
    out = tmp_path / "cold" / "archive.jsonl"
    first = memory.export_archive(path, out)
    assert first["ok"] and first["rows"] == 3
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert {p["id"] for p in parsed} == {"M-old-0", "M-old-1", "M-old-2"}
    assert all(p["content"].startswith("老消息") for p in parsed)
    assert all(p["archived_at"] == now for p in parsed)
    # 重复导出覆盖写，不追加
    memory.export_archive(path, out)
    assert len(out.read_text(encoding="utf-8").splitlines()) == 3


def test_archive_stats(tmp_path):
    path = tmp_path / "stats.sqlite3"
    now = 1_800_000_000.0
    _seed(path, now)
    memory.archive_cold(path, now=now, days=90)
    stats = memory.archive_stats(path, now=now)
    assert stats["messages_total"] == 5
    assert stats["archived"] == 3 and stats["active"] == 2
    assert stats["oldest_active_days"] == pytest.approx(10.0, abs=0.1)


# --------------------------------------------------------------------------- #
# nightly 调度 + db 迁移收敛
# --------------------------------------------------------------------------- #
def test_nightly_archive_step_idempotent(tmp_path):
    path = tmp_path / "nightly.sqlite3"
    now = time.time()
    _insert_messages(path, [(f"M-old-{i}", "user", f"老消息{i}",
                             now - 120 * DAY, 0) for i in range(2)])
    first = nightly.run(path, None, force=True)
    assert "archive_cold" in first["steps"]
    assert first["steps"]["archive_cold"]["archived"] == 2
    second = nightly.run(path, None, force=True)
    assert second["steps"]["archive_cold"]["archived"] == 0


def test_db_migration_old_schema(tmp_path):
    """极早期库：messages 无 archived_at、summaries 无 kind/year_month/source。"""
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE messages(id TEXT PRIMARY KEY, role TEXT NOT NULL, "
                "text TEXT NOT NULL, ts REAL NOT NULL, proactive INTEGER NOT NULL DEFAULT 0)")
    con.execute("CREATE TABLE summaries(id TEXT PRIMARY KEY, content TEXT NOT NULL, "
                "evidence_json TEXT NOT NULL, turns_count INTEGER NOT NULL DEFAULT 0, "
                "created_at REAL NOT NULL)")
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES('M1','user','旧消息',10.0,0)")
    con.execute("INSERT INTO summaries(id,content,evidence_json,turns_count,created_at) "
                "VALUES('S1','旧摘要','{}',1,10.0)")
    con.commit()
    con.close()

    con = db.connect(path)
    mcols = {r[1] for r in con.execute("PRAGMA table_info(messages)").fetchall()}
    scols = {r[1] for r in con.execute("PRAGMA table_info(summaries)").fetchall()}
    con.close()
    assert "archived_at" in mcols
    assert {"kind", "year_month", "source"} <= scols
    assert _message_rows(path)["M1"]["text"] == "旧消息"

    # 二次 connect 幂等
    con = db.connect(path)
    mcols2 = {r[1] for r in con.execute("PRAGMA table_info(messages)").fetchall()}
    scols2 = {r[1] for r in con.execute("PRAGMA table_info(summaries)").fetchall()}
    con.close()
    assert (mcols, scols) == (mcols2, scols2)


def test_summary_context_no_longer_pragma_migrates(tmp_path, monkeypatch):
    path = tmp_path / "no_pragma.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO summaries(id,content,evidence_json,turns_count,created_at,"
                "kind,year_month,source) VALUES(?,?,?,?,?,?,?,?)",
                ("S1", "阶段摘要内容", "{}", 1, 10.0, "stage", "", "llm"))
    con.commit()
    con.close()

    def boom(con):  # pragma: no cover - 只应在旧调用路径兜底时出现
        raise AssertionError("summary.context() 不应每轮跑 PRAGMA/迁移")

    monkeypatch.setattr(summary, "_ensure_monthly_schema", boom)
    assert summary.context(path) == "阶段摘要内容"
    assert summary.latest(path)["id"] == "S1"
