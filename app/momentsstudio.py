# -*- coding: utf-8 -*-
"""Moments Studio 后端：只读抓取微信朋友圈、缓存、列表/详情、媒体文件定位。

数据来源：`SoulAgent/tools/moments_fetch.py`（用 wechatauto.MomentDB 只读 sns.db）。
缓存：`idea/.data/moments/<YYYY-MM-DD>.jsonl`，按动态 id/tid 去重。
绝不点赞、评论、发布；所有缓存写操作走 audit.record。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from . import audit, paths

WX_PYTHON = os.environ.get("IDEA_WX_PYTHON", r"E:/agent/venv_wx312/Scripts/python.exe")
FETCH_TIMEOUT_S = 180
_MD5_RE = re.compile(r"^[0-9a-fA-F]{16,64}$")
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def _soul_root() -> Path:
    return Path(paths.soulagent_home()).resolve()


def _cache_dir() -> Path:
    target = Path(paths.DATA_DIR) / "moments"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _today_cache() -> Path:
    return _cache_dir() / f"{time.strftime('%Y-%m-%d')}.jsonl"


def _key(item: dict) -> str:
    return str(item.get("id") or item.get("tid") or "")


def _normalize_item(raw: dict, *, now: float | None = None) -> dict:
    images = []
    for image in raw.get("images") or []:
        if not isinstance(image, dict):
            continue
        md5 = str(image.get("md5") or "")
        local = media_path(md5) if md5 else None
        images.append({"md5": md5, "url": str(image.get("url") or ""),
                       "size": image.get("size"), "width": image.get("width"),
                       "height": image.get("height"),
                       "local_path": str(local) if local else ""})
    return {
        "id": str(raw.get("id") or ""),
        "tid": str(raw.get("tid") or ""),
        "username": str(raw.get("username") or ""),
        "nickname": str(raw.get("nickname") or ""),
        "text": str(raw.get("text") or ""),
        "create_time": int(raw.get("create_time") or 0),
        "images": images,
        "videos": raw.get("videos") or [],
        "likes": raw.get("likes") or [],
        "comments": raw.get("comments") or [],
        "location": raw.get("location") or {},
        "fetched_at": round(float(now if now is not None else time.time()), 3),
    }


def _load_cache_file(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and _key(item):
                rows.append(item)
    except OSError:
        return []
    return rows


def _load_cache() -> dict[str, dict]:
    merged: dict[str, dict] = {}
    try:
        files = sorted(_cache_dir().glob("*.jsonl"))
    except OSError:
        return merged
    for path in files:
        for item in _load_cache_file(path):
            key = _key(item)
            if not key:
                continue
            old = merged.get(key)
            if old is None or int(item.get("fetched_at") or 0) >= int(old.get("fetched_at") or 0):
                merged[key] = item
    return merged


def _write_cache(items: list[dict]) -> Path:
    path = _today_cache()
    existing = {_key(item): item for item in _load_cache_file(path)}
    for item in items:
        key = _key(item)
        if key:
            existing[key] = item
    ordered = sorted(existing.values(), key=lambda x: (int(x.get("create_time") or 0), _key(x)),
                     reverse=True)
    text = "\n".join(json.dumps(item, ensure_ascii=False, default=str) for item in ordered)
    tmp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex[:6]}")
    tmp.write_text(text + ("\n" if text else ""), encoding="utf-8")
    os.replace(tmp, path)
    return path


def fetch(limit: int = 50) -> dict:
    """调 moments_fetch.py（只读），解析并合并到当日缓存。"""
    limit = max(1, min(500, int(limit or 50)))
    script = _soul_root() / "tools" / "moments_fetch.py"
    if not script.is_file():
        return {"ok": False, "error": "fetch_script_not_found", "count": 0, "items": []}
    cmd = [WX_PYTHON, str(script), "--limit", str(limit)]
    try:
        proc = subprocess.run(cmd, cwd=str(_soul_root()), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=FETCH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        audit.record("moments.fetch", str(script), actor="workbench",
                     meta={"limit": limit, "error": "timeout"})
        return {"ok": False, "error": "fetch_timeout_180s", "count": 0, "items": []}
    except Exception as exc:  # noqa: BLE001
        audit.record("moments.fetch", str(script), actor="workbench",
                     meta={"limit": limit, "error": str(exc)})
        return {"ok": False, "error": f"fetch_failed:{type(exc).__name__}: {exc}",
                "count": 0, "items": []}
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[-1000:]
        audit.record("moments.fetch", str(script), actor="workbench",
                     meta={"limit": limit, "returncode": proc.returncode,
                           "error": err[:300]})
        return {"ok": False, "error": "fetch_command_failed",
                "stderr": err, "count": 0, "items": []}
    try:
        raw = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"bad_json:{exc}", "count": 0, "items": []}
    if isinstance(raw, dict):
        if raw.get("ok") is False:
            return {"ok": False, "error": str(raw.get("error") or "fetch_failed"),
                    "count": 0, "items": []}
        raw = raw.get("items") or []
    if not isinstance(raw, list):
        return {"ok": False, "error": "unexpected_output", "count": 0, "items": []}
    items = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            items.append(_normalize_item(row))
        except Exception:  # noqa: BLE001 - 单条失败不拖累整批
            continue
    cache = _write_cache(items)
    audit.record("moments.fetch", str(script), actor="workbench",
                 meta={"limit": limit, "count": len(items),
                       "cache": str(cache), "returncode": proc.returncode})
    return {"ok": True, "count": len(items), "items": items,
            "cache": str(cache)}


def list_moments(limit: int = 50, keyword: str = "", since: Any = None,
                 refresh: bool = False) -> dict:
    limit = max(1, min(500, int(limit or 50)))
    fetch_error = None
    if refresh:
        fetched = fetch(limit)
        if not fetched.get("ok"):
            fetch_error = fetched.get("error")
    merged = _load_cache()
    items = list(merged.values())
    kw = str(keyword or "").strip().casefold()
    if kw:
        items = [item for item in items
                 if kw in str(item.get("text") or "").casefold()
                 or kw in str(item.get("nickname") or "").casefold()
                 or kw in str(item.get("username") or "").casefold()]
    if since is not None and str(since).strip() != "":
        try:
            floor = float(since)
            items = [item for item in items if int(item.get("create_time") or 0) >= floor]
        except (TypeError, ValueError):
            pass
    items.sort(key=lambda item: (int(item.get("create_time") or 0), _key(item)),
               reverse=True)
    total = len(items)
    items = items[:limit]
    return {"ok": True, "count": len(items), "total": total, "items": items,
            "cached": True, **({"fetch_error": fetch_error} if fetch_error else {})}


def detail(tid: str) -> dict:
    key = str(tid or "").strip()
    if not key:
        return {"ok": False, "error": "tid_required"}
    merged = _load_cache()
    item = merged.get(key)
    if item is None:
        for candidate in merged.values():
            if key in (str(candidate.get("id") or ""), str(candidate.get("tid") or "")):
                item = candidate
                break
    if item is None:
        return {"ok": False, "error": "moment_not_found"}
    return {"ok": True, "item": item}


def _media_roots() -> list[Path]:
    roots = []
    env = os.environ.get("WECHATAUTO_MOMENTS_DIR")
    if env:
        roots.append(Path(env).expanduser())
    home = Path(os.path.expanduser("~"))
    roots += [home / "Documents" / "xwechat_files",
              home / "Documents" / "wechatauto_moments",
              home / "Documents" / "wechatauto_media"]
    result = []
    for root in roots:
        try:
            if root.is_dir():
                result.append(root.resolve())
        except OSError:
            continue
    return result


def media_path(md5: str) -> Path | None:
    """按 md5 找已解密图片；找不到返回 None（前端显示占位）。"""
    value = str(md5 or "").strip().lower()
    if not _MD5_RE.match(value):
        return None
    for root in _media_roots():
        # 优先朋友圈/聊天缓存里的 Img 目录，避免全盘递归
        directories = []
        try:
            directories.extend(root.rglob("Img"))
        except OSError:
            continue
        for directory in directories:
            try:
                for candidate in directory.rglob(value + "*"):
                    if candidate.is_file() and candidate.suffix.lower() in _IMAGE_SUFFIXES:
                        return candidate.resolve()
                    if candidate.is_file() and candidate.suffix.lower() == ".dat":
                        continue
            except OSError:
                continue
        # 下载器落盘目录也可能直接是 <md5>.jpg
        try:
            for candidate in root.rglob(value + "*"):
                if candidate.is_file() and candidate.suffix.lower() in _IMAGE_SUFFIXES:
                    return candidate.resolve()
        except OSError:
            continue
    return None
