# -*- coding: utf-8 -*-
"""11 分享候选/事件的本地持久化；只记录，不发送。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from . import db


def _id(*parts) -> str:
    return "SH-" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def record_candidate(path: Path | str, *, level: str, kind: str, text: str,
                     reason: str = "", source: str = "", source_id: str = "",
                     score: float = 0.0, status: str = "candidate") -> dict[str, Any]:
    if not str(text or "").strip():
        return {"ok": False, "error": "text 不能为空"}
    now = time.time()
    cid = _id(level, kind, text, source_id, now)
    con = db.connect(path)
    try:
        con.execute("INSERT INTO share_candidates(id,level,kind,text,reason,source,source_id,score,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (cid, str(level), str(kind), str(text)[:500], str(reason)[:300],
                     str(source), str(source_id), float(score), str(status), now))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "id": cid, "level": level, "kind": kind, "score": score}


def record_event(path: Path | str, candidate_id: str, action: str,
                 detail: Any = None) -> dict[str, Any]:
    now = time.time()
    eid = _id("EV-", candidate_id, action, now)
    con = db.connect(path)
    try:
        con.execute("INSERT INTO share_events(id,candidate_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                    (eid, str(candidate_id), str(action),
                     json.dumps(detail, ensure_ascii=False) if detail is not None else "", now))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "id": eid, "action": action}


def list_candidates(path: Path | str, limit: int = 20) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM share_candidates ORDER BY created_at DESC LIMIT ?", (int(limit),))]
    finally:
        con.close()


def stats(path: Path | str) -> dict:
    con = db.connect(path)
    try:
        c = con.execute("SELECT count(*) FROM share_candidates").fetchone()[0]
        e = con.execute("SELECT count(*) FROM share_events").fetchone()[0]
    finally:
        con.close()
    return {"ok": True, "candidates": int(c), "events": int(e)}
