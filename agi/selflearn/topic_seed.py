# -*- coding: utf-8 -*-
"""P8-b：主动消息话题种子（本地只读记忆，不调 LLM）。

从 memory 库取 2-3 个候选：user_memories active、claims active、24-72h 前用户消息；
过滤敏感/物理起居/最近 7 天主动消息重复；最多 3 条，每条 ≤30 字。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from agi.paths import data_dir as _default_data_dir

ROOT = Path(__file__).resolve().parents[2]
WECHAT_CONFIG = _default_data_dir() / "wechat" / "config.json"
MAX_CANDIDATES = 3
MAX_CHARS = 30
RECENT_DAYS = 7
_NEGATIVE = ("密码", "银行卡", "身份证", "详细住址", "家庭住址", "验证码")
BILI_MAX = 2


def enabled() -> bool:
    env = os.environ.get("PROACTIVE_TOPIC_SEED")
    if env is not None:
        return str(env).strip().lower() in ("1", "true", "yes", "on")
    try:
        raw = json.loads(WECHAT_CONFIG.read_text(encoding="utf-8"))
        if "proactive_topic_seed" in raw:
            return bool(raw.get("proactive_topic_seed"))
    except Exception:  # noqa: BLE001
        pass
    return True


def bili_enabled() -> bool:
    """P10：B站日报候选源开关（默认开启）。"""
    env = os.environ.get("PROACTIVE_TOPIC_BILI")
    if env is not None:
        return str(env).strip().lower() in ("1", "true", "yes", "on")
    try:
        raw = json.loads(WECHAT_CONFIG.read_text(encoding="utf-8"))
        if "proactive_topic_bili" in raw:
            return bool(raw.get("proactive_topic_bili"))
    except Exception:  # noqa: BLE001
        pass
    return True


def _keywords(text: str) -> set[str]:
    value = re.sub(r"[\s，,。.！!？?；;、…「」“”\"'（）()]+", "", str(text or ""))
    words = set(re.findall(r"[A-Za-z0-9_]{2,}", value))
    for index in range(len(value) - 1):
        left, right = value[index], value[index + 1]
        if "\u4e00" <= left <= "\u9fff" and "\u4e00" <= right <= "\u9fff":
            words.add(left + right)
    return words


def _clean(text: Any) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip().strip("「」“”\"'")
    return value[:MAX_CHARS].strip()


def build(path: Path | str = "", now: float | None = None) -> dict:
    """返回 {picked, candidates, reason}；无候选时 candidates=[]。"""
    from . import db, memory as memory_mod, proactive
    now = time.time() if now is None else float(now)
    candidates: list[dict] = []
    seen: set[str] = set()
    recent_proactive: list[str] = []
    try:
        con = db.connect(path)
        try:
            rows = con.execute(
                "SELECT text FROM proactive WHERE created_at>=? ORDER BY created_at DESC",
                (now - RECENT_DAYS * 86400,)).fetchall()
            recent_proactive = [str(r[0] or "") for r in rows]
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        recent_proactive = []

    def acceptable(text: str) -> bool:
        value = _clean(text)
        if len(value) < 4 or value in seen:
            return False
        if any(word in value for word in _NEGATIVE):
            return False
        try:
            if memory_mod._sensitive(value):
                return False
        except Exception:  # noqa: BLE001
            pass
        try:
            if proactive.find_physical_detail(value) is not None:
                return False
        except Exception:  # noqa: BLE001
            pass
        words = _keywords(value)
        for recent in recent_proactive:
            if len(words & _keywords(recent)) >= 3:
                return False
        seen.add(value)
        return True

    def add(text: Any, source: str, label: str) -> None:
        if len(candidates) >= MAX_CANDIDATES:
            return
        value = _clean(text)
        if acceptable(value):
            candidates.append({"text": value, "source": source, "label": label})

    sources = {"bili": False, "report_date": ""}
    # 1) 用户记忆：active，近 30 天优先，importance*confidence 排序
    try:
        con = db.connect(path)
        try:
            rows = con.execute(
                "SELECT content FROM user_memories WHERE status='active' "
                "ORDER BY CASE WHEN created_at>=? THEN 0 ELSE 1 END, "
                "importance*confidence DESC, created_at DESC LIMIT 8",
                (now - 30 * 86400,)).fetchall()
            for row in rows:
                add(row[0], "memory", "用户之前提过")
                if len(candidates) >= MAX_CANDIDATES:
                    break
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        pass

    # 2) claims：active 最近
    if len(candidates) < MAX_CANDIDATES:
        try:
            con = db.connect(path)
            try:
                rows = con.execute(
                    "SELECT object, predicate FROM claims WHERE status='active' "
                    "ORDER BY created_at DESC LIMIT 4").fetchall()
                for row in rows:
                    add(row[0] or row[1], "claim", "用户最近在忙")
                    if len(candidates) >= MAX_CANDIDATES:
                        break
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            pass

    # 3) 24-72h 前用户可延续消息
    if len(candidates) < MAX_CANDIDATES:
        try:
            con = db.connect(path)
            try:
                rows = con.execute(
                    "SELECT text FROM messages WHERE role='user' AND ts>=? AND ts<=? "
                    "ORDER BY ts DESC LIMIT 10",
                    (now - 72 * 3600, now - 24 * 3600)).fetchall()
                for row in rows:
                    raw = str(row[0] or "").strip()
                    if len(raw) < 8 or raw.endswith(("？", "?")):
                        continue
                    add(raw, "message", "用户最近在忙")
                    if len(candidates) >= MAX_CANDIDATES:
                        break
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            pass

    # 4) P10：B站日报（记忆类之后；最多 2 条，标题清洗；why 只作可聊方向）
    if len(candidates) < MAX_CANDIDATES and bili_enabled():
        try:
            from agi.integrations import bili_digest
            latest = bili_digest.latest_topics(days=2, limit=3, now=now)
            sources["report_date"] = str(latest.get("report_date") or "")
            for topic in latest.get("topics") or []:
                title = bili_digest.clean_title(topic.get("title"), MAX_CHARS)
                if not title:
                    continue
                value = _clean(title)
                if not acceptable(value):
                    continue
                candidates.append({
                    "text": value, "source": "bili",
                    "label": "最近刷到一条视频",
                    "direction": _clean(topic.get("why")),
                    "bv": str(topic.get("bv") or ""),
                    "date": str(topic.get("date") or ""),
                })
                sources["bili"] = True
                bili_count = sum(1 for item in candidates
                                 if item.get("source") == "bili")
                if len(candidates) >= MAX_CANDIDATES or bili_count >= BILI_MAX:
                    break
        except Exception:  # noqa: BLE001
            pass

    picked = candidates[0]["text"] if candidates else ""
    reason = ("no_candidates" if not candidates
              else "selected:" + ",".join(item["source"] for item in candidates))
    return {"picked": picked, "candidates": candidates[:MAX_CANDIDATES],
            "reason": reason, "sources": sources}


def prompt_block(seed: dict | None) -> str:
    candidates = list((seed or {}).get("candidates") or [])
    if not candidates:
        return ""
    lines = ["可自然提起的旧话题（可选，不必都用；用就具体，不要复述记忆原文）："]
    for item in candidates[:MAX_CANDIDATES]:
        if item.get("source") == "bili":
            lines.append(f"- 最近刷到「{item['text']}」（视频，可聊）")
        else:
            lines.append(f"- {item.get('label') or '用户之前提过'}「{item['text']}」")
    lines.append("最多提 1 个；不要每条都提；不要写“我记得你说过…”这种复述腔。")
    return "\n".join(lines)
