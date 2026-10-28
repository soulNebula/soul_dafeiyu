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
import time
from pathlib import Path
from typing import Any

from agi.selflearn import db as _selflearn_db
from agi.selflearn import pacing
from agi.selflearn import initiative
from agi.selflearn import urges

from . import sendclass

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


def _connect(db_path: Path | str):
    """P7-f：事件表在 memory 域，统一走 selflearn db 层，禁止打开基名文件。"""
    return _selflearn_db.connect(db_path)


def _ensure_events(db_path: Path | str) -> None:
    con = _connect(db_path)
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
    con = _connect(db_path)
    try:
        con.execute("INSERT OR REPLACE INTO proactive_events"
                    "(id, contact, ts, text, status, kind) VALUES(?,?,?,?,?,?)",
                    (eid, str(contact), now, str(text)[:200], status,
                     str(kind or "")[:40]))
        con.commit()
    finally:
        con.close()
    return eid


def last_sent_ts(db_path: Path | str, contact: str) -> float:
    """最近一条 status=sent 的主动消息时间戳（没有返回 0）。"""
    try:
        con = _connect(db_path)
        try:
            row = con.execute(
                "SELECT ts FROM proactive_events WHERE contact=? AND status='sent' "
                "ORDER BY ts DESC LIMIT 1", (str(contact),)).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - 表/库异常都当没有
        return 0.0


def recent_kind_sent(db_path: Path | str, contact: str, kind: str,
                     since: float) -> float | None:
    """窗口内是否发过同类主动消息；命中返回最近一次的 ts（用于同类追问冷却）。"""
    try:
        _ensure_events(db_path)
        con = _connect(db_path)
    except Exception:  # noqa: BLE001
        return None
    try:
        row = con.execute(
            "SELECT max(ts) FROM proactive_events WHERE contact=? AND kind=? AND ts>=? "
            "AND status NOT IN ('failed','blocked','draft')",
            (str(contact), str(kind), float(since))).fetchone()
        return float(row[0]) if row and row[0] else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        con.close()



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
        con = _connect(db_path)
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
        con = _connect(db_path)
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
# P7-b/backlog：发送结果统一口径在 :mod:`agi.wechat_gateway.sendclass`（唯一真源）。
# 计数口径（与 /status 一致）：
#   delivered  → out.sent +1，重置熔断；
#   failed     → 参与熔断计数，draft 落盘另计 out.drafts；
#   draft      → out.drafts（confirm 人工草稿，非故障）；
#   clipboard  → out.clipboard（内容保留、未送达，不计 sent、不重置熔断）；
#   unverified → out.unverified（ok=True 但无送达证据，如窗口兜底 verified=False：
#                不计 sent、不重置熔断）；
#   planned    → out.planned（dry-run 未发生，单列）；
#   blocked    → 非故障、非送达，不计数（send_blocked 日志已记）。


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
        con = _connect(db_path)
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
    """发送是否算失败（参与熔断计数）——委托 sendclass 统一口径。"""
    return sendclass.is_failure(status)


def send_status(status: Any) -> str:
    """取发送结果的 status 字符串（小写归一）；非 dict 返回空串。"""
    return sendclass.send_status(status)


def is_delivered(status: Any) -> bool:
    """是否**真正送达对方**（sendclass.delivered：ok=True + verified 不为 False）。"""
    return sendclass.is_delivered(status)


def is_clipboard(status: Any) -> bool:
    """是否剪贴板兜底（内容已复制、等人手动粘贴，**未送达**）。"""
    return sendclass.classify_send_result(status) == sendclass.CLIPBOARD


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


def _record_video_share(gateway, contact: str, pro: dict, *, delivered: bool,
                        media_sent: int = 0, text: str = "") -> bool:
    """P12-C：视频分享的发放记录（bvid/value/封面/结果）；送达后标记已分享。"""
    video = (pro or {}).get("video_share") or {}
    bvid = str(video.get("bvid") or "").strip()
    marked = False
    if delivered and bvid:
        try:
            from agi.integrations import bili_ingest
            marked = bool(bili_ingest.mark_shared(bvid))
        except Exception:  # noqa: BLE001 - 标记失败不影响发送结果
            marked = False
    try:
        gateway._record("proactive_video_share", {
            "friend": contact, "bvid": bvid,
            "title": str(video.get("title") or "")[:80],
            "value": video.get("value"),
            "share_line": str(video.get("share_line") or "")[:60],
            "cover_sent": bool(video.get("cover_sent")),
            "media_sent": int(media_sent or 0),
            "delivered": bool(delivered), "marked_shared": bool(marked),
            "text": str(text or "")[:120]})
    except Exception:  # noqa: BLE001
        pass
    return marked


