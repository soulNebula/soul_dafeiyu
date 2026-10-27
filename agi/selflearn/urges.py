# -*- coding: utf-8 -*-
"""P34：内驱力队列（urges）——让主动不是纯概率，而是攒着「想跟他说/想分享」的欲望。

用户原话：主动不能总是骰子；要**攒着一个想跟你说、想分享的欲望**，有强度、会衰减，
合适时机优先说；被回应就满足，被忽略就慢慢凉。

本模块只做**队列数据 + 触发规则**（纯逻辑；不联网、不发消息）：
- memory 库新表 ``urges``（惰性创建，失败不阻断主链）；
- kind：share_find（冲浪/新闻/视频发现）、share_thought（自己想到的）、
  ask_about（好奇想问）、progress_check（追问进度）、light（就想说一句）；
- 半衰期衰减、去重提强度、队列上限、同日同类上限、top/spend/satisfy/cleanup；
- 触发 helper：冲浪结束 / 兴趣权重上升 / 未来事件 / 心情极值（由调用方接线）。

线程与发送在 ``proactive_worker``（后续接线）；本模块所有函数都 try/except 静默兜底。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

TABLE = "urges"
KINDS = ("share_find", "share_thought", "ask_about", "progress_check", "light")
#: 强度半衰期（小时）：越「一时兴起」凉得越快；progress_check 要等 24h，凉得慢
HALF_LIFE_HOURS = {
    "share_find": 3.0,
    "share_thought": 1.5,
    "ask_about": 2.0,
    "progress_check": 12.0,
    "light": 0.75,
}
QUEUE_MAX = 8                    # 队列上限（超出丢最弱）
SAME_KIND_DAILY_MAX = 3          # 同一天同类最多 push 3 次
DEDUPE_WINDOW_S = 86400.0        # 同 kind+topic 24h 内去重
PROGRESS_DELAY_S = 86400.0       # progress_check 推入后 24h 才允许触发
CLEANUP_AGE_S = 86400.0          # 普通欲望 24h 未用 → 凉掉
CLEANUP_MIN_INTENSITY = 0.05
TOP_MIN_INTENSITY = 0.35

SURF_SIGNALS = ("想看", "有意思", "值得说", "值得一看", "推荐", "分享",
                "太好玩", "震惊", "新鲜", "离谱", "笑死", "没想到",
                # surf.py 的「我的看法」模板里天然带这些“想继续聊”的信号
                "想再翻翻", "没太想过", "有门道", "挺实在的", "先记一笔",
                "回头细看")
#: 未来事件 → progress_check（topic, 匹配词）
EVENT_WORDS = (
    ("面试", "面试"), ("考试", "考试"), ("体检", "体检"), ("搬家", "搬家"),
    ("出差", "出差"), ("下周", "下周的安排"), ("明天要", "明天要做的事"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS urges(
  id TEXT PRIMARY KEY,
  contact TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  topic TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  intensity REAL NOT NULL DEFAULT 0.0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at REAL,
  status TEXT NOT NULL DEFAULT 'active',
  ref_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_urges_status ON urges(status, intensity);
CREATE INDEX IF NOT EXISTS ix_urges_contact ON urges(contact, status);
"""


# ------------------------------------------------------------------ 基础
def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "on", "y"):
        return True
    if value in ("0", "false", "no", "off", "n", ""):
        return False
    return None


def enabled() -> bool:
    value = _env_bool("URGES_ENABLED")
    if value is not None:
        return value
    try:
        from . import config as sl_config
        return bool(getattr(sl_config, "URGES_ENABLED", True))
    except Exception:  # noqa: BLE001
        return True


def contact_of(path: Path | str = DB_PATH, contact: str | None = None) -> str:
    if contact:
        return str(contact)
    return Path(str(path)).stem


def _conn(path: Path | str = DB_PATH):
    con = db.memory_connect(path)
    con.executescript(_SCHEMA)
    con.commit()
    return con


