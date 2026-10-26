# -*- coding: utf-8 -*-
"""P16-B：主动消息节奏对等（按用户主动量自适应）。

用户诉求（2026-10-26）：**主动发消息的条数要和用户主动发给它的条数相近**
（"而不是我多他少"）。旧配置是固定概率 0.5–0.6、间隔 60–7200 秒随机，
用户聊得多时它明显偏少；聊得少时又可能反过来刷屏。

算法（7 天滚动窗口，按联系人隔离）：
- ``user_initiated``：messages 表 role='user' 的条数（用户发来的消息即"用户发起"，
  被动回复也是用户先开口）；
- ``agent_proactive``：proactive_events 里 status='sent' 且 intent kind != 'reply'
  的条数（表缺 kind 列时按 status 统计；库/表不存在时回退 gateway.log 的
  ``proactive_send`` 事件，两者**只取其一**避免重复计数）；
- ``ratio = agent_proactive / user_initiated``（双方都为 0 视为 1.0 平局；
  用户 0 而它发过 → ratio=agent 条数，视为偏高）；
- ``deficit = user_initiated - agent_proactive``（正数 = 它发少了）。

``adjust(config, balance)`` 是纯函数：目标 1.0、容差 ±25%；
- ratio < 0.75 → 概率 +0.15（上限 0.9，若当前已 ≥0.9 则保持）、间隔 ×0.6
  （下限 ``pacing_min_interval_s``=1200 秒）；
- ratio > 1.25 → 概率 -0.15（下限 0.2）、间隔 ×1.5；
- 容差内 → 用原概率/间隔；
- 连续 3 条主动消息无回应 → 保守节奏（概率 ≤0.2、间隔 ≥1h）；
- 静默时段 / STOP / 当天主动条数已达 ``pacing_daily_cap``(12) → ``send=False``。

只读：本模块不写配置、不发消息；真正的调度由 ``proactive_worker`` 使用返回值。
"""
from __future__ import annotations

import os

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import db as _db

WINDOW_DAYS = 7
DAY_S = 86400.0
#: 配置缺省值（网关 config.DEFAULTS 里也有一份；这里保证独立可测）
DEFAULTS: dict[str, Any] = {
    "pacing_enabled": True,
    "pacing_target_ratio": 1.0,
    "pacing_tolerance": 0.25,
    "pacing_daily_cap": 12,
    "pacing_min_interval_s": 1200.0,
    "proactive_probability_min": 0.3,
    "proactive_probability_max": 0.9,
    "proactive_interval_min_s": 3600.0,
    "proactive_interval_max_s": 7200.0,
}


def _float_or(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def day_start(now: float | None = None) -> float:
    """本地时区当天 0 点的时间戳（日上限用）。"""
    now = time.time() if now is None else float(now)
    return time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))


def _prob_range(config: dict[str, Any]) -> tuple[float, float]:
    lo = _float_or(config.get("proactive_probability_min"), 0.3)
    hi = _float_or(config.get("proactive_probability_max"), 0.9)
    legacy = config.get("proactive_probability")
    if config.get("proactive_probability_min") is None \
            and config.get("proactive_probability_max") is None and legacy is not None:
        value = _float_or(legacy, 0.5)
        return value, value
    lo = max(0.0, min(1.0, lo))
    hi = max(0.0, min(1.0, hi))
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def enabled(config: dict[str, Any] | None) -> bool:
    """pacing 是否启用（默认 true；显式 false 关闭）。

    env ``PACING_ENABLED`` 优先（测试隔离用：默认测试进程关闭，
    避免自适应节奏改变既有断言；专项测试显式打开）。
    """
    env = os.environ.get("PACING_ENABLED")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off", "")
    if not isinstance(config, dict):
        return True
    value = config.get("pacing_enabled")
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def _in_quiet(quiet: Any, hour: int) -> bool:
    if not isinstance(quiet, (list, tuple)) or len(quiet) != 2:
        return False
    start, end = _int_or(quiet[0], -1), _int_or(quiet[1], -1)
    if start < 0 or end < 0 or start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _memory_file(path: Path | str) -> Path:
    try:
        return Path(_db.memory_path(Path(path)))
    except Exception:  # noqa: BLE001 - 非 selflearn DB 路径
        return Path(path)


def _read_events(con: sqlite3.Connection) -> dict[str, Any] | None:
    """从一个 memory 库读 proactive_events 统计；没有该表返回 None。"""
    try:
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return None
    if "proactive_events" not in tables:
        return None
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(proactive_events)")}
    except sqlite3.Error:
        return None
    return {"has_kind": "kind" in columns, "has_response": "response" in columns}


