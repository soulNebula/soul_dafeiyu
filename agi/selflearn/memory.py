# -*- coding: utf-8 -*-
"""记忆：由 LLM 自己决定留存什么；用户事实与 Agent 自人格分表。"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

KINDS = ("fact", "preference", "event", "plan", "value", "insight")

SENSITIVE_PATTERNS = (
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(密码|银行卡|身份证|详细住址|家庭住址)"),
)


def _sensitive(text: str) -> bool:
    value = str(text or "")
    return any(p.search(value) for p in SENSITIVE_PATTERNS)


def _id(prefix: str, *parts) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part).encode("utf-8", "replace"))
        h.update(b"\x1e")
    return prefix + h.hexdigest()[:14]


def store_message(path: Path | str, role: str, text: str, now: float | None = None,
                  *, proactive: bool = False) -> str:
    ts = time.time() if now is None else float(now)
    mid = _id("M-", role, text, ts)
    con = db.connect(path)
    try:
        con.execute("INSERT OR REPLACE INTO messages(id,role,text,ts,proactive) "
                    "VALUES(?,?,?,?,?)",
                    (mid, role, str(text).strip(), ts, 1 if proactive else 0))
        con.commit()
    finally:
        con.close()
    return mid


def recent_messages(path: Path | str = DB_PATH, limit: int = 12) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM messages ORDER BY ts DESC,id DESC LIMIT ?", (int(limit),)).fetchall()][::-1]
    finally:
        con.close()


def _clean(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _question(text: str) -> bool:
    return _clean(text).endswith(("?", "？", "吗", "呢"))


def _rule(text: str) -> list[dict]:
    out = []
    for kind, pat in (
        ("preference", r"我(?:喜欢|爱|讨厌|不喜欢)([^。！？!?]{1,40})"),
        ("fact", r"我(?:叫|是|住在|在)([^。！？!?]{1,40})"),
        ("plan", r"我(?:计划|打算|准备|想要|想)([^。！？!?]{1,40})"),
        ("event", r"我(?:最近|昨天|今天|明天|刚)([^。！？!?]{1,50})"),
    ):
        for m in re.finditer(pat, text):
            out.append({"scope": "user", "kind": kind, "content": m.group(0),
                        "evidence": m.group(0), "confidence": 0.6})
    return out


def extract_user_memories(path: Path | str, client, text: str, message_id: str) -> list[dict]:
    text = _clean(text)
    if not text:
        return []
    candidates: list[dict] = []
    if client is not None and getattr(client, "available", lambda: False)():
        prompt = (
            "你是这个自学习对话体自己的记忆系统。回顾下面这段交互，"
            "由你自行判断哪些内容值得长期记住：可以关于用户、世界/事件，"
            "也可以关于你自己刚才的观察、策略和结果。只记对未来对话有用的，"
            "不要复述用户要求你记的东西，不要推测没有证据的内容。"
            '输出 JSON 对象：{"items":[{"scope":"user|self|world",'
            '"kind":"fact|preference|event|plan|value|insight",'
            '"content":"一句短记忆","evidence":"原话连续片段","confidence":0.0-1.0}]}。'
            '没有值得记的就输出 {"items":[]}。\n交互原话：' + text
        )
        r = client.chat([{"role": "system", "content": "你是自学习对话体的记忆系统。"},
                         {"role": "user", "content": prompt}],
                        purpose="selflearn_memory", max_tokens=700, temperature=0.2,
                        json_mode=True)
        if r.get("ok"):
            from .llm import parse_json
            raw = str(r.get("text") or "")
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                data = None
            if isinstance(data, dict):
                data = data.get("items") or data.get("memories")
            if not isinstance(data, list):
                obj = parse_json(raw)
                data = obj.get("items") if isinstance(obj, dict) else None
            if isinstance(data, list):
                candidates = [x for x in data if isinstance(x, dict)]
    if not candidates:
        candidates = _rule(text)
    saved = []
    con = db.connect(path)
    try:
        now = time.time()
        for item in candidates:
            kind = str(item.get("kind") or "fact").lower()
            if kind not in KINDS:
                kind = "fact"
            scope = str(item.get("scope") or "user").lower()
            if scope not in ("user", "self", "world"):
                scope = "user"
            content, evidence = _clean(item.get("content")), _clean(item.get("evidence"))
            if not content or not evidence or evidence not in text:
                continue
            if _question(evidence) or _question(content):
                continue
            if _sensitive(evidence) or _sensitive(content):
                continue
            try:
                conf = max(0.0, min(1.0, float(item.get("confidence") or 0.6)))
            except (TypeError, ValueError):
                conf = 0.6
            status = "active" if conf >= 0.6 else "pending"
            mid = _id("UM-", content)
            conflict = None
            parsed = _parse_fact(content) if status == "active" else None
            if parsed is not None:
                for other in con.execute(
                        "SELECT * FROM user_memories WHERE status='active' AND kind=? AND scope=?",
                        (kind, scope)).fetchall():
                    if _is_conflict(parsed, _parse_fact(other["content"])):
                        conflict = other
                        break
            if conflict is not None:
                group = _id("CF-", mid, conflict["id"], now)
                con.execute("UPDATE user_memories SET status='conflict',conflict_group=? WHERE id=?",
                            (group, conflict["id"]))
                con.execute(
                    "INSERT OR IGNORE INTO user_memories(id,scope,kind,content,evidence,"
                    "evidence_message_id,confidence,status,conflict_group,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (mid, scope, kind, content, evidence, message_id, conf, "conflict", group, now))
                con.execute("INSERT INTO conflicts(id,mem_a,mem_b,status,created_at) "
                            "VALUES(?,?,?,?,?)", (group, conflict["id"], mid, "open", now))
                saved.append({"id": mid, "scope": scope, "kind": kind, "content": content,
                              "evidence": evidence, "confidence": conf, "conflict": group})
                continue
            cur = con.execute(
                "INSERT OR IGNORE INTO user_memories(id,scope,kind,content,evidence,"
                "evidence_message_id,confidence,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (mid, scope, kind, content, evidence, message_id, conf, status, now))
            if cur.rowcount:
                saved.append({"id": mid, "scope": scope, "kind": kind, "content": content,
                              "evidence": evidence, "confidence": conf, "status": status})
        con.commit()
    finally:
        con.close()
    return saved


def store_image_memory(path: Path | str, description: str, message_id: str) -> dict | None:
    """把模型对图片的理解写成一条 world 记忆；图片本身是证据。"""
    description = _clean(description)[:300]
    if not description:
        return None
    content = "图片内容：" + description
    mid = _id("UM-", content)
    con = db.connect(path)
    try:
        cur = con.execute(
            "INSERT OR IGNORE INTO user_memories(id,scope,kind,content,evidence,"
            "evidence_message_id,confidence,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (mid, "world", "insight", content, "[图片]", message_id, 0.6, "active", time.time()))
        con.commit()
        if cur.rowcount:
            return {"id": mid, "scope": "world", "kind": "insight", "content": content,
                    "evidence": "[图片]", "confidence": 0.6}
    finally:
        con.close()
    return None


#: 简单事实解析：只用于冲突检测，不替代 LLM 抽取。
_PREFIXES = (
    ("不喜欢", "preference|喜欢", -1),
    ("喜欢", "preference|喜欢", 1),
    ("爱", "preference|喜欢", 1),
    ("讨厌", "preference|喜欢", -1),
    ("住在", "fact|住", 1),
    ("在", "fact|在", 1),
    ("叫", "fact|叫", 1),
    ("是", "fact|是", 1),
    ("计划", "plan|计划", 1),
    ("打算", "plan|计划", 1),
)


def _parse_fact(content: str):
    text = _clean(content)
    text = re.sub(r"^(用户|我)", "", text).strip()
    for prefix, key, polarity in _PREFIXES:
        if text.startswith(prefix):
            obj = text[len(prefix):].strip("，,。！？!? ")
            if obj:
                return key, obj, polarity
    return None


def _is_conflict(a, b) -> bool:
    if a is None or b is None:
        return False
    if a[0] != b[0]:
        return False
    # 同一偏好对象，正负相反；同一住址/姓名/身份，对象不同
    if a[1] == b[1] and a[2] != b[2]:
        return True
    if a[0] in ("fact|住", "fact|在", "fact|叫", "fact|是") and a[1] != b[1]:
        return True
    return False


def open_conflicts(path: Path | str = DB_PATH) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM conflicts WHERE status='open' ORDER BY created_at DESC LIMIT 20")]
    finally:
        con.close()


def resolve_conflict(path: Path | str, conflict_id: str, winner_id: str) -> dict:
    con = db.connect(path)
    try:
        row = con.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "冲突不存在"}
        loser = row["mem_b"] if row["mem_a"] == winner_id else row["mem_a"]
        con.execute("UPDATE user_memories SET status='active',conflict_group=NULL WHERE id=?",
                    (winner_id,))
        con.execute("UPDATE user_memories SET status='invalid',conflict_group=NULL WHERE id=?",
                    (loser,))
        con.execute("UPDATE conflicts SET status='resolved',resolution=? WHERE id=?",
                    (winner_id, conflict_id))
        con.commit()
        return {"ok": True, "winner": winner_id, "loser": loser}
    finally:
        con.close()


def _shingles(text: str) -> set[str]:
    v = re.sub(r"\s+", "", text)
    return {v[i:i + 2] for i in range(max(0, len(v) - 1))}


def retrieve(path: Path | str = DB_PATH, query: str = "", k: int = 6) -> list[dict]:
    con = db.connect(path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM user_memories WHERE status='active' ORDER BY created_at DESC LIMIT 200")]
    finally:
        con.close()
    q = _shingles(query)
    scored = []
    for row in rows:
        overlap = len(q & _shingles(f"{row['content']} {row['evidence']}"))
        if overlap or not q:
            scored.append((overlap, row))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:max(1, int(k))]]


def forget(path: Path | str, memory_id: str, reason: str = "user_forget") -> dict:
    """让一条记忆失效，不物理删除，保留审计。"""
    con = db.connect(path)
    try:
        cur = con.execute(
            "UPDATE user_memories SET status='invalid',conflict_group=NULL WHERE id=?",
            (memory_id,))
        con.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "记忆不存在"}
        try:
            from . import search
            search.delete(path, "memory", memory_id)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "id": memory_id, "action": "invalid", "reason": reason}
    finally:
        con.close()


def export_brain(path: Path | str = DB_PATH) -> dict:
    """导出可审计的脑数据（不包含 API key）。"""
    con = db.connect(path)
    try:
        data = {}
        for table in ("messages", "user_memories", "turns", "feedback", "persona",
                      "persona_candidates", "skills", "variants", "conflicts",
                      "errors", "lessons", "weak_checks", "proactive", "intents", "attachments"):
            data[table] = [dict(r) for r in con.execute(f"SELECT * FROM {table}").fetchall()]
        return {"ok": True, "exported_at": time.time(), "db": str(path), "tables": data}
    finally:
        con.close()


def list_user_memories(path: Path | str = DB_PATH, limit: int = 50) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM user_memories ORDER BY created_at DESC LIMIT ?", (int(limit),))]
    finally:
        con.close()
