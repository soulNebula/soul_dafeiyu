# -*- coding: utf-8 -*-
"""S5 表情包人格档案：从本地使用记录统计情绪分布/Top/攻击性/自嘲。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import db

EMOTIONS = ("happy", "angry", "sad", "shy", "confused", "speechless", "daily")
ATTACK_WORDS = ("怼", "骂", "打", "白眼", "嘲讽", "生气")
MOCK_WORDS = ("自嘲", "摆烂", "废物", "菜", "丑", "穷", "搞笑")


def _tags(raw) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
        return [str(x) for x in value] if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def build_profile(path: Path | str = "agi/data/selflearn/selflearn.sqlite3",
                  subject: str = "selflearn") -> dict[str, Any]:
    con = db.connect(path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT a.name,a.tags_json,a.usage_count,count(u.id) n "
            "FROM emoji_archive a LEFT JOIN emoji_usage u ON u.emoji_id=a.id "
            "GROUP BY a.id ORDER BY n DESC, a.name")]
        try:
            messages = int(con.execute(
                "SELECT count(*) FROM messages WHERE role='user'").fetchone()[0])
        except Exception:  # noqa: BLE001
            messages = 0
    finally:
        con.close()
    total = sum(int(r.get("n") or 0) for r in rows)
    emotion_counts = {e: 0 for e in EMOTIONS}
    attack = mock = 0
    top = []
    for row in rows:
        n = int(row.get("n") or 0)
        tags = _tags(row.get("tags_json"))
        joined = " ".join(tags) + " " + str(row.get("name") or "")
        for emotion in EMOTIONS:
            if any(emotion == t or emotion in t for t in tags):
                emotion_counts[emotion] += n
                break
        else:
            emotion_counts["daily"] += n
        if any(w in joined for w in ATTACK_WORDS):
            attack += n
        if any(w in joined for w in MOCK_WORDS):
            mock += n
        if n:
            top.append({"name": row["name"], "count": n, "tags": tags})
    dist = {k: round(v / total, 3) for k, v in emotion_counts.items()} if total else {e: 0.0 for e in EMOTIONS}
    ratio = round(total / messages, 3) if messages else 0.0
    signature = "表情用得不多，主要靠文字表达。"
    if total and dist.get("happy", 0) >= 0.4:
        signature = "用开心/卖萌的表情缓和气氛，很少用攻击性表情。"
    elif total and attack / total >= 0.3:
        signature = "会用带攻击性的表情表达情绪，但多数时候仍是玩笑语气。"
    elif total and mock / total >= 0.3:
        signature = "常用自嘲化解尴尬，不太主动怼人。"
    elif total:
        signature = "表情类型比较杂，更多是随手补一个语气。"
    return {"ok": True, "subject": subject, "total_used": total,
            "text_emoji_ratio": ratio, "emotion_dist": dist,
            "aggression": round(attack / total, 3) if total else 0.0,
            "self_mockery": round(mock / total, 3) if total else 0.0,
            "top_emoji": top[:20], "signature": signature,
            "note": "基于本地标签/文件名弱推断，不是视觉模型分类。"}
