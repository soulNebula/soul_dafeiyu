# -*- coding: utf-8 -*-
"""09/10 情绪与记仇：心情/精力/分享欲/电量 + 未解决事件的克制表达。

这里是纯规则状态机，不调用模型、不联网：
- 09 的 Mood 会随时间恢复（半衰期），危机场景优先照顾人；
- 10 的 Trust 由 trust.py 账本负责，本模块只读，只在反馈/道歉时调用它；
- 未解决负面事件只翻译成行为指令（降投入、不翻旧账、不惩罚），绝不鼓励报复。

状态沿用 state.py 的 self 段，额外元数据放在 self["affect"]，因此和其他模块
（state.feedback / proactive.tick / chat）共用同一份 mood/sharing/battery。
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

from . import state, trust
from .config import DB_PATH

BASELINE = {"mood": 0.55, "energy": 0.6, "sharing": 0.45, "battery": 0.7}
# 空闲时会回落的平衡点：电量会慢慢回充，其余回到日常基线。
TARGET = {"mood": 0.55, "energy": 0.6, "sharing": 0.45, "battery": 0.85}
BOUNDS = {
    "mood": (0.05, 0.95),
    "energy": (0.05, 0.98),
    "sharing": (0.0, 0.95),
    "battery": (0.05, 1.0),
}
# 09：不悦在半小时内回落一半；精力和电量恢复更慢。
HALF_LIFE = {
    "mood": 30 * 60,
    "energy": 2 * 3600,
    "sharing": 4 * 3600,
    "battery": 2 * 3600,
}
CRISIS_HOLD = 6 * 3600
MAX_EVENTS = 12

INSULT = (
    "傻逼", "智障", "废物", "垃圾", "滚开", "滚蛋", "闭嘴", "讨厌你",
    "破程序", "去死", "妈的", "操你", "恶心", "蠢货", "你懂个屁",
)
NEG = tuple(state.NEG) + ("难受", "压力", "郁闷", "心累", "不开心", "失眠", "撑不住")
POS = tuple(state.POS) + ("哈哈", "爱你", "好棒", "厉害", "可爱", "真好")
APOLOGY = ("对不起", "抱歉", "不好意思", "我错了", "是我不好", "别生气", "原谅我")
SETTLE = ("算了", "过去吧", "翻篇", "不提了")

SEVERE_MARKS = ("冒犯", "侮辱", "骂", "威胁", "隐私", "利用", "背叛", "破程序")
UNFORGIVABLE_MARKS = ("背叛", "出卖", "利用脆弱", "利用我", "侵犯隐私", "骗我", "骗钱", "诈骗")
FEEDBACK_REASON = {
    "good": "这次回应用户满意",
    "ok": "这次回应还行",
    "bad": "这次回应用户不满意",
}
LEVEL_ORDER = ("g1", "g2", "g3", "g4")


def _num(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return out if math.isfinite(out) else float(default)


def _clamp(key: str, value: float) -> float:
    low, high = BOUNDS[key]
    return max(low, min(high, float(value)))


def _bump(own: dict, key: str, delta: float) -> None:
    own[key] = round(_clamp(key, _num(own.get(key), BASELINE[key]) + float(delta)), 3)


def _meta(own: dict) -> dict[str, Any]:
    raw = own.get("affect")
    raw = raw if isinstance(raw, dict) else {}
    now = time.time()
    events = []
    for item in raw.get("events") or []:
        if isinstance(item, dict) and item.get("kind"):
            events.append({
                "kind": str(item["kind"])[:24],
                "at": _num(item.get("at"), now),
                "mood_delta": round(_num(item.get("mood_delta"), 0.0), 4),
            })
    return {
        "updated_at": _num(raw.get("updated_at"), 0.0),
        "crisis": bool(raw.get("crisis")),
        "crisis_at": _num(raw.get("crisis_at"), 0.0),
        "last_feedback": str(raw.get("last_feedback") or "")[:16],
        "events": events[-MAX_EVENTS:],
    }


def _load(path: Path | str) -> tuple[dict, dict, dict]:
    data = state.load(path)
    own = data.get("self")
    if not isinstance(own, dict):
        own = {}
        data["self"] = own
    for key, default in BASELINE.items():
        own[key] = round(_clamp(key, _num(own.get(key), default)), 3)
    meta = _meta(own)
    own["affect"] = meta
    return data, own, meta


def _save(path: Path | str, data: dict, own: dict, meta: dict) -> None:
    own["affect"] = meta
    state.save(data, path)


def _record(meta: dict, kind: str, mood_delta: float, now: float) -> None:
    meta["events"].append({"kind": str(kind)[:24], "at": round(now, 3),
                           "mood_delta": round(float(mood_delta), 4)})
    if len(meta["events"]) > MAX_EVENTS:
        del meta["events"][:-MAX_EVENTS]


def _crisis_active(meta: dict, now: float) -> bool:
    if not meta.get("crisis"):
        return False
    since = _num(meta.get("crisis_at"), _num(meta.get("updated_at"), now))
    return (now - since) <= CRISIS_HOLD


def _normalize_feedback(feedback: Any) -> str:
    value = str(feedback or "").strip().lower()
    if not value:
        return ""
    if value in {"good", "great", "love", "like", "nice", "up", "positive", "赞", "好"}:
        return "good"
    if value in {"ok", "fine", "neutral", "so-so", "一般", "还行"}:
        return "ok"
    if value in {"bad", "terrible", "awful", "wrong", "angry", "down", "negative",
                 "meh", "差", "烂"}:
        return "bad"
    return ""


def _reason_label(reason: Any) -> str:
    reason = str(reason or "")
    if any(word in reason for word in SEVERE_MARKS):
        return "被冒犯过"
    if any(word in reason for word in ("反馈", "回应", "不满意", "没接住")):
        return "有一次回应没被接住"
    if any(word in reason for word in ("冷淡", "敷衍", "已读", "冷落")):
        return "有一次被冷淡对待"
    return "有过一次不愉快"


def _grudge_items(events: list[dict], now: float) -> list[dict[str, Any]]:
    """把 trust 的未解决负面事件翻译成克制摘要；G1 会自然变淡，G2/G3 不靠时间消失。"""
    events = list(events or [])
    repeated = len(events) >= 2
    items = []
    for event in events:
        reason = event.get("reason")
        if any(word in str(reason or "") for word in UNFORGIVABLE_MARKS):
            level, half_life = "g4", None
        elif any(word in str(reason or "") for word in SEVERE_MARKS):
            level, half_life = "g3", None
        elif repeated:
            level, half_life = "g2", None
        else:
            level, half_life = "g1", 14 * 86400
        age = max(0.0, now - _num(event.get("created_at"), now))
        salience = 1.0 if half_life is None else 0.5 ** (age / half_life)
        items.append({
            "id": event.get("id"),
            "level": level,
            "label": _reason_label(reason),
            "age_seconds": round(age, 1),
            "salience": round(salience, 3),
            "active": salience >= 0.25,
        })
    return items


def _age_text(seconds: float) -> str:
    if seconds < 90:
        return "刚才"
    if seconds < 5400:
        return f"{max(1, int(seconds // 60))} 分钟前"
    if seconds < 36 * 3600:
        return f"{max(1, int(seconds // 3600))} 小时前"
    if seconds < 45 * 86400:
        return f"{max(1, int(seconds // 86400))} 天前"
    return f"{max(1, int(seconds // (30 * 86400)))} 个月前"


def _mood_label(value: float) -> str:
    if value >= 0.75:
        return "不错"
    if value >= 0.6:
        return "还行"
    if value >= 0.42:
        return "一般"
    if value >= 0.25:
        return "有点低"
    return "很低"


def _energy_label(value: float) -> str:
    if value >= 0.7:
        return "有精神"
    if value >= 0.45:
        return "还行"
    return "有点累"


def _sharing_label(value: float) -> str:
    if value >= 0.7:
        return "很想说"
    if value >= 0.5:
        return "有点想说"
    return "先存着"


def _battery_label(value: float) -> str:
    if value >= 0.6:
        return "够用"
    if value >= 0.3:
        return "不多"
    return "快没电"


def _build(path: Path | str, data: dict, now: float) -> dict[str, Any]:
    own = data["self"]
    meta = own.get("affect")
    meta = meta if isinstance(meta, dict) else _meta(own)
    trust_value = trust.get(path)
    unresolved = trust.unresolved(path)
    items = _grudge_items(unresolved, now)
    active = [item for item in items if item["active"]]
    crisis = _crisis_active(meta, now)

    if crisis:
        grudge_summary = "用户可能处在危机里：旧事不计，先回应人"
        grudge_level = "crisis"
    elif active:
        grudge_summary = (f"有 {len(active)} 件没过去的事：降投入，"
                          "但不还击、不冷暴力、不翻旧账")
        grudge_level = max((item["level"] for item in active), key=LEVEL_ORDER.index)
    elif unresolved:
        grudge_summary = "之前的不愉快已经淡了，正常聊"
        grudge_level = "faded"
    else:
        grudge_summary = "没有未解决的不愉快"
        grudge_level = "none"

    mood = round(own["mood"], 3)
    energy = round(own["energy"], 3)
    sharing = round(own["sharing"], 3)
    battery = round(own["battery"], 3)
    summary = (f"心情{_mood_label(mood)}，精力{_energy_label(energy)}，"
               f"分享欲{_sharing_label(sharing)}，电量{_battery_label(battery)}；"
               f"信任 {trust_value:.2f}（{trust.stage(trust_value)}）")
    if crisis:
        summary += "；现在先照顾人"
    elif active:
        summary += f"；另有 {len(active)} 件没过去的事"

    return {
        "mood": mood,
        "energy": energy,
        "sharing": sharing,
        "battery": battery,
        "trust": round(trust_value, 4),
        "stage": trust.stage(trust_value),
        "mood_label": _mood_label(mood),
        "energy_label": _energy_label(energy),
        "sharing_label": _sharing_label(sharing),
        "battery_label": _battery_label(battery),
        "crisis": crisis,
        "care": crisis,
        "grudge_count": len(unresolved),
        "grudge_active": len(active),
        "grudge_level": grudge_level,
        "grudge_summary": grudge_summary,
        "summary": summary,
        "updated_at": round(_num(meta.get("updated_at"), now), 3),
        "self": {"mood": mood, "energy": energy, "sharing": sharing,
                 "battery": battery},
    }


def snapshot(path: Path | str = DB_PATH) -> dict[str, Any]:
    """只读快照：它现在的内部状态 + 信任 + 未解决事件摘要。"""
    now = time.time()
    data, _own, _meta_now = _load(path)
    return _build(path, data, now)


def update_from_message(path: Path | str = DB_PATH, text: str = "",
                        *, feedback: str = "") -> dict[str, Any]:
    """一条用户消息（可带这次回复的反馈）如何改变它自己的状态。"""
    now = time.time()
    data, own, meta = _load(path)
    text = str(text or "")[:2000]
    applied: list[str] = []
    signals = {
        "crisis": False, "insult": False, "apology": False,
        "negative": False, "positive": False, "trust_suppressed": False,
    }
    fb = _normalize_feedback(feedback)
    crisis_before = _crisis_active(meta, now)
    trust_before = trust.get(path)
    mood_before = own["mood"]
    trust_written = False

    crisis = any(word in text for word in state.CRISIS)
    insult = (not crisis) and any(word in text for word in INSULT)
    apology = (not crisis) and (not insult) and any(word in text for word in APOLOGY + SETTLE)
    negative = any(word in text for word in NEG)
    positive = any(word in text for word in POS)

    if crisis:
        signals["crisis"] = True
        _bump(own, "mood", -0.05)
        _bump(own, "energy", -0.05)
        _bump(own, "sharing", -0.05)
        _bump(own, "battery", -0.02)
        meta["crisis"] = True
        meta["crisis_at"] = now
        applied.append("crisis")
    elif insult:
        signals["insult"] = True
        _bump(own, "mood", -0.12)
        _bump(own, "energy", -0.08)
        _bump(own, "sharing", -0.10)
        _bump(own, "battery", -0.03)
        applied.append("insult")
        # 先动 Mood 再动 Trust；这条作为一次冒犯证据写进账本。
        # 同一条消息里反馈也是 bad 时不重复扣一次，但理由要保留“冒犯”的严重度。
        reason = "用户说了冒犯的话"
        if fb == "bad":
            reason += "，这次回应也不满意"
        trust.update(path, "bad", None, reason)
        trust_written = True
    else:
        if negative:
            signals["negative"] = True
            _bump(own, "mood", -0.04)
            _bump(own, "energy", -0.03)
            _bump(own, "sharing", -0.02)
            _bump(own, "battery", -0.015)
            applied.append("negative")
        if positive:
            signals["positive"] = True
            _bump(own, "mood", 0.03)
            _bump(own, "energy", 0.02)
            _bump(own, "sharing", 0.03)
            _bump(own, "battery", -0.005)
            applied.append("positive")
        if apology:
            signals["apology"] = True
            _bump(own, "mood", 0.05)
            _bump(own, "energy", 0.01)
            _bump(own, "sharing", 0.03)
            applied.append("apology")
            # 和解交给 trust：只解除一条旧账，绝不把信任一次回满。
            if trust.unresolved(path):
                repaired = trust.repair(path, "用户表示了歉意")
                if repaired.get("repaired"):
                    applied.append("repair")

    if fb:
        applied.append(f"feedback:{fb}")
        if crisis or crisis_before:
            # 危机优先：不因为这次回应再扣信任，先照顾人。
            signals["trust_suppressed"] = True
        elif not (trust_written and fb == "bad"):
            trust.update(path, fb, None, FEEDBACK_REASON[fb])
        if fb == "good":
            _bump(own, "mood", 0.06)
            _bump(own, "energy", 0.03)
            _bump(own, "sharing", 0.04)
            _bump(own, "battery", -0.01)
        elif fb == "ok":
            _bump(own, "mood", 0.02)
            _bump(own, "sharing", 0.01)
        elif fb == "bad":
            _bump(own, "mood", -0.08)
            _bump(own, "energy", -0.03)
            _bump(own, "sharing", -0.04)
            _bump(own, "battery", -0.02)
    elif str(feedback or "").strip():
        applied.append("feedback_ignored")

    if own["mood"] != mood_before:
        kind = ("crisis" if crisis else "insult" if insult else
                f"feedback:{fb}" if fb else "apology" if apology else
                "negative" if negative else "positive" if positive else "message")
        _record(meta, kind, own["mood"] - mood_before, now)

    meta["updated_at"] = now
    if fb:
        meta["last_feedback"] = fb
    _save(path, data, own, meta)

    snap = _build(path, data, now)
    result = dict(snap)
    result.update({
        "ok": True,
        "applied": applied,
        "signals": signals,
        "feedback": fb,
        "trust_before": round(trust_before, 4),
        "trust_after": snap["trust"],
    })
    return result


def decay(path: Path | str = DB_PATH, elapsed_seconds: float | None = None) -> dict[str, Any]:
    """时间流逝：心情/精力/分享欲回到基线，电量回充；trust 不随时间恢复。"""
    now = time.time()
    data, own, meta = _load(path)
    if elapsed_seconds is None:
        stamp = _num(meta.get("updated_at"), 0.0)
        elapsed = max(0.0, now - stamp) if stamp > 0 else 0.0
    else:
        elapsed = max(0.0, _num(elapsed_seconds, 0.0))

    changed: dict[str, dict] = {}
    if elapsed > 0:
        for key, half_life in HALF_LIFE.items():
            before = own[key]
            after = TARGET[key] + (before - TARGET[key]) * (0.5 ** (elapsed / half_life))
            after = round(_clamp(key, after), 3)
            if abs(after - before) > 1e-9:
                changed[key] = {"before": round(before, 3), "after": after}
            own[key] = after
        meta["updated_at"] = now
        if meta.get("crisis") and not _crisis_active(meta, now):
            meta["crisis"] = False
        _save(path, data, own, meta)

    snap = _build(path, data, now)
    result = dict(snap)
    result.update({
        "elapsed_seconds": round(elapsed, 3),
        "changed": changed,
        "trust_decayed": False,
    })
    return result


def grudge_context(path: Path | str = DB_PATH) -> str:
    """注入用的记仇上下文：只给行为指令，不给报复理由；没有旧账就返回空串。"""
    now = time.time()
    data, _own, meta = _load(path)
    trust_value = trust.get(path)
    unresolved = trust.unresolved(path)

    if _crisis_active(meta, now):
        return ("<relation>\n"
                "先照顾人：用户可能处在危机里，之前的不愉快一律不计，不翻旧账。\n"
                "表现：照常回应，短一点、明确一点，先接住人再谈别的。\n"
                "</relation>")

    if not unresolved:
        return ""

    items = _grudge_items(unresolved, now)
    active = [item for item in items if item["active"]]
    head = f"信任 {trust_value:.2f}（{trust.stage(trust_value)}）"
    if not active:
        return (f"<relation>\n{head}。之前那点不愉快已经淡了，不用再端着。\n"
                "表现：正常聊，别主动提旧事。\n"
                "</relation>")

    labels: list[str] = []
    for item in active[:3]:
        piece = f"{item['label']}（{_age_text(item['age_seconds'])}）"
        if piece not in labels:
            labels.append(piece)
    count_text = "一件事" if len(active) == 1 else f"{len(active)} 件事"
    worst = max((item["level"] for item in active), key=LEVEL_ORDER.index)
    if worst == "g4":
        behavior = "表现：保持距离；不主动翻旧账、不报复、不假装回到从前；只做必要的正常回应。"
    elif worst == "g3":
        behavior = "表现：正常聊；不主动深聊、不先交底，同类话题直接说边界。"
    elif worst == "g2":
        behavior = "表现：正常聊，但别主动深聊、别先交底。"
    else:
        behavior = "表现：正常聊，别主动提这件事。"

    return (f"<relation>\n{head}，心里还记着{count_text}：{'、'.join(labels)}。\n"
            f"{behavior}\n"
            "记得但不惩罚对方：不还击、不冷暴力、不主动翻旧账；"
            "只有对方主动问起、或又在做同样的事，才可以提一句。\n"
            "</relation>")
