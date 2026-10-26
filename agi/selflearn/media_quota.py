# -*- coding: utf-8 -*-
"""P19：被动回复媒体配额（日常节奏：10 条里至少 2 条带 emoji/贴纸/GIF）。

只记录「AI 对用户的被动回复」；正文 Unicode emoji 也算媒体。
存储单独表 ``reply_media_log``（memory 库），不依赖旧表字段。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from . import config

try:
    from .media_choice import _INLINE_EMOJI_RE
except Exception:  # pragma: no cover
    _INLINE_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reply_media_log(
  id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL DEFAULT '',
  at REAL NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  types_json TEXT NOT NULL DEFAULT '[]',
  names_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_reply_media_log_at ON reply_media_log(at);
"""


def _memory_path(path: Path | str | None = None) -> Path:
    from . import db
    return db.memory_path(path if path is not None else config.DB_PATH)


def _connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = _memory_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(target), timeout=5)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    return con


def _has_emoji(text: Any) -> bool:
    return bool(_INLINE_EMOJI_RE.search(str(text or "")))


def _media_types(media: Any, text: str = "") -> tuple[list[str], list[str]]:
    types: list[str] = []
    names: list[str] = []
    if isinstance(media, dict):
        for item in media.get("bubbles") or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            if kind == "sticker":
                types.append("sticker")
            elif kind == "gif":
                types.append("gif")
            elif kind == "image":
                types.append("image")
            elif kind == "emoji_text":
                types.append("emoji")
            name = str(item.get("name") or "")
            if name:
                names.append(name)
        if media.get("stickers"):
            types.extend(["sticker"] * len(media.get("stickers") or []))
            names.extend(str(x) for x in media.get("stickers") or [])
    if _has_emoji(text):
        types.append("emoji")
    # 去重并保持顺序
    seen: set[str] = set()
    types = [x for x in types if not (x in seen or seen.add(x))]
    names = list(dict.fromkeys(names))
    return types, names


def record(path: Path | str | None, turn_id: Any, media: Any,
           text: str = "", *, now: float | None = None) -> dict:
    """记录一条被动回复（无媒体也写 used=0）；失败返回 ok=False，不抛给主链。"""
    try:
        types, names = _media_types(media, text)
        used = bool(types)
        at = time.time() if now is None else float(now)
        con = _connect(path)
        try:
            con.execute(
                "INSERT INTO reply_media_log(id,turn_id,at,used,types_json,names_json) "
                "VALUES(?,?,?,?,?,?)",
                ("RM-" + uuid.uuid4().hex[:16], str(turn_id or ""), at,
                 int(used), json.dumps(types, ensure_ascii=False),
                 json.dumps(names, ensure_ascii=False)))
            con.commit()
        finally:
            con.close()
        return {"ok": True, "used": used, "types": types, "names": names}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "used": False, "error": str(exc)[:120]}


def _usage_rows(path: Path | str | None, limit: int = 10) -> list[dict]:
    con = _connect(path)
    try:
        rows = con.execute(
            "SELECT * FROM reply_media_log ORDER BY at DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["types"] = json.loads(item.pop("types_json") or "[]")
            except (TypeError, ValueError):
                item["types"] = []
            try:
                item["names"] = json.loads(item.pop("names_json") or "[]")
            except (TypeError, ValueError):
                item["names"] = []
            item["used"] = bool(item.get("used"))
            out.append(item)
        return out
    finally:
        con.close()


def recent_usage(path: Path | str | None, limit: int = 10) -> list[dict]:
    """最近 N 条被动回复的媒体记录（按时间倒序）；表缺失/读取失败返回 []。"""
    try:
        return _usage_rows(path, limit)
    except Exception:  # noqa: BLE001
        return []


def need_media(path: Path | str | None = None, now: float | None = None) -> dict:
    """基于最近 10 条决定本条是否强制带媒体。

    <2 条带媒体 → force；2–3 → 自然鼓励（30%）；≥4 → 不强制。
    最近连续 3 条带媒体 → soft_cool（仍可带，但优先 emoji）。
    """
    if os.environ.get("MEDIA_QUOTA_ENABLED") is not None:
        if str(os.environ["MEDIA_QUOTA_ENABLED"]).strip().lower() not in ("1", "true", "yes", "on"):
            return {"force": False, "reason": "disabled", "used": 0,
                    "total": 0, "soft_cool": False}
    elif not bool(getattr(config, "MEDIA_QUOTA_ENABLED", True)):
        return {"force": False, "reason": "disabled", "used": 0,
                "total": 0, "soft_cool": False}
    try:
        rows = _usage_rows(path, 10)
        used_count = sum(1 for row in rows if row.get("used"))
        total = len(rows)
        soft_cool = total >= 3 and all(row.get("used") for row in rows[:3])
        if used_count < 2:
            return {"force": True, "reason": "quota_min", "used": used_count,
                    "total": total, "soft_cool": soft_cool}
        if used_count <= 3:
            return {"force": False, "reason": "quota_encourage", "used": used_count,
                    "total": total, "soft_cool": soft_cool}
        return {"force": False, "reason": "quota_ok", "used": used_count,
                "total": total, "soft_cool": soft_cool}
    except Exception:  # noqa: BLE001
        return {"force": False, "reason": "quota_error", "used": 0,
                "total": 0, "soft_cool": False}


def prompt_block(need: dict | None) -> str:
    """把配额决策变成 chat prompt 里的一段自然指令。"""
    info = need or {}
    if info.get("force"):
        return ("这一条请带一个表情（贴纸指令 [[media:sticker:名字]] 或正文 emoji 均可，"
                "选最贴切的）。")
    if info.get("soft_cool"):
        return ("最近连续几条都带了媒体，这条优先用正文 emoji/文字表情，"
                "不必再额外发贴纸；没有贴切的就自然说。")
    if info.get("reason") == "quota_encourage":
        return "大约 10 条里 2–3 条带表情/贴纸/emoji；这次有贴切的就发，不用刻意每条都发。"
    return ""
