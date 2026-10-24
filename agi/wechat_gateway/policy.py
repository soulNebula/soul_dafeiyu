# -*- coding: utf-8 -*-
"""801 真实发送策略：白名单之外的硬闸门。"""
from __future__ import annotations

from typing import Any


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # 跨午夜


def check_send(*, now: float, hour: int, recent_60s: int, today_count: int,
               cfg: dict[str, Any], reactive: bool = False) -> dict[str, Any]:
    if bool(cfg.get("stop")):
        return {"ok": False, "reason": "stop_switch"}
    quiet = cfg.get("quiet_hours") or []
    if isinstance(quiet, (list, tuple)) and len(quiet) == 2:
        try:
            if in_quiet_hours(hour, int(quiet[0]), int(quiet[1])):
                return {"ok": False, "reason": "quiet_hours"}
        except (TypeError, ValueError):
            pass
    key = "reactive_rate_per_minute" if reactive else "rate_per_minute"
    rate = int(cfg.get(key) or (8 if reactive else 3))
    if recent_60s >= max(1, rate):
        return {"ok": False, "reason": "rate_limit"}
    daily = int(cfg.get("daily_limit") or 200)
    if today_count >= max(1, daily):
        return {"ok": False, "reason": "daily_limit"}
    return {"ok": True, "reason": "allowed"}
