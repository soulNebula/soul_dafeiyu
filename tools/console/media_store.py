# -*- coding: utf-8 -*-
"""P17：素材库管理（media_assets 表 CRUD + 迁移导入）。

表在 ``agi.selflearn.db`` 的 memory schema 里（``media_assets``）；本模块只做
控制台/管理侧逻辑，聊天生成读取由 ``agi/selflearn/media_choice.py`` 适配。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from agi import media_acquisition
from agi.selflearn import config, db

ROOT = Path(__file__).resolve().parents[2]
MEDIA_DIR = config.DATA_DIR / "media"
#: P23：聊天图片目录，永不进入素材库扫描/导入
CHAT_MEDIA_DIR = config.DATA_DIR / "media_chat"
#: builtin 迁移扫描目录（现有素材分散在 3 个目录：微信表情资产 / stickers / 导入库）
MEDIA_SCAN_DIRS = (
    ROOT / "wx_core" / "emoji_assets",
    ROOT / "media" / "stickers",
    config.DATA_DIR / "media",
)
TRASH_DIR = config.DATA_DIR / "media_trash"
SOURCES_PATH = config.DATA_DIR.parent / "media_sources.json"
TYPES = ("sticker", "image", "gif")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_NAME = 30
MAX_CAPTION = 200
MAX_TAGS = 10
MAX_TAG_LEN = 20
DELETE_CONFIRM = "删除素材"


def library_db_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("MEDIA_LIBRARY_DB")
    return Path(env) if env else Path(config.DB_PATH)


def _connect(path: Path | str | None = None):
    return db.connect(library_db_path(path))


def _media_dir() -> Path:
    env = os.environ.get("MEDIA_LIBRARY_DIR")
    return Path(env) if env else Path(MEDIA_DIR)


def _norm_path(value) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(value or "")))
    except Exception:  # noqa: BLE001
        return str(value or "")


def _is_chat_media(path: Path) -> bool:
    try:
        path.resolve().relative_to(CHAT_MEDIA_DIR.resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


def _attachment_paths(con) -> set[str]:
    """attachments 表里已登记的聊天图片路径（防御：这些文件不导入素材库）。"""
    try:
        rows = con.execute("SELECT path FROM attachments").fetchall()
        return {_norm_path(row[0]) for row in rows if row and row[0]}
    except Exception:  # noqa: BLE001
        return set()


def _scan_dirs() -> list[Path]:
    """迁移扫描目录：测试用 MEDIA_LIBRARY_DIR 覆盖成单目录，否则扫全部现有素材目录。

    P23：聊天图目录 media_chat 永不扫描（即使 MEDIA_LIBRARY_DIR 误指过去）。
    """
    env = os.environ.get("MEDIA_LIBRARY_DIR")
    if env:
        return [p for p in (Path(env),) if not _is_chat_media(p)]
    return [Path(item) for item in MEDIA_SCAN_DIRS
            if not _is_chat_media(Path(item))]


def _trash_dir() -> Path:
    env = os.environ.get("MEDIA_TRASH_DIR")
    return Path(env) if env else Path(TRASH_DIR)


def _sources() -> dict:
    env = os.environ.get("MEDIA_SOURCES_FILE")
    path = Path(env) if env else Path(SOURCES_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _row_dict(row) -> dict:
    item = dict(row)
    try:
        tags = json.loads(str(item.get("tags_json") or "[]"))
    except (TypeError, ValueError):
        tags = []
    item["tags"] = [str(tag) for tag in tags] if isinstance(tags, list) else []
    item.pop("tags_json", None)
    item["enabled"] = bool(item.get("enabled"))
    item["builtin"] = bool(item.get("builtin"))
    path = str(item.get("path") or "")
    item["thumb"] = "/api/media/file?path=" + quote(path) if Path(path).is_file() else ""
    item["file_exists"] = Path(path).is_file()
    return item


def _validate_type(value) -> str:
    text = str(value or "").strip().lower()
    if text not in TYPES:
        raise ValueError(f"type 只能是 {'/'.join(TYPES)}")
    return text


def _validate_name(value) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("name 不能为空")
    if len(text) > MAX_NAME:
        raise ValueError(f"name 超过 {MAX_NAME} 字")
    return text


def _validate_caption(value) -> str:
    text = str(value if value is not None else "").strip()
    if len(text) > MAX_CAPTION:
        raise ValueError(f"caption 超过 {MAX_CAPTION} 字")
    return text


def _validate_tags(value) -> list[str]:
    if isinstance(value, str):
        value = [item.strip() for item in re.split(r"[,，\n]", value)]
    if value is None:
        value = []
    if not isinstance(value, list):
        raise ValueError("tags 需要字符串数组")
    tags: list[str] = []
    for item in value:
        text = str(item or "").strip()[:MAX_TAG_LEN]
        if text and text not in tags:
            tags.append(text)
    if len(tags) > MAX_TAGS:
        raise ValueError(f"tags 最多 {MAX_TAGS} 个")
    return tags


def ensure_imported(path: Path | str | None = None, *, force: bool = False) -> dict:
    """把 media 目录里的现有素材幂等导入 media_assets（builtin=1）。"""
    con = _connect(path)
    try:
        table = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='media_assets'"
        ).fetchone()
        if table is None:
            db.ensure_schema(library_db_path(path)) if hasattr(db, "ensure_schema") else None
        total = int(con.execute("SELECT count(*) FROM media_assets").fetchone()[0])
        if total > 0 and not force:
            return {"ok": True, "imported": 0, "total": total, "cached": True}
        sources = _sources()
        by_path = {str(item.get("path") or ""): item for item in sources.values()
                   if isinstance(item, dict)}
        chat_paths = _attachment_paths(con)
        candidates: list[Path] = []
        for scan_dir in _scan_dirs():
            if not scan_dir.is_dir():
                continue
            try:
                candidates.extend(sorted(
                    item for item in scan_dir.rglob("*")
                    if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES))
            except OSError:
                continue
        imported = 0
        now = time.time()
        with con:
            for file_path in candidates:
                if _norm_path(file_path) in chat_paths or _is_chat_media(file_path):
                    continue
                try:
                    raw = file_path.read_bytes()
                except OSError:
                    continue
                digest = hashlib.sha256(raw).hexdigest()
                existing = con.execute(
                    "SELECT id FROM media_assets WHERE hash = ?", (digest,)).fetchone()
                if existing is not None:
                    continue
                record = by_path.get(str(file_path)) or next(
                    (item for item in sources.values()
                     if isinstance(item, dict)
                     and Path(str(item.get("path") or "")).name == file_path.name), {})
                name = str(record.get("name") or file_path.stem)[:MAX_NAME]
                tags = record.get("tags") if isinstance(record.get("tags"), list) else []
                asset_type = "gif" if file_path.suffix.lower() == ".gif" else "sticker"
                con.execute(
                    "INSERT INTO media_assets(id,hash,path,type,name,caption,tags_json,enabled,"
                    "builtin,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,1,?,?)",
                    ("MA-" + digest[:14], digest, str(file_path), asset_type, name.strip(),
                     "", json.dumps([str(tag) for tag in tags], ensure_ascii=False),
                     now, now))
                imported += 1
        total = int(con.execute("SELECT count(*) FROM media_assets").fetchone()[0])
    finally:
        con.close()
    return {"ok": True, "imported": imported, "total": total, "cached": False}


def list_assets(type_: str = "", tag: str = "", q: str = "", enabled: str = "",
                limit: int = 100, offset: int = 0, path: Path | str | None = None) -> dict:
    ensure_imported(path)
    try:
        limit = max(1, min(500, int(limit)))
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        limit, offset = 100, 0
    where = []
    params: list = []
    if type_:
        try:
            where.append("type = ?")
            params.append(_validate_type(type_))
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "items": [], "total": 0}
    if tag:
        where.append("tags_json LIKE ?")
        params.append(f"%{str(tag).strip()}%")
    if q:
        where.append("(name LIKE ? OR caption LIKE ? OR tags_json LIKE ?)")
        like = f"%{str(q).strip()}%"
        params.extend([like, like, like])
    if enabled in ("0", "1", "false", "true"):
        where.append("enabled = ?")
        params.append(1 if enabled in ("1", "true") else 0)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    con = _connect(path)
    try:
        total = int(con.execute(
            f"SELECT count(*) FROM media_assets{clause}", params).fetchone()[0])
        rows = con.execute(
            f"SELECT * FROM media_assets{clause} ORDER BY builtin DESC, created_at DESC, "
            "rowid DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "items": [], "total": 0}
    finally:
        con.close()
    return {"ok": True, "items": [_row_dict(row) for row in rows], "total": total,
            "limit": limit, "offset": offset}


def get_asset(asset_id: str, path: Path | str | None = None) -> dict:
    ensure_imported(path)
    con = _connect(path)
    try:
        row = con.execute("SELECT * FROM media_assets WHERE id = ?",
                          (str(asset_id or ""),)).fetchone()
    finally:
        con.close()
    if row is None:
        return {"ok": False, "error": "asset_not_found"}
    return {"ok": True, "asset": _row_dict(row)}


def create_asset(*, data: bytes | None = None, file_path: str | None = None,
                 type_: str = "sticker", name: str = "", caption: str = "",
                 tags: list[str] | None = None, path: Path | str | None = None) -> dict:
    ensure_imported(path)
    try:
        asset_type = _validate_type(type_)
        clean_name = _validate_name(name)
        clean_caption = _validate_caption(caption)
        clean_tags = _validate_tags(tags)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if data:
        imported = media_acquisition.import_local(
            data, "library_upload.png", name=clean_name, tags=clean_tags,
            source="console_library")
    elif file_path:
        imported = media_acquisition.import_local_file(
            file_path, name=clean_name, tags=clean_tags, source="console_library")
    else:
        return {"ok": False, "error": "需要 data 或 path"}
    if not imported.get("ok"):
        return {"ok": False, "error": imported.get("reason") or imported.get("status")
                or "import_failed", "import": imported}
    digest = str(imported.get("hash") or "")
    con = _connect(path)
    try:
        row = con.execute("SELECT * FROM media_assets WHERE hash = ?", (digest,)).fetchone()
        if row is not None:
            return {"ok": True, "duplicate": True, "asset": _row_dict(row)}
        now = time.time()
        asset_id = "MA-" + digest[:14]
        with con:
            con.execute(
                "INSERT INTO media_assets(id,hash,path,type,name,caption,tags_json,enabled,"
                "builtin,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,0,?,?)",
                (asset_id, digest, str(imported.get("path") or ""), asset_type,
                 clean_name, clean_caption,
                 json.dumps(clean_tags, ensure_ascii=False), now, now))
        row = con.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone()
    finally:
        con.close()
    return {"ok": True, "duplicate": False, "asset": _row_dict(row)}


def update_asset(asset_id: str, updates: dict, path: Path | str | None = None) -> dict:
    if not isinstance(updates, dict) or not updates:
        return {"ok": False, "error": "没有要修改的字段"}
    clean: dict = {}
    errors: list[str] = []
    for key, value in updates.items():
        try:
            if key == "type":
                clean["type"] = _validate_type(value)
            elif key == "name":
                clean["name"] = _validate_name(value)
            elif key == "caption":
                clean["caption"] = _validate_caption(value)
            elif key == "tags":
                clean["tags_json"] = json.dumps(_validate_tags(value), ensure_ascii=False)
            elif key == "enabled":
                clean["enabled"] = 1 if bool(value) else 0
            else:
                errors.append(f"not_allowed:{key}")
        except ValueError as exc:
            errors.append(f"{key}: {exc}")
    if errors:
        return {"ok": False, "errors": errors}
    ensure_imported(path)
    con = _connect(path)
    try:
        row = con.execute("SELECT * FROM media_assets WHERE id = ?",
                          (str(asset_id or ""),)).fetchone()
        if row is None:
            return {"ok": False, "error": "asset_not_found"}
        clean["updated_at"] = time.time()
        sets = ", ".join(f"{key} = ?" for key in clean)
        with con:
            con.execute(f"UPDATE media_assets SET {sets} WHERE id = ?",
                        (*clean.values(), str(asset_id)))
        updated = con.execute("SELECT * FROM media_assets WHERE id = ?",
                              (str(asset_id),)).fetchone()
    finally:
        con.close()
    return {"ok": True, "asset": _row_dict(updated)}


def delete_asset(asset_id: str, *, hard: bool = False, confirm: str = "",
                 path: Path | str | None = None) -> dict:
    if str(confirm or "").strip() != DELETE_CONFIRM:
        return {"ok": False, "error": "confirm_phrase_mismatch",
                "hint": f"必须输入：{DELETE_CONFIRM}"}
    ensure_imported(path)
    con = _connect(path)
    try:
        row = con.execute("SELECT * FROM media_assets WHERE id = ?",
                          (str(asset_id or ""),)).fetchone()
    finally:
        con.close()
    if row is None:
        return {"ok": False, "error": "asset_not_found"}
    item = _row_dict(row)
    if not hard:
        con = _connect(path)
        try:
            with con:
                con.execute("UPDATE media_assets SET enabled = 0, updated_at = ? WHERE id = ?",
                            (time.time(), str(asset_id)))
        finally:
            con.close()
        return {"ok": True, "mode": "soft", "id": str(asset_id),
                "note": "已软删除（enabled=0，可恢复）"}
    source = Path(str(item.get("path") or ""))
    trash_dir = _trash_dir()
    trash_dir.mkdir(parents=True, exist_ok=True)
    target = trash_dir / source.name
    suffix = 1
    while target.exists():
        target = trash_dir / f"{source.stem}_{suffix}{source.suffix}"
        suffix += 1
    moved = ""
    if source.is_file():
        try:
            shutil.move(str(source), str(target))
            moved = str(target)
        except OSError as exc:
            return {"ok": False, "error": f"move_failed:{type(exc).__name__}: {exc}"}
    con = _connect(path)
    try:
        with con:
            con.execute("DELETE FROM media_assets WHERE id = ?", (str(asset_id),))
            try:
                con.execute("DELETE FROM emoji_archive WHERE path = ?",
                            (str(item.get("path") or ""),))
            except Exception:  # noqa: BLE001
                pass
    finally:
        con.close()
    return {"ok": True, "mode": "hard", "id": str(asset_id), "moved_to": moved,
            "note": "已彻底删除（文件移到 media_trash/，索引已移除）"}


def restore_asset(asset_id: str, path: Path | str | None = None) -> dict:
    ensure_imported(path)
    con = _connect(path)
    try:
        row = con.execute("SELECT * FROM media_assets WHERE id = ?",
                          (str(asset_id or ""),)).fetchone()
        if row is None:
            return {"ok": False, "error": "asset_not_found"}
        with con:
            con.execute("UPDATE media_assets SET enabled = 1, updated_at = ? WHERE id = ?",
                        (time.time(), str(asset_id)))
        updated = con.execute("SELECT * FROM media_assets WHERE id = ?",
                              (str(asset_id),)).fetchone()
    finally:
        con.close()
    return {"ok": True, "asset": _row_dict(updated)}


def stats(path: Path | str | None = None) -> dict:
    ensure_imported(path)
    con = _connect(path)
    try:
        total = int(con.execute("SELECT count(*) FROM media_assets").fetchone()[0])
        enabled = int(con.execute(
            "SELECT count(*) FROM media_assets WHERE enabled = 1").fetchone()[0])
        by_type = {row[0]: int(row[1]) for row in con.execute(
            "SELECT type, count(*) FROM media_assets WHERE enabled = 1 GROUP BY type")}
        builtin = int(con.execute(
            "SELECT count(*) FROM media_assets WHERE builtin = 1").fetchone()[0])
    finally:
        con.close()
    return {"ok": True, "total": total, "enabled": enabled,
            "soft_deleted": max(0, total - enabled),
            "by_type": {name: by_type.get(name, 0) for name in TYPES},
            "builtin": builtin}
