# -*- coding: utf-8 -*-
"""07/13 从错误中学习：错误账本 + 可执行教训。"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

LESSONS = {
    "too_long": "回复超过 220 字，下次先压到 1-3 句。",
    "too_many_questions": "一条回复里连续问超过 2 个问题，容易让人有压力。",
    "pushy": "不要用“你必须/你一定”替用户下结论。",
    "bad_feedback": "这次反馈为 bad；先接住情绪，再考虑建议。",
}


def _id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def classify(reply: str, note: str = "") -> str:
    if len(reply) > 220:
        return "too_long"
    if reply.count("?") + reply.count("？") > 2:
        return "too_many_questions"
    if any(w in reply for w in ("你必须", "你一定", "你应该马上")):
        return "pushy"
    return "bad_feedback"


def record(path: Path | str, turn_id: str, reply: str, note: str = "") -> dict[str, Any]:
    category = classify(reply, note)
    con = db.connect(path)
    try:
        now = time.time()
        eid = _id("ERR-", turn_id, category, now)
        con.execute("INSERT INTO errors(id,turn_id,category,detail,created_at) VALUES(?,?,?,?,?)",
                    (eid, turn_id, category, reply[:500], now))
        lesson = LESSONS.get(category, LESSONS["bad_feedback"])
        exists = con.execute("SELECT id FROM lessons WHERE category=? AND status='active'",
                             (category,)).fetchone()
        if exists is None:
            con.execute("INSERT INTO lessons(id,category,lesson,evidence,status,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (_id("L-", category), category, lesson, reply[:300], "active", now))
        con.commit()
    finally:
        con.close()
    return {"category": category, "lesson": LESSONS.get(category, LESSONS["bad_feedback"])}


def active_lessons(path: Path | str = DB_PATH) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM lessons WHERE status='active' ORDER BY created_at DESC LIMIT 10")]
    finally:
        con.close()


def budget(path: Path | str = DB_PATH, window: int = 20) -> dict:
    """最近 window 条 turn 内同类错误 >=3 → 保守模式。"""
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT category,count(*) n FROM errors WHERE turn_id IN ("
            "SELECT id FROM turns ORDER BY created_at DESC LIMIT ?) GROUP BY category",
            (int(window),)).fetchall()
    finally:
        con.close()
    total = sum(int(r["n"]) for r in rows)
    return {"count": total, "by_category": {r["category"]: int(r["n"]) for r in rows},
            "conservative": any(int(r["n"]) >= 3 for r in rows)}


def context(path: Path | str = DB_PATH) -> str:
    rows = active_lessons(path)
    return "\n".join(f"- {r['lesson']}" for r in rows)


def list_errors(path: Path | str = DB_PATH, limit: int = 20) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM errors ORDER BY created_at DESC LIMIT ?", (int(limit),))]
    finally:
        con.close()
