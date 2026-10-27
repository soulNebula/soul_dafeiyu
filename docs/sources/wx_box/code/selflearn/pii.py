# -*- coding: utf-8 -*-
"""S1 清洗：PII 占位、地址模糊化、短句合并。"""
from __future__ import annotations

import re
from typing import Any

PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
BANK = re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4,7}(?!\d)")
PASSWORD = re.compile(r"(?i)(密码|口令|验证码|password)\s*[:：=]?\s*\S+")
ADDRESS = re.compile(r"([\u4e00-\u9fff]{2,}(?:路|街|道|巷|小区|花园|大厦|公寓))(\d{1,5}号|\d{1,3}栋|\d{1,3}单元|\d{2,4}室)")
PLACEHOLDERS = {
    "phone": "<手机号>", "id": "<身份证>", "bank": "<银行卡>",
    "password": "<密码>", "address": "<地址>",
}


def scrub_pii(text: str) -> tuple[str, list[dict[str, Any]]]:
    value = str(text or "")
    hits: list[dict[str, Any]] = []
    for kind, pattern in (("phone", PHONE), ("id", ID_CARD), ("bank", BANK),
                          ("address", ADDRESS), ("password", PASSWORD)):
        def repl(m):
            hits.append({"kind": kind, "span": m.group(0)[:60]})
            return PLACEHOLDERS[kind]
        value = pattern.sub(repl, value)
    return value, hits


def fuzz_location(location: str) -> str:
    """只保留城市 + 场所类型；无法识别返回空串。"""
    value = str(location or "").strip()
    if not value:
        return ""
    city = re.match(r"(.{1,8}?市|.{1,8}?区|.{1,8}?县)", value)
    base = city.group(1) if city else ""
    if "小区" in value or "花园" in value:
        return base + "住宅区"
    if "路" in value or "街" in value or "巷" in value:
        return base + "街道路段"
    if "公司" in value or "大厦" in value or "写字楼" in value:
        return base + "办公区"
    if "学校" in value or "大学" in value:
        return base + "学校"
    return base or "<地点>"


def merge_short_bursts(items: list[dict[str, Any]], gap_sec: int = 60) -> list[dict[str, Any]]:
    """同一发送者短间隔的连续短句合并（不合并图片/系统消息）。"""
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        last = out[-1] if out else None
        text = str(item.get("text") or "").strip()
        can = (last is not None and text and len(text) <= 20
               and str(last.get("sender")) == str(item.get("sender"))
               and str(last.get("kind")) == "text" and str(item.get("kind")) == "text"
               and float(item.get("ts") or 0) - float(last.get("ts") or 0) <= gap_sec)
        if can:
            last["text"] = (str(last.get("text") or "") + " " + text).strip()
            last["ts"] = item.get("ts") or last.get("ts")
            last["merged"] = int(last.get("merged") or 1) + 1
        else:
            out.append(dict(item))
    return out
