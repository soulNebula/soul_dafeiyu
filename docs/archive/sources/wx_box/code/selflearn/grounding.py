# -*- coding: utf-8 -*-
"""M2 无据断言弱审计。

不是 NLI，也不能证明事实正确；只检查回复中的陈述句是否和用户原话/记忆/摘要
有字符级重叠。用来发现明显跑题或凭空补细节，不能当最终裁判。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import db

_QUESTION = re.compile(r"[?？]$|(吗|呢|么)$|^(什么|怎么|为什么|哪|谁|多少|是否|要不要|能不能)")
_SOFT = re.compile(r"^(我觉得|我感觉|可能|也许|大概|应该|听起来|好像)")
_CJK = re.compile(r"[\u3400-\u9fff]{2,}")
_ASCII = re.compile(r"[A-Za-z0-9_+#.\-]{2,}")


def content_tokens(text: str) -> set[str]:
    value = str(text or "")
    tokens: set[str] = set()
    for segment in _CJK.findall(value):
        if len(segment) == 1:
            tokens.add(segment)
        for i in range(len(segment) - 1):
            tokens.add(segment[i:i + 2])
    tokens |= {t.lower() for t in _ASCII.findall(value)}
    return {t for t in tokens if t not in ("这个", "那个", "就是", "然后", "还是", "可以")}


def supported(sentence: str, contexts: list[str]) -> bool:
    tokens = content_tokens(sentence)
    if not tokens:
        return True
    blob = "\n".join(contexts)
    if not blob.strip():
        return False
    hit = sum(1 for t in tokens if t in blob)
    return hit / max(1, len(tokens)) >= 0.6


def check_reply(reply: str, contexts: list[str]) -> dict[str, Any]:
    sentences = [s.strip() for s in re.split(r"[。！？!?\n]+", str(reply or "")) if s.strip()]
    claims = []
    for s in sentences:
        if _QUESTION.search(s) or _SOFT.search(s):
            continue
        claims.append({"sentence": s, "supported": supported(s, contexts)})
    bad = [c for c in claims if not c["supported"]]
    return {"ok": True, "claims": claims, "unsupported": len(bad),
            "rate": round(len(bad) / len(claims), 3) if claims else 0.0,
            "bad": bad[:10], "note": "字符重叠弱审计，不是 NLI"}


def audit(path: Path | str, *, limit: int = 50) -> dict[str, Any]:
    con = db.connect(path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT t.reply,m.text FROM turns t LEFT JOIN messages m "
            "ON m.id=t.user_message_id ORDER BY t.created_at DESC LIMIT ?", (max(1, int(limit)),))]
        memories = [str(r["content"] or "") for r in con.execute(
            "SELECT content FROM user_memories WHERE status='active' LIMIT 30")]
    finally:
        con.close()
    results = []
    for r in rows:
        res = check_reply(str(r.get("reply") or ""), [str(r.get("text") or "")] + memories)
        results.append(res)
    total = sum(len(x["claims"]) for x in results)
    bad = sum(x["unsupported"] for x in results)
    rate = bad / total if total else 0.0
    if not total:
        verdict = "insufficient"
    elif rate < 0.05:
        verdict = "pass"
    elif rate > 0.80:
        verdict = "unreliable"  # 字符重叠法把改写/共情全误报，不能当质量结论
    else:
        verdict = "needs_work"
    return {"ok": True, "turns": len(results), "claims": total, "unsupported": bad,
            "rate": round(rate, 3), "verdict": verdict,
            "note": "字符重叠弱审计；unreliable 表示该指标当前不可用于生产判罚。"}
