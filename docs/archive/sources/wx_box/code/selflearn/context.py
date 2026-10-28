# -*- coding: utf-8 -*-
"""上下文预算：粗估 token 并裁剪，优先保留开头设定与结尾用户原话。"""
from __future__ import annotations

import re

MAX_PROMPT_TOKENS = 5200
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def estimate_tokens(text: str) -> int:
    value = str(text or "")
    cjk = len(_CJK.findall(value))
    other = len(value) - cjk
    return int(cjk * 1.6 + other / 3.5) + 1


def fit(text: str, max_tokens: int = MAX_PROMPT_TOKENS) -> str:
    """超预算时保留前 55% 与后 45%，中间加省略标记。"""
    value = str(text or "")
    limit = max(128, int(max_tokens))
    if estimate_tokens(value) <= limit:
        return value
    head_budget = int(limit * 0.55 * 0.62)
    tail_budget = max(64, limit - head_budget - 4)
    head_chars = max(1, int(head_budget / 1.6))
    tail_chars = max(1, int(tail_budget / 1.6))
    return value[:head_chars] + "\n…（上下文过长，已裁剪）…\n" + value[-tail_chars:]
