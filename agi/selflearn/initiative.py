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

from . import db, decision, memory, state, trust
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
    confidence = 0.0
    confidence_n = 0
    try:
        con = db.connect(path)
        try:
            row = con.execute(
                "SELECT count(*) n, sum(CASE WHEN reaction='good' THEN 1 ELSE 0 END) ok "
                "FROM feedback").fetchone()
        finally:
            con.close()
        confidence_n = int(row["n"] or 0)
        confidence = decision.wilson_lcb(int(row["ok"] or 0), confidence_n)
    except Exception:  # noqa: BLE001
        pass
    score = float(best["score"]) if best else 0.0
    quadrant = decision.quadrant(score, confidence)
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
        "confidence_lcb": round(confidence, 4),
        "confidence_n": confidence_n,
        "quadrant": quadrant,
        "note": "只生成候选，不会自动发送",
        "now": now,
    }


# =========================================================================== #
# P33：主动度 drive（关系熟悉度 × 心情 × 信任 → 概率↑/间隔↓/分享偏好）
# =========================================================================== #
DEFAULT_MOOD = 0.55
DEFAULT_TRUST = 0.5
#: drive ≤ 0.35 时只允许这两类（不发分享/关心/追问）
LOW_DRIVE_ALLOWED = ("light", "progress_check")
#: 分享类 kind（权重受 drive 放大）
SHARE_KINDS = ("share", "share_video", "share_find", "share_thought")
INITIATIVE_KEY = "initiative_enabled"


