# -*- coding: utf-8 -*-
"""成长报告：把对话/记忆/人格/技能/信任/错误汇总成人可读证据。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import db, trust
from .config import DB_PATH


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (ValueError, OSError):
        return "-"


def growth(path: Path | str = DB_PATH) -> dict[str, Any]:
    con = db.connect(path)
    try:
        counts = db.counts(path)
        rows = con.execute("SELECT role,count(*) n FROM messages GROUP BY role").fetchall()
        msgs = {r["role"]: int(r["n"]) for r in rows}
        first = con.execute("SELECT min(ts) v FROM messages").fetchone()["v"]
        last = con.execute("SELECT max(ts) v FROM messages").fetchone()["v"]
        feedback = {r["reaction"]: int(r["n"]) for r in con.execute(
            "SELECT reaction,count(*) n FROM feedback GROUP BY reaction")}
        scopes = {r["scope"]: int(r["n"]) for r in con.execute(
            "SELECT scope,count(*) n FROM user_memories GROUP BY scope")}
        kinds = {r["kind"]: int(r["n"]) for r in con.execute(
            "SELECT kind,count(*) n FROM user_memories GROUP BY kind")}
        persona = [dict(r) for r in con.execute(
            "SELECT key,value,version,updated_at FROM persona WHERE status='active' ORDER BY key")]
        conflicts_open = int(con.execute(
            "SELECT count(*) FROM conflicts WHERE status='open'").fetchone()[0])
        skills = [dict(r) for r in con.execute(
            "SELECT id,name,level FROM skills WHERE status='active'")]
        variants = [dict(r) for r in con.execute(
            "SELECT id,hint,alpha,beta FROM variants WHERE status='active' ORDER BY id")]
        errors = [dict(r) for r in con.execute(
            "SELECT category,count(*) n FROM errors GROUP BY category ORDER BY n DESC")]
        lessons = [dict(r) for r in con.execute(
            "SELECT category,lesson FROM lessons WHERE status='active' ORDER BY created_at DESC")]
        proactive = {r["status"]: int(r["n"]) for r in con.execute(
            "SELECT status,count(*) n FROM proactive GROUP BY status")}
        intents = {r["status"]: int(r["n"]) for r in con.execute(
            "SELECT status,count(*) n FROM intents GROUP BY status")}
        state = con.execute("SELECT value FROM state WHERE key='self_state'").fetchone()
    finally:
        con.close()
    messages = sum(msgs.values())
    turns = counts.get("turns", 0)
    fb_total = sum(feedback.values())
    if messages == 0:
        stage = "blank"
    elif messages < 6:
        stage = "fresh"
    elif turns < 10 or fb_total < 3:
        stage = "forming"
    elif persona and fb_total >= 10:
        stage = "stable"
    else:
        stage = "growing"
    trust_value = trust.get(path)
    return {
        "ok": True,
        "stage": stage,
        "period": {"first": _fmt_time(first), "last": _fmt_time(last)},
        "messages": {"total": messages, **msgs},
        "turns": turns,
        "feedback": feedback,
        "feedback_total": fb_total,
        "memories": {"total": counts.get("user_memories", 0), "scopes": scopes, "kinds": kinds},
        "persona": persona,
        "persona_count": len(persona),
        "conflicts_open": conflicts_open,
        "skills": skills,
        "variants": variants,
        "errors": errors,
        "lessons": lessons,
        "proactive": proactive,
        "intents": intents,
        "trust": {"value": round(trust_value, 4), "stage": trust.stage(trust_value)},
        "state": state["value"] if state else None,
        "counts": counts,
        "note": "成长报告只展示本地可验证数据，不代表生产质量。",
    }


def markdown(path: Path | str = DB_PATH) -> str:
    g = growth(path)
    lines = [
        "# SelfLearn 成长报告", "",
        f"- 阶段：`{g['stage']}`",
        f"- 时间：{g['period']['first']} → {g['period']['last']}",
        f"- 消息：{g['messages']['total']}（用户 {g['messages'].get('user', 0)} / AI {g['messages'].get('assistant', 0)}）",
        f"- 对话轮：{g['turns']}；反馈：{g['feedback_total']} {g['feedback']}",
        f"- 信任：{g['trust']['value']}（{g['trust']['stage']}）",
        f"- 记忆：{g['memories']['total']}；scope={g['memories']['scopes']}",
        f"- 人格：{g['persona_count']} 条；未解决记忆冲突：{g['conflicts_open']}",
        f"- 技能：{[(s['name'], s['level']) for s in g['skills']]}",
        f"- 未解决错误类型：{g['errors']}",
        f"- 主动候选：{g['proactive']}；调度：{g['intents']}",
    ]
    if g["persona"]:
        lines += ["", "## 已稳定的人格"]
        lines += [f"- **{p['key']}**：{p['value']}" for p in g["persona"]]
    if g["lessons"]:
        lines += ["", "## 学到的教训"]
        lines += [f"- `{x['category']}`：{x['lesson']}" for x in g["lessons"]]
    return "\n".join(lines) + "\n"
