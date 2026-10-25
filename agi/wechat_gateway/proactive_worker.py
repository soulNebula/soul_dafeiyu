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
import random
import re
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
  kind TEXT NOT NULL DEFAULT '',
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
        # 兼容 P0-2 之前创建的旧表：补 kind 列（记录模板分布用）
        columns = {row[1] for row in con.execute("PRAGMA table_info(proactive_events)")}
        if "kind" not in columns:
            con.execute("ALTER TABLE proactive_events ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
        con.commit()
    finally:
        con.close()


def record_event(db_path: Path | str, contact: str, text: str,
                 status: str = "sent", now: float | None = None,
                 kind: str = "") -> str:
    now = time.time() if now is None else float(now)
    _ensure_events(db_path)
    eid = "PE-" + hashlib.md5(f"{contact}|{now}|{text[:40]}".encode()).hexdigest()[:14]
    con = sqlite3.connect(_db(db_path))
    try:
        con.execute("INSERT OR REPLACE INTO proactive_events"
                    "(id, contact, ts, text, status, kind) VALUES(?,?,?,?,?,?)",
                    (eid, str(contact), now, str(text)[:200], status,
                     str(kind or "")[:40]))
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


# --------------------------------------------------------------------------- #
# P0-2：同内容去重 + 同类错误熔断
# --------------------------------------------------------------------------- #
#: 默认阈值（均可被 gateway.cfg 覆盖，见 config.DEFAULTS）
DEDUPE_WINDOW_S = 1800.0
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN_S = 1800.0
#: 熔断状态挂在 gateway 实例上，生命周期与进程一致
BREAKER_STATE_ATTR = "_proactive_breaker"
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_WS_RE = re.compile(r"\s+")
#: 不算"发送失败"的状态（被闸门拦截不是故障；draft 要另有 send_failed 才算，见下）
_NON_FAILURE_STATUS = ("draft", "blocked")
#: 真正"送达对方"的状态（P0-7：必须 ok=True 且 status 属于此集合）。
#: 注意 ``clipboard`` 不在这里——剪贴板兜底只是把内容复制到剪贴板等人工粘贴，
#: **内容已保留但没送达**，既不能计 ``out.sent``，也不能重置熔断。
_DELIVERED_STATUS = frozenset({"sent", "ok"})
#: 内容已保留、未送达的人工协助类兜底状态（单列统计，不进 sent/失败）
_UNDELIVERED_COVERED_STATUS = frozenset({"clipboard"})
#
# 失败率统计口径（P0-6/P0-7，2026-10-25 与 P0-4 对齐）：
#   - status == "failed"  → 失败；
#   - status == "draft"   → **只有 send_failed=True 才算失败**（P0-4 把真实
#     发送失败落成草稿并打这个标记；内容不丢、不重复发）；
#   - status == "draft" 且无 send_failed → confirm 模式的人工草稿，非故障；
#   - status == "blocked" → 内容/静音/限流闸门拦截，非故障；
#   - status == "clipboard"（ok=True）→ 内容已进剪贴板等人工粘贴：**未送达**，
#     不计 sent、不重置熔断、单列 ``out.clipboard``（P0-7）；
#   - 其余未知 status / 非 dict → 保守视为失败。
# 所以「失败率 = failed + draft(send_failed) + clipboard」，其中 sent 只统计
# ``is_delivered()``；真正失败证据是 send_failed 字段（draft 只是落盘形态）；
# 熔断一律按 error_key（P0-4 已写入，缺省回退 normalize_error 前 60 字）计数。


def _cfg_float(gateway, key: str, default: float) -> float:
    raw = gateway.cfg.get(key)
    if raw is None:
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _cfg_int(gateway, key: str, default: int) -> int:
    return int(_cfg_float(gateway, key, default))


def _charset(text: Any) -> set[str]:
    return {ch for ch in str(text or "").strip() if not ch.isspace()}


def text_similar(a: Any, b: Any, threshold: float = 0.8) -> bool:
    """完全相同，或字符集 Jaccard ≥ threshold（≥0.8）视为「高度相似」。"""
    left, right = _charset(a), _charset(b)
    if not left or not right:
        return False
    if str(a or "").strip() == str(b or "").strip():
        return True
    return len(left & right) / len(left | right) >= float(threshold)


def recent_sent_texts(db_path: Path | str, contact: str,
                      since: float) -> list[tuple[float, str]]:
    """取该联系人窗口内**真正发出去过**的文本（status='sent'），新→旧。"""
    try:
        _ensure_events(db_path)
        con = sqlite3.connect(_db(db_path))
    except Exception:  # noqa: BLE001
        return []
    try:
        rows = con.execute(
            "SELECT ts,text FROM proactive_events WHERE contact=? AND ts>=? "
            "AND status='sent' ORDER BY ts DESC",
            (str(contact), float(since))).fetchall()
    except Exception:  # noqa: BLE001
        return []
    finally:
        con.close()
    return [(float(row[0]), str(row[1])) for row in rows]


def find_duplicate(db_path: Path | str, contact: str, text: str, *,
                   now: float, window_s: float) -> tuple[float, str] | None:
    """窗口内是否发过相同/高度相似文本；命中返回 (历史 ts, 历史文本)。"""
    for ts, old in recent_sent_texts(db_path, contact, float(now) - float(window_s)):
        if text_similar(old, text):
            return ts, old
    return None


def normalize_error(text: Any) -> str:
    """错误文本归一化：去掉 0x… 对象地址、压空白，取前 60 字符当 error_key。

    与 task-18（sender.py 的 error_key 字段）同口径；优先用对方已写好的 error_key。
    """
    value = _ADDR_RE.sub("0xADDR", str(text or ""))
    value = _WS_RE.sub(" ", value).strip()
    return value[:60]


def error_key(status: Any) -> str:
    """取一条发送结果里的熔断 key：优先 error_key 字段，否则归一化 error/reason 前 60 字。"""
    if not isinstance(status, dict):
        return normalize_error(status)
    precomputed = str(status.get("error_key") or "").strip()
    if precomputed:
        return precomputed[:60]
    raw = status.get("error") or status.get("reason") or status.get("status") or ""
    return normalize_error(raw)


def is_send_failure(status: Any) -> bool:
    """发送是否算失败（参与熔断计数）。

    P0-6 口径（与 P0-4 的 draft 兜底对齐）：``status == "failed"`` → 失败；
    ``status == "draft"`` 只有 ``send_failed`` 为真才算失败（真失败落草稿）；
    无 ``send_failed`` 的旧 draft（confirm 人工草稿）与 ``blocked`` 属非故障。
    """
    if not isinstance(status, dict):
        return True
    if status.get("ok") is True:
        return False
    state = str(status.get("status") or "")
    if state == "failed":
        return True
    if state == "draft":
        # P0-4：真失败被落成草稿并打 send_failed=True；旧 confirm 草稿没有该键
        return bool(status.get("send_failed"))
    return state not in _NON_FAILURE_STATUS


def send_status(status: Any) -> str:
    """取发送结果的 status 字符串（小写归一）；非 dict 返回空串。"""
    if not isinstance(status, dict):
        return ""
    return str(status.get("status") or "").strip().lower()


def is_delivered(status: Any) -> bool:
    """是否**真正送达对方**（P0-7：ok=True 且 status ∈ {sent, ok}）。

    draft / clipboard / planned / failed 都不算送达。
    """
    return (isinstance(status, dict) and status.get("ok") is True
            and send_status(status) in _DELIVERED_STATUS)


def is_clipboard(status: Any) -> bool:
    """是否剪贴板兜底（内容已复制、等人手动粘贴，**未送达**）。"""
    return (isinstance(status, dict) and status.get("ok") is True
            and send_status(status) in _UNDELIVERED_COVERED_STATUS)


def _breaker_state(gateway) -> dict:
    state = getattr(gateway, BREAKER_STATE_ATTR, None)
    if not isinstance(state, dict):
        state = {}
        try:
            setattr(gateway, BREAKER_STATE_ATTR, state)
        except Exception:  # noqa: BLE001
            pass
    state.setdefault("consecutive", 0)
    state.setdefault("error_key", "")
    state.setdefault("open_until", 0.0)
    return state


def breaker_snapshot(gateway, now: float | None = None) -> dict:
    """只读快照，供 /status 与测试排查用。"""
    state = _breaker_state(gateway)
    now = time.time() if now is None else float(now)
    open_until = float(state.get("open_until") or 0.0)
    return {"open": bool(open_until > now), "consecutive": int(state.get("consecutive") or 0),
            "error_key": str(state.get("error_key") or ""),
            "open_until": open_until, "cooldown_left_s": max(0.0, round(open_until - now, 1))}


def _note_send_failure(gateway, status: Any, *, now: float,
                       threshold: int, cooldown_s: float) -> bool:
    """累计同类连续失败；达到阈值就开熔断并记事件。返回是否刚打开。"""
    state = _breaker_state(gateway)
    key = error_key(status)
    if key == str(state.get("error_key") or ""):
        state["consecutive"] = int(state.get("consecutive") or 0) + 1
    else:
        state["error_key"] = key
        state["consecutive"] = 1
    open_until = float(state.get("open_until") or 0.0)
    if int(state["consecutive"]) >= max(1, int(threshold)) and open_until <= float(now):
        state["open_until"] = float(now) + max(1.0, float(cooldown_s))
        error_text = str(status.get("error") or "") if isinstance(status, dict) else str(status or "")
        status_name = str(status.get("status") or "") if isinstance(status, dict) else ""
        try:
            gateway._record("proactive_breaker_open", {
                "error_key": key,
                "consecutive": int(state["consecutive"]),
                "threshold": int(threshold),
                "cooldown_s": round(max(1.0, float(cooldown_s)), 1),
                "until": round(float(state["open_until"]), 1),
                "error": error_text[:200],
                # P0-6：失败形态证据（真失败看 send_failed，draft 只是落盘形态）
                "status": status_name,
                "send_failed": bool(status.get("send_failed")) if isinstance(status, dict) else False,
                "stage": str(status.get("stage") or "") if isinstance(status, dict) else "",
            })
        except Exception:  # noqa: BLE001
            pass
        return True
    return False


def _note_send_success(gateway) -> None:
    state = _breaker_state(gateway)
    state["consecutive"] = 0
    state["error_key"] = ""
    state["open_until"] = 0.0



def _contacts(gateway, contacts=None) -> list[str]:
    raw = contacts or gateway.cfg.get("proactive_contacts") \
        or gateway.cfg.get("allowed_contacts") or []
    return [str(x) for x in raw if str(x).strip()]


def run_cycle(gateway, *, contacts=None, client=None, now=None) -> dict[str, Any]:
    """对所有（或指定）联系人跑一次主动循环；返回统计，不抛异常。"""
    from agi.selflearn import proactive as selflearn_proactive
    from agi.selflearn import ticker
    from . import config as gw_cfg

    now = time.time() if now is None else float(now)
    until = float(gateway.cfg.get("proactive_until_ts") or 0)
    if until and now >= until:
        gateway.cfg["proactive_enabled"] = False
        try:
            gateway._record("proactive_trial_ended", {"until": until})
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "ran": False, "reason": "trial_ended"}
    if not gateway.cfg.get("proactive_enabled"):
        return {"ok": True, "ran": False, "reason": "proactive_disabled"}
    contact_list = _contacts(gateway, contacts)
    dedupe_window = _cfg_float(gateway, "proactive_dedupe_window_s", DEDUPE_WINDOW_S)
    breaker_threshold = _cfg_int(gateway, "proactive_breaker_threshold", BREAKER_THRESHOLD)
    breaker_cooldown = _cfg_float(gateway, "proactive_breaker_cooldown_s", BREAKER_COOLDOWN_S)
    # P0-2 熔断：冷却期内直接不发送，只记一条 proactive_breaker_open（每轮一次，防刷屏）
    breaker = _breaker_state(gateway)
    open_until = float(breaker.get("open_until") or 0.0)
    if open_until > now:
        try:
            gateway._record("proactive_breaker_open", {
                "error_key": str(breaker.get("error_key") or ""),
                "consecutive": int(breaker.get("consecutive") or 0),
                "open_until": round(open_until, 1),
                "cooldown_left_s": round(open_until - now, 1),
                "contacts": len(contact_list)})
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "ran": False, "reason": "breaker_open",
                "contacts": len(contact_list), "sent": 0, "drafts": 0, "deduped": 0,
                "clipboard": 0, "skipped": [],
                "breaker": breaker_snapshot(gateway, now)}
    if open_until and open_until <= now:
        # 冷却结束：记 close 并清零（下一轮恢复发送）
        try:
            gateway._record("proactive_breaker_close", {
                "error_key": str(breaker.get("error_key") or ""),
                "cooldown_s": round(max(1.0, breaker_cooldown), 1),
                "cooldown_ended_at": round(open_until, 1)})
        except Exception:  # noqa: BLE001
            pass
        breaker["open_until"] = 0.0
        breaker["consecutive"] = 0
        breaker["error_key"] = ""
    # P0-5：概率闸门从「每轮一次」改成「每个联系人独立掷骰」，p 每次在 [min,max] 随机
    client = client if client is not None else default_client()
    out: dict[str, Any] = {"ok": True, "ran": True, "contacts": len(contact_list),
                           "sent": 0, "drafts": 0, "deduped": 0,
                           "clipboard": 0, "breaker_open": False, "skipped": []}
    idle_gap = float(gateway.cfg.get("proactive_idle_s") or 0)
    for contact in contact_list:
        probability = _probability(gateway)
        roll = random.random()
        if roll >= probability:
            # 跳过时不查库、不调 LLM，只记一条带 contact 和本次 p 的日志
            out["skipped"].append({"contact": contact, "reason": "probability",
                                   "probability": round(probability, 4),
                                   "roll": round(roll, 4)})
            try:
                gateway._record("proactive_skipped", {
                    "reason": "probability", "contact": contact,
                    "probability": round(probability, 4),
                    "probability_range": [round(x, 4)
                                          for x in probability_range(gateway)],
                    "roll": round(roll, 4), "contacts": len(contact_list)})
            except Exception:  # noqa: BLE001
                pass
            continue
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
        kind = str(pro.get("kind") or "").strip()
        turn_id = str(pro.get("turn_id") or "")
        # P0-3 出站文本质检：补句末标点/合并空白/超长按标点切分（带 kind 的新主动消息才处理，
        # 无 kind 的旧 payload 原样透传，保持向后兼容）。
        outbound: list[str] = []
        for bubble in bubbles:
            raw = str(bubble or "").strip()
            if not raw:
                continue
            if not kind:
                outbound.append(raw)
                continue
            polished = selflearn_proactive.polish_outbound(raw)
            if not polished:
                continue
            if "\n" in polished:  # 超长多句：逐段发送，避免一整段粘连
                outbound.extend(part.strip() for part in polished.split("\n")
                                if part.strip())
            else:
                outbound.append(polished)
        sent_any = False
        for text in outbound:
            # P0-2 同内容去重：窗口内发过相同/高度相似文本 → 跳过并记事件
            try:
                duplicate = find_duplicate(db_path, contact, text, now=now,
                                           window_s=dedupe_window)
            except Exception:  # noqa: BLE001 - 去重查询失败不阻塞发送
                duplicate = None
            if duplicate:
                hit_ts, hit_text = duplicate
                out["deduped"] += 1
                try:
                    gateway._record("proactive_deduped", {
                        "friend": contact, "text": text[:120], "intent_kind": kind,
                        "hit_ts": round(float(hit_ts), 1),
                        "hit_text": str(hit_text)[:120],
                        "window_s": round(float(dedupe_window), 1)})
                except Exception:  # noqa: BLE001
                    pass
                continue
            try:
                status = gateway._send(contact, text, proactive=True)
            except Exception as exc:  # noqa: BLE001
                status = {"ok": False, "status": "failed",
                          "error": f"{type(exc).__name__}: {exc}"}
            gateway._record("proactive_send", {
                "friend": contact, "text": text[:120], "intent_kind": kind,
                "status": status.get("status"), "draft_id": status.get("draft_id"),
                # P0-6：失败率统计口径 status∈{failed,draft}，真正失败证据看 send_failed
                "send_failed": bool(status.get("send_failed")),
                "error_key": str(status.get("error_key") or "")[:120],
                "stage": str(status.get("stage") or "")[:40],
                # P0-7：clipboard（未送达）也带 warning，统计时单列
                "warning": str(status.get("warning") or "")[:120],
                "error": str(status.get("error") or "")[:120]})
            try:
                record_event(db_path, contact, text, status.get("status") or "unknown",
                             now, kind=kind)
            except Exception:  # noqa: BLE001
                pass
            # P0-7：clipboard 兜底 = 内容进剪贴板、没送达：不计 sent、不重置熔断，单列统计。
            if is_clipboard(status):
                out["clipboard"] += 1
                try:
                    gateway._record("proactive_clipboard", {
                        "friend": contact, "text": text[:120], "intent_kind": kind,
                        "status": status.get("status"),
                        "method": str(status.get("method") or "")[:40],
                        "verified": bool(status.get("verified", False)),
                        "warning": str(status.get("warning") or "")[:200]})
                except Exception:  # noqa: BLE001
                    pass
            # P0-6 顺序很重要：draft+send_failed 先按「失败」处理（参与熔断），
            # 再顺便记一次 drafts（内容已落草稿）；否则永远进不了熔断分支。
            elif is_send_failure(status):
                if status.get("status") == "draft":
                    out["drafts"] += 1  # 真失败落草稿：内容保住但仍算失败
                if _note_send_failure(gateway, status, now=now,
                                      threshold=breaker_threshold,
                                      cooldown_s=breaker_cooldown):
                    out["breaker_open"] = True
                    break  # 熔断后本轮不再发剩余气泡/联系人
            elif is_delivered(status):
                out["sent"] += 1
                sent_any = True
                _note_send_success(gateway)  # 只有真正送达才清零
            elif status.get("status") == "draft":
                out["drafts"] += 1  # confirm 人工草稿（非故障，不影响熔断）
        if sent_any and turn_id:
            # 只有真的发出去了才登记：草稿还没到对方手里，反馈会错位
            gateway.last_turns[contact] = turn_id
        if out["breaker_open"]:
            break
    outcome = {"ts": now, "contacts": out["contacts"],
               "sent": out["sent"], "drafts": out["drafts"],
               "deduped": out["deduped"], "clipboard": out["clipboard"],
               "breaker": breaker_snapshot(gateway, now),
               "skipped": len(out["skipped"])}
    try:
        gateway.last_proactive = outcome
    except Exception:  # noqa: BLE001
        pass
    # 兼容旧返回语义：整轮所有联系人都被概率闸门跳过 → ran=False/reason=probability_skip
    probability_skips = [s for s in out["skipped"] if s.get("reason") == "probability"]
    if (probability_skips and out["skipped"] and not out["sent"] and not out["drafts"]
            and not out["deduped"] and not out["clipboard"]
            and all(s.get("reason") == "probability" for s in out["skipped"])):
        last_skip = probability_skips[-1]
        out.update({"ran": False, "reason": "probability_skip",
                    "probability": last_skip.get("probability"),
                    "roll": last_skip.get("roll")})
    return out


