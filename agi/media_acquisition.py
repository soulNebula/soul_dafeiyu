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

import hashlib
import json
import os
import shutil
import time
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

# --------------------------------------------------------------------------- #
# P5-e-B：本地导入（不联网）
# --------------------------------------------------------------------------- #
IMPORT_DIR = ROOT / "agi" / "data" / "selflearn" / "media"
MEDIA_SOURCES_PATH = ROOT / "agi" / "data" / "media_sources.json"
WISH_STATUS_FILE = ROOT / "agi" / "data" / "media_wishes_status.json"
#: 路径导入白名单（可 monkeypatch；桌面目录兼容英文 Desktop）
IMPORT_PATH_ROOTS: tuple[Path, ...] = (
    IMPORT_DIR,
    Path.home() / "Desktop",
    ROOT / "media",
)
#: 单文件上限 2MB（与任务约定一致；add_to_library 仍保留 10MB 旧口径）
IMPORT_MAX_BYTES = 2 * 1024 * 1024
IMPORT_ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_FORMAT_EXT = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}
#: 索引进哪个 emoji_archive 库；None = 默认 selflearn 库（测试可 monkeypatch）
IMPORT_INDEX_DB: Path | None = None


def _index_db_path() -> Path | str:
    return IMPORT_INDEX_DB if IMPORT_INDEX_DB else config.DB_PATH


def _import_max_bytes() -> int:
    try:
        value = int(getattr(config, "MEDIA_IMPORT_MAX_BYTES", IMPORT_MAX_BYTES))
    except (TypeError, ValueError):
        value = IMPORT_MAX_BYTES
    return value if value > 0 else IMPORT_MAX_BYTES


def sources_path() -> Path:
    env = os.environ.get("MEDIA_SOURCES_FILE")
    return Path(env) if env else Path(MEDIA_SOURCES_PATH)


def wish_status_path() -> Path:
    env = os.environ.get("MEDIA_WISH_STATUS_FILE")
    return Path(env) if env else Path(WISH_STATUS_FILE)


def _read_json_file(path: Path, default: dict) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(default)
    return raw if isinstance(raw, dict) else dict(default)


