# -*- coding: utf-8 -*-
"""P5-e 预留：缺素材的获取管线 **stub**（默认绝不联网、绝不下载）。

本任务只提供稳定接口与安全默认：
- ``search_wishes()``：读 ``agi/data/media_wishes.jsonl``（模型表达过但库里没有的愿望）；
- ``try_acquire(wish)``：默认返回 ``reserved``，**不发任何网络请求**；即使
  ``media_acquisition_enabled=True`` 也只返回待人工确认，不自动下载；
- ``add_to_library(path, name, tags)``：把外部人工确认过的图片加入 ``media/stickers/``，
  带扩展名白名单、大小校验与索引。

完整“搜索→下载→人工审核→入库”由 task-63（P5-e）实现。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from agi.selflearn import config
from agi.selflearn import emoji as emoji_archive

ROOT = Path(__file__).resolve().parents[1]
WISH_FILE = ROOT / "agi" / "data" / "media_wishes.jsonl"
LIBRARY_DIR = ROOT / "media" / "stickers"
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_BYTES = 10 * 1024 * 1024


def _enabled() -> bool:
    return bool(getattr(config, "MEDIA_ACQUISITION_ENABLED", False))


def wishes_path() -> Path:
    env = os.environ.get("MEDIA_WISH_FILE")
    return Path(env) if env else Path(getattr(config, "MEDIA_WISH_FILE", WISH_FILE))


def search_wishes(path: Path | str | None = None) -> list[dict]:
    """读取 wish 记录（坏行跳过，不抛异常）。"""
    target = Path(path) if path else wishes_path()
    if not target.exists():
        return []
    wishes: list[dict] = []
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("wish"):
                    wishes.append(item)
    except OSError:
        return wishes
    return wishes


def try_acquire(wish: Any, *, now: float | None = None) -> dict:
    """预留接口：默认 reserved，绝不发网络请求/不下载。"""
    value = str(wish or "").strip()
    if not value:
        return {"ok": False, "status": "invalid", "reason": "empty_wish"}
    if not _enabled():
        return {"ok": False, "status": "reserved", "reason": "acquisition_not_enabled"}
    return {"ok": False, "status": "reserved", "reason": "manual_review_required"}


def add_to_library(path: Path | str, name: str, tags: list[str] | None = None) -> dict:
    """人工确认后把外部图片加入 media/stickers（白名单+大小校验+索引）。"""
    raw = Path(str(path or ""))
    clean_name = str(name or "").strip()
    if not raw.is_file():
        return {"ok": False, "status": "invalid", "reason": "file_not_found"}
    if raw.suffix.lower() not in ALLOWED_SUFFIXES:
        return {"ok": False, "status": "invalid", "reason": "suffix_not_allowed"}
    try:
        size = raw.stat().st_size
    except OSError:
        return {"ok": False, "status": "invalid", "reason": "stat_failed"}
    if size <= 0 or size > MAX_BYTES:
        return {"ok": False, "status": "invalid", "reason": "size_not_allowed"}
    if not clean_name or "/" in clean_name or "\\" in clean_name:
        return {"ok": False, "status": "invalid", "reason": "invalid_name"}
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    target = LIBRARY_DIR / f"{clean_name}{raw.suffix.lower()}"
    try:
        shutil.copy2(raw, target)
    except OSError as exc:
        return {"ok": False, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    result = emoji_archive.index_assets(None, str(LIBRARY_DIR), limit=500)
    return {"ok": True, "status": "added", "path": str(target),
            "name": clean_name, "tags": list(tags or []), "index": result}
