# -*- coding: utf-8 -*-
"""记忆：由 LLM 自己决定留存什么；用户事实与 Agent 自人格分表。"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import db, timefuzz
from .config import DB_PATH

#: 兼容旧调用方（P1-b 常量仍从 timefuzz 取唯一真源）
WARM_LABEL = timefuzz.WARM_LABEL
COLD_LABEL = timefuzz.COLD_LABEL

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

# --------------------------------------------------------------------------- #
# P1-b：时间分层检索（hot ≤30d / warm 30–90d / cold >90d）
# --------------------------------------------------------------------------- #
TIER_HOT_DAYS = 30.0          # ≤30 天：记得细
TIER_WARM_DAYS = 90.0         # 30–90 天：前阵子，只挑重要的
#: 默认每层预算：最近 4 条 + 前阵子 2 条 + 更早以前 1 条（总 7）
TIER_BUDGET = {"hot": 4, "warm": 2, "cold": 1}
WARM_MIN_IMPORTANCE = 0.35    # warm 无查询命中时的最低重要性门槛
COLD_STABLE_KINDS = frozenset({"value", "preference", "plan"})  # cold 只收稳定事实
COLD_MIN_IMPORTANCE = 0.5     # cold 稳定事实的 importance 门槛
#: 用户明确"翻旧账"的线索：cold 候选放开（仍限稳定事实）、hybrid 老片段豁免
LOOKBACK_CUES = ("以前", "之前", "上次", "好久", "还记得", "记得",
                 "从前", "那时候", "过去", "当初", "回忆")


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
    """最近消息（升序返回）；**没发出去的 assistant 消息不算既成事实**。

    delivered=0 只由 :func:`agi.selflearn.chat.mark_turn_delivery` 在「turn 被明确
    标成 blocked/failed」时写入（例如白名单/STOP/静默拦下，这句话从没到对方手机上）。
    如果读侧仍把它当"我说过"，模型下一轮会顺着一个没发生的对话继续演。
    旧库迁移后 delivered 默认 1，历史消息全部照旧可见（向后兼容红线）。
    """
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM messages "
            "WHERE role <> 'assistant' OR COALESCE(delivered, 1) != 0 "
            "ORDER BY ts DESC,id DESC LIMIT ?", (int(limit),)).fetchall()][::-1]
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


def _age_days(created_at: Any, now: float) -> float:
    try:
        created = float(created_at or now)
    except (TypeError, ValueError):
        created = now
    return max(0.0, (now - created) / 86400.0)


def tier_of(created_at: Any, now: float | None = None) -> str:
    """按创建时间分时间层：``hot`` ≤30 天，``warm`` 30–90 天，``cold`` >90 天。"""
    now = time.time() if now is None else float(now)
    age = _age_days(created_at, now)
    if age <= TIER_HOT_DAYS:
        return "hot"
    if age <= TIER_WARM_DAYS:
        return "warm"
    return "cold"


def wants_lookback(query: Any) -> bool:
    """用户这句话是否明确在"翻旧账"（以前/上次/还记得…）。"""
    text = str(query or "")
    return any(cue in text for cue in LOOKBACK_CUES)


def _tier_budget(budget: Any) -> dict[str, int]:
    result = dict(TIER_BUDGET)
    if isinstance(budget, dict):
        for key in result:
            if key not in budget:
                continue
            try:
                result[key] = max(0, int(budget[key]))
            except (TypeError, ValueError):
                pass
    return result


def retrieve_tiered(path: Path | str = DB_PATH, query: str = "",
                    now: float | None = None,
                    budget: dict | None = None) -> dict:
    """时间分层检索：hot ≤30d / warm 30–90d / cold >90d。

    - hot：全部 active 记忆参与，沿用 P1-a 打分（recency 半衰 180 天）
    - warm：importance ≥0.35 或 query 命中才进候选（recency 半衰 90 天）
    - cold：只收稳定事实（kind ∈ value/preference/plan 且 importance ≥0.5），
      且必须 query 命中；用户明确问「以前/上次/还记得」时允许无命中也取
    - 命中后更新 access_count/last_accessed；dormant/invalid/conflict 全排除
    - 返回 ``{"items": [...], "tiers": {hot,warm,cold}, "dropped": n}``；
      ``tiers`` 是**选中**条数，``dropped`` 是超预算被截断条数
    """
    now = time.time() if now is None else float(now)
    budgets = _tier_budget(budget)
    lookback = wants_lookback(query)
    con = db.connect(path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM user_memories WHERE status='active' "
            "ORDER BY created_at DESC LIMIT 2000")]
        q = _shingles(query)
        q_len = max(1, len(q))
        candidates: dict[str, list[dict]] = {"hot": [], "warm": [], "cold": []}
        for row in rows:
            age = _age_days(row.get("created_at"), now)
            tier = ("hot" if age <= TIER_HOT_DAYS
                    else "warm" if age <= TIER_WARM_DAYS else "cold")
            overlap = len(q & _shingles(
                f"{row.get('content') or ''} {row.get('evidence') or ''}"))
            try:
                importance = max(0.0, min(1.0, float(row.get("importance") or 0.0)))
            except (TypeError, ValueError):
                importance = DEFAULT_IMPORTANCE
            if tier == "warm":
                if importance < WARM_MIN_IMPORTANCE and overlap <= 0:
                    continue
            elif tier == "cold":
                if str(row.get("kind") or "") not in COLD_STABLE_KINDS:
                    continue
                if importance < COLD_MIN_IMPORTANCE:
                    continue
                if overlap <= 0 and not lookback:
                    continue
            half_life = 180.0 if tier == "hot" else 90.0
            recency = math.exp(-age / half_life)
            score = (0.6 * (overlap / q_len if q else 0.0)
                     + 0.25 * importance + 0.15 * recency)
            item = dict(row)
            item.update({"tier": tier, "age_days": round(age, 2),
                         "overlap": overlap, "score": round(score, 6)})
            candidates[tier].append(item)
        items: list[dict] = []
        tiers: dict[str, int] = {}
        dropped = 0
        for tier in ("hot", "warm", "cold"):
            pool = sorted(candidates[tier],
                          key=lambda x: (-x["score"],
                                         -float(x.get("created_at") or 0.0)))
            take = budgets[tier]
            items.extend(pool[:take])
            tiers[tier] = len(pool[:take])
            dropped += max(0, len(pool) - take)
        for item in items:  # 命中即访问：更新 access 统计（P1-a）
            con.execute(
                "UPDATE user_memories SET access_count=COALESCE(access_count,0)+1,"
                "last_accessed=? WHERE id=?", (now, item["id"]))
            item["access_count"] = int(item.get("access_count") or 0) + 1
            item["last_accessed"] = now
        con.commit()
    finally:
        con.close()
    # P1-g：检索计数放内存缓冲，每 20 次或 nightly 才批量落盘——
    # 纯读/无命中的轮次不再为了指标额外写库（每轮对话的开销保持接近 0）。
    hit = any(int(item.get("overlap") or 0) > 0 for item in items)
    _buffer_retrieval(path, items=len(items), hit=hit, dropped=dropped)
    return {"items": items, "tiers": tiers, "dropped": dropped}


def render_memories(items: list[dict] | None, now: float | None = None) -> list[str]:
    """把检索到的记忆渲染成 prompt 行；**只做模糊时间前缀，绝不输出日期/天数**。

    - hot：不加时间前缀（和 P1-a 之前的格式逐字节一致）
    - warm/cold：前缀统一由 :func:`timefuzz.relative` 提供（P1-e 唯一真源）
    """
    now = time.time() if now is None else float(now)
    lines: list[str] = []
    for mem in items or []:
        tier = str(mem.get("tier") or "")
        if tier not in ("hot", "warm", "cold"):
            tier = tier_of(mem.get("created_at"), now)
        if tier == "hot":
            prefix = ""
        else:
            label = timefuzz.relative(mem.get("created_at"), now)
            prefix = f"[{label}] " if label else ""
        lines.append(
            f"- {prefix}[{mem.get('scope', 'user')}/{mem.get('kind', 'fact')}] "
            f"{timefuzz.strip_absolute(mem.get('content', ''))}"
            f"（依据：{timefuzz.strip_absolute(mem.get('evidence', ''))}）")
    return lines


def retrieve(path: Path | str = DB_PATH, query: str = "", k: int = 6) -> list[dict]:
    """兼容旧签名：只取 hot 池（≤30 天），并保持"query 无重叠不返回"的旧语义。

    新调用方请用 :func:`retrieve_tiered`（分层 + 预算 + 时间模糊渲染）。
    """
    result = retrieve_tiered(path, query,
                             budget={"hot": max(1, int(k)), "warm": 0, "cold": 0})
    items = result["items"]
    if query:
        q = _shingles(query)
        if q:
            items = [m for m in items
                     if q & _shingles(f"{m.get('content') or ''} {m.get('evidence') or ''}")]
    return items


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


# --------------------------------------------------------------------------- #
# P1-d：>90 天原文冷归档（只加 archived_at 标记，不删、不改写 content/ts）
# --------------------------------------------------------------------------- #
def archive_cold(path: Path | str = DB_PATH, now: float | None = None, *,
                 days: float = 90, batch: int = 500) -> dict:
    """把超过 ``days`` 天的原始 messages 标记为冷归档（幂等、分批）。

    只写 ``messages.archived_at``：content/text 与 ts **逐字节不变**，永不删除。
    蒸馏层（user_memories / claims / summaries）不归档——它们已是压缩层。
    """
    now = time.time() if now is None else float(now)
    cutoff = now - max(0.0, float(days)) * 86400.0
    size = max(1, int(batch))
    con = db.connect(path)
    try:
        scanned = int(con.execute(
            "SELECT count(*) FROM messages WHERE ts<?", (cutoff,)).fetchone()[0])
        before = int(con.execute(
            "SELECT count(*) FROM messages WHERE archived_at IS NULL AND ts<?",
            (cutoff,)).fetchone()[0])
        while True:
            rows = con.execute(
                "SELECT id FROM messages WHERE archived_at IS NULL AND ts<? LIMIT ?",
                (cutoff, size)).fetchall()
            if not rows:
                break
            ids = [str(row["id"]) for row in rows]
            con.executemany(
                "UPDATE messages SET archived_at=? WHERE id=? AND archived_at IS NULL",
                [(now, mid) for mid in ids])
            con.commit()
            if len(ids) < size:
                break
        remaining = int(con.execute(
            "SELECT count(*) FROM messages WHERE archived_at IS NULL AND ts<?",
            (cutoff,)).fetchone()[0])
    finally:
        con.close()
    return {"ok": True, "archived": before - remaining, "scanned": scanned,
            "remaining": remaining, "cutoff": cutoff, "days": float(days)}


def archive_stats(path: Path | str = DB_PATH, now: float | None = None) -> dict:
    """冷归档指标：总消息数 / 已归档 / 活跃 / 最老活跃天数。"""
    now = time.time() if now is None else float(now)
    con = db.connect(path)
    try:
        total = int(con.execute("SELECT count(*) FROM messages").fetchone()[0])
        archived = int(con.execute(
            "SELECT count(*) FROM messages WHERE archived_at IS NOT NULL").fetchone()[0])
        row = con.execute(
            "SELECT min(ts) AS oldest FROM messages WHERE archived_at IS NULL").fetchone()
        oldest_ts = float(row["oldest"] or 0.0) if row is not None else 0.0
    finally:
        con.close()
    active = total - archived
    oldest_active_days = max(0.0, (now - oldest_ts) / 86400.0) if active and oldest_ts else 0.0
    return {"messages_total": total, "archived": archived, "active": active,
            "oldest_active_days": round(oldest_active_days, 1)}


def export_archive(path: Path | str = DB_PATH, out_path: Path | str = "") -> dict:
    """把已归档 messages 导出 JSONL 冷备（覆盖写，不追加、不删库内数据）。"""
    if not out_path:
        return {"ok": False, "error": "out_path 不能为空"}
    target = Path(out_path)
    con = db.connect(path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id, ts, role, text, archived_at FROM messages "
            "WHERE archived_at IS NOT NULL ORDER BY ts ASC, id ASC").fetchall()]
    finally:
        con.close()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps({
                "id": str(row["id"]), "ts": float(row["ts"]),
                "role": str(row["role"]), "content": str(row["text"]),
                "archived_at": float(row["archived_at"]),
            }, ensure_ascii=False) + "\n")
    try:  # P1-g：记录最近一次冷备导出（只写 state，不动数据）
        con = db.connect(path)
        try:
            now = time.time()
            _state_set(con, "mem_archive_exported_at", now, now)
            _state_set(con, "mem_archive_export_rows", len(rows), now)
            con.commit()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "rows": len(rows), "out": str(target)}


# --------------------------------------------------------------------------- #
# P1-g：记忆指标（让"记忆是否在退化"可观测）
# --------------------------------------------------------------------------- #
def _state_get(con, key: str) -> float | None:
    try:
        row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return None


def _state_set(con, key: str, value: Any, now: float) -> None:
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES(?,?,?)",
                (str(key), str(value), float(now)))


def _state_add(con, key: str, delta: float, now: float) -> None:
    current = _state_get(con, key)
    _state_set(con, key, float((current or 0.0) + float(delta)), now)


#: P1-g 检索计数缓冲：纯读/无命中路径不写盘，每 20 次或 nightly 批量落盘。
_RETRIEVAL_FLUSH_EVERY = 20
_RETRIEVAL_PENDING: dict[str, Any] = {
    "path": "", "calls": 0, "hits": 0, "items": 0, "dropped": 0,
}


def _pending_for(path: Path | str) -> dict[str, int]:
    buf = _RETRIEVAL_PENDING
    if buf.get("path") and str(buf["path"]) != str(path):
        return {"calls": 0, "hits": 0, "items": 0, "dropped": 0}
    return {"calls": int(buf["calls"]), "hits": int(buf["hits"]),
            "items": int(buf["items"]), "dropped": int(buf["dropped"])}


def _buffer_retrieval(path: Path | str, *, items: int, hit: bool,
                      dropped: int = 0) -> None:
    buf = _RETRIEVAL_PENDING
    target = str(path)
    if buf.get("path") and buf["path"] != target:
        flush_retrieval_counters(buf["path"])   # 多库切换：先落旧库
        buf = _RETRIEVAL_PENDING
    buf["path"] = target
    buf["calls"] = int(buf["calls"]) + 1
    buf["hits"] = int(buf["hits"]) + (1 if hit else 0)
    buf["items"] = int(buf["items"]) + max(0, int(items))
    buf["dropped"] = int(buf["dropped"]) + max(0, int(dropped))
    if int(buf["calls"]) >= _RETRIEVAL_FLUSH_EVERY:
        flush_retrieval_counters(target)


def flush_retrieval_counters(path: Path | str | None = None) -> dict:
    """把内存里的检索计数批量写进 state（nightly / 每 20 次调用触发）。

    这样每轮对话不因为指标多写一次库；代价是崩溃时最多丢 19 次计数。
    """
    buf = _RETRIEVAL_PENDING
    target = str(path or buf.get("path") or "")
    flushed = {"calls": int(buf["calls"]), "hits": int(buf["hits"]),
               "items": int(buf["items"]), "dropped": int(buf["dropped"])}
    if not target or not flushed["calls"]:
        return {"ok": True, "flushed": {"calls": 0}}
    try:
        con = db.connect(target)
        try:
            now = time.time()
            _state_add(con, "mem_retrieval_calls", flushed["calls"], now)
            _state_add(con, "mem_retrieval_hits", flushed["hits"], now)
            _state_add(con, "mem_retrieval_items", flushed["items"], now)
            _state_add(con, "mem_retrieval_dropped_stale", flushed["dropped"], now)
            con.commit()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - 指标失败不影响主链
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "flushed": {"calls": 0}}
    for key in ("calls", "hits", "items", "dropped"):
        buf[key] = 0
    return {"ok": True, "flushed": flushed}


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def _count_by(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        name = str(row.get(key) or "unknown")
        out[name] = out.get(name, 0) + 1
    return out


def metrics(path: Path | str = DB_PATH, now: float | None = None) -> dict:
    """记忆层可观测指标（JSON 可序列化；读不出来的字段给 None，不抛错）。

    覆盖：messages 分层/归档、memories 状态/kind/时间层/重要性/合并、claims、
    summaries（月 gist）、检索调用与命中、最近一次 context 预算、归档导出。
    """
    now = time.time() if now is None else float(now)
    result: dict[str, Any] = {
        "ok": True, "now": now,
        "messages": {}, "memories": {}, "claims": {"total": None},
        "summaries": {"total": None, "monthly": None, "latest_month": None},
        "retrieval": {"calls": 0, "hits": 0, "items": 0, "avg_items": None,
                      "hit_rate": None, "dropped_stale": 0},
        "context": {"tokens": None, "dropped": None, "at": None},
        "archive": {"total": None, "exported_at": None, "last_export_rows": None},
    }
    con = db.connect(path)
    try:
        # messages
        try:
            total = int(con.execute("SELECT count(*) FROM messages").fetchone()[0])
            archived = int(con.execute(
                "SELECT count(*) FROM messages WHERE archived_at IS NOT NULL").fetchone()[0])
            row = con.execute(
                "SELECT min(ts) AS oldest FROM messages WHERE archived_at IS NULL").fetchone()
            oldest_ts = float(row["oldest"] or 0.0) if row is not None else 0.0
            row = con.execute("SELECT max(ts) AS newest FROM messages").fetchone()
            newest_ts = float(row["newest"] or 0.0) if row is not None else 0.0
            result["messages"] = {
                "total": total, "archived": archived, "active": total - archived,
                "oldest_active_days": round(max(0.0, (now - oldest_ts) / 86400.0), 1)
                if (total - archived) and oldest_ts else None,
                "newest_days": round(max(0.0, (now - newest_ts) / 86400.0), 1)
                if total and newest_ts else None,
            }
        except Exception:  # noqa: BLE001
            pass
        # memories
        try:
            mem_rows = [dict(r) for r in con.execute("SELECT * FROM user_memories").fetchall()]
            by_status = _count_by(mem_rows, "status")
            by_kind = _count_by(mem_rows, "kind")
            active_rows = [r for r in mem_rows if str(r.get("status")) == "active"]
            tiers = {"hot": 0, "warm": 0, "cold": 0}
            importances: list[float] = []
            merged_30d = 0
            for row in active_rows:
                tiers[tier_of(row.get("created_at"), now)] += 1
                try:
                    importances.append(float(row.get("importance")))
                except (TypeError, ValueError):
                    importances.append(DEFAULT_IMPORTANCE)
                try:
                    history = json.loads(row.get("evidence_history") or "[]")
                except (TypeError, ValueError):
                    history = []
                if isinstance(history, list) and len(history) > 1:
                    ref = row.get("last_accessed") or row.get("created_at") or now
                    try:
                        if now - float(ref) <= 30 * 86400.0:
                            merged_30d += 1
                    except (TypeError, ValueError):
                        pass
            total_mem = len(mem_rows)
            result["memories"] = {
                "total": total_mem,
                "by_status": by_status,
                "by_kind": by_kind,
                "by_tier": tiers,
                "avg_importance": round(sum(importances) / len(importances), 3)
                if importances else None,
                "median_importance": round(_median(importances), 3)
                if importances else None,
                "dormant_rate": round(by_status.get("dormant", 0) / total_mem, 3)
                if total_mem else 0.0,
                "merged_within_30d": merged_30d,
            }
        except Exception:  # noqa: BLE001
            pass
        # claims
        try:
            result["claims"] = {"total": int(
                con.execute("SELECT count(*) FROM claims").fetchone()[0])}
        except Exception:  # noqa: BLE001
            pass
        # summaries
        try:
            total_sum = int(con.execute("SELECT count(*) FROM summaries").fetchone()[0])
            monthly = int(con.execute(
                "SELECT count(*) FROM summaries WHERE kind='monthly'").fetchone()[0])
            row = con.execute(
                "SELECT max(year_month) AS ym FROM summaries WHERE kind='monthly'").fetchone()
            latest_month = str(row["ym"] or "") if row is not None else ""
            result["summaries"] = {"total": total_sum, "monthly": monthly,
                                   "latest_month": latest_month or None}
        except Exception:  # noqa: BLE001
            pass
        # retrieval state counters（含尚未落盘的内存缓冲）
        try:
            pending = _pending_for(path)
            calls = int(_state_get(con, "mem_retrieval_calls") or 0) + pending["calls"]
            hits = int(_state_get(con, "mem_retrieval_hits") or 0) + pending["hits"]
            items = int(_state_get(con, "mem_retrieval_items") or 0) + pending["items"]
            dropped_stale = int(_state_get(con, "mem_retrieval_dropped_stale") or 0) \
                + pending["dropped"]
            result["retrieval"] = {
                "calls": calls, "hits": hits, "items": items,
                "avg_items": round(items / calls, 2) if calls else None,
                "hit_rate": round(hits / calls, 3) if calls else None,
                "dropped_stale": dropped_stale,
                "pending": pending["calls"],
            }
        except Exception:  # noqa: BLE001
            pass
        # last context budget event
        try:
            row = con.execute(
                "SELECT ts, meta_json FROM raw_events WHERE source='context_budget' "
                "ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
            if row is not None:
                try:
                    payload = json.loads(row["meta_json"] or "{}")
                except (TypeError, ValueError):
                    payload = {}
                result["context"] = {
                    "tokens": payload.get("tokens"),
                    "dropped": payload.get("dropped"),
                    "at": float(row["ts"]),
                }
        except Exception:  # noqa: BLE001
            pass
        # archive export
        try:
            result["archive"] = {
                "total": int(con.execute(
                    "SELECT count(*) FROM messages WHERE archived_at IS NOT NULL").fetchone()[0]),
                "exported_at": _state_get(con, "mem_archive_exported_at"),
                "last_export_rows": _state_get(con, "mem_archive_export_rows"),
            }
        except Exception:  # noqa: BLE001
            pass
    finally:
        con.close()
    return result

