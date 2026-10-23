# -*- coding: utf-8 -*-
"""11/12 主动表达的欲望与候选。

算分不只看用户空闲，而是把这几件事一起加权：
- 它自己的心情 mood / 分享欲 sharing（state.self）；
- 信任 trust（信任账本，含关系阶段）；
- 未解决话题（trust_events 里没解决的负面事件 + intents 里挂着的约定）；
- 空闲 idle（最后一次互动到现在多久）。

本模块只生成候选：不发送、不写库、不联网。真正发不发由调用方看 available 决定。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import db, memory, state, trust
from .config import DB_PATH

# 说话的下限：心情/分享欲低于这个值就不主动，和 proactive.tick 的边界一致
MOOD_FLOOR = 0.32
SHARING_FLOOR = 0.30
BATTERY_FLOOR = 0.15
MIN_SCORE = 0.45
# 刚聊过就不插话；再久一点才谈得上“主动”
MIN_IDLE_SECONDS = 300.0
MIN_IDLE_HOURS = 1.5
# 两次主动表达之间的冷却，避免刷屏（读 proactive 表，不写）
COOLDOWN = 2 * 3600
MAX_CANDIDATES = 5
WEIGHTS = {
    "mood": 0.30,
    "sharing": 0.30,
    "trust": 0.15,
    "topics": 0.15,
    "idle": 0.10,
}


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = low
    return max(low, min(high, value))


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(text: Any, limit: int = 40) -> str:
    return " ".join(str(text or "").split())[:limit]


def _last_interaction(path: Path | str) -> float:
    con = db.connect(path)
    try:
        row = con.execute("SELECT max(ts) FROM messages").fetchone()
        return float(row[0] or 0) if row else 0.0
    finally:
        con.close()


def _last_proactive(path: Path | str) -> float:
    con = db.connect(path)
    try:
        row = con.execute("SELECT max(created_at) FROM proactive").fetchone()
        return float(row[0] or 0) if row else 0.0
    finally:
        con.close()


def _open_topics(path: Path | str, now: float) -> dict[str, Any]:
    """未解决话题：没解决的负面信任事件 + 还没走完的 intent。"""
    unresolved = trust.unresolved(path)
    con = db.connect(path)
    try:
        intents = [dict(r) for r in con.execute(
            "SELECT * FROM intents WHERE status='pending' ORDER BY wake_at LIMIT 30")]
    finally:
        con.close()
    due = [r for r in intents if _num(r.get("wake_at"), 0.0) <= now]
    waiting = [r for r in intents if _num(r.get("wake_at"), 0.0) > now]
    topics: list[dict[str, Any]] = []
    for row in unresolved[:3]:
        topics.append({"source": "trust", "kind": "unresolved_trust",
                       "text": str(row.get("reason") or ""), "due": True})
    for row in due[:3]:
        topics.append({"source": "intent", "kind": str(row.get("kind") or "intent"),
                       "text": str(row.get("text") or ""), "due": True})
    for row in waiting[:3]:
        topics.append({"source": "intent", "kind": str(row.get("kind") or "intent"),
                       "text": str(row.get("text") or ""), "due": False})
    pressure = min(1.0,
                   0.35 * len(unresolved)
                   + 0.45 * len(due)
                   + 0.10 * len(waiting))
    return {"unresolved": unresolved, "due": due, "waiting": waiting,
            "topics": topics, "pressure": pressure}


def _bubble_quota(mood: float, sharing: float, *, crisis: bool = False,
                  battery: float = 0.7) -> int:
    """能拆几条气泡：情绪好/分享欲高 1-3，低则 0。"""
    if crisis or mood < MOOD_FLOOR or sharing < SHARING_FLOOR or battery < BATTERY_FLOOR:
        return 0
    if mood >= 0.75 or sharing >= 0.70:
        return 3
    if mood >= 0.60 or sharing >= 0.60:
        return 2
    return 1


def _in_cooldown(path: Path | str, now: float) -> bool:
    last = _last_proactive(path)
    return bool(last) and (now - last) < COOLDOWN


def desire(path: Path | str = DB_PATH, now: float | None = None) -> dict[str, Any]:
    """此刻有多想主动开口：五因子加权 + 边界门槛。只读。"""
    now = time.time() if now is None else float(now)
    st = state.load(path)
    own = st.get("self") or {}
    mood = _clamp(own.get("mood", 0.55))
    sharing = _clamp(own.get("sharing", 0.45))
    energy = _clamp(own.get("energy", 0.6))
    battery = _clamp(own.get("battery", 0.7))
    emotion = str((st.get("emotion") or {}).get("label") or "neutral")
    boundary = st.get("boundary") or {}
    crisis = bool(boundary.get("crisis")) or emotion == "crisis"

    t = trust.get(path)
    open_topics = _open_topics(path, now)

    last = _last_interaction(path)
    if last:
        idle_seconds: float | None = max(0.0, now - last)
        idle_hours: float | None = idle_seconds / 3600.0
        # 6 小时以上算“够久”，最多拿满 idle 分
        idle_score = _clamp(idle_hours / 6.0)
    else:
        idle_seconds = None
        idle_hours = None
        idle_score = 0.5  # 还没聊过：不当作超长空闲，按空白处理

    breakdown = {
        "mood": round(WEIGHTS["mood"] * mood, 4),
        "sharing": round(WEIGHTS["sharing"] * sharing, 4),
        "trust": round(WEIGHTS["trust"] * t, 4),
        "topics": round(WEIGHTS["topics"] * open_topics["pressure"], 4),
        "idle": round(WEIGHTS["idle"] * idle_score, 4),
    }
    raw = sum(breakdown.values())
    damped = mood < 0.40 or sharing < 0.35
    # 心情/分享欲很低时，别的因素不该硬把它拉起来
    score = round(_clamp(raw * (0.6 if damped else 1.0)), 3)

    just_talked = idle_seconds is not None and idle_seconds < MIN_IDLE_SECONDS
    quota = _bubble_quota(mood, sharing, crisis=crisis, battery=battery)

    if crisis:
        available, reason = False, "对方状态不好，先不主动闲聊"
    elif mood < MOOD_FLOOR or sharing < SHARING_FLOOR:
        available, reason = False, "心情或分享欲太低，现在不太想说话"
    elif battery < BATTERY_FLOOR:
        available, reason = False, "电量太低，先歇一会儿"
    elif just_talked:
        available, reason = False, "刚聊过，先不打扰"
    elif score < MIN_SCORE:
        available, reason = False, "主动的劲还不够"
    else:
        available, reason = True, "心情和分享欲都够，想主动说点什么"

    return {
        "ok": True,
        "available": available,
        "score": score,
        "raw_score": round(raw, 4),
        "damped": damped,
        "reason": reason,
        "mood": round(mood, 3),
        "sharing": round(sharing, 3),
        "energy": round(energy, 3),
        "battery": round(battery, 3),
        "trust": round(t, 4),
        "stage": trust.stage(t),
        "emotion": emotion,
        "crisis": crisis,
        "idle_seconds": None if idle_seconds is None else round(idle_seconds, 1),
        "idle_hours": None if idle_hours is None else round(idle_hours, 2),
        "just_talked": just_talked,
        "unresolved": len(open_topics["unresolved"]),
        "due_intents": len(open_topics["due"]),
        "waiting_intents": len(open_topics["waiting"]),
        "topics": open_topics["topics"],
        "topic_pressure": round(open_topics["pressure"], 3),
        "breakdown": breakdown,
        "weights": dict(WEIGHTS),
        "threshold": MIN_SCORE,
        "bubble_quota": quota,
        "now": now,
    }


def _candidate(kind: str, text: str, reason: str, base: float, factor: str) -> dict[str, Any]:
    return {"kind": kind, "text": text, "reason": reason,
            "base": round(_clamp(base, 0.0, 1.0), 3), "factor": factor}


def _collect(path: Path | str, d: dict[str, Any]) -> list[dict[str, Any]]:
    """按五因子生成原始候选（未打分）。"""
    out: list[dict[str, Any]] = []

    if d["crisis"]:
        return [_candidate(
            "crisis_care",
            "我在。你要是不想说也没关系，我就在这儿。",
            "对方说了很重的话，先陪着",
            0.80, "crisis")]

    # 1) 未解决的不愉快 / 上次情绪偏负
    unresolved = d["unresolved"]
    if unresolved or d["emotion"] == "negative":
        base = 0.50 + 0.05 * min(2, unresolved)
        if d["emotion"] == "negative":
            base += 0.05
        text = ("上次那件事我后来还想了想，你现在还好吗？不急，你想说再说。"
                if unresolved else
                "感觉你这两天状态不太好，现在缓过来一点没？")
        reason = (f"还有 {unresolved} 件没解决的不愉快" if unresolved
                  else "上次情绪偏负")
        out.append(_candidate("care", text, reason, base, "topics"))

    # 2) 挂着的约定/提醒到期了
    due_topics = [t for t in d["topics"] if t["source"] == "intent" and t["due"]]
    for index, topic in enumerate(due_topics[:2]):
        text = _clip(topic["text"], 36)
        if text:
            out.append(_candidate(
                "follow_up", f"对了，之前说过的：{text}",
                "之前挂着的约定到期了", 0.52 - 0.06 * index, "topics"))

    # 3) 空闲：隔了一会儿，想问问
    idle_hours = d["idle_hours"]
    if idle_hours is None:
        out.append(_candidate(
            "check_in", "在吗？还没怎么聊过，想先跟你说句话。",
            "还没有对话记录", 0.30 + 0.08 * d["sharing"], "idle"))
    elif idle_hours >= MIN_IDLE_HOURS:
        text = ("隔了挺久，突然想问问你在忙什么。" if idle_hours >= 12
                else "刚才忙去了？突然想问一句在干嘛。")
        base = 0.34 + min(0.22, idle_hours / 48.0) + 0.08 * d["sharing"]
        out.append(_candidate(
            "check_in", text, f"已经 {idle_hours:.1f} 小时没互动", base, "idle"))

    # 4) 心情/分享欲：想说点自己的事
    if d["mood"] >= 0.60 and d["sharing"] >= 0.50:
        out.append(_candidate(
            "share_self", "今天心情莫名不错，想跟你说两句。",
            "心情不错，分享欲也够",
            0.40 + 0.12 * d["mood"] + 0.08 * d["sharing"], "mood"))
    if d["sharing"] >= 0.68 and d["trust"] >= 0.55:
        out.append(_candidate(
            "share_thought",
            "刚发呆想到个事：我们这样一点点聊下来，好像真的熟起来了。",
            "分享欲高，信任也够",
            0.38 + 0.10 * d["sharing"] + 0.10 * d["trust"], "sharing"))

    # 5) 记忆回访：信任够才翻旧话
    if d["trust"] >= 0.45:
        mems = [m for m in memory.list_user_memories(path, 10)
                if str(m.get("status") or "active") == "active"
                and _clip(m.get("content"))]
        if mems:
            content = _clip(mems[0]["content"], 36)
            out.append(_candidate(
                "share_memory",
                f"我刚又想到你之前说过「{content}」，现在还是这样吗？",
                "记忆里有可以回访的事",
                0.40 + 0.08 * d["trust"], "trust"))

    # 6) 电量/精力低：提前说一声，不是冷淡
    if (d["battery"] <= 0.30 or d["energy"] <= 0.30) and d["trust"] >= 0.50:
        out.append(_candidate(
            "low_energy", "今天有点没电，可能话会少一点，不是不想理你。",
            "电量/精力偏低，提前说一声",
            0.36 + 0.10 * d["trust"], "energy"))

    # 7) 兜底：总有一个能开口的候选
    if not out:
        out.append(_candidate(
            "check_in", "在吗？突然想跟你说句话。",
            "没什么特别理由，就是想主动一次",
            0.28 + 0.10 * d["mood"] + 0.08 * d["sharing"], "mood"))
    return out


def _finish(items: list[dict[str, Any]], d: dict[str, Any]) -> list[dict[str, Any]]:
    """用当前五因子给候选统一加权，去重、排序、截断。"""
    boost = (0.15 * (d["mood"] - 0.5)
             + 0.15 * (d["sharing"] - 0.5)
             + 0.10 * (d["trust"] - 0.5)
             + 0.05 * d["topic_pressure"])
    seen: set[tuple[str, str]] = set()
    scored: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda c: (c["kind"], c["text"])):
        key = (item["kind"], item["text"])
        if key in seen:
            continue
        seen.add(key)
        item["score"] = round(_clamp(item["base"] + boost, 0.0, 0.95), 3)
        scored.append(item)
    scored.sort(key=lambda c: (-c["score"], c["kind"]))
    return scored[:MAX_CANDIDATES]


def candidates(path: Path | str = DB_PATH,
               now: float | None = None) -> list[dict[str, Any]]:
    """候选列表（含分数），按分数从高到低。只读，不发送。"""
    now = time.time() if now is None else float(now)
    d = desire(path, now)
    return _finish(_collect(path, d), d)


def decide(path: Path | str = DB_PATH, now: float | None = None) -> dict[str, Any]:
    """给调用方的决定：现在是否适合主动、候选是什么、最多几条气泡。

    available=True 也只是“可以主动”，不会发送任何消息。
    """
    now = time.time() if now is None else float(now)
    d = desire(path, now)
    cands = _finish(_collect(path, d), d)
    best = cands[0] if cands else None

    if best is None:
        available, reason = False, "没有合适的候选"
    elif _in_cooldown(path, now):
        available, reason = False, "主动表达冷却中"
    elif not d["available"]:
        available, reason = False, d["reason"]
    elif best["score"] < MIN_SCORE:
        available, reason = False, "候选分数还不够"
    else:
        available, reason = True, best["reason"]

    quota = int(d["bubble_quota"]) if available and best is not None else 0
    return {
        "ok": True,
        "available": bool(available),
        "score": round(float(best["score"]), 3) if best else 0.0,
        "desire_score": d["score"],
        "reason": reason,
        "kind": best["kind"] if available and best else None,
        "text": best["text"] if available and best else "",
        "best": best,
        "candidates": cands,
        "max_bubbles": quota,
        "bubble_count": quota,
        "desire": d,
        "note": "只生成候选，不会自动发送",
        "now": now,
    }
