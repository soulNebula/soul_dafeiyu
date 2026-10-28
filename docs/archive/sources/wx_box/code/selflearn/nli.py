# -*- coding: utf-8 -*-
"""M2 语义蕴含校验：可选 LLM judge，不是规则替代品。"""
from __future__ import annotations

import json
import re
from typing import Any


def _sentences(reply: str) -> list[str]:
    return [s.strip() for s in re.split(r"[。！？!?\n]+", str(reply or "")) if s.strip()]


def verify(reply: str, contexts: list[str], client) -> dict[str, Any]:
    sentences = _sentences(reply)
    if not sentences:
        return {"ok": False, "error": "没有可校验句子"}
    if client is None or not getattr(client, "available", lambda: False)():
        return {"ok": False, "error": "LLM 不可用"}
    ctx = "\n".join(str(c) for c in contexts if str(c).strip())[:4000]
    prompt = (
        "判断下面每条回复陈述是否被上下文支持。只输出 JSON："
        '{"claims":[{"index":0,"supported":true,"reason":"..."}]}。'
        "上下文没有依据就标 false；不要用常识补。\n"
        + "\n".join(f"{i}. {s}" for i, s in enumerate(sentences))
        + "\n\n上下文：\n" + ctx
    )
    try:
        r = client.chat([{"role": "user", "content": prompt}],
                        purpose="selflearn_nli", max_tokens=1200, temperature=0.1,
                        json_mode=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:150]}"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "LLM 失败"}
    try:
        obj = json.loads(str(r.get("text") or ""))
        items = obj.get("claims") if isinstance(obj, dict) else None
    except (TypeError, ValueError):
        return {"ok": False, "error": "NLI 输出不是 JSON"}
    if not isinstance(items, list):
        return {"ok": False, "error": "NLI 输出没有 claims"}
    supported = 0
    bad = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("supported") is True:
            supported += 1
        else:
            idx = item.get("index")
            bad.append({"index": idx, "reason": str(item.get("reason") or "")[:200],
                        "sentence": sentences[idx] if isinstance(idx, int) and 0 <= idx < len(sentences) else ""})
    total = len(sentences)
    return {"ok": True, "total": total, "supported": supported,
            "unsupported": max(0, total - supported),
            "rate": round(supported / total, 3) if total else 0.0,
            "bad": bad[:10],
            "note": "LLM judge 弱校验；不是形式化 NLI。"}