def _row_dict(row) -> dict:
    return dict(row) if row is not None else {}


def _loads(text: Any, default: Any) -> Any:
    try:
        value = json.loads(str(text or ""))
        return value if isinstance(value, (dict, list)) else default
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False,
                          default=str)
    except Exception:  # noqa: BLE001
        return "{}"


def _new_id(contact: str, kind: str, topic: str, now: float) -> str:
    raw = f"{contact}|{kind}|{topic}|{now}".encode("utf-8")
    return "U-" + hashlib.sha1(raw).hexdigest()[:16]


def _day_start(now: float) -> float:
    try:
        local = datetime.fromtimestamp(float(now))
        return datetime(local.year, local.month, local.day).timestamp()
    except (OSError, OverflowError, ValueError):
        # Windows 对 1970 附近/极值时间戳的本地时区换算可能抛 OSError；
        # 退化成滚动 24h 窗口，语义足够（同日同类上限）。
        return float(now) - 86399.0


def _available_at(kind: str, payload: dict, now: float) -> float:
    if kind != "progress_check":
        return 0.0
    try:
        return float(payload.get("available_at") or (now + PROGRESS_DELAY_S))
    except (TypeError, ValueError):
        return now + PROGRESS_DELAY_S


# ------------------------------------------------------------------ 写
def push(path: Path | str = DB_PATH, kind: str = "", topic: str = "",
         payload: dict | None = None, intensity: float = 0.6, *,
         contact: str | None = None, now: float | None = None,
         ref: dict | None = None, source: str = "") -> str | None:
    """往队列里放一个欲望；新 id 或去重后的 id；被上限/同类限制拦下返回 None。"""
    kind = str(kind or "").strip()
    if kind not in KINDS:
        return None
    now = time.time() if now is None else float(now)
    contact = contact_of(path, contact)
    strength = _clamp(intensity, 0.0, 1.0)
    body = dict(payload or {})
    body.setdefault("created_at", now)
    if source:
        body.setdefault("source", source)
    if kind == "progress_check":
        # 24h 后才允许触发：推入时写入 available_at，decay/top 都尊重它
        try:
            body.setdefault("available_at", now + PROGRESS_DELAY_S)
        except Exception:  # noqa: BLE001
            body["available_at"] = now + PROGRESS_DELAY_S
    ref_json = _dumps(ref or {})
    try:
        con = _conn(path)
    except Exception:  # noqa: BLE001
        return None
    try:
        # ① 同一天同类最多 3 次（按本地日期）
        day_count = con.execute(
            "SELECT count(*) FROM urges WHERE contact=? AND kind=? AND created_at>=?",
            (contact, kind, _day_start(now))).fetchone()[0]
        if int(day_count) >= SAME_KIND_DAILY_MAX:
            return None
        # ② 同 contact+kind+topic 24h 内 → 提高强度而非新增
        existing = con.execute(
            "SELECT id,intensity FROM urges WHERE contact=? AND kind=? AND topic=? "
            "AND status='active' AND created_at>=? ORDER BY intensity DESC LIMIT 1",
            (contact, kind, str(topic or "")[:80],
             now - DEDUPE_WINDOW_S)).fetchone()
        if existing is not None:
            new_strength = _clamp(max(float(existing["intensity"]), strength) + 0.1)
            con.execute(
                "UPDATE urges SET intensity=?, updated_at=?, payload_json=?, "
                "ref_json=? WHERE id=?",
                (new_strength, now, _dumps(body), ref_json, existing["id"]))
            con.commit()
            return str(existing["id"])
        # ③ 队列上限 8：满了丢最弱；新欲望比最弱还弱就不进
        active = con.execute(
            "SELECT id,intensity FROM urges WHERE contact=? AND status='active' "
            "ORDER BY intensity ASC", (contact,)).fetchall()
        if len(active) >= QUEUE_MAX:
            weakest = active[0] if active else None
            if weakest is not None and float(weakest["intensity"]) >= strength:
                return None
            if weakest is not None:
                con.execute(
                    "UPDATE urges SET status='dropped', updated_at=? WHERE id=?",
                    (now, weakest["id"]))
        urge_id = _new_id(contact, kind, str(topic or ""), now)
        con.execute(
            "INSERT OR REPLACE INTO urges(id,contact,kind,topic,payload_json,"
            "intensity,created_at,updated_at,attempts,last_attempt_at,status,"
            "ref_json) VALUES(?,?,?,?,?,?,?,?,0,NULL,'active',?)",
            (urge_id, contact, kind, str(topic or "")[:80], _dumps(body),
             strength, now, now, ref_json))
        con.commit()
        return urge_id
    except Exception:  # noqa: BLE001
        return None
    finally:
        con.close()


