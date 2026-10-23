# -*- coding: utf-8 -*-
"""03 防幻觉：生成后的轻量规则校验，不重调大模型。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

ABSOLUTE = {
    "一定": "可能",
    "从来": "印象里",
    "永远": "通常",
    "所有人": "很多人",
    "绝对": "很可能",
}
TOOL_CLAIMS = ("我查了", "我搜了", "我看了资料", "我上网查")
NUMBER = re.compile(r"(?<!\d)(?:\d{2,4}年|\d{1,2}月|\d{1,2}日|\d+(?:\.\d+)?\s*(?:元|块|岁|点|个|次|天|小时|分钟|%|公里|km|kg))")
CITATION = re.compile(r"\[(M-[0-9a-zA-Z_-]+)\]")


def _rewrite(reply: str) -> tuple[str, list[str]]:
    issues = []
    out = reply
    for word, soft in ABSOLUTE.items():
        if word in out:
            issues.append(f"absolute:{word}")
            out = out.replace(word, soft)
    if any(claim in out for claim in TOOL_CLAIMS):
        issues.append("tool_claim_without_tool")
        out = "我不太确定，先不装作查过。" + re.sub(
            "|".join(re.escape(x) for x in TOOL_CLAIMS), "", out)
    if len(out) > 800:
        issues.append("too_long")
        out = out[:800].rstrip() + "…"
    return out, issues


def check(reply: str, path: Path | str = DB_PATH, *, context: str = "") -> dict[str, Any]:
    """返回 {level, reply, issues}；L2 用安全回复，不重试。"""
    text = str(reply or "").strip()
    if not text:
        return {"level": "L2", "reply": "我不太确定该怎么说。你愿意再多告诉我一点吗？",
                "issues": ["empty"]}
    rewritten, issues = _rewrite(text)
    ctext = str(context or "")
    if ctext:
        for num in NUMBER.findall(rewritten):
            if num not in ctext:
                issues.append(f"number_not_in_context:{num}")
                break
    # 引用必须是本地真实 message id；不存在的引用直接删掉
    bad_citations = []
    for cid in CITATION.findall(rewritten):
        con = db.connect(path)
        try:
            row = con.execute("SELECT 1 FROM messages WHERE id=?", (cid,)).fetchone()
        finally:
            con.close()
        if row is None:
            bad_citations.append(cid)
    if bad_citations:
        issues.append("bad_citation")
        for cid in bad_citations:
            rewritten = rewritten.replace(f"[{cid}]", "")
    if any(i.startswith("tool_claim") for i in issues) or len(issues) >= 2:
        return {"level": "L2",
                "reply": "这个我不太确定，先不瞎说。你可以再告诉我一点吗？",
                "issues": issues}
    if issues:
        return {"level": "L1", "reply": rewritten, "issues": issues}
    return {"level": "L0", "reply": text, "issues": []}


def open_conflict_question(path: Path | str = DB_PATH) -> str:
    """把未解决记忆冲突变成一句自然确认，不审问。"""
    from . import memory
    conflicts = memory.open_conflicts(path)
    if not conflicts:
        return ""
    con = db.connect(path)
    try:
        a = con.execute("SELECT content FROM user_memories WHERE id=?",
                        (conflicts[0]["mem_a"],)).fetchone()
        b = con.execute("SELECT content FROM user_memories WHERE id=?",
                        (conflicts[0]["mem_b"],)).fetchone()
    finally:
        con.close()
    if not a or not b:
        return ""
    return f"我好像记得「{a['content']}」，又记得「{b['content']}」，哪个才是现在的？"