def _event_stats(con: sqlite3.Connection, *, cut: float, today: float) -> dict[str, Any]:
    info = _read_events(con) or {"has_kind": False, "has_response": False}
    kind_filter = " AND COALESCE(kind,'') != 'reply'" if info["has_kind"] else ""
    row = con.execute(
        "SELECT count(*) FROM proactive_events WHERE status='sent'" + kind_filter +
        " AND ts>=?", (cut,)).fetchone()
    agent_proactive = int(row[0] if row else 0)
    row = con.execute(
        "SELECT count(*) FROM proactive_events WHERE status='sent'" + kind_filter +
        " AND ts>=?", (today,)).fetchone()
    today_count = int(row[0] if row else 0)
    streak = 0
    if info["has_response"]:
        try:
            for row in con.execute(
                    "SELECT response FROM proactive_events WHERE status='sent' "
                    "ORDER BY ts DESC LIMIT 50"):
                if row[0] is None:
                    streak += 1
                else:
                    break
        except sqlite3.Error:
            streak = 0
    return {"agent_proactive": agent_proactive,
            "today_agent_proactive": today_count,
            "unanswered_streak": streak}


def _log_stats(log_path: Path | str, *, cut: float, today: float,
               contact: str | None = None) -> dict[str, Any]:
    """回退：从 gateway.log 数 ``proactive_send``（status=sent、intent_kind != reply）。"""
    target = Path(log_path)
    agent_proactive = 0
    today_count = 0
    streak = 0
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("kind") != "proactive_send":
            continue
        if str(event.get("status") or "") != "sent":
            continue
        if str(event.get("intent_kind") or "") == "reply":
            continue
        if contact is not None and str(event.get("friend") or "") != str(contact):
            continue
        ts = _float_or(event.get("ts"), 0.0)
        if ts < cut:
            continue
        agent_proactive += 1
        if ts >= today:
            today_count += 1
    return {"agent_proactive": agent_proactive,
            "today_agent_proactive": today_count,
            "unanswered_streak": streak}


