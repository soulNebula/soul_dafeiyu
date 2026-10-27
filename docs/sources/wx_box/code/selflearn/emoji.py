# -*- coding: utf-8 -*-
"""S5 本地表情包档案：索引本地资产、记录使用、统计与列表。

Batch 4 / Agent A 契约::

    index_assets(path, asset_dir, limit=200) -> dict
    record_use(path, emoji_name, message_id="", context="") -> dict
    stats(path) -> dict
    list_emojis(path, limit=50) -> list[dict]

边界：
- 只扫描本地目录里扩展名为 PNG/JPG/GIF/WebP 的普通文件；
- 不下载、不联网、不读取任何旧客户数据；
- ``emoji_archive`` 按本地绝对路径幂等写入，重复索引不会产生重复行；
- ``emoji_usage`` 每次使用写一条事件，并同步 ``usage_count``。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
DEFAULT_INDEX_LIMIT = 200
DEFAULT_LIST_LIMIT = 50
MAX_TEXT_CHARS = 1000


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": message}


def _db_path(path: Path | str | None) -> Path | str:
    """默认库在调用时解析，便于测试 monkeypatch ``DB_PATH``。"""
    return DB_PATH if path is None else path


def _as_limit(value: Any, default: int) -> int:
    """把 limit 规整成非负整数；非法值回退默认值。"""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, number)


def _archive_id(path_key: str) -> str:
    digest = hashlib.sha1(path_key.encode("utf-8", "surrogatepass")).hexdigest()
    return "EMO-" + digest[:14]


def _usage_id() -> str:
    return "EU-" + uuid.uuid4().hex[:16]


def _text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _tags(raw: Any) -> list[str]:
    """解析 tags_json；坏数据按空列表处理，保持列表接口稳定。"""
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        tag = str(item).strip()
        if tag and tag not in out:
            out.append(tag)
        if len(out) >= 32:
            break
    return out


def _item(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["tags"] = _tags(item.get("tags_json"))
    item["usage_count"] = int(item.get("usage_count") or 0)
    item["filename"] = Path(str(item.get("path") or "")).name
    item["extension"] = Path(str(item.get("path") or "")).suffix.lower()
    return item


def _scan(asset_dir: Path) -> tuple[list[Path], int]:
    """递归扫描本地目录，返回 (支持的图片, 不支持的普通文件数)。"""
    files: list[Path] = []
    unsupported = 0
    for current, dirnames, filenames in os.walk(asset_dir, followlinks=False):
        dirnames.sort(key=str.casefold)
        for filename in sorted(filenames, key=str.casefold):
            candidate = Path(current) / filename
            try:
                if not candidate.is_file():
                    continue
            except OSError:
                continue
            if candidate.suffix.lower() in IMAGE_SUFFIXES:
                files.append(candidate)
            else:
                unsupported += 1
    files.sort(key=lambda p: (str(p.relative_to(asset_dir)).casefold(), str(p)))
    return files, unsupported


def _upsert(con: sqlite3.Connection, file_path: Path, now: float) -> tuple[dict[str, Any], bool]:
    """按绝对路径幂等写入一行；返回 (行, 是否新增)。"""
    target = file_path.resolve()
    stored = str(target)
    existing = con.execute(
        "SELECT * FROM emoji_archive WHERE path = ? COLLATE NOCASE "
        "ORDER BY created_at ASC, rowid ASC LIMIT 1",
        (stored,),
    ).fetchone()
    name = target.stem or target.name
    if existing is None:
        emoji_id = _archive_id(os.path.normcase(stored))
        con.execute(
            "INSERT INTO emoji_archive"
            "(id,name,path,tags_json,usage_count,created_at) VALUES(?,?,?,?,?,?)",
            (emoji_id, name, stored, "[]", 0, now),
        )
        row = con.execute("SELECT * FROM emoji_archive WHERE id = ?", (emoji_id,)).fetchone()
        return _item(row), True

    # 同名文件/手工录入的旧行也复用原 id，避免把已有使用记录切断。
    con.execute(
        "UPDATE emoji_archive SET name = ?, path = ? WHERE id = ?",
        (name, stored, existing["id"]),
    )
    row = con.execute(
        "SELECT * FROM emoji_archive WHERE id = ?", (existing["id"],)
    ).fetchone()
    return _item(row), False


def index_assets(path: Path | str | None = None, asset_dir: Path | str = "",
                 limit: int = DEFAULT_INDEX_LIMIT) -> dict[str, Any]:
    """扫描本地表情资产并幂等写入 ``emoji_archive``。

    ``limit`` 限制本次处理的候选文件数（按相对路径排序后的前 N 个）；
    超出部分留在目录里，下次提高 limit 再索引。不会下载或读取远程内容。
    """
    if isinstance(asset_dir, Path):
        raw_dir = str(asset_dir)
    elif isinstance(asset_dir, str):
        raw_dir = asset_dir.strip()
    else:
        return _error("invalid_asset_dir", "asset_dir 必须是本地目录字符串或 Path")
    if not raw_dir:
        return _error("invalid_asset_dir", "asset_dir 为空")

    try:
        root = Path(raw_dir).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return _error("invalid_asset_dir", "asset_dir 无法解析")
    if not root.exists():
        return _error("asset_dir_not_found", "本地资产目录不存在")
    if not root.is_dir():
        return _error("not_directory", "asset_dir 不是目录")

    size = _as_limit(limit, DEFAULT_INDEX_LIMIT)
    try:
        candidates, unsupported = _scan(root)
    except OSError:
        return _error("scan_failed", "本地资产目录扫描失败")

    selected = candidates[:size]
    now = time.time()
    added = 0
    updated = 0
    items: list[dict[str, Any]] = []
    total = 0
    con = None
    try:
        con = db.connect(_db_path(path))
        with con:
            for candidate in selected:
                item, is_new = _upsert(con, candidate, now)
                items.append(item)
                if is_new:
                    added += 1
                else:
                    updated += 1
            total = int(con.execute("SELECT count(*) FROM emoji_archive").fetchone()[0])
    except (sqlite3.Error, OSError) as exc:
        return _error("db_error", f"写入表情档案失败：{type(exc).__name__}: {exc}"[:240])
    finally:
        if con is not None:
            con.close()

    return {
        "ok": True,
        "asset_dir": str(root),
        "limit": size,
        "scanned": len(candidates),
        "matched": len(selected),
        "remaining": max(0, len(candidates) - len(selected)),
        "indexed": added,
        "inserted": added,
        "added": added,
        "updated": updated,
        "existing": updated,
        "skipped": unsupported,
        "unsupported": unsupported,
        "count": added,
        "total": total,
        "items": items,
    }


def _find(con: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    row = con.execute(
        "SELECT * FROM emoji_archive WHERE name = ? "
        "ORDER BY usage_count DESC, created_at ASC, rowid ASC LIMIT 1",
        (key,),
    ).fetchone()
    if row is not None:
        return row
    stem = Path(key).stem
    if stem and stem != key:
        row = con.execute(
            "SELECT * FROM emoji_archive WHERE name = ? "
            "ORDER BY usage_count DESC, created_at ASC, rowid ASC LIMIT 1",
            (stem,),
        ).fetchone()
        if row is not None:
            return row
    # 也接受 emoji id，方便调用方直接使用列表返回值。
    return con.execute("SELECT * FROM emoji_archive WHERE id = ? LIMIT 1", (key,)).fetchone()


def record_use(path: Path | str | None = None, emoji_name: str = "",
               message_id: str = "", context: str = "") -> dict[str, Any]:
    """记录一次表情使用，写 ``emoji_usage`` 并把 ``usage_count`` 同步为真实次数。"""
    key = _text(emoji_name, 255)
    if not key:
        return _error("invalid_name", "emoji_name 不能为空")
    mid = _text(message_id, 128)
    ctx = _text(context, MAX_TEXT_CHARS)
    con = None
    try:
        con = db.connect(_db_path(path))
        row = _find(con, key)
        if row is None:
            return _error("not_found", "表情档案里没有这个名称")
        emoji_id = str(row["id"])
        usage_id = _usage_id()
        now = time.time()
        with con:
            con.execute(
                "INSERT INTO emoji_usage(id,emoji_id,message_id,context,created_at) "
                "VALUES(?,?,?,?,?)",
                (usage_id, emoji_id, mid, ctx, now),
            )
            count = int(con.execute(
                "SELECT count(*) FROM emoji_usage WHERE emoji_id = ?", (emoji_id,)
            ).fetchone()[0])
            con.execute(
                "UPDATE emoji_archive SET usage_count = ? WHERE id = ?",
                (count, emoji_id),
            )
    except (sqlite3.Error, OSError) as exc:
        return _error("db_error", f"记录表情使用失败：{type(exc).__name__}: {exc}"[:240])
    finally:
        if con is not None:
            con.close()

    return {
        "ok": True,
        "id": emoji_id,
        "emoji_id": emoji_id,
        "usage_id": usage_id,
        "name": str(row["name"]),
        "path": str(row["path"]),
        "message_id": mid,
        "context": ctx,
        "usage_count": count,
        "count": count,
        "created_at": now,
    }


def stats(path: Path | str | None = None) -> dict[str, Any]:
    """返回档案总量、使用总量、被使用/未使用数量与使用最多的表情。"""
    con = None
    try:
        con = db.connect(_db_path(path))
        total = int(con.execute("SELECT count(*) FROM emoji_archive").fetchone()[0])
        uses = int(con.execute("SELECT count(*) FROM emoji_usage").fetchone()[0])
        used = int(con.execute(
            "SELECT count(DISTINCT emoji_id) FROM emoji_usage"
        ).fetchone()[0])
        top_rows = con.execute(
            "SELECT * FROM emoji_archive WHERE usage_count > 0 "
            "ORDER BY usage_count DESC, name COLLATE NOCASE ASC, rowid ASC LIMIT 5"
        ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        return _error("db_error", f"读取表情统计失败：{type(exc).__name__}: {exc}"[:240])
    finally:
        if con is not None:
            con.close()

    top = [_item(row) for row in top_rows]
    unused = max(0, total - used)
    return {
        "ok": True,
        "total": total,
        "total_emojis": total,
        "emojis": total,
        "emoji_count": total,
        "assets": total,
        "count": total,
        "uses": uses,
        "usage": uses,
        "usage_count": uses,
        "use_count": uses,
        "total_usage": uses,
        "total_uses": uses,
        "usage_events": uses,
        "used": used,
        "used_emojis": used,
        "unused": unused,
        "unused_emojis": unused,
        "top": top,
        "top_emojis": top,
    }


def list_emojis(path: Path | str | None = None,
                limit: int = DEFAULT_LIST_LIMIT) -> list[dict[str, Any]]:
    """按使用次数倒序（同次数按名称）列出表情档案，tags_json 解析为 tags。"""
    size = _as_limit(limit, DEFAULT_LIST_LIMIT)
    if size == 0:
        return []
    con = db.connect(_db_path(path))
    try:
        rows = con.execute(
            "SELECT * FROM emoji_archive "
            "ORDER BY usage_count DESC, name COLLATE NOCASE ASC, rowid ASC LIMIT ?",
            (size,),
        ).fetchall()
    finally:
        con.close()
    return [_item(row) for row in rows]