def _write_json_atomic(path: Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def _clean_name(value: str, fallback: str) -> str:
    import re
    text = re.sub(r"[^\w\u4e00-\u9fff\- .]", "", str(value or "")).strip()[:40]
    return text or fallback


def _clean_tags(tags: list[str] | None, *extras: str) -> list[str]:
    out: list[str] = []
    for item in list(tags or []) + [str(extra or "") for extra in extras]:
        for part in str(item).replace("，", ",").split(","):
            tag = part.strip()[:20]
            if tag and tag not in out:
                out.append(tag)
    return out[:12]


def _sniff_image(data: bytes, filename: str) -> tuple[dict | None, str]:
    """按内容 + 扩展名双校验；返回 ({"image":..., "format":...}, 错误码)。"""
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix not in IMPORT_ALLOWED_EXTS:
        return None, "suffix_not_allowed"
    if not data:
        return None, "empty_file"
    if len(data) > _import_max_bytes():
        return None, "too_large"
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return None, "pillow_missing"
    try:
        from io import BytesIO
        image = Image.open(BytesIO(data))
        image.load()
        fmt = str(image.format or "").upper()
        if fmt not in _FORMAT_EXT:
            return None, "bad_image_format"
        # 纯文本伪装成 svg/html 等：Pillow 打不开，已在上面失败
        return {"image": image, "format": fmt}, ""
    except Exception:  # noqa: BLE001
        return None, "not_an_image"


def _normalize_for_save(image, fmt: str):
    """剥离 EXIF/元数据后重新编码；GIF 保留动画帧。"""
    from PIL import Image
    if fmt == "GIF" and getattr(image, "is_animated", False):
        frames = []
        try:
            for frame in range(int(getattr(image, "n_frames", 1))):
                image.seek(frame)
                frames.append(image.convert("RGBA").copy())
        except Exception:  # noqa: BLE001
            frames = [image.convert("RGBA").copy()]
        if len(frames) <= 1:
            return frames[0], False
        return frames, True
    if fmt == "JPEG":
        return image.convert("RGB"), False
    if image.mode not in ("RGB", "RGBA", "P", "L", "LA"):
        return image.convert("RGB"), False
    return image.copy(), False


def _save_image(target: Path, image, fmt: str) -> None:
    from io import BytesIO
    from PIL import Image
    payload = BytesIO()
    normalized, animated = _normalize_for_save(image, fmt)
    save_kwargs: dict = {}
    if fmt == "JPEG":
        save_kwargs.update(quality=90, optimize=True)
    elif fmt == "WEBP":
        save_kwargs.update(quality=90)
    if animated and isinstance(normalized, list):
        first, *others = normalized
        first.save(payload, format=fmt, save_all=True, append_images=others,
                   **save_kwargs)
    else:
        normalized.save(payload, format=fmt, **save_kwargs)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(payload.getvalue())
    os.replace(tmp, target)


def import_local(data: bytes, filename: str, *, name: str = "",
                 tags: list[str] | None = None,
                 source: str = "local_import") -> dict:
    """本地导入一张图片：校验→去重→脱 EXIF 落盘→写 sources→索引。"""
    if not isinstance(data, (bytes, bytearray)) or not data:
        return {"ok": False, "status": "invalid", "reason": "empty_file"}
    payload = bytes(data)
    sniffed, reason = _sniff_image(payload, filename)
    if sniffed is None:
        return {"ok": False, "status": "invalid", "reason": reason}
    fmt = sniffed["format"]
    ext = _FORMAT_EXT[fmt]
    digest = hashlib.sha256(payload).hexdigest()

    target_dir = Path(IMPORT_DIR)
    existing = target_dir / f"{digest}{ext}"
    sources = _read_json_file(sources_path(), {})
    if existing.is_file() or digest in sources:
        path = existing if existing.is_file() else Path(str(sources[digest].get("path") or ""))
        return {"ok": True, "status": "duplicate", "duplicate": True,
                "path": str(path), "name": (sources.get(digest) or {}).get("name")
                or _clean_name(name, Path(filename).stem or digest[:8]),
                "hash": digest, "bytes": len(payload), "ext": ext,
                "tags": list((sources.get(digest) or {}).get("tags") or []),
                "indexed": bool((sources.get(digest) or {}).get("indexed"))}

    clean_tags = _clean_tags(tags, Path(filename).stem, name)
    clean_name = _clean_name(name, Path(filename).stem or digest[:8])
    target = target_dir / f"{digest}{ext}"
    try:
        _save_image(target, sniffed["image"], fmt)
        written_bytes = target.stat().st_size
    except Exception as exc:  # noqa: BLE001
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "status": "failed",
                "reason": f"save_failed:{type(exc).__name__}: {exc}"}

    indexed = False
    try:
        index_result = emoji_archive.index_assets(_index_db_path(), str(target_dir),
                                                  limit=500)
        indexed = bool(index_result.get("ok"))
    except Exception:  # noqa: BLE001
        indexed = False
    sources[digest] = {
        "hash": digest, "name": clean_name, "filename": str(filename),
        "tags": clean_tags, "source": str(source or "local_import"),
        "imported_at": time.time(), "bytes": written_bytes,
        "ext": ext, "path": str(target), "indexed": indexed,
    }
    try:
        _write_json_atomic(sources_path(), sources)
    except OSError:
        pass
    return {"ok": True, "status": "imported", "duplicate": False,
            "path": str(target), "name": clean_name, "tags": clean_tags,
            "hash": digest, "bytes": written_bytes, "ext": ext,
            "source": str(source or "local_import"), "indexed": indexed}