def balance(path: Path | str, now: float | None = None, *,
            contact: str | None = None,
            log_path: Path | str | None = None) -> dict[str, Any]:
    """统计一个联系人近 7 天的对等度，并给出基线建议（只读）。"""
    now = time.time() if now is None else float(now)
    cut = now - WINDOW_DAYS * DAY_S
    today = day_start(now)
    user_initiated = 0
    stats: dict[str, Any] = {"agent_proactive": 0,
                             "today_agent_proactive": 0,
                             "unanswered_streak": 0,
                             "source": "empty"}
    mem = _memory_file(path)
    if mem.exists():
        try:
            con = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
            try:
                con.row_factory = sqlite3.Row
                tables = {row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "messages" in tables:
                    row = con.execute(
                        "SELECT count(*) FROM messages WHERE role='user' AND ts>=?",
                        (cut,)).fetchone()
                    user_initiated = int(row[0] if row else 0)
                if "proactive_events" in tables:
                    stats = _event_stats(con, cut=cut, today=today)
                    stats["source"] = "proactive_events"
            finally:
                con.close()
        except sqlite3.Error:
            stats["source"] = "db_error"
    if stats["source"] in ("empty", "db_error") and log_path:
        stats = _log_stats(log_path, cut=cut, today=today, contact=contact)
        stats["source"] = "gateway_log"

    agent = int(stats.get("agent_proactive") or 0)
    if user_initiated > 0:
        ratio = agent / user_initiated
    elif agent == 0:
        ratio = 1.0            # 双方都没说话 → 视为对等（不做动作）
    else:
        ratio = float(agent)   # 用户没主动、它发过 → 偏高
    result = {
        "contact": str(contact or ""),
        "window_days": WINDOW_DAYS,
        "user_initiated": int(user_initiated),
        "agent_proactive": agent,
        "today_agent_proactive": int(stats.get("today_agent_proactive") or 0),
        "unanswered_streak": int(stats.get("unanswered_streak") or 0),
        "ratio": round(float(ratio), 4),
        "deficit": int(user_initiated - agent),
        "source": stats.get("source"),
    }
    result["suggested"] = {key: adjust(DEFAULTS, result, now=now)[key]
                           for key in ("probability", "interval_min_s",
                                       "interval_max_s", "send", "reason")}
    return result


def adjust(config: dict[str, Any] | None, balance_result: dict[str, Any], *,
           now: float | None = None, stopped: bool = False) -> dict[str, Any]:
    """纯函数：根据 config + balance 给出本轮建议（不写盘、不发消息）。

    ``send=False`` 表示本轮不要主动（原因见 reason：stopped/quiet_hours/daily_cap）。
    """
    config = dict(config or {})
    params = dict(DEFAULTS)
    params.update({key: value for key, value in config.items() if value is not None})
    target = max(0.1, _float_or(params.get("pacing_target_ratio"), 1.0))
    tolerance = max(0.0, _float_or(params.get("pacing_tolerance"), 0.25))
    cap = max(0, _int_or(params.get("pacing_daily_cap"), 12))
    floor_interval = max(60.0, _float_or(params.get("pacing_min_interval_s"), 1200.0))
    prob_lo, prob_hi = _prob_range(params)
    base_probability = round((prob_lo + prob_hi) / 2.0, 4)
    base_min = max(60.0, _float_or(params.get("proactive_interval_min_s"), 3600.0))
    base_max = max(base_min, _float_or(params.get("proactive_interval_max_s"), 7200.0))

    ratio = _float_or(balance_result.get("ratio"), 1.0)
    today_count = _int_or(balance_result.get("today_agent_proactive"), 0)
    if not enabled(config):
        # 总开关关闭：不动概率/间隔，让调用方走旧的固定调度
        return {
            "applied": False, "send": True, "reason": "pacing_disabled",
            "conservative": False, "ratio": round(ratio, 4),
            "deficit": int(_int_or(balance_result.get("deficit"), 0)),
            "target_ratio": round(target, 4), "tolerance": round(tolerance, 4),
            "daily_cap": cap, "today_agent_proactive": today_count,
            "unanswered_streak": _int_or(balance_result.get("unanswered_streak"), 0),
            "probability": round(base_probability, 4),
            "interval_min_s": int(round(base_min)),
            "interval_max_s": int(round(base_max)),
            "base": {"probability": base_probability,
                     "interval_min_s": int(round(base_min)),
                     "interval_max_s": int(round(base_max))},
        }
    streak = _int_or(balance_result.get("unanswered_streak"), 0)
    hour = time.localtime(time.time() if now is None else float(now)).tm_hour
    override_quiet = bool(config.get("proactive_override_quiet"))
    quiet = bool(_in_quiet(config.get("quiet_hours"), hour)) and not override_quiet

    probability = base_probability
    interval_min, interval_max = base_min, base_max
    send = True
    reason = "within"
    conservative = False

    if stopped or bool(config.get("stop")):
        send, reason = False, "stopped"
    elif quiet:
        send, reason = False, "quiet_hours"
    elif cap and today_count >= cap:
        send, reason = False, "daily_cap"
    elif streak >= 3:
        conservative = True
        probability = min(base_probability, 0.2)
        interval_min = max(floor_interval, 3600.0)
        interval_max = max(interval_min, 7200.0)
        reason = "no_response"
    elif ratio < target * (1.0 - tolerance):
        probability = base_probability if base_probability >= 0.9 \
            else min(0.9, base_probability + 0.15)
        interval_min = max(floor_interval, base_min * 0.6)
        interval_max = max(interval_min, base_max * 0.6)
        reason = "under"
    elif ratio > target * (1.0 + tolerance):
        probability = max(0.2, base_probability - 0.15)
        interval_min = max(floor_interval, base_min * 1.5)
        interval_max = max(interval_min, base_max * 1.5)
        reason = "over"
    else:
        probability = base_probability
        reason = "within"

    return {
        "applied": enabled(config),
        "send": bool(send),
        "reason": reason,
        "conservative": bool(conservative),
        "ratio": round(ratio, 4),
        "deficit": int(_int_or(balance_result.get("deficit"), 0)),
        "target_ratio": round(target, 4),
        "tolerance": round(tolerance, 4),
        "daily_cap": cap,
        "today_agent_proactive": today_count,
        "unanswered_streak": streak,
        "probability": round(float(probability), 4),
        "interval_min_s": int(round(interval_min)),
        "interval_max_s": int(round(interval_max)),
        "base": {"probability": base_probability,
                 "interval_min_s": int(round(base_min)),
                 "interval_max_s": int(round(base_max))},
    }


def summary(balance_result: dict[str, Any], adjustment: dict[str, Any] | None = None) -> dict[str, Any]:
    """给日志/outcome 用的精简结构（pacing_adjusted 事件）。"""
    data = {
        "ratio": balance_result.get("ratio"),
        "deficit": balance_result.get("deficit"),
        "user_initiated": balance_result.get("user_initiated"),
        "agent_proactive": balance_result.get("agent_proactive"),
        "today_agent_proactive": balance_result.get("today_agent_proactive"),
        "unanswered_streak": balance_result.get("unanswered_streak"),
        "source": balance_result.get("source"),
    }
    if adjustment:
        data.update({
            "send": adjustment.get("send"),
            "reason": adjustment.get("reason"),
            "probability": adjustment.get("probability"),
            "interval_min_s": adjustment.get("interval_min_s"),
            "interval_max_s": adjustment.get("interval_max_s"),
        })
    return data
