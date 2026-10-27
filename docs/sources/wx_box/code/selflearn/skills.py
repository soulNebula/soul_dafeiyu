# -*- coding: utf-8 -*-
"""聊天技能/策略变体与反馈统计。"""
from __future__ import annotations

import hashlib
import random
import time
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

SKILL_ID = "S-CONV-001"
BASELINE_HINT = "自然回应"
BASELINE_ID = "V-BASE"
#: 旧版本预置的策略，迁移时一律退役，不再显示为已学会技能
LEGACY_DEFAULT_HINTS = (
    "先共情、短句接住，不急着给建议",
    "先问一个具体问题，再回应",
    "直接给一个可执行的小建议",
    "用一点轻松/幽默的语气",
)


def _new(prefix: str, *parts) -> str:
    h = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]
    return prefix + h


def ensure_defaults(path: Path | str = DB_PATH) -> None:
    con = db.connect(path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO skills(id,name,description,level,alpha,beta,status) "
            "VALUES(?,?,?,?,?,?,?)",
            (SKILL_ID, "日常聊天", "根据反馈学习更合适的聊天策略", 0, 1, 1, "active"),
        )
        # 初始不预置任何“已学会策略”；只有一个不显示的基线策略
        con.execute(
            "UPDATE variants SET status='retired' WHERE status='active' AND hint IN (?,?,?,?)",
            LEGACY_DEFAULT_HINTS,
        )
        con.execute(
            "INSERT OR IGNORE INTO variants(id,skill_id,hint,alpha,beta,status) "
            "VALUES(?,?,?,?,?,?)",
            (BASELINE_ID, SKILL_ID, BASELINE_HINT, 1, 1, "base"),
        )
        # 没有任何弱验证样本时，等级必须是空白 0（兼容早期 DB）
        n = con.execute("SELECT count(*) FROM weak_checks").fetchone()[0]
        if int(n or 0) == 0:
            con.execute("UPDATE skills SET level=0 WHERE id=?", (SKILL_ID,))
        con.commit()
    finally:
        con.close()


def wilson_lower(success: int, n: int, z: float = 1.96) -> float:
    """二项分布的 Wilson 95% 下限；弱验证不拿点估计升级。"""
    if n <= 0:
        return 0.0
    p = max(0.0, min(1.0, success / n))
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return max(0.0, (centre - margin) / denom)


def record_weak_check(path: Path | str, skill_id: str, variant_id: str | None,
                      passed: bool, note: str = "") -> str:
    ensure_defaults(path)
    cid = _new("WC-", skill_id, variant_id, passed, time.time())
    con = db.connect(path)
    try:
        con.execute("INSERT INTO weak_checks(id,skill_id,variant_id,passed,note,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (cid, skill_id, variant_id, 1 if passed else 0, note[:300], time.time()))
        con.commit()
    finally:
        con.close()
    return cid


def evaluate_skill(path: Path | str = DB_PATH) -> dict[str, Any]:
    """样本不足 5 或 Wilson 下限 <=0.5 不升级；只升不降。"""
    ensure_defaults(path)
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT id,skill_id,alpha,beta,status FROM variants "
            "WHERE status IN ('active','gray')").fetchall()
        checks = {}
        for r in rows:
            data = con.execute(
                "SELECT count(*) n, sum(passed) ok FROM weak_checks WHERE variant_id=?",
                (r["id"],)).fetchone()
            n, ok = int(data["n"] or 0), int(data["ok"] or 0)
            rate = ok / n if n else 0.0
            lower = wilson_lower(ok, n)
            level = 0
            if n >= 5 and lower > 0.5:
                level = min(5, 1 + int((rate - 0.5) * 8))
            if r["status"] == "gray" and n >= 5:
                if lower > 0.5 and rate >= 0.5:
                    con.execute("UPDATE variants SET status='active' WHERE id=?", (r["id"],))
                    checks[r["id"]] = {"n": n, "ok": ok, "rate": round(rate, 3),
                                       "wilson_lower": round(lower, 3), "level": level,
                                       "promoted": True}
                else:
                    con.execute("UPDATE variants SET status='retired' WHERE id=?", (r["id"],))
                    checks[r["id"]] = {"n": n, "ok": ok, "rate": round(rate, 3),
                                       "wilson_lower": round(lower, 3), "level": 0,
                                       "retired": True}
                continue
            checks[r["id"]] = {"n": n, "ok": ok, "rate": round(rate, 3),
                               "wilson_lower": round(lower, 3), "level": level}
        # 技能等级取该技能下表现最好的变体；只升不降
        best = max((v["level"] for v in checks.values()), default=0)
        cur = con.execute("SELECT level FROM skills WHERE id=?", (SKILL_ID,)).fetchone()
        new_level = max(int(cur["level"] or 0) if cur else 0, best)
        con.execute("UPDATE skills SET level=? WHERE id=?", (new_level, SKILL_ID))
        con.commit()
    finally:
        con.close()
    return {"skill_id": SKILL_ID, "level": new_level, "variants": checks}


