# -*- coding: utf-8 -*-
"""2026-10-29 主动消息质量收口：出站风格、草稿 TTL、无回应冷却。"""
import sqlite3
import time
from pathlib import Path

from agi.selflearn import proactive
from agi.wechat_gateway import drafts
from agi.wechat_gateway import proactive_worker as worker

NL = chr(10)


def _write_drafts(path: Path, rows: list[dict]) -> None:
    path.write_text(NL.join(__import__("json").dumps(row, ensure_ascii=False)
                            for row in rows) + NL, encoding="utf-8")


def _draft(draft_id: str, ts: float, status: str = "pending"):
    return {"id": draft_id, "ts": ts, "contact": "某人", "text": "测试草稿",
            "reason": "send_failed", "status": status}


def test_drafts_purge_expired_pending(tmp_path):
    path = tmp_path / "drafts.jsonl"
    now = time.time()
    _write_drafts(path, [
        _draft("old-pending", now - 30 * 3600),
        _draft("fresh-pending", now - 3600),
        _draft("old-done", now - 30 * 3600, status="sent"),
    ])
    result = drafts.purge(path=path, now=now)
    assert result["removed"] == 1 and result["kept"] == 2
    ids = [row["id"] for row in drafts.list_all(path=path, limit=10)]
    assert ids == ["old-done", "fresh-pending"] or ids == ["fresh-pending", "old-done"]
    assert "old-pending" not in ids


def test_drafts_add_drops_expired(tmp_path):
    path = tmp_path / "drafts.jsonl"
    now = time.time()
    _write_drafts(path, [_draft("old-pending", now - 48 * 3600)])
    item = drafts.add("某人", "新草稿", path=path)
    rows = drafts.list_all(path=path, limit=10)
    assert item["id"] in [row["id"] for row in rows]
    assert "old-pending" not in [row["id"] for row in rows]


def test_polish_outbound_strips_corner_brackets_and_ledger():
    raw = "想起你说「晚点回来帮忙修东西」，账先记着，你那边调试得怎么样了"
    out = proactive.polish_outbound(raw)
    assert "「" not in out and "」" not in out
    assert "账" not in out
    assert "想起你说" not in out


def test_polish_outbound_idempotent_for_clean_text():
    clean = "今天出去走了走，风挺大。"
    assert proactive.polish_outbound(clean) == proactive.polish_outbound(
        proactive.polish_outbound(clean))


def _event_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE proactive_events(id TEXT PRIMARY KEY, contact TEXT, ts REAL,"
        " text TEXT, status TEXT, kind TEXT, response TEXT, response_ts REAL)")
    now = time.time()
    con.execute("INSERT INTO proactive_events VALUES(?,?,?,?,?,?,?,?)",
                ("a", "某人", now - 3 * 3600, "第一条", "sent", "care", None, None))
    con.execute("INSERT INTO proactive_events VALUES(?,?,?,?,?,?,?,?)",
                ("b", "某人", now - 2 * 3600, "第二条", "sent", "share", None, None))
    con.commit()
    con.close()


def test_last_sent_ts_reads_latest_sent(tmp_path):
    db = tmp_path / "x.memory.sqlite3"
    _event_db(db)
    ts = worker.last_sent_ts(db, "某人")
    assert ts > 0 and abs(ts - (time.time() - 2 * 3600)) < 120
    assert worker.last_sent_ts(db, "没聊过的人") == 0.0