def decay(path: Path | str = DB_PATH, now: float | None = None) -> int:
    """按 kind 半衰期衰减所有 active 欲望；progress_check 在 available_at 前不衰减。"""
    now = time.time() if now is None else float(now)
    updated = 0
    try:
        con = _conn(path)
    except Exception:  # noqa: BLE001
        return 0
    try:
        rows = con.execute(
            "SELECT * FROM urges WHERE status='active'").fetchall()
        for raw in rows:
            row = _row_dict(raw)
            payload = _loads(row.get("payload_json"), {})
            kind = str(row.get("kind") or "")
            available = _available_at(kind, payload, float(row.get("created_at") or now))
            if kind == "progress_check" and now < available:
                continue
            # progress_check 只从「可用时刻」开始衰减（之前强度原样保留）
            start_ts = float(row.get("updated_at") or now)
            if kind == "progress_check":
                start_ts = max(start_ts, available)
            elapsed_h = max(0.0, now - start_ts) / 3600.0
            half_life = float(HALF_LIFE_HOURS.get(kind, 2.0))
            factor = 0.5 ** (elapsed_h / half_life)
            new_intensity = round(_clamp(float(row.get("intensity") or 0.0) * factor),
                                  4)
            con.execute(
                "UPDATE urges SET intensity=?, updated_at=? WHERE id=?",
                (new_intensity, now, row["id"]))
            updated += 1
        con.commit()
        return updated
    except Exception:  # noqa: BLE001
        return 0
    finally:
        con.close()


def top(path: Path | str = DB_PATH, now: float | None = None,
        min_intensity: float = TOP_MIN_INTENSITY) -> dict | None:
    """最强、且当前可用的欲望（先衰减；progress_check 需到 available_at）。"""
    now = time.time() if now is None else float(now)
    try:
        decay(path, now)
        con = _conn(path)
    except Exception:  # noqa: BLE001
        return None
    try:
        rows = con.execute(
            "SELECT * FROM urges WHERE status='active' AND intensity>=? "
            "ORDER BY intensity DESC, created_at ASC", (float(min_intensity),)).fetchall()
    except Exception:  # noqa: BLE001
        return None
    finally:
        con.close()
    for raw in rows:
        row = _row_dict(raw)
        payload = _loads(row.get("payload_json"), {})
        kind = str(row.get("kind") or "")
        available = _available_at(kind, payload, float(row.get("created_at") or now))
        if kind == "progress_check" and now < available:
            continue
        item = dict(row)
        item["payload"] = payload
        item["ref"] = _loads(row.get("ref_json"), {})
        item["intensity"] = float(row.get("intensity") or 0.0)
        item["age_hours"] = round(max(0.0, now - float(row.get("created_at") or now))
                                  / 3600.0, 2)
        return item
    return None


