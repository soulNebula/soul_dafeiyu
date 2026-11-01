# -*- coding: utf-8 -*-
"""V1 PR5：微信联系人快照（wxid 稳定 ID）+ 白名单双写辅助。

- ``refresh()``：调用 WCDB 联系人枚举（只读），落 ``contacts.sqlite3``；
- ``wxid_for(name)``：按昵称/备注反查 wxid；
- ``is_allowed(name, cfg)``：名称在白名单，或昵称映射到的 wxid 在 ``allowed_wxids``；
- 不发送、不改微信、不碰消息库。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from . import config as gw_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS wechat_accounts(
  wxid TEXT PRIMARY KEY,
  nickname TEXT DEFAULT '',
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts(
  wxid TEXT PRIMARY KEY,
  nickname TEXT DEFAULT '',
  local_type INTEGER DEFAULT 0,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_contacts_nickname ON contacts(nickname);
"""


def db_path() -> Path:
    return Path(gw_config.DATA_DIR) / "contacts.sqlite3"


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _default_reader() -> list[dict]:
    try:
        from wx_core import db_source
        return db_source.enumerate_contacts({}) or []
    except Exception:  # noqa: BLE001 - 非 Windows/微信未启动
        return []


def refresh(reader=None) -> dict:
    """刷新联系人快照；``reader`` 可注入（测试/其它后端）。"""
    items = reader() if callable(reader) else _default_reader()
    if not items:
        return {"ok": False, "error": "contact_reader_unavailable",
                "hint": "确认微信已登录/版本受支持，或稍后重试"}
    now = time.time()
    with _connect() as con:
        for item in items:
            wxid = str((item or {}).get("wxid") or "").strip()
            if not wxid:
                continue
            con.execute(
                "INSERT INTO contacts(wxid,nickname,local_type,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(wxid) DO UPDATE SET "
                "nickname=excluded.nickname, local_type=excluded.local_type, "
                "updated_at=excluded.updated_at",
                (wxid, str(item.get("nickname") or wxid),
                 int(item.get("local_type") or 0), now))
    return {"ok": True, "count": len(items), "contacts": list_contacts(500),
            "db": str(db_path())}


def list_contacts(limit: int = 500) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT wxid,nickname,local_type,updated_at FROM contacts "
            "ORDER BY updated_at DESC, nickname LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
    return [dict(row) for row in rows]


def wxid_for(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    with _connect() as con:
        row = con.execute(
            "SELECT wxid FROM contacts WHERE nickname=? OR wxid=? "
            "ORDER BY updated_at DESC LIMIT 1", (text, text)).fetchone()
    return str(row["wxid"]) if row else ""


def is_allowed(name: str, cfg: dict) -> bool:
    """名称白名单命中，或映射到的 wxid 命中 ``allowed_wxids``。"""
    text = str(name or "").strip()
    names = {str(x).strip() for x in (cfg.get("allowed_contacts") or []) if str(x).strip()}
    if text in names:
        return True
    allowed_wxids = {str(x).strip() for x in (cfg.get("allowed_wxids") or []) if str(x).strip()}
    if not allowed_wxids:
        return False
    wxid = wxid_for(text)
    return bool(wxid and wxid in allowed_wxids)
