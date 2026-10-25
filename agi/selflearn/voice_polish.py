# -*- coding: utf-8 -*-
"""线上回复的“口语化安全层”（pure functions，无 I/O、无模型、无依赖）。

用途：把线上人格生成的回复做**安全的口语化后处理**，不改名词/数字/实体、不加事实。
这是线 B 评测里方案 C 的生产实现（C 在 17 条 held-out 上：原意 100%、锚点 100%、
幻觉 0、风格 58.8%、回退 0）。

规则顺序（幂等）：
1. 去掉句尾 `。`（保留 `？`）；
2. 书面连接词 → 口语映射（然而→但是、因此→所以、非常→挺、我认为→我觉得、
   此外→另外、并且→而且、以及→还有、即使→就算；“因为…所以”这类保留）；
3. 长度 > `max_chars` 时按逗号截前 2 句；若截断会丢掉受保护实体/数字，则跳过截断；
4. 5–10% 概率在句末加 1 个语气词（吧/啊/呢/呀），句末已有标点或已是语气词则跳过；
5. 幂等（同一文本重复处理结果不变）。

受保护 token（`protected_tokens`）：
- 数字 + 常见单位（200帧/50块/3点/20%），
- ASCII/字母数字混排（occ、Mary、RTX4090、WiFi5），
- 2–4 字中文实体词（排除虚词与连接词映射源）。
硬约束：这些 token 在输出里一个都不能少。

用法::

    from agi.selflearn.voice_polish import polish_reply
    result = polish_reply("然而我今天非常累。", rng=None, max_chars=40)
    # {"text": "但是我今天挺累", "changed": True, "reasons": ["strip_period", ...]}
"""
from __future__ import annotations

import random
import re
from typing import Iterable

#: 书面连接词 → 口语（“因为…所以”保留，不做替换）
WRITTEN_TO_SPOKEN: tuple[tuple[str, str], ...] = (
    ("然而", "但是"),
    ("因此", "所以"),
    ("非常", "挺"),
    ("我认为", "我觉得"),
    ("此外", "另外"),
    ("并且", "而且"),
    ("以及", "还有"),
    ("即使", "就算"),
)

#: 句末语气词（用于幂等：已有语气词就不再追加）
PARTICLES = ("吧", "啊", "呢", "呀", "哦", "嘛", "诶", "哈")
#: 句末标点（有标点时不加语气词）
ENDING_PUNCT = "。！？!?…～~"
#: 截断优先在逗号/分号处切
CLAUSE_SPLIT_RE = re.compile(r"(?<=[，,；;])")
#: 句尾句号
TRAILING_PERIOD_RE = re.compile(r"[。]+$")
#: 数字 + 可选单位（中英文）
NUMBER_RE = re.compile(
    r"\d+(?:\.\d+)?(?:%|％)?(?:[元块岁天年月中日次个帧点分秒]|[A-Za-z]{1,4})?"
)
#: 英文/字母数字混排
ASCII_RE = re.compile(r"[A-Za-z][0-9A-Za-z_\-]*")
#: 中文候选词
CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,4}")

#: 虚词/功能词（不作为受保护实体）
FUNCTION_WORDS = {
    "因为", "所以", "但是", "然而", "因此", "非常", "我认为", "我觉得", "此外", "另外",
    "并且", "而且", "以及", "还有", "即使", "就算", "这个", "那个", "什么", "怎么",
    "就是", "没有", "现在", "时候", "东西", "事情", "觉得", "感觉", "可能", "真的",
    "有点", "比较", "还是", "已经", "应该", "出来", "起来", "一下", "这种", "那种",
    "这样", "那样", "哪里", "这里", "那边", "这边", "今晚", "明天", "今天", "昨天",
    "我们", "你们", "他们", "它们", "一个", "不是", "知道", "可以", "然后", "如果",
    "或者", "不过", "只是", "其实", "反正", "估计", "大概", "应该", "必须", "肯定",
}
#: 映射源里出现过的汉字，抽取实体时避开这些片段
_MAPPING_SOURCE_CHARS = set("".join(source for source, _ in WRITTEN_TO_SPOKEN))


def protected_tokens(text: str) -> list[str]:
    """抽取不能被改动的 token（数字/单位、ASCII 混排、2–4 字中文实体）。

    顺序稳定、去重；规则层不会修改这些 token；单测会验证输出里一个不少。
    """
    value = str(text or "")
    tokens: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)

    for match in NUMBER_RE.finditer(value):
        add(match.group(0))
    for match in ASCII_RE.finditer(value):
        add(match.group(0))
    # 中文：对每个 2–8 字 run 枚举 2–4 字窗口，保证「汕头」「泡面」这类实体一定被覆盖
    for match in re.finditer(r"[一-鿿]{2,8}", value):
        run = match.group(0)
        for length in (4, 3, 2):
            for index in range(0, len(run) - length + 1):
                token = run[index:index + length]
                if token in FUNCTION_WORDS:
                    continue
                if any(source in token or token in source
                       for source, _ in WRITTEN_TO_SPOKEN):
                    continue
                if set(token) & _MAPPING_SOURCE_CHARS:
                    continue
                add(token)
    return tokens


def _strip_trailing_period(text: str, reasons: list[str]) -> str:
    stripped = TRAILING_PERIOD_RE.sub("", text)
    if stripped != text:
        reasons.append("strip_period")
    return stripped


def _spoken_connectives(text: str, reasons: list[str]) -> str:
    value = text
    for source, target in WRITTEN_TO_SPOKEN:
        if source in value:
            value = value.replace(source, target)
            reasons.append(f"connective:{source}->{target}")
    return value


def _truncate(text: str, max_chars: int, reasons: list[str]) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    parts = [part for part in CLAUSE_SPLIT_RE.split(text) if part.strip()]
    if len(parts) <= 2:
        return text
    candidate = "".join(parts[:2]).rstrip("，,；;")
    missing = [token for token in protected_tokens(text) if token not in candidate]
    if missing:
        reasons.append("truncate_skipped_protected")
        return text
    reasons.append("truncate")
    return candidate


def _add_particle(text: str, rng: random.Random, reasons: list[str]) -> str:
    if not text or text[-1] in ENDING_PUNCT or text[-1] in PARTICLES:
        return text
    if rng.random() >= 0.07:
        return text
    particle = rng.choice(("吧", "啊", "呢", "呀"))
    reasons.append(f"add_particle:{particle}")
    return text + particle


def polish_reply(text: str, *, rng: random.Random | None = None,
                 max_chars: int = 40) -> dict:
    """把回复做口语化安全处理，返回 `{text, changed, reasons}`。

    - 只做安全变换，不新增事实、不改名词/数字/实体；
    - 幂等：对已处理过的文本再跑一次，`text` 不变（`reasons` 可能为空）；
    - `rng` 用于语气词概率；不传则用 `random.Random(0)`（确定性）。
    """
    original = str(text or "")
    if not original.strip():
        return {"text": "", "changed": False, "reasons": []}
    generator = rng or random.Random(0)
    reasons: list[str] = []
    value = original
    value = _strip_trailing_period(value, reasons)
    value = _spoken_connectives(value, reasons)
    value = _truncate(value, max_chars, reasons)
    value = _add_particle(value, generator, reasons)
    return {"text": value, "changed": value != original, "reasons": reasons}
