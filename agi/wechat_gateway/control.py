# -*- coding: utf-8 -*-
"""801 发送控制面：confirm/draft、投诉熔断、可选 token。"""
from __future__ import annotations

import re
from typing import Any

COMPLAINT = re.compile(r"(投诉|举报|骗人|骗子|机器人|不是真人|骚扰|再发就拉黑)")


def is_complaint(text: str) -> bool:
    return bool(COMPLAINT.search(str(text or "")))


def auth_ok(headers: dict[str, str], cfg: dict[str, Any]) -> bool:
    token = str(cfg.get("admin_token") or "").strip()
    if not token:
        return True
    got = str((headers or {}).get("X-Admin-Token") or "").strip()
    return got == token


def send_mode(cfg: dict[str, Any]) -> str:
    mode = str(cfg.get("send_mode") or "auto").strip().lower()
    return mode if mode in ("auto", "confirm", "off") else "auto"
