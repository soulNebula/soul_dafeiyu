# -*- coding: utf-8 -*-
"""Agent 自人格：只从自己的行动结果/反馈中形成，且缓慢、不可轻易翻转。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from . import db, skills
from .config import DB_PATH


def context(path: Path | str = DB_PATH, limit_chars: int = 1400) -> str:
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT key,value FROM persona WHERE status='active' ORDER BY key"
        ).fetchall()
    finally:
        con.close()
    return "\n".join(f"{r['key']}：{r['value']}" for r in rows)[:max(0, int(limit_chars))]


def list_persona(path: Path | str = DB_PATH) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM persona WHERE status='active' ORDER BY key")]
    finally:
        con.close()


def list_candidates(path: Path | str = DB_PATH) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM persona_candidates WHERE status='candidate' ORDER BY count DESC")]
    finally:
        con.close()


def reflect(path: Path | str = DB_PATH, client=None) -> dict[str, Any]:
    """从自己的行动结果中提炼自人格；先候选，至少两次一致才激活。

    激活后同 key 不再改值——人格要稳定，不能今天一个明天一个。
    """
    if client is None or not getattr(client, "available", lambda: False)():
        return {"ok": False, "error": "LLM 不可用"}
    con = db.connect(path)
    try:
        turns = con.execute(
            "SELECT id,reply,feedback,score FROM turns WHERE feedback IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    finally:
        con.close()
    if len(turns) < 3:
        return {"ok": True, "written": 0, "reason": "反馈样本不足"}
    lines = "\n".join(
        f"- turn={t['id']} | 我的回复：{t['reply'][:160]} | 用户反馈：{t['feedback']}"
        for t in turns
    )
    prompt = (
        "你在观察一个自学习对话体自己的行动和结果。"
        "只根据“我做了什么/结果如何”，提炼 1-3 条它自己的聊天规则或自人格特征。"
        "不要从用户偏好推断人格，不要写空话；每条必须引用 turn id 作为证据。"
        '只输出 JSON：{"traits":[{"key":"short_key","value":"可执行的自人格/规则",'
        '"evidence_turns":["T-..."]}]}\n\n' + lines
    )
    result = client.chat(
        [{"role": "system", "content": "你在帮助对话体从自己的行动结果中形成稳定人格。"},
         {"role": "user", "content": prompt}],
        purpose="selflearn_reflect", max_tokens=900, temperature=0.2,
        json_mode=True,
    )
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "LLM 失败"}
    from .llm import parse_json
    obj = parse_json(str(result.get("text") or ""))
    traits = obj.get("traits") if isinstance(obj, dict) else None
    if not isinstance(traits, list):
        return {"ok": False, "error": "反思输出不是 traits 数组",
                "raw": str(result.get("text") or "")[:300]}
    new_variant = obj.get("new_variant") if isinstance(obj, dict) else None
    variant_result = None
    if isinstance(new_variant, str) and new_variant.strip():
        variant_result = skills.add_dynamic_variant(
            path, new_variant, evidence="reflection:" + ",".join(t["id"] for t in turns[:3]))
    turn_ids = {t["id"] for t in turns}
    candidates, activated = [], []
    con = db.connect(path)
    try:
        now = time.time()
        active_count = int(con.execute(
            "SELECT count(*) FROM persona WHERE status='active'").fetchone()[0])
        for trait in traits[:3]:
            if not isinstance(trait, dict):
                continue
            key = str(trait.get("key") or "").strip()[:40]
            value = str(trait.get("value") or "").strip()[:240]
            evidence = [str(x) for x in (trait.get("evidence_turns") or []) if str(x) in turn_ids]
            if not key or not value or not evidence:
                continue
            active = con.execute(
                "SELECT value FROM persona WHERE key=? AND status='active' LIMIT 1", (key,)
            ).fetchone()
            if active is not None:
                # 已有稳定人格：同 key 不允许改值，保护稳定性
                if active["value"] == value:
                    continue
                candidates.append({"key": key, "value": value, "status": "rejected_active"})
                continue
            row = con.execute(
                "SELECT id,count,evidence_json FROM persona_candidates "
                "WHERE key=? AND value=? AND status='candidate'", (key, value)
            ).fetchone()
            if row is None:
                if active_count >= 8:
                    continue
                pid = "P-" + hashlib.sha1(f"{key}|{value}".encode()).hexdigest()[:14]
                con.execute(
                    "INSERT INTO persona(id,key,value,evidence_json,version,status,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (pid, key, value, json.dumps({"turns": evidence}, ensure_ascii=False),
                     1, "active", now),
                )
                con.execute(
                    "INSERT INTO reflections(id,summary,evidence_turns,created_at) VALUES(?,?,?,?)",
                    ("R-" + pid, f"{key}: {value}", json.dumps(evidence, ensure_ascii=False), now),
                )
                active_count += 1
                activated.append({"key": key, "value": value, "evidence_turns": evidence})
                candidates.append({"key": key, "value": value, "count": 1})
                continue
            old = json.loads(row["evidence_json"] or "{}").get("turns") or []
            merged = sorted(set(old) | set(evidence))
            count = int(row["count"] or 1) + 1
            if count >= 2 and len(merged) >= 3:
                pid = "P-" + hashlib.sha1(f"{key}|{value}".encode()).hexdigest()[:14]
                con.execute(
                    "INSERT INTO persona(id,key,value,evidence_json,version,status,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (pid, key, value, json.dumps({"turns": merged}, ensure_ascii=False),
                     1, "active", now),
                )
                con.execute("UPDATE persona_candidates SET status='promoted',count=?,"
                            "evidence_json=?,updated_at=? WHERE id=?",
                            (count, json.dumps({"turns": merged}, ensure_ascii=False), now, row["id"]))
                con.execute(
                    "INSERT INTO reflections(id,summary,evidence_turns,created_at) VALUES(?,?,?,?)",
                    ("R-" + pid, f"{key}: {value}", json.dumps(merged, ensure_ascii=False), now),
                )
                activated.append({"key": key, "value": value, "evidence_turns": merged})
            else:
                con.execute(
                    "UPDATE persona_candidates SET count=?,evidence_json=?,updated_at=? WHERE id=?",
                    (count, json.dumps({"turns": merged}, ensure_ascii=False), now, row["id"]),
                )
                candidates.append({"key": key, "value": value, "count": count})
        con.commit()
    finally:
        con.close()
    return {"ok": True, "written": len(activated), "activated": activated,
            "candidates": candidates, "new_variant": variant_result}
