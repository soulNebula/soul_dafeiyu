# -*- coding: utf-8 -*-
"""11 分享三级：观察 / 记忆 / 观点。

分享欲（state.self.sharing）和心情（state.self.mood）决定**最多能到哪一级**，
而不是直接决定「发不发」。本模块只产出候选：不发送、不调 LLM、不写库。

三级对齐文档 L1→L2→L3，学习路径必须从低到高：

- ``observation``：L1 转发/观察。只用最近聊天里的话题，轻、低风险；
- ``memory``：L2 关联。要翻记忆，用「你之前说过……」把当下和过去接上；
- ``opinion``：L3 观点/创作。在记忆之上加自己的判断，最贵，也最容易翻车。

分享欲和心情各自分档（分享欲 / 心情），取**更低**的那一档：

- ``< 0.30 / < 0.32``：不说，素材先存着；
- ``< 0.55 / < 0.50``：最多 L1 观察/转发；
- ``< 0.75 / < 0.70``：最多 L2 记忆关联；
- ``>= 0.75 / >= 0.70``：可以到 L3 观点。

另外按 11 文档第五节「硬窗口」兜底：对方处在危机里、或情绪明显偏负
（valence <= -0.3）时不分享轻松内容，先关心人。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import memory, state
from .config import DB_PATH

LEVELS = ("observation", "memory", "opinion")
LEVEL_INDEX = {name: index for index, name in enumerate(LEVELS)}
LEVEL_LABELS = {"observation": "观察/转发", "memory": "记忆关联", "opinion": "观点"}

# 分享欲分档：不想说 / L1 / L2 / L3
SHARING_THRESHOLDS = (0.30, 0.55, 0.75)
# 心情同一套分档，门槛略高一点：心情一般时别翻旧账、别下判断
MOOD_THRESHOLDS = (0.32, 0.50, 0.70)
# 对方情绪低于这个值，不分享轻松内容（11 文档 5.1）
EMOTION_FLOOR = -0.3

# 候选基准分：级别越高越贵，默认也越靠前；真正的门槛由 choose_level 把关
BASE_SCORE = {"observation": 0.50, "memory": 0.62, "opinion": 0.72}
MAX_CANDIDATES = 6
MIN_TEXT = 4
# 最近 6 小时内聊到的话题，时效性更好
FRESH_SECONDS = 6 * 3600

_REASON = {
    "observation": "只想随手分享看到的东西，还没到翻记忆的程度",
    "memory": "想聊到你，愿意把记忆翻出来",
    "opinion": "分享欲很足，愿意把自己的判断拿出来",
}

# L3 的观点模板：按记忆类型给一句自己的判断，避免空对空
_STANCE = {
    "preference": "喜欢什么这件事，往往比理由更能说明一个人",
    "plan": "计划一旦说出口，其实就已经开始算数了",
    "event": "过去的事还愿意提，说明它确实留下了点什么",
    "fact": "能被记住的事实，多半都跟人有关系",
    "value": "真正在意的东西，通常都不太说得出口",
    "insight": "想明白和还没想明白之间，差的多半是时间",
}
_DEFAULT_STANCE = "同一件事换个位置看，可能是另一个样子"


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


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def _sensitive(text: Any) -> bool:
    """复用记忆模块的敏感信息规则：手机号/身份证/银行卡/密码/住址。"""
    value = str(text or "")
    return any(pattern.search(value) for pattern in memory.SENSITIVE_PATTERNS)


def _clip(text: Any, limit: int = 28) -> str:
    value = _clean(text)
    if len(value) <= limit:
        return value
    return value[:limit].rstrip("，,。；;、 ") + "…"


def _question(text: str) -> bool:
    return _clean(text).endswith(("?", "？", "吗", "呢"))


def _level_at(value: float, thresholds: tuple[float, float, float]) -> str | None:
    """把一个 0-1 的值折成级别；None 表示这一档还不想说。"""
    if value < thresholds[0]:
        return None
    if value < thresholds[1]:
        return LEVELS[0]
    if value < thresholds[2]:
        return LEVELS[1]
    return LEVELS[2]


def choose_level(path: Path | str = DB_PATH, *, mood: Any = None,
                 sharing: Any = None) -> dict[str, Any]:
    """此刻最多能分享到哪一级；level=None 表示这次不说。

    mood/sharing 显式传入时覆盖 DB 里的 state.self，方便调用方复用同一套门槛。
    只读 state，不写库、不发送。
    """
    st = state.load(path)
    own = st.get("self") if isinstance(st.get("self"), dict) else {}
    mood_value = _clamp(own.get("mood", 0.55) if mood is None else mood)
    sharing_value = _clamp(own.get("sharing", 0.45) if sharing is None else sharing)

    by_sharing = _level_at(sharing_value, SHARING_THRESHOLDS)
    by_mood = _level_at(mood_value, MOOD_THRESHOLDS)

    if by_sharing is None and by_mood is None:
        level, limit_by = None, "sharing"
        reason = "分享欲和心情都太低，现在不想说，素材先存着"
    elif by_sharing is None:
        level, limit_by = None, "sharing"
        reason = "分享欲太低，现在不想说，素材先存着"
    elif by_mood is None:
        level, limit_by = None, "mood"
        reason = "心情太低，现在不想说，素材先存着"
    elif LEVEL_INDEX[by_mood] < LEVEL_INDEX[by_sharing]:
        level, limit_by = by_mood, "mood"
        reason = f"心情一般，最多「{LEVEL_LABELS[by_mood]}」：{_REASON[by_mood]}"
    else:
        level, limit_by = by_sharing, "sharing"
        reason = _REASON[by_sharing]

    boundary = st.get("boundary") if isinstance(st.get("boundary"), dict) else {}
    emotion = st.get("emotion") if isinstance(st.get("emotion"), dict) else {}
    valence = _num(emotion.get("valence"), 0.0)
    if bool(boundary.get("crisis")) or str(emotion.get("label") or "") == "crisis":
        level, limit_by = None, "crisis"
        reason = "对方状态不太好，这时候不分享，先陪着"
    elif valence <= EMOTION_FLOOR and level is not None:
        level, limit_by = None, "care"
        reason = "对方情绪偏低，这时候不分享轻松的东西，先关心"

    return {
        "ok": True,
        "level": level,
        "index": -1 if level is None else LEVEL_INDEX[level],
        "max_level": level,
        "allowed": level is not None,
        "limit_by": limit_by,
        "mood": round(mood_value, 3),
        "sharing": round(sharing_value, 3),
        "by_sharing": by_sharing,
        "by_mood": by_mood,
        "reason": reason,
        "thresholds": {"sharing": list(SHARING_THRESHOLDS),
                       "mood": list(MOOD_THRESHOLDS)},
        "note": "只算级别，不发送任何东西",
    }


def _recent_user_messages(path: Path | str, limit: int = 12) -> list[dict[str, Any]]:
    """最近的可分享话题：用户说过的话，去掉寒暄、问句和敏感内容。"""
    out: list[dict[str, Any]] = []
    try:
        rows = memory.recent_messages(path, limit=limit)
    except Exception:  # noqa: BLE001
        return out
    seen: set[str] = set()
    for row in reversed(rows):  # 新 → 旧
        if str(row.get("role") or "") != "user" or int(row.get("proactive") or 0):
            continue
        text = _clean(row.get("text"))
        if len(text) < MIN_TEXT or text in seen:
            continue
        if _question(text) or _sensitive(text):
            continue
        seen.add(text)
        out.append({"id": str(row.get("id") or ""), "text": text,
                    "ts": _num(row.get("ts"), 0.0)})
    return out


def _usable_memory(row: dict) -> bool:
    if str(row.get("status") or "active") != "active":
        return False
    if str(row.get("scope") or "user") == "self":
        return False
    content = _clean(row.get("content"))
    return bool(content) and not _sensitive(content)


def _memory_material(path: Path | str, query: str = "",
                     limit: int = 6) -> list[dict[str, Any]]:
    """记忆素材：优先取跟最近话题对得上的，其次取最近的；带 associated 标记。"""
    rows: list[dict] = []
    associated = False
    if query:
        try:
            rows = memory.retrieve(path, query=query, k=limit)
        except Exception:  # noqa: BLE001
            rows = []
        associated = bool(rows)
    if not rows:
        try:
            rows = memory.list_user_memories(path, limit=limit * 3)
        except Exception:  # noqa: BLE001
            rows = []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not _usable_memory(row):
            continue
        content = _clean(row.get("content"))
        if content in seen:
            continue
        seen.add(content)
        out.append({"id": str(row.get("id") or ""), "content": content,
                    "kind": str(row.get("kind") or "fact"),
                    "confidence": _num(row.get("confidence"), 0.6),
                    "associated": associated})
        if len(out) >= limit:
            break
    return out


def _score(level: str, choice: dict[str, Any], *, bonus: float = 0.0) -> float:
    base = BASE_SCORE[level]
    value = (base
             + 0.10 * (float(choice.get("sharing") or 0.0) - 0.5)
             + 0.08 * (float(choice.get("mood") or 0.0) - 0.5)
             + bonus)
    return round(max(0.0, min(0.95, value)), 3)


def _candidate(level: str, kind: str, text: str, reason: str, source: str,
               source_id: str, score: float) -> dict[str, Any]:
    return {"level": level, "kind": kind, "text": _clean(text), "reason": reason,
            "source": source, "source_id": source_id, "score": score}


def _observation_candidates(messages: list[dict[str, Any]], choice: dict[str, Any],
                            now: float | None = None) -> list[dict[str, Any]]:
    """L1：只用最近聊天里的话题，不翻记忆。"""
    now = time.time() if now is None else float(now)
    out = []
    for item in messages[:3]:
        topic = _clip(item["text"], 24)
        fresh = bool(item.get("ts")) and (now - float(item["ts"])) <= FRESH_SECONDS
        out.append(_candidate(
            "observation", "forward",
            f"刚看到点跟「{topic}」有关的，想发给你看看",
            "最近聊到的这个话题还在，可以顺手把看到的东西带出来",
            "message", str(item.get("id") or ""),
            _score("observation", choice, bonus=0.04 if fresh else 0.0),
        ))
    return out


def _memory_candidates(memories: list[dict[str, Any]],
                       choice: dict[str, Any]) -> list[dict[str, Any]]:
    """L2：把最近话题和一条旧记忆接上；没有关联时就单纯回访一条记忆。"""
    out = []
    for item in memories[:3]:
        content = _clip(item["content"], 28)
        if item.get("associated"):
            text = f"刚聊到点相关的，你之前说过{content}"
            reason = "最近聊到的内容和这条记忆对得上，接得比较自然"
        else:
            text = f"突然想起你之前说过{content}，不知道现在还作不作数"
            reason = "记忆里有可以回访的事"
        bonus = max(-0.03, min(0.03, 0.2 * (float(item.get("confidence") or 0.6) - 0.6)))
        out.append(_candidate(
            "memory", f"memory_{item.get('kind') or 'fact'}", text, reason,
            "memory", str(item.get("id") or ""),
            _score("memory", choice, bonus=bonus),
        ))
    return out


def _opinion_candidates(memories: list[dict[str, Any]],
                        choice: dict[str, Any]) -> list[dict[str, Any]]:
    """L3：在记忆之上加自己的判断；没有记忆就不硬造，宁可不发。"""
    out = []
    for item in memories[:2]:
        content = _clip(item["content"], 26)
        stance = _STANCE.get(str(item.get("kind") or ""), _DEFAULT_STANCE)
        out.append(_candidate(
            "opinion", f"opinion_{item.get('kind') or 'insight'}",
            f"你之前说「{content}」，我越想越觉得：{stance}",
            "分享欲够高，愿意把自己的判断拿出来；这一级最贵，也最容易尴尬",
            "memory", str(item.get("id") or ""),
            _score("opinion", choice),
        ))
    return out


def build_share(path: Path | str = DB_PATH, *,
                level: str | None = None) -> dict[str, Any]:
    """从最近消息/记忆拼分享候选；只产出候选，不发送、不调 LLM、不写库。

    level=None 时由 choose_level 自动定级，并生成**不高于**该级的候选；
    level 指定时只生成这一级，且不会越过分享欲/心情允许的上限（capped=True）。
    返回里的 level 是这次允许的最高级别，实际候选各自带 level。
    """
    requested = None if level is None else str(level).strip().lower()
    if requested is not None and requested not in LEVELS:
        return {"ok": False, "available": False,
                "error": f"level 只允许 {list(LEVELS)} 或 None",
                "candidates": [], "count": 0, "best": None}

    choice = choose_level(path)
    chosen = choice["level"]
    capped = False
    if requested is None:
        effective = chosen
    elif chosen is None:
        effective = None
    else:
        requested_index = LEVEL_INDEX[requested]
        chosen_index = LEVEL_INDEX[chosen]
        capped = requested_index > chosen_index
        effective = LEVELS[min(requested_index, chosen_index)]

    note = "只生成候选：不发送、不调 LLM、不写库"
    if effective is None:
        return {"ok": True, "available": False, "level": None, "max_level": None,
                "requested_level": requested, "capped": False, "levels": [],
                "count": 0, "candidates": [], "best": None,
                "reason": choice["reason"], "gate": choice["limit_by"],
                "mood": choice["mood"], "sharing": choice["sharing"],
                "material": {"messages": 0, "memories": 0}, "note": note}

    messages = _recent_user_messages(path)
    query = " ".join(item["text"] for item in messages[:3])
    memories = _memory_material(path, query)
    wanted = [effective] if requested is not None else list(
        LEVELS[:LEVEL_INDEX[effective] + 1])

    candidates: list[dict[str, Any]] = []
    if "observation" in wanted:
        candidates += _observation_candidates(messages, choice)
    if "memory" in wanted:
        candidates += _memory_candidates(memories, choice)
    if "opinion" in wanted:
        candidates += _opinion_candidates(memories, choice)

    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in sorted(candidates, key=lambda c: (-c["score"], -LEVEL_INDEX[c["level"]],
                                                  c["text"])):
        key = (item["level"], item["text"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique = unique[:MAX_CANDIDATES]

    if unique:
        available = True
        reason = (f"最多到「{LEVEL_LABELS[effective]}」，共 {len(unique)} 个候选，"
                  "发不发、发哪个由调用方定")
    else:
        available = False
        reason = "没有可分享的素材：最近消息和记忆里都没有能用的，先存着"
    if capped:
        reason += f"；本来想要 {LEVEL_LABELS[requested]}，但分享欲/心情只到这一级"

    return {
        "ok": True,
        "available": available,
        "level": effective,
        "max_level": effective,
        "requested_level": requested,
        "capped": capped,
        "levels": wanted,
        "count": len(unique),
        "candidates": unique,
        "best": unique[0] if unique else None,
        "reason": reason,
        "gate": choice["limit_by"],
        "mood": choice["mood"],
        "sharing": choice["sharing"],
        "material": {"messages": len(messages), "memories": len(memories)},
        "note": note,
    }
