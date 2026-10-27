# -*- coding: utf-8 -*-
"""12 主动行为决策：置信下界 + 四象限。"""
from __future__ import annotations

import math
from typing import Any


def wilson_lcb(success: int, n: int, z: float = 1.64) -> float:
    if n <= 0:
        return 0.0
    p = max(0.0, min(1.0, success / n))
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def quadrant(score: float, confidence: float) -> str:
    """score 是预期收益，confidence 是 LCB；四象限决策。"""
    try:
        s = float(score)
        c = float(confidence)
    except (TypeError, ValueError):
        return "hold"
    if s >= 0.5 and c >= 0.5:
        return "do"
    if s >= 0.5 and c < 0.5:
        return "explore"
    if s < 0.5 and c >= 0.5:
        return "skip"
    return "hold"


def decide(success: int, n: int, *, score: float = 0.5,
           threshold: float = 0.5, z: float = 1.64) -> dict[str, Any]:
    confidence = wilson_lcb(int(success), int(n), z)
    action = quadrant(score, confidence)
    return {"ok": True, "success": int(success), "n": int(n),
            "confidence": round(confidence, 4), "score": score,
            "action": action,
            "speak": action in ("do", "explore") and score >= threshold,
            "reason": {"do": "高收益高置信", "explore": "高收益低置信，小样本探索",
                       "skip": "低收益高置信", "hold": "低收益低置信"}[action]}
