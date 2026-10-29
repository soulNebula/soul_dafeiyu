# -*- coding: utf-8 -*-
"""训练样本打包：把每日语料快照成可交付的包（不删除原始数据）。

路径（Bot 隔离后由 ``agi.paths.data_dir()`` 决定）::

    <data>/train/daily/YYYY-MM-DD.jsonl    每日流水线产物
    <data>/train/packages/<id>.jsonl       训练包（去重后的样本）
    <data>/train/packages/<id>.json       manifest
    <data>/train/packages/index.json      包列表

进度口径：``pending = 累计样本 - 已打包样本``；打包不清零原始 daily，只增加
``packaged_total``，因此进度条会归零但数据仍可追溯。自动打包阈值默认 300，
与 ``tools/train/train_gate.py`` 的门槛一致。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
THRESHOLD = 300


def _root(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root)
    try:
        from agi.paths import data_dir
        return data_dir() / "train"
    except Exception:  # noqa: BLE001
        return ROOT / "agi" / "data" / "train"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    except OSError:
        pass
    return rows


def _daily_rows(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    daily = root / "daily"
    rows: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    if not daily.is_dir():
        return rows, files
    for path in sorted(daily.glob("*.jsonl")):
        items = _read_jsonl(path)
        rows.extend(items)
        files.append({"date": path.stem, "samples": len(items)})
    return rows, files


def _index(root: Path) -> list[dict[str, Any]]:
    path = root / "packages" / "index.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _write_index(root: Path, items: list[dict[str, Any]]) -> None:
    path = root / "packages" / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def summary(root: Path | str | None = None, *, threshold: int | None = None) -> dict[str, Any]:
    threshold = THRESHOLD if threshold is None else int(threshold)
    base = _root(root)
    rows, files = _daily_rows(base)
    packages = _index(base)
    packaged = sum(int(item.get("samples") or 0) for item in packages)
    pending = max(0, len(rows) - packaged)
    return {
        "ok": True,
        "threshold": threshold,
        "cumulative": len(rows),
        "packaged": packaged,
        "pending": pending,
        "progress": round(min(1.0, pending / max(1, threshold)), 4),
        "ready": pending >= threshold,
        "files": files,
        "packages": packages[-20:],
        "root": str(base),
    }


def create_package(root: Path | str | None = None, *, label: str = "",
                   threshold: int | None = None) -> dict[str, Any]:
    """创建快照包（默认不检查阈值，调用方可用 ``auto`` 判断）。"""
    threshold = THRESHOLD if threshold is None else int(threshold)
    base = _root(root)
    rows, _files = _daily_rows(base)
    packages = _index(base)
    packaged = sum(int(item.get("samples") or 0) for item in packages)
    if len(rows) <= packaged:
        return {"ok": False, "error": "no_new_samples", "summary": summary(base, threshold=threshold)}
    new_rows = rows[packaged:]
    # 去重（同一条 messages 只留一次），同时保留原始 meta
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in new_rows:
        key = hashlib.sha1(json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"pkg-{stamp}-{len(unique)}"
    label = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", str(label or ""))[:40]
    if label:
        name = f"{name}-{label}"
    packages_dir = base / "packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    data_path = packages_dir / f"{name}.jsonl"
    data_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in unique) + "\n",
        encoding="utf-8")
    manifest = {
        "id": name,
        "created_at": int(time.time()),
        "samples": len(unique),
        "source_samples": len(new_rows),
        "label": label or "manual",
        "status": "ready",
        "file": data_path.name,
        "format": "messages-jsonl",
    }
    (packages_dir / f"{name}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    packages.append(manifest)
    _write_index(base, packages)
    return {"ok": True, "package": manifest, "summary": summary(base, threshold=threshold)}


def auto_package(root: Path | str | None = None, *, threshold: int | None = None) -> dict[str, Any]:
    threshold = THRESHOLD if threshold is None else int(threshold)
    base = _root(root)
    state = summary(base, threshold=threshold)
    if not state["ready"]:
        return {"ok": True, "created": False, "reason": "below_threshold",
                "summary": state}
    result = create_package(base, label="auto", threshold=threshold)
    result["created"] = bool(result.get("ok"))
    return result
