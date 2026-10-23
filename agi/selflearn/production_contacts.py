# -*- coding: utf-8 -*-
"""把 801 各联系人独立 DB 聚合起来做生产验收。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import production

ROOT = Path(__file__).resolve().parents[1]
CONTACT_DB_DIR = ROOT / "data" / "selflearn" / "wechat_contacts"


def _paths(paths=None) -> list[Path]:
    if paths:
        return [Path(p) for p in paths]
    if not CONTACT_DB_DIR.exists():
        return []
    return sorted(CONTACT_DB_DIR.glob("*.sqlite3"))


def aggregate(paths=None, *, days: int = 7) -> dict[str, Any]:
    files = [p for p in _paths(paths) if p.exists()]
    snapshots = [production.snapshot(p, days=days) for p in files]
    raw = {k: 0 for k in ("messages", "user_messages", "turns", "feedback",
                          "explicit_feedback", "memories", "persona", "claims",
                          "conflicts", "errors", "proactive", "variants")}
    for s in snapshots:
        for k in raw:
            raw[k] += int((s.get("raw") or {}).get(k) or 0)
    active_days = max([int(s.get("active_days") or 0) for s in snapshots] or [0])
    feedback_days = max([int(s.get("feedback_days") or 0) for s in snapshots] or [0])
    checks = {
        "messages": raw["messages"] >= production.MIN_MESSAGES,
        "turns": raw["turns"] >= production.MIN_TURNS,
        "feedback": raw["feedback"] >= production.MIN_FEEDBACK,
        "explicit_feedback": raw["explicit_feedback"] >= production.MIN_FEEDBACK,
        "memories": raw["memories"] >= production.MIN_MEMORIES,
        "persona": raw["persona"] >= production.MIN_PERSONA,
        "claims": raw["claims"] >= production.MIN_CLAIMS,
        "active_days": active_days >= production.MIN_ACTIVE_DAYS,
        "feedback_days": feedback_days >= production.MIN_FEEDBACK_DAYS,
        "conflicts": raw["conflicts"] <= production.MAX_OPEN_CONFLICTS,
        "errors": (raw["errors"] / max(1, raw["turns"])) <= production.MAX_ERROR_RATE,
    }
    passed = sum(1 for ok in checks.values() if ok)
    total = len(checks)
    return {"ok": True, "db_count": len(files), "dbs": [str(p) for p in files],
            "raw": raw, "active_days": active_days, "feedback_days": feedback_days,
            "checks": checks, "passed": passed, "total": total,
            "score": round(passed * 100 / total, 1),
            "verdict": "pilot_ready" if passed == total else ("not_ready" if passed < total * 0.7 else "needs_work"),
            "snapshots": snapshots,
            "note": "联系人库聚合；explicit_feedback 必须来自用户主动反馈。"}
