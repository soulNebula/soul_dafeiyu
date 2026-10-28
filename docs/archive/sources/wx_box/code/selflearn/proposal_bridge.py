# -*- coding: utf-8 -*-
"""把反思结果转成 learning_proposals，供人工审阅/灰度。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import proposals


def from_reflection(path: Path | str, reflected: dict[str, Any]) -> dict[str, Any]:
    """把 personality.reflect 的 activated/candidates 转成 draft 提案；不自动批准。"""
    if not isinstance(reflected, dict) or not reflected.get("ok"):
        return {"ok": False, "error": "反思结果不可用", "created": 0}
    candidates = reflected.get("activated") or reflected.get("candidates") or []
    if not isinstance(candidates, list):
        return {"ok": False, "error": "反思结果没有候选列表", "created": 0}
    created = skipped = 0
    ids = []
    for item in candidates[:5]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        value = str(item.get("value") or "").strip()
        if not key or not value:
            continue
        evidence = item.get("evidence_turns") or item.get("evidence") or []
        existing = proposals.list_proposals(path, status=["draft", "gray"])
        duplicate = any(
            str(p.get("kind")) == "persona_trait"
            and str(p.get("target")) == key
            and (p.get("payload") or {}).get("value") == value
            for p in existing)
        if duplicate:
            skipped += 1
            continue
        result = proposals.create(
            path, "persona_trait", key,
            {"value": value, "source": "reflection"},
            evidence if isinstance(evidence, list) else [str(evidence)],
        )
        if result.get("ok"):
            created += 1
            ids.append(result["id"])
        else:
            skipped += 1
    return {"ok": True, "created": created, "skipped": skipped, "ids": ids,
            "note": "只生成 draft，不自动改人格；需人工 transition"}
