# -*- coding: utf-8 -*-
"""802 控制台：模型管理（聚合列表 / 本地导入 / 回收站删除 / URL 下载）。"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
AGENT_HOME = Path(os.environ.get("MODEL_ADMIN_AGENT") or r"E:/agent")
MODELS_ROOT = Path(os.environ.get("MODEL_ADMIN_ROOT") or AGENT_HOME / "models")
ADAPTERS_ROOT = Path(os.environ.get("MODEL_ADMIN_ADAPTERS") or AGENT_HOME / "output" / "adapters")
IMPORT_ROOT = MODELS_ROOT / "imported"
DOWNLOAD_ROOT = MODELS_ROOT / "downloads"
TRASH_ROOT = MODELS_ROOT / ".trash"
STATUS_PATH = Path(os.environ.get("MODEL_DOWNLOAD_STATUS")
                   or ROOT / "agi" / "data" / "model_downloads.json")
FORMATS = ("gguf", "hf", "adapter")
_LOCK = threading.RLock()


def _safe_name(value: str, fallback: str = "model") -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
    return text[:80] or fallback


def _size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0


def _gguf_ok(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == b"GGUF"
    except OSError:
        return False


def detect_format(path: Path) -> tuple[str, str]:
    """返回 (format, error)；path 不存在也明确报错。"""
    path = Path(path)
    if not path.exists():
        return "", "path_not_found"
    if path.is_file():
        if path.suffix.lower() != ".gguf":
            return "", "file_must_be_gguf"
        return ("gguf", "") if _gguf_ok(path) else ("", "gguf_header_invalid")
    if path.is_dir():
        if (path / "adapter_config.json").is_file():
            return "adapter", ""
        if (path / "config.json").is_file():
            return "hf", ""
        try:
            ggufs = list(path.glob("*.gguf"))
        except OSError:
            ggufs = []
        if ggufs:
            return "gguf", ""
        return "", "unrecognized_model_directory"
    return "", "unsupported_path"


def _item(path: Path, fmt: str, source: str) -> dict:
    return {"name": path.name, "path": str(path), "format": fmt,
            "size": _size(path), "source": source,
            "is_dir": path.is_dir(), "gguf_valid": fmt != "gguf" or _gguf_ok(path)}


def _scan(root: Path, source: str, *, adapters: bool = False) -> list[dict]:
    items: list[dict] = []
    if not root.is_dir():
        return items
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return items
    for child in children:
        if child.name in (".trash", "imported", "downloads") and source == "models":
            continue
        if child.is_file():
            if child.suffix.lower() == ".gguf":
                items.append(_item(child, "gguf", source))
            continue
        if not child.is_dir():
            continue
        if (child / "adapter_config.json").is_file():
            items.append(_item(child, "adapter", source))
            continue
        if (child / "config.json").is_file():
            items.append(_item(child, "hf", source))
            continue
        try:
            ggufs = sorted(child.glob("*.gguf"))
        except OSError:
            ggufs = []
        for gguf in ggufs:
            items.append(_item(gguf, "gguf", source))
    return items


def list_models() -> dict:
    items = _scan(MODELS_ROOT, "models")
    for sub in ("imported", "downloads"):
        items.extend(_scan(MODELS_ROOT / sub, sub))
    items.extend(_scan(ADAPTERS_ROOT, "adapters"))
    # 兼容历史布局：adapter 也可能直接放在 output/ 下（如 output/soul_qwen25_05b_v0）
    output_root = AGENT_HOME / "output"
    if output_root.resolve() != ADAPTERS_ROOT.resolve():
        seen = {row["path"] for row in items}
        for row in _scan(output_root, "adapters"):
            if row["path"] not in seen:
                items.append(row)
    items.sort(key=lambda row: (row["format"], row["name"].lower()))
    counts = {fmt: sum(1 for row in items if row["format"] == fmt) for fmt in FORMATS}
    return {"ok": True, "items": items, "count": len(items), "counts": counts,
            "root": str(MODELS_ROOT), "downloads": str(DOWNLOAD_ROOT)}


def import_model(source: str, name: str = "", fmt: str = "auto") -> dict:
    src = Path(str(source or "")).expanduser()
    if not src.exists():
        return {"ok": False, "error": "path_not_found", "path": str(src)}
    detected, error = detect_format(src)
    if error:
        return {"ok": False, "error": error, "path": str(src)}
    requested = str(fmt or "auto").strip().lower()
    if requested not in ("auto", *FORMATS):
        return {"ok": False, "error": "invalid_format", "allowed": ["auto", *FORMATS]}
    if requested != "auto" and requested != detected:
        return {"ok": False, "error": "format_mismatch",
                "detected": detected, "requested": requested}
    target_name = _safe_name(name or src.stem if src.is_file() else name or src.name)
    if src.is_file():
        destination = IMPORT_ROOT / target_name
        dest_file = destination / src.name
        if destination.exists():
            return {"ok": False, "error": "destination_exists", "path": str(destination)}
        destination.mkdir(parents=True)
        shutil.copy2(src, dest_file)
        result = dest_file
    else:
        destination = IMPORT_ROOT / target_name
        if destination.exists():
            return {"ok": False, "error": "destination_exists", "path": str(destination)}
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, destination)
        result = destination
    return {"ok": True, "format": detected, "path": str(result),
            "name": target_name, "size": _size(Path(result))}


def _under_models(path: Path) -> bool:
    try:
        path.resolve().relative_to(MODELS_ROOT.resolve())
        return True
    except (OSError, ValueError):
        return False


def delete_model(path: str) -> dict:
    target = Path(str(path or "")).expanduser()
    if not target.exists():
        return {"ok": False, "error": "path_not_found", "path": str(target)}
    try:
        resolved = target.resolve()
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not _under_models(resolved):
        return {"ok": False, "error": "path_outside_models_root",
                "root": str(MODELS_ROOT)}
    if resolved == MODELS_ROOT.resolve():
        return {"ok": False, "error": "cannot_delete_models_root"}
    stamp = time.strftime("%Y%m%d_%H%M%S")
    rel = resolved.relative_to(MODELS_ROOT.resolve())
    trash = TRASH_ROOT / stamp / rel
    if trash.exists():
        trash = TRASH_ROOT / f"{stamp}_{uuid.uuid4().hex[:6]}" / rel
    try:
        trash.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(resolved), str(trash))
    except OSError as exc:
        return {"ok": False, "error": f"move_failed:{type(exc).__name__}: {exc}"}
    return {"ok": True, "moved_to": str(trash), "path": str(resolved),
            "hint": "已移入 models/.trash，可从回收目录恢复"}


def _read_status() -> dict:
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_status(data: dict) -> None:
    with _LOCK:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATUS_PATH)


def _update_status(download_id: str, **fields) -> None:
    with _LOCK:
        data = _read_status()
        entry = dict(data.get(download_id) or {})
        entry.update(fields)
        data[download_id] = entry
        _write_status(data)


def _download_worker(download_id: str, url: str, target: Path) -> None:
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            target.parent.mkdir(parents=True, exist_ok=True)
            done = 0
            with target.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    _update_status(download_id, bytes=done, total=total,
                                   status="downloading")
        if target.suffix.lower() != ".gguf":
            target.unlink(missing_ok=True)
            _update_status(download_id, status="error", error="downloaded_file_not_gguf")
            return
        if not _gguf_ok(target):
            target.unlink(missing_ok=True)
            _update_status(download_id, status="error", error="gguf_header_invalid")
            return
        _update_status(download_id, status="done", path=str(target),
                       bytes=target.stat().st_size, total=target.stat().st_size)
    except Exception as exc:  # noqa: BLE001
        _update_status(download_id, status="error",
                       error=f"{type(exc).__name__}: {exc}")


def download_model(url: str, name: str = "") -> dict:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"ok": False, "error": "url_must_be_http"}
    filename = Path(parsed.path).name
    if not filename.lower().endswith(".gguf"):
        return {"ok": False, "error": "url_must_point_to_gguf"}
    safe = _safe_name(name or Path(filename).stem)
    target = DOWNLOAD_ROOT / safe / filename
    if target.exists():
        return {"ok": False, "error": "destination_exists", "path": str(target)}
    download_id = uuid.uuid4().hex[:12]
    _update_status(download_id, id=download_id, url=url, name=safe,
                   path=str(target), status="started", bytes=0, total=0,
                   started_at=time.time(), error="")
    thread = threading.Thread(target=_download_worker, args=(download_id, url, target),
                              name=f"model-download-{download_id}", daemon=True)
    thread.start()
    return {"ok": True, "id": download_id, "name": safe, "path": str(target),
            "status": "started"}


def download_status() -> dict:
    data = _read_status()
    items = sorted(data.values(), key=lambda row: row.get("started_at") or 0, reverse=True)
    return {"ok": True, "items": items, "count": len(items)}