def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _cfg_float(config: dict | None, key: str, default: float) -> float:
    try:
        return float((config or {}).get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _cfg_int(config: dict | None, key: str, default: int) -> int:
    try:
        return int((config or {}).get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _local_midnight(now: float) -> float:
    # 用「当前时刻 - 本地当天已过秒数」算本地 0 点：避免 Windows mktime 对 1970
    # 附近/负时间戳抛 OverflowError（测试常用 now=100 这类小值）。
    value = float(now)
    tm = time.localtime(value)
    return value - (tm.tm_hour * 3600 + tm.tm_min * 60 + tm.tm_sec)


def _memory_db(path: Path | str) -> Path:
    try:
        return Path(db.memory_path(Path(path)))
    except Exception:  # noqa: BLE001
        return Path(path)


def _user_message_ts(path: Path | str) -> list[float]:
    """只读取出历史 user 消息时间戳（缺库/坏库 → []）。"""
    target = _memory_db(path)
    if not target.exists():
        return []
    con = None
    try:
        con = _sqlite_ro(target)
        rows = con.execute("SELECT ts FROM messages WHERE role='user'").fetchall()
        return [float(row[0] or 0.0) for row in rows]
    except Exception:  # noqa: BLE001
        return []
    finally:
        if con is not None:
            con.close()


def _sqlite_ro(path: Path):
    import sqlite3
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)


def familiarity(path: Path | str, now: float | None = None) -> dict[str, Any]:
    """关系熟悉度（0–1）：活跃天数 45% + 总消息量 35% + 近 7 天 20%。

    全部用 log1p：30 天活跃 / 2000 条历史 / 近 7 天 150 条 = 满分 1.0。
    """
    now = time.time() if now is None else float(now)
    timestamps = _user_message_ts(path)
    per_day: dict[tuple[int, int, int], int] = {}
    for ts in timestamps:
        try:
            day = tuple(time.localtime(ts)[:3])
        except (OSError, ValueError):
            continue
        per_day[day] = per_day.get(day, 0) + 1
    days_active = sum(1 for count in per_day.values() if count >= 3)
    total = len(timestamps)
    cutoff = now - 7 * 86400.0
    recent_7d = sum(1 for ts in timestamps if ts >= cutoff)
    import math
    value = (0.45 * math.log1p(days_active) / math.log1p(30)
             + 0.35 * math.log1p(total) / math.log1p(2000)
             + 0.20 * math.log1p(recent_7d) / math.log1p(150))
    return {"familiarity": round(_clamp01(value), 4),
            "days_active": int(days_active), "total_user_msgs": int(total),
            "recent_7d_msgs": int(recent_7d), "now": now}


def mood_factor(path: Path | str) -> float:
    """现有心情快照 0–1（缺省 0.55；若上游给 0–100 则 /100）。"""
    value: Any = None
    try:
        from . import affect
        value = affect.snapshot(path).get("mood")
    except Exception:  # noqa: BLE001
        try:
            value = state.load(path).get("self", {}).get("mood")
        except Exception:  # noqa: BLE001
            value = None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_MOOD
    if number > 1.5:          # 兼容 0–100 的心情标度
        number /= 100.0
    return round(_clamp01(number), 4)


def trust_factor(path: Path | str) -> float:
    """现有信任快照 0–1（缺省 0.5）。"""
    try:
        from . import trust as _trust
        value = _trust.snapshot(path).get("trust")
    except Exception:  # noqa: BLE001
        try:
            from . import trust as _trust
            value = _trust.get(path)
        except Exception:  # noqa: BLE001
            value = None
    try:
        return round(_clamp01(value, DEFAULT_TRUST), 4)
    except (TypeError, ValueError):
        return DEFAULT_TRUST


def drive(path: Path | str, now: float | None = None) -> dict[str, Any]:
    """主动度 = 0.45*familiarity + 0.35*mood + 0.20*trust（0–1）。"""
    fam = familiarity(path, now)
    mood = mood_factor(path)
    trust_value = trust_factor(path)
    value = 0.45 * float(fam["familiarity"]) + 0.35 * mood + 0.20 * trust_value
    return {"familiarity": fam["familiarity"],
            "days_active": fam["days_active"],
            "total_user_msgs": fam["total_user_msgs"],
            "recent_7d_msgs": fam["recent_7d_msgs"],
            "mood": mood, "trust": trust_value,
            "drive": round(_clamp01(value), 4)}


def user_msgs_today(path: Path | str, now: float | None = None) -> int:
    now = time.time() if now is None else float(now)
    midnight = _local_midnight(now)
    return sum(1 for ts in _user_message_ts(path) if ts >= midnight)


def today_sent(path: Path | str, now: float | None = None) -> int:
    """当天已发的主动消息条数（proactive_events；缺表/坏库 → 0）。"""
    now = time.time() if now is None else float(now)
    midnight = _local_midnight(now)
    target = _memory_db(path)
    if not target.exists():
        return 0
    con = None
    try:
        con = _sqlite_ro(target)
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "proactive_events" not in tables:
            return 0
        columns = {row[1] for row in con.execute(
            "PRAGMA table_info(proactive_events)")}
        kind_filter = " AND COALESCE(kind,'') != 'reply'" if "kind" in columns else ""
        row = con.execute(
            "SELECT count(*) FROM proactive_events WHERE status='sent'"
            + kind_filter + " AND ts>=?", (midnight,)).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:  # noqa: BLE001
        return 0
    finally:
        if con is not None:
            con.close()


def daily_cap(path: Path | str, now: float | None = None, *,
              config: dict | None = None) -> dict[str, Any]:
    """P33-补：按当天聊天频率动态日上限（最少 12，最多 100）。

    ``cap = clamp(cap_min, cap_max, round(user_msgs_today * 1.2) + 4)``：
    今天用户 0 条 → 12；10 条 → 16；30 条 → 40；80/100 条 → 100。
    旧配置 ``pacing_daily_cap`` 作为 cap_min 兜底（向后兼容）。
    """
    now = time.time() if now is None else float(now)
    config = dict(config or {})
    cap_min = _cfg_int(config, "pacing_daily_cap_min", 12)
    cap_max = _cfg_int(config, "pacing_daily_cap_max", 100)
    if "pacing_daily_cap_min" not in config and "pacing_daily_cap" in config:
        cap_min = _cfg_int(config, "pacing_daily_cap", 12)   # 兼容旧键
    cap_min = max(1, cap_min)
    cap_max = max(cap_min, cap_max)
    today = user_msgs_today(path, now)
    raw = int(round(today * 1.2)) + 4
    cap = max(cap_min, min(cap_max, raw))
    return {"daily_cap": int(cap), "user_msgs_today": int(today),
            "cap_min": cap_min, "cap_max": cap_max, "now": now}


def enabled(config: dict | None, key: str = INITIATIVE_KEY) -> bool:
    """主动度开关（默认 true；显式 false/0/off 关闭）。"""
    if not isinstance(config, dict) or key not in config:
        return True
    value = config.get(key)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def note(config_key: str = INITIATIVE_KEY, default: bool = True) -> str:
    """配置说明（给文档/控制台展示用）。"""
    return (f"{config_key} 默认 {'开启' if default else '关闭'}；"
            "可在 agi/data/wechat/config.json 覆盖")


def _base_probability(config: dict) -> float:
    fixed = config.get("proactive_probability")
    if config.get("proactive_probability_min") is None \
            and config.get("proactive_probability_max") is None \
            and fixed is not None:
        try:
            return max(0.0, min(1.0, float(fixed)))
        except (TypeError, ValueError):
            return 0.55
    lo = _cfg_float(config, "proactive_probability_min", 0.3)
    hi = _cfg_float(config, "proactive_probability_max", 0.9)
    lo, hi = max(0.0, min(1.0, lo)), max(0.0, min(1.0, hi))
    if lo > hi:
        lo, hi = hi, lo
    return (lo + hi) / 2.0


def _in_quiet(quiet: Any, hour: int) -> bool:
    if not isinstance(quiet, (list, tuple)) or len(quiet) != 2:
        return False
    try:
        start, end = int(quiet[0]), int(quiet[1])
    except (TypeError, ValueError):
        return False
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def adjust(config: dict | None, drive_result: dict[str, Any], *,
           now: float | None = None, stopped: bool = False) -> dict[str, Any]:
    """P33 纯函数：按主动度调整本轮概率/间隔（不写盘）。

    - prob = base * (0.55 + 0.9*drive)，clamp [0.15, 0.92]
    - interval *= (1.15 - 0.5*drive)，clamp [20min, 120min]
    - 保护优先：STOP / 静默 / 当天动态日上限 / 连续 3 条无回应 → drive 不生效；
      前三者直接 ``send=False``，无回应则回保守值（prob≤0.2、interval≥3600s）。
    """
    config = dict(config or {})
    result = dict(drive_result or {})
    value = _clamp01(result.get("drive"), 0.0)
    base_prob = _base_probability(config)
    base_min = max(60.0, _cfg_float(config, "proactive_interval_min_s", 3600.0))
    base_max = max(base_min, _cfg_float(config, "proactive_interval_max_s", 7200.0))
    scale = 1.15 - 0.5 * value
    probability = max(0.15, min(0.92, base_prob * (0.55 + 0.9 * value)))
    interval_min = max(1200.0, min(7200.0, base_min * scale))
    interval_max = max(1200.0, min(7200.0, base_max * scale))
    interval_min, interval_max = min(interval_min, interval_max), max(interval_min, interval_max)

    now = time.time() if now is None else float(now)
    hour = time.localtime(now).tm_hour
    override_quiet = bool(config.get("proactive_override_quiet"))
    quiet = _in_quiet(config.get("quiet_hours"), hour) and not override_quiet
    cap = _cfg_int(result, "daily_cap",
                   _cfg_int(config, "pacing_daily_cap", 12))
    sent_today = _cfg_int(result, "today_sent", 0)
    streak = _cfg_int(result, "unanswered_streak", 0)

    send = True
    reason = "drive"
    conservative = False
    if stopped or bool(config.get("stop")) or bool(config.get("stop_file")):
        send, reason = False, "stopped"
    elif quiet:
        send, reason = False, "quiet_hours"
    elif cap > 0 and sent_today >= cap:
        send, reason = False, "daily_cap"
    elif streak >= 3:
        # 连续无回应：cap 再高也不放开，drive 完全不生效
        conservative = True
        send, reason = True, "no_response"
        probability = min(0.2, base_prob)
        interval_min = max(3600.0, base_min)
        interval_max = max(interval_min, min(7200.0, max(base_max, 3600.0)))

    return {
        "applied": enabled(config),
        "send": bool(send), "reason": reason,
        "conservative": bool(conservative),
        "drive": round(value, 4),
        "familiarity": round(_clamp01(result.get("familiarity")), 4),
        "mood": round(_clamp01(result.get("mood"), DEFAULT_MOOD), 4),
        "trust": round(_clamp01(result.get("trust"), DEFAULT_TRUST), 4),
        "probability": round(float(probability), 4),
        "interval_min_s": int(round(interval_min)),
        "interval_max_s": int(round(interval_max)),
        "base": {"probability": round(base_prob, 4),
                 "interval_min_s": int(round(base_min)),
                 "interval_max_s": int(round(base_max))},
        "daily_cap": int(cap), "today_sent": int(sent_today),
        "unanswered_streak": int(streak),
    }


def drive_level(value: Any) -> str:
    number = _clamp01(value, 0.0)
    if number >= 0.7:
        return "高"
    if number <= 0.35:
        return "低"
    return "中"


def status(path: Path | str, now: float | None = None, *,
           config: dict | None = None) -> dict[str, Any]:
    """只读状态：给 `GET /api/initiative/status?contact=` 用。"""
    now = time.time() if now is None else float(now)
    config = dict(config or {})
    did = drive(path, now)
    cap = daily_cap(path, now, config=config)
    sent = today_sent(path, now)
    return {"ok": True, "ts": now,
            "familiarity": did["familiarity"],
            "days_active": did["days_active"],
            "total_user_msgs": did["total_user_msgs"],
            "recent_7d_msgs": did["recent_7d_msgs"],
            "mood": did["mood"], "trust": did["trust"],
            "drive": did["drive"], "drive_level": drive_level(did["drive"]),
            "user_msgs_today": cap["user_msgs_today"],
            "daily_cap": cap["daily_cap"], "today_sent": sent,
            "initiative_enabled": enabled(config)}


# --------------------------------------------------------------------------- #
# P33 行为联动：候选 kind 权重（worker/后续 proactive 选择复用）
# --------------------------------------------------------------------------- #
def allowed_kinds(value: Any) -> tuple[str, ...] | None:
    """drive ≤0.35 时只允许 light/progress_check；否则 None=不限制。"""
    if _clamp01(value) <= 0.35:
        return LOW_DRIVE_ALLOWED
    return None


def kind_allowed(kind: Any, value: Any) -> bool:
    allowed = allowed_kinds(value)
    if allowed is None:
        return True
    return str(kind or "") in allowed


def kind_weight(kind: Any, value: Any) -> float:
    """候选权重：drive≥0.7 分享类 ×1.5、light ×0.7；drive≤0.35 只留允许类。"""
    name = str(kind or "")
    number = _clamp01(value)
    if number <= 0.35 and name not in LOW_DRIVE_ALLOWED:
        return 0.0
    if number >= 0.7:
        if name in SHARE_KINDS:
            return 1.5
        if name == "light":
            return 0.7
    return 1.0


def apply_kind_weights(candidates: list[dict], value: Any) -> list[dict]:
    """给候选打上 P33 权重（不改原对象；后续选择逻辑可直接用 weight）。"""
    weighted: list[dict] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        item = dict(candidate)
        weight = kind_weight(item.get("kind"), value)
        item["weight"] = weight
        base = item.get("base")
        if isinstance(base, (int, float)):
            item["weighted_base"] = round(float(base) * weight, 4)
        weighted.append(item)
    return weighted
