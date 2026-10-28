# -*- coding: utf-8 -*-
"""S0 实时入库：把 selflearn 的每条消息镜像进 raw_events。

只记录、不生成 session；session/segment 由 `ingest.build_sessions` 按需重建。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from . import db


def _id(*parts) -> str:
    return "RE-" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def record_message(path: Path | str, role: str, text: str, *,
                   ts: float | None = None, friend: str = "selflearn",
                   message_id: str = "", kind: str = "text") -> str:
    role = str(role or "").strip() or "user"
    body = str(text or "").strip()
    if not body:
        return ""
    now = float(ts if ts is not None else time.time())
    rid = _id(friend, role, body[:80], f"{now:.6f}", message_id)
    meta = json.dumps({"role": role, "message_id": message_id}, ensure_ascii=False)
    con = db.connect(path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO raw_events(id,source,source_id,friend,sender,kind,text,ts,meta_json) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, "live", message_id or rid, friend, role, kind, body, now, meta))
        con.commit()
    finally:
        con.close()
    return rid
