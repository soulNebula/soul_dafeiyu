# -*- coding: utf-8 -*-
"""P15-A：控制台「推理过程」只读 API（由 `tools/console/server.py` 路由接线）。

接线只需一行（Lead/w3-feedback 在 server.py 的 `/api/trace` 分支里）：

    result = trace_api.handle_trace(path, query)   # path = "/api/trace/turns" 等
    # 返回 dict；{"ok": False, "error": "admin token required", "status": 401} 时回 401

本模块**不做鉴权**（console 的 `token_ok()` 已先行校验）；为方便单测，
`handle_trace(..., admin_token="secret")` 可做一次可选 token 校验。
只读：查询走 `agi.selflearn.trace` 与 SQLite 只读连接。
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from agi.selflearn import db, trace

ROOT = Path(__file__).resolve().parents[2]
CONTACTS_DIR = ROOT / "agi" / "data" / "selflearn" / "wechat_contacts"
ROUTES = ("/turns", "/turn", "/stream", "/stats")


# ------------------------------------------------------------------ query/路径
def _normalize_query(query: Any) -> dict:
    """兼容 parse_qs dict（值是 list）、普通 dict 与 query string。"""
    if isinstance(query, str):
        query = parse_qs(query.lstrip("?"))
    if not isinstance(query, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in query.items():
        if isinstance(value, (list, tuple)):
            out[str(key)] = value[0] if value else ""
        else:
            out[str(key)] = value
    return out


def _q(query: dict, name: str, default: str = "") -> str:
    value = query.get(name, default)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else default
    return str(value if value is not None else default)


def resolve_route(path: Any) -> str:
    """`/api/trace/turns` / `turns` / `/turns` → `/turns`；未知→原样。"""
    text = str(path or "").strip()
    if "?" in text:
        text = text.split("?", 1)[0]
    text = text.rstrip("/")
    if "/api/trace" in text:
        text = text[text.index("/api/trace") + len("/api/trace"):]
    if not text:
        return "/turns"
    if not text.startswith("/"):
        text = "/" + text
    # 完整 URL 兜底：取最后一段
    if text not in ROUTES:
        last = text.rsplit("/", 1)[-1]
        if last in [route.strip("/") for route in ROUTES]:
            return "/" + last
    return text


def _preferred_contact_stems() -> list[str]:
    try:
        from agi.wechat_gateway import config as gateway_config
        raw = json.loads(Path(gateway_config.CONFIG_PATH).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    ordered: list[str] = []
    for key in ("proactive_contacts", "monitored_contacts", "allowed_contacts"):
        for alias in (raw.get(key) or []):
            if alias and alias not in ordered:
                ordered.append(alias)
    return [hashlib.sha1(str(alias).encode("utf-8")).hexdigest()[:14]
            for alias in ordered]


def _fallback_db_path() -> Path:
    """没传 contact/db 时：优先配置里的真实联系人库，其次最近的联系人库。"""
    env = str(__import__("os").environ.get("TRACE_DB_PATH") or "").strip()
    if env:
        return Path(env)
    for stem in _preferred_contact_stems():
        for suffix in (".memory.sqlite3", ".sqlite3", ".legacy.sqlite3"):
            candidate = CONTACTS_DIR / (stem + suffix)
            if candidate.is_file():
                return candidate
    try:
        candidates = sorted(
            list(CONTACTS_DIR.glob("*.memory.sqlite3"))
            + [path for path in CONTACTS_DIR.glob("*.sqlite3")
               if not path.name.endswith((".memory.sqlite3", ".persona.sqlite3"))],
            key=lambda path: path.stat().st_mtime, reverse=True)
        if candidates:
            return candidates[0]
    except OSError:
        pass
    from agi.selflearn.config import DB_PATH
    return Path(DB_PATH)


def resolve_db_path(query: dict) -> Path:
    """显式 db/db_path > TRACE_DB_PATH > contact alias（网关同款 hash）> 默认联系人库。"""
    for key in ("db", "db_path"):
        value = _q(query, key)
        if value:
            return Path(value)
    contact = _q(query, "contact")
    if contact:
        try:
            from agi.wechat_gateway import config as gateway_config
            return Path(gateway_config.contact_db(contact))
        except Exception:  # noqa: BLE001
            return _fallback_db_path()
    return _fallback_db_path()


# ------------------------------------------------------------------ handlers
def _turns(db_path: Path, query: dict) -> dict:
    try:
        limit = int(_q(query, "limit", "50") or 50)
    except (TypeError, ValueError):
        limit = 50
    mode = _q(query, "mode")
    if mode not in trace.MODES:
        mode = ""
    items = trace.list_turns(db_path, limit=limit, contact=_q(query, "contact"),
                             mode=mode)
    return {"ok": True, "turns": items, "items": items, "count": len(items),
            "db": str(db_path)}


def _turn(db_path: Path, query: dict) -> dict:
    turn_id = _q(query, "id") or _q(query, "turn_id")
    if not turn_id:
        return {"ok": False, "error": "missing_id"}
    record = trace.get_turn(db_path, turn_id)
    if record is None:
        return {"ok": False, "error": "not_found:turn", "id": turn_id}
    return {"ok": True, "turn": record, "item": record, "db": str(db_path)}


def _stream(db_path: Path, query: dict) -> dict:
    try:
        limit = int(_q(query, "limit", "20") or 20)
    except (TypeError, ValueError):
        limit = 20
    try:
        since = float(_q(query, "since") or 0.0)
    except (TypeError, ValueError):
        since = 0.0
    events = trace.stream(db_path, limit=limit, since_ts=since or None)
    return {"ok": True, "events": events, "items": events, "count": len(events),
            "db": str(db_path)}


def _stats(db_path: Path, query: dict) -> dict:
    try:
        days = int(_q(query, "days", "7") or 7)
    except (TypeError, ValueError):
        days = 7
    stats = trace.stats(db_path, days=max(1, days))
    # 兼容前端契约：同时给扁平字段 + 嵌套 stats（p95 两种命名都给）
    return {"ok": True,
            "total": stats.get("count", 0),
            "avg_latency_ms": stats.get("avg_latency_ms", 0.0),
            "skip_rate": stats.get("skip_rate", 0.0),
            "p95_latency_ms": stats.get("p95_latency_ms", 0.0),
            "p95": stats.get("p95_latency_ms", 0.0),
            "two_stage_rate": stats.get("two_stage_rate", 0.0),
            "crisis_count": stats.get("crisis_count", 0),
            "by_mode": stats.get("by_mode", {}),
            "by_day": stats.get("by_day", {}),
            "days": stats.get("days", days),
            "stats": stats, "db": str(db_path)}


_HANDLERS = {"/turns": _turns, "/turn": _turn, "/stream": _stream, "/stats": _stats}


def handle_trace(path: Any, query: Any = None, *,
                 admin_token: str | None = None) -> dict:
    """只读 trace API 入口。

    - ``path``：``/turns`` | ``/turn`` | ``/stream`` | ``/stats``
      （也接受完整 ``/api/trace/turns``；未知路由返回 ``not_found``）；
    - ``query``：console 的 parse_qs dict（值 list）/普通 dict / query string；
    - ``admin_token``：可选；给出时要求 query 里 ``token``/``admin_token`` 匹配，
      否则返回 ``{"ok": False, "status": 401, "error": "admin token required"}``。
    """
    query_dict = _normalize_query(query or {})
    if admin_token is not None:
        provided = _q(query_dict, "token") or _q(query_dict, "admin_token")
        if not provided or not hmac.compare_digest(str(provided), str(admin_token)):
            return {"ok": False, "status": 401, "error": "admin token required"}
    route = resolve_route(path)
    handler = _HANDLERS.get(route)
    if handler is None:
        return {"ok": False, "error": f"not_found:{route}", "routes": list(ROUTES)}
    db_path = resolve_db_path(query_dict)
    try:
        result = handler(db_path, query_dict)
    except Exception as exc:  # noqa: BLE001 - 查询失败返回结构化错误，不炸 console
        return {"ok": False, "error": "trace_api_error",
                "hint": f"{type(exc).__name__}: {exc}"}
    if isinstance(result, dict):
        result.setdefault("ok", True)
        return result
    return {"ok": True, "data": result}


def trace_routes() -> list[str]:
    """给 console 路由注册用（/api/trace/turns 等）。"""
    return ["/api/trace" + route for route in ROUTES]
