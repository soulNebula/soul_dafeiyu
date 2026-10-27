# -*- coding: utf-8 -*-
"""01 媒体持久化：图片落盘、关联消息、可回查。"""
from __future__ import annotations

import base64
import hashlib
import re
import time
from pathlib import Path

from . import db
from .config import DATA_DIR, DB_PATH

MEDIA_DIR = DATA_DIR / "media"
#: P23：聊天图片与素材库物理分离；save_image 只落这里。
CHAT_MEDIA_DIR = DATA_DIR / "media_chat"
MAX_BYTES = 12 * 1024 * 1024
_DATA_RE = re.compile(r"^data:image/(png|jpeg|jpg|webp|gif);base64,(.+)$", re.S)


def chat_media_dir() -> Path:
    """聊天图片目录（不进素材库扫描）。"""
    return CHAT_MEDIA_DIR


def library_media_dir() -> Path:
    """素材库目录（只放人工/导入素材；聊天图不再写这里）。"""
    return MEDIA_DIR


def save_image(data_url: str, message_id: str | None = None,
               path: Path | str = DB_PATH) -> dict:
    m = _DATA_RE.match(str(data_url or ""))
    if not m:
        raise ValueError("不是合法的图片 data URL")
    subtype, payload = m.group(1).lower(), m.group(2)
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"图片 base64 解码失败：{exc}") from exc
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("图片为空或超过 12MB")
    sha = hashlib.sha256(raw).hexdigest()
    ext = "jpg" if subtype in ("jpeg", "jpg") else subtype
    CHAT_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    target = CHAT_MEDIA_DIR / f"{sha[:24]}.{ext}"
    if not target.exists():
        target.write_bytes(raw)
    aid = "ATT-" + sha[:16]
    con = db.connect(path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO attachments(id,message_id,kind,mime,path,bytes,sha256,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (aid, message_id, "image", f"image/{subtype}", str(target), len(raw), sha, time.time()))
        con.commit()
    finally:
        con.close()
    return {"id": aid, "path": str(target), "mime": f"image/{subtype}",
            "bytes": len(raw), "sha256": sha}


def list_attachments(path: Path | str = DB_PATH, limit: int = 30) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM attachments ORDER BY created_at DESC LIMIT ?", (int(limit),))]
    finally:
        con.close()
