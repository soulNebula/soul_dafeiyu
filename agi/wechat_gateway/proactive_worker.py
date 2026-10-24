# -*- coding: utf-8 -*-
"""M5 主动链：把「主动想说」真正接到微信上。

之前的结构性断点（2026-10-24 评估）：
1. `proactive.tick` 只把消息写进 selflearn 的 messages 表，**从不发送**；
2. 网关只被动回复，全仓库 0 处 proactive 调用；
3. 调度在网页的 `setInterval` 里——关掉页面就全停；
4. 主动链跑活动库，真实对话在 per-contact 库，**对空库说话**；
5. 主动消息不建 turn，反馈永远收不回来。

本模块一次解决 1–4；第 5 点由 `proactive.record_proactive_turn` 补 turn，
发送成功后登记到 `gateway.last_turns[contact]`，之后的「反馈 好/差」就能落到该轮。

安全：所有发送仍走 `gateway._send(proactive=True)`，因此 confirm/draft、静默时段、
频率窗口、内容闸门、白名单全部照常生效；`autonomy.gate` 再挡一层。
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any

#: 主动消息收到"负反馈"后冷却多久（秒）；连续两次负反馈再拉长
NEGATIVE_COOLDOWN_S = 24 * 3600
NEGATIVE_REPEAT_COOLDOWN_S = 7 * 24 * 3600
#: 对方多久之前还在说话就不主动（秒）
NEGATIVE_WORDS = ("别发", "别说了", "烦", "不喜欢", "没意思", "闭嘴", "滚", "骚扰", "吵")
POSITIVE_WORDS = ("哈哈", "笑死", "有意思", "喜欢", "好呀", "可以", "在", "嗯")

_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS proactive_events(
  id TEXT PRIMARY KEY,
  contact TEXT NOT NULL,
  ts REAL NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'sent',
  response TEXT,
  response_ts REAL
);
"""


def _db(db_path: Path | str) -> str:
    return str(db_path)


def _ensure_events(db_path: Path | str) -> None:
    con = sqlite3.connect(_db(db_path))
    try:
        con.executescript(_EVENTS_SCHEMA)
        con.commit()
    finally:
        con.close()


def record_event(db_path: Path | str, contact: str, text: str,
                 status: str = "sent", now: float | None = None) -> str:
    now = time.time() if now is None else float(now)
    _ensure_events(db_path)
    eid = "PE-" + hashlib.md5(f"{contact}|{now}|{text[:40]}".encode()).hexdigest()[:14]
    con = sqlite3.connect(_db(db_path))
    try:
        con.execute("INSERT OR REPLACE INTO proactive_events"
                    "(id, contact, ts, text, status) VALUES(?,?,?,?,?)",
                    (eid, str(contact), now, str(text)[:200], status))
        con.commit()
    finally:
        con.close()
    return eid


def mark_response(db_path: Path | str, text: str, now: float | None = None) -> str:
    """收到对方消息时，给最近一条未响应的主动消息标上反应（正/负/中性）。"""
    now = time.time() if now is None else float(now)
    value = str(text or "")
    if not value:
        return ""
    if any(w in value for w in NEGATIVE_WORDS):
        kind = "negative"
    elif any(w in value for w in POSITIVE_WORDS):
        kind = "positive"
    else:
        kind = "neutral"
    try:
        _ensure_events(db_path)
        con = sqlite3.connect(_db(db_path))
    except Exception:  # noqa: BLE001
        return ""
    try:
        row = con.execute(
            "SELECT id FROM proactive_events WHERE response IS NULL "
            "ORDER BY ts DESC LIMIT 1").fetchone()
        if not row:
            return ""
        con.execute("UPDATE proactive_events SET response=?, response_ts=? WHERE id=?",
                    (kind, now, row[0]))
        con.commit()
        return kind
    except Exception:  # noqa: BLE001
        return ""
    finally:
        con.close()


