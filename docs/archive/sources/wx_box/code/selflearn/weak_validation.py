# -*- coding: utf-8 -*-
"""13 弱验证复测：跨时间复查、失败标记回退，但不直接改技能等级。

Batch 5 边界：

- 每次复测只写 ``weak_rechecks``；
- 复测计划与“需要回退”标记放在通用 ``state`` 表，不新增表、不改 ``db.py``；
- 本模块不调用 ``skills.record_weak_check`` / ``skills.evaluate_skill``，
  也不改 ``skills.level``、``variants.status``、``weak_checks``；
- ``status()`` 只汇总证据，是否降级由上层/人工决定。

这样可以区分两件事：
“看到一次失败” 与 “立刻把技能等级改掉”。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

#: 默认复测间隔：三天后再看一次，避免一条反馈就下结论
RECHECK_INTERVAL_SECONDS = 3 * 24 * 3600
#: state 表中的键：{skill_id: schedule}
SCHEDULE_KEY = "weak_recheck_schedules"
#: state 表中的键：{skill_id: rollback_flag}
ROLLBACK_KEY = "weak_recheck_rollback"
NOTE_LIMIT = 300


def _id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return prefix + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14]


def _clock(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {
            "1", "true", "yes", "y", "ok", "pass", "passed", "good",
        }
    return bool(value)


def _clean_note(note: Any) -> str:
    return " ".join(str(note or "").split())[:NOTE_LIMIT]


def _load_map(con, key: str) -> dict:
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_map(con, key: str, data: dict, now: float) -> None:
    con.execute(
        "INSERT OR REPLACE INTO state(key,value,updated_at) VALUES(?,?,?)",
        (key, json.dumps(data, ensure_ascii=False), now),
    )


def _level(con, skill_id: str) -> int:
    row = con.execute("SELECT level FROM skills WHERE id=?", (skill_id,)).fetchone()
    try:
        return int(row["level"] or 0) if row else 0
    except (TypeError, ValueError):
        return 0


def _checks(con, skill_id: str) -> tuple[int, int]:
    row = con.execute(
        "SELECT count(*) n, coalesce(sum(passed),0) ok FROM weak_checks WHERE skill_id=?",
        (skill_id,),
    ).fetchone()
    return int(row["n"] or 0), int(row["ok"] or 0)


def _rechecks(con, skill_id: str) -> tuple[int, int]:
    row = con.execute(
        "SELECT count(*) n, coalesce(sum(passed),0) ok "
        "FROM weak_rechecks WHERE skill_id=?",
        (skill_id,),
    ).fetchone()
    return int(row["n"] or 0), int(row["ok"] or 0)


def _pick_variant(con, skill_id: str) -> str | None:
    """优先复测已有样本最多的变体；没有样本时取仍在灰度/启用的变体。"""
    row = con.execute(
        "SELECT variant_id, count(*) n, max(created_at) last FROM weak_checks "
        "WHERE skill_id=? AND variant_id IS NOT NULL AND variant_id<>'' "
        "GROUP BY variant_id ORDER BY n DESC, last DESC, variant_id LIMIT 1",
        (skill_id,),
    ).fetchone()
    if row and row["variant_id"]:
        return str(row["variant_id"])
    row = con.execute(
        "SELECT id FROM variants WHERE skill_id=? AND status IN ('active','gray') "
        "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id LIMIT 1",
        (skill_id,),
    ).fetchone()
    return str(row["id"]) if row else None


def _consecutive_failures(con, skill_id: str) -> int:
    rows = con.execute(
        "SELECT passed FROM weak_rechecks WHERE skill_id=? "
        "ORDER BY created_at DESC, rowid DESC",
        (skill_id,),
    ).fetchall()
    count = 0
    for row in rows:
        if int(row["passed"] or 0):
            break
        count += 1
    return count


def _latest_recheck(con, skill_id: str):
    return con.execute(
        "SELECT passed, note, created_at FROM weak_rechecks WHERE skill_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (skill_id,),
    ).fetchone()


def _public_schedule(schedule: dict | None, now: float) -> dict | None:
    if not isinstance(schedule, dict):
        return None
    out = dict(schedule)
    try:
        due_at = float(out.get("due_at"))
    except (TypeError, ValueError):
        due_at = 0.0
    pending = str(out.get("status") or "") == "pending"
    out["due"] = bool(pending and due_at <= now)
    out["overdue_seconds"] = round(max(0.0, now - due_at), 3) if out["due"] else 0.0
    return out


def schedule_recheck(path: Path | str, skill_id: str, reason: str = "",
                     *, now: float | None = None) -> dict[str, Any]:
    """排一次跨时间复测；同一个技能已有 pending 计划时不重复排队。"""
    skill_id = str(skill_id or "").strip()
    if not skill_id:
        return {"ok": False, "error": "skill_id 不能为空"}
    now = _clock(now)
    reason = _clean_note(reason)

    con = db.connect(path)
    try:
        schedules = _load_map(con, SCHEDULE_KEY)
        existing = schedules.get(skill_id)
        level = _level(con, skill_id)
        checks, _ = _checks(con, skill_id)
        if isinstance(existing, dict) and existing.get("status") == "pending":
            pub = _public_schedule(existing, now)
            return {
                "ok": True,
                "skill_id": skill_id,
                "created": False,
                "already_scheduled": True,
                "schedule_id": existing.get("id"),
                "schedule": pub,
                "due_at": existing.get("due_at"),
                "due": bool(pub and pub["due"]),
                "reason": existing.get("reason", ""),
                "level": level,
                "checks": checks,
                "note": "已有复测计划，不重复排队",
            }

        interval = float(RECHECK_INTERVAL_SECONDS)
        schedule = {
            "id": _id("WRS-", skill_id, now, reason),
            "skill_id": skill_id,
            "status": "pending",
            "reason": reason,
            "created_at": now,
            "due_at": now + interval,
            "interval_seconds": interval,
            "level_at_schedule": level,
            "checks_at_schedule": checks,
        }
        schedules[skill_id] = schedule
        _save_map(con, SCHEDULE_KEY, schedules, now)
        con.commit()
        pub = _public_schedule(schedule, now)
    finally:
        con.close()

    return {
        "ok": True,
        "skill_id": skill_id,
        "created": True,
        "already_scheduled": False,
        "schedule_id": schedule["id"],
        "schedule": pub,
        "due_at": schedule["due_at"],
        "due": False,
        "reason": reason,
        "level": level,
        "checks": checks,
        "interval_seconds": interval,
        "note": "已排定复测；本模块只记录证据，不直接改 level",
    }


def run_recheck(path: Path | str, skill_id: str, passed: bool, note: str = "",
                *, variant_id: str | None = None,
                now: float | None = None) -> dict[str, Any]:
    """记录一次复测结果。

    失败只打“需要回退”的标记；``skills.level`` 在前后保持完全一致。
    """
    skill_id = str(skill_id or "").strip()
    if not skill_id:
        return {"ok": False, "error": "skill_id 不能为空"}
    now = _clock(now)
    passed = _as_bool(passed)
    note = _clean_note(note)

    con = db.connect(path)
    try:
        level_before = _level(con, skill_id)
        variant = (str(variant_id).strip() if variant_id else "") or _pick_variant(con, skill_id)

        schedules = _load_map(con, SCHEDULE_KEY)
        latest = schedules.get(skill_id)
        scheduled = isinstance(latest, dict) and latest.get("status") == "pending"
        schedule_reason = str(latest.get("reason") or "") if scheduled else ""

        recheck_id = _id("WR-", skill_id, variant, passed, note, now, time.time_ns())
        con.execute(
            "INSERT INTO weak_rechecks(id,skill_id,variant_id,passed,note,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (recheck_id, skill_id, variant, 1 if passed else 0, note, now),
        )

        schedule_out = None
        if scheduled:
            done = dict(latest)
            done.update({
                "status": "done",
                "completed_at": now,
                "recheck_id": recheck_id,
                "passed": passed,
                "note": note,
            })
            schedules[skill_id] = done
            _save_map(con, SCHEDULE_KEY, schedules, now)
            schedule_out = _public_schedule(done, now)

        failures = _consecutive_failures(con, skill_id)
        flags = _load_map(con, ROLLBACK_KEY)
        rollback = None
        rollback_cleared = False
        if passed:
            if skill_id in flags:
                flags.pop(skill_id, None)
                _save_map(con, ROLLBACK_KEY, flags, now)
                rollback_cleared = True
        else:
            rollback = {
                "skill_id": skill_id,
                "needed": True,
                "at": now,
                "recheck_id": recheck_id,
                "note": note,
                "level": level_before,
                "consecutive_failures": failures,
                "reason": schedule_reason,
            }
            flags[skill_id] = rollback
            _save_map(con, ROLLBACK_KEY, flags, now)

        con.commit()
        level_after = _level(con, skill_id)
        total, ok = _rechecks(con, skill_id)
        checks, checks_ok = _checks(con, skill_id)
    finally:
        con.close()

    return {
        "ok": True,
        "recheck_id": recheck_id,
        "skill_id": skill_id,
        "variant_id": variant,
        "passed": passed,
        "note": note,
        "created_at": now,
        "level_before": level_before,
        "level_after": level_after,
        "level_changed": level_after != level_before,
        "needs_rollback": not passed,
        "rollback": rollback,
        "rollback_cleared": rollback_cleared,
        "consecutive_failures": failures,
        "checks": checks,
        "checks_passed": checks_ok,
        "rechecks": total,
        "rechecks_passed": ok,
        "pass_rate": round(ok / total, 3) if total else 0.0,
        "scheduled": scheduled,
        "schedule": schedule_out,
        "note_for_review": (
            "复测失败：已标记需要回退，但未直接改 level"
            if not passed
            else ("复测通过：已清掉该技能的回退标记" if rollback_cleared
                  else "复测通过：记录证据，未改 level")
        ),
    }


def status(path: Path | str = DB_PATH, *, now: float | None = None) -> dict[str, Any]:
    """汇总复测证据、到期计划与需要回退的技能。"""
    now = _clock(now)
    con = db.connect(path)
    try:
        skill_levels = {
            str(row["id"]): int(row["level"] or 0)
            for row in con.execute("SELECT id, level FROM skills")
        }
        recheck_ids = {
            str(row["skill_id"])
            for row in con.execute("SELECT DISTINCT skill_id FROM weak_rechecks")
        }
        schedules = _load_map(con, SCHEDULE_KEY)
        flags = _load_map(con, ROLLBACK_KEY)
        ids = sorted(set(skill_levels) | recheck_ids | set(schedules) | set(flags))

        items: list[dict[str, Any]] = []
        for skill_id in ids:
            recheck_n, recheck_ok = _rechecks(con, skill_id)
            check_n, check_ok = _checks(con, skill_id)
            last = _latest_recheck(con, skill_id)
            schedule = _public_schedule(schedules.get(skill_id), now)
            flag = flags.get(skill_id) if isinstance(flags.get(skill_id), dict) else None
            needs_rollback = bool(flag)
            due = bool(schedule and schedule["due"])
            if needs_rollback:
                action = "rollback_review"
            elif due:
                action = "recheck"
            elif schedule and schedule.get("status") == "pending":
                action = "wait"
            elif recheck_n:
                action = "observe"
            else:
                action = "idle"
            items.append({
                "skill_id": skill_id,
                "level": skill_levels.get(skill_id, 0),
                "checks": check_n,
                "checks_passed": check_ok,
                "rechecks": recheck_n,
                "rechecks_passed": recheck_ok,
                "pass_rate": round(recheck_ok / recheck_n, 3) if recheck_n else 0.0,
                "last_recheck_at": float(last["created_at"]) if last else None,
                "last_passed": bool(last["passed"]) if last else None,
                "last_note": str(last["note"] or "") if last else "",
                "consecutive_failures": _consecutive_failures(con, skill_id),
                "needs_rollback": needs_rollback,
                "rollback": flag,
                "schedule": schedule,
                "due": due,
                "recommended_action": action,
            })

        total_rechecks = int(
            con.execute("SELECT count(*) FROM weak_rechecks").fetchone()[0] or 0)
        last_recheck_at = con.execute(
            "SELECT max(created_at) FROM weak_rechecks").fetchone()[0]
    finally:
        con.close()

    schedules_out = [
        item["schedule"] for item in items
        if item["schedule"] and item["schedule"].get("status") == "pending"
    ]
    due_out = [item["schedule"] for item in items if item["due"]]
    rollback_out = [item for item in items if item["needs_rollback"]]

    return {
        "ok": True,
        "now": now,
        "skills": items,
        "schedules": schedules_out,
        "scheduled": len(schedules_out),
        "due": due_out,
        "due_count": len(due_out),
        "rollback_needed": rollback_out,
        "needs_rollback": bool(rollback_out),
        "counts": {
            "skills": len(items),
            "rechecks": total_rechecks,
            "scheduled": len(schedules_out),
            "due": len(due_out),
            "rollback": len(rollback_out),
            "weak_checks": sum(item["checks"] for item in items),
        },
        "total_rechecks": total_rechecks,
        "last_recheck_at": float(last_recheck_at) if last_recheck_at else None,
        "note": "这里只汇总证据；降级需上层决定，本模块不改 level",
    }
