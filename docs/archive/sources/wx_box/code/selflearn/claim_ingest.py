# -*- coding: utf-8 -*-
"""把 S6 claim 抽取接到 S7 闸门，并落 claims 状态。"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from . import claims as claims_mod
from . import db, gates


def _id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def _existing_active(path: Path | str) -> list[dict]:
    return [c for c in claims_mod.list_claims(path, limit=300)
            if str(c.get("status") or "") == "active"]


def _set_status(path: Path | str, claim_id: str, status: str,
                group: str | None = None) -> None:
    con = db.connect(path)
    try:
        con.execute("UPDATE claims SET status=?, conflict_group=? WHERE id=?",
                    (status, group, claim_id))
        con.commit()
    finally:
        con.close()


def ingest(path: Path | str, client, text: str, message_id: str) -> dict[str, Any]:
    """抽取 → 逐条过闸门 → 更新 claims 状态。"""
    # 必须在抽取前拿 existing，否则刚写进去的 claim 会被自己 merge
    existing = _existing_active(path)
    extracted = claims_mod.extract_claims(path, client, text, message_id)
    accepted, pending, rejected, merged, conflicts, inferences = [], [], [], [], [], []
    for claim in extracted:
        result = gates.gate_claim(claim, existing, source_text=text)
        kind = result.get("result")
        if kind == "accept":
            _set_status(path, claim.id, "active")
            accepted.append(claim.id)
            existing.append(
                {"id": claim.id, "subject": claim.subject, "predicate": claim.predicate,
                 "object": claim.object, "polarity": claim.polarity,
                 "claim_type": claim.claim_type, "status": "active"}
            )
        elif kind == "pending":
            # C 级/推断不是“待确认事实”，单独存 inference；低置信度事实仍 pending
            ctype = str(getattr(claim, "claim_type", ""))
            level = str(getattr(claim, "source_level", ""))
            if ctype == "inference" or level == "C":
                _set_status(path, claim.id, "inference")
                inferences.append({"id": claim.id, "reason": result.get("reason")})
            else:
                _set_status(path, claim.id, "pending")
                pending.append({"id": claim.id, "reason": result.get("reason")})
        elif kind == "merge":
            target = result.get("match_id") or ""
            _set_status(path, claim.id, "merged", target or None)
            merged.append({"id": claim.id, "into": target})
        elif kind == "conflict":
            # 偏好可以有多个对象（喜欢猫也喜欢狗）；只有正负相反才算冲突
            if (str(getattr(claim, "claim_type", "")) in ("preference", "value")
                    and result.get("reason_code") == "conflict_object"):
                _set_status(path, claim.id, "active")
                accepted.append(claim.id)
                existing.append(
                    {"id": claim.id, "subject": claim.subject,
                     "predicate": claim.predicate, "object": claim.object,
                     "polarity": claim.polarity, "claim_type": claim.claim_type,
                     "status": "active"})
                continue
            target = result.get("conflict_with") or ""
            group = _id("CC-", claim.id, target, time.time())
            _set_status(path, claim.id, "conflict", group)
            if target:
                _set_status(path, target, "conflict", group)
            conflicts.append({"id": claim.id, "with": target, "group": group})
        else:
            _set_status(path, claim.id, "invalid")
            rejected.append({"id": claim.id, "reason": result.get("reason")})
    return {"extracted": len(extracted), "accepted": accepted, "pending": pending,
            "rejected": rejected, "merged": merged, "conflicts": conflicts,
            "inferences": inferences}


def list_conflicts(path: Path | str) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM claims WHERE status='conflict' ORDER BY created_at DESC")]
    finally:
        con.close()