def _urge_text(urge: dict) -> str:
    """P34：把一条欲望变成一句可发的话（简单、可离线；复杂文案仍走原生成链）。"""
    kind = str((urge or {}).get("kind") or "")
    topic = str((urge or {}).get("topic") or "").strip()
    payload = (urge or {}).get("payload") or {}
    try:
        from agi.selflearn import proactive as _pro
        if kind == "light":
            return str(_pro._safe_fallback("light"))[:60]
        if kind == "progress_check":
            anchor_text = topic or str(payload.get("source_text") or "")[:20]
            return str(_pro._safe_fallback("progress_check", anchor=anchor_text))[:120]
        if kind in ("share", "share_thought"):
            return str(_pro._safe_fallback("share", topic=topic or "最近的事"))[:120]
    except Exception:  # noqa: BLE001
        pass
    if kind == "ask_about":
        return f"想问你个事：{topic}"[:120]
    if kind == "share_find":
        summary = str(payload.get("summary") or topic or "刚看到的东西").strip()
        return f"刚看到{summary[:80]}，挺想跟你说说"[:160]
    if kind == "progress_check":
        return f"你之前说的{topic}，后来弄到哪一步了？"[:120]
    if kind == "light":
        return "在吗"
    return ""


def _urge_penalize(db_path, urge_id: str, *, now: float,
                   factor: float = 0.5) -> float:
    """发送失败：强度 ×factor，attempts+1（不 spend）。返回新强度（失败 0）。"""
    try:
        from agi.selflearn import urges as _urges
        con = _urges._conn(db_path)
        try:
            row = con.execute("SELECT intensity FROM urges WHERE id=?",
                              (str(urge_id or ""),)).fetchone()
            if row is None:
                return 0.0
            value = max(0.0, float(row[0] or 0.0) * float(factor))
            con.execute("UPDATE urges SET intensity=?, attempts=attempts+1, "
                        "last_attempt_at=?, updated_at=? WHERE id=?",
                        (round(value, 4), now, now, str(urge_id or "")))
            con.commit()
            return round(value, 4)
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return 0.0


