# -*- coding: utf-8 -*-
"""从真实历史消息回填 claims / persona；不伪造 feedback。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import claim_ingest, db, personality


def claims_from_messages(path: Path | str, client, *, limit: int = 30,
                         only_user: bool = True) -> dict[str, Any]:
    """对历史 user 消息重新抽取 claim；空文本/失败跳过。"""
    con = db.connect(path)
    try:
        if only_user:
            sql = "SELECT id,text FROM messages WHERE role='user' ORDER BY ts ASC LIMIT ?"
            args = (max(1, int(limit)),)
        else:
            sql = "SELECT id,text FROM messages WHERE role IN ('user','assistant') ORDER BY ts ASC LIMIT ?"
            args = (max(1, int(limit)),)
        rows = [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()
    extracted = accepted = pending = 0
    errors = 0
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        try:
            result = claim_ingest.ingest(path, client, text, str(row["id"]))
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        extracted += int(result.get("extracted") or 0)
        accepted += len(result.get("accepted") or [])
        pending += len(result.get("pending") or [])
    return {"ok": errors == 0, "messages": len(rows), "extracted": extracted,
            "accepted": accepted, "pending": pending, "errors": errors}


def persona_from_history(path: Path | str, client) -> dict[str, Any]:
    """从真实记忆/对话反思出人格；无证据时由 personality 自己拒绝。"""
    try:
        return personality.reflect(path, client)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
