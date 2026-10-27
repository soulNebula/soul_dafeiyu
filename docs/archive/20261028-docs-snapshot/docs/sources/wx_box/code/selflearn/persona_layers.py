# -*- coding: utf-8 -*-
"""人格三层：core（人工批准）/ evolving（证据累积+限速）/ relations（关系语境）。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from . import db

LAYERS = ("core", "evolving", "relations")
MAX_PROMOTIONS_PER_DAY = 2


def _id(*parts) -> str:
    return "PL-" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def _changelog(con, key, action, old, new, layer, now):
    con.execute("INSERT INTO persona_changelog(id,key,action,old_value,new_value,layer,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (_id("CL-", key, action, now), key, action, old[:300], new[:300], layer, now))


def observe(path: Path | str, key: str, value: str, evidence=None,
            layer: str = "evolving") -> dict[str, Any]:
    key = str(key or "").strip()[:60]
    value = str(value or "").strip()[:240]
    layer = layer if layer in LAYERS else "evolving"
    if not key or not value:
        return {"ok": False, "error": "key/value 不能为空"}
    now = time.time()
    con = db.connect(path)
    try:
        row = con.execute("SELECT * FROM persona_layers WHERE key=? AND layer=? AND value=?",
                          (key, layer, value)).fetchone()
        if row:
            ev = set(json.loads(row["evidence_json"] or "[]"))
            ev.update(str(x) for x in (evidence or []))
            status = "active" if layer == "evolving" and len(ev) >= 2 else row["status"]
            con.execute("UPDATE persona_layers SET evidence_json=?,status=?,updated_at=? WHERE id=?",
                        (json.dumps(sorted(ev), ensure_ascii=False), status, now, row["id"]))
            if row["status"] != "active" and status == "active":
                _changelog(con, key, "promote", row["value"], value, layer, now)
            con.commit()
            return {"ok": True, "id": row["id"], "status": status, "evidence": len(ev)}
        if layer == "evolving":
            promoted = con.execute(
                "SELECT count(*) FROM persona_changelog WHERE layer='evolving' AND action='promote' "
                "AND created_at>?", (now - 86400,)).fetchone()[0]
            if int(promoted) >= MAX_PROMOTIONS_PER_DAY:
                return {"ok": False, "error": "今日人格晋级配额已用完"}
        pid = _id(key, layer, value, now)
        status = "candidate"
        ev = sorted({str(x) for x in (evidence or [])})
        if layer == "evolving" and len(ev) >= 2:
            status = "active"
            _changelog(con, key, "promote", "", value, layer, now)
        con.execute("INSERT INTO persona_layers(id,layer,key,value,status,evidence_json,version,"
                    "approved_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (pid, layer, key, value, status, json.dumps(ev, ensure_ascii=False),
                     1, "", now, now))
        con.commit()
        return {"ok": True, "id": pid, "status": status, "evidence": len(ev)}
    finally:
        con.close()


def approve(path: Path | str, layer_id: str, approved_by: str) -> dict[str, Any]:
    if not str(approved_by or "").strip():
        return {"ok": False, "error": "core 需要 approved_by"}
    now = time.time()
    con = db.connect(path)
    try:
        row = con.execute("SELECT * FROM persona_layers WHERE id=?", (layer_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "不存在"}
        if row["layer"] != "core":
            return {"ok": False, "error": "只有 core 层需要人工批准"}
        con.execute("UPDATE persona_layers SET status='active',approved_by=?,updated_at=? WHERE id=?",
                    (str(approved_by)[:60], now, layer_id))
        _changelog(con, row["key"], "approve", row["value"], row["value"], "core", now)
        con.commit()
        return {"ok": True, "id": layer_id, "status": "active", "approved_by": approved_by}
    finally:
        con.close()


def context(path: Path | str) -> str:
    con = db.connect(path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT layer,key,value FROM persona_layers WHERE status='active' "
            "ORDER BY layer,updated_at DESC LIMIT 40")]
    finally:
        con.close()
    if not rows:
        return ""
    by = {}
    for r in rows:
        by.setdefault(r["layer"], []).append(f"{r['key']}={r['value']}")
    return "\n".join(f"{layer}: " + "；".join(items) for layer, items in by.items())


def changelog(path: Path | str, limit: int = 30) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM persona_changelog ORDER BY created_at DESC LIMIT ?", (int(limit),))]
    finally:
        con.close()
