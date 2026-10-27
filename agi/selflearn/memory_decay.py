# -*- coding: utf-8 -*-
"""P38 类海马体记忆衰减：strength / 渐进遗忘 / 唤醒 / 检索续命 / 休眠清理。

不物理删除；`purge_dormant` 默认 dry-run、200 天后才真正删。
仅操作 memory 库的 ``user_memories``；聊天/网关接线等 Lead 通知。

阶段：
- strength ≥0.5 → full（原文）
- 0.2–0.5 → compressed（≤40 字一句事实，原文进 content_full）
- 0.05–0.2 → keyword（2–4 关键词）
- <0.05 → dormant（status=dormant，不物理删；可 revive）
"""
from __future__ import annotations

import argparse
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = db.DB_PATH
DORMANT_PURGE_DAYS = 200
REVIVE_STRENGTH = 0.6
COMPRESS_CHARS = 40
COLUMNS = {
    "strength": "strength REAL NOT NULL DEFAULT 1.0",
    "last_strength_at": "last_strength_at REAL NOT NULL DEFAULT 0",
    "forget_stage": "forget_stage TEXT NOT NULL DEFAULT 'full'",
    "revive_count": "revive_count INTEGER NOT NULL DEFAULT 0",
    "content_full": "content_full TEXT NOT NULL DEFAULT ''",
}
#: 任务指定：profile 30d / preference 14d / event 7d / smalltalk 2d
HALF_LIFE_DAYS = {
    "profile": 30.0, "value": 30.0, "plan": 30.0, "fact": 30.0,
    "preference": 14.0, "insight": 14.0,
    "event": 7.0,
    "smalltalk": 2.0, "chat": 2.0,
}
DEFAULT_HALF_LIFE = 7.0
CJK_STOP = frozenset("用户 一个 一些 这个 那个 什么 怎么 就是 觉得 感觉 自己 我们 你们 他们 还是 已经 可以 可能 应该 因为 所以 但是 然后 其实".split())


def _now(value: float | None = None) -> float:
    return float(value) if value is not None else time.time()


def _connect(path: Path | str, *, write: bool) -> Any:
    # dry-run 用 direct memory connection，避免 ADD COLUMN/建表等写操作
    if write:
        return db.connect(path)
    return db.memory_connect(path)


def _columns(con) -> set[str]:
    try:
        return {str(row[1]) for row in con.execute("PRAGMA table_info(user_memories)").fetchall()}
    except Exception:  # noqa: BLE001
        return set()


def ensure_columns(con) -> list[str]:
    """惰性补齐新列（只在真正要写/revive 时调用）。"""
    existing = _columns(con)
    added = []
    for col, ddl in COLUMNS.items():
        if col not in existing:
            try:
                con.execute(f"ALTER TABLE user_memories ADD COLUMN {ddl}")
                added.append(col)
            except Exception:  # noqa: BLE001 - 并发/旧库兼容
                pass
    if added:
        try:
            con.execute(
                "UPDATE user_memories SET last_strength_at=created_at "
                "WHERE last_strength_at=0 OR last_strength_at IS NULL")
            con.execute(
                "UPDATE user_memories SET content_full=content "
                "WHERE content_full='' OR content_full IS NULL")
            con.commit()
        except Exception:  # noqa: BLE001
            pass
    return added


def half_life_days(kind: str, importance: float = 0.5) -> float:
    base = HALF_LIFE_DAYS.get(str(kind or "").strip().lower(), DEFAULT_HALF_LIFE)
    value = float(importance)
    if value >= 0.8:
        base *= 2.0
    elif value <= 0.3:
        base *= 0.5
    return max(0.1, base)


def stage_for(strength: float) -> str:
    value = float(strength)
    if value >= 0.5:
        return "full"
    if value >= 0.2:
        return "compressed"
    if value >= 0.05:
        return "keyword"
    return "dormant"


def compress_text(text: str) -> str:
    """压成 ≤40 字的一句事实（规则版；不调用 LLM）。"""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = re.sub(r"^用户", "", value).strip(" ，,。.;；：:")
    if not value:
        return ""
    # 优先取第一句
    match = re.split(r"[。！？!?；;\n]", value, maxsplit=1)
    sentence = match[0].strip() or value
    if len(sentence) > COMPRESS_CHARS:
        sentence = sentence[:COMPRESS_CHARS - 1].rstrip("，,。.;；：: ") + "…"
    return sentence[:COMPRESS_CHARS]


def keywords_of(text: str, limit: int = 4) -> str:
    """2–4 个关键词；不足 2 个时退回压缩句片段。"""
    value = re.sub(r"\s+", " ", str(text or ""))
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+#.-]{1,}|[\u4e00-\u9fff]{2,}", value)
    picked: list[str] = []
    for token in tokens:
        token = token.strip()
        if token in CJK_STOP or token in picked:
            continue
        if len(token) < 2:
            continue
        picked.append(token)
        if len(picked) >= max(2, min(4, limit)):
            break
    if len(picked) < 2:
        fallback = compress_text(value)
        picked = [part for part in re.split(r"[、，, ]+", fallback) if part][:4]
    return "、".join(picked[:4])


