# -*- coding: utf-8 -*-
"""07 三次法则：case → rule → guard。

流水线：
1. ``record_case``：每次错误先落 ``errors``（case）。同一轮里同 category + detail 的
   重复上报复用同一行，只累加 ``hit_count``，不重复建 case；
2. ``promote_rule``：同类错误累计到 ``THRESHOLD``（3）个 case 才允许生成 lesson/rule，
   写入 ``lessons``（id 前缀 ``RULE-``，与 errors.py 的 ``L-`` 区分）；不足 3 次只拒绝，不写库；
3. ``guard_for``：取该 category 已升级的规则文本，供 chat 注入；没有规则返回 ``""``。

复用 db.py 已有的 ``errors`` / ``lessons`` 表，不新增表、不改 db.py。
所有失败都不抛异常：``record_case`` / ``promote_rule`` 返回 ``{"ok": False, "error": ...}``，
``guard_for`` 返回 ``""``。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from . import db

THRESHOLD = 3          # 三次法则：同一类错误满 3 个 case 才升级
RULE_PREFIX = "RULE-"  # lessons 表里规则行的 id 前缀
DETAIL_LIMIT = 500
CAUSE_LIMIT = 300
RULE_LIMIT = 1000
EVIDENCE_LIMIT = 50

__all__ = ["THRESHOLD", "record_case", "promote_rule", "guard_for"]


def _id(prefix: str, *parts: str) -> str:
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return prefix + hashlib.sha1(raw).hexdigest()[:14]


def _category(value: Any) -> str:
    """category 归一化：去空白 + 小写，避免 TOO_LONG / too_long 各记一套。"""
    return str(value or "").strip().lower()


def _one_line(value: Any, limit: int) -> str:
    """压成一行并截断；同一句话多空格/换行不算新 case。"""
    return " ".join(str(value or "").split())[:limit]


def _rule_text(value: Any) -> str:
    return str(value or "").strip()[:RULE_LIMIT]


def _counts(con, category: str) -> tuple[int, int]:
    """返回 (case 数, 累计错误次数)。

    case 数按 (turn_id, category, detail) 去重后算：同一轮重复上报、或 errors.record
    留下的重复行，都不会把一次事故算成三次；occurrences 只是审计用的累计次数，
    不用于升级门槛。
    """
    case_row = con.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT turn_id, category, detail "
        "FROM errors WHERE category=?)", (category,)).fetchone()
    occ_row = con.execute(
        "SELECT COALESCE(SUM(hit_count), 0) FROM errors WHERE category=?",
        (category,)).fetchone()
    return int(case_row[0] or 0), int(occ_row[0] or 0)


def _find_case(con, turn: str, category: str, detail: str):
    """同一 (turn_id, category, detail) 视为同一个 case 的重复上报。"""
    return con.execute(
        "SELECT id, turn_id, root_cause, hit_count FROM errors "
        "WHERE turn_id=? AND category=? AND detail=? "
        "ORDER BY created_at DESC LIMIT 1", (turn, category, detail)).fetchone()


def record_case(path: Path | str, turn_id: str, category: str, detail: str,
                root_cause: str = "") -> dict[str, Any]:
    """记一条错误 case（写/复用 errors 行）。

    - 新 case：INSERT，hit_count=1；
    - 同一轮重复上报：复用原行，hit_count+1，root_cause 为空时补上归因；
    - 返回里带 ``case_count`` / ``occurrences`` / ``promotable``，方便调用方判断是否够三次。
    """
    try:
        cat = _category(category)
        text = _one_line(detail, DETAIL_LIMIT)
        cause = _one_line(root_cause, CAUSE_LIMIT)
        turn = str(turn_id or "").strip()
        if not cat:
            return {"ok": False, "error": "category 不能为空"}
        if not text:
            return {"ok": False, "error": "detail 不能为空"}
        con = db.connect(path)
        try:
            now = time.time()
            row = _find_case(con, turn, cat, text)
            if row is not None:
                merged_cause = str(row["root_cause"] or "") or cause
                con.execute(
                    "UPDATE errors SET hit_count=hit_count+1, root_cause=? WHERE id=?",
                    (merged_cause, row["id"]))
                con.commit()
                eid = str(row["id"])
                first_turn = str(row["turn_id"] or turn)
                hit_count = int(row["hit_count"] or 0) + 1
                reused = True
            else:
                eid = _id("ERR-", turn, cat, text, str(now))
                con.execute(
                    "INSERT INTO errors(id,turn_id,category,detail,created_at,"
                    "root_cause,fix_verified,hit_count) VALUES(?,?,?,?,?,?,?,?)",
                    (eid, turn, cat, text, now, cause, 0, 1))
                con.commit()
                merged_cause = cause
                first_turn = turn
                hit_count = 1
                reused = False
            cases, occurrences = _counts(con, cat)
        finally:
            con.close()
        return {
            "ok": True,
            "error": "",
            "id": eid,
            "category": cat,
            "turn_id": first_turn,
            "last_turn_id": turn,
            "detail": text,
            "root_cause": merged_cause,
            "reused": reused,
            "hit_count": hit_count,
            "case_count": cases,
            "occurrences": occurrences,
            "threshold": THRESHOLD,
            "promotable": cases >= THRESHOLD,
        }
    except Exception as exc:  # noqa: BLE001 - 账本失败不能把主流程带崩
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _evidence(con, category: str, evidence: Any) -> list[str]:
    """归一化证据；不传/为空时自动取该类别的 case id，让规则可回溯。"""
    if evidence is not None:
        if isinstance(evidence, str):
            raw = evidence.strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, list):
                    return [str(x) for x in parsed][:EVIDENCE_LIMIT]
                return [raw[:DETAIL_LIMIT]]
        elif isinstance(evidence, (list, tuple, set)):
            items = [str(x) for x in evidence]
            if items:
                return items[:EVIDENCE_LIMIT]
        else:
            return [str(evidence)[:DETAIL_LIMIT]]
    rows = con.execute(
        "SELECT id FROM errors WHERE category=? ORDER BY created_at DESC LIMIT ?",
        (category, EVIDENCE_LIMIT)).fetchall()
    return [str(row["id"]) for row in rows]


def promote_rule(path: Path | str, category: str, rule: str,
                 evidence: Any = None) -> dict[str, Any]:
    """同类错误满 3 个 case 才允许升级为 lesson/rule；不足 3 次直接拒绝、不写库。

    成功时写入 ``lessons``（同 category 反复升级只替换同一行），返回 rule_id / evidence。
    """
    try:
        cat = _category(category)
        text = _rule_text(rule)
        if not cat:
            return {"ok": False, "promoted": False, "error": "category 不能为空"}
        if not text:
            return {"ok": False, "promoted": False, "error": "rule 不能为空"}
        con = db.connect(path)
        try:
            cases, occurrences = _counts(con, cat)
            if cases < THRESHOLD:
                return {
                    "ok": False,
                    "promoted": False,
                    "category": cat,
                    "case_count": cases,
                    "occurrences": occurrences,
                    "threshold": THRESHOLD,
                    "error": (f"同类错误只有 {cases} 个 case（门槛 {THRESHOLD}），"
                              "先记 case，不生成 rule/guard"),
                }
            ev = _evidence(con, cat, evidence)
            rid = _id(RULE_PREFIX, cat)
            con.execute(
                "INSERT OR REPLACE INTO lessons(id,category,lesson,evidence,status,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (rid, cat, text, json.dumps(ev, ensure_ascii=False), "active", time.time()))
            con.commit()
        finally:
            con.close()
        return {
            "ok": True,
            "error": "",
            "promoted": True,
            "id": rid,
            "rule_id": rid,
            "category": cat,
            "rule": text,
            "evidence": ev,
            "case_count": cases,
            "occurrences": occurrences,
            "threshold": THRESHOLD,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "promoted": False, "error": f"{type(exc).__name__}: {exc}"}


def guard_for(path: Path | str, category: str) -> str:
    """返回该 category 已升级的规则文本；没有规则或读取失败返回 ""。"""
    try:
        cat = _category(category)
        if not cat:
            return ""
        con = db.connect(path)
        try:
            row = con.execute(
                "SELECT lesson FROM lessons WHERE category=? AND status='active' "
                "AND id LIKE ? ORDER BY created_at DESC LIMIT 1",
                (cat, RULE_PREFIX + "%")).fetchone()
        finally:
            con.close()
        return str(row["lesson"] or "") if row is not None else ""
    except Exception:  # noqa: BLE001 - 注入失败时宁可不注入，也不打断 chat
        return ""
