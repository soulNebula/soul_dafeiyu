# -*- coding: utf-8 -*-
"""记忆：由 LLM 自己决定留存什么；用户事实与 Agent 自人格分表。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

KINDS = ("fact", "preference", "event", "plan", "value", "insight")

# --------------------------------------------------------------------------- #
# P1-a：重要性 / 去重合并 / 衰减
# --------------------------------------------------------------------------- #
#: kind → 初始重要性（LLM 返回里带 importance 时优先用 LLM 的）
KIND_IMPORTANCE = {
    "value": 0.8, "plan": 0.8, "preference": 0.7,
    "insight": 0.6, "fact": 0.5, "event": 0.4,
}
DEFAULT_IMPORTANCE = 0.5
IMPORTANCE_FLOOR = 0.05
MERGE_JACCARD = 0.75          # 字符 shingle(bigram) Jaccard 合并阈值
EVIDENCE_HISTORY_MAX = 10     # evidence_history 最多保留 10 条
EVIDENCE_SNIPPET_MAX = 120    # 每条证据片段最多 120 字
DECAY_PER_DAY = 0.98          # 重要性每天衰减系数
DORMANT_IMPORTANCE = 0.2      # 低于此值且长期未访问 → 休眠
DORMANT_AFTER_DAYS = 180      # 180 天没被访问才休眠


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


def _importance_for(kind: str, item: dict | None = None) -> float:
    """重要性初始化：LLM 给了就用，否则按 kind 兜底。"""
    raw = (item or {}).get("importance")
    try:
        if raw is not None:
            return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        pass
    return float(KIND_IMPORTANCE.get(str(kind or ""), DEFAULT_IMPORTANCE))


def _jaccard(a: str, b: str) -> float:
    """字符 shingle（bigram）Jaccard；空集不判相似。"""
    left, right = _shingles(a), _shingles(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _history_from(evidence: str) -> str:
    snippet = _clean(evidence)[:EVIDENCE_SNIPPET_MAX]
    return json.dumps([snippet], ensure_ascii=False) if snippet else "[]"


def _load_history(value: Any) -> list[str]:
    try:
        data = json.loads(value) if value else []
    except (TypeError, ValueError):
        data = []
    if not isinstance(data, list):
        return []
    out = []
    for raw in data:
        snippet = str(raw or "").strip()[:EVIDENCE_SNIPPET_MAX]
        if snippet and snippet not in out:
            out.append(snippet)
    return out[-EVIDENCE_HISTORY_MAX:]


def _append_history(value: Any, evidence: str) -> str:
    history = _load_history(value)
    snippet = _clean(evidence)[:EVIDENCE_SNIPPET_MAX]
    if snippet and snippet not in history:
        history.append(snippet)
    return json.dumps(history[-EVIDENCE_HISTORY_MAX:], ensure_ascii=False)


def _insert_memory(con, *, mid: str, scope: str, kind: str, content: str,
                   evidence: str, message_id: str | None, confidence: float,
                   status: str, now: float, importance: float | None = None,
                   conflict_group: str | None = None) -> int:
    cur = con.execute(
        "INSERT OR IGNORE INTO user_memories(id,scope,kind,content,evidence,"
        "evidence_message_id,confidence,status,conflict_group,created_at,"
        "importance,access_count,last_accessed,evidence_history) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, scope, kind, content, evidence, message_id, confidence, status,
         conflict_group, now,
         _importance_for(kind) if importance is None else importance,
         0, 0.0, _history_from(evidence)))
    return cur.rowcount


def _merge_existing(con, *, scope: str, kind: str, content: str, evidence: str,
                    confidence: float, importance: float, now: float) -> dict | None:
    """同 scope+kind 的 active 记忆里找完全相同或 Jaccard≥0.75 的，合并并返回结果。

    合并策略：保留最早 id 与原 content（新 content 更长且旧的 <20 字才替换）、
    confidence=max、importance=min(1, max(old,new)+0.05)、access_count+=1、
    evidence 追加进 evidence_history（去重、≤10 条）。
    """
    rows = con.execute(
        "SELECT * FROM user_memories WHERE status='active' AND scope=? AND kind=? "
        "ORDER BY created_at ASC, id ASC LIMIT 200", (scope, kind)).fetchall()
    target = None
    for row in rows:                     # 完全相同优先，且取最早
        if _clean(row["content"]) == content:
            target = row
            break
    if target is None:
        for row in rows:
            if _jaccard(row["content"], content) >= MERGE_JACCARD:
                target = row
                break
    if target is None:
        return None
    old_content = str(target["content"] or "")
    keep_content = old_content
    if len(content) > len(old_content) and len(old_content) < 20:
        keep_content = content
    try:
        old_imp = float(target["importance"])
    except (TypeError, ValueError):
        old_imp = DEFAULT_IMPORTANCE
    new_imp = min(1.0, max(old_imp, importance) + 0.05)
    try:
        old_conf = float(target["confidence"] or 0.0)
    except (TypeError, ValueError):
        old_conf = 0.0
    new_conf = max(old_conf, confidence)
    history = _append_history(target["evidence_history"], evidence)
    access = int(target["access_count"] or 0) + 1
    values = (keep_content, new_conf, new_imp, access, now, history, target["id"])
    try:
        con.execute(
            "UPDATE user_memories SET content=?,confidence=?,importance=?,"
            "access_count=?,last_accessed=?,evidence_history=? WHERE id=?", values)
    except sqlite3.IntegrityError:
        # 新 content 与另一行 UNIQUE(content) 冲突：保留旧 content 也要合并
        values = (old_content, new_conf, new_imp, access, now, history, target["id"])
        con.execute(
            "UPDATE user_memories SET content=?,confidence=?,importance=?,"
            "access_count=?,last_accessed=?,evidence_history=? WHERE id=?", values)
    return {"id": target["id"], "scope": scope, "kind": kind, "content": keep_content,
            "evidence": evidence, "confidence": new_conf, "status": "active",
            "importance": new_imp, "merged": True}



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
            importance = _importance_for(kind, item)
            mid = _id("UM-", content)
            # P1-a：写入时去重合并（只合并 status='active'，dormant/conflict 不参与）
            if status == "active":
                merged = _merge_existing(con, scope=scope, kind=kind, content=content,
                                         evidence=evidence, confidence=conf,
                                         importance=importance, now=now)
                if merged is not None:
                    saved.append(merged)
                    continue
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
                _insert_memory(con, mid=mid, scope=scope, kind=kind, content=content,
                               evidence=evidence, message_id=message_id, confidence=conf,
                               status="conflict", now=now, importance=importance,
                               conflict_group=group)
                con.execute("INSERT INTO conflicts(id,mem_a,mem_b,status,created_at) "
                            "VALUES(?,?,?,?,?)", (group, conflict["id"], mid, "open", now))
                saved.append({"id": mid, "scope": scope, "kind": kind, "content": content,
                              "evidence": evidence, "confidence": conf, "conflict": group})
                continue
            cur_count = _insert_memory(con, mid=mid, scope=scope, kind=kind, content=content,
                                       evidence=evidence, message_id=message_id,
                                       confidence=conf, status=status, now=now,
                                       importance=importance)
            if cur_count:
                saved.append({"id": mid, "scope": scope, "kind": kind, "content": content,
                              "evidence": evidence, "confidence": conf, "status": status,
                              "importance": importance})
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
        count = _insert_memory(con, mid=mid, scope="world", kind="insight",
                               content=content, evidence="[图片]",
                               message_id=message_id, confidence=0.6,
                               status="active", now=time.time())
        con.commit()
        if count:
            return {"id": mid, "scope": "world", "kind": "insight", "content": content,
                    "evidence": "[图片]", "confidence": 0.6,
                    "importance": _importance_for("insight")}
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
    """检索 active 记忆（休眠/失效不参与）。

    P1-a 打分：``0.6*overlap_norm + 0.25*importance + 0.15*recency``，
    recency = exp(-days/180)（days 距 last_accessed，没有则用 created_at）。
    命中后更新 access_count / last_accessed。
    """
    now = time.time()
    con = db.connect(path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM user_memories WHERE status='active' "
            "ORDER BY created_at DESC LIMIT 200")]
        q = _shingles(query)
        q_len = max(1, len(q))
        scored = []
        for row in rows:
            overlap = len(q & _shingles(f"{row['content']} {row['evidence']}"))
            if not overlap and q:
                continue
            try:
                importance = max(0.0, min(1.0, float(row.get("importance") or 0.0)))
            except (TypeError, ValueError):
                importance = DEFAULT_IMPORTANCE
            try:
                last = float(row.get("last_accessed") or 0.0)
            except (TypeError, ValueError):
                last = 0.0
            ref = last or float(row.get("created_at") or now)
            days = max(0.0, (now - ref) / 86400.0)
            recency = 2.718281828459045 ** (-days / 180.0)
            score = (0.6 * (overlap / q_len if q else 0.0)
                     + 0.25 * importance + 0.15 * recency)
            scored.append((score, row))
        scored.sort(key=lambda x: (-x[0], -float(x[1].get("created_at") or 0.0)))
        picked = [row for _, row in scored[:max(1, int(k))]]
        for row in picked:
            con.execute(
                "UPDATE user_memories SET access_count=COALESCE(access_count,0)+1,"
                "last_accessed=? WHERE id=?", (now, row["id"]))
            row["access_count"] = int(row.get("access_count") or 0) + 1
            row["last_accessed"] = now
        con.commit()
    finally:
        con.close()
    return picked


def decay(path: Path | str = DB_PATH, now: float | None = None, *,
          days: float | None = None) -> dict:
    """记忆重要性衰减 + 休眠（不删数据，检索自动排除，可人工恢复）。

    - importance *= 0.98^days（按天，下限 0.05）；只处理 status='active'
    - importance < 0.2 且 180 天没被访问（last_accessed，缺省用 created_at）
      → status='dormant'
    - 用 state 表 ``memory_decay_at`` 记录上次衰减时间，重复调用幂等（按经过天数）
    """
    now = time.time() if now is None else float(now)
    con = db.connect(path)
    try:
        if days is None:
            row = con.execute(
                "SELECT value FROM state WHERE key='memory_decay_at'").fetchone()
            try:
                last = float(row["value"]) if row is not None else 0.0
            except (TypeError, ValueError):
                last = 0.0
            days = max(0.0, (now - last) / 86400.0) if last else 1.0
        days = max(0.0, float(days))
        factor = DECAY_PER_DAY ** days
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM user_memories WHERE status='active'")]
        decayed = 0
        dormant = 0
        for row in rows:
            try:
                old = float(row.get("importance"))
            except (TypeError, ValueError):
                old = DEFAULT_IMPORTANCE
            importance = max(IMPORTANCE_FLOOR, old * factor)
            if importance != old:
                con.execute("UPDATE user_memories SET importance=? WHERE id=?",
                            (importance, row["id"]))
                decayed += 1
            try:
                last_accessed = float(row.get("last_accessed") or 0.0)
            except (TypeError, ValueError):
                last_accessed = 0.0
            ref = last_accessed or float(row.get("created_at") or now)
            idle_days = max(0.0, (now - ref) / 86400.0)
            if importance < DORMANT_IMPORTANCE and idle_days > DORMANT_AFTER_DAYS:
                con.execute("UPDATE user_memories SET status='dormant' WHERE id=?",
                            (row["id"],))
                dormant += 1
        con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) "
                    "VALUES('memory_decay_at',?,?)", (str(now), now))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "days": round(days, 3), "decayed": decayed,
            "dormant": dormant, "ts": now}


def forget(path: Path | str, memory_id: str, reason: str = "user_forget",
           *, redact_evidence: bool = True) -> dict:
    """让一条记忆失效，并尽量从证据消息/派生 claims/检索索引中移除原话。"""
    con = db.connect(path)
    try:
        row = con.execute("SELECT * FROM user_memories WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "记忆不存在"}
        evidence = str(row["evidence"] or "").strip()
        message_id = str(row["evidence_message_id"] or "")
        con.execute("UPDATE user_memories SET status='invalid',conflict_group=NULL WHERE id=?",
                    (memory_id,))
        claim_ids = []
        try:
            for c in con.execute("SELECT id FROM claims WHERE evidence_message_id=?", (message_id,)):
                claim_ids.append(str(c[0]))
            con.execute("UPDATE claims SET status='invalid' WHERE evidence_message_id=?", (message_id,))
        except Exception:  # noqa: BLE001
            pass
        redacted = False
        if redact_evidence and message_id and evidence:
            msg = con.execute("SELECT text FROM messages WHERE id=?", (message_id,)).fetchone()
            if msg is not None and evidence in str(msg["text"] or ""):
                text = str(msg["text"]).replace(evidence, "[已遗忘]")
                con.execute("UPDATE messages SET text=? WHERE id=?", (text, message_id))
                redacted = True
        con.commit()
    finally:
        con.close()
    try:
        from . import search
        search.delete(path, "memory", memory_id)
        if message_id:
            search.delete(path, "message", message_id)
        for cid in ("claim",):
            for claim_id in claim_ids:
                search.delete(path, cid, claim_id)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "id": memory_id, "action": "invalid", "reason": reason,
            "message_id": message_id, "redacted_message": redacted,
            "claims_invalidated": len(claim_ids)}


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


def correct(path: Path | str, memory_id: str, content: str,
            evidence: str = "用户纠正") -> dict[str, Any]:
    """纠错：旧条目失效，新条目带证据写入，并同步检索索引。"""
    new_text = _clean(content)
    if not new_text:
        return {"ok": False, "error": "纠正内容不能为空"}
    if _sensitive(new_text):
        return {"ok": False, "error": "纠正内容包含敏感信息，拒绝写入"}
    con = db.connect(path)
    try:
        old = con.execute("SELECT * FROM user_memories WHERE id=?", (memory_id,)).fetchone()
        if old is None:
            return {"ok": False, "error": "记忆不存在"}
        new_id = _id("UM-", "correct", old["scope"], old["kind"], new_text, time.time())
        now = time.time()
        try:
            importance = float(old["importance"])
        except (TypeError, ValueError, KeyError):
            importance = _importance_for(old["kind"])
        _insert_memory(con, mid=new_id, scope=old["scope"], kind=old["kind"],
                       content=new_text, evidence=str(evidence or "用户纠正"),
                       message_id=old["evidence_message_id"],
                       confidence=float(old["confidence"] or 0.8),
                       status="active", now=now, importance=importance)
        con.execute("UPDATE user_memories SET status='invalid',conflict_group=NULL WHERE id=?",
                    (memory_id,))
        con.commit()
    finally:
        con.close()
    try:
        from . import search
        search.delete(path, "memory", memory_id)
        search.index_texts(path, [{"source_type": "memory", "source_id": new_id, "text": new_text}])
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "old_id": memory_id, "id": new_id, "content": new_text,
            "action": "corrected", "evidence": evidence}


_CORRECTION_CUE = re.compile(r"(记错|记错了|不对|更正|纠正|改成|不是)")
_PAIR = re.compile(r"不是(.{1,60}?)[，,。；; ]*(?:而)?是(.{1,60})")


def apply_correction(path: Path | str, text: str) -> dict[str, Any] | None:
    """识别“记错了，不是A，是B”并更新最相关的一条 active 记忆。"""
    value = _clean(text)
    if not value or not _CORRECTION_CUE.search(value):
        return None
    m = _PAIR.search(value)
    if not m:
        return None
    old_text = _clean(m.group(1))
    new_text = _clean(m.group(2))
    if not old_text or not new_text:
        return None
    con = db.connect(path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM user_memories WHERE status='active' ORDER BY created_at DESC LIMIT 100")]
    finally:
        con.close()
    target = None
    for row in rows:
        content = str(row.get("content") or "")
        if old_text and (old_text in content or content in old_text):
            target = row
            break
    if target is None:
        return {"ok": False, "reason": "no_matching_memory", "old": old_text, "new": new_text}
    result = correct(path, target["id"], new_text, evidence=value)
    result["matched"] = target["content"]
    result["trigger"] = "chat_correction"
    return result
