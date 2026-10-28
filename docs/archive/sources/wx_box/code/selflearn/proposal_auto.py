# -*- coding: utf-8 -*-
"""04 提案自动灰度（只 draft→gray，不自动 merged/rejected）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import db, proposals


def auto_gray(path: Path | str, *, min_evidence: int = 2) -> dict[str, Any]:
    moved = []
    skipped = []
    for item in proposals.list_proposals(path, status="draft"):
        evidence = item.get("evidence") or []
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except (TypeError, ValueError):
                evidence = [evidence] if evidence.strip() else []
        if not isinstance(evidence, list):
            evidence = []
        if len(evidence) >= int(min_evidence):
            r = proposals.transition(path, item["id"], "gray")
            if r.get("ok"):
                moved.append(item["id"])
            else:
                skipped.append({"id": item["id"], "error": r.get("error")})
        else:
            skipped.append({"id": item["id"], "reason": "evidence_lt_2"})
    return {"ok": True, "moved": moved, "skipped": skipped,
            "note": "只做 draft→gray；merged/rejected 必须人工"}


def weekly_stats(path: Path | str) -> dict[str, Any]:
    rows = proposals.list_proposals(path)
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    total = len(rows)
    merged = counts.get("merged", 0)
    return {"ok": True, "total": total, "by_status": counts,
            "merge_rate": round(merged / total, 3) if total else 0.0,
            "note": "merge_rate 目标 30–50%，但 merged 仍由人工完成"}
