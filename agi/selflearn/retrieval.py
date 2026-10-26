# -*- coding: utf-8 -*-
"""S8 混合检索：关键词召回 + 来源权重 + recency + MMR。"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

from . import search

SOURCE_WEIGHT = {"memory": 1.0, "claim": 0.9, "message": 0.7, "text": 0.6}
_CJK = re.compile(r"[\u3400-\u9fff]")


def _grams(text: str) -> set[str]:
    value = str(text or "")
    out = set()
    for seg in re.findall(r"[\u3400-\u9fff]{1,}", value):
        if len(seg) == 1:
            out.add(seg)
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
    return out


def _archived_message_ids(path: Path | str, source_ids: list[str]) -> dict[str, float]:
    """P1-d/P1-e：返回这些 message source_id 里已冷归档的 {id: ts}。

    用轻量 sqlite3 连接（不跑 db.connect 的全量表迁移），避免给每轮检索加开销；
    ts 用于渲染时走 timefuzz.relative（"更早以前/很久以前"）。
    """
    ids = [str(i) for i in source_ids if str(i)]
    if not ids:
        return {}
    try:
        from . import db as _db
        con = _db.memory_connect(path)
    except Exception:  # noqa: BLE001
        return {}
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(messages)").fetchall()}
        if "archived_at" not in cols:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = con.execute(
            f"SELECT id, ts FROM messages WHERE archived_at IS NOT NULL "
            f"AND id IN ({marks})", ids).fetchall()
        return {str(r[0]): float(r[1] or 0.0) for r in rows}
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def hybrid(path: Path | str, query: str, *, k: int = 6, now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else float(now)
    limit = max(1, int(k))
    raw = search.search(path, query, limit=max(8, limit * 4))
    if not raw:
        return []
    # P1-d：默认排除已冷归档（>90 天）的原文消息；用户明确"翻旧账"时才允许检索，
    # 并在结果上标 archived=True 供下游做时间模糊（"更早以前"）。
    try:
        from . import memory as _memory
        lookback = _memory.wants_lookback(query)
    except Exception:  # noqa: BLE001
        lookback = False
    archived_ts = _archived_message_ids(
        path, [str(r.get("source_id") or "") for r in raw
               if str(r.get("source_type") or "") == "message"])
    filtered: list[dict[str, Any]] = []
    for r in raw:
        st = str(r.get("source_type") or "text")
        sid = str(r.get("source_id") or "")
        is_archived = st == "message" and sid in archived_ts
        if is_archived and not lookback:
            continue
        item = dict(r)
        item["archived"] = is_archived
        if is_archived:
            item["archived_ts"] = archived_ts[sid]
        filtered.append(item)
    raw = filtered
    if not raw:
        return []
    max_score = max(float(r.get("score") or 0) for r in raw) or 1.0
    cands = []
    for i, r in enumerate(raw):
        text = str(r.get("text") or "")
        st = str(r.get("source_type") or "text")
        rel = float(r.get("score") or 0) / max_score
        source = SOURCE_WEIGHT.get(st, 0.6)
        recency = 1.0 / (1.0 + max(0, i) * 0.08)
        score = 0.6 * rel + 0.25 * source + 0.15 * recency
        cands.append({"source_type": st, "source_id": str(r.get("source_id") or ""),
                      "text": text, "score": round(score, 4), "relevance": round(rel, 4),
                      "source_weight": source,
                      "archived": bool(r.get("archived")),
                      "archived_ts": r.get("archived_ts"),
                      "tokens": _grams(text)})

    selected = []
    while cands and len(selected) < limit:
        best = None
        best_mmr = -1e9
        for c in cands:
            sim = 0.0
            if selected:
                sim = max((len(c["tokens"] & s["tokens"]) /
                           max(1, len(c["tokens"] | s["tokens"]))) for s in selected)
            mmr = 0.75 * c["score"] - 0.25 * sim
            if mmr > best_mmr:
                best, best_mmr = c, mmr
        selected.append(best)
        cands.remove(best)
    for s in selected:
        s.pop("tokens", None)
    return selected


def context(path: Path | str, query: str, *, k: int = 5,
            now: float | None = None) -> str:
    from . import timefuzz
    hits = hybrid(path, query, k=k, now=now)
    lines = []
    for h in hits:
        # P1-d/P1-e：归档内容用 timefuzz 的模糊时间前缀，绝不输出具体日期/天数
        if h.get("archived"):
            label = timefuzz.relative(h.get("archived_ts"), now) or timefuzz.COLD_LABEL
            prefix = f"[{label}] "
        else:
            prefix = ""
        lines.append(f"- {prefix}[{h['source_type']}] {h['text'][:160]}")
    return timefuzz.strip_absolute("\n".join(lines))
