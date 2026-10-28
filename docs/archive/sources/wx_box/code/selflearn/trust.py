# -*- coding: utf-8 -*-
"""10 信任账本：正向/负向事件，信任分与未解决事件。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

DELTA = {"good": 0.025, "ok": 0.008, "bad": -0.045}


def _id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def get(path: Path | str = DB_PATH) -> float:
    con = db.connect(path)
    try:
        row = con.execute("SELECT value FROM state WHERE key='trust'").fetchone()
        value = float(row["value"]) if row else 0.5
    except (TypeError, ValueError):
        value = 0.5
    finally:
        con.close()
    return max(0.1, min(0.95, value))


def set_value(path: Path | str, value: float) -> float:
    value = max(0.1, min(0.95, float(value)))
    con = db.connect(path)
    try:
        con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES(?,?,?)",
                    ("trust", str(value), time.time()))
        con.commit()
    finally:
        con.close()
    return value


def update(path: Path | str, reaction: str, turn_id: str | None = None,
           reason: str = "") -> dict[str, Any]:
    reaction = str(reaction).lower()
    delta = DELTA.get(reaction, 0.0)
    before = get(path)
    # 一次好的互动可以修复一条旧的不愉快，而不是让账本永远记着
    if reaction == "good":
        old = unresolved(path)
        if old:
            resolve(path, old[-1]["id"])
            delta += 0.01
    after = set_value(path, before + delta)
    kind = "positive" if delta >= 0 else "negative"
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO trust_events(id,kind,delta,reason,turn_id,resolved,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (_id("TE-", turn_id, time.time()), kind, delta, reason[:300], turn_id, 0, time.time()))
        con.commit()
    finally:
        con.close()
    return {"before": round(before, 4), "after": round(after, 4), "delta": delta,
            "stage": stage(after)}


def stage(value: float) -> str:
    if value < 0.35:
        return "疏远"
    if value < 0.55:
        return "初识"
    if value < 0.72:
        return "熟悉"
    if value < 0.85:
        return "亲近"
    return "信任"


def unresolved(path: Path | str = DB_PATH) -> list[dict]:
    con = db.connect(path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM trust_events WHERE kind='negative' AND resolved=0 "
            "ORDER BY created_at DESC LIMIT 10")]
    finally:
        con.close()
    return rows


def resolve(path: Path | str, event_id: str) -> bool:
    con = db.connect(path)
    try:
        cur = con.execute("UPDATE trust_events SET resolved=1 WHERE id=?", (event_id,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def repair(path: Path | str = DB_PATH, note: str = "") -> dict[str, Any]:
    """显式修复一条未解决事件（用户说“算了/过去吧”时可调用）。"""
    items = unresolved(path)
    if not items:
        return {"ok": True, "repaired": 0, "trust": round(get(path), 4)}
    resolve(path, items[-1]["id"])
    value = set_value(path, get(path) + 0.02)
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO trust_events(id,kind,delta,reason,turn_id,resolved,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (_id("TE-", "repair", time.time()), "positive", 0.02, note[:300], None, 1, time.time()))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "repaired": 1, "trust": round(value, 4), "stage": stage(value)}


def summary(path: Path | str = DB_PATH) -> str:
    value = get(path)
    unresolved_count = len(unresolved(path))
    if unresolved_count:
        return f"信任 {value:.2f}（{stage(value)}），有 {unresolved_count} 个未解决的不愉快"
    return f"信任 {value:.2f}（{stage(value)}）"


def snapshot(path: Path | str = DB_PATH) -> dict[str, Any]:
    return {"trust": round(get(path), 4), "stage": stage(get(path)),
            "unresolved": unresolved(path), "summary": summary(path)}
