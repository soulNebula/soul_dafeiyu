# -*- coding: utf-8 -*-
"""11/12 分享欲与主动表达候选：只生成候选，不自动发送。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from . import db, initiative, memory, share_store, sharing, state, style_profile, trust
from .config import DB_PATH

COOLDOWN = 2 * 3600
MIN_SCORE = 0.45
DAILY_BUDGET = 6


def _id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def _last_interaction(path: Path | str) -> float:
    con = db.connect(path)
    try:
        row = con.execute("SELECT max(ts) FROM messages").fetchone()
        return float(row[0] or 0) if row else 0.0
    finally:
        con.close()


def _cooldown_ok(path: Path | str, now: float) -> bool:
    con = db.connect(path)
    try:
        row = con.execute("SELECT max(created_at) FROM proactive").fetchone()
        return not row or not row[0] or now - float(row[0]) >= COOLDOWN
    finally:
        con.close()


def build_candidates(path: Path | str = DB_PATH, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else float(now)
    last = _last_interaction(path)
    idle = (now - last) / 3600 if last else 99
    mems = memory.list_user_memories(path, 10)
    t = trust.get(path)
    st = state.load(path)
    mood = st["emotion"]["label"]
    candidates = []
    if idle >= 3:
        candidates.append({
            "kind": "check_in", "text": "隔了一会儿，突然想问问你：刚才聊的那件事后来怎么样了？",
            "reason": f"已 {idle:.1f} 小时没互动", "base": min(0.6, 0.2 + idle / 24),
        })
    if mems and t >= 0.4:
        m = mems[0]
        candidates.append({
            "kind": "share_memory",
            "text": f"我刚想到你之前说过「{m['content']}」。现在还是这样吗？",
            "reason": "记忆里有可回访的内容", "base": 0.5,
        })
    if t >= 0.55:
        candidates.append({
            "kind": "share_thought",
            "text": "今天脑子冒出一个念头：我们这样慢慢聊下去，会变成什么样？",
            "reason": "信任足够，适合分享自己的念头", "base": 0.45,
        })
    if mood == "negative":
        candidates.append({
            "kind": "care", "text": "你上次情绪不太好，现在有没有好一点？不急，想说再说。",
            "reason": "上次情绪偏负", "base": 0.55,
        })
    if not candidates:
        candidates.append({
            "kind": "check_in", "text": "在吗？突然想听听你的声音。",
            "reason": "没有特别理由，只是想主动一次", "base": 0.35,
        })
    for c in candidates:
        c["score"] = round(min(0.95, c["base"] + 0.1 * t), 3)
    return sorted(candidates, key=lambda x: -x["score"])


def tick(path: Path | str = DB_PATH, client=None, now: float | None = None,
         idle_seconds: float = 30, cooldown: float = 900) -> dict[str, Any]:
    """网页轮询：由它自己的心情/分享欲决定说不说，可一次说 1-2 条。"""
    now = time.time() if now is None else float(now)
    own = state.load(path)["self"]
    mood = float(own.get("mood", 0.55))
    sharing = float(own.get("sharing", 0.45))
    if mood < 0.32 or sharing < 0.30:
        return {"ok": True, "available": False, "reason": "现在不太想说话"}
    last = _last_interaction(path)
    if not last:
        return {"ok": True, "available": False, "reason": "还没有对话"}
    if now - last < 8:
        return {"ok": True, "available": False, "reason": "刚聊过"}
    if mood < 0.55 and sharing < 0.65 and now - last < idle_seconds:
        return {"ok": True, "available": False, "reason": "还在聊"}
    con = db.connect(path)
    try:
        row = con.execute("SELECT max(created_at) FROM proactive").fetchone()
        last_p = float(row[0] or 0) if row else 0.0
    finally:
        con.close()
    if last_p and now - last_p < cooldown:
        return {"ok": True, "available": False, "reason": "主动表达冷却中"}
    con = db.connect(path)
    try:
        daily = int(con.execute(
            "SELECT count(*) FROM proactive WHERE created_at>=? AND status IN ('spoken','proposed')",
            (now - 86400,)).fetchone()[0])
    finally:
        con.close()
    if daily >= DAILY_BUDGET:
        return {"ok": True, "available": False, "reason": "今日主动预算已用完"}
    candidates = build_candidates(path, now)
    init_info = {}
    try:
        init_info = initiative.decide(path, now)
    except Exception:  # noqa: BLE001
        init_info = {}
    share_info = {}
    try:
        share_info = sharing.choose_level(path)
    except Exception:  # noqa: BLE001
        share_info = {}
    best = candidates[0]
    quadrant = str(init_info.get("quadrant") or "")
    if quadrant == "skip" or (quadrant == "hold" and int(init_info.get("confidence_n") or 0) >= 3):
        return {"ok": True, "available": False,
                "reason": f"决策象限 {quadrant}", "initiative": init_info,
                "sharing_level": share_info}
    if best["score"] < MIN_SCORE:
        return {"ok": True, "available": False, "reason": "分享欲不足"}
    recent = memory.recent_messages(path, limit=8)
    dialog = " | ".join(f"{m['role']}:{m['text']}" for m in recent[-6:])
    prompt = (
        f"你现在心情 {mood:.2f}，分享欲 {sharing:.2f}。用户安静了，你想主动说点什么。"
        "只输出你要说的话，1-2 句，口语；可以分享自己的念头或问一句，不要客服腔、不报能力。"
        "如果实在没话说，输出 SKIP。最近对话：" + dialog
    )
    text = ""
    if client is not None and getattr(client, "available", lambda: False)():
        r = client.chat([{"role": "system", "content": "你说话像普通人，短句。"},
                         {"role": "user", "content": prompt}],
                        purpose="selflearn_proactive", max_tokens=150, temperature=0.8)
        if r.get("ok"):
            text = str(r.get("text") or "").strip()
    if not text or text.upper() == "SKIP":
        text = best["text"]
    text = style_profile.tune_reply("", text)[:200]
    max_bubbles = 3 if sharing >= 0.7 else (2 if sharing >= 0.55 or mood >= 0.7 else 1)
    if int(init_info.get("max_bubbles") or 0) > 0:
        max_bubbles = min(max_bubbles, int(init_info["max_bubbles"]))
    if quadrant == "explore":
        max_bubbles = min(max_bubbles, 1)
    bubbles = style_profile.split_bubbles(text, max_bubbles)
    bubbles = [style_profile.strip_periods(b) for b in bubbles if style_profile.strip_periods(b)]
    if not bubbles:
        return {"ok": True, "available": False, "reason": "没组织好语言"}
    ids = [memory.store_message(path, "assistant", b, now=now, proactive=True) for b in bubbles]
    joined = " ".join(bubbles)
    try:
        cand = share_store.record_candidate(
            path, level=str(share_info.get("level") or ""),
            kind=best["kind"], text=joined, reason=best["reason"],
            source="proactive", score=best["score"], status="spoken")
        if cand.get("id"):
            share_store.record_event(path, cand["id"], "spoken",
                                     {"quadrant": quadrant})
    except Exception:  # noqa: BLE001
        pass
    con = db.connect(path)
    try:
        con.execute("INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (_id("PRO-", best["kind"], joined, now), best["kind"], joined,
                     best["reason"], best["score"], "spoken", now))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "available": True, "message": bubbles[0], "messages": bubbles,
            "message_id": ids[0], "kind": best["kind"], "score": best["score"],
            "reason": best["reason"], "mood": mood, "sharing": sharing,
            "initiative": {k: init_info.get(k) for k in
                            ("available", "score", "reason", "max_bubbles", "bubble_count",
                             "confidence_lcb", "quadrant")},
            "sharing_level": share_info}


def propose(path: Path | str = DB_PATH, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    if not _cooldown_ok(path, now):
        return {"ok": True, "available": False, "reason": "主动表达冷却中"}
    candidates = build_candidates(path, now)
    init_info = {}
    try:
        init_info = initiative.decide(path, now)
    except Exception:  # noqa: BLE001
        init_info = {}
    share_info = {}
    try:
        share_info = sharing.choose_level(path)
    except Exception:  # noqa: BLE001
        share_info = {}
    best = candidates[0]
    quadrant = str(init_info.get("quadrant") or "")
    if quadrant == "skip" or (quadrant == "hold" and int(init_info.get("confidence_n") or 0) >= 3):
        return {"ok": True, "available": False,
                "reason": f"决策象限 {quadrant}", "initiative": init_info,
                "sharing_level": share_info}
    if best["score"] < MIN_SCORE:
        return {"ok": True, "available": False, "reason": "分享欲分数不足",
                "candidates": candidates}
    pid = _id("PRO-", best["kind"], best["text"], now)
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (pid, best["kind"], best["text"], best["reason"], best["score"], "proposed", now))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "available": True, "id": pid, **best,
            "candidates": candidates,
            "initiative": {k: init_info.get(k) for k in
                           ("available", "score", "confidence_lcb", "quadrant")},
            "sharing_level": share_info,
            "note": "这是候选，不会自动发送"}


def decide(path: Path | str, proposal_id: str, action: str) -> dict[str, Any]:
    action = str(action).lower()
    if action not in ("accepted", "rejected"):
        return {"ok": False, "error": "action 只允许 accepted/rejected"}
    con = db.connect(path)
    try:
        cur = con.execute("UPDATE proactive SET status=? WHERE id=? AND status='proposed'",
                          (action, proposal_id))
        con.commit()
        row = con.execute("SELECT * FROM proactive WHERE id=?", (proposal_id,)).fetchone()
    finally:
        con.close()
    if cur.rowcount == 0:
        return {"ok": False, "error": "候选不存在或已处理"}
    return {"ok": True, "action": action, "proposal": dict(row)}


def list_proactive(path: Path | str = DB_PATH, limit: int = 20) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM proactive ORDER BY created_at DESC LIMIT ?", (int(limit),))]
    finally:
        con.close()