def stage_text(row: dict) -> str:
    """按阶段取可注入文本；dormant 返回空串（不注入）。"""
    stage = str(row.get("forget_stage") or "full")
    content = str(row.get("content") or "")
    full = str(row.get("content_full") or "")
    if stage == "dormant":
        return ""
    if stage == "compressed":
        return content or compress_text(full)
    if stage == "keyword":
        return content or keywords_of(full)
    return content


def _row_value(row: dict, key: str, default: Any) -> Any:
    value = row.get(key, default)
    return default if value is None else value


def decay_all(path: Path | str = DEFAULT_DB_PATH, *, now: float | None = None,
              dry_run: bool = False) -> dict:
    """按 kind/importance 半衰期衰减全部记忆；默认 dry_run=False。"""
    now = _now(now)
    con = _connect(path, write=not dry_run)
    try:
        con.row_factory = __import__("sqlite3").Row
        cols = _columns(con)
        if not dry_run:
            ensure_columns(con)
            cols = _columns(con)
        rows = [dict(row) for row in con.execute(
            "SELECT * FROM user_memories WHERE status IN ('active','dormant')").fetchall()]
        changed = []
        stage_counts: dict[str, int] = {}
        for row in rows:
            old_strength = float(_row_value(row, "strength", 1.0) or 1.0)
            last = float(_row_value(row, "last_strength_at", 0) or 0.0)
            if not last:
                last = float(row.get("created_at") or now)
            dt_days = max(0.0, (now - last) / 86400.0)
            importance = float(_row_value(row, "importance", 0.5) or 0.5)
            half = half_life_days(str(row.get("kind") or ""), importance)
            new_strength = old_strength * (0.5 ** (dt_days / half))
            new_strength = max(0.0, min(1.0, new_strength))
            stage = stage_for(new_strength)
            old_stage = str(_row_value(row, "forget_stage", "full") or "full")
            original = str(_row_value(row, "content_full", "") or row.get("content") or "")
            content = str(row.get("content") or "")
            new_content = content
            if stage == "compressed":
                new_content = compress_text(original or content)
            elif stage == "keyword":
                new_content = keywords_of(original or content)
            elif stage == "full":
                if old_stage != "full" and original:
                    new_content = original
            status = "dormant" if stage == "dormant" else "active"
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            if (abs(new_strength - old_strength) < 1e-9 and stage == old_stage
                    and new_content == content and status == str(row.get("status") or "active")):
                continue
            change = {"id": row.get("id"), "kind": row.get("kind"),
                      "old_strength": round(old_strength, 4),
                      "strength": round(new_strength, 4),
                      "old_stage": old_stage, "stage": stage,
                      "dt_days": round(dt_days, 2), "half_life_days": half,
                      "content": new_content[:80]}
            changed.append(change)
            if dry_run:
                continue
            con.execute(
                "UPDATE user_memories SET strength=?, last_strength_at=?, forget_stage=?, "
                "content=?, content_full=?, status=?, revive_count=COALESCE(revive_count,0) "
                "WHERE id=?",
                (new_strength, now, stage, new_content,
                 original if original else content, status, row.get("id")))
        if not dry_run:
            con.commit()
        return {"ok": True, "dry_run": bool(dry_run), "checked": len(rows),
                "changed": len(changed), "stages": stage_counts,
                "samples": changed[:10], "ts": now}
    finally:
        con.close()


def revive(path: Path | str = DEFAULT_DB_PATH, memory_id: str = "",
           *, boost: float = REVIVE_STRENGTH, now: float | None = None) -> dict:
    """命中/被提起：强度≥boost、恢复原文/压缩态、revive_count+1。"""
    now = _now(now)
    if not str(memory_id or "").strip():
        return {"ok": False, "error": "memory_id 不能为空"}
    con = db.connect(path)
    try:
        ensure_columns(con)
        row = con.execute("SELECT * FROM user_memories WHERE id=?", (str(memory_id),)).fetchone()
        if row is None:
            return {"ok": False, "error": "记忆不存在"}
        row = dict(row)
        old_strength = float(_row_value(row, "strength", 1.0) or 1.0)
        strength = max(old_strength, float(boost))
        original = str(_row_value(row, "content_full", "") or row.get("content") or "")
        content = str(row.get("content") or "")
        stage = "full" if strength >= 0.5 else "compressed"
        new_content = original if original else content
        if stage == "compressed" and not original:
            new_content = compress_text(content)
        con.execute(
            "UPDATE user_memories SET strength=?, last_strength_at=?, forget_stage=?, "
            "content=?, content_full=?, status='active', "
            "revive_count=COALESCE(revive_count,0)+1, last_accessed=?, "
            "access_count=COALESCE(access_count,0)+1 WHERE id=?",
            (strength, now, stage, new_content, original or new_content, now, memory_id))
        con.commit()
        return {"ok": True, "id": memory_id, "strength": strength, "stage": stage,
                "content": new_content, "revived": True}
    finally:
        con.close()


