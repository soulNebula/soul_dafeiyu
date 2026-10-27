# -*- coding: utf-8 -*-
"""M3 防 hacking：检测回复风格整体漂移（变长/变软/变客服）。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import db

SOFT = re.compile(r"(当然|理解你|抱歉|辛苦|没关系的|别担心|你已经很棒|可以的|嗯嗯|宝子)")


def _metrics(texts: list[str]) -> dict[str, float]:
    if not texts:
        return {"count": 0, "avg_len": 0.0, "question_ratio": 0.0, "soft_ratio": 0.0}
    total = sum(len(t) for t in texts)
    q = sum(1 for t in texts if "?" in t or "？" in t)
    soft = sum(1 for t in texts if SOFT.search(t))
    return {"count": len(texts), "avg_len": round(total / len(texts), 1),
            "question_ratio": round(q / len(texts), 3),
            "soft_ratio": round(soft / len(texts), 3)}


def detect(path: Path | str, *, window: int = 20) -> dict[str, Any]:
    con = db.connect(path)
    try:
        rows = [str(r["reply"] or "") for r in con.execute(
            "SELECT reply FROM turns ORDER BY created_at DESC LIMIT ?", (max(2, int(window)) * 2,))]
    finally:
        con.close()
    w = max(2, int(window))
    recent, older = rows[:w], rows[w:2 * w]
    if len(recent) < 3 or len(older) < 3:
        return {"ok": True, "verdict": "insufficient", "reason": "轮次不足",
                "recent": _metrics(recent), "older": _metrics(older), "flags": []}
    a, b = _metrics(recent), _metrics(older)
    flags = []
    if b["avg_len"] and a["avg_len"] > b["avg_len"] * 1.35:
        flags.append("reply_length_increasing")
    if a["soft_ratio"] > b["soft_ratio"] + 0.25:
        flags.append("softening")
    if a["question_ratio"] < b["question_ratio"] * 0.5:
        flags.append("question_ratio_dropping")
    return {
        "ok": True,
        "verdict": "drifting" if flags else "stable",
        "recent": a, "older": b,
        "length_ratio": round(a["avg_len"] / b["avg_len"], 2) if b["avg_len"] else None,
        "soft_delta": round(a["soft_ratio"] - b["soft_ratio"], 3),
        "flags": flags,
        "note": "只做风格审计，不自动改策略；阈值基于本地窗口。",
    }