def import_local_file(path: Path | str, **kwargs) -> dict:
    """从白名单目录导入文件（桌面 / 素材库 / 仓库 media）。"""
    raw = Path(str(path or ""))
    allowed: list[str] = []
    try:
        resolved = raw.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return {"ok": False, "status": "invalid", "reason": "path_unresolvable"}
    for root in IMPORT_PATH_ROOTS:
        try:
            root_resolved = Path(root).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        allowed.append(str(root_resolved))
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            continue
        if not resolved.is_file():
            return {"ok": False, "status": "invalid", "reason": "file_not_found"}
        try:
            data = resolved.read_bytes()
        except OSError as exc:
            return {"ok": False, "status": "invalid",
                    "reason": f"read_failed:{type(exc).__name__}: {exc}"}
        return import_local(data, resolved.name, **kwargs)
    return {"ok": False, "status": "invalid", "reason": "path_not_allowed",
            "hint": "只允许从 Desktop / 素材库 / 仓库 media 导入",
            "allowed_roots": allowed}


# --------------------------------------------------------------------------- #
# 愿望：状态旁路 + 手动满足
# --------------------------------------------------------------------------- #
def _wish_id(item: dict, index: int) -> str:
    raw = str(item.get("id") or "").strip()
    if raw:
        return raw
    seed = f"{item.get('wish')}|{item.get('ts')}|{item.get('created_at')}|{index}"
    return "WISH-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _read_wish_status() -> dict:
    return _read_json_file(wish_status_path(), {})


def list_wishes(path: Path | str | None = None) -> list[dict]:
    """愿望列表 + 状态（pending/fulfilled），不改原 jsonl。"""
    statuses = _read_wish_status()
    items: list[dict] = []
    for index, item in enumerate(search_wishes(path)):
        wish_id = _wish_id(item, index)
        status = statuses.get(wish_id) if isinstance(statuses.get(wish_id), dict) else {}
        merged = dict(item)
        merged["wish_id"] = wish_id
        merged["status"] = str(status.get("status") or item.get("status") or "pending")
        if status.get("asset"):
            merged["fulfilled_asset"] = status.get("asset")
        if status.get("fulfilled_at"):
            merged["fulfilled_at"] = status.get("fulfilled_at")
        items.append(merged)
    return items


def fulfill_wish(wish_id: str, *, data: bytes | None = None,
                 path: Path | str | None = None, name: str = "",
                 tags: list[str] | None = None, source: str = "wish_import") -> dict:
    """手动用一张图满足愿望：导入图片 + 旁路标记 fulfilled（不删原记录）。"""
    target_id = str(wish_id or "").strip()
    if not target_id:
        return {"ok": False, "status": "invalid", "reason": "missing_wish_id"}
    wishes = list_wishes()
    wish = next((item for item in wishes if item.get("wish_id") == target_id), None)
    if wish is None:
        return {"ok": False, "status": "invalid", "reason": "wish_not_found"}
    if wish.get("status") == "fulfilled":
        return {"ok": False, "status": "invalid", "reason": "already_fulfilled",
                "wish": wish}
    if path is not None:
        result = import_local_file(path, name=name, tags=tags, source=source)
    elif data is not None:
        result = import_local(data, "wish.png", name=name, tags=tags, source=source)
    else:
        return {"ok": False, "status": "invalid", "reason": "missing_image"}
    if not result.get("ok"):
        return {"ok": False, "status": "invalid",
                "reason": result.get("reason") or "import_failed",
                "import": result}
    statuses = _read_wish_status()
    statuses[target_id] = {
        "status": "fulfilled", "fulfilled_at": time.time(),
        "asset": result.get("path"), "hash": result.get("hash"),
        "wish": wish.get("wish"),
    }
    try:
        _write_json_atomic(wish_status_path(), statuses)
    except OSError:
        return {"ok": False, "status": "failed", "reason": "status_write_failed",
                "import": result}
    result["wish"] = {**wish, "status": "fulfilled"}
    return result