def feedback_cooldown(db_path: Path | str, now: float | None = None) -> str:
    """负反馈冷却：最近 24h 有过 negative → 停一天；7 天内 ≥2 次 → 停一周。"""
    now = time.time() if now is None else float(now)
    try:
        _ensure_events(db_path)
        con = sqlite3.connect(_db(db_path))
    except Exception:  # noqa: BLE001
        return ""
    try:
        negative_day = con.execute(
            "SELECT count(*) FROM proactive_events WHERE response='negative' AND response_ts>=?",
            (now - NEGATIVE_COOLDOWN_S,)).fetchone()[0]
        negative_week = con.execute(
            "SELECT count(*) FROM proactive_events WHERE response='negative' AND response_ts>=?",
            (now - NEGATIVE_REPEAT_COOLDOWN_S,)).fetchone()[0]
        last_negative = con.execute(
            "SELECT max(response_ts) FROM proactive_events WHERE response='negative'").fetchone()[0]
    except Exception:  # noqa: BLE001
        return ""
    finally:
        con.close()
    if negative_week >= 2:
        return "negative_repeat_cooldown"
    if negative_day >= 1 and last_negative and now - float(last_negative) < NEGATIVE_COOLDOWN_S:
        return "negative_cooldown"
    return ""


def default_client():
    from agi.selflearn import llm
    return llm.Client()


def _contacts(gateway, contacts=None) -> list[str]:
    raw = contacts or gateway.cfg.get("proactive_contacts") \
        or gateway.cfg.get("allowed_contacts") or []
    return [str(x) for x in raw if str(x).strip()]


def run_cycle(gateway, *, contacts=None, client=None, now=None) -> dict[str, Any]:
    """对所有（或指定）联系人跑一次主动循环；返回统计，不抛异常。"""
    from agi.selflearn import ticker
    from . import config as gw_cfg

    now = time.time() if now is None else float(now)
    if not gateway.cfg.get("proactive_enabled"):
        return {"ok": True, "ran": False, "reason": "proactive_disabled"}
    contact_list = _contacts(gateway, contacts)
    client = client if client is not None else default_client()
    out: dict[str, Any] = {"ok": True, "ran": True, "contacts": len(contact_list),
                           "sent": 0, "drafts": 0, "skipped": []}
    idle_gap = float(gateway.cfg.get("proactive_idle_s") or 0)
    for contact in contact_list:
        db_path = gw_cfg.contact_db(contact)
        last_in = float(getattr(gateway, "last_inbound", {}).get(contact, 0) or 0)
        if idle_gap > 0 and last_in and now - last_in < idle_gap:
            out["skipped"].append({"contact": contact, "reason": "user_active"})
            continue
        try:
            cooldown = feedback_cooldown(db_path, now)
        except Exception:  # noqa: BLE001
            cooldown = ""
        if cooldown:
            out["skipped"].append({"contact": contact, "reason": cooldown})
            continue
        try:
            tick = ticker.run_once(db_path, client, now=now) or {}
        except Exception as exc:  # noqa: BLE001 - 单个联系人失败不影响其他人
            out["skipped"].append({"contact": contact,
                                   "reason": f"{type(exc).__name__}: {str(exc)[:120]}"})
            continue
        pro = tick.get("proactive") or {}
        if not tick.get("ran") or not pro.get("available"):
            out["skipped"].append({
                "contact": contact,
                "reason": str(tick.get("reason") or pro.get("reason") or "gate")[:80]})
            continue
        bubbles = pro.get("messages") or ([pro.get("message")] if pro.get("message") else [])
        turn_id = str(pro.get("turn_id") or "")
        sent_any = False
        for bubble in bubbles:
            text = str(bubble or "").strip()
            if not text:
                continue
            try:
                status = gateway._send(contact, text, proactive=True)
            except Exception as exc:  # noqa: BLE001
                status = {"ok": False, "status": "failed",
                          "error": f"{type(exc).__name__}: {exc}"}
            gateway._record("proactive_send", {
                "friend": contact, "text": text[:120],
                "status": status.get("status"), "draft_id": status.get("draft_id"),
                "error": str(status.get("error") or "")[:120]})
            try:
                record_event(db_path, contact, text, status.get("status") or "unknown", now)
            except Exception:  # noqa: BLE001
                pass
            if status.get("ok") and status.get("status") != "draft":
                out["sent"] += 1
                sent_any = True
            elif status.get("status") == "draft":
                out["drafts"] += 1
        if sent_any and turn_id:
            # 只有真的发出去了才登记：草稿还没到对方手里，反馈会错位
            gateway.last_turns[contact] = turn_id
    outcome = {"ts": now, "contacts": out["contacts"],
               "sent": out["sent"], "drafts": out["drafts"],
               "skipped": len(out["skipped"])}
    try:
        gateway.last_proactive = outcome
    except Exception:  # noqa: BLE001
        pass
    return out
