# -*- coding: utf-8 -*-
"""14 按需唤醒：预算、优先级、去重、``wake_runs`` 审计，不自动发微信。

设计边界（Batch 5）：

- 复用已有 ``intents`` 队列表；早期表没有 ``priority`` 列，本模块按需补列，
  不改 ``db.py`` / ``scheduler.py``；
- ``due()`` 只查询，不改状态、不发送；
- ``run_due()`` 只做三件事：按预算选一批、写 ``wake_runs`` 审计、把意图标成
  ``done`` / ``dropped``。它**没有**任何微信/网络调用；
- 去重键是 ``(kind, 归一化后的 text)``：同一条待办不会排队两次；已有更早的
  唤醒时间或更高优先级会合并进去；
- 预算分两层：单次 ``budget`` 与 ``DAILY_WAKE_BUDGET``（按本地自然日统计
  ``wake_runs``）。预算耗尽的意图保持 pending，留给下一轮。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

#: 显式声明：本模块不发送任何消息，唤醒只产生审计与队列状态
AUTO_SEND = False
#: 每个本地自然日最多真正唤醒处理的条数
DAILY_WAKE_BUDGET = 20
#: 单次查询上限，防止异常大的待办队列拖慢 tick
QUERY_LIMIT = 200
REASON_LIMIT = 300


def _id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return prefix + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14]


def _clock(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _budget(value: Any) -> int:
    if value is None:
        return 5
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 5


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split())


def _priority(row) -> float:
    try:
        value = float(row["priority"])
    except (KeyError, TypeError, ValueError):
        return 0.5
    if not math.isfinite(value):
        return 0.5
    return max(0.0, min(1.0, value))


def _ensure_queue(con) -> None:
    """给早期 intents 表补 priority 列；幂等，容忍并发补列。"""
    cols = {row[1] for row in con.execute("PRAGMA table_info(intents)").fetchall()}
    if "priority" in cols:
        return
    try:
        con.execute(
            "ALTER TABLE intents ADD COLUMN priority REAL NOT NULL DEFAULT 0.5")
        con.commit()
    except sqlite3.OperationalError:
        # 另一个 tick 抢先补了列；确认它真的存在，否则把异常抛出去
        cols = {row[1] for row in con.execute("PRAGMA table_info(intents)").fetchall()}
        if "priority" not in cols:
            raise


def _day_window(now: float) -> tuple[float, float]:
    """本地自然日窗口；不用 mktime，避免 Windows 上 1970 附近溢出。"""
    lt = time.localtime(now)
    offset = getattr(lt, "tm_gmtoff", None)
    if offset is None:
        offset = -time.timezone
    start = now - ((now + offset) % (24 * 3600))
    return start, start + 24 * 3600


def _daily_usage(con, now: float) -> dict[str, Any]:
    start, end = _day_window(now)
    used = int(con.execute(
        "SELECT count(*) FROM wake_runs WHERE started_at>=? AND started_at<?",
        (start, end),
    ).fetchone()[0] or 0)
    return {
        "budget": int(DAILY_WAKE_BUDGET),
        "used": used,
        "remaining": max(0, int(DAILY_WAKE_BUDGET) - used),
        "day_start": start,
        "day_end": end,
    }


def _collect(con, now: float) -> dict[str, Any]:
    """取出到期 pending，并按 (kind,text) 去重，保留优先级最高的那条。"""
    rows = con.execute(
        "SELECT id,kind,text,wake_at,status,reason,created_at,"
        "coalesce(priority,0.5) AS priority FROM intents "
        "WHERE status='pending' AND wake_at<=? "
        "ORDER BY coalesce(priority,0.5) DESC, wake_at ASC, created_at ASC, id ASC "
        "LIMIT ?",
        (now, QUERY_LIMIT),
    ).fetchall()
    pending_total = int(con.execute(
        "SELECT count(*) FROM intents WHERE status='pending'").fetchone()[0] or 0)

    items: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        item["priority"] = _priority(row)
        item["wake_at"] = float(item.get("wake_at") or 0.0)
        item["late_seconds"] = round(max(0.0, now - item["wake_at"]), 3)
        item["merged_ids"] = []
        key = (str(item.get("kind") or ""), _norm(item.get("text")))
        keep = seen.get(key)
        if keep is not None:
            keep["merged_ids"].append(str(item["id"]))
            duplicates.append({
                "id": str(item["id"]),
                "kind": item.get("kind"),
                "text": item.get("text"),
                "wake_at": item["wake_at"],
                "priority": item["priority"],
                "merged_into": str(keep["id"]),
            })
            continue
        seen[key] = item
        items.append(item)

    return {
        "items": items,
        "duplicates": duplicates,
        "due_rows": len(rows),
        "pending_total": pending_total,
        "truncated": len(rows) >= QUERY_LIMIT,
        "daily": _daily_usage(con, now),
    }


def add(path: Path | str, kind: str, text: str, wake_at: float,
        priority: float = 0.5, reason: str = "") -> str:
    """排一条唤醒意图；返回意图 id。

    同一 ``(kind, text)`` 已有 pending 时复用旧 id，并合并：
    取更早的 ``wake_at``、更高的 ``priority``，reason 保留原来的非空值。
    """
    kind = str(kind or "").strip().lower()
    text = str(text or "").strip()
    if not kind:
        raise ValueError("kind 不能为空")
    if not text:
        raise ValueError("text 不能为空")
    try:
        wake_at = float(wake_at)
    except (TypeError, ValueError):
        raise ValueError("wake_at 必须是时间戳") from None
    if not math.isfinite(wake_at):
        raise ValueError("wake_at 必须是有限时间戳")
    try:
        priority = float(priority)
    except (TypeError, ValueError):
        raise ValueError("priority 必须是 0..1 的数") from None
    if not math.isfinite(priority):
        raise ValueError("priority 必须是有限数")
    priority = max(0.0, min(1.0, priority))
    reason = " ".join(str(reason or "").split())[:REASON_LIMIT]
    norm = _norm(text)

    con = db.connect(path)
    try:
        _ensure_queue(con)
        pending = con.execute(
            "SELECT * FROM intents WHERE status='pending' AND kind=?",
            (kind,),
        ).fetchall()
        for row in pending:
            if _norm(row["text"]) != norm:
                continue
            old_wake = float(row["wake_at"] or 0.0)
            new_wake = min(old_wake, wake_at)
            old_priority = _priority(row)
            new_priority = max(old_priority, priority)
            old_reason = str(row["reason"] or "")
            new_reason = old_reason or reason
            con.execute(
                "UPDATE intents SET wake_at=?, priority=?, reason=? WHERE id=?",
                (new_wake, new_priority, new_reason, row["id"]),
            )
            con.commit()
            return str(row["id"])

        intent_id = _id("WK-", kind, text, wake_at, reason, time.time_ns())
        con.execute(
            "INSERT INTO intents(id,kind,text,wake_at,status,reason,priority,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (intent_id, kind, text, wake_at, "pending", reason, priority, time.time()),
        )
        con.commit()
        return intent_id
    finally:
        con.close()


def due(path: Path | str = DB_PATH, now: float | None = None,
        budget: int = 5) -> dict[str, Any]:
    """查看到期意图：按优先级/时间排序，最多返回 ``budget`` 条。"""
    now = _clock(now)
    budget = _budget(budget)
    con = db.connect(path)
    try:
        _ensure_queue(con)
        data = _collect(con, now)
    finally:
        con.close()

    items = data["items"]
    selected = items[:budget]
    deferred = items[budget:]
    daily = data["daily"]
    runnable = min(len(selected), daily["remaining"])
    return {
        "ok": True,
        "now": now,
        "budget": budget,
        "items": selected,
        "intents": selected,
        "selected_count": len(selected),
        "runnable": runnable,
        "deferred": deferred,
        "duplicates": data["duplicates"],
        "total_due": len(items),
        "total_pending": data["pending_total"],
        "due_rows": data["due_rows"],
        "truncated": data["truncated"],
        "daily": {**daily, "runnable": runnable},
        "counts": {
            "pending": data["pending_total"],
            "due": len(items),
            "selected": len(selected),
            "deferred": len(deferred),
            "duplicates": len(data["duplicates"]),
            "runnable": runnable,
        },
        "auto_send": AUTO_SEND,
        "note": "due 只查询，不改状态、不发送消息",
    }


def run_due(path: Path | str = DB_PATH, now: float | None = None,
            budget: int = 5) -> dict[str, Any]:
    """处理一批到期意图：写 wake_runs 审计、标记完成，但不发送。"""
    now = _clock(now)
    budget = _budget(budget)
    con = db.connect(path)
    try:
        _ensure_queue(con)
        data = _collect(con, now)

        items = data["items"]
        daily = data["daily"]
        capacity = min(budget, int(daily["remaining"]))
        allowed = items[:capacity]
        budget_blocked = items[capacity:budget] if capacity < budget else []
        deferred = items[budget:]

        merged: dict[str, list[str]] = {}
        for duplicate in data["duplicates"]:
            merged.setdefault(duplicate["merged_into"], []).append(duplicate["id"])

        runs: list[dict[str, Any]] = []
        processed: list[dict[str, Any]] = []
        dropped: list[str] = []
        for index, item in enumerate(allowed):
            intent_id = str(item["id"])
            merged_ids = merged.get(intent_id, [])

            detail = {
                "intent_id": intent_id,
                "kind": item.get("kind"),
                "text": item.get("text"),
                "reason": item.get("reason"),
                "priority": item["priority"],
                "wake_at": item["wake_at"],
                "late_seconds": item["late_seconds"],
                "merged_intent_ids": merged_ids,
                "action": "wake_processed",
                "auto_send": AUTO_SEND,
            }
            run_id = _id("RUN-", intent_id, now, index, time.time_ns())
            cur = con.execute(
                "UPDATE intents SET status='done' WHERE id=? AND status='pending'",
                (intent_id,),
            )
            if cur.rowcount == 0:
                # 同一轮被别处处理掉了：不写假审计
                continue
            for duplicate_id in merged_ids:
                done = con.execute(
                    "UPDATE intents SET status='dropped' "
                    "WHERE id=? AND status='pending'",
                    (duplicate_id,),
                )
                if done.rowcount:
                    dropped.append(duplicate_id)
            con.execute(
                "INSERT INTO wake_runs(id,kind,status,detail_json,started_at,finished_at) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, item.get("kind") or "unknown", "done",
                 json.dumps(detail, ensure_ascii=False), now, now),
            )
            runs.append({
                "id": run_id,
                "run_id": run_id,
                "intent_id": intent_id,
                "kind": item.get("kind"),
                "status": "done",
                "detail": detail,
                "started_at": now,
                "finished_at": now,
            })
            processed.append({**item, "status": "done"})

        con.commit()
    finally:
        con.close()

    daily_after = {
        **daily,
        "used_after": int(daily["used"]) + len(runs),
        "remaining_after": max(0, int(daily["remaining"]) - len(runs)),
    }
    return {
        "ok": True,
        "now": now,
        "budget": budget,
        "processed": len(runs),
        "count": len(runs),
        "runs": runs,
        "intents": processed,
        "ids": [run["intent_id"] for run in runs],
        "deferred": deferred,
        "budget_blocked": budget_blocked,
        "dropped_duplicates": dropped,
        "total_due": len(items),
        "daily": daily_after,
        "sent": False,
        "auto_send": AUTO_SEND,
        "counts": {
            "due": len(items),
            "processed": len(runs),
            "deferred": len(deferred),
            "budget_blocked": len(budget_blocked),
            "dropped_duplicates": len(dropped),
        },
        "note": "唤醒只写审计并把意图标为 done/dropped，不自动发微信",
    }