def run_cycle(gateway, *, contacts=None, client=None, now=None) -> dict[str, Any]:
    """对所有（或指定）联系人跑一次主动循环；返回统计，不抛异常。"""
    from agi.selflearn import proactive as selflearn_proactive
    from agi.selflearn import ticker
    from . import config as gw_cfg

    now = time.time() if now is None else float(now)
    # P0-22：每轮先重读配置（真实 Gateway 才开；mtime 变了才重载，读失败沿用旧配置）
    if getattr(gateway, "_config_hot_reload", False):
        try:
            loaded, changes, warning = gw_cfg.hot_reload(gateway.cfg)
        except Exception as exc:  # noqa: BLE001
            loaded, changes, warning = dict(gateway.cfg), {}, f"{type(exc).__name__}: {exc}"
        if warning:
            try:
                gateway._record("config_reload_failed", {"error": str(warning)[:200]})
            except Exception:  # noqa: BLE001
                pass
        elif changes:
            try:
                gateway._record("config_reloaded", {"changed": sorted(changes),
                                                    "changes": changes})
            except Exception:  # noqa: BLE001
                pass
            try:
                gateway.cfg.clear()
                gateway.cfg.update(loaded)
            except Exception:  # noqa: BLE001
                pass
    # P0-22b：紧急停止开关是最优先判断——STOP 文件存在则本轮不发任何消息。
    # 路径必须是「注入后的」：优先 Gateway.stop_present()（显式 stop_path 或
    # config.stop_path()，conftest 已把生产路径隔离到 tmp）；轻量 FakeGateway
    # 没有该方法时才回退模块级 config.stopped()（同样走隔离后的 stop_path）。
    try:
        checker = getattr(gateway, "stop_present", None)
        stop_file = bool(checker()) if callable(checker) else bool(gw_cfg.stopped())
    except Exception:  # noqa: BLE001
        stop_file = False
    if stop_file:
        try:
            gateway._log_stopped(now)
        except AttributeError:
            try:
                gateway._record("proactive_stopped", {"reason": "stop_file", "ts": now})
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "ran": False, "reason": "stopped", "stopped": True,
                "contacts": 0, "sent": 0, "drafts": 0, "deduped": 0,
                "clipboard": 0, "skipped": []}
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
                           "clipboard": 0, "unverified": 0, "planned": 0,
                           "media_sent": 0,
                           "initiative": None, "daily_cap": None,
                           "today_sent": 0, "initiative_suppressed": False,
                           "urge": None, "failed_any": False,
                           "breaker_open": False, "skipped": []}
    idle_gap = float(gateway.cfg.get("proactive_idle_s") or 0)
    media_sent = 0
    # P33：上一轮发送失败 → 本轮 drive 不叠加（回退 pacing/固定概率）
    suppress_drive = bool(getattr(gateway, "_initiative_prev_fail", False))
    out["initiative_suppressed"] = suppress_drive
    for contact in contact_list:
        db_path = gw_cfg.contact_db(contact)
        # P34：每轮先衰减/清理内驱力队列（失败静默，不影响发送）
        if gateway.cfg.get("urges_enabled", True):
            try:
                urges.decay(db_path, now)
                urges.cleanup(db_path, now)
            except Exception:  # noqa: BLE001
                pass
        # P33-补：按当天 user 消息数动态算日上限（旧 pacing_daily_cap 兼容成下限）
        cap_info = initiative.daily_cap(db_path, now, config=gateway.cfg)
        daily_cap = int(cap_info["daily_cap"])
        today_sent = int(initiative.today_sent(db_path, now))
        out["daily_cap"] = daily_cap
        out["today_sent"] = today_sent
        if daily_cap > 0 and today_sent >= daily_cap:
            out["skipped"].append({
                "contact": contact, "reason": "daily_cap_dynamic",
                "cap": daily_cap, "used": today_sent})
            try:
                gateway._record("daily_cap_hit", {
                    "contact": contact, "cap": daily_cap, "used": today_sent,
                    "user_msgs_today": cap_info.get("user_msgs_today")})
            except Exception:  # noqa: BLE001
                pass
            continue
        round_cfg = dict(gateway.cfg)
        round_cfg["pacing_daily_cap"] = daily_cap      # pacing.adjust 用动态上限
        # P16-B：按近 7 天「用户主动 vs 它主动」对等度调整本轮调度（不写 config 文件）。
        pacing_adjustment: dict[str, Any] | None = None
        balance_result: dict[str, Any] | None = None
        if pacing.enabled(gateway.cfg):
            try:
                balance_result = pacing.balance(
                    db_path, now, contact=contact,
                    log_path=getattr(gateway, "log_path", None))
                pacing_adjustment = pacing.adjust(
                    round_cfg, balance_result, now=now,
                    stopped=bool(gateway.cfg.get("stop")))
                out["pacing"] = dict(pacing.summary(balance_result, pacing_adjustment),
                                     contact=contact)
                try:
                    gateway._record("pacing_adjusted", {
                        "contact": contact, **out["pacing"],
                        "target_ratio": pacing_adjustment.get("target_ratio"),
                        "daily_cap": daily_cap, "today_sent": today_sent})
                except Exception:  # noqa: BLE001
                    pass
                if not pacing_adjustment.get("send", True):
                    out["skipped"].append({
                        "contact": contact,
                        "reason": "pacing:" + str(pacing_adjustment.get("reason") or "hold")})
                    continue
            except Exception:  # noqa: BLE001 - pacing 失败不影响主链，回退旧概率
                pacing_adjustment = None
        # 2026-10-29：连续 3 条主动消息用户都没回应 → 暂停 6 小时（别自言自语刷屏）。
        try:
            silent_streak = int((pacing_adjustment or {}).get("unanswered_streak") or 0)
        except (TypeError, ValueError):
            silent_streak = 0
        cooldown_s = _cfg_float(gateway, "proactive_no_response_cooldown_s", 6 * 3600)
        if silent_streak >= 3:
            last_sent = last_sent_ts(db_path, contact)
            if last_sent and float(now) - last_sent < cooldown_s:
                out["skipped"].append({"contact": contact,
                                       "reason": "no_response_cooldown",
                                       "streak": silent_streak})
                try:
                    gateway._record("proactive_no_response_cooldown", {
                        "friend": contact, "streak": silent_streak,
                        "last_sent_ts": round(last_sent, 1),
                        "cooldown_s": round(cooldown_s, 1)})
                except Exception:  # noqa: BLE001
                    pass
                continue
        # P33：主动度（关系熟悉度×心情×信任）→ 概率↑/间隔↓；与 pacing 取更保守
        drive_info: dict[str, Any] | None = None
        init_adjustment: dict[str, Any] | None = None
        if "initiative_enabled" in gateway.cfg and initiative.enabled(gateway.cfg) \
                and not suppress_drive:
            try:
                drive_info = initiative.drive(db_path, now)
                init_adjustment = initiative.adjust(
                    round_cfg,
                    {**drive_info,
                     "today_sent": today_sent, "daily_cap": daily_cap,
                     "unanswered_streak": int((balance_result or {}).get(
                         "unanswered_streak") or 0)},
                    now=now, stopped=bool(gateway.cfg.get("stop")))
                if not init_adjustment.get("send", True):
                    out["skipped"].append({
                        "contact": contact,
                        "reason": "initiative:" + str(init_adjustment.get("reason") or "hold")})
                    try:
                        gateway._record("initiative_hold", {
                            "contact": contact,
                            "reason": init_adjustment.get("reason"),
                            "drive": init_adjustment.get("drive"),
                            "daily_cap": daily_cap, "today_sent": today_sent})
                    except Exception:  # noqa: BLE001
                        pass
                    continue
            except Exception:  # noqa: BLE001 - drive 失败不影响主链
                drive_info = None
                init_adjustment = None
        adjustments = [item for item in (pacing_adjustment, init_adjustment) if item]
        if adjustments:
            probability = min(float(item.get("probability") or 1.0)
                              for item in adjustments)
            lo = max(float(item.get("interval_min_s") or 0.0) for item in adjustments)
            hi = max(float(item.get("interval_max_s") or 0.0) for item in adjustments)
            lo = max(60.0, lo)
            hi = max(lo, hi)
            final_source = "pacing+initiative" if len(adjustments) == 2 else (
                "pacing" if pacing_adjustment else "initiative")
            out["pacing_suggested"] = {"probability": round(probability, 4),
                                       "interval_min_s": int(round(lo)),
                                       "interval_max_s": int(round(hi))}
        else:
            probability = _probability(gateway)
            final_source = "legacy"
        if init_adjustment is not None:
            out["initiative"] = {
                "contact": contact,
                "familiarity": init_adjustment.get("familiarity"),
                "mood": init_adjustment.get("mood"),
                "trust": init_adjustment.get("trust"),
                "drive": init_adjustment.get("drive"),
                "drive_level": initiative.drive_level(init_adjustment.get("drive")),
                "reason": init_adjustment.get("reason"),
                "conservative": init_adjustment.get("conservative"),
                "probability": init_adjustment.get("probability"),
                "interval_min_s": init_adjustment.get("interval_min_s"),
                "interval_max_s": init_adjustment.get("interval_max_s"),
                "daily_cap": daily_cap, "today_sent": today_sent,
                "final_source": final_source,
                "combined_probability": round(float(probability), 4),
                "suppressed": bool(suppress_drive)}
            try:
                gateway._record("initiative_adjusted", dict(out["initiative"]))
            except Exception:  # noqa: BLE001
                pass
        # P34：内驱力优先——drive≥0.35 且不在静默/日上限（init_adjustment 已校验）
        urge_info = None
        if gateway.cfg.get("urges_enabled", True) and drive_info is not None \
                and float(drive_info.get("drive") or 0.0) >= 0.35 \
                and not suppress_drive and init_adjustment is not None \
                and init_adjustment.get("send", True):
            try:
                urge_info = urges.top(db_path, now)
            except Exception:  # noqa: BLE001
                urge_info = None
        if urge_info:
            content = _urge_text(urge_info)
            urge_status: dict[str, Any] = {}
            if content:
                try:
                    urge_status = gateway._send(contact, content, proactive=True)
                except Exception as exc:  # noqa: BLE001
                    urge_status = {"ok": False, "status": "failed",
                                   "error": f"{type(exc).__name__}: {exc}"}
            urge_class = sendclass.classify_send_result(urge_status)
            base_event = {
                "contact": contact,
                "kind": str(urge_info.get("kind") or ""),
                "topic": str(urge_info.get("topic") or "")[:80],
                "intensity": float(urge_info.get("intensity") or 0.0),
                "attempts": int(urge_info.get("attempts") or 0) + 1,
                "text": str(content or "")[:120],
                "status": urge_status.get("status"),
                "classification": urge_class,
                "drive": float(drive_info.get("drive") or 0.0),
            }
            if content and urge_class == sendclass.DELIVERED:
                try:
                    urges.spend(db_path, str(urge_info.get("id") or ""), now)
                except Exception:  # noqa: BLE001
                    pass
                out["sent"] += 1
                out["urge"] = dict(base_event, spent=True)
                try:
                    gateway._record("urge_fired", out["urge"])
                except Exception:  # noqa: BLE001
                    pass
            else:
                new_intensity = _urge_penalize(
                    db_path, str(urge_info.get("id") or ""), now=now)
                out["urge"] = dict(base_event, spent=False,
                                   intensity_after=new_intensity,
                                   error=str(urge_status.get("error") or "")[:120])
                out["failed_any"] = True
                try:
                    gateway._record("urge_failed", out["urge"])
                except Exception:  # noqa: BLE001
                    pass
            continue
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
            if drive_info:
                # P33：drive 透传给候选选择；旧测试 monkeypatch 的 run_once 可能
                # 只接受 (path, client, now)，无 drive 时保持旧调用形状。
                tick = ticker.run_once(db_path, client, now=now,
                                       drive=float(drive_info.get("drive"))) or {}
            else:
                tick = ticker.run_once(db_path, client, now=now) or {}
        except Exception as exc:  # noqa: BLE001 - 单个联系人失败不影响其他人
            out["skipped"].append({"contact": contact,
                                   "reason": f"{type(exc).__name__}: {str(exc)[:120]}"})
            continue
        pro = tick.get("proactive") or {}
        if not tick.get("ran") or not pro.get("available"):
            if pro.get("style") == "simple" and pro.get("reason") == "discarded":
                try:
                    gateway._record("proactive_discarded", {
                        "friend": contact,
                        "discard_reason": str(pro.get("discard_reason") or "")[:200],
                        "regen_reason": str(pro.get("regen_reason") or "")[:200],
                        "style": "simple"})
                except Exception:  # noqa: BLE001
                    pass
                out["skipped"].append({"contact": contact,
                                       "reason": "discarded:simple"})
                continue
            out["skipped"].append({
                "contact": contact,
                "reason": str(tick.get("reason") or pro.get("reason") or "gate")[:80]})
            continue
        bubbles = pro.get("messages") or ([pro.get("message")] if pro.get("message") else [])
        kind = str(pro.get("kind") or "").strip()
        # P33 行为联动：drive≤0.35 只允许 light/progress_check（不发分享/关心/追问）
        if drive_info is not None and kind \
                and not initiative.kind_allowed(kind, drive_info.get("drive")):
            out["skipped"].append({"contact": contact,
                                   "reason": "initiative_kind_gate", "kind": kind,
                                   "drive": drive_info.get("drive")})
            try:
                gateway._record("initiative_kind_blocked", {
                    "contact": contact, "kind": kind,
                    "drive": drive_info.get("drive"),
                    "allowed": list(initiative.LOW_DRIVE_ALLOWED)})
            except Exception:  # noqa: BLE001
                pass
            continue
        # 同类追问冷却：progress_check/followup 在窗口内只发一次（10-28 线上：同一件事追问了 3 次）
        if kind in ("progress_check", "followup"):
            cooldown_s = _cfg_float(gateway, "proactive_kind_cooldown_s", 8 * 3600)
            last_same = recent_kind_sent(db_path, contact, kind, float(now) - cooldown_s)
            if last_same is not None:
                out["skipped"].append({"contact": contact,
                                       "reason": f"{kind}_cooldown"})
                try:
                    gateway._record("proactive_kind_cooldown", {
                        "friend": contact, "kind": kind,
                        "last_ts": round(float(last_same), 1),
                        "window_s": round(float(cooldown_s), 1)})
                except Exception:  # noqa: BLE001
                    pass
                continue
        turn_id = str(pro.get("turn_id") or "")
        # P0-3 出站文本质检：补句末标点/合并空白/超长按标点切分（带 kind 的新主动消息才处理，
        # 无 kind 的旧 payload 原样透传，保持向后兼容）。
        outbound: list[str] = []
        for bubble in bubbles:
            raw = str(bubble or "").strip()
            if not raw:
                continue
            # 2026-10-29：legacy payload（无 kind）也过一遍出站质检（去「」/账比喻/补标点），
            # 以前直接透传，线上出现过旧 payload 带「」和"账"比喻。
            polished = selflearn_proactive.polish_outbound(raw)
            if not polished:
                continue
            # P0-8 发送前最后一道内容闸：命中「安静/没动静/忙吗…」禁词 → 换该 kind 兜底句
            if kind:
                polished = selflearn_proactive.sanitize_topic(polished, kind)
            if "\n" in polished:  # 超长多句：逐段发送，避免一整段粘连
                outbound.extend(part.strip() for part in polished.split("\n")
                                if part.strip())
            else:
                outbound.append(polished)
        sent_any = False
        media_plan = pro.get("media") or {}
        media_items = [item for item in (media_plan.get("bubbles") or [])
                       if isinstance(item, dict) and item.get("type") in ("sticker", "gif", "image")]
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
                "media": media_plan,
                "text_media_check": pro.get("text_media_check"),
                "proactive_style": pro.get("style"),
                "discard_reason": pro.get("discard_reason"),
                "regen_reason": pro.get("regen_reason"),
                "topic_seed": pro.get("topic_seed"),
                "progress_check": pro.get("progress_check"),
                "status": status.get("status"), "draft_id": status.get("draft_id"),
                # P0-6：失败率统计口径 status∈{failed,draft}，真正失败证据看 send_failed
                "send_failed": bool(status.get("send_failed")),
                "error_key": str(status.get("error_key") or "")[:120],
                "stage": str(status.get("stage") or "")[:40],
                # P0-7：clipboard（未送达）也带 warning，统计时单列
                "warning": str(status.get("warning") or "")[:120],
                "error": str(status.get("error") or "")[:120]})
            if kind == "progress_check":
                pc = pro.get("progress_check") or {}
                try:
                    gateway._record("proactive_progress_check", {
                        "friend": contact, "anchor": str(pc.get("anchor") or "")[:30],
                        "msg_id": str(pc.get("msg_id") or "")[:80],
                        "status": status.get("status")})
                except Exception:  # noqa: BLE001
                    pass
            try:
                record_event(db_path, contact, text, status.get("status") or "unknown",
                             now, kind=kind)
            except Exception:  # noqa: BLE001
                pass
            # P7-b：统一口径（sendclass 是唯一真源），按 7 类分发计数
            classification = sendclass.classify_send_result(status)
            if classification == sendclass.CLIPBOARD:
                # 剪贴板兜底：内容已保留但未送达 → 不计 sent、不重置熔断
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
            elif classification == sendclass.UNVERIFIED:
                # 窗口兜底 verified=False / 未知成功状态：发了但无法确认 → 不计 sent、不重置熔断
                out["unverified"] += 1
                try:
                    gateway._record("proactive_unverified", {
                        "friend": contact, "text": text[:120], "intent_kind": kind,
                        "status": status.get("status"),
                        "method": str(status.get("method") or "")[:40],
                        "verified": bool(status.get("verified", False)),
                        "warning": str(status.get("warning") or "")[:200]})
                except Exception:  # noqa: BLE001
                    pass
            elif classification == sendclass.PLANNED:
                out["planned"] += 1      # dry-run：未发生，单列
            elif classification == sendclass.FAILED:
                out["failed_any"] = True
                if status.get("status") == "draft":
                    out["drafts"] += 1   # 真失败落草稿：内容保住但仍算失败
                if _note_send_failure(gateway, status, now=now,
                                      threshold=breaker_threshold,
                                      cooldown_s=breaker_cooldown):
                    out["breaker_open"] = True
                    break                # 熔断后本轮不再发剩余气泡/联系人
            elif classification == sendclass.DELIVERED:
                out["sent"] += 1
                sent_any = True
                _note_send_success(gateway)  # 只有真正送达才清零
            elif classification == sendclass.DRAFT:
                out["drafts"] += 1       # confirm 人工草稿（非故障，不影响熔断）
            # BLOCKED：闸门拦截，非故障非送达，不计数（send_blocked 日志已记）
        # P5-b：文本已发送后，媒体作为额外气泡发；失败不影响文本结果
        local_media_sent = 0
        if sent_any and media_items and gateway.cfg.get("media_enabled"):
            sender = getattr(gateway, "send_bubbles", None)
            if callable(sender):
                try:
                    results = sender(contact, media_items, proactive=True)
                    local_media_sent = sum(1 for item in (results or [])
                                           if isinstance(item, dict) and item.get("ok"))
                    media_sent = local_media_sent
                    out["media_sent"] = int(out.get("media_sent") or 0) + media_sent
                    gateway._record("proactive_media", {
                        "friend": contact, "items": media_items,
                        "results": results or [], "reason": media_plan.get("reason")})
                except Exception as exc:  # noqa: BLE001
                    gateway._record("proactive_media_failed", {
                        "friend": contact, "error": f"{type(exc).__name__}: {exc}"[:200]})
        # P12-C：视频分享的审计（bvid/value/封面/送达），送达后 mark_shared
        if kind == "share_video":
            _record_video_share(gateway, contact, pro, delivered=bool(sent_any),
                                media_sent=local_media_sent,
                                text=(outbound[0] if outbound else ""))
        if sent_any and turn_id:
            # 只有真的发出去了才登记：草稿还没到对方手里，反馈会错位
            gateway.last_turns[contact] = turn_id
        if out["breaker_open"]:
            break
    try:
        gateway._initiative_prev_fail = bool(out.get("failed_any"))
    except Exception:  # noqa: BLE001
        pass
    outcome = {"ts": now, "contacts": out["contacts"],
               "sent": out["sent"], "drafts": out["drafts"],
               "deduped": out["deduped"], "clipboard": out["clipboard"],
               "unverified": out["unverified"], "planned": out["planned"],
               "media_sent": media_sent,
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
            and not out["unverified"] and not out["planned"]
            and all(s.get("reason") == "probability" for s in out["skipped"])):
        last_skip = probability_skips[-1]
        out.update({"ran": False, "reason": "probability_skip",
                    "probability": last_skip.get("probability"),
                    "roll": last_skip.get("roll")})
    return out


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bounds(gateway) -> tuple[float, float]:
    """检查间隔上下界（秒）。默认 60–7200 秒（1–120 分钟）；非法值兜底。"""
    lo = _float_or_none(gateway.cfg.get("proactive_interval_min_s"))
    hi = _float_or_none(gateway.cfg.get("proactive_interval_max_s"))
    if lo is None or lo <= 0:
        lo = 60.0
    if hi is None or hi <= 0:
        hi = 7200.0
    lo = max(10.0, lo)
    if hi < lo:  # 非法区间（如 min>max）→ 上界兜底回 7200 量级
        hi = max(7200.0, lo)
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
    """下一次轮询间隔：**60–7200 秒均匀随机**（2026-10-25 用户拍板）。

    P0-12 教训（2026-10-25 真机 2 小时窗口）：旧实现让 **LLM 自己决定**间隔
    （prompt 要求输出 60–7200 的整数），LLM 倾向给几百秒 → 实测 13 次调度
    min/mean/max = 257.7 / **577.6** / 1689.6 秒，发送率 2.05 条/小时（目标 0.6）。
    用户要的是「1–120 分钟**均匀随机**」，不是模型决定，因此这里改为
    ``random.uniform(lo, hi)`` 纯随机，同时省掉每轮一次 DeepSeek 调用。

    ``client`` / ``now`` 仅保留旧调用方签名兼容，不参与计算；
    ``lo/hi`` 来自 ``_bounds()``（配置非法时兜底 60/7200）。
    P16-B：若 ``last_outcome`` 带 ``pacing_suggested``（本轮对等节奏建议），
    优先用建议区间（已按 pacing_min_interval_s 夹紧），实现「用户聊得多→它多发」。
    """
    lo, hi = _bounds(gateway)
    suggestion = last_outcome.get("pacing_suggested") if isinstance(last_outcome, dict) else None
    if suggestion and pacing.enabled(gateway.cfg):
        try:
            sugg_lo = float(suggestion.get("interval_min_s") or 0)
            sugg_hi = float(suggestion.get("interval_max_s") or 0)
            if sugg_lo > 0 and sugg_hi >= sugg_lo:
                lo, hi = max(60.0, sugg_lo), max(sugg_lo, sugg_hi)
        except (TypeError, ValueError):
            pass
    try:
        delay = random.uniform(float(lo), float(hi))
    except (TypeError, ValueError):  # 极端防御：random 被替换成异常实现时
        delay = random.uniform(60.0, 7200.0)
    return round(max(lo, min(hi, delay)), 1)
