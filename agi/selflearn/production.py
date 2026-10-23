# -*- coding: utf-8 -*-
"""生产级验收：真实运行数据的长期指标门槛。

不是“再跑一次单测”，而是检查活动库在消息量、记忆证据、反馈、人格稳定、
claim/检索、错误/主动行为上的可重复指标；不足时明确 not_ready。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

# 生产 pilot 的最低门槛；7 天真实运行后应更高。
MIN_MESSAGES = 30
MIN_TURNS = 10
MIN_FEEDBACK = 3
MIN_MEMORIES = 5
MIN_EVIDENCE_RATIO = 0.80
MIN_PERSONA = 3
MIN_CLAIMS = 5
MAX_OPEN_CONFLICTS = 5
MAX_ERROR_RATE = 0.25
MIN_ACTIVE_DAYS = 3
MIN_FEEDBACK_DAYS = 2


def _table_rows(con, sql: str, args: tuple = ()) -> list[dict]:
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    except Exception:  # noqa: BLE001
        return []


def _count(con, table: str) -> int:
    try:
        return int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    except Exception:  # noqa: BLE001
        return 0


def snapshot(path: Path | str = DB_PATH, *, days: int = 7) -> dict[str, Any]:
    """汇总活动库的生产指标；坏表/空库不崩。"""
    con = db.connect(path)
    try:
        now = time.time()
        since = now - max(1, int(days)) * 86400
        messages = _count(con, "messages")
        user_messages = _count_by(con, "messages", "role", "user")
        turns = _count(con, "turns")
        feedback = _count(con, "feedback")
        try:
            explicit_feedback = int(con.execute(
                "SELECT count(*) FROM feedback WHERE note NOT LIKE 'implicit:%'").fetchone()[0])
        except Exception:  # noqa: BLE001
            explicit_feedback = feedback
        memories = _count(con, "user_memories")
        evidence_rows = _table_rows(
            con, "SELECT count(*) n FROM user_memories WHERE evidence IS NOT NULL AND trim(evidence)<>''")
        evidence = int(evidence_rows[0]["n"]) if evidence_rows else 0
        persona = _count(con, "persona") if _table_exists(con, "persona") else 0
        claims = _count(con, "claims")
        claim_evidence = 0
        if _table_exists(con, "claims"):
            rows = _table_rows(con, "SELECT count(*) n FROM claims WHERE trim(evidence_span)<>''")
            claim_evidence = int(rows[0]["n"]) if rows else 0
        conflicts = 0
        if _table_exists(con, "conflicts"):
            rows = _table_rows(con, "SELECT count(*) n FROM conflicts WHERE status='open'")
            conflicts = int(rows[0]["n"]) if rows else 0
        errors = _count(con, "errors")
        proactive = _count(con, "proactive")
        variants = _count(con, "variants")
        recent = _table_rows(
            con, "SELECT count(*) n FROM messages WHERE ts>=?", (since,))
        recent_messages = int(recent[0]["n"]) if recent else 0
        days_active = _table_rows(
            con, "SELECT count(DISTINCT date(ts,'unixepoch','localtime')) n FROM messages")
        active_days = int(days_active[0]["n"]) if days_active else 0
        try:
            feedback_days = int(con.execute(
                "SELECT count(DISTINCT date(created_at,'unixepoch','localtime')) "
                "FROM feedback").fetchone()[0])
        except Exception:  # noqa: BLE001
            feedback_days = 0
        try:
            days_with_messages = [dict(r) for r in con.execute(
                "SELECT date(ts,'unixepoch','localtime') day, count(*) n FROM messages "
                "GROUP BY day ORDER BY day DESC LIMIT ?", (max(1, int(days)),))]
        except Exception:  # noqa: BLE001
            days_with_messages = []
    finally:
        con.close()

    evidence_ratio = evidence / memories if memories else 0.0
    error_rate = errors / max(1, turns)
    checks = {
        "messages": {"ok": messages >= MIN_MESSAGES, "value": messages, "min": MIN_MESSAGES},
        "turns": {"ok": turns >= MIN_TURNS, "value": turns, "min": MIN_TURNS},
        "feedback": {"ok": feedback >= MIN_FEEDBACK, "value": feedback, "min": MIN_FEEDBACK},
        "explicit_feedback": {"ok": explicit_feedback >= MIN_FEEDBACK,
                              "value": explicit_feedback, "min": MIN_FEEDBACK},
        "memories": {"ok": memories >= MIN_MEMORIES, "value": memories, "min": MIN_MEMORIES},
        "memory_evidence": {"ok": evidence_ratio >= MIN_EVIDENCE_RATIO,
                            "value": round(evidence_ratio, 3), "min": MIN_EVIDENCE_RATIO},
        "persona": {"ok": persona >= MIN_PERSONA, "value": persona, "min": MIN_PERSONA},
        "claims": {"ok": claims >= MIN_CLAIMS, "value": claims, "min": MIN_CLAIMS},
        "claim_evidence": {"ok": claim_evidence == 0 or claim_evidence == claims,
                           "value": claim_evidence, "min": claims},
        "conflicts": {"ok": conflicts <= MAX_OPEN_CONFLICTS,
                      "value": conflicts, "max": MAX_OPEN_CONFLICTS},
        "errors": {"ok": error_rate <= MAX_ERROR_RATE,
                   "value": round(error_rate, 3), "max": MAX_ERROR_RATE},
        "active_days": {"ok": active_days >= MIN_ACTIVE_DAYS, "value": active_days,
                        "min": MIN_ACTIVE_DAYS},
        "feedback_days": {"ok": feedback_days >= MIN_FEEDBACK_DAYS, "value": feedback_days,
                          "min": MIN_FEEDBACK_DAYS},
    }
    passed = sum(1 for item in checks.values() if item["ok"])
    total = len(checks)
    verdict = "pilot_ready" if passed == total else ("not_ready" if passed < total * 0.7 else "needs_work")
    return {
        "ok": True,
        "db": str(path),
        "generated_at": time.time(),
        "window_days": int(days),
        "active_days": active_days,
        "feedback_days": feedback_days,
        "days_with_messages": days_with_messages,
        "recent_messages": recent_messages,
        "raw": {"messages": messages, "user_messages": user_messages, "turns": turns,
                "feedback": feedback, "explicit_feedback": explicit_feedback, "memories": memories, "persona": persona,
                "claims": claims, "conflicts": conflicts, "errors": errors,
                "proactive": proactive, "variants": variants},
        "checks": checks,
        "passed": passed,
        "total": total,
        "score": round(passed * 100 / total, 1),
        "verdict": verdict,
        "note": "只证明本地数据达到 pilot 门槛；explicit_feedback 必须是用户主动评价，隐式反馈不计入；7 天真实留存/编造率仍需真实运行。",
    }


def _count_by(con, table: str, col: str, value: str) -> int:
    try:
        return int(con.execute(f"SELECT count(*) FROM {table} WHERE {col}=?", (value,)).fetchone()[0])
    except Exception:  # noqa: BLE001
        return 0


def _table_exists(con, name: str) -> bool:
    try:
        return bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())
    except Exception:  # noqa: BLE001
        return False


def markdown(path: Path | str = DB_PATH, *, days: int = 7) -> str:
    s = snapshot(path, days=days)
    lines = ["# 生产级验收", "",
             f"- 库：`{s['db']}`",
             f"- 结论：**{s['verdict']}**（{s['passed']}/{s['total']}，score {s['score']}）",
             f"- 活跃天数：{s['active_days']}；窗口消息：{s['recent_messages']}", "",
             "| 指标 | 值 | 门槛 | 结果 |", "|---|---:|---:|---|"]
    for key, item in s["checks"].items():
        threshold = item.get("min", item.get("max"))
        lines.append(f"| {key} | {item['value']} | {threshold} | {'PASS' if item['ok'] else 'FAIL'} |")
    lines += ["", s["note"]]
    return "\n".join(lines) + "\n"
