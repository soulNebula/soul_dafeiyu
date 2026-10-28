# -*- coding: utf-8 -*-
"""801 真实发送策略：白名单之外的硬闸门（M1）。

三层：
1. 时间/频率：静默时段、最小间隔、每分钟、每小时、每日；
2. 内容：长度上限、本机路径/凭据/内部 ID 拦截；
3. 兜底：``check_send`` 只做判定，不发送（发送在 server）。
"""
from __future__ import annotations

import re
from typing import Any

#: 出站内容里一律不允许出现的片段（避免把本机信息/凭据发进微信）
SENSITIVE_CONTENT = (
    (r"[A-Za-z]:\\", "本机绝对路径（盘符）"),
    (r"(?i)/home/|/Users/", "本机绝对路径（POSIX）"),
    (r"(?i)\b(?:api[\s_-]?key|access[\s_-]?token|password|passwd|secret)\b\s*[:=：]", "疑似凭据赋值"),
    (r"\bwxid_[0-9a-z]{6,}\b", "微信内部 ID"),
    (r"(?i)\bsk-[0-9a-z]{16,}\b", "疑似 API key"),
    (r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥"),
)


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # 跨午夜


def check_content(text: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """发送前的文本闸门：空、超长、敏感内容一律拒绝。"""
    value = str(text or "").strip()
    if not value:
        return {"ok": False, "reason": "empty_text"}
    max_chars = int(cfg.get("max_chars") or 500)
    if len(value) > max(1, max_chars):
        return {"ok": False, "reason": "too_long", "chars": len(value)}
    for pattern, label in SENSITIVE_CONTENT:
        if re.search(pattern, value):
            return {"ok": False, "reason": "sensitive", "detail": label}
    return {"ok": True, "reason": "allowed"}


def check_send(*, now: float, hour: int, recent_60s: int, today_count: int,
               cfg: dict[str, Any], reactive: bool = False,
               text: str = "", last_sent_at: float = 0.0,
               hourly_count: int = 0) -> dict[str, Any]:
    if bool(cfg.get("stop")):
        return {"ok": False, "reason": "stop_switch"}
    quiet = cfg.get("quiet_hours") or []
    if isinstance(quiet, (list, tuple)) and len(quiet) == 2:
        try:
            if in_quiet_hours(hour, int(quiet[0]), int(quiet[1])):
                return {"ok": False, "reason": "quiet_hours"}
        except (TypeError, ValueError):
            pass
    # 最小间隔：只约束主动发送；被动回复是多气泡、天然连续，不能卡这个闸
    min_interval = float(cfg.get("min_interval_s") or 0.0)
    if (not reactive and min_interval > 0 and last_sent_at > 0
            and (now - last_sent_at) < min_interval):
        return {"ok": False, "reason": "min_interval",
                "wait_s": round(min_interval - (now - last_sent_at), 2)}
    key = "reactive_rate_per_minute" if reactive else "rate_per_minute"
    rate = int(cfg.get(key) or (8 if reactive else 3))
    if recent_60s >= max(1, rate):
        return {"ok": False, "reason": "rate_limit"}
    hourly = int(cfg.get("hourly_limit") or 30)
    if hourly_count >= max(1, hourly):
        return {"ok": False, "reason": "hourly_limit"}
    daily = int(cfg.get("daily_limit") or 200)
    if today_count >= max(1, daily):
        return {"ok": False, "reason": "daily_limit"}
    return {"ok": True, "reason": "allowed"}