def search_with_revive(path: Path | str = DEFAULT_DB_PATH, query: str = "",
                       *, limit: int = 8, now: float | None = None) -> list[dict]:
    """检索命中即续命；返回 hits（memory hit 会带 revived/strength/stage）。"""
    hits: list[dict] = []
    try:
        # 优先用带来源权重/MMR 的 hybrid 检索；空/失败再退回 search
        from . import retrieval
        hits = list(retrieval.hybrid(path, query, k=limit))
    except Exception:  # noqa: BLE001
        hits = []
    if not hits:
        try:
            from . import search
            hits = list(search.search(path, query, limit=limit))
        except Exception:  # noqa: BLE001
            hits = []
    if not hits:
        con = _connect(path, write=False)
        try:
            cols = _columns(con)
            like = f"%{str(query or '').strip()}%"
            rows = [dict(row) for row in con.execute(
                "SELECT id,content,kind FROM user_memories "
                "WHERE status IN ('active','dormant') AND content LIKE ? LIMIT ?",
                (like, max(1, int(limit)))).fetchall()]
            hits = [{"source_type": "memory", "source_id": str(row["id"]),
                     "text": str(row.get("content") or ""), "score": 0.0,
                     "kind": row.get("kind")} for row in rows]
        except Exception:  # noqa: BLE001
            hits = []
        finally:
            con.close()
    for hit in hits:
        if str(hit.get("source_type") or "") != "memory":
            continue
        memory_id = str(hit.get("source_id") or "")
        if not memory_id:
            continue
        try:
            restored = revive(path, memory_id, now=now)
            if restored.get("ok"):
                hit.update({"text": restored.get("content") or hit.get("text"),
                            "revived": True, "strength": restored.get("strength"),
                            "stage": restored.get("stage")})
        except Exception:  # noqa: BLE001
            hit["revive_error"] = True
    return hits


def purge_dormant(path: Path | str = DEFAULT_DB_PATH, *, days: int = DORMANT_PURGE_DAYS,
                  now: float | None = None, dry_run: bool = True) -> dict:
    """清理 >200 天的 dormant；默认 dry-run（不物理删）。"""
    now = _now(now)
    cutoff = now - max(0, int(days)) * 86400
    con = _connect(path, write=not dry_run)
    try:
        cols = _columns(con)
        if "forget_stage" not in cols:
            return {"ok": True, "dry_run": bool(dry_run), "count": 0, "ids": []}
        rows = [dict(row) for row in con.execute(
            "SELECT id,last_strength_at,created_at,forget_stage FROM user_memories "
            "WHERE forget_stage='dormant'").fetchall()]
        targets = []
        for row in rows:
            last = float(row.get("last_strength_at") or row.get("created_at") or 0)
            if last and last < cutoff:
                targets.append(str(row.get("id")))
        if not dry_run and targets:
            marks = ",".join("?" for _ in targets)
            con.execute(f"DELETE FROM user_memories WHERE id IN ({marks})", targets)
            con.commit()
            try:
                from . import search
                for memory_id in targets:
                    search.delete(path, "memory", memory_id)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "dry_run": bool(dry_run), "count": len(targets),
                "ids": targets[:50], "cutoff_ts": cutoff}
    finally:
        con.close()


def strength_report(path: Path | str = DEFAULT_DB_PATH, limit: int = 50) -> dict:
    """只读：按 stage 汇总 + 最强/最弱记忆（GET /api/memory/strength 用）。"""
    con = _connect(path, write=False)
    try:
        cols = _columns(con)
        rows = [dict(row) for row in con.execute(
            "SELECT * FROM user_memories WHERE status IN ('active','dormant')").fetchall()]
    finally:
        con.close()
    stages: dict[str, int] = {}
    items = []
    for row in rows:
        stage = str(row.get("forget_stage") or "full")
        stages[stage] = stages.get(stage, 0) + 1
        strength = float(row.get("strength", 1.0) or 1.0)
        items.append({"id": row.get("id"), "kind": row.get("kind"),
                      "content": str(row.get("content") or "")[:100],
                      "strength": round(strength, 4), "stage": stage,
                      "revive_count": int(row.get("revive_count") or 0)})
    items.sort(key=lambda item: item["strength"], reverse=True)
    return {"ok": True, "count": len(rows), "stages": stages,
            "strongest": items[:5], "weakest": items[-5:],
            "items": items[:max(1, int(limit))]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="记忆衰减/唤醒/休眠清理（P38）")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--write", action="store_true", help="真正写衰减结果")
    parser.add_argument("--purge", action="store_true")
    parser.add_argument("--purge-write", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--revive", metavar="ID", default="")
    parser.add_argument("--now", type=float, default=None)
    args = parser.parse_args(argv)
    if args.status or args.report:
        result = strength_report(args.db)
    elif args.revive:
        result = revive(args.db, args.revive, now=args.now)
    elif args.purge:
        result = purge_dormant(args.db, now=args.now,
                               dry_run=not args.purge_write)
    else:
        result = decay_all(args.db, now=args.now, dry_run=not args.write)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
