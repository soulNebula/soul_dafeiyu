# -*- coding: utf-8 -*-
"""14 按需运行：自动 tick 的总闸门。

网页/定时器在调用 proactive.tick 前先过这里；不通过就不执行任何主动行为。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import db

LEVELS = ("off", "manual", "assist", "auto", "full")
DEFAULT = {
    "enabled": True,
    "level": "assist",
    "quiet_hours": [0, 7],
    "daily_budget": 6,
    "min_interval_seconds": 30,
}


def _config(path: Path | str) -> dict[str, Any]:
    cfg = dict(DEFAULT)
    con = db.connect(path)
    try:
        row = con.execute("SELECT value FROM state WHERE key='autonomy'").fetchone()
    finally:
        con.close()
    if row:
        try:
            import json
            raw = json.loads(row["value"])
            if isinstance(raw, dict):
                cfg.update(raw)
        except Exception:  # noqa: BLE001
            pass
    return cfg


def gate(path: Path | str, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    cfg = _config(path)
    level = str(cfg.get("level") or "assist").lower()
    if level not in LEVELS:
        level = "assist"
    if not cfg.get("enabled", True) or level in ("off", "manual"):
        return {"ok": True, "allowed": False, "reason": "autonomy_disabled",
                "level": level, "config": cfg}
    quiet = cfg.get("quiet_hours") or []
    if isinstance(quiet, (list, tuple)) and len(quiet) == 2:
        hour = time.localtime(now).tm_hour
        start, end = int(quiet[0]), int(quiet[1])
        if start == end:
            in_quiet = False
        else:
            in_quiet = (start <= hour < end) if start < end else (hour >= start or hour < end)
        if in_quiet:
            return {"ok": True, "allowed": False, "reason": "quiet_hours", "config": cfg}
    con = db.connect(path)
    try:
        daily = int(con.execute(
            "SELECT count(*) FROM proactive WHERE created_at>=? AND status IN ('spoken','proposed')",
            (now - 86400,)).fetchone()[0])
        row = con.execute("SELECT max(created_at) FROM proactive").fetchone()
        last = float(row[0] or 0) if row else 0.0
    finally:
        con.close()
    if daily >= int(cfg.get("daily_budget") or 6):
        return {"ok": True, "allowed": False, "reason": "daily_budget", "config": cfg}
    gap = int(cfg.get("min_interval_seconds") or 30)
    if last and now - last < gap:
        return {"ok": True, "allowed": False, "reason": "min_interval", "config": cfg}
    return {"ok": True, "allowed": True, "reason": "allowed", "level": level, "config": cfg}
