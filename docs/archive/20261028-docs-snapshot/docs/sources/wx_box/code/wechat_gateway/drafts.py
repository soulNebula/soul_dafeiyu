# -*- coding: utf-8 -*-
"""801 待发草稿（draft）：confirm 模式下先落盘，人工批准后才真正发送。

存储：``agi/data/wechat/drafts.jsonl``，一行一条 JSON，原子重写；
进程重启不丢草稿。纯本地文件，不涉及微信。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "agi" / "data" / "wechat" / "drafts.jsonl"

_LOCK = threading.Lock()


def _path(path: Path | str | None) -> Path:
    return Path(path) if path else DEFAULT_PATH


def _read_all(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("id"):
                rows.append(item)
    except OSError:
        return []
    return rows


def _write_all(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, path)


def add(contact: str, text: str, reason: str = "confirm",
        *, path: Path | str | None = None) -> dict:
    """新增一条草稿；返回草稿本身（含 id/ts）。"""
    target = _path(path)
    item = {
        "id": "D-" + time.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3),
        "ts": time.time(),
        "contact": str(contact or ""),
        "text": str(text or ""),
        "reason": str(reason or "confirm"),
        "status": "pending",
    }
    with _LOCK:
        rows = _read_all(target)
        rows.append(item)
        _write_all(target, rows[-200:])
    return item


def list_all(*, path: Path | str | None = None, limit: int = 50) -> list[dict]:
    rows = _read_all(_path(path))
    return rows[-max(1, int(limit)):]


def get(draft_id: str, *, path: Path | str | None = None) -> dict | None:
    target = str(draft_id or "").strip()
    for row in _read_all(_path(path)):
        if row.get("id") == target:
            return row
    return None


def discard(draft_id: str, *, path: Path | str | None = None) -> dict | None:
    """删除一条草稿（批准或拒绝后调用）；返回被删除的草稿。"""
    target_path = _path(path)
    target = str(draft_id or "").strip()
    with _LOCK:
        rows = _read_all(target_path)
        kept, removed = [], None
        for row in rows:
            if row.get("id") == target and removed is None:
                removed = row
                continue
            kept.append(row)
        if removed is not None:
            _write_all(target_path, kept)
    return removed
