# -*- coding: utf-8 -*-
"""P32 控制台只读 helper：成长/自我叙事（等 Lead 接线路由用）。

不直接注册路由；server.py 之后只需：
    GET /api/growth/narrative -> growth_narrative()
    GET /api/growth/status    -> growth_status(enabled=<config.narrative_enabled>)
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from agi.selflearn import self_narrative

DEFAULT_WEEKLY_DIR = self_narrative.DEFAULT_WEEKLY_DIR
DEFAULT_HISTORY_PATH = self_narrative.DEFAULT_HISTORY_PATH


def _week_files(weekly_dir: Path | str = DEFAULT_WEEKLY_DIR) -> list[str]:
    target = Path(weekly_dir)
    if not target.exists():
        return []
    return sorted(path.stem for path in target.glob("*.md"))


def growth_narrative(*, db_path: Path | str | None = None,
                     history_path: Path | str | None = None,
                     limit: int = 20) -> dict:
    """GET /api/growth/narrative 的数据。"""
    kwargs = {"db_path": db_path} if db_path else {}
    current = self_narrative.current(**kwargs)
    hist = self_narrative.history(
        path=history_path or DEFAULT_HISTORY_PATH, limit=limit)
    return {"ok": True, "text": current.get("text") or "",
            "found": bool(current.get("found")),
            "id": current.get("id"), "approved_by": current.get("approved_by"),
            "updated_at": current.get("updated_at"),
            "version": current.get("version"),
            "history": hist}


def growth_status(*, enabled: bool = False, now: datetime | None = None,
                  db_path: Path | str | None = None,
                  weekly_dir: Path | str | None = None,
                  history_path: Path | str | None = None) -> dict:
    """GET /api/growth/status 的数据（只读）。"""
    now = now or datetime.now()
    weekly_dir = weekly_dir or DEFAULT_WEEKLY_DIR
    current = self_narrative.current(**({"db_path": db_path} if db_path else {}))
    schedule = self_narrative.maybe_weekly(
        now=now, weekly_dir=weekly_dir, enabled=bool(enabled))
    weeks = _week_files(weekly_dir)
    hist = self_narrative.history(
        path=history_path or DEFAULT_HISTORY_PATH, limit=20)
    return {"ok": True, "enabled": bool(enabled),
            "narrative_available": bool(current.get("found")),
            "narrative_updated_at": current.get("updated_at"),
            "narrative_approved_by": current.get("approved_by"),
            "last_week": weeks[-1] if weeks else None,
            "weeks": len(weeks),
            "history_count": len(hist),
            "schedule": schedule,
            "next_run": schedule.get("next_run"),
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S", now.timetuple())}