def spend(path: Path | str = DB_PATH, urge_id: str = "",
          now: float | None = None) -> bool:
    """发送成功：标 spent、attempts+1。"""
    now = time.time() if now is None else float(now)
    try:
        con = _conn(path)
    except Exception:  # noqa: BLE001
        return False
    try:
        cur = con.execute(
            "UPDATE urges SET status='spent', attempts=attempts+1, "
            "last_attempt_at=?, updated_at=? WHERE id=? AND status='active'",
            (now, now, str(urge_id or "")))
        con.commit()
        return bool(cur.rowcount)
    except Exception:  # noqa: BLE001
        return False
    finally:
        con.close()


def satisfy(path: Path | str = DB_PATH, contact: str | None = None,
            now: float | None = None) -> int:
    """用户回复了：把该联系人 active 欲望强度×0.3，并标 spent（被满足）。"""
    now = time.time() if now is None else float(now)
    target = contact_of(path, contact)
    try:
        con = _conn(path)
    except Exception:  # noqa: BLE001
        return 0
    try:
        rows = con.execute(
            "SELECT id,intensity FROM urges WHERE contact=? AND status='active'",
            (target,)).fetchall()
        for raw in rows:
            row = _row_dict(raw)
            con.execute(
                "UPDATE urges SET intensity=?, status='spent', updated_at=? WHERE id=?",
                (round(_clamp(float(row.get("intensity") or 0.0) * 0.3), 4),
                 now, row["id"]))
        con.commit()
        return len(rows)
    except Exception:  # noqa: BLE001
        return 0
    finally:
        con.close()


def cleanup(path: Path | str = DB_PATH, now: float | None = None) -> int:
    """清掉凉透/太老的欲望（intensity<0.05 或 24h 未用；progress_check 等 24h+48h）。"""
    now = time.time() if now is None else float(now)
    try:
        con = _conn(path)
    except Exception:  # noqa: BLE001
        return 0
    dropped = 0
    try:
        rows = con.execute("SELECT * FROM urges WHERE status='active'").fetchall()
        for raw in rows:
            row = _row_dict(raw)
            payload = _loads(row.get("payload_json"), {})
            kind = str(row.get("kind") or "")
            created = float(row.get("created_at") or now)
            intensity = float(row.get("intensity") or 0.0)
            available = _available_at(kind, payload, created)
            expired = False
            if kind == "progress_check":
                if now >= available and now - available > 2 * CLEANUP_AGE_S:
                    expired = True
            elif now - created > CLEANUP_AGE_S:
                expired = True
            if intensity < CLEANUP_MIN_INTENSITY:
                expired = True
            if expired:
                con.execute(
                    "UPDATE urges SET status='dropped', updated_at=? WHERE id=?",
                    (now, row["id"]))
                dropped += 1
        con.commit()
        return dropped
    except Exception:  # noqa: BLE001
        return 0
    finally:
        con.close()


def list_active(path: Path | str = DB_PATH, now: float | None = None,
                limit: int = 5, *, contact: str | None = None) -> list[dict]:
    """队列前 N 条（控制台/调试用），含 age/available 状态。"""
    now = time.time() if now is None else float(now)
    target = contact_of(path, contact)
    try:
        decay(path, now)
        con = _conn(path)
    except Exception:  # noqa: BLE001
        return []
    try:
        rows = con.execute(
            "SELECT * FROM urges WHERE contact=? AND status='active' "
            "ORDER BY intensity DESC, created_at ASC LIMIT ?",
            (target, max(1, int(limit or 5)))).fetchall()
    except Exception:  # noqa: BLE001
        return []
    finally:
        con.close()
    out: list[dict] = []
    for raw in rows:
        row = _row_dict(raw)
        payload = _loads(row.get("payload_json"), {})
        available = _available_at(str(row.get("kind") or ""), payload, now)
        out.append({
            "id": row.get("id"),
            "kind": row.get("kind"),
            "topic": row.get("topic") or "",
            "intensity": round(float(row.get("intensity") or 0.0), 4),
            "attempts": int(row.get("attempts") or 0),
            "age_hours": round(max(0.0, now - float(row.get("created_at") or now))
                               / 3600.0, 2),
            "available": not (row.get("kind") == "progress_check" and now < available),
            "available_in_hours": (round(max(0.0, available - now) / 3600.0, 2)
                                   if available > now else 0.0),
            "payload": payload,
        })
    return out


