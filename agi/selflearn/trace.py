# -*- coding: utf-8 -*-
"""P15-A：推理过程采集（turn_traces）。

用户要求「在控制台看到整个模型的推理过程」：每条回复的上下文 / 记忆 / 内状态 /
心情 / 生成原文（含 reasoning）/ 润色 / 守卫 / 最终发送 / 耗时。本模块只做
**只读采集 + 查询**，绝不改变生成/润色/守卫的行为与顺序。

约定：
- 表 `turn_traces` 在**记忆库**（`db.MEMORY_TABLES`），`db.connect()` 透明路由；
- 写失败一律 try/except 吞掉——trace 挂了也不能影响回复主链；
- `trace_enabled=false`（wechat config / env `TRACE_ENABLED`）时不写；
- 单条：prompt 类字段各 ≤20KB（保头尾、中间省略），整条 ≤100KB；
- `trace_max_rows`（默认 5000）超出删最旧，防止数据库膨胀；
- 查询函数给 `tools/console/trace_api.py` 用（只读）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

TABLE = "turn_traces"
PROMPT_FIELD_LIMIT = 20_000      # prompt 类字段：20KB
RECORD_LIMIT = 100_000           # 整条记录：100KB
TEXT_FIELD_LIMIT = 20_000        # 其它文本/JSON 字段的上限（与 prompt 一致）
DEFAULT_MAX_ROWS = 5000
DEFAULT_ENABLED = True
LIST_TEXT_LIMIT = 160            # 列表页 user_text/final_reply 截断
JSON_FIELDS = ("memories_json", "inner_json", "mood_json", "generation_json",
               "polish_json", "guard_json", "proactive_json")
TEXT_FIELDS = ("user_text", "final_reply", "skip_reason")
PROMPT_FIELDS = ("system_prompt", "prompt_user")
MODES = ("chat", "proactive")
_TRUNC_MARK = chr(10) + "…（trace 已截断 {dropped} 字节）…" + chr(10)


# ------------------------------------------------------------------ 开关/配置
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


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _gateway_config() -> dict:
    """读 wechat config（含 DEFAULTS）；读不到返回 {}。测试可用 TRACE_CONFIG_PATH 覆盖。"""
    override = os.environ.get("TRACE_CONFIG_PATH")
    try:
        if override:
            raw = json.loads(Path(override).read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        from ..wechat_gateway import config as gateway_config
        defaults = dict(getattr(gateway_config, "DEFAULTS", {}) or {})
        try:
            raw = json.loads(Path(gateway_config.CONFIG_PATH).read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                defaults.update(raw)
        except Exception:  # noqa: BLE001 - 文件不存在/坏 JSON 用默认
            pass
        return defaults
    except Exception:  # noqa: BLE001
        return {}


def settings() -> dict:
    """{enabled, max_rows}：env 优先，其次 wechat config，默认 true / 5000。"""
    cfg = _gateway_config()
    enabled = _env_bool("TRACE_ENABLED")
    if enabled is None:
        enabled = bool(cfg.get("trace_enabled", DEFAULT_ENABLED))
    max_rows = _env_int("TRACE_MAX_ROWS")
    if max_rows is None:
        try:
            max_rows = int(cfg.get("trace_max_rows", DEFAULT_MAX_ROWS))
        except (TypeError, ValueError):
            max_rows = DEFAULT_MAX_ROWS
    return {"enabled": bool(enabled), "max_rows": max(0, int(max_rows))}


def enabled() -> bool:
    return bool(settings().get("enabled"))


# ------------------------------------------------------------------ 截断
def _byte_len(text: Any) -> int:
    try:
        return len(str(text or "").encode("utf-8"))
    except Exception:  # noqa: BLE001
        return 0


def truncate_text(value: Any, limit: int = PROMPT_FIELD_LIMIT) -> str:
    """按 UTF-8 字节截断：保头尾、中间省略，保证结果 ≤ limit 字节。"""
    text = str(value or "")
    if limit <= 0:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    marker = _TRUNC_MARK.format(dropped=len(raw) - limit).encode("utf-8")
    head_limit = max(16, int((limit - len(marker)) * 0.62))
    tail_limit = max(16, limit - len(marker) - head_limit)
    head = raw[:head_limit].decode("utf-8", "ignore")
    tail = raw[-tail_limit:].decode("utf-8", "ignore")
    return head + marker.decode("utf-8") + tail


def _to_json_text(value: Any, limit: int = TEXT_FIELD_LIMIT) -> str:
    if isinstance(value, str):
        return truncate_text(value, limit)
    try:
        text = json.dumps(value if value is not None else {}, ensure_ascii=False,
                          default=str)
    except Exception:  # noqa: BLE001
        text = "{}"
    return truncate_text(text, limit)


def _safe_json(text: Any, default: Any) -> Any:
    if isinstance(text, (dict, list)):
        return text
    try:
        value = json.loads(str(text or ""))
        return value if isinstance(value, (dict, list)) else default
    except Exception:  # noqa: BLE001
        return default


# ------------------------------------------------------------------ 写
def _trace_id(payload: dict, ts: float) -> str:
    hint = payload.get("id") or payload.get("turn_id")
    if hint:
        return str(hint)[:80]
    digest = hashlib.sha1(
        f"{ts}|{payload.get('mode')}|{payload.get('user_text')}|"
        f"{payload.get('final_reply')}".encode("utf-8")).hexdigest()
    return "TR-" + digest[:16]


def normalize_payload(payload: dict, *, now: float | None = None) -> dict:
    """把采集 payload 归一化成一行（含截断与总大小限制）；不写库。"""
    payload = payload if isinstance(payload, dict) else {}
    ts = float(payload.get("ts") or (time.time() if now is None else now))
    mode = str(payload.get("mode") or "chat").strip().lower()
    if mode not in MODES:
        mode = "chat"
    row: dict[str, Any] = {
        "id": _trace_id(payload, ts),
        "ts": round(ts, 3),
        "contact": str(payload.get("contact") or "")[:120],
        "user_text": truncate_text(payload.get("user_text") or "", 8000),
        "mode": mode,
        "system_prompt": truncate_text(payload.get("system_prompt") or "",
                                       PROMPT_FIELD_LIMIT),
        "prompt_user": truncate_text(payload.get("prompt_user") or "",
                                     PROMPT_FIELD_LIMIT),
        "final_reply": truncate_text(payload.get("final_reply") or "",
                                     TEXT_FIELD_LIMIT),
        "skipped": 1 if payload.get("skipped") else 0,
        "skip_reason": truncate_text(payload.get("skip_reason") or "", 2000),
        "crisis": 1 if payload.get("crisis") else 0,
        "latency_ms": _float_or_none(payload.get("latency_ms")),
        "created_at": round(time.time() if now is None else float(now), 3),
    }
    for field in JSON_FIELDS:
        row[field] = _to_json_text(payload.get(field, {}), TEXT_FIELD_LIMIT)
    _enforce_record_limit(row)
    return row


def _float_or_none(value: Any) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _row_bytes(row: dict) -> int:
    return sum(_byte_len(value) for value in row.values())


def _enforce_record_limit(row: dict) -> dict:
    """整条 ≤100KB：从最大的字段开始截，直到放得下。"""
    total = _row_bytes(row)
    if total <= RECORD_LIMIT:
        return row
    shrink_order = list(TEXT_FIELDS) + list(JSON_FIELDS) + list(PROMPT_FIELDS)
    guard = 0
    while total > RECORD_LIMIT and guard < 200:
        guard += 1
        candidates = [(len(str(row.get(field) or "").encode("utf-8")), field)
                      for field in shrink_order if _byte_len(row.get(field)) > 64]
        if not candidates:
            break
        size, field = max(candidates)
        excess = total - RECORD_LIMIT
        new_limit = max(64, size - excess - 64)
        row[field] = truncate_text(row.get(field), new_limit)
        total = _row_bytes(row)
    return row


def _insert(path: Path | str, row: dict) -> None:
    con = db.connect(path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO turn_traces("
            "id,ts,contact,user_text,mode,system_prompt,prompt_user,memories_json,"
            "inner_json,mood_json,generation_json,polish_json,guard_json,"
            "final_reply,skipped,skip_reason,crisis,latency_ms,proactive_json,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], row["ts"], row["contact"], row["user_text"], row["mode"],
             row["system_prompt"], row["prompt_user"], row["memories_json"],
             row["inner_json"], row["mood_json"], row["generation_json"],
             row["polish_json"], row["guard_json"], row["final_reply"],
             row["skipped"], row["skip_reason"], row["crisis"], row["latency_ms"],
             row["proactive_json"], row["created_at"]))
        con.commit()
        _prune(con)
    finally:
        con.close()


def _prune(con) -> None:
    cfg = settings()
    max_rows = int(cfg.get("max_rows") or 0)
    if max_rows <= 0:
        return
    try:
        con.execute(
            "DELETE FROM turn_traces WHERE id NOT IN ("
            "SELECT id FROM turn_traces ORDER BY ts DESC, created_at DESC LIMIT ?)",
            (max_rows,))
        con.commit()
    except Exception:  # noqa: BLE001 - 裁剪失败不影响主链
        pass


def record(path: Path | str = DB_PATH, payload: dict | None = None) -> str | None:
    """写一条 trace；关闭/任何异常都返回 None（绝不影响主链）。"""
    try:
        if not enabled():
            return None
        row = normalize_payload(payload or {})
        _insert(path, row)
        return row["id"]
    except Exception:  # noqa: BLE001 - trace 失败必须静默
        return None


def record_chat(path: Path | str = DB_PATH, *, turn_id: str = "",
                contact: str = "", user_text: str = "",
                system_prompt: str = "", prompt_user: str = "",
                memories: Any = None, inner: Any = None, mood: Any = None,
                generation: Any = None, polish: Any = None, guard: Any = None,
                final_reply: str = "", skipped: bool = False,
                skip_reason: str = "", crisis: bool = False,
                latency_ms: float | None = None, now: float | None = None) -> str | None:
    """chat.send 的便捷采集入口（字段名对齐表）。"""
    if isinstance(memories, str):
        memories_payload: Any = memories
    else:
        memories_payload = {"items": list(memories or [])}
    return record(path, {
        "id": turn_id, "ts": now, "contact": contact, "user_text": user_text,
        "mode": "chat", "system_prompt": system_prompt, "prompt_user": prompt_user,
        "memories_json": memories_payload, "inner_json": inner, "mood_json": mood,
        "generation_json": generation, "polish_json": polish,
        "guard_json": guard, "final_reply": final_reply, "skipped": skipped,
        "skip_reason": skip_reason, "crisis": crisis, "latency_ms": latency_ms,
    })


def record_proactive(path: Path | str = DB_PATH, result: dict | None = None, *,
                     contact: str = "", now: float | None = None,
                     extra: dict | None = None) -> str | None:
    """proactive.tick 的采集入口：mode=proactive。

    只记「有决策内容」的 tick（available / kind / candidates / 生成结果）；
    「刚聊过 / 还没对话」这类每 30s 的静默轮询不记，避免把表刷爆。
    """
    result = result if isinstance(result, dict) else {}
    available = bool(result.get("available"))
    meaningful = available or result.get("kind") or result.get("candidates") \
        or result.get("messages") or result.get("message") or extra
    if not meaningful:
        return None
    proactive = {
        "available": available,
        "kind": result.get("kind") or "",
        "reason": result.get("reason") or "",
        "probability": result.get("probability"),
        "candidates": result.get("candidates"),
        "candidate_count": result.get("candidate_count"),
        "discarded": result.get("discarded") or result.get("drop_reason"),
        "regen": result.get("regen") or result.get("regenerated"),
        "video_share": result.get("video_share"),
        "id": result.get("id"),
        "messages": result.get("messages") or ([result.get("message")]
                                               if result.get("message") else []),
    }
    if extra:
        proactive.update(extra)
    first = ""
    messages = proactive.get("messages") or []
    if messages:
        first = str(messages[0] or "")
    return record(path, {
        "ts": now, "contact": contact or Path(str(path)).stem,
        "mode": "proactive", "user_text": result.get("user_text") or "",
        "proactive_json": proactive,
        "final_reply": first,
        "skipped": 0 if available else 1,
        "skip_reason": "" if available else str(result.get("reason") or ""),
    })


# ------------------------------------------------------------------ 读（trace_api 用）
def _connect(path: Path | str):
    return db.connect(path)


def _row_dict(row) -> dict:
    return dict(row) if row is not None else {}


def _summary(row: dict) -> dict:
    generation = _safe_json(row.get("generation_json"), {})
    proactive = _safe_json(row.get("proactive_json"), {})
    kind = ""
    if isinstance(proactive, dict):
        kind = str(proactive.get("kind") or proactive.get("selected_kind") or "")
    if not kind and isinstance(generation, dict):
        kind = str(generation.get("purpose") or "")
    return {
        "id": row.get("id"),
        "ts": row.get("ts"),
        "mode": row.get("mode") or "chat",
        "contact": row.get("contact") or "",
        "user_text": truncate_text(row.get("user_text"), LIST_TEXT_LIMIT),
        "final_reply": truncate_text(row.get("final_reply"), LIST_TEXT_LIMIT),
        "latency_ms": row.get("latency_ms"),
        "skipped": int(row.get("skipped") or 0),
        "skip_reason": str(row.get("skip_reason") or "")[:200],
        "crisis": int(row.get("crisis") or 0),
        "kind": kind,
    }


def list_turns(path: Path | str = DB_PATH, *, limit: int = 50,
               contact: str = "", mode: str = "") -> list[dict]:
    limit = max(1, min(int(limit or 50), 500))
    sql = ("SELECT id,ts,mode,contact,user_text,final_reply,latency_ms,skipped,"
           "skip_reason,crisis,generation_json,proactive_json FROM turn_traces")
    clauses: list[str] = []
    params: list[Any] = []
    if contact:
        clauses.append("contact=?")
        params.append(str(contact))
    if mode in MODES:
        clauses.append("mode=?")
        params.append(str(mode))
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY ts DESC, created_at DESC LIMIT ?"
    params.append(limit)
    con = _connect(path)
    try:
        rows = [_row_dict(row) for row in con.execute(sql, params).fetchall()]
    finally:
        con.close()
    return [_summary(row) for row in rows]


def get_turn(path: Path | str = DB_PATH, turn_id: str = "") -> dict | None:
    con = _connect(path)
    try:
        row = con.execute("SELECT * FROM turn_traces WHERE id=?",
                          (str(turn_id or ""),)).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    item = _row_dict(row)
    record = {
        "id": item.get("id"), "ts": item.get("ts"), "contact": item.get("contact"),
        "user_text": item.get("user_text"), "mode": item.get("mode"),
        "system_prompt": item.get("system_prompt"),
        "prompt_user": item.get("prompt_user"),
        "final_reply": item.get("final_reply"),
        "skipped": int(item.get("skipped") or 0),
        "skip_reason": item.get("skip_reason") or "",
        "crisis": int(item.get("crisis") or 0),
        "latency_ms": item.get("latency_ms"),
        "created_at": item.get("created_at"),
    }
    memories = _safe_json(item.get("memories_json"), [])
    if isinstance(memories, dict) and isinstance(memories.get("items"), list):
        memories = memories["items"]          # 前端契约：memories 是数组
    record["memories"] = memories if isinstance(memories, list) else []
    record["inner"] = _safe_json(item.get("inner_json"), {})
    record["mood"] = _safe_json(item.get("mood_json"), {})
    record["generation"] = _safe_json(item.get("generation_json"), {})
    record["polish"] = _safe_json(item.get("polish_json"), {})
    record["guard"] = _safe_json(item.get("guard_json"), {})
    record["proactive"] = _safe_json(item.get("proactive_json"), {})
    record["raw"] = {field: item.get(field) or "" for field in
                     ("memories_json", "inner_json", "mood_json", "generation_json",
                      "polish_json", "guard_json", "proactive_json")}
    return record


def stats(path: Path | str = DB_PATH, *, days: int = 7,
          now: float | None = None) -> dict:
    days = max(1, int(days or 7))
    now_ts = time.time() if now is None else float(now)
    since = now_ts - days * 86400.0
    con = _connect(path)
    try:
        rows = [_row_dict(row) for row in con.execute(
            "SELECT ts,mode,latency_ms,skipped,crisis,generation_json "
            "FROM turn_traces WHERE ts>=? ORDER BY ts DESC", (since,)).fetchall()]
    finally:
        con.close()
    latencies = sorted(float(row["latency_ms"]) for row in rows
                       if row.get("latency_ms") is not None)
    def pct(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        index = max(0, min(len(values) - 1, int(round((p / 100.0) * (len(values) - 1)))))
        return round(values[index], 2)
    two_stage = 0
    by_mode: dict[str, int] = {}
    by_day: dict[str, int] = {}
    for row in rows:
        generation = _safe_json(row.get("generation_json"), {})
        if isinstance(generation, dict) and generation.get("two_stage"):
            two_stage += 1
        mode = str(row.get("mode") or "chat")
        by_mode[mode] = by_mode.get(mode, 0) + 1
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(float(row["ts"])))
        except (KeyError, TypeError, ValueError, OSError):
            day = "unknown"
        by_day[day] = by_day.get(day, 0) + 1
    count = len(rows)
    skipped = sum(1 for row in rows if row.get("skipped"))
    return {
        "days": days,
        "count": count,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 2)
        if latencies else 0.0,
        "p95_latency_ms": pct(latencies, 95),
        "skip_rate": round(skipped / count, 4) if count else 0.0,
        "two_stage_rate": round(two_stage / count, 4) if count else 0.0,
        "crisis_count": sum(1 for row in rows if row.get("crisis")),
        "by_mode": by_mode,
        "by_day": dict(sorted(by_day.items())),
    }


def stream(path: Path | str = DB_PATH, *, limit: int = 20,
           since_ts: float | None = None) -> list[dict]:
    """最近事件（前端轮询增量）：先简单复用 turns；since_ts 过滤。"""
    items = list_turns(path, limit=min(int(limit or 20), 100))
    if since_ts is not None:
        items = [item for item in items if float(item.get("ts") or 0) >= float(since_ts)]
    return items
