# -*- coding: utf-8 -*-
"""从真实对话的后续行为推断隐式反馈。

这不是用户点击的反馈，也不能当作用户满意度：只在下一句用户消息有明确
“继续/谢谢”或“不对/重来”时打 good/bad，其余连续追问记 ok，并写 note 标明来源。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from . import db

POS = re.compile(r"(谢谢|好的|好呀|可以|懂了|明白了|哈哈|嗯嗯|继续|对|有用|不错)")
NEG = re.compile(r"(不对|错了|不是这样|不好|听不懂|重来|算了|别|烦|没用|答非所问|乱说|？{2,}|为什么这样)")
MAX_GAP = 2 * 3600


def _id(*parts: Any) -> str:
    import hashlib
    return "F-IMP-" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def infer(path: Path | str, *, limit: int = 80, now: float | None = None) -> dict:
    """给没有反馈的 turns 推断隐式反馈；不覆盖真实反馈。"""
    con = db.connect(path)
    inserted = []
    try:
        turns = [dict(r) for r in con.execute(
            "SELECT id,reply,feedback,created_at FROM turns "
            "WHERE feedback IS NULL ORDER BY created_at ASC LIMIT ?", (max(1, int(limit)),))]
        for turn in turns:
            nxt = con.execute(
                "SELECT id,text,ts FROM messages WHERE role='user' AND ts>? "
                "ORDER BY ts ASC LIMIT 1", (float(turn["created_at"]),)).fetchone()
            if not nxt:
                continue
            gap = float(nxt["ts"]) - float(turn["created_at"])
            if gap <= 0 or gap > MAX_GAP:
                continue
            text = str(nxt["text"] or "")
            if NEG.search(text):
                reaction = "bad"
            elif POS.search(text):
                reaction = "good"
            else:
                reaction = "ok"
            note = f"implicit:next_user_message:{nxt['id']}"
            fid = _id(turn["id"], nxt["id"])
            try:
                con.execute(
                    "INSERT OR IGNORE INTO feedback(id,turn_id,reaction,score,note,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (fid, turn["id"], reaction, {"good": 1.0, "ok": 0.0, "bad": -1.0}[reaction],
                     note, time.time()))
                con.execute("UPDATE turns SET feedback=? WHERE id=? AND feedback IS NULL",
                            (reaction, turn["id"]))
                inserted.append({"turn_id": turn["id"], "reaction": reaction, "note": note})
            except Exception:  # noqa: BLE001
                continue
        con.commit()
    finally:
        con.close()
    counts = {"good": 0, "ok": 0, "bad": 0}
    for item in inserted:
        counts[item["reaction"]] += 1
    return {"ok": True, "inserted": len(inserted), "counts": counts, "items": inserted[:50],
            "note": "隐式反馈，来自下一句用户消息，不等于用户主动评价"}