def pick_variant(path: Path | str = DB_PATH, rng: random.Random | None = None) -> dict[str, Any]:
    ensure_defaults(path)
    rng = rng or random
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT * FROM variants WHERE skill_id=? AND status IN ('active','gray')",
            (SKILL_ID,)
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return {"skill_id": SKILL_ID, "variant_id": BASELINE_ID,
                "hint": BASELINE_HINT, "alpha": 1, "beta": 1, "status": "base"}
    active = [r for r in rows if r["status"] == "active"]
    gray = [r for r in rows if r["status"] == "gray"]
    # 10% 灰度流量；其余走 active 的 Thompson 采样
    pool = gray if (gray and rng.random() < 0.2) else (active or rows)
    best = max(pool, key=lambda r: rng.betavariate(float(r["alpha"] or 1), float(r["beta"] or 1)))
    return {"skill_id": SKILL_ID, "variant_id": best["id"], "hint": best["hint"],
            "alpha": best["alpha"], "beta": best["beta"], "status": best["status"]}


def score(reaction: str) -> float:
    return {"good": 1.0, "ok": 0.6, "bad": 0.0}.get(str(reaction).lower(), 0.4)


def record_feedback(path: Path | str, turn_id: str, reaction: str,
                    note: str = "") -> dict[str, Any]:
    ensure_defaults(path)
    reaction = str(reaction or "").lower()
    if reaction not in ("good", "ok", "bad"):
        return {"ok": False, "error": "reaction 只允许 good/ok/bad"}
    con = db.connect(path)
    try:
        turn = con.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        if turn is None:
            return {"ok": False, "error": "turn 不存在"}
        s = score(reaction)
        con.execute("UPDATE turns SET feedback=?, score=? WHERE id=?",
                    (reaction, s, turn_id))
        con.execute(
            "INSERT INTO feedback(id,turn_id,reaction,score,note,created_at) VALUES(?,?,?,?,?,?)",
            (_new("FB-", turn_id, time.time()), turn_id, reaction, s, note[:500], time.time()),
        )
        if turn["variant_id"]:
            con.execute(
                "UPDATE variants SET alpha=alpha+?, beta=beta+? WHERE id=?",
                (s, 1 - s, turn["variant_id"]),
            )
        con.execute(
            "INSERT INTO weak_checks(id,skill_id,variant_id,passed,note,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (_new("WC-", SKILL_ID, turn["variant_id"], reaction, time.time()),
             SKILL_ID, turn["variant_id"], 1 if s >= 0.6 else 0, reaction[:300], time.time()),
        )
        con.commit()
        row = con.execute("SELECT * FROM variants WHERE id=?", (turn["variant_id"],)).fetchone()
    finally:
        con.close()
    evaluation = evaluate_skill(path)
    return {"ok": True, "turn_id": turn_id, "reaction": reaction, "score": s,
            "variant": dict(row) if row else None,
            "stats": variant_stats(path), "skill": evaluation}


def add_dynamic_variant(path: Path | str = DB_PATH, hint: str = "", evidence: str = "",
                        max_variants: int = 8) -> dict[str, Any]:
    """从自己的行动结果里长出一条新策略；必须简短、去重、限量。"""
    ensure_defaults(path)
    hint = " ".join(str(hint or "").split()).strip()[:120]
    if len(hint) < 4:
        return {"ok": False, "error": "hint 太短"}
    con = db.connect(path)
    try:
        existing = [r["hint"] for r in con.execute(
            "SELECT hint FROM variants WHERE skill_id=? AND status IN ('active','gray')",
            (SKILL_ID,))]
        if len(existing) >= int(max_variants):
            return {"ok": False, "error": "策略数量已满"}
        if hint in existing:
            return {"ok": True, "created": False, "reason": "已存在", "hint": hint}
        vid = _new("V-", SKILL_ID, hint)
        con.execute(
            "INSERT OR IGNORE INTO variants(id,skill_id,hint,alpha,beta,status) "
            "VALUES(?,?,?,?,?,?)",
            (vid, SKILL_ID, hint, 1, 1, "gray"))
        con.commit()
        return {"ok": True, "created": True, "id": vid, "hint": hint,
                "evidence": str(evidence or "")[:300]}
    finally:
        con.close()


def variant_stats(path: Path | str = DB_PATH) -> list[dict]:
    ensure_defaults(path)
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT v.*, s.name AS skill_name, s.level AS skill_level "
            "FROM variants v JOIN skills s ON s.id=v.skill_id "
            "WHERE v.status IN ('active','gray') ORDER BY v.status, v.alpha/(v.alpha+v.beta) DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()
