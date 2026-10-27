# -*- coding: utf-8 -*-
"""M4 目标树：记录它现在想做什么，不自动执行。"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from . import db

STATUSES = ("active", "paused", "done", "dropped")


def _id(*parts) -> str:
    return "G-" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def add(path: Path | str, title: str, *, parent_id: str = "",
        priority: float = 0.5) -> dict[str, Any]:
    text = str(title or "").strip()[:160]
    if not text:
        return {"ok": False, "error": "title 不能为空"}
    now = time.time()
    gid = _id(text, parent_id, now)
    con = db.connect(path)
    try:
        con.execute("INSERT INTO goals(id,title,status,priority,progress,parent_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (gid, text, "active", max(0.0, min(1.0, float(priority))), 0.0,
                     str(parent_id or "") or None, now, now))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "id": gid, "title": text, "status": "active",
            "priority": priority, "parent_id": parent_id or None}


def update(path: Path | str, goal_id: str, **fields: Any) -> dict[str, Any]:
    allowed = {"title", "status", "priority", "progress", "parent_id"}
    values = {k: v for k, v in fields.items() if k in allowed}
    if not values:
        return {"ok": False, "error": "没有可更新字段"}
    if "status" in values and values["status"] not in STATUSES:
        return {"ok": False, "error": "status 非法"}
    if "priority" in values:
        values["priority"] = max(0.0, min(1.0, float(values["priority"])))
    if "progress" in values:
        values["progress"] = max(0.0, min(1.0, float(values["progress"])))
    values["updated_at"] = time.time()
    sets = ",".join(f"{k}=?" for k in values)
    con = db.connect(path)
    try:
        cur = con.execute(f"UPDATE goals SET {sets} WHERE id=?", (*values.values(), goal_id))
        con.commit()
        if not cur.rowcount:
            return {"ok": False, "error": "目标不存在"}
    finally:
        con.close()
    return {"ok": True, "id": goal_id, **{k: v for k, v in values.items() if k != "updated_at"}}


def list_goals(path: Path | str, status: str = "") -> list[dict[str, Any]]:
    con = db.connect(path)
    try:
        if status:
            rows = con.execute("SELECT * FROM goals WHERE status=? ORDER BY priority DESC, created_at",
                               (status,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM goals ORDER BY priority DESC, created_at").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def tree(path: Path | str) -> list[dict[str, Any]]:
    rows = list_goals(path)
    by_id = {r["id"]: {**r, "children": []} for r in rows}
    roots = []
    for row in rows:
        node = by_id[row["id"]]
        parent = row.get("parent_id")
        if parent and parent in by_id:
            by_id[parent]["children"].append(node)
        else:
            roots.append(node)
    return roots


__all__ = ["STATUSES", "add", "update", "list_goals", "tree"]
