# -*- coding: utf-8 -*-
"""S3 图片理解：本地图片 -> 结构化 caption -> ``media_understanding``。

Batch 3 / Agent B 契约::

    describe_image(path, client, attachment_id="", existing_caption="") -> dict
    structured_caption(path, client) -> dict
    list_media(path, limit=20) -> list[dict]

约定：
- ``describe_image`` / ``structured_caption`` 的 ``path`` 必须是本地图片文件；
  只接受 PNG/JPEG/GIF/WebP/BMP 魔数，拒绝 URL / data URL / 目录 / 非图片。
- ``list_media`` 的 ``path`` 是 selflearn SQLite 路径。
- 没有可用 client 时 fail-closed：返回 ``{"ok": False, ...}``，不写库。
- 不发起网络请求，不 import wxauto；本地图片只编码成 data URL 交给注入的 client。
- 落库使用模块级 ``DB_PATH``。测试可
  ``monkeypatch.setattr(image_understanding, "DB_PATH", tmp_db)`` 隔离临时库。
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH
from .llm import parse_json

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_CAPTION_CHARS = 500
MAX_TAGS = 8
MAX_TAG_CHARS = 30
DEFAULT_CONFIDENCE = 0.6
DEFAULT_EMOTION = "neutral"
PURPOSE = "selflearn_image_understanding"

_REMOTE_PREFIXES = ("http://", "https://", "data:", "file://", "ftp://",
                    "//", "\\\\")

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)

_PROMPT = (
    "请仔细看这张图片，输出适合聊天使用的结构化理解。"
    "只描述图片里确实存在的内容，不猜测拍摄者身份、地点或隐私。"
    "confidence 是 0 到 1 的小数。只输出一个 JSON 对象，不要 markdown："
    '{"caption":"一句中文描述，不超过 120 字",'
    '"tags":["3-6 个主题词"],'
    '"emotion":"happy/sad/angry/neutral",'
    '"confidence":0.85}'
)


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": message}


def _text(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    return ""


def _sniff_mime(head: bytes) -> str:
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _prepare_image(path: Path | str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """校验本地图片并编码成 data URL；失败返回 (None, error)。"""
    if isinstance(path, Path):
        raw = str(path)
    elif isinstance(path, str):
        raw = path.strip()
    else:
        return None, _error("invalid_path", "图片路径必须是本地字符串或 Path")
    if not raw:
        return None, _error("invalid_path", "图片路径为空")
    if raw.lower().startswith(_REMOTE_PREFIXES):
        return None, _error("unsupported_path", "只接受本地图片路径，不接受 URL/data URL")

    try:
        target = Path(raw).expanduser()
        target = target.resolve()
    except (OSError, RuntimeError, ValueError):
        return None, _error("invalid_path", "图片路径无法解析")
    try:
        if not target.exists():
            return None, _error("not_found", "图片文件不存在")
        if not target.is_file():
            return None, _error("not_file", "图片路径不是普通文件")
        # 只读上限 +1，既判空/超限，也避免被超大文件撑爆内存
        with target.open("rb") as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
    except OSError:
        return None, _error("unreadable", "图片文件无法读取")
    if not data:
        return None, _error("empty_file", "图片文件为空")
    if len(data) > MAX_IMAGE_BYTES:
        return None, _error("too_large", "图片超过 12MB")
    mime = _sniff_mime(data[:16])
    if not mime:
        return None, _error("not_image", "不是可识别的图片文件")
    payload = base64.b64encode(data).decode("ascii")
    return {"path": target, "mime": mime, "size": len(data),
            "data_url": f"data:{mime};base64,{payload}"}, None


def _client_ready(client: Any) -> bool:
    if client is None:
        return False
    available = getattr(client, "available", None)
    if available is None:
        return True
    try:
        return bool(available())
    except Exception:  # noqa: BLE001 - client 自身异常按不可用处理
        return False


def _messages(info: dict[str, Any], existing_caption: str = "") -> list[dict[str, Any]]:
    prompt = _PROMPT
    existing = _text(existing_caption)[:MAX_CAPTION_CHARS]
    if existing:
        prompt += "\n已有描述（可能不准确，仅供对照修正）：" + existing
    return [
        {"role": "system",
         "content": "你是图片理解模块，只描述图片里确实存在的内容，只输出 JSON。"},
        {"role": "user",
         "content": [
             {"type": "text", "text": prompt},
             {"type": "image_url", "image_url": {"url": info["data_url"]}},
         ]},
    ]


def _normalise_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        parts: list[Any] = [p for p in value.replace("，", ",").replace("；", ",")
                            .replace(";", ",").replace("#", " ").split(",")]
        value = [p for part in parts for p in str(part).split()]
    if not isinstance(value, (list, tuple, set)):
        return []
    tags: list[str] = []
    for item in value:
        tag = _text(item).lstrip("#").strip()
        if not tag or tag in tags:
            continue
        tags.append(tag[:MAX_TAG_CHARS])
        if len(tags) >= MAX_TAGS:
            break
    return tags


def _confidence(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE
    if not math.isfinite(score):
        return DEFAULT_CONFIDENCE
    return max(0.0, min(1.0, score))


def _structure(client: Any, info: dict[str, Any],
               existing_caption: str = "") -> dict[str, Any]:
    try:
        result = client.chat(
            _messages(info, existing_caption),
            purpose=PURPOSE, max_tokens=500, temperature=0.2, json_mode=True,
        )
    except Exception as exc:  # noqa: BLE001 - 外部 client 不可信，fail-closed
        return _error("client_error",
                      f"图片理解 client 调用失败：{type(exc).__name__}: {str(exc)[:120]}")
    if not isinstance(result, dict):
        return _error("llm_failed", "图片理解 client 返回格式不正确")
    if not result.get("ok"):
        return _error("llm_failed", str(result.get("error") or "图片理解失败")[:200])

    obj = parse_json(str(result.get("text") or ""))
    if not isinstance(obj, dict):
        return _error("invalid_output", "图片理解结果不是 JSON 对象")

    caption = _text(obj.get("caption")) or _text(obj.get("description"))
    if not caption:
        return _error("empty_caption", "图片理解结果缺少 caption")
    raw_tags = obj.get("tags")
    if raw_tags is None:
        raw_tags = obj.get("keywords")
    emotion = _text(obj.get("emotion"))[:24] or DEFAULT_EMOTION
    return {
        "ok": True,
        "caption": caption[:MAX_CAPTION_CHARS],
        "tags": _normalise_tags(raw_tags),
        "emotion": emotion,
        "confidence": _confidence(obj.get("confidence", DEFAULT_CONFIDENCE)),
        "path": str(info["path"]),
        "mime": info["mime"],
    }


def structured_caption(path: Path | str, client: Any) -> dict[str, Any]:
    """把本地图片交给 client，返回结构化 caption（不写库）。"""
    info, err = _prepare_image(path)
    if err is not None:
        return err
    if not _client_ready(client):
        return _error("client_unavailable", "没有可用的图片理解 client")
    return _structure(client, info or {})


def _media_id(key: str) -> str:
    return "MU-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:14]


def describe_image(path: Path | str, client: Any, attachment_id: str = "",
                   existing_caption: str = "", db_path: Path | str | None = None) -> dict[str, Any]:
    """理解本地图片并写入 ``media_understanding``（同一附件/路径幂等更新）。"""
    info, err = _prepare_image(path)
    if err is not None:
        return err
    if not _client_ready(client):
        return _error("client_unavailable", "没有可用的图片理解 client")
    result = _structure(client, info or {}, existing_caption)
    if not result.get("ok"):
        return result

    aid = str(attachment_id or "").strip()[:128]
    key = aid or os.path.normcase(str(result["path"]))
    media_id = _media_id(key)
    now = time.time()
    con = None
    try:
        con = db.connect(db_path or DB_PATH)
        con.execute(
            "INSERT INTO media_understanding"
            "(id,attachment_id,path,caption,tags_json,emotion,confidence,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "attachment_id=excluded.attachment_id, path=excluded.path, "
            "caption=excluded.caption, tags_json=excluded.tags_json, "
            "emotion=excluded.emotion, confidence=excluded.confidence, "
            "status=excluded.status, created_at=excluded.created_at",
            (media_id, aid, result["path"], result["caption"],
             json.dumps(result["tags"], ensure_ascii=False), result["emotion"],
             float(result["confidence"]), "active", now),
        )
        con.commit()
    except (sqlite3.Error, OSError) as exc:
        return _error("db_error", f"写入 media_understanding 失败：{type(exc).__name__}")
    finally:
        if con is not None:
            con.close()

    return {
        "ok": True,
        "id": media_id,
        "attachment_id": aid,
        "path": result["path"],
        "caption": result["caption"],
        "tags": list(result["tags"]),
        "emotion": result["emotion"],
        "confidence": result["confidence"],
        "status": "active",
        "created_at": now,
    }


def _load_tags(raw: Any) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for item in value:
        tag = str(item).strip()[:MAX_TAG_CHARS]
        if tag:
            tags.append(tag)
    return tags


def list_media(path: Path | str, limit: int = 20) -> list[dict[str, Any]]:
    """按 created_at 倒序列出 ``media_understanding``，``tags_json`` 解析成 ``tags``。"""
    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = 20
    size = max(0, min(size, 200))
    if size == 0:
        return []
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT id,attachment_id,path,caption,tags_json,emotion,confidence,"
            "status,created_at FROM media_understanding "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (size,),
        ).fetchall()
    finally:
        con.close()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["tags"] = _load_tags(item.get("tags_json"))
        items.append(item)
    return items
