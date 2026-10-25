# -*- coding: utf-8 -*-
"""上下文预算：粗估 token、按段落优先级裁剪、记账。

P1-f（2026-10-25）：``fit()`` 的"头 55% + 尾 45%"暴力裁剪会把人设/记忆拦腰切断且
无记账；新增 :func:`assemble` 按 section 优先级**整段丢弃**，protect 段永丢；
丢完仍超才在段内按句子从尾部裁剪。未超预算时输出与直接拼接**逐字节一致**。
"""
from __future__ import annotations

import re

MAX_PROMPT_TOKENS = 5200
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_SENT_SPLIT = re.compile(r"(?<=[。！？!?…；;])\s*")


def estimate_tokens(text: str) -> int:
    value = str(text or "")
    cjk = len(_CJK.findall(value))
    other = len(value) - cjk
    return int(cjk * 1.6 + other / 3.5) + 1


def fit(text: str, max_tokens: int = MAX_PROMPT_TOKENS) -> str:
    """兜底裁剪：超预算时保留前 55% 与后 45%（assemble 之后理论上不触发）。"""
    value = str(text or "")
    limit = max(128, int(max_tokens))
    if estimate_tokens(value) <= limit:
        return value
    head_budget = int(limit * 0.55 * 0.62)
    tail_budget = max(64, limit - head_budget - 4)
    head_chars = max(1, int(head_budget / 1.6))
    tail_chars = max(1, int(tail_budget / 1.6))
    return value[:head_chars] + "\n…（上下文过长，已裁剪）…\n" + value[-tail_chars:]


def _normalize(sections) -> list[dict]:
    out: list[dict] = []
    for i, raw in enumerate(sections or []):
        item = dict(raw or {})
        item["name"] = str(item.get("name") or f"section_{i}")
        item["text"] = str(item.get("text") or "")
        try:
            item["priority"] = int(item.get("priority", 0) or 0)
        except (TypeError, ValueError):
            item["priority"] = 0
        try:
            item["min_tokens"] = max(0, int(item.get("min_tokens", 0) or 0))
        except (TypeError, ValueError):
            item["min_tokens"] = 0
        item["protect"] = bool(item.get("protect"))
        out.append(item)
    return out


def _join(items: list[dict]) -> str:
    return "".join(str(item.get("text") or "") for item in items)


def _truncate_tail_sentences(text: str, target_tokens: int,
                             floor_tokens: int = 0) -> str:
    """从尾部按句子删除，直到 ≤ target_tokens（不低于 floor_tokens）。"""
    value = str(text or "")
    if estimate_tokens(value) <= target_tokens:
        return value
    pieces = [piece for piece in _SENT_SPLIT.split(value) if piece]
    if len(pieces) <= 1:
        # 没有句读：只能按字符粗裁（尽量留给 protect 的锚点）
        keep_chars = max(1, int(len(value) *
                                max(0.0, target_tokens / max(1, estimate_tokens(value)))))
        return value[:keep_chars]
    keep = len(pieces)
    while keep > 1:
        candidate = "".join(pieces[:keep - 1])
        if estimate_tokens(candidate) <= target_tokens:
            return candidate
        if floor_tokens and estimate_tokens(candidate) < floor_tokens:
            return candidate
        keep -= 1
    return pieces[0]


def assemble(sections, max_tokens: int = MAX_PROMPT_TOKENS) -> dict:
    """按段落优先级裁剪 prompt；返回 ``{text, tokens, kept, dropped, truncated}``。

    - ``protect=True`` 的段（人设/状态/最近对话/当前用户原话/收尾指令）永不整段丢
    - 其余按 ``priority`` 从低到高尝试整段丢弃，直到符合预算
    - 丢完仍超预算，才在剩余段内按句子从尾部裁剪（标 ``truncated``）
    - 未超预算：输出 == 直接拼接（逐字节一致、幂等）
    """
    items = _normalize(sections)
    limit = max(1, int(max_tokens))
    full_text = _join(items)
    tokens = estimate_tokens(full_text)
    all_names = [item["name"] for item in items if item["text"]]
    if tokens <= limit:
        return {"text": full_text, "tokens": tokens, "kept": all_names,
                "dropped": [], "truncated": []}

    current = [dict(item) for item in items]
    # 整段丢弃：priority 低者先丢；同优先级按原始顺序
    order = sorted((i for i, item in enumerate(current) if not item["protect"]),
                   key=lambda i: (current[i]["priority"], i))
    dropped: list[str] = []
    for idx in order:
        if estimate_tokens(_join(current)) <= limit:
            break
        dropped.append(current[idx]["name"])
        current[idx]["text"] = ""

    truncated: list[str] = []
    if estimate_tokens(_join(current)) > limit:
        # 仍超：对剩余段（含 protect）从低优先级开始按句子裁尾巴
        remaining = [i for i, item in enumerate(current) if item["text"]]
        candidates = sorted(remaining,
                            key=lambda i: (current[i]["protect"], current[i]["priority"], i))
        for idx in candidates:
            if estimate_tokens(_join(current)) <= limit:
                break
            others = estimate_tokens(_join(
                [item for j, item in enumerate(current) if j != idx]))
            target = max(0, limit - others)
            floor = current[idx]["min_tokens"]
            new_text = _truncate_tail_sentences(current[idx]["text"], target, floor)
            if new_text != current[idx]["text"]:
                current[idx]["text"] = new_text
                truncated.append(current[idx]["name"])

    # 极端兜底：所有段都被裁到 floor 仍超 → 从最长的非空段硬裁，直到符合或无内容
    guard = 0
    while estimate_tokens(_join(current)) > limit and guard < 200:
        guard += 1
        longest = max(
            (i for i, item in enumerate(current) if item["text"]),
            key=lambda i: len(current[i]["text"]), default=None)
        if longest is None:
            break
        item = current[longest]
        if item["name"] not in truncated:
            truncated.append(item["name"])
        keep = max(1, int(len(item["text"]) * 0.9))
        if keep >= len(item["text"]):
            keep = len(item["text"]) - 1
        if keep <= 0:
            item["text"] = ""
            continue
        item["text"] = item["text"][:keep]

    text = _join(current)
    kept = [item["name"] for item in current if item["text"]]
    return {"text": text, "tokens": estimate_tokens(text),
            "kept": kept, "dropped": dropped, "truncated": truncated}
