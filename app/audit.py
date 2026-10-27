# -*- coding: utf-8 -*-
"""审计日志：每次工作台写操作留 before/after，本机 sqlite。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT,
  meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


def _connect(db: Path | None = None) -> sqlite3.Connection:
    paths.ensure_data_dir()
    con = sqlite3.connect(str(db or paths.AUDIT_DB))
    con.executescript(SCHEMA)
    con.commit()
    return con


def record(action: str, target: str, *, actor: str = "local",
           before=None, after=None, meta=None, db: Path | None = None) -> None:
    """记录一次变更。失败静默（审计不能把业务搞挂）。"""
    try:
        con = _connect(db)
        try:
            con.execute(
                "INSERT INTO audit(ts,actor,action,target,before_json,after_json,meta_json)"
                " VALUES(?,?,?,?,?,?,?)",
                (time.time(), actor, action, target,
                 _dump(before), _dump(after), _dump(meta)))
            con.commit()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        pass


def list_recent(limit: int = 100, *, db: Path | None = None) -> list[dict]:
    con = _connect(db)
    try:
        rows = con.execute(
            "SELECT id,ts,actor,action,target,before_json,after_json,meta_json"
            " FROM audit ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "ts": r[1], "actor": r[2], "action": r[3], "target": r[4],
            "before": _load(r[5]), "after": _load(r[6]), "meta": _load(r[7]),
        })
    return out


def _dump(value) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(value)


def _load(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return raw
