# -*- coding: utf-8 -*-
"""纯规则后端 —— 给 ``relevance_score`` 这类简单用途兜底，不调模型、零成本。

目前支持：
- ``relevance_score``：用「最后一条消息 vs 前文」的字符二元组重合率算 0–1 分，
  输出 ``{"score": 0.xx}`` JSON 文本；结果确定、可复现。

其它用途明确返回 ``rule_unsupported_purpose``（不静默冒充模型输出）。
"""
from __future__ import annotations

import json
import re

LAYER_NAME = "rule"
SUPPORTED_PURPOSES = frozenset({"relevance_score"})
_WS = re.compile(r"\s+")


def _text_of(message) -> str:
    if not isinstance(message, dict):
        return str(message or "")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content
                       if isinstance(part, dict))
    return ""


def _bigrams(text: str) -> set[str]:
    value = _WS.sub("", str(text or "")).lower()
    return {value[i:i + 2] for i in range(max(0, len(value) - 1))}


def _relevance(messages: list[dict]) -> float:
    texts = [_text_of(item) for item in (messages or [])]
    texts = [text for text in texts if text.strip()]
    if len(texts) < 2:
        return 0.0
    query = _bigrams(texts[-1])
    context = _bigrams("".join(texts[:-1]))
    if not query or not context:
        return 0.0
    overlap = len(query & context) / float(len(query))
    return round(max(0.0, min(1.0, overlap)), 4)


class RuleBackend:
    kind = "rule"

    def __init__(self, name: str = "rule") -> None:
        self.name = str(name)

    def available(self) -> bool:
        return True

    def chat(self, messages: list[dict], *, purpose: str = "chat",
             max_tokens: int = 800, temperature: float = 0.7,
             timeout: float | None = None, json_mode: bool = False) -> dict:
        purpose = str(purpose or "chat")
        if purpose not in SUPPORTED_PURPOSES:
            return {"ok": False, "text": "",
                    "error": f"rule_unsupported_purpose:{purpose}",
                    "usage": {"tokens_in": 0, "tokens_out": 0}}
        if purpose == "relevance_score":
            text = json.dumps({"score": _relevance(messages)}, ensure_ascii=False)
        else:  # pragma: no cover - 目前只有 relevance_score
            text = "{}"
        return {"ok": True, "text": text, "error": "",
                "usage": {"tokens_in": 0, "tokens_out": 0}, "rule": True}
