# -*- coding: utf-8 -*-
"""04 学习闭环与打分：60 情境桶 + 七维加权 sigmoid 评分 + 同桶 baseline。

设计边界（对齐 `04-学习闭环与打分.md`）：
- 桶只有三维：关系阶段 × 情绪极性 × 话题类型 = 4 × 3 × 5 = 60 桶，不做 8 维；
- 绝对分没意义：score 只和**同桶**历史均分（baseline）比；无历史时 score_delta=None；
- raw = Σ(w_i · s_i)，每个 s_i 先裁剪到 [-1, 1]，score = sigmoid(raw) ∈ (0, 1)；
- 复发权重为负：同一困惑反复出现会拉低分数；
- 长度信号饱和封顶，防「越啰嗦越好」；
- 本模块不联网；只有 `record_score` / `baseline_of` 读写 SQLite（score_records）。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

__all__ = [
    "STAGES",
    "MOODS",
    "TOPICS",
    "WEIGHTS",
    "SIGNAL_ORDER",
    "LENGTH_CAP",
    "stage_of",
    "mood_of",
    "topic_of",
    "buckets",
    "bucket_of",
    "parse_bucket",
    "length_signal",
    "score_turn",
    "record_score",
    "baseline_of",
]

# 三维桶：4 × 3 × 5 = 60
STAGES = ("陌生", "熟", "亲近", "疏远")
MOODS = ("负", "中性", "正")
TOPICS = ("工作", "感情", "生活", "兴趣", "求助")

# 07 个信号权重（显式 / 行为 / 长度 / 情绪 / 留存 / 复发 / 裁判）
WEIGHTS: dict[str, float] = {
    "explicit": 0.30,
    "behavior": 0.25,
    "length": 0.10,
    "emotion": 0.15,
    "retention": 0.15,
    "recurrence": -0.10,
    "judge": 0.20,
}
SIGNAL_ORDER = tuple(WEIGHTS)

# 长度比上限：超过 4 倍不再加分，避免「越啰嗦越好」
LENGTH_CAP = 4.0
# 情绪极性判定的死区，避免把轻微波动当正/负
MOOD_DEADBAND = 0.15

_STAGE_ALIASES = {
    "陌生": "陌生",
    "初识": "陌生",
    "不熟": "陌生",
    "stranger": "陌生",
    "strange": "陌生",
    "new": "陌生",
    "熟": "熟",
    "熟悉": "熟",
    "familiar": "熟",
    "acquainted": "熟",
    "regular": "熟",
    "亲近": "亲近",
    "信任": "亲近",
    "亲密": "亲近",
    "close": "亲近",
    "intimate": "亲近",
    "trust": "亲近",
    "trusted": "亲近",
    "疏远": "疏远",
    "冷": "疏远",
    "冷淡": "疏远",
    "distant": "疏远",
    "estranged": "疏远",
    "cold": "疏远",
}

_MOOD_ALIASES = {
    "负": "负",
    "负面": "负",
    "负向": "负",
    "消极": "负",
    "negative": "负",
    "neg": "负",
    "bad": "负",
    "中性": "中性",
    "中": "中性",
    "neutral": "中性",
    "flat": "中性",
    "正": "正",
    "正面": "正",
    "正向": "正",
    "积极": "正",
    "positive": "正",
    "pos": "正",
    "good": "正",
}

_TOPIC_ALIASES = {
    "工作": "工作",
    "加班": "工作",
    "职场": "工作",
    "work": "工作",
    "job": "工作",
    "career": "工作",
    "感情": "感情",
    "爱情": "感情",
    "恋爱": "感情",
    "love": "感情",
    "relationship": "感情",
    "dating": "感情",
    "生活": "生活",
    "日常": "生活",
    "life": "生活",
    "daily": "生活",
    "兴趣": "兴趣",
    "爱好": "兴趣",
    "interest": "兴趣",
    "hobby": "兴趣",
    "hobbies": "兴趣",
    "求助": "求助",
    "帮忙": "求助",
    "问题": "求助",
    "help": "求助",
    "support": "求助",
}

# 文本信号 → [-1, 1]；同一条信号既接受数字也接受这些词
_WORDS: dict[str, dict[str, float]] = {
    "explicit": {
        "good": 1.0,
        "great": 1.0,
        "like": 1.0,
        "up": 1.0,
        "+1": 1.0,
        "赞": 1.0,
        "点赞": 1.0,
        "谢谢": 1.0,
        "你说得对": 1.0,
        "ok": 0.0,
        "fine": 0.0,
        "neutral": 0.0,
        "中": 0.0,
        "bad": -1.0,
        "dislike": -1.0,
        "down": -1.0,
        "-1": -1.0,
        "踩": -1.0,
        "差评": -1.0,
        "不喜欢": -1.0,
    },
    "behavior": {
        "continue": 1.0,
        "deeper": 1.0,
        "deep": 1.0,
        "深聊": 0.9,
        "继续": 0.9,
        "ask": 0.6,
        "follow_up": 0.6,
        "追问": 0.6,
        "brief": -0.3,
        "short": -0.3,
        "一句带过": -0.3,
        "switch": -0.5,
        "topic_switch": -0.5,
        "换话题": -0.5,
        "gone": -1.0,
        "leave": -1.0,
        "left": -1.0,
        "消失": -1.0,
        "silent": -1.0,
    },
    "emotion": {
        "up": 1.0,
        "better": 1.0,
        "improve": 1.0,
        "好转": 1.0,
        "变好": 1.0,
        "flat": 0.0,
        "same": 0.0,
        "平": 0.0,
        "down": -1.0,
        "worse": -1.0,
        "drop": -1.0,
        "变差": -1.0,
        "恶化": -1.0,
    },
    "retention": {
        "returned": 1.0,
        "return": 1.0,
        "back": 1.0,
        "回来": 1.0,
        "no_return": -1.0,
        "没回来": -1.0,
    },
    "recurrence": {
        "repeat": 1.0,
        "recurred": 1.0,
        "again": 1.0,
        "复发": 1.0,
        "反复": 1.0,
        "none": 0.0,
        "no": 0.0,
        "没有": 0.0,
    },
    "judge": {
        "progress": 1.0,
        "推进": 1.0,
        "pass": 1.0,
        "ok": 0.0,
        "flat": 0.0,
        "stuck": -0.5,
        "stall": -0.5,
        "卡住": -0.5,
        "regress": -1.0,
        "倒退": -1.0,
        "fail": -0.8,
    },
}

# 同一信号允许的输入键名；第一个是该信号的规范名
_ALIASES: dict[str, tuple[str, ...]] = {
    "explicit": ("explicit", "explicit_signal", "feedback", "reaction", "rating"),
    "behavior": ("behavior", "behaviour", "behavior_signal", "engagement"),
    "length": ("length", "length_signal", "length_ratio", "len_ratio"),
    "emotion": ("emotion", "emotion_signal", "emotion_delta", "mood_delta"),
    "retention": ("retention", "retention_signal", "return_24h", "returned", "retained"),
    "recurrence": ("recurrence", "recurrence_signal", "recurred", "repeat", "repeat_count"),
    "judge": ("judge", "judge_signal", "judge_score", "rubric", "judge_result"),
}
# 这些键传的是「本轮字数 ÷ 上轮字数」，要先做饱和映射
_RATIO_KEYS = {"length_ratio", "len_ratio"}


def _id(prefix: str, *parts: Any) -> str:
    return prefix + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return low if value < low else high if value > high else value


def _clip01(value: float) -> float:
    return _clip(value, 0.0, 1.0)


def _sigmoid(raw: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-float(raw)))
    except OverflowError:  # 极端 raw 不抛异常，直接给边界
        return 0.0 if raw < 0 else 1.0


def _pick(value: Any, choices: tuple[str, ...], aliases: dict[str, str],
          default: str, *, by_index: bool = False) -> str:
    """把任意输入归一到一个规范值；不认识就回默认桶，不抛异常。"""
    if isinstance(value, bool):
        value = str(value)
    if by_index and isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number) and number == int(number):
            index = int(number)
            if 0 <= index < len(choices):
                return choices[index]
        return default
    text = str(value).strip() if value is not None else ""
    if not text:
        return default
    if text in choices:
        return text
    if by_index and text.isdigit() and int(text) < len(choices):
        return choices[int(text)]
    key = text.lower()
    return aliases.get(key, default)


def stage_of(value: Any = "陌生") -> str:
    """关系阶段归一：陌生 / 熟 / 亲近 / 疏远；兼容 trust.stage 的「初识/熟悉/信任」。"""
    return _pick(value, STAGES, _STAGE_ALIASES, "陌生", by_index=True)


def mood_of(value: Any = "neutral") -> str:
    """情绪极性归一：负 / 中性 / 正；也接受 valance 数值（±0.15 死区）。"""
    if isinstance(value, bool):
        return "正" if value else "负"
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return "中性"
        if number > MOOD_DEADBAND:
            return "正"
        if number < -MOOD_DEADBAND:
            return "负"
        return "中性"
    text = str(value).strip() if value is not None else ""
    if text:
        try:
            number = float(text)
        except ValueError:
            number = None
        if number is not None and math.isfinite(number):
            return mood_of(number)
    return _pick(text, MOODS, _MOOD_ALIASES, "中性")


def topic_of(value: Any = "生活") -> str:
    return _pick(value, TOPICS, _TOPIC_ALIASES, "生活", by_index=True)


def buckets() -> list[str]:
    """全部 60 个规范桶名（固定顺序，便于测试/报表）。"""
    return ["|".join(parts) for parts in itertools.product(STAGES, MOODS, TOPICS)]


def bucket_of(*, stage: Any = "陌生", mood: Any = "neutral", topic: Any = "生活") -> str:
    """三维桶：`关系阶段|情绪极性|话题类型`，如 `陌生|中性|生活`。"""
    return "|".join((stage_of(stage), mood_of(mood), topic_of(topic)))


def parse_bucket(text: Any) -> tuple[str, str, str] | None:
    """把 `a|b|c` 拆回规范三元组；形状不对返回 None（分量不合法则回退默认值）。"""
    parts = [part.strip() for part in str(text or "").split("|")]
    if len(parts) != 3:
        return None
    return stage_of(parts[0]), mood_of(parts[1]), topic_of(parts[2])


def _ratio_signal(ratio: Any, cap: float = LENGTH_CAP) -> float:
    """长度比 r → [-1, 1]：`(r-1)/(r+1)`，并封顶到 cap 倍。"""
    try:
        number = float(ratio)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    if number <= 0.0:
        return -1.0
    number = min(number, float(cap))
    return _clip((number - 1.0) / (number + 1.0))


def length_signal(current: Any, previous: Any, cap: float = LENGTH_CAP) -> float:
    """本轮字数 ÷ 上轮字数 → [-1, 1]；上限 cap 倍，防「越啰嗦越好」。"""
    try:
        cur = max(0.0, float(current))
        prev = max(0.0, float(previous))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(cur) or not math.isfinite(prev):
        return 0.0
    if prev <= 0.0:
        return _ratio_signal(cap, cap) if cur > 0.0 else 0.0
    return _ratio_signal(cur / prev, cap)


def _first(signals: Mapping[str, Any], aliases: tuple[str, ...]) -> tuple[bool, str, Any]:
    for key in aliases:
        if key in signals and signals[key] is not None:
            return True, key, signals[key]
    return False, aliases[0], None


def _normalize_signal(name: str, key: str, value: Any) -> tuple[float | None, str]:
    """单信号 → [-1, 1]；无法解析返回 (None, note)，由调用方记入 missing。"""
    note = ""
    if isinstance(value, dict):
        note = str(value.get("note") or value.get("reason") or "")[:300]
        for field in ("value", "score", "signal"):
            if field in value:
                value = value[field]
                break
        else:
            return None, note
    if value is None:
        return None, note
    if isinstance(value, bool):
        if name == "recurrence":
            return (1.0 if value else 0.0), note
        if name == "retention":
            return (1.0 if value else -1.0), note
        return (1.0 if value else -1.0), note

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, note
        words = _WORDS.get(name, {})
        if text in words:
            return _clip(words[text]), note
        if text.lower() in words:
            return _clip(words[text.lower()]), note
        try:
            number = float(text)
        except ValueError:
            return None, note
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None, note

    if not math.isfinite(number):
        return None, note
    if name == "length" and key in _RATIO_KEYS:
        return _ratio_signal(number), note
    return _clip(number), note


def _bucket_parts(signals: Mapping[str, Any]) -> tuple[str, str, str]:
    text = signals.get("bucket")
    if isinstance(text, str) and text.strip():
        parts = parse_bucket(text)
        if parts:
            return parts
    return (
        stage_of(signals.get("stage", "陌生")),
        mood_of(signals.get("mood", "neutral")),
        topic_of(signals.get("topic", "生活")),
    )


def baseline_of(
    path: Path | str = DB_PATH,
    bucket: str = "",
    *,
    exclude_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """同桶历史均分；桶里没有任何记录时返回 None。"""
    name = str(bucket or "").strip()
    if not name:
        return None
    sql = ("SELECT count(*) AS n, avg(score) AS mean, min(score) AS lo, max(score) AS hi "
           "FROM score_records WHERE bucket=?")
    params: list[Any] = [name]
    if exclude_turn_id:
        sql += " AND (turn_id IS NULL OR turn_id<>?)"
        params.append(str(exclude_turn_id))
    con = db.connect(path)
    try:
        row = con.execute(sql, params).fetchone()
    finally:
        con.close()
    count = int(row["n"] or 0) if row is not None else 0
    if count <= 0:
        return None
    return {
        "bucket": name,
        "n": count,
        "mean": round(float(row["mean"]), 6),
        "min": round(float(row["lo"]), 6),
        "max": round(float(row["hi"]), 6),
    }


def score_turn(
    signals: dict | None = None,
    *,
    baseline: float | None = None,
    path: Path | str | None = None,
    exclude_turn_id: str | None = None,
) -> dict[str, Any]:
    """七维信号 → raw / sigmoid score / breakdown / bucket / score_delta。

    - 每个信号先裁剪到 [-1, 1]；缺失或无法解析按 0 处理并记入 breakdown["missing"]；
    - baseline 优先用显式传入值，其次从 `path` 的 score_records 读同桶均分；
    - 没有 baseline 时 score_delta=None（绝对分没有意义）。
    """
    data: Mapping[str, Any] = signals if isinstance(signals, Mapping) else {}
    parts = _bucket_parts(data)
    bucket = "|".join(parts)

    components: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    raw = 0.0
    for name in SIGNAL_ORDER:
        weight = WEIGHTS[name]
        found, key, value = _first(data, _ALIASES[name])
        normalized, note = _normalize_signal(name, key, value) if found else (None, "")
        if normalized is None:
            missing.append(name)
            normalized = 0.0
        contribution = weight * normalized
        raw += contribution
        item: dict[str, Any] = {
            "value": round(normalized, 6),
            "weight": weight,
            "contribution": round(contribution, 6),
        }
        if note:
            item["note"] = note
        components[name] = item

    score = _sigmoid(raw)
    base_value: float | None = None
    base_n = 0
    if baseline is not None:
        try:
            base_value = _clip01(float(baseline))
        except (TypeError, ValueError):
            base_value = None
    elif path is not None:
        info = baseline_of(path, bucket, exclude_turn_id=exclude_turn_id)
        if info:
            base_value = info["mean"]
            base_n = info["n"]
    delta = None if base_value is None else round(score - base_value, 6)

    breakdown = {
        "bucket": bucket,
        "stage": parts[0],
        "mood": parts[1],
        "topic": parts[2],
        "weights": dict(WEIGHTS),
        "components": components,
        "missing": missing,
        "raw": round(raw, 6),
        "score": round(score, 6),
    }
    return {
        "ok": True,
        "bucket": bucket,
        "stage": parts[0],
        "mood": parts[1],
        "topic": parts[2],
        "raw": round(raw, 6),
        "score": round(score, 6),
        "score_delta": delta,
        "baseline": None if base_value is None else round(base_value, 6),
        "baseline_n": int(base_n),
        "breakdown": breakdown,
    }


def record_score(
    path: Path | str,
    turn_id: str | None,
    signals: dict | None = None,
) -> dict[str, Any]:
    """打分并写入 `score_records`；同一 turn_id 重复写会覆盖，不污染同桶 baseline。"""
    turn = str(turn_id) if turn_id not in (None, "") else None
    result = score_turn(signals, path=path, exclude_turn_id=turn)
    now = time.time()
    record_id = _id("SR-", turn) if turn else _id("SR-", result["bucket"], time.time_ns())

    con = db.connect(path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO score_records"
            "(id,turn_id,bucket,raw,score,breakdown_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                record_id,
                turn,
                result["bucket"],
                result["raw"],
                result["score"],
                json.dumps(result["breakdown"], ensure_ascii=False),
                now,
            ),
        )
        con.commit()
    finally:
        con.close()

    out = dict(result)
    out["id"] = record_id
    out["turn_id"] = turn
    out["created_at"] = round(now, 6)
    return out
