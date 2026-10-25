# -*- coding: utf-8 -*-
"""语气改写的唯一 prompt 构造器（训练与推理必须逐字节一致）。

两条链路：
1. **AI 口吻 input 生成**（DeepSeek 模拟线上人格，造数据/评测用）：
   `build_ai_style_messages(context, intended)` / `build_ai_open_messages(context)`
2. **改写器训练/推理**（小模型把 AI 内容改成用户语气）：
   `build_messages(context, ai_input)`

改写器 system 固定：把 AI 想说的内容改写成“我”平时的说话方式；只改说法，不改意思、
不新增事实、不加解释。附 3–5 条 `fewshot.json` 真实短句（只学语气，不要照抄内容）。

训练（tone_style_train.py）与评测（tone_style_eval.py）都必须调用本文件，不要各自拼 prompt。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FEWSHOT_PATH = ROOT / "agi" / "eval_data" / "human_tone" / "fewshot.json"
FEWSHOT_COUNT = 4

BASE_SYSTEM = (
    "你是改写器。把 AI 想说的内容，改写成“我”平时在微信/QQ 里的说话方式；"
    "只改说法，不改意思、不新增事实、不加解释。"
)

#: AI 人格的说话方式（与线上 `agi/selflearn/proactive.py` 的 VOICE_CONSTRAINTS 对齐）
AI_PERSONA_SYSTEM = (
    "你是这个 AI：一个在微信里跟用户聊天的数字生命。用你平时真实的说话方式表达内容。\n"
    "聊天语气（必须遵守）：\n"
    "- 只说你此刻的内心念头/感受/评价，不汇报动作流水账；\n"
    "- 口语语气词自然带出（吧/啊/呢/呀/诶/嗯），每条最多 1 个；\n"
    "- 短句为主：多数 2–12 字，一条只说一件事；\n"
    "- 句尾默认不加任何标点，只有真提问才用？；\n"
    "- 偶尔可以带 1 个 emoji，不要多；\n"
    "- 不要客服腔、不要列清单、不要解释、不要复述上下文。"
)

#: prompt 版本（训练/评测记录用，改 prompt 必须 bump）
PROMPT_VERSION = "tone_style_ai_v2"


def _load_fewshot_phrases() -> list[str]:
    """从 fewshot.json 取 3–5 条真实短句，优先短句。"""
    if not FEWSHOT_PATH.exists():
        return []
    try:
        payload = json.loads(FEWSHOT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    phrases: list[str] = []
    for item in payload.get("examples") or []:
        text = str(item.get("text") or "").strip()
        if text and text not in phrases:
            phrases.append(text)
    short = [p for p in phrases if len(p) <= 8]
    long = [p for p in phrases if len(p) > 8]
    return (short + long)[:FEWSHOT_COUNT]


def _fewshot_block() -> str:
    phrases = _load_fewshot_phrases()
    if not phrases:
        return ""
    return ("\n参考语气（这些是“我”以前真实说过的话，只学语气、长度和标点习惯，"
            "不要照抄内容）：\n" + "\n".join(f"- {phrase}" for phrase in phrases))


def build_system_prompt() -> str:
    """改写器 system（小模型训练/推理用）。"""
    return BASE_SYSTEM + _fewshot_block()


def build_ai_persona_system() -> str:
    """AI 人格 system（DeepSeek 造 input 用，带同一批 fewshot 参考）。"""
    return AI_PERSONA_SYSTEM + _fewshot_block()


def _context_messages(context: list[dict] | None) -> list[dict]:
    messages: list[dict] = []
    for item in context or []:
        role = item.get("role")
        if role in ("user", "assistant"):
            content = str(item.get("content") or "")
            if content.strip():
                messages.append({"role": role, "content": content})
    return messages


def build_ai_style_messages(context: list[dict] | None, intended: str) -> list[dict]:
    """DeepSeek 用：把「要表达的内容（意思）」用 AI 平时口吻说出来（造 input 数据）。"""
    messages = [{"role": "system", "content": build_ai_persona_system()}]
    messages.extend(_context_messages(context))
    messages.append({
        "role": "user",
        "content": (
            f"你想表达的内容（意思）：{intended}\n"
            "请用你平时的说话方式发一条微信消息（≤30 字、一句话，用自己的说法，"
            "不要照抄原句、不要解释）。只输出 JSON：{\"ai_input\": \"...\"}"
        ),
    })
    return messages


def build_ai_open_messages(context: list[dict] | None) -> list[dict]:
    """评测用：只给上下文，让 AI 人格现场决定说什么（模拟线上）。"""
    messages = [{"role": "system", "content": build_ai_persona_system()}]
    messages.extend(_context_messages(context))
    messages.append({
        "role": "user",
        "content": (
            "根据上面的聊天上下文，用你平时的说话方式主动发一条微信消息"
            "（≤30 字、一句话，只输出 JSON：{\"ai_input\": \"...\"}）。"
        ),
    })
    return messages


#: 抽锚点时的停用词（只做轻量启发式，不引入分词依赖）
_ANCHOR_STOP = (
    "如果", "但是", "因为", "所以", "然后", "可以", "我们", "你们", "他们", "这个",
    "那个", "什么", "怎么", "就是", "没有", "现在", "时候", "东西", "事情", "觉得",
    "感觉", "可能", "真的", "有点", "比较", "还是", "已经", "应该", "出来", "起来",
    "一下", "这种", "那种", "这样", "那样", "哪里", "这里", "那边", "这边", "今晚",
    "明天", "今天", "昨天", "上次", "下次", "一个", "不是", "知道", "还休", "意思",
    "关系", "问题", "地方", "东西", "干活", "做件",
)
_NUM_UNIT_RE = re.compile(r"[一二三四五六七八九十百千万两几][个天块元年月日次点岁帧遍周]")
#: 锚点 token 首尾需要剥掉的常用动词/虚词/指代字（启发式，不引分词依赖）
_TRIM_CHARS = set(
    "的地得了吧啊呢呀哦嘛么哈就还也都很太再又在这那一个下上里边去来做到买给看说"
    "想要能会有没了吃喝完睡洗走跑坐躺玩干整搞开叫问发拿放用完过你我他她它"
)


def _trim_token(token: str) -> str:
    """把 token 首尾的动词/虚词/指代字反复剥掉，尽量留下名词性核心。"""
    value = token
    changed = True
    while len(value) >= 2 and changed:
        changed = False
        if value[0] in _TRIM_CHARS:
            value = value[1:]
            changed = True
        if len(value) >= 2 and value[-1] in _TRIM_CHARS:
            value = value[:-1]
            changed = True
    return value


#: 明显是虚词/动词/代词的字：锚点窗口里出现就跳过，避免把整句片段当实体
_FUNCTION_CHARS = set(
    "的地得了吧啊呢呀哦嘛么哈就还也都很太再又在这那一个下上里边去来做到买给看说"
    "想要能会有没了吃喝完睡洗走跑坐躺玩干整搞开叫问发拿放用完过你我他她它们咋啥"
    "拜吵吐真先事说过吗"
)


def _content_tokens(run: str) -> list[str]:
    """从一段 CJK run 里取 2–4 字“像实体”的子串（不含虚词/动词/代词）。"""
    run = _trim_token(run)
    tokens: list[str] = []
    for length in (4, 3, 2):
        for index in range(0, len(run) - length + 1):
            token = run[index:index + length]
            if any(ch in _FUNCTION_CHARS for ch in token):
                continue
            if any(stop in token for stop in _ANCHOR_STOP):
                continue
            tokens.append(token)
    return tokens


def extract_anchors(text: str, context_text: str = "", max_anchors: int = 4) -> list[str]:
    """从 AI input 抽「必须保留」的信息：数字/数量词 + 像实体的 2–4 字片段。"""
    value = str(text or "")
    numeric: list[str] = []
    nouns: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\d+(?:\.\d+)?[一-鿿]?" + "|"
                             + _NUM_UNIT_RE.pattern, value):
        token = match.group(0)
        if token and token not in seen:
            seen.add(token)
            numeric.append(token)
    for match in re.finditer(r"[一-鿿]{2,8}", value):
        for token in _content_tokens(match.group(0)):
            if token not in seen:
                seen.add(token)
                nouns.append(token)
    if context_text:
        numeric = [t for t in numeric if True]
        nouns = [t for t in nouns if t in context_text]
    numeric = [t for t in numeric if not any(t != other and t in other for other in numeric)]
    nouns = [t for t in nouns if not any(t != other and t in other for other in numeric + nouns)]
    anchors = numeric + nouns
    return anchors[:max_anchors]


def build_messages(context: list[dict] | None, ai_input: str,
                   anchors: list[str] | None = None) -> list[dict]:
    """改写器推理/训练：system + 上下文 + 最后一条 user(AI 想说的内容 + 必须保留的信息)。"""
    messages: list[dict] = [{"role": "system", "content": build_system_prompt()}]
    messages.extend(_context_messages(context))
    content = str(ai_input or "").strip()
    if anchors:
        content += ("\n\n（改写时必须保留这些信息：" + "、".join(anchors)
                    + "；只换说法，不新增事实、不加解释）")
    messages.append({"role": "user", "content": content})
    return messages


def prompt_fingerprint() -> dict:
    """记录 prompt 版本与 system 文本，便于训练/评测一致性检查。"""
    return {"version": PROMPT_VERSION,
            "rewriter_system": build_system_prompt(),
            "ai_persona_system": build_ai_persona_system()}