def _bounds(gateway) -> tuple[float, float]:
    """检查间隔上下界（秒）。默认 60–7200 秒（1–120 分钟）。"""
    try:
        lo = float(gateway.cfg.get("proactive_interval_min_s") or 60)
    except (TypeError, ValueError):
        lo = 60.0
    try:
        hi = float(gateway.cfg.get("proactive_interval_max_s") or 7200)
    except (TypeError, ValueError):
        hi = 7200.0
    lo = max(10.0, lo)
    hi = max(lo, hi)
    return lo, hi


def probability_range(gateway) -> tuple[float, float]:
    """当前配置的发送概率区间（旧键 `proactive_probability` 当固定值兼容）。"""
    min_raw = gateway.cfg.get("proactive_probability_min")
    max_raw = gateway.cfg.get("proactive_probability_max")
    legacy = gateway.cfg.get("proactive_probability")
    if min_raw is None and max_raw is None:
        if legacy is None:
            return 1.0, 1.0
        try:
            value = max(0.0, min(1.0, float(legacy)))
        except (TypeError, ValueError):
            value = 1.0
        return value, value
    try:
        lo = max(0.0, min(1.0, float(min_raw))) if min_raw is not None else 0.3
    except (TypeError, ValueError):
        lo = 0.3
    try:
        hi = max(0.0, min(1.0, float(max_raw))) if max_raw is not None else 0.9
    except (TypeError, ValueError):
        hi = 0.9
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _probability(gateway) -> float:
    """每轮检查真正发送的概率（0–1），**每轮在 [min, max] 内随机采**。

    - 默认配置：``uniform(0.3, 0.9)``（2026-10-25 用户拍板，替代固定 0.3）；
    - 旧配置只有 ``proactive_probability``：当固定值用；
    - 三个键都没有（旧测试/旧库）：返回 1.0，保持向下兼容（不引入随机闸门）。
    """
    lo, hi = probability_range(gateway)
    if lo == hi:
        return lo
    return random.uniform(lo, hi)


