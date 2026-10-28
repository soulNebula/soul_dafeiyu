# -*- coding: utf-8 -*-
"""14 按需唤醒/意图调度：只产生到期提醒，不自动发送。"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH


def _id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def add(path: Path | str, kind: str, text: str, wake_at: float,
        reason: str = "") -> str:
    iid = _id("INT-", kind, text, wake_at)
    con = db.connect(path)
    try:
        con.execute("INSERT OR REPLACE INTO intents(id,kind,text,wake_at,status,reason,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (iid, kind, text, float(wake_at), "pending", reason, time.time()))
        con.commit()
    finally:
        con.close()
    return iid


def due(path: Path | str = DB_PATH, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else float(now)
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM intents WHERE status='pending' AND wake_at<=? "
            "ORDER BY wake_at LIMIT 50", (now,))]
    finally:
        con.close()


def mark_done(path: Path | str, intent_id: str) -> bool:
    con = db.connect(path)
    try:
        cur = con.execute("UPDATE intents SET status='done' WHERE id=?", (intent_id,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def list_intents(path: Path | str = DB_PATH, limit: int = 30) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM intents ORDER BY wake_at DESC LIMIT ?", (int(limit),))]
    finally:
        con.close()
