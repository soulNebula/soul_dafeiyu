# -*- coding: utf-8 -*-
"""Learn Loop 盲测：问题给学习者，答案关键词隐藏；二源 + 盲测通过才解锁。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from . import db, learn_loop, verifier


def _token(*parts) -> str:
    return "BT-" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _load(path: Path | str) -> dict:
    con = db.connect(path)
    try:
        row = con.execute("SELECT value FROM state WHERE key='blind_tests'").fetchone()
    finally:
        con.close()
    if not row:
        return {}
    try:
        data = json.loads(row[0])
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _save(path: Path | str, data: dict) -> None:
    con = db.connect(path)
    try:
        con.execute("INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    ("blind_tests", json.dumps(data, ensure_ascii=False), time.time()))
        con.commit()
    finally:
        con.close()


def create(path: Path | str, skill_id: str, question: str,
           expected_terms: list[str]) -> dict[str, Any]:
    if not str(skill_id or "").strip() or not str(question or "").strip():
        return {"ok": False, "error": "skill_id/question 不能为空"}
    terms = [str(t) for t in (expected_terms or []) if str(t).strip()]
    if not terms:
        return {"ok": False, "error": "expected_terms 不能为空"}
    token = _token(skill_id, question, time.time())
    data = _load(path)
    data[token] = {"skill_id": str(skill_id), "question": str(question)[:500],
                   "expected_terms": terms[:8], "created_at": time.time(), "used": False}
    _save(path, data)
    return {"ok": True, "token": token, "question": str(question)[:500], "skill_id": skill_id}


def grade(path: Path | str, token: str, answer: str) -> dict[str, Any]:
    data = _load(path)
    item = data.get(str(token))
    if not item:
        return {"ok": False, "error": "盲测不存在"}
    result = verifier.verify_answer(answer, item.get("expected_terms") or [])
    item["used"] = True
    _save(path, data)
    return {"ok": bool(result.get("ok")), "passed": bool(result.get("passed")),
            "graded": result, "skill_id": item.get("skill_id")}


def verify_with_blind(path: Path | str, task_id: str, token: str,
                      answer: str) -> dict[str, Any]:
    task = None
    con = db.connect(path)
    try:
        row = con.execute("SELECT * FROM learn_tasks WHERE id=?", (str(task_id),)).fetchone()
        task = dict(row) if row else None
    finally:
        con.close()
    if task is None:
        return {"ok": False, "error": "learn_task 不存在"}
    skill_id = str(task["skill_id"])
    sources = learn_loop.status(path, skill_id).get("source_count") or 0
    if int(sources) < 2:
        return {"ok": False, "reason": "需要至少 2 个来源", "sources": int(sources)}
    blind = grade(path, token, answer)
    if not blind.get("passed"):
        return {"ok": False, "reason": "盲测未通过", "blind": blind}
    unlocked = learn_loop.verify_and_unlock(path, task_id, True, "blind_test_passed")
    return {"ok": bool(unlocked.get("unlocked")), "sources": int(sources),
            "blind": blind, "unlocked": unlocked}
