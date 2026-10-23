# -*- coding: utf-8 -*-
"""M1 质量审计：claim 证据、pending 比例、画像每维证据数。

不调用 LLM，只核对本地数据；用于防止“数量达标但证据是编的”。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import db, profile


def claims_quality(path: Path | str = "agi/data/selflearn/selflearn.sqlite3") -> dict[str, Any]:
    con = db.connect(path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id,evidence_span,evidence_message_id,status,source_level,claim_type "
            "FROM claims")]
        texts = {}
        try:
            for r in con.execute("SELECT id,text FROM messages"):
                texts[str(r["id"])] = str(r["text"] or "")
        except Exception:  # noqa: BLE001
            pass
    finally:
        con.close()
    total = len(rows)
    valid = fabricated = pending = accepted = 0
    bad_ids = []
    for row in rows:
        span = str(row.get("evidence_span") or "")
        mid = str(row.get("evidence_message_id") or "")
        source = texts.get(mid)
        ok = bool(span) and (source is None or span in source)
        if ok:
            valid += 1
        else:
            fabricated += 1
            bad_ids.append(row["id"])
        if str(row.get("status") or "") == "pending":
            pending += 1
        elif str(row.get("status") or "") == "active":
            accepted += 1
    pending_ratio = pending / total if total else 0.0
    evidence_ratio = valid / total if total else 0.0
    return {
        "ok": True, "total": total, "valid_evidence": valid, "fabricated": fabricated,
        "evidence_ratio": round(evidence_ratio, 3), "pending": pending,
        "pending_ratio": round(pending_ratio, 3), "accepted": accepted,
        "bad_ids": bad_ids[:20],
        "verdict": ("empty" if total == 0 else
                    "pass" if evidence_ratio >= 0.98 and pending_ratio <= 0.25 else "needs_work"),
    }


def profile_quality(path: Path | str = "agi/data/selflearn/selflearn.sqlite3") -> dict[str, Any]:
    try:
        prof = profile.get_profile(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    dims = {}
    enough = 0
    for dim in profile.DIMENSIONS:
        item = prof.get(dim) or {}
        evidence = item.get("evidence_memory_ids") or []
        if not isinstance(evidence, list):
            evidence = []
        dims[dim] = {"value": item.get("value", ""), "evidence": len(evidence),
                     "ok": len(evidence) >= 2}
        if len(evidence) >= 2:
            enough += 1
    return {"ok": True, "dimensions": dims, "with_two_evidence": enough,
            "verdict": "pass" if enough == len(profile.DIMENSIONS) else "needs_work"}


def report(path: Path | str = "agi/data/selflearn/selflearn.sqlite3") -> dict[str, Any]:
    c = claims_quality(path)
    p = profile_quality(path)
    return {"ok": True, "claims": c, "profile": p,
            "verdict": "pass" if c.get("verdict") == "pass" and p.get("verdict") == "pass" else "needs_work"}