def status(path: Path | str = DB_PATH, *, contact: str | None = None,
           now: float | None = None) -> dict:
    """给 `GET /api/urges/status?contact=` 的快照。"""
    now = time.time() if now is None else float(now)
    items = list_active(path, now, limit=5, contact=contact)
    return {"enabled": enabled(), "contact": contact_of(path, contact),
            "count": len(items), "items": items, "now": round(now, 3)}


# ------------------------------------------------------------------ 触发 helper
def observe_surf(path: Path | str = DB_PATH, summary: str = "",
                 keywords: list | None = None, items: list | None = None,
                 score: float = 0.0, *, contact: str | None = None,
                 now: float | None = None, ref: dict | None = None) -> str | None:
    """冲浪结束：摘要里有「想看/有意思/值得说」信号或 score 高 → share_find。"""
    now = time.time() if now is None else float(now)
    keywords = [str(word) for word in (keywords or []) if str(word or "").strip()]
    titles = [str((item or {}).get("title") or "") for item in (items or [])]
    text = str(summary or "")
    if not text:
        text = " ".join(titles[:5])
    signal = any(word in text for word in SURF_SIGNALS)
    strength = _clamp(float(score or 0.0))
    if not signal and strength < 0.7:
        return None
    topic = keywords[0] if keywords else (titles[0][:20] if titles else "冲浪发现")
    payload = {
        "summary": text[:400],
        "keywords": keywords[:8],
        "titles": titles[:5],
        "score": round(strength, 3),
    }
    return push(path, "share_find", topic, payload,
                intensity=0.5 + 0.3 * strength, contact=contact, now=now,
                ref=ref or {"surf_summary": text[:120]}, source="surf")


def observe_interest(path: Path | str = DB_PATH, topic: str = "",
                     delta: float = 1.0, *, contact: str | None = None,
                     now: float | None = None) -> str | None:
    """兴趣权重上升 ≥0.1 → share_thought（她最近总在想这个）。"""
    now = time.time() if now is None else float(now)
    topic = str(topic or "").strip()
    try:
        delta_value = float(delta)
    except (TypeError, ValueError):
        return None
    if not topic or delta_value < 0.1:
        return None
    return push(path, "share_thought", topic, {"delta": round(delta_value, 3)},
                intensity=0.4, contact=contact, now=now, source="interests")


def observe_future_event(path: Path | str = DB_PATH, text: str = "", *,
                         contact: str | None = None,
                         now: float | None = None) -> str | None:
    """用户提到未来事件（面试/考试/体检/搬家/出差/下周/明天要）→ progress_check。"""
    now = time.time() if now is None else float(now)
    value = str(text or "")
    if not value:
        return None
    for word, topic in EVENT_WORDS:
        if word in value:
            payload = {"source_text": value[:300], "match": word}
            return push(path, "progress_check", topic, payload, intensity=0.6,
                        contact=contact, now=now, source="user_event")
    return None


def observe_mood(path: Path | str = DB_PATH, mood: float = 0.55, *,
                 contact: str | None = None,
                 now: float | None = None) -> str | None:
    """心情极值（≥0.8 或 ≤0.3）→ light（就想找他说一句）。"""
    now = time.time() if now is None else float(now)
    try:
        value = float(mood)
    except (TypeError, ValueError):
        return None
    if not (value >= 0.8 or value <= 0.3):
        return None
    topic = "心情不错" if value >= 0.8 else "心情有点低"
    return push(path, "light", topic, {"mood": round(value, 3)}, intensity=0.3,
                contact=contact, now=now, source="mood")
