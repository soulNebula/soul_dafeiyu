# -*- coding: utf-8 -*-
"""S8 混合检索：关键词召回 + 来源权重 + recency + MMR。"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

from . import search

SOURCE_WEIGHT = {"memory": 1.0, "claim": 0.9, "message": 0.7, "text": 0.6}
_CJK = re.compile(r"[\u3400-\u9fff]")


def _grams(text: str) -> set[str]:
    value = str(text or "")
    out = set()
    for seg in re.findall(r"[\u3400-\u9fff]{1,}", value):
        if len(seg) == 1:
            out.add(seg)
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
    return out


def hybrid(path: Path | str, query: str, *, k: int = 6, now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else float(now)
    limit = max(1, int(k))
    raw = search.search(path, query, limit=max(8, limit * 4))
    if not raw:
        return []
    max_score = max(float(r.get("score") or 0) for r in raw) or 1.0
    cands = []
    for i, r in enumerate(raw):
        text = str(r.get("text") or "")
        st = str(r.get("source_type") or "text")
        rel = float(r.get("score") or 0) / max_score
        source = SOURCE_WEIGHT.get(st, 0.6)
        recency = 1.0 / (1.0 + max(0, i) * 0.08)
        score = 0.6 * rel + 0.25 * source + 0.15 * recency
        cands.append({"source_type": st, "source_id": str(r.get("source_id") or ""),
                      "text": text, "score": round(score, 4), "relevance": round(rel, 4),
                      "source_weight": source, "tokens": _grams(text)})
    selected = []
    while cands and len(selected) < limit:
        best = None
        best_mmr = -1e9
        for c in cands:
            sim = 0.0
            if selected:
                sim = max((len(c["tokens"] & s["tokens"]) /
                           max(1, len(c["tokens"] | s["tokens"]))) for s in selected)
            mmr = 0.75 * c["score"] - 0.25 * sim
            if mmr > best_mmr:
                best, best_mmr = c, mmr
        selected.append(best)
        cands.remove(best)
    for s in selected:
        s.pop("tokens", None)
    return selected


def context(path: Path | str, query: str, *, k: int = 5) -> str:
    hits = hybrid(path, query, k=k)
    return "\n".join(f"- [{h['source_type']}] {h['text'][:160]}" for h in hits)
