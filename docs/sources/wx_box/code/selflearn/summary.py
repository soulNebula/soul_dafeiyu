# -*- coding: utf-8 -*-
"""04/02 阶段摘要：把多轮对话压缩成可复用的长期经历。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from . import db, memory
from .config import DB_PATH
from .llm import parse_json


def _id(*parts) -> str:
    return "SUM-" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def latest(path: Path | str = DB_PATH) -> dict | None:
    con = db.connect(path)
    try:
        row = con.execute(
            "SELECT * FROM summaries ORDER BY created_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def context(path: Path | str = DB_PATH, max_chars: int = 900) -> str:
    item = latest(path)
    return (item["content"] if item else "")[:max_chars]


def build(path: Path | str = DB_PATH, client=None, min_turns: int = 6,
          force: bool = False) -> dict[str, Any]:
    if client is None or not getattr(client, "available", lambda: False)():
        return {"ok": False, "error": "LLM 不可用"}
    con = db.connect(path)
    try:
        turns = con.execute("SELECT count(*) FROM turns").fetchone()[0]
        last = con.execute("SELECT max(created_at) FROM summaries").fetchone()[0]
        msgs = [dict(r) for r in con.execute(
            "SELECT role,text,ts FROM messages WHERE ts>? ORDER BY ts ASC LIMIT 40",
            (float(last or 0),)).fetchall()]
    finally:
        con.close()
    if not force and (int(turns) < min_turns or len(msgs) < 4):
        return {"ok": True, "created": False, "reason": "对话样本不足"}
    if not msgs:
        return {"ok": True, "created": False, "reason": "没有新消息"}
    dialog = "\n".join(f"{'用户' if m['role'] == 'user' else 'AI'}：{m['text']}"
                       for m in msgs[-30:])
    prompt = (
        "把下面这段长期对话压缩成一段阶段摘要，供以后的自己使用。"
        "只保留：用户明确说过的事实/偏好、关系进展、未解决的话题和承诺。"
        "不要编造，不要评价，不要写建议。只输出 JSON："
        '{"summary":"不超过 300 字","open_threads":["未解决话题，最多3条"]}\n\n' + dialog
    )
    result = client.chat(
        [{"role": "system", "content": "你是长期记忆压缩器。"},
         {"role": "user", "content": prompt}],
        purpose="selflearn_summary", max_tokens=700, temperature=0.2, json_mode=True,
    )
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "LLM 失败"}
    obj = parse_json(str(result.get("text") or ""))
    content = str(obj.get("summary") or "").strip() if isinstance(obj, dict) else ""
    if not content:
        return {"ok": False, "error": "摘要为空"}
    threads = obj.get("open_threads") if isinstance(obj, dict) else []
    sid = _id(content, time.time())
    con = db.connect(path)
    try:
        con.execute("INSERT INTO summaries(id,content,evidence_json,turns_count,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (sid, content[:800], json.dumps({"open_threads": threads or []},
                                                     ensure_ascii=False), int(turns), time.time()))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "created": True, "id": sid, "summary": content,
            "open_threads": threads or []}