def next_interval(gateway, *, client=None, last_outcome=None,
                  now=None) -> float:
    """下一次轮询间隔：**LLM 自己定**（默认 60–7200 秒 = 1–120 分钟），
    失败/不可用时随机；最终再乘一个 0.85–1.15 的抖动，避免机械节奏。"""
    lo, hi = _bounds(gateway)
    client = client if client is not None else default_client()
    idea = 0
    try:
        if client is not None and getattr(client, "available", lambda: False)():
            prompt = (
                "你是微信聊天里的普通人。刚结束一次主动查看，现在决定多久后再看一次消息。"
                f"只输出一个 {int(lo)} 到 {int(hi)} 的整数（单位：秒）。"
                "刚说过话就等久一点，心情活跃就看勤一点。"
                f"上一次结果：{str(last_outcome or {})[:160]}"
            )
            r = client.chat(
                [{"role": "system", "content": "只输出一个整数，不要解释。"},
                 {"role": "user", "content": prompt}],
                purpose="proactive_schedule", max_tokens=8, temperature=1.0)
            match = re.search(r"\d+", str((r or {}).get("text") or ""))
            if match:
                idea = int(match.group(0))
    except Exception:  # noqa: BLE001
        idea = 0
    if idea <= 0:
        idea = random.randint(int(lo), int(hi))
    idea = int(max(lo, min(hi, idea)))
    delay = idea * random.uniform(0.85, 1.15)
    return round(max(lo, min(hi, delay)), 1)
