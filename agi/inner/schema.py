# -*- coding: utf-8 -*-
"""W2-A 内状态数据结构：InnerState + 校验。

字段来源：
- 数值：``state.load()`` 的 ``self``（mood/energy/sharing/battery）+ 信任账本 trust；
- ``stage``：关系阶段（陌生/初识/熟悉/亲近…）；
- ``stance/intent/monologue/behavior``：由 render_rule 或模型生成，描述「这一轮怎么表现」。

约定：
- 数值字段一律 clamp 到 [0, 1]，不做静默修正以外的解释；
- 类型非法（非数值、behavior 不是 dict、source 不在白名单）抛 ValueError，
  调用方（inject.model 模式）据此回退 rule。
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

SOURCES = ("rule", "model", "static")
LENGTHS = ("short", "normal", "long")
EMOJI = ("偶尔", "不用")
MAX_MONOLOGUE = 40

DEFAULT_BEHAVIOR = {"length": "normal", "ask": True, "advice": False, "emoji": "偶尔"}

_LENGTH_ALIASES = {
    "short": "short", "brief": "short", "短": "short", "短句": "short", "s": "short",
    "normal": "normal", "medium": "normal", "mid": "normal", "正常": "normal", "m": "normal",
    "long": "long", "长": "long", "长句": "long", "l": "long",
}
_EMOJI_ALIASES = {
    "偶尔": "偶尔", "少量": "偶尔", "有": "偶尔", "yes": "偶尔", "true": "偶尔",
    "不用": "不用", "无": "不用", "no": "不用", "false": "不用", "none": "不用", "": "不用",
}
_TRUE = {"true", "1", "yes", "y", "on", "是", "要", "可以"}
_FALSE = {"false", "0", "no", "n", "off", "否", "不要", "不用"}
_WS = re.compile(r"\s+")


def _clamp01(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数值") from exc
    if math.isnan(number) or math.isinf(number):
        raise ValueError(f"{name} 必须是有限数值")
    return max(0.0, min(1.0, number))


def _to_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in _TRUE:
            return True
        if raw in _FALSE:
            return False
    raise ValueError(f"{name} 必须是布尔值")


def _clean_str(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, (str, int, float)):
        raise ValueError(f"{name} 必须是字符串")
    text = _WS.sub(" ", str(value).replace("\r", " ").replace("\n", " ")).strip()
    return text[:limit]


def _validate_behavior(value: Any) -> dict:
    if value is None:
        return dict(DEFAULT_BEHAVIOR)
    if not isinstance(value, dict):
        raise ValueError("behavior 必须是 dict")
    out = dict(DEFAULT_BEHAVIOR)
    if "length" in value:
        raw = str(value["length"]).strip().lower()
        if raw not in _LENGTH_ALIASES:
            raise ValueError(f"behavior.length 非法：{value['length']!r}")
        out["length"] = _LENGTH_ALIASES[raw]
    for key in ("ask", "advice"):
        if key in value:
            out[key] = _to_bool(value[key], f"behavior.{key}")
    if "emoji" in value:
        raw = str(value["emoji"]).strip().lower()
        if raw not in _EMOJI_ALIASES:
            raise ValueError(f"behavior.emoji 非法：{value['emoji']!r}")
        out["emoji"] = _EMOJI_ALIASES[raw]
    return out


@dataclass
class InnerState:
    """一轮的内在状态。

    数值字段约束在 [0, 1]；``behavior`` 是最终给模型的**行为参数**：
    length=回复长度档位，ask=是否允许追问，advice=是否允许给建议，emoji=表情使用。
    ``patience``/``openness`` 是由 mood/sharing 派生的行为维度（specs/09 的 patience/openness），
    单独列出是为了让阈值可直接断言；未显式给出时分别等于 mood / sharing。
    """

    mood: float = 0.55
    energy: float = 0.6
    sharing: float = 0.45
    battery: float = 0.7
    trust: float = 0.5
    stage: str = "陌生"
    stance: str = "平和"
    intent: str = "自然接话"
    monologue: str = ""
    behavior: dict = field(default_factory=lambda: dict(DEFAULT_BEHAVIOR))
    source: str = "rule"
    patience: float | None = None
    openness: float | None = None
    crisis: bool = False

    def __post_init__(self) -> None:
        if self.patience is None:
            self.patience = self.mood
        if self.openness is None:
            self.openness = self.sharing
        if not isinstance(self.behavior, dict):
            raise ValueError("behavior 必须是 dict")

    def to_dict(self) -> dict:
        return asdict(self)


def from_state(st: dict, trust_value: float) -> InnerState:
    """把 ``state.load()`` 的结果 + 信任分映射成 InnerState。"""
    data: dict = st if isinstance(st, dict) else {}
    own = data.get("self") if isinstance(data.get("self"), dict) else {}
    rel = data.get("relationship") if isinstance(data.get("relationship"), dict) else {}
    boundary = data.get("boundary") if isinstance(data.get("boundary"), dict) else {}
    raw: dict = {
        "trust": 0.5 if trust_value is None else trust_value,
        "stage": rel.get("stage") or "陌生",
        "crisis": bool(boundary.get("crisis")),
        "source": "rule",
    }
    for key in ("mood", "energy", "sharing", "battery"):
        value = own.get(key)
        if value is not None:
            raw[key] = value
    return validate(raw)


def validate(d: dict) -> InnerState:
    """校验并归一化一份状态 dict；越界 clamp，非法抛 ValueError。"""
    if not isinstance(d, dict):
        raise ValueError("InnerState 必须是 dict")
    out = InnerState()
    for key in ("mood", "energy", "sharing", "battery", "trust"):
        if key in d:
            setattr(out, key, _clamp01(d[key], key))
    out.patience = _clamp01(d["patience"], "patience") if "patience" in d else out.mood
    out.openness = _clamp01(d["openness"], "openness") if "openness" in d else out.sharing
    if "stage" in d:
        out.stage = _clean_str(d["stage"], "stage", 20) or "陌生"
    if "stance" in d:
        out.stance = _clean_str(d["stance"], "stance", 30) or "平和"
    if "intent" in d:
        out.intent = _clean_str(d["intent"], "intent", 60) or "自然接话"
    if "monologue" in d:
        out.monologue = _clean_str(d["monologue"], "monologue", MAX_MONOLOGUE)
    if "behavior" in d:
        out.behavior = _validate_behavior(d["behavior"])
    if "source" in d:
        if d["source"] not in SOURCES:
            raise ValueError(f"source 非法：{d['source']!r}")
        out.source = d["source"]
    if "crisis" in d:
        out.crisis = _to_bool(d["crisis"], "crisis")
    return out
