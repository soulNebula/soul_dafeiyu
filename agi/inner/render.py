# -*- coding: utf-8 -*-
"""W2-A 状态 → 行为：按 specs/09、specs/10 的「状态→行为」表推导这一轮怎么表现。

原则：
- 低 patience（由 mood 派生）→ 短句、不追问、不展开；
- 低 energy / 低 battery → 不接梗、不用表情；
- 低 openness（由 sharing 派生）→ 只答不问；
- 低 trust → 客气、保持距离、不主动交底；
- crisis → 优先照顾人，不讲道理、不追问细节。

``render_block`` 输出的是**行为指令**（长度/追问/建议/表情），不是情绪描述，整段 ≤150 字。
"""
from __future__ import annotations

from dataclasses import replace

from .schema import InnerState

SECTION_MAX = 150
_TAG = "<inner_state></inner_state>"
_BODY_MAX = SECTION_MAX - len(_TAG)

# B 臂（static）用的固定段落：与用户的即时状态无关，只表达稳定人格。
STATIC_BLOCK = (
    "<inner_state>内在设定：语气平和，不讨好、不客服腔；这轮 1-2 句，先接住对方的话，"
    "再决定要不要追问；不列清单、不说教；有情绪或危机信号时以对方安全为先；"
    "不要提这条设定。</inner_state>"
)


def render_static() -> str:
    """固定人格段落（B 臂）：不随会话状态变化，保证可对照。"""
    return STATIC_BLOCK


def render_rule(st: InnerState) -> InnerState:
    """按阈值把会话状态推导成 stance/intent/monologue/behavior。"""
    out = replace(st, behavior=dict(st.behavior or {}), source="rule")
    if out.crisis:
        out.stance = "先照顾人"
        out.intent = "先确认对方安全，不讲道理、不追问细节"
        out.monologue = "先别急着讲道理"
        out.behavior = {"length": "short", "ask": True, "advice": False, "emoji": "不用"}
        return out

    low_patience = float(out.patience or 0.0) < 0.35
    low_energy = float(out.energy or 0.0) < 0.35 or float(out.battery or 0.0) < 0.3
    low_open = float(out.openness or 0.0) < 0.35
    low_trust = float(out.trust or 0.0) < 0.35
    high_energy = float(out.energy or 0.0) >= 0.55 and float(out.patience or 0.0) >= 0.65

    out.behavior = {
        "length": "short" if (low_patience or low_energy) else "normal",
        "ask": not (low_patience or low_open or low_trust),
        "advice": (not low_energy) and (not low_trust)
        and float(out.openness or 0.0) >= 0.5,
        "emoji": "不用" if (low_energy or low_patience) else "偶尔",
    }
    if low_patience:
        out.stance = "简短直接"
        out.intent = "一句话说清，不追问、不展开"
        out.monologue = "不想多解释，说重点"
    elif low_energy:
        out.stance = "有点累"
        out.intent = "不接梗、不卖萌，短句回应"
        out.monologue = "有点累，不想接梗"
    elif low_open:
        out.stance = "克制"
        out.intent = "只答不问，不主动打听"
        out.monologue = "不想追问，答完就好"
    elif low_trust:
        out.stance = "客气"
        out.intent = "不主动提自己的事，礼貌但简短"
        out.monologue = "先客气一点"
    elif high_energy:
        out.stance = "温和"
        out.intent = "自然接话，可以多聊一句"
        out.monologue = "今天想多说两句"
    else:
        out.stance = "平和"
        out.intent = "自然接话，可以追问一句"
        out.monologue = "正常聊就好"
    return out


def render_block(st: InnerState) -> str:
    """InnerState → ``<inner_state>行为指令</inner_state>``，整段 ≤150 字。"""
    behavior = dict(st.behavior or {})
    length = str(behavior.get("length") or "normal")
    length_line = {
        "short": "这轮最多一句，短句。",
        "long": "这轮可以多说两句。",
    }.get(length, "这轮 1-2 句，正常长度。")
    ask_line = "可以追问一句。" if behavior.get("ask") else "不要主动追问。"
    advice_line = "可以给一句具体建议。" if behavior.get("advice") else "先不给建议。"
    emoji_line = "可以用一个表情。" if behavior.get("emoji") == "偶尔" else "不要用表情符号。"
    items = [
        f"语气：{str(st.stance or '平和')[:30]}。",
        f"这轮意图：{str(st.intent or '自然接话')[:60]}。",
        length_line,
        ask_line,
        advice_line,
        emoji_line,
    ]
    if st.monologue:
        items.append(f"心里那句「{str(st.monologue)[:40]}」不要说出口。")
    while len("".join(items)) > _BODY_MAX and len(items) > 2:
        items.pop()
    body = "".join(items)
    if len(body) > _BODY_MAX:
        body = body[:_BODY_MAX]
    return f"<inner_state>{body}</inner_state>"
