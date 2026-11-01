# -*- coding: utf-8 -*-
"""P4-a：本地调试前端后端（纯标准库，绑 127.0.0.1:802）。

只服务静态页 + 本地数据；写操作必须带 ``X-Admin-Token``（与网关 admin_token 一致），
全部落 ``agi/data/console_audit.log``。不改旧 ``agi/selflearn/web.py``。

启动::

    python tools/console/server.py            # http://127.0.0.1:802/
    python tools/console/server.py --port 802 --contact-db <sqlite>
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import http.cookiejar
import urllib.request
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:            # 以脚本方式启动时也能 import agi.*
    sys.path.insert(0, str(ROOT))
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[2]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from agi.paths import agent_path, bot_root as _bot_root
DATA_DIR = ROOT / "agi" / "data"
GATEWAY_DIR = DATA_DIR / "wechat"
GATEWAY_LOG = GATEWAY_DIR / "gateway.log"
GATEWAY_CONFIG = GATEWAY_DIR / "config.json"
DRAFTS_PATH = GATEWAY_DIR / "drafts.jsonl"
STOP_PATH = GATEWAY_DIR / "STOP"
GATEWAY_STATUS_URL = "http://127.0.0.1:801/status"
SELFLEARN_DIR = DATA_DIR / "selflearn"
CONTACTS_DIR = SELFLEARN_DIR / "wechat_contacts"
PROMPTS_PATH = DATA_DIR / "prompts.json"
LOG_DIR = ROOT / "log"
LOG_MESSAGES = LOG_DIR / "messages.jsonl"
LOG_CHAT = LOG_DIR / "聊天记录.txt"
BACKUP_DIR = agent_path(r"backups")
AUDIT_PATH = DATA_DIR / "console_audit.log"
DIARY_DIR = DATA_DIR / "diary"
TONE_PROFILE = ROOT / "agi" / "eval_data" / "human_tone" / "profile.md"
STATIC_DIR = Path(__file__).resolve().parent / "static"

#: P4-c 恢复出厂：必须手动输入的短语
FACTORY_PHRASE = "清空我和TA的回忆"
#: 对话框里写明的「保留」清单（只展示，不删除）
FACTORY_KEEP = [
    "模型权重与 GGUF（E:\\agent）",
    "评测/训练语料 agi/eval_data/",
    "agi/data/wechat/config.json",
    "agi/data/local_models.json",
    "代码本身",
]

HOST = "127.0.0.1"
PORT = 802
MAX_LIMIT = 1000
READ_LIMIT_BYTES = 65536

#: 设置页可编辑的白名单键（与网关热加载/内状态读取口径一致）
EDITABLE_DEFAULTS: dict = {
    "proactive_enabled": False,
    "quiet_hours": [23, 8],
    "proactive_interval_min_s": 60,
    "proactive_interval_max_s": 7200,
    "proactive_probability_min": 0.3,
    "proactive_probability_max": 0.9,
    "inner_mode": "off",
    "inner_shadow_only": False,
    "inner_mode_scope": "proactive_only",
    "inner_schema": "slim",
    "reply_max_bubbles": 0,
    "user_llm_ratio": "1:2",
    "proactive_method": "prob",
    "proactive_max_per_day": 3,
    "proactive_max_bubbles": 1,
    "media_enabled": False,
}
#: 其余不可编辑但页面要看的键
READONLY_CONFIG_KEYS = ("allowed_contacts", "proactive_contacts", "stop", "send_mode",
                        "dry_run", "auto_reply", "reply_delay", "admin_token")
#: 只允许这些模型被启停（1.5B qwen15 为 reserved，必须拒绝）
MODEL_ALLOWLIST = frozenset({"soul-05b"})
INNER_MODES = ("off", "static", "rule", "model")
INNER_SCOPES = ("all", "proactive_only", "off")
INNER_SCHEMAS = ("slim", "full")
FILLER_WORDS = ("哈哈哈", "笑死", "笑死我了", "真的服了", "服了", "诶", "咋说呢",
                "可惜", "哎", "离谱", "无语", "有点上头", "挺逗", "嗯…",
                "说真的", "你别说")


def now_ts() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# 基础 IO
# --------------------------------------------------------------------------- #
def read_json(path: Path, default):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return payload if isinstance(payload, type(default)) else default


def write_json_atomic(path: Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def audit(action: str, payload: dict | None = None, *, ok: bool = True,
          note: str = "", client: str = "") -> None:
    entry = {"ts": now_ts(), "action": action, "ok": bool(ok),
             "payload": payload or {}, "note": str(note)[:200], "client": client}
    try:
        Path(AUDIT_PATH).parent.mkdir(parents=True, exist_ok=True)
        with Path(AUDIT_PATH).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 网关配置 / 状态 / STOP
# --------------------------------------------------------------------------- #
def gateway_config() -> dict:
    data = read_json(GATEWAY_CONFIG, {})
    return data if isinstance(data, dict) else {}


def admin_token() -> str:
    return str(gateway_config().get("admin_token") or "")


def stop_active() -> bool:
    try:
        return Path(STOP_PATH).exists()
    except OSError:
        return False


def set_stop(enabled: bool, *, client: str = "") -> dict:
    target = Path(STOP_PATH)
    try:
        if enabled:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text("stopped\n", encoding="utf-8")
        else:
            target.unlink(missing_ok=True)
    except OSError as exc:
        audit("proactive_stop" if enabled else "proactive_resume",
              {"enabled": enabled}, ok=False, note=str(exc), client=client)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "stopped": stop_active()}
    audit("proactive_stop" if enabled else "proactive_resume",
          {"enabled": enabled}, ok=True, client=client)
    return {"ok": True, "stopped": bool(enabled), "path": str(target)}


def gateway_risk_state() -> dict:
    """801 不通时给出"为什么"：STOP 文件/总开关/发送模式（只读）。"""
    try:
        cfg = read_json(GATEWAY_CONFIG, {}) or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    return {
        "stop_file": Path(STOP_PATH).exists(),
        "enabled": bool(cfg.get("enabled")),
        "engine_enabled": bool(cfg.get("engine_enabled")),
        "auto_reply": bool(cfg.get("auto_reply")),
        "dry_run": bool(cfg.get("dry_run", True)),
        "send_mode": str(cfg.get("send_mode") or ""),
    }


def gateway_status() -> dict:
    token = admin_token()
    request = urllib.request.Request(GATEWAY_STATUS_URL)
    if token:
        request.add_header("X-Admin-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
        return {"ok": True, "status": payload}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"gateway http {exc.code}",
                "gateway_reachable": True}
    except Exception as exc:  # noqa: BLE001
        risk = gateway_risk_state()
        stopped = bool(risk.get("stop_file")) or not risk.get("enabled")
        hint = ("801 已被风控停止（STOP 文件存在或 enabled=false），这是预期状态；"
                "确认微信已登录后再按安全顺序恢复，不要直接开 auto_reply/自动发送。"
                if stopped else
                "确认 python -m agi.wechat_gateway.server 正在 127.0.0.1:801 运行。")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "gateway_reachable": False, "risk_stop": risk, "hint": hint}


# --------------------------------------------------------------------------- #
# gateway.log 事件流
# --------------------------------------------------------------------------- #
def _tail_lines(path: Path, want_lines: int) -> list[str]:
    target = Path(path)
    if not target.exists() or want_lines <= 0:
        return []
    lines: list[bytes] = []
    try:
        with target.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            buffer = b""
            while position > 0 and len(lines) < want_lines:
                step = min(READ_LIMIT_BYTES, position)
                position -= step
                handle.seek(position)
                buffer = handle.read(step) + buffer
                lines = buffer.splitlines()
            return [line.decode("utf-8", "replace") for line in lines[-want_lines:]]
    except OSError:
        return []


def _event_brief(event: dict) -> dict:
    brief = {"ts": event.get("ts"), "kind": event.get("kind")}
    for key in ("friend", "contact", "status", "reason", "text", "next_delay_s",
                "probability", "roll", "source", "error", "error_key", "stage",
                "tokens", "dropped", "injected", "mode", "before", "after"):
        if key in event:
            value = event.get(key)
            if isinstance(value, str) and len(value) > 300:
                value = value[:300] + "…"
            brief[key] = value
    return brief


def feed_events(limit: int = 200, kind_filter: str = "") -> list[dict]:
    try:
        limit = max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = 200
    wanted = {part.strip() for part in str(kind_filter or "").replace(",", " ").split()
              if part.strip()}
    lines = _tail_lines(GATEWAY_LOG, limit * 4 if wanted else limit)
    events: list[dict] = []
    for line in reversed(lines):                       # 新→旧
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "")
        if wanted and kind not in wanted:
            continue
        events.append(event)
        if len(events) >= limit:
            break
    return events


# --------------------------------------------------------------------------- #
# 联系人库（只读）
# --------------------------------------------------------------------------- #
DEFAULT_CONTACT_DB: Path | None = None


def _preferred_contact_stems() -> list[str]:
    """真实联系人优先：按 config 里 allowed/proactive/monitored 顺序算 sha1(alias)[:14]。"""
    try:
        import hashlib
        cfg = json.loads(Path(GATEWAY_CONFIG).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    ordered = []
    for key in ("proactive_contacts", "monitored_contacts", "allowed_contacts"):
        for alias in (cfg.get(key) or []):
            if alias and alias not in ordered:
                ordered.append(alias)
    return [hashlib.sha1(str(alias).encode()).hexdigest()[:14] for alias in ordered]


def contact_db_path() -> Path | None:
    """默认数据 DB：优先新拆分库的 memory 部分，其次未迁移的单库，最后 legacy。

    选择顺序：配置里的真实联系人（proactive/monitored/allowed）优先，
    其次"最近有新消息的库"；避免默认落到「文件传输助手」这类测试会话。
    """
    if DEFAULT_CONTACT_DB:
        target = Path(DEFAULT_CONTACT_DB)
        if target.is_file():
            return target
        # w2-inner 拆库后旧单库会被改名为 *.legacy.sqlite3，数据在 *.memory.sqlite3
        for suffix in (".memory.sqlite3", ".legacy.sqlite3"):
            sibling = target.with_name(target.stem + suffix)
            if Path(sibling).is_file():
                return sibling
        return target
    try:
        # 1) 配置里的真实联系人优先（有消息才用）
        for stem in _preferred_contact_stems():
            for suffix in (".memory.sqlite3", ".sqlite3", ".legacy.sqlite3"):
                candidate = Path(CONTACTS_DIR) / (stem + suffix)
                if not candidate.is_file():
                    continue
                try:
                    con = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True, timeout=2)
                    try:
                        count = con.execute("SELECT count(*) FROM messages").fetchone()[0]
                    finally:
                        con.close()
                except sqlite3.Error:
                    continue
                if count:
                    return candidate
        memory = sorted(Path(CONTACTS_DIR).glob("*.memory.sqlite3"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        singles = [p for p in sorted(Path(CONTACTS_DIR).glob("*.sqlite3"),
                                     key=lambda p: p.stat().st_mtime, reverse=True)
                   if _db_kind(p) == "single"]
        legacy = sorted(Path(CONTACTS_DIR).glob("*.legacy.sqlite3"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        candidates = memory + singles + legacy
    except OSError:
        return None
    for candidate in candidates:
        try:
            con = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True, timeout=2)
            try:
                count = con.execute("SELECT count(*) FROM messages").fetchone()[0]
            finally:
                con.close()
        except sqlite3.Error:
            continue
        if count:
            return candidate
    return candidates[0] if candidates else None


def _ro_connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    return con


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def read_inner(limit: int = 100, contact: str = "") -> dict:
    path = _db_for_contact(contact)
    if not path:
        return {"ok": False, "error": "contact_db_missing", "items": [],
                "hint": "该联系人还没有独立数据库；请先在 803 工作台创建并训练"}
    try:
        limit = max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = 100
    try:
        con = _ro_connect(path)
        try:
            cols = _columns(con, "inner_states")
            if not cols:
                return {"ok": True, "items": [], "db": str(path)}
            wanted = [name for name in
                      ("id", "turn_id", "mode", "state_json", "block", "injected",
                       "latency_ms", "fallback", "source", "created_at") if name in cols]
            rows = con.execute(
                f"SELECT {','.join(wanted)} FROM inner_states "
                "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "items": []}
    items = []
    for row in rows:
        item = {name: row[name] for name in row.keys()}
        try:
            state = json.loads(item.pop("state_json", "{}") or "{}")
        except ValueError:
            state = {}
        item["state"] = state if isinstance(state, dict) else {}
        item["block"] = str(item.get("block") or "")[:2000]
        item["injected"] = bool(item.get("injected"))
        items.append(item)
    return {"ok": True, "items": items, "db": str(path)}


def _db_for_contact(contact: str) -> Path | None:
    """按联系人 alias 解析数据库；空 alias = 当前默认库。"""
    raw = str(contact or "").strip()
    if not raw:
        return contact_db_path()
    return _contact_db_by_alias(raw)


#: 802 对话流气泡的送达状态（与 801 ``turns.delivery_status`` 对齐的 6 个值）
DELIVERY_STATUSES = ("sent", "unverified", "sender_unavailable", "failed", "draft", "unknown")
_DELIVERY_ALIASES = {
    "sent": "sent", "delivered": "sent", "ok": "sent",
    "unverified": "unverified", "partial": "unverified", "warning": "unverified",
    "send_timeout": "unverified",
    "sender_unavailable": "sender_unavailable",
    "failed": "failed", "blocked": "failed", "error": "failed",
    "draft": "draft", "planned": "draft", "pending": "draft",
}


def _normalize_delivery(value) -> str:
    """把 801 的 delivery_status 归一成前端认识的 6 个值；未知返回空串。"""
    return _DELIVERY_ALIASES.get(str(value or "").strip().lower(), "")


def _message_delivery(item: dict, turn_status: str = "") -> str:
    """单条消息的送达状态：turns 优先，其次 messages.delivered，缺失给 unknown。

    用户消息不属于「她发出去的东西」，返回空串（前端不显示状态标记）。
    """
    if str(item.get("role") or "") == "user":
        return ""
    normalized = _normalize_delivery(turn_status)
    if normalized:
        return normalized
    delivered = item.get("delivered")
    if delivered is not None and str(delivered).strip() in ("0", "0.0", "False", "false"):
        return "failed"
    return "unknown"


def _message_turn_statuses(path: Path, ids: list[str]) -> dict[str, str]:
    """批量取 ``turns.delivery_status``（assistant_message_id → status）。"""
    if not ids:
        return {}
    statuses: dict[str, str] = {}
    try:
        con = _ro_connect(path)
        try:
            tcols = _columns(con, "turns")
            if not tcols or "delivery_status" not in tcols \
                    or "assistant_message_id" not in tcols:
                return {}
            qmarks = ",".join("?" for _ in ids)
            for row in con.execute(
                    "SELECT assistant_message_id, delivery_status FROM turns "
                    f"WHERE assistant_message_id IN ({qmarks})", ids):
                value = str(row["delivery_status"] or "").strip()
                if value:
                    statuses[str(row["assistant_message_id"])] = value
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return statuses


def read_messages(limit: int = 100, contact: str = "") -> dict:
    path = _contact_db_by_alias(contact) if str(contact or "").strip() else contact_db_path()
    if not path:
        return {"ok": False, "error": "contact_db_not_found", "items": []}
    try:
        limit = max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = 100
    try:
        con = _ro_connect(path)
        try:
            cols = _columns(con, "messages")
            if not cols:
                return {"ok": True, "items": [], "db": str(path)}
            wanted = [name for name in ("id", "role", "text", "ts", "proactive",
                                        "archived_at", "delivered") if name in cols]
            rows = con.execute(
                f"SELECT {','.join(wanted)} FROM messages "
                "ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "items": []}
    items = [dict(row) for row in reversed(rows)]
    # 图片/表情消息：把 attachments 挂到消息上（前端显示缩略图 + 类型标签）
    ids = [str(item.get("id") or "") for item in items if item.get("id")]
    attachments: dict[str, dict] = {}
    if ids:
        try:
            con = _ro_connect(path)
            try:
                cols = _columns(con, "attachments")
                if cols:
                    qmarks = ",".join("?" for _ in ids)
                    wanted = [name for name in ("id", "message_id", "kind", "mime", "path")
                              if name in cols]
                    for row in con.execute(
                            f"SELECT {','.join(wanted)} FROM attachments "
                            f"WHERE message_id IN ({qmarks})", ids):
                        attachments[str(row["message_id"])] = dict(row)
            finally:
                con.close()
        except sqlite3.Error:
            attachments = {}
    chat_dir = Path(SELFLEARN_DIR) / "media_chat"
    for item in items:
        att = attachments.get(str(item.get("id") or ""))
        if not att:
            continue
        att = dict(att)
        target = Path(str(att.get("path") or ""))
        if not target.is_file():
            fallback = chat_dir / target.name
            if fallback.is_file():
                att["path"] = str(fallback)
                target = fallback
        if target.is_file():
            att["url"] = _media_thumb(str(target))
        item["attachment"] = att
    # A5：给每条「她发出去的消息」挂送达状态（turns.delivery_status → 归一化）
    turn_statuses = _message_turn_statuses(path, ids)
    for item in items:
        delivery = _message_delivery(
            item, turn_statuses.get(str(item.get("id") or ""), ""))
        item["delivery"] = delivery
        # unverified = 发送超时"可能已发出、不会自动重发"；前端据此显示而不是当失败
        item["possible_sent"] = delivery == "unverified"
    return {"ok": True, "db": str(path), "items": items}


def contacts_overview() -> dict:
    """联系人列表：alias / 库名 / 消息数 / 最近消息时间（只读）。"""
    current = contact_db_path()
    current_name = current.name if current else ""
    items: list[dict] = []
    for row in _contact_aliases():
        db_name = row.get("db")
        db_path = None
        for kind in ("memory", "single", "legacy"):
            files = _contact_paths(row["alias"])
            if files.get(kind):
                db_path = files[kind]
                break
        count, last_ts = 0, 0.0
        if db_path is not None:
            try:
                con = _ro_connect(db_path)
                try:
                    cols = _columns(con, "messages")
                    if cols:
                        row_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()
                        count = int(row_count[0] or 0) if row_count else 0
                        if "ts" in cols:
                            last = con.execute("SELECT MAX(ts) FROM messages").fetchone()
                            last_ts = float(last[0] or 0) if last else 0.0
                finally:
                    con.close()
            except sqlite3.Error:
                pass
        items.append({
            "alias": row["alias"],
            "db": db_name,
            "exists": bool(row.get("exists")),
            "messages": count,
            "last_ts": last_ts,
            "current": bool(current_name and db_name == current_name),
        })
    if current_name and not any(item["current"] for item in items):
        items.append({"alias": _contact_label(current_name), "db": current_name,
                      "exists": True, "messages": 0, "last_ts": 0.0,
                      "current": True})
    # 配置里没登记、但本机存在库的联系人也要能切（显示为「未命名库 <stem>」）
    known_dbs = {str(item.get("db") or "") for item in items}
    try:
        for db_path in sorted(Path(CONTACTS_DIR).glob("*.memory.sqlite3")):
            if db_path.name in known_dbs:
                continue
            stem = db_path.name.split(".memory.sqlite3")[0]
            count, last_ts = 0, 0.0
            try:
                con = _ro_connect(db_path)
                try:
                    cols = _columns(con, "messages")
                    if cols:
                        row_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()
                        count = int(row_count[0] or 0) if row_count else 0
                        if "ts" in cols:
                            last = con.execute("SELECT MAX(ts) FROM messages").fetchone()
                            last_ts = float(last[0] or 0) if last else 0.0
                finally:
                    con.close()
            except sqlite3.Error:
                pass
            items.append({"alias": "未命名库 " + stem, "db": db_path.name,
                          "exists": True, "messages": count, "last_ts": last_ts,
                          "current": db_path.name == current_name,
                          "unnamed": True})
    except OSError:
        pass
    deduped: dict[str, dict] = {}
    for item in items:
        key = str(item.get("alias") or item.get("db") or "")
        if key and key not in deduped:
            deduped[key] = item
    items = list(deduped.values())
    items.sort(key=lambda item: (not item["current"], -float(item["last_ts"] or 0)))
    return {"ok": True, "items": items,
            "current": current_name, "current_label": _contact_label(current_name) if current_name else ""}


def memory_snapshot(contact: str = "") -> dict:
    path = _db_for_contact(contact)
    if not path:
        return {"ok": False, "error": "contact_db_missing", "metrics": {},
                "recent": [], "hint": "该联系人还没有独立数据库；请先在 803 工作台创建并训练"}
    metrics: dict = {}
    recent: list[dict] = []
    # 先读最近记忆：metrics 可能触发 db 拆库迁移（旧单库改名 + 新 memory/persona 库）
    try:
        con = _ro_connect(path)
        try:
            cols = _columns(con, "user_memories")
            if cols:
                wanted = [name for name in
                          ("id", "kind", "content", "importance", "status",
                           "created_at", "access_count", "strength",
                           "last_strength_at", "forget_stage", "last_accessed") if name in cols]
                recent = []
                for row in con.execute(
                        f"SELECT {','.join(wanted)} FROM user_memories "
                        "ORDER BY created_at DESC LIMIT 50").fetchall():
                    item = dict(row)
                    item.update(_forget_meta(item))
                    recent.append(item)
        finally:
            con.close()
    except sqlite3.Error:
        recent = []
    try:
        from agi.selflearn import memory as memory_mod
        metrics = memory_mod.metrics(path)
    except Exception as exc:  # noqa: BLE001 - 指标失败不影响页面
        metrics = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "db": str(path), "metrics": metrics, "recent": recent}


def memory_strength(contact: str = "") -> dict:
    """P38：GET /api/memory/strength 只读（按遗忘阶段/强度）。"""
    path = _db_for_contact(contact)
    if not path:
        return {"ok": False, "error": "contact_db_missing", "count": 0,
                "stages": {}, "strongest": [], "weakest": [], "items": [],
                "hint": "该联系人还没有独立数据库；请先在 803 工作台创建并训练"}
    try:
        from agi.selflearn import memory_decay
        return memory_decay.strength_report(path)
    except Exception as exc:  # noqa: BLE001 - 页面只读观测，失败降级
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "count": 0, "stages": {}, "strongest": [], "weakest": [],
                "items": []}



# --------------------------------------------------------------------------- #
# 上网记录 / B站档案（只读）
# --------------------------------------------------------------------------- #
def _all_memory_dbs() -> list[Path]:
    paths: list[Path] = []
    try:
        paths.extend(sorted(Path(CONTACTS_DIR).glob("*.memory.sqlite3")))
    except OSError:
        pass
    paths.append(SELFLEARN_DIR / "selflearn.memory.sqlite3")
    return [path for path in paths if path.is_file()]


def web_records(limit: int = 100, q: str = "") -> dict:
    """所有联系人库的 research_sources，按时间倒序；只读。"""
    try:
        limit = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        limit = 100
    needle = str(q or "").strip()
    items: list[dict] = []
    for path in _all_memory_dbs():
        try:
            con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                if not con.execute("SELECT 1 FROM sqlite_master WHERE name='research_sources'").fetchone():
                    continue
                sql = "SELECT id,ts,query,url,title,snippet,turn_id FROM research_sources"
                params: list = []
                if needle:
                    sql += " WHERE query LIKE ? OR title LIKE ? OR url LIKE ? OR snippet LIKE ?"
                    like = f"%{needle}%"
                    params = [like, like, like, like]
                sql += " ORDER BY ts DESC LIMIT ?"
                rows = con.execute(sql, (*params, limit)).fetchall()
                for row in rows:
                    item = dict(row)
                    item["db"] = path.name
                    item["kind"] = "source"
                    items.append(item)
                # 2026-10-31：检索尝试（含 0 结果/失败）也要显示，否则“他说搜了但没记录”
                if con.execute("SELECT 1 FROM sqlite_master WHERE name='research_attempts'").fetchone():
                    attempts = con.execute(
                        "SELECT id,ts,query,status,results,error FROM research_attempts "
                        "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
                    for row in attempts:
                        item = dict(row)
                        item.update({"url": "", "title": "", "db": path.name,
                                     "kind": "attempt",
                                     "snippet": str(row["error"] or "")})
                        items.append(item)
            finally:
                con.close()
        except sqlite3.Error:
            continue
    items.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
    items = items[:limit]
    return {"ok": True, "items": items, "count": len(items)}


def _contact_label(db_name: str) -> str:
    """把联系人库文件名映射成用户看得懂的名字（不暴露哈希）。"""
    name = str(db_name or "")
    if name == "selflearn.memory.sqlite3":
        return "主库"
    try:
        cfg = json.loads(Path(GATEWAY_CONFIG).read_text(encoding="utf-8"))
        aliases = []
        for key in ("proactive_contacts", "monitored_contacts", "allowed_contacts"):
            for alias in (cfg.get(key) or []):
                if alias and alias not in aliases:
                    aliases.append(alias)
        for alias in aliases:
            if hashlib.sha1(str(alias).encode("utf-8")).hexdigest()[:14] in name:
                return str(alias)
    except Exception:  # noqa: BLE001
        pass
    return "本机记录"


def web_research(limit: int = 100, q: str = "") -> dict:
    """上网检索记录（前端 /api/web/research）：补时间、域名、联系人与 total。"""
    base = web_records(limit=limit, q=q)
    items = []
    for item in base.get("items", []):
        try:
            domain = urlsplit(str(item.get("url") or "")).hostname or ""
        except Exception:  # noqa: BLE001
            domain = ""
        ts = float(item.get("ts") or 0)
        items.append({
            "ts": ts,
            "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "",
            "query": item.get("query") or "",
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "domain": domain,
            "snippet": item.get("snippet") or "",
            "contact": _contact_label(item.get("db") or ""),
            "kind": item.get("kind") or "source",
            "status": item.get("status") or "",
            "results": item.get("results"),
        })
    return {"ok": True, "items": items, "total": len(items)}


def _bili_impressions(limit: int = 100, q: str = "") -> dict:
    try:
        limit = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        limit = 100
    needle = str(q or "").strip()
    merged: dict[str, dict] = {}
    for path in _all_memory_dbs():
        try:
            con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                if not con.execute("SELECT 1 FROM sqlite_master WHERE name='bili_impressions'").fetchone():
                    continue
                sql = "SELECT * FROM bili_impressions"
                params: list = []
                if needle:
                    sql += " WHERE title LIKE ? OR summary LIKE ? OR why LIKE ? OR owner LIKE ?"
                    like = f"%{needle}%"
                    params = [like, like, like, like]
                sql += " ORDER BY created_at DESC LIMIT ?"
                for row in con.execute(sql, (*params, limit)).fetchall():
                    item = dict(row)
                    try:
                        item["topics"] = json.loads(item.pop("topics_json", "[]") or "[]")
                    except (TypeError, ValueError):
                        item["topics"] = []
                    item["db"] = path.name
                    old = merged.get(str(item.get("bvid") or ""))
                    if old is None or float(item.get("created_at") or 0) > float(old.get("created_at") or 0):
                        merged[str(item.get("bvid") or "")] = item
            finally:
                con.close()
        except sqlite3.Error:
            continue
    items = list(merged.values())
    items.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
    items = items[:limit]
    return {"ok": True, "items": items, "count": len(items)}


def bili_feed(limit: int = 100, filter: str = "all", q: str = "") -> dict:
    """B站刷到的视频（前端 /api/bili/feed）；filter=all/shared/skipped。"""
    mode = str(filter or "all").strip().lower()
    if mode not in ("all", "shared", "skipped"):
        mode = "all"
    raw = _bili_impressions(limit=1000, q=q)
    items = []
    for item in raw.get("items", []):
        shared = bool(item.get("shared"))
        if mode == "shared" and not shared:
            continue
        if mode == "skipped" and shared:
            continue
        ts = float(item.get("created_at") or 0)
        row = dict(item)
        row.update({
            "ts": ts,
            "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "",
            "shared": shared,
            "topics": item.get("topics") or [],
        })
        items.append(row)
    items = items[:max(1, min(500, int(limit or 100)))]
    return {"ok": True, "items": items, "total": len(items), "filter": mode}


def _bili_md_list(directory: Path) -> list[dict]:
    items = []
    try:
        files = sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            stat = path.stat()
        except OSError:
            continue
        preview = ""
        for line in text.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                preview = line[:60]
                break
        items.append({"name": path.name, "chars": len(text.strip()),
                      "preview": preview, "mtime": stat.st_mtime})
    return items


def bili_digests(name: str = "") -> dict:
    directory = bili_home() / "digest"
    if not name:
        items = _bili_md_list(directory)
        return {"ok": True, "items": items, "count": len(items)}
    return _bili_md_read(directory, name)


def bili_reports(name: str = "") -> dict:
    directory = bili_home() / "reports"
    if not name:
        items = _bili_md_list(directory)
        return {"ok": True, "items": items, "count": len(items)}
    return _bili_md_read(directory, name)


def bili_reports_index() -> dict:
    """视频报告清单：name/bvid/date/size/mtime（前端 /api/bili/reports）。"""
    directory = bili_home() / "reports"
    items = []
    try:
        files = sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    for path in files:
        name = path.name
        match = re.match(r"^(BV[0-9A-Za-z]+)-(\d{4}-\d{2}-\d{2})\.md$", name)
        bvid = match.group(1) if match else ""
        date = match.group(2) if match else ""
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({"name": name, "bvid": bvid, "date": date,
                      "size": stat.st_size, "mtime": stat.st_mtime})
    return {"ok": True, "items": items, "count": len(items)}


def bili_digest() -> dict:
    """解析 digest/*.md 的结构化摘要行；解析失败保留原文行。"""
    directory = bili_home() / "digest"
    items: list[dict] = []
    try:
        files = sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line.startswith("- "):
                continue
            body = line[2:].strip()
            parts = [part.strip() for part in body.split(" · ")]
            if len(parts) >= 6:
                url = parts[-1]
                items.append({"date": parts[0], "bvid": parts[1], "title": parts[2],
                              "owner": parts[3],
                              "reason": " · ".join(parts[4:-1]), "url": url,
                              "line": body, "file": path.name})
            else:
                items.append({"date": "", "bvid": "", "title": body,
                              "owner": "", "reason": "", "url": "",
                              "line": body, "file": path.name})
    items.sort(key=lambda item: (item.get("date") or "", item.get("bvid") or ""), reverse=True)
    return {"ok": True, "items": items, "count": len(items)}


def _bili_md_read(directory: Path, name: str) -> dict:
    raw = str(name or "").strip()
    if not raw or Path(raw).name != raw or not raw.lower().endswith(".md"):
        return {"ok": False, "error": "invalid_name"}
    target = (directory / raw).resolve()
    try:
        target.relative_to(directory.resolve())
    except ValueError:
        return {"ok": False, "error": "invalid_name"}
    if not target.is_file():
        return {"ok": False, "error": "not_found"}
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "name": raw, "text": text, "chars": len(text.strip())}


def bili_duty_log(lines: int = 200) -> dict:
    try:
        lines = max(1, min(1000, int(lines)))
    except (TypeError, ValueError):
        lines = 200
    path = bili_home() / "duty.log"
    if not path.is_file():
        return {"ok": True, "lines": [], "count": 0, "path": str(path),
                "note": "日志文件不存在"}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    tail = text.splitlines()[-lines:]
    return {"ok": True, "lines": tail, "count": len(tail), "path": str(path)}


# --------------------------------------------------------------------------- #
# 日记（只读）
# --------------------------------------------------------------------------- #
_DIARY_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def diary_list(limit: int = 60) -> dict:
    """列出所有日记：日期 + 字数 + 一句预览。只读，不创建/修改文件。"""
    try:
        limit = max(1, min(365, int(limit)))
    except (TypeError, ValueError):
        limit = 60
    items: list[dict] = []
    try:
        files = sorted(Path(DIARY_DIR).glob("*.txt"), reverse=True)
    except OSError:
        files = []
    for path in files:
        date = path.stem
        if not _DIARY_DATE_RE.match(date):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            stat = path.stat()
        except OSError:
            continue
        clean = text.strip()
        preview = ""
        for line in clean.splitlines():
            line = line.strip()
            if line:
                preview = line[:40]
                break
        items.append({"date": date, "chars": len(clean),
                      "preview": preview, "mtime": stat.st_mtime})
    items = items[:limit]
    return {"ok": True, "items": items, "count": len(items),
            "dir": str(DIARY_DIR)}


def diary_get(date: str) -> dict:
    """按日期读日记；日期严格 YYYY-MM-DD，防路径穿越。"""
    value = str(date or "").strip()
    if not _DIARY_DATE_RE.match(value):
        return {"ok": False, "error": "invalid_date",
                "hint": "日期格式应为 YYYY-MM-DD"}
    try:
        time.strptime(value, "%Y-%m-%d")
    except ValueError:
        return {"ok": False, "error": "invalid_date", "hint": "日期不存在"}
    base = Path(DIARY_DIR).resolve()
    target = (base / f"{value}.txt").resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return {"ok": False, "error": "invalid_date", "hint": "路径不合法"}
    if not target.is_file():
        return {"ok": False, "error": "not_found", "hint": f"没有 {value} 的日记"}
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
        stat = target.stat()
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "date": value, "text": text,
            "chars": len(text.strip()), "mtime": stat.st_mtime}


# --------------------------------------------------------------------------- #
# 语气对照
# --------------------------------------------------------------------------- #
_ENDING_KEYS = {"无标点": "none", "问号": "question", "感叹号": "exclaim",
                "句号": "period", "省略号": "ellipsis"}


def _ending_of(text: str) -> str:
    value = str(text or "").rstrip()
    if not value:
        return "none"
    last = value[-1]
    return {"?": "question", "？": "question", "!": "exclaim", "！": "exclaim",
            "。": "period", "…": "ellipsis"}.get(last, "none")


def _tone_stats(texts: list[str]) -> dict:
    cleaned = [str(t).strip() for t in texts if str(t or "").strip()]
    total = len(cleaned)
    counts = {"none": 0, "question": 0, "exclaim": 0, "period": 0, "ellipsis": 0}
    filler = 0
    lengths = []
    for text in cleaned:
        counts[_ending_of(text)] = counts.get(_ending_of(text), 0) + 1
        lengths.append(len(text))
        if any(word in text for word in FILLER_WORDS):
            filler += 1
    median = None
    if lengths:
        ordered = sorted(lengths)
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (
            (ordered[mid - 1] + ordered[mid]) / 2)
    return {
        "count": total,
        "length_median": median,
        "ending": {key: round(value / total, 4) if total else None
                   for key, value in counts.items()},
        "filler_rate": round(filler / total, 4) if total else None,
        "period_rate": round(counts["period"] / total, 4) if total else None,
        "question_rate": round(counts["question"] / total, 4) if total else None,
    }


def _tone_baseline() -> dict:
    try:
        text = Path(TONE_PROFILE).read_text(encoding="utf-8")
    except OSError:
        return {}
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not blocks:
        return {}
    try:
        payload = json.loads(blocks[-1])
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    length = payload.get("length") or {}
    ending_raw = payload.get("ending") or {}
    ending = {}
    for name, value in ending_raw.items():
        key = _ENDING_KEYS.get(str(name))
        if key:
            ending[key] = value
    ai = payload.get("ai") or {}
    return {"length_median": length.get("median"),
            "length_mean": length.get("mean"),
            "ending": ending,
            "emoji_rate": payload.get("emoji_rate"),
            "self_rows": payload.get("self_rows"),
            "ai_proactive": (ai.get("proactive") or {}).get("length_median"),
            "ai_reply": (ai.get("reply") or {}).get("length_median")}


def tone_snapshot(limit: int = 100) -> dict:
    path = contact_db_path()
    if not path:
        return {"ok": False, "error": "contact_db_not_found"}
    try:
        limit = max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = 100
    texts: list[str] = []
    try:
        con = _ro_connect(path)
        try:
            rows = con.execute(
                "SELECT text FROM messages WHERE role='assistant' "
                "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
            texts = [str(row[0]) for row in rows]
        finally:
            con.close()
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    baseline = _tone_baseline()
    ai = _tone_stats(texts)
    gap = {}
    try:
        if baseline.get("length_median") is not None and ai.get("length_median") is not None:
            gap["length_median_ratio"] = round(
                float(ai["length_median"]) / max(1.0, float(baseline["length_median"])), 2)
        base_period = (baseline.get("ending") or {}).get("period")
        base_none = (baseline.get("ending") or {}).get("none")
        if base_period is not None and ai.get("period_rate") is not None:
            gap["period_delta"] = round(float(ai["period_rate"]) - float(base_period), 4)
        if base_none is not None and ai["ending"].get("none") is not None:
            gap["no_punct_delta"] = round(
                float(ai["ending"]["none"]) - float(base_none), 4)
    except (TypeError, ValueError):
        gap = {}
    return {"ok": True, "sample_n": ai.get("count"), "ai": ai,
            "baseline": baseline, "gap": gap,
            "baseline_note": "真人 self 语料（微信截图 OCR + 联系人库 user 消息）"}


# --------------------------------------------------------------------------- #
# 模型 / 设置 / 提示词
# --------------------------------------------------------------------------- #
def _model_registry():
    """尝试 import 本地模型注册表；不可用返回 None（单测可 monkeypatch 本函数注入假表）。"""
    try:
        from agi.selflearn import model_registry
        return model_registry
    except Exception:  # noqa: BLE001
        return None


def models_snapshot() -> dict:
    registry = _model_registry()
    if registry is None:
        return {"ok": False, "error": "model_registry unavailable", "models": []}
    try:
        models = registry.list_models()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "models": []}
    items = []
    for model in models:
        name = str(model.get("name") or "")
        try:
            healthy = bool(registry.health(name))
        except Exception:  # noqa: BLE001
            healthy = False
        runtime: dict = {}
        try:
            if hasattr(registry, "runtime_info"):
                runtime = dict(registry.runtime_info(name) or {})
        except Exception:  # noqa: BLE001
            runtime = {}
        running = bool(runtime.get("running", healthy))
        items.append({
            "name": name, "port": model.get("port"), "role": model.get("role"),
            "status": model.get("status"), "purpose": model.get("purpose"),
            "healthy": healthy, "editable": name in MODEL_ALLOWLIST,
            "reserved": str(model.get("role")) == "reserved"
                        or str(model.get("status")) == "reserved",
            "gguf": model.get("gguf"),
            "running": running, "pid": runtime.get("pid"),
            "external": bool(runtime.get("external")),
            "hint": "该模型是外部启动的，停止前会二次确认"
                    if runtime.get("external") else "",
        })
    return {"ok": True, "models": items,
            "allow_start": sorted(MODEL_ALLOWLIST)}


#: P20：注册表错误码 → 控制台中文提示（前端不再显示英文 error 原文）
MODEL_ERROR_HINTS = {
    "model_registry unavailable": "模型注册表不可用，请检查 agi/selflearn/model_registry.py 与配置",
    "model_not_editable": "该模型不允许启停（只允许 soul-05b；reserved/研究模型不可动）",
    "not_started_by_registry": "该模型不是控制台启动的（可能已停止，或由外部命令启动）",
    "external": "该模型是外部启动的 llama-server，停止前需要二次确认",
    "unsafe_process": "端口上的进程不是 llama-server（或缺少 .gguf），为安全起见已拒绝停止",
    "port_in_use_by_other_process": "端口已被非 llama-server 进程占用，为安全起见不启动",
    "reserved_model_not_started": "该模型为 reserved 预留位，禁止启动",
    "gguf_not_found": "GGUF 模型文件不存在，请检查模型配置",
    "model_has_no_port": "该模型没有登记端口，无法启停",
    "llama_server_missing": "找不到 llama-server.exe，请检查 llama.cpp 路径",
}


def _with_model_hint(result: dict) -> dict:
    """给 start/stop 结果补中文 hint；外部进程的 hint 以注册表返回为准。"""
    data = dict(result or {})
    if data.get("hint"):
        return data
    key = str(data.get("error") or data.get("status") or "")
    data["hint"] = MODEL_ERROR_HINTS.get(key) or ("操作失败：" + (key or "未知错误"))
    return data


# --------------------------------------------------------------------------- #
# P30：每日语料/训练门禁只读接口
# --------------------------------------------------------------------------- #
TRAIN_ROOT = ROOT / "agi" / "data" / "train"
TRAIN_DAILY_DIR = TRAIN_ROOT / "daily"
TRAIN_HISTORY_PATH = TRAIN_ROOT / "history.jsonl"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def train_status() -> dict:
    """今日/累计样本、最近训练、门禁状态（只读，不改任何文件）。"""
    today = time.strftime("%Y-%m-%d")
    today_rows = _read_jsonl(TRAIN_DAILY_DIR / f"{today}.jsonl")
    daily_files = sorted(TRAIN_DAILY_DIR.glob("*.jsonl")) if TRAIN_DAILY_DIR.exists() else []
    cumulative = sum(len(_read_jsonl(path)) for path in daily_files)
    history = _read_jsonl(TRAIN_HISTORY_PATH)
    last = history[-1] if history else None
    gate: dict = {}
    min_samples = 300
    try:
        try:
            from tools.train import train_gate
        except ImportError:  # 直接 `python tools/console/server.py` 时 repo root 不在 sys.path
            import sys as _sys
            if str(ROOT) not in _sys.path:
                _sys.path.insert(0, str(ROOT))
            from tools.train import train_gate
        min_samples = int(getattr(train_gate, "MIN_SAMPLES", min_samples))
        gate = train_gate.check_ready()
    except Exception as exc:  # noqa: BLE001 - 页面只读观测，失败降级
        gate = {"ready": None, "error": f"{type(exc).__name__}: {exc}"}
    usable = sum(1 for row in today_rows
                 if str(row.get("meta", {}).get("quality") or "") in ("positive", "correction"))
    dates = [path.stem for path in sorted(daily_files, reverse=True)[:14]]
    return {"ok": True, "today": {"date": today, "samples": len(today_rows),
                                  "usable": usable},
            "cumulative": {"samples": cumulative, "files": len(daily_files)},
            "daily": {"dates": dates, "files": len(daily_files)},
            "min_samples": min_samples,
            "last_training": last, "gate": gate,
            "next_ready": gate.get("next_ready") if isinstance(gate, dict) else None,
            "hint": "只读展示；训练默认只计划，不自动执行"}


def train_daily(date: str) -> dict:
    """某天日报 + 语气总结 markdown 原文。"""
    date = str(date or "").strip()
    if not date:
        date = time.strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return {"ok": False, "error": "bad_date", "hint": "日期格式应为 YYYY-MM-DD"}
    report = TRAIN_DAILY_DIR / f"{date}.md"
    summary = TRAIN_DAILY_DIR / f"{date}.summary.md"
    if not report.exists():
        return {"ok": False, "error": "not_found", "date": date,
                "hint": f"{date} 还没有日报（当天没有跑流水线？）"}
    return {"ok": True, "date": date,
            "report": report.read_text(encoding="utf-8"),
            "summary": summary.read_text(encoding="utf-8") if summary.exists() else "",
            "path": str(report)}




def _config_view() -> dict:
    raw = gateway_config()
    view = {key: raw.get(key, default) for key, default in EDITABLE_DEFAULTS.items()}
    readonly = {key: raw.get(key) for key in READONLY_CONFIG_KEYS if key != "admin_token"}
    return {"ok": True, "config": view, "readonly": readonly,
            "stopped": stop_active(), "path": str(GATEWAY_CONFIG)}


def _validate_config(updates: dict) -> tuple[dict, list[str]]:
    if not isinstance(updates, dict):
        return {}, ["body 必须是对象"]
    cleaned: dict = {}
    errors: list[str] = []
    allowed = set(EDITABLE_DEFAULTS)
    for key, value in updates.items():
        if key not in allowed:
            errors.append(f"not_allowed:{key}")
            continue
        try:
            if key == "proactive_enabled":
                cleaned[key] = bool(value) if isinstance(value, bool) else _as_bool(value)
            elif key == "quiet_hours":
                if not isinstance(value, (list, tuple)) or len(value) != 2:
                    raise ValueError("quiet_hours 需要 [start,end]")
                hours = [int(value[0]), int(value[1])]
                if not all(0 <= hour <= 24 for hour in hours):
                    raise ValueError("quiet_hours 取值 0–24")
                cleaned[key] = hours
            elif key in ("proactive_interval_min_s", "proactive_interval_max_s"):
                number = int(value)
                if not 10 <= number <= 86400:
                    raise ValueError(f"{key} 取值 10–86400")
                cleaned[key] = number
            elif key in ("proactive_probability_min", "proactive_probability_max"):
                number = float(value)
                if not 0.0 <= number <= 1.0:
                    raise ValueError(f"{key} 取值 0–1")
                cleaned[key] = round(number, 4)
            elif key == "inner_mode":
                text = str(value or "").strip().lower()
                if text not in INNER_MODES:
                    raise ValueError("inner_mode 非法")
                cleaned[key] = text
            elif key == "inner_shadow_only":
                cleaned[key] = bool(value) if isinstance(value, bool) else _as_bool(value)
            elif key == "inner_mode_scope":
                text = str(value or "").strip().lower()
                if text not in INNER_SCOPES:
                    raise ValueError("inner_mode_scope 非法")
                cleaned[key] = text
            elif key == "inner_schema":
                text = str(value or "").strip().lower()
                if text not in INNER_SCHEMAS:
                    raise ValueError("inner_schema 非法")
                cleaned[key] = text
            elif key == "reply_max_bubbles":
                number = int(value)
                if not 0 <= number <= 20:
                    raise ValueError("reply_max_bubbles 取值 0–20（0=自动）")
                cleaned[key] = number
            elif key == "user_llm_ratio":
                text = str(value or "").strip()
                if text not in ("1:1", "1:2", "1:3"):
                    raise ValueError("user_llm_ratio 只支持 1:1/1:2/1:3")
                cleaned[key] = text
            elif key == "proactive_method":
                text = str(value or "").strip().lower()
                if text not in ("prob", "timer", "topic"):
                    raise ValueError("proactive_method 只支持 prob/timer/topic")
                cleaned[key] = text
            elif key == "media_enabled":
                cleaned[key] = bool(value) if isinstance(value, bool) else _as_bool(value)
            elif key in ("proactive_max_per_day", "proactive_max_bubbles"):
                number = int(value)
                limit = 20 if key == "proactive_max_per_day" else 5
                if not 0 <= number <= limit:
                    raise ValueError(f"{key} 取值 0–{limit}")
                cleaned[key] = number
        except (TypeError, ValueError) as exc:
            errors.append(f"{key}: {exc}")
    merged = {**{key: gateway_config().get(key, default)
                 for key, default in EDITABLE_DEFAULTS.items()}, **cleaned}
    try:
        if int(merged["proactive_interval_min_s"]) > int(merged["proactive_interval_max_s"]):
            errors.append("interval_min_s > interval_max_s")
        if float(merged["proactive_probability_min"]) > float(
                merged["proactive_probability_max"]):
            errors.append("probability_min > probability_max")
    except (TypeError, ValueError):
        errors.append("interval/probability 类型错误")
    return cleaned, errors


def _as_bool(value) -> bool:
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"非法布尔值: {value!r}")


def apply_config(updates: dict, *, client: str = "") -> dict:
    cleaned, errors = _validate_config(updates)
    if errors:
        audit("config_update", {"keys": sorted(updates or {})}, ok=False,
              note=";".join(errors)[:200], client=client)
        return {"ok": False, "errors": errors, "config": _config_view()["config"]}
    raw = gateway_config()
    before = {key: raw.get(key) for key in cleaned}
    raw.update(cleaned)
    try:
        write_json_atomic(GATEWAY_CONFIG, raw)
    except OSError as exc:
        audit("config_update", {"keys": sorted(cleaned)}, ok=False,
              note=str(exc), client=client)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    audit("config_update", {"keys": sorted(cleaned), "before": before,
                            "after": cleaned}, ok=True, client=client)
    view = _config_view()
    view["note"] = "已保存；网关热加载，下一条消息/下一轮 proactive 生效"
    return view


#: P4-d：11 个内置提示词键的中文标签与说明（与 agi/selflearn/PROMPTS.md 对齐）
PROMPT_LABELS: dict[str, dict] = {
    "proactive.persona_constraints": {
        "label": "人格化边界",
        "desc": "轻度人格化：只写数字生活/感受，禁止物理起居细节；影响每条主动 prompt"},
    "proactive.kind.care": {
        "label": "care 关心",
        "desc": "主动消息 care 类型指令：关心近况，低活跃时使用"},
    "proactive.kind.share": {
        "label": "share 分享",
        "desc": "主动消息 share 类型指令：分享自己的事/想法"},
    "proactive.kind.light": {
        "label": "light 轻松",
        "desc": "主动消息 light 类型指令：轻松调侃/接梗"},
    "proactive.kind.followup": {
        "label": "followup 接话",
        "desc": "主动消息 followup 类型指令：承接上次没聊完的话题"},
    "proactive.kind.silence_ack": {
        "label": "silence_ack 久未联系",
        "desc": "久未联系后的轻打招呼（低频）"},
    "proactive.anti_fabrication": {
        "label": "不编造约束",
        "desc": "P0-9：禁止编造共同经历，必须锚定真实记忆"},
    "proactive.punctuation": {
        "label": "标点/分句约束",
        "desc": "P0-10/13：run-on 分句与句末标点规则"},
    "proactive.continuity": {
        "label": "连续性约束",
        "desc": "P0-17：continuation / new_topic 的承接规则"},
    "proactive.voice_fewshot": {
        "label": "真实语气 few-shot",
        "desc": "真实语气示例段（参考语气，不要照抄内容）"},
    "chat.system": {
        "label": "被动对话 SYSTEM",
        "desc": "chat.send 的 system message：身份、语气与边界"},
}


def _prompt_store():
    """尝试复用 P4-b 的 prompt_store（同一仓库/同一 Python）；不可用返回 None。"""
    try:
        from agi.selflearn import prompt_store
        return prompt_store
    except Exception:  # noqa: BLE001
        return None


def _prompt_labels(keys) -> dict[str, dict]:
    return {str(key): PROMPT_LABELS.get(str(key), {"label": str(key), "desc": ""})
            for key in keys}


def prompts_state() -> dict:
    """提示词状态：defaults / overrides / effective（优先 prompt_store，降级直读 JSON）。"""
    store = _prompt_store()
    if store is not None:
        try:
            snapshot = store.snapshot()
            keys = list(getattr(store, "KNOWN_KEYS", ()) or snapshot.get("defaults", {}))
            raw = read_json(Path(store.prompts_path()), {})
            updated_at = raw.get("updated_at") if isinstance(raw, dict) else None
            return {
                "ok": True, "degraded": False,
                "version": snapshot.get("version"),
                "path": snapshot.get("path") or str(PROMPTS_PATH),
                "updated_at": updated_at,
                "defaults": snapshot.get("defaults") or {},
                "overrides": snapshot.get("overrides") or {},
                "effective": snapshot.get("effective") or {},
                "labels": _prompt_labels(keys),
                "config": {"max_chars": getattr(store, "MAX_CHARS_PER_ENTRY", 4000),
                           "max_file_bytes": getattr(store, "MAX_FILE_BYTES", 65536),
                           "keys": keys},
                "note": "保存后下一条消息 / 下一次 proactive tick 生效（mtime 热加载，无需重启）",
            }
        except Exception:  # noqa: BLE001 - 降级
            pass
    payload = read_json(PROMPTS_PATH, {})
    prompts_raw = payload.get("prompts") if isinstance(payload, dict) else {}
    prompts = ({str(k): str(v) for k, v in prompts_raw.items() if isinstance(k, str)}
               if isinstance(prompts_raw, dict) else {})
    return {
        "ok": True, "degraded": True,
        "version": int(payload.get("version") or 1) if isinstance(payload, dict) else 1,
        "path": str(PROMPTS_PATH),
        "updated_at": payload.get("updated_at") if isinstance(payload, dict) else None,
        "defaults": {}, "overrides": prompts, "effective": prompts,
        "labels": _prompt_labels(PROMPT_LABELS.keys()),
        "config": {"max_chars": 4000, "max_file_bytes": 65536,
                   "keys": list(PROMPT_LABELS.keys())},
        "note": "prompt_store 不可用：已降级为直接读写 prompts.json（看不到代码默认值）",
    }


_PROMPT_KEY = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")
LEGACY_PROMPT_MAX_CHARS = 20000


def save_prompts(prompts: dict, *, client: str = "") -> dict:
    """保存覆盖：优先 prompt_store.set_many（校验/原子写/热加载）；不可用走旧 JSON 路径。"""
    if not isinstance(prompts, dict) or not prompts:
        return {"ok": False, "error": "prompts 必须是非空对象"}
    store = _prompt_store()
    if store is not None:
        before = dict(store.overrides())
        try:
            payload = store.set_many({str(k): str(v or "") for k, v in prompts.items()})
        except Exception as exc:  # noqa: BLE001 - 校验失败返回错误
            audit("prompt_save", {"keys": sorted(prompts)}, ok=False,
                  note=str(exc), client=client)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        after = dict(payload.get("prompts") or {})
        changed = sorted(key for key in set(before) | set(after)
                         if before.get(key) != after.get(key))
        audit("prompt_save", {"keys": sorted(prompts), "changed": changed},
              ok=True, client=client)
        try:
            snapshot = store.snapshot()
            effective = snapshot.get("effective") or {}
        except Exception:  # noqa: BLE001
            effective = after
        return {"ok": True, "overrides": after, "effective": effective,
                "changed_keys": changed,
                "note": "已保存；下一条消息 / 下一次 proactive tick 生效"}
    cleaned: dict[str, str] = {}
    for key, value in prompts.items():
        if not isinstance(key, str) or not _PROMPT_KEY.match(key):
            return {"ok": False, "error": f"非法 key: {key!r}"}
        text = str(value or "")
        if len(text) > LEGACY_PROMPT_MAX_CHARS:
            return {"ok": False, "error": f"{key} 超过 {LEGACY_PROMPT_MAX_CHARS} 字"}
        cleaned[key] = text
    payload = read_json(PROMPTS_PATH, {})
    current = payload.get("prompts") if isinstance(payload, dict) else {}
    merged = dict(current) if isinstance(current, dict) else {}
    before = dict(merged)
    for key, text in cleaned.items():
        if text == "":
            merged.pop(key, None)
        else:
            merged[key] = text
    try:
        write_json_atomic(PROMPTS_PATH, {"version": 1, "prompts": merged})
    except OSError as exc:
        audit("prompt_save", {"keys": sorted(cleaned)}, ok=False, note=str(exc),
              client=client)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    changed = sorted(key for key in set(before) | set(merged)
                     if before.get(key) != merged.get(key))
    audit("prompt_save", {"keys": sorted(cleaned), "changed": changed},
          ok=True, client=client)
    return {"ok": True, "overrides": merged, "effective": merged,
            "changed_keys": changed, "degraded": True,
            "note": "已保存（降级模式：直接写 prompts.json）；下一条消息生效"}


def reset_prompts(keys: list[str] | None = None, *, all: bool = False,
                  client: str = "") -> dict:
    """恢复默认：指定键或全部（优先 prompt_store.delete/delete_all）。"""
    store = _prompt_store()
    requested = [str(key) for key in (keys or []) if str(key)]
    if not all and not requested:
        return {"ok": False, "error": "需要 keys 或 all=true"}
    if store is not None:
        before = dict(store.overrides())
        try:
            if all:
                store.delete_all()
            else:
                unknown = [key for key in requested if key not in store.KNOWN_KEY_SET]
                if unknown:
                    return {"ok": False, "error": f"未知键: {','.join(unknown)}"}
                store.set_many({key: "" for key in requested})
        except Exception as exc:  # noqa: BLE001
            audit("prompt_reset", {"keys": requested, "all": bool(all)}, ok=False,
                  note=str(exc), client=client)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        after = dict(store.overrides())
        changed = sorted(before) if all else requested
        try:
            effective = store.snapshot().get("effective") or {}
        except Exception:  # noqa: BLE001
            effective = after
        audit("prompt_reset", {"keys": requested, "all": bool(all),
                              "changed": changed}, ok=True, client=client)
        return {"ok": True, "overrides": after, "effective": effective,
                "changed_keys": changed,
                "note": "已恢复默认；下一条消息 / 下一次 proactive tick 生效"}
    payload = read_json(PROMPTS_PATH, {})
    current = payload.get("prompts") if isinstance(payload, dict) else {}
    merged = dict(current) if isinstance(current, dict) else {}
    before = dict(merged)
    try:
        if all:
            Path(PROMPTS_PATH).unlink(missing_ok=True)
            merged = {}
        else:
            for key in requested:
                merged.pop(key, None)
            write_json_atomic(PROMPTS_PATH, {"version": 1, "prompts": merged})
    except OSError as exc:
        audit("prompt_reset", {"keys": requested, "all": bool(all)}, ok=False,
              note=str(exc), client=client)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    changed = sorted(before) if all else requested
    audit("prompt_reset", {"keys": requested, "all": bool(all), "changed": changed},
          ok=True, client=client)
    return {"ok": True, "overrides": merged, "effective": merged,
            "changed_keys": changed, "degraded": True,
            "note": "已恢复默认（降级模式）；下一条消息生效"}


# --------------------------------------------------------------------------- #
# P4-c：恢复出厂设置（先备份 → 白名单路径 → 删除运行数据）
# --------------------------------------------------------------------------- #
def reset_roots() -> list[Path]:
    """白名单根：只允许删 data/ 与 log/ 下的内容（以及其中的指定文件）。"""
    return [Path(DATA_DIR), Path(LOG_DIR)]


def reset_targets() -> tuple[list[Path], list[Path]]:
    """返回 (要清空的目录, 要删除的文件)；目录只清内容、保留目录本身。"""
    dirs = [Path(SELFLEARN_DIR)]
    files = [Path(GATEWAY_LOG), Path(DRAFTS_PATH), Path(PROMPTS_PATH),
             Path(LOG_MESSAGES), Path(LOG_CHAT)]
    return dirs, files


def _resolved_within(path: Path | str, roots: list[Path]) -> Path:
    target = Path(path).resolve()
    for root in roots:
        try:
            target.relative_to(Path(root).resolve())
            return target
        except ValueError:
            continue
    raise ValueError(f"unsafe_target:{path}")


def _safe_targets() -> tuple[list[Path], list[Path], list[Path]]:
    roots = reset_roots()
    dirs, files = reset_targets()
    safe_dirs = [_resolved_within(item, roots) for item in dirs]
    safe_files = [_resolved_within(item, roots) for item in files]
    return safe_dirs, safe_files, roots


def _arcname(path: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return "external/" + resolved.name


def _count_file_lines(path: Path, limit: int = 2_000_000) -> int | None:
    try:
        target = Path(path)
        if not target.is_file():
            return 0
        if target.stat().st_size > 50 * 1024 * 1024:
            return None
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return None


def _count_table(con: sqlite3.Connection, table: str) -> int:
    try:
        return int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0


def _factory_stats() -> dict:
    contacts = 0
    messages = memories = claims = inner = 0
    try:
        db_files = sorted(Path(SELFLEARN_DIR).rglob("*.sqlite3"))
    except OSError:
        db_files = []
    for db in db_files:
        is_contact = "wechat_contacts" in db.parts
        try:
            con = _ro_connect(db)
        except sqlite3.Error:
            continue
        try:
            messages += _count_table(con, "messages")
            memories += _count_table(con, "user_memories")
            claims += _count_table(con, "claims")
            inner += _count_table(con, "inner_states")
        finally:
            con.close()
        if is_contact:
            contacts += 1
    prompts = read_json(PROMPTS_PATH, {})
    prompt_overrides = len(prompts.get("prompts") or {}) if isinstance(prompts, dict) else 0
    return {
        "contacts": contacts,
        "messages": messages,
        "memories": memories,
        "claims": claims,
        "inner_states": inner,
        "prompt_overrides": prompt_overrides,
        "gateway_events": _count_file_lines(GATEWAY_LOG),
        "drafts": _count_file_lines(DRAFTS_PATH),
        "log_messages": _count_file_lines(LOG_MESSAGES),
    }


def factory_preview() -> dict:
    """只读统计 + 将删除清单 + 备份路径预演；不创建/删除任何文件。"""
    dirs, files = reset_targets()
    stats = _factory_stats()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    delete_dirs = [{"path": str(path), "exists": Path(path).exists()} for path in dirs]
    delete_files = [{"path": str(path), "exists": Path(path).exists()} for path in files]
    return {
        "ok": True,
        "stats": stats,
        "delete_dirs": delete_dirs,
        "delete_files": delete_files,
        "keep": FACTORY_KEEP,
        "backup_preview": str(Path(BACKUP_DIR) / f"factory_reset_{stamp}.zip"),
        "phrase_hint": FACTORY_PHRASE,
        "note": "预览不会删除任何东西；真正执行前会先做 zip 备份。",
    }


def create_backup(now: float | None = None, *, prefix: str = "factory_reset") -> dict:
    """把重置范围内所有文件打包成 zip；失败抛异常（调用方必须中止）。"""
    now = time.time() if now is None else float(now)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
    target_dir = Path(BACKUP_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_path = target_dir / f"{prefix}_{stamp}.zip"
    dirs, files, _roots = _safe_targets()
    written = 0
    try:
        with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for directory in dirs:
                if not directory.exists():
                    continue
                for item in sorted(directory.rglob("*")):
                    if item.is_file():
                        archive.write(item, _arcname(item))
                        written += 1
            for item in files:
                if item.is_file():
                    archive.write(item, _arcname(item))
                    written += 1
        with zipfile.ZipFile(backup_path, "r") as archive:
            bad = archive.testzip()
            if bad is not None:
                raise OSError(f"backup_corrupt:{bad}")
    except Exception:
        try:
            backup_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return {"ok": True, "path": str(backup_path), "files": written,
            "bytes": backup_path.stat().st_size}


def factory_reset(confirm: str = "", *, backup: bool = True,
                  client: str = "") -> dict:
    """真正执行：校验短语/备份/白名单后清空；任何一步失败都中止且不删数据。"""
    started = time.time()
    if str(confirm or "").strip() != FACTORY_PHRASE:
        audit("factory_reset", {"confirm": str(confirm or "")[:40]}, ok=False,
              note="confirm_phrase_mismatch", client=client)
        return {"ok": False, "error": "confirm_phrase_mismatch",
                "hint": f"必须原样输入：{FACTORY_PHRASE}"}
    if not backup:
        audit("factory_reset", {}, ok=False, note="backup_required", client=client)
        return {"ok": False, "error": "backup_required",
                "hint": "必须先备份再清空（backup=true）"}
    try:
        dirs, files, roots = _safe_targets()
    except ValueError as exc:
        audit("factory_reset", {}, ok=False, note=str(exc), client=client)
        return {"ok": False, "error": str(exc), "aborted": True}
    stats = _factory_stats()
    try:
        backup_info = create_backup(started)
    except Exception as exc:  # noqa: BLE001 - 备份失败必须中止
        audit("factory_reset", {"stats": stats}, ok=False,
              note=f"backup_failed:{exc}", client=client)
        return {"ok": False, "error": f"backup_failed:{type(exc).__name__}: {exc}",
                "aborted": True, "stats": stats}

    deleted = {"contacts": 0, "messages": stats["messages"],
               "memories": stats["memories"], "claims": stats["claims"],
               "inner_states": stats["inner_states"], "files": 0, "dirs": 0}
    errors: list[str] = []
    for directory in dirs:
        if not directory.exists():
            continue
        deleted["dirs"] += 1
        try:
            children = list(directory.iterdir())
        except OSError as exc:
            errors.append(f"{directory}: {exc}")
            continue
        for child in children:
            try:
                _resolved_within(child, roots)    # 再次校验，绝不动白名单外
            except ValueError as exc:
                errors.append(str(exc))
                continue
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
                deleted["files"] += 1
            except OSError as exc:
                errors.append(f"{child}: {exc}")
    for item in files:
        if not item.is_file():
            continue
        try:
            _resolved_within(item, roots)
            item.unlink(missing_ok=True)
            deleted["files"] += 1
        except (ValueError, OSError) as exc:
            errors.append(f"{item}: {exc}")

    deleted["contacts"] = stats["contacts"]
    took_s = round(time.time() - started, 2)
    audit("factory_reset", {"backup_path": backup_info["path"], "stats": stats,
                            "deleted": deleted, "errors": errors[:5],
                            "took_s": took_s}, ok=not errors, client=client)
    result = {"ok": True, "backup_path": backup_info["path"],
              "files_backed": backup_info["files"], "deleted": deleted,
              "took_s": took_s,
              "restart_hint": "已清空。建议重启网关，让内存里的状态一起归零；"
                              "备份 zip 在 backup_path，需要时可手工恢复。"}
    if errors:
        result["ok"] = False
        result["errors"] = errors[:10]
    return result


def start_model(name: str, *, client: str = "") -> dict:
    name = str(name or "").strip()
    if name not in MODEL_ALLOWLIST:
        audit("model_start", {"name": name}, ok=False, note="not_allowlisted",
              client=client)
        return {"ok": False, "error": "model_not_editable",
                "hint": f"只允许启停 {sorted(MODEL_ALLOWLIST)}；reserved/研究模型不可动"}
    registry = _model_registry()
    if registry is None:
        result = {"ok": False, "error": "model_registry unavailable"}
    else:
        try:
            result = registry.start(name)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result = _with_model_hint(result)
    audit("model_start", {"name": name, "result": result}, ok=bool(result.get("ok")),
          client=client)
    return result


def stop_model(name: str, *, client: str = "", force_external: bool = False) -> dict:
    name = str(name or "").strip()
    if name not in MODEL_ALLOWLIST:
        audit("model_stop", {"name": name}, ok=False, note="not_allowlisted",
              client=client)
        return {"ok": False, "error": "model_not_editable",
                "hint": f"只允许启停 {sorted(MODEL_ALLOWLIST)}"}
    registry = _model_registry()
    if registry is None:
        result = {"ok": False, "error": "model_registry unavailable"}
    else:
        try:
            # 兼容旧假 registry（不接受 force_external）：仅二次确认时才传
            if force_external:
                result = registry.stop(name, force_external=True)
            else:
                result = registry.stop(name)
        except TypeError:
            # 测试/旧实现不支持 force_external：退回普通 stop
            try:
                result = registry.stop(name)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result = _with_model_hint(result)
    audit("model_stop", {"name": name, "force_external": bool(force_external),
                         "result": result}, ok=bool(result.get("ok")),
          client=client)
    return result



# --------------------------------------------------------------------------- #
# P5-d：媒体页（能力矩阵 / 素材库 / 愿望清单 / 测试发送）
# --------------------------------------------------------------------------- #
MEDIA_WISHES_PATH = DATA_DIR / "media_wishes.jsonl"
MEDIA_ASSET_DIRS: tuple[Path, ...] = (
    ROOT / "wx_core" / "emoji_assets",
    ROOT / "media" / "stickers",
    SELFLEARN_DIR / "media",
    SELFLEARN_DIR / "media_chat",  # 聊天里收到的图片（attachments.path 指向这里）
)
MEDIA_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
MEDIA_TEST_CONTACT = "文件传输助手"
#: 人工确认后的素材入库目录（agi/media_acquisition.add_to_library 的目标）
MEDIA_STICKERS_DIR = ROOT / "media" / "stickers"


def _gateway_media():
    """P5-a 的媒体能力模块；还没落地时返回 None（降级静态矩阵）。"""
    try:
        from agi.wechat_gateway import media
        return media
    except Exception:  # noqa: BLE001
        return None


def _sticker_native():
    try:
        from agi.wechat_gateway import sticker_native
        return sticker_native
    except Exception:  # noqa: BLE001
        return None


def _media_kind(path: str) -> str:
    suffix = Path(str(path or "")).suffix.lower()
    if suffix == ".gif":
        return "gif"
    return "image"


def _media_roots() -> list[Path]:
    return [Path(item).resolve() for item in MEDIA_ASSET_DIRS]


def _media_file_ok(raw: str) -> Path | None:
    """只允许读白名单素材目录里的图片（缩略图用）。"""
    target = Path(str(raw or "")).resolve()
    for root in _media_roots():
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue
    return None


def _media_thumb(path: str) -> str:
    try:
        if Path(str(path or "")).is_file():
            return "/api/media/file?path=" + quote(str(path))
    except OSError:
        pass
    return ""


def media_capabilities() -> dict:
    media_mod = _gateway_media()
    source = "static"
    caps: dict = {}
    enabled = False
    if media_mod is not None:
        source = "agi.wechat_gateway.media"
        try:
            # 传真实配置，否则 capabilities() 会按默认值把 media_enabled 报成 false
            try:
                from agi.wechat_gateway import config as gw_config
                _cfg = gw_config.load()
            except Exception:  # noqa: BLE001
                _cfg = {}
            caps = media_mod.capabilities(_cfg) or {}
            enabled = bool(caps.get("media_enabled", _cfg.get("media_enabled")))
        except Exception:  # noqa: BLE001
            caps = {}
    sticker = _sticker_native()
    native_enabled = False
    if sticker is not None:
        try:
            native_enabled = bool(sticker.enabled())
        except Exception:  # noqa: BLE001
            native_enabled = False
    ok = "ok"
    matrix = [
        {"key": "text", "label": "文本", "status": ok if caps.get("text", True) else "no",
         "note": "SendMsg 文本；单条 ≤500 字"},
        {"key": "unicode_emoji", "label": "Unicode emoji",
         "status": ok if caps.get("unicode_emoji", True) else "no",
         "note": "emoji 作为普通文本字符发送，不需要额外接口"},
        {"key": "image", "label": "图片", "status": ok if caps.get("image", True) else "no",
         "note": "SendFiles(png/jpg/webp)，单文件 ≤10MB"},
        {"key": "gif", "label": "GIF 动图", "status": ok if caps.get("gif", True) else "no",
         "note": "SendFiles(.gif)，微信客户端渲染成动图"},
        {"key": "sticker_as_image", "label": "贴纸（降级为图片）",
         "status": "degraded" if caps.get("sticker_as_image", True) else "no",
         "note": "wxauto4 无贴纸 API，贴纸图当图片发"},
        {"key": "sticker_native", "label": "原生系统表情",
         "status": "ok" if native_enabled else "reserved",
         "note": f"实验性（sticker_native enabled={native_enabled}）；需 wxauto4 backend，默认关闭；收藏贴纸不支持"},
        {"key": "sticker_collect", "label": "收藏贴纸", "status": "no",
         "note": "❌ 自定义表情 tab 不暴露 Name/索引，无法枚举"},
        {"key": "mixed", "label": "文字+表情混排",
         "status": ok if caps.get("mixed", True) else "no",
         "note": "多气泡按顺序逐条发送，间隔 80–150ms"},
    ]
    notes = {row["key"]: row["note"] for row in matrix}
    supported = {row["key"]: row["status"] in ("ok", "degraded") for row in matrix}
    capabilities = {
        "text": {"supported": bool(caps.get("text", True)), "note": notes["text"]},
        "unicode_emoji": {"supported": bool(caps.get("unicode_emoji", True)),
                          "note": notes["unicode_emoji"]},
        "image": {"supported": bool(caps.get("image", True)), "note": notes["image"]},
        "gif": {"supported": bool(caps.get("gif", True)), "note": notes["gif"]},
        "sticker_as_image": {"supported": bool(caps.get("sticker_as_image", True)),
                             "note": notes["sticker_as_image"]},
        "sticker_native": {"supported": native_enabled, "note": notes["sticker_native"]},
        "sticker_collect": {"supported": False, "note": notes["sticker_collect"]},
        "mixed": {"supported": bool(caps.get("mixed", True)), "note": notes["mixed"]},
        "media_enabled": {"supported": enabled, "note": "P5-a 发送层媒体总开关"},
    }
    return {"ok": True, "source": source, "media_enabled": enabled,
            "sticker_native_enabled": native_enabled,
            "test_contact": MEDIA_TEST_CONTACT, "capabilities": capabilities,
            "matrix": matrix,
            "note": "" if enabled else "发送层 media_enabled=false（P5-a 媒体总开关默认关闭）"}


def _scan_media_files(limit: int) -> list[dict]:
    rows: list[tuple[float, Path]] = []
    for directory in MEDIA_ASSET_DIRS:
        try:
            if not Path(directory).is_dir():
                continue
            for item in Path(directory).rglob("*"):
                if not item.is_file() or item.suffix.lower() not in MEDIA_IMAGE_SUFFIXES:
                    continue
                try:
                    rows.append((item.stat().st_mtime, item))
                except OSError:
                    continue
        except OSError:
            continue
    rows.sort(key=lambda pair: pair[0], reverse=True)
    items: list[dict] = []
    for _mtime, item in rows[:max(1, limit)]:
        digest = hashlib.sha1(str(item.resolve()).encode("utf-8", "surrogatepass")).hexdigest()
        items.append({
            "id": "SCAN-" + digest[:14], "name": item.stem, "filename": item.name,
            "path": str(item.resolve()), "file_exists": True,
            "kind": _media_kind(str(item)), "tags": [],
            "usage_count": 0, "source": "scan", "thumb": _media_thumb(str(item)),
        })
    return items


def media_assets(limit: int = 200, kind: str = "") -> dict:
    try:
        limit = max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = 200
    kind = str(kind or "").strip().lower()
    items: list[dict] = []
    source = "scan"
    db = contact_db_path()
    if db is not None:
        try:
            from agi.selflearn import emoji as emoji_archive
            rows = emoji_archive.list_emojis(db, limit=limit)
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            path = str(row.get("path") or "")
            items.append({
                "id": row.get("id"), "name": row.get("name"),
                "filename": row.get("filename"), "path": path,
                "file_exists": Path(path).is_file() if path else False,
                "kind": _media_kind(path), "tags": row.get("tags") or [],
                "usage_count": int(row.get("usage_count") or 0),
                "source": "emoji_archive", "thumb": _media_thumb(path),
            })
        if items:
            source = "emoji_archive"
    scanned = _scan_media_files(limit)
    acquisition = _media_acquisition()
    sources_file = DATA_DIR / "media_sources.json"
    if acquisition is not None and hasattr(acquisition, "sources_path"):
        try:
            sources_file = Path(acquisition.sources_path())
        except Exception:  # noqa: BLE001
            pass
    import_sources = read_json(sources_file, {})
    if not isinstance(import_sources, dict):
        import_sources = {}
    if items:
        known = {str(Path(item.get("path") or "").resolve()).casefold()
                 for item in items if item.get("path")}
        for item in scanned:
            path_key = str(Path(item.get("path") or "").resolve()).casefold()
            if path_key not in known:
                items.append(item)          # 本地导入的新文件也可在素材库看到
                known.add(path_key)
    else:
        items = scanned
    for item in items:
        record = import_sources.get(Path(str(item.get("path") or "")).stem) or {}
        if record:
            item["name"] = record.get("name") or item.get("name")
            item["tags"] = record.get("tags") or []
            item["source"] = "local_import"
            item["indexed"] = bool(record.get("indexed"))
    if kind:
        items = [item for item in items if item.get("kind") == kind]
    return {"ok": True, "source": source, "items": items, "total": len(items),
            "dirs": [str(item) for item in MEDIA_ASSET_DIRS],
            "note": "" if items else "素材库为空：等 P5-b/P5-e 索引或往 wx_core/emoji_assets、media/stickers 放图"}


def media_library(limit: int = 100) -> dict:
    """素材库清单（P5-d 契约字段：name/path/ext/usage_count/tags/mtime/indexed）。"""
    data = media_assets(limit=limit)
    items = []
    for item in data.get("items") or []:
        path = str(item.get("path") or "")
        try:
            mtime = Path(path).stat().st_mtime if path else None
        except OSError:
            mtime = None
        source_name = item.get("source") or (
            "emoji_archive" if item.get("indexed") else "scan")
        items.append({
            "name": item.get("name"), "path": path,
            "ext": (Path(path).suffix.lower().lstrip(".") if path else ""),
            "usage_count": int(item.get("usage_count") or 0),
            "tags": item.get("tags") or [], "mtime": mtime,
            "indexed": bool(item.get("indexed")) or source_name == "emoji_archive",
            "kind": item.get("kind"), "thumb": item.get("thumb"),
            "file_exists": bool(item.get("file_exists")), "source": source_name,
        })
    return {"ok": True, "items": items, "total": len(items),
            "source": data.get("source"), "note": data.get("note") or ""}


def media_stats() -> dict:
    library = media_library(limit=MAX_LIMIT)
    items = library.get("items") or []
    total = len(items)
    by_ext: dict[str, int] = {}
    for item in items:
        ext = str(item.get("ext") or "unknown")
        by_ext[ext] = by_ext.get(ext, 0) + 1
    used = sum(1 for item in items if int(item.get("usage_count") or 0) > 0)
    top = sorted([item for item in items if int(item.get("usage_count") or 0) > 0],
                 key=lambda item: (-int(item.get("usage_count") or 0),
                                   str(item.get("name") or "")))[:10]
    return {"ok": True, "source": library.get("source"),
            "total": total, "used": used, "unused": max(0, total - used),
            "by_ext": by_ext, "top": top,
            "note": "" if library.get("source") == "emoji_archive"
                    else "emoji_archive 为空，统计来自目录扫描（无使用次数）"}


def _media_acquisition_module():
    """加载 agi.media_acquisition；不可用时返回 None（页面降级为只读文件）。"""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from agi import media_acquisition as module
        return module
    except Exception:  # noqa: BLE001 - 只读观测页，失败降级
        return None


def media_acquisition() -> dict:
    """P5-e：素材获取现状（只读；没有搜索/下载管线就如实说没有）。

    真实能力边界（agi/media_acquisition.py）：
    - ``search_wishes`` / ``list_wishes``：只读愿望清单，可用；
    - ``try_acquire``：永远返回 reserved，**不发网络请求/不下载**；
    - ``add_to_library`` / ``import_local*``：人工导入，走素材页已有按钮。
    """
    module = _media_acquisition_module()
    wishes = media_wishes()
    rows = wishes.get("items") or []
    items: list[dict] = []
    for row in rows[:50]:
        if not isinstance(row, dict):
            continue
        items.append({
            "wish_id": str(row.get("wish_id") or ""),
            "wish": str(row.get("wish") or "")[:200],
            "status": str(row.get("status") or "pending"),
            "ts": row.get("ts") or row.get("created_at"),
            "source": str(row.get("source") or ""),
        })
    acquire_probe: dict | None = None
    if module is not None and hasattr(module, "try_acquire"):
        try:
            acquire_probe = module.try_acquire("__probe__")
        except Exception as exc:  # noqa: BLE001 - 探测失败不影响页面
            acquire_probe = {"ok": False, "status": "error",
                             "reason": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "enabled": False, "mode": "read_only",
            "wishes": int(wishes.get("count") or len(items)),
            "items": items,
            "capabilities": {
                "list_wishes": module is not None and hasattr(module, "list_wishes"),
                "search_online": False,
                "download": False,
                "manual_import": True,
                "add_to_library": module is not None and hasattr(module, "add_to_library"),
            },
            "acquire_probe": acquire_probe,
            "library": {"path": str(MEDIA_STICKERS_DIR),
                        "exists": MEDIA_STICKERS_DIR.is_dir()},
            "note": ("预留接口：只读列出愿望清单；没有搜索/下载管线（P5-e task-63 未实现），"
                     "素材入库请用本页导入/愿望满足")}


def media_wishes() -> dict:
    items: list[dict] = []
    try:
        from agi import media_acquisition
        if hasattr(media_acquisition, "list_wishes"):
            items = media_acquisition.list_wishes(MEDIA_WISHES_PATH)
        else:
            items = media_acquisition.search_wishes(MEDIA_WISHES_PATH)
    except Exception:  # noqa: BLE001 - 降级直接读 JSONL
        try:
            if MEDIA_WISHES_PATH.is_file():
                for line in MEDIA_WISHES_PATH.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict) and entry.get("wish"):
                        items.append(entry)
        except OSError:
            items = []
    return {"ok": True, "items": items, "count": len(items),
            "path": str(MEDIA_WISHES_PATH),
            "note": "获取/下载管线预留（P5-e task-63）：当前不联网、不下载"}


def _post_gateway_media(payload: dict) -> dict:
    """把测试发送请求转发给网关（P5-a 的 HTTP 接口）；测试可 monkeypatch 本函数。"""
    base = GATEWAY_STATUS_URL.rsplit("/status", 1)[0]
    request = urllib.request.Request(base + "/api/media/send",
                                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     method="POST")
    request.add_header("Content-Type", "application/json")
    token = admin_token()
    if token:
        request.add_header("X-Admin-Token", token)
    # 图片/表情包发送可能走 UIA 窗口兜底，实测要 30–60s；5s 会误报 502
    with urllib.request.urlopen(request, timeout=90.0) as response:
        raw = response.read().decode("utf-8", "replace") or "{}"
    return json.loads(raw)


def manual_send(contact: str, text: str, *, client: str = "") -> dict:
    """控制台「给某人发消息」：代理 801 的 /api/approve（manual+confirm）。

    安全：必须白名单联系人（801 兜底）；控制台只做转发，不绕过发送闸门。
    """
    name = str(contact or "").strip()
    message = str(text or "").strip()
    if not name or not message:
        return {"ok": False, "error": "contact/text 不能为空"}
    if len(message) > 2000:
        return {"ok": False, "error": "消息过长（>2000 字）"}
    base = GATEWAY_STATUS_URL.rsplit("/status", 1)[0]
    payload = json.dumps({"contact": name, "text": message, "confirm": True},
                         ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = admin_token()
    if token:
        headers["X-Admin-Token"] = token
    request = urllib.request.Request(base + "/api/approve", data=payload,
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace") or "{}")
        except Exception:  # noqa: BLE001
            detail = {}
        return {"ok": False, "error": detail.get("error") or f"gateway http {exc.code}",
                "detail": detail}
    except urllib.error.URLError as exc:
        return {"ok": False,
                "error": "801 网关未就绪（连接被拒绝）：请确认网关已启动后重试",
                "detail": str(getattr(exc, "reason", exc))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result = result if isinstance(result, dict) else {}
    audit("manual_send", {"contact": name, "chars": len(message),
                          "status": result.get("status")}, ok=bool(result.get("ok")),
          client=client)
    # 把 801 的发送语义原样带出来（sender_unavailable / unverified + possible_sent），
    # 前端才能显示"发送器不可用"和"可能已发出，不会自动重发"，而不是笼统的失败。
    status = str(result.get("status") or "")
    return {"ok": bool(result.get("ok")), "status": status,
            "error": str(result.get("error") or ""),
            "error_kind": str(result.get("error_kind") or ""),
            "draft_id": result.get("draft_id"),
            "possible_sent": bool(result.get("possible_sent")),
            "gateway": result}


# --------------------------------------------------------------------------- #
# P40：待审草稿（802 → 801 /api/drafts、/api/draft/approve、/api/draft/reject）
# --------------------------------------------------------------------------- #
DRAFT_APPROVE_TIMEOUT_S = 120.0


def _gateway_proxy(path: str, payload: dict | None = None, *, method: str = "GET",
                   timeout: float = 30.0) -> dict:
    """通用 801 代理：透传 X-Admin-Token，失败返回结构化 error（不抛异常）。

    与 ``manual_send`` 的差别：不拼业务参数、不改写结果，返回网关原始 JSON；
    失败时至少带 ``error``（+ ``hint``）给前端展示，避免「点了没反应」。
    """
    base = GATEWAY_STATUS_URL.rsplit("/status", 1)[0]
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    token = admin_token()
    if token:
        headers["X-Admin-Token"] = token
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace") or "{}"
    except urllib.error.HTTPError as exc:
        detail: dict = {}
        try:
            parsed = json.loads(exc.read().decode("utf-8", "replace") or "{}")
            detail = parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            detail = {}
        return {"ok": False, "http": exc.code,
                "error": str(detail.get("error") or f"gateway_http_{exc.code}"),
                "hint": str(detail.get("detail") or detail.get("hint") or ""),
                "detail": detail}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": "gateway_unreachable",
                "hint": "801 网关未就绪：请确认 python -m agi.wechat_gateway.server 正在运行",
                "detail": str(getattr(exc, "reason", exc))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        result = json.loads(raw)
    except ValueError:
        return {"ok": False, "error": "gateway_bad_json", "hint": raw[:200]}
    return result if isinstance(result, dict) else {"ok": False, "error": "gateway_bad_payload"}


def drafts_pending() -> dict:
    """待审草稿列表：来源 801 ``/api/drafts``（802 不再自己读一份 drafts.jsonl）。"""
    result = _gateway_proxy("/api/drafts")
    if not result.get("ok"):
        result.setdefault("items", [])
        return result
    drafts = result.get("drafts")
    return {"ok": True, "items": drafts if isinstance(drafts, list) else [],
            "source": "gateway:801"}


def refresh_contacts(*, client: str = "") -> dict:
    """V1：转发 801 的联系人刷新（只读，落 contacts.sqlite3）。"""
    result = _gateway_proxy("/api/wechat/refresh_contacts", {}, method="POST",
                            timeout=60.0)
    audit("contacts_refresh", {"ok": bool(result.get("ok")),
                               "count": result.get("count")},
          ok=bool(result.get("ok")), client=client)
    return result


def draft_decide(draft_id: str, approve: bool, *, client: str = "") -> dict:
    """批准（真实发送）或驳回一条草稿；失败必须带具体 error 给前端显示。"""
    target = str(draft_id or "").strip()
    if not target:
        return {"ok": False, "error": "id 不能为空"}
    path = "/api/draft/approve" if approve else "/api/draft/reject"
    payload = {"id": target, "confirm": True} if approve else {"id": target}
    result = _gateway_proxy(path, payload, method="POST",
                            timeout=DRAFT_APPROVE_TIMEOUT_S if approve else 30.0)
    audit("draft_approve" if approve else "draft_reject",
          {"id": target, "ok": bool(result.get("ok")),
           "status": result.get("status"),
           "error": result.get("error") or ""},
          ok=bool(result.get("ok")), client=client)
    return result


def sender_status(force: bool = False) -> dict:
    """802 顶部「发送器」指示：代理 801 ``/api/sender/status``（force=1 现场重探）。

    网关不可达时也回一个 ``sender`` 结构，前端统一按 ``available=false`` 显示，
    不会因为 801 没起就整块状态区消失。
    """
    result = _gateway_proxy("/api/sender/status" + ("?force=1" if force else ""),
                            timeout=20.0)
    if not result.get("ok"):
        result.setdefault("sender", {
            "available": False,
            "module": "",
            "python": "",
            "error": result.get("hint") or result.get("error") or "gateway_unreachable",
            "tried": [],
            "checked_at": now_ts(),
        })
    sender = result.get("sender") if isinstance(result.get("sender"), dict) else None
    if sender is not None:
        result["available"] = bool(sender.get("available"))
    return result


def initiative_status(contact: str = "") -> dict:
    """代理 801 ``/api/initiative/status``（主动度/内驱力只读，需 token）。

    801 缺该路由/未就绪时返回 ok:false + hint，前端显示空态而不是整页报错。
    """
    query = "?contact=" + quote(str(contact or ""))
    result = _gateway_proxy("/api/initiative/status" + query, timeout=15.0)
    if not result.get("ok"):
        result.setdefault("hint", "801 未就绪或该联系人没有内驱力数据")
        result.setdefault("contact", contact)
    return result


def _current_db_path(contact: str = "") -> Path | None:
    """当前联系人库：显式 contact 优先，否则默认当前库。"""
    raw = str(contact or "").strip()
    return _contact_db_by_alias(raw) if raw else contact_db_path()


def _weak_validation_module():
    """加载 agi.selflearn.weak_validation；不可用返回 None（端点降级为 ok:false）。"""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from agi.selflearn import weak_validation
        return weak_validation
    except Exception:  # noqa: BLE001 - 只读观测页，失败降级
        return None


def _normalize_weak_due_row(row: dict) -> dict:
    """把 core-brain 的 due 项归一成前端契约字段（缺字段给 None/空串）。"""
    return {
        "skill_id": str(row.get("skill_id") or ""),
        "next_at": row.get("next_at") if row.get("next_at") is not None
                   else row.get("due_at"),
        "reason": str(row.get("reason") or ""),
        "variant": str(row.get("variant") or ""),
        "level": row.get("level"),
        "overdue_s": row.get("overdue_s") if row.get("overdue_s") is not None
                     else row.get("overdue_seconds"),
    }


def weak_due(contact: str = "") -> dict:
    """GET /api/growth/weak-due：到期复测提醒（只读，不会自动判定/降级）。

    依赖 ``agi.selflearn.weak_validation.due_rechecks(path, now=None)``（core-brain 落地）；
    模块/库/函数任一不可用都返回 ``ok:false + error:"weak_due_unavailable"``（**不返回 501**）。
    """
    module = _weak_validation_module()
    if module is None:
        return {"ok": False, "error": "weak_due_unavailable",
                "hint": "agi.selflearn.weak_validation 不可用（模块未落地/导入失败）",
                "count": 0, "items": []}
    func = getattr(module, "due_rechecks", None)
    if not callable(func):
        return {"ok": False, "error": "weak_due_unavailable",
                "hint": "weak_validation.due_rechecks 尚未落地（等 core-brain 后端 API）",
                "count": 0, "items": []}
    db_path = _current_db_path(contact)
    if db_path is None or not Path(db_path).is_file():
        return {"ok": False, "error": "weak_due_unavailable",
                "hint": f"找不到联系人库: {contact or 'default'}", "count": 0, "items": []}
    try:
        raw = func(db_path)
    except Exception as exc:  # noqa: BLE001 - 只读观测页，失败降级
        return {"ok": False, "error": "weak_due_unavailable",
                "hint": f"{type(exc).__name__}: {exc}", "count": 0, "items": []}
    if isinstance(raw, dict):
        rows = raw.get("items")
        if rows is None:
            rows = raw.get("due") or raw.get("rechecks") or []
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []
    items = [_normalize_weak_due_row(row) for row in rows
             if isinstance(row, dict)][:200]
    return {"ok": True, "count": len(items), "items": items,
            "note": "只提醒，不会自动判定/降级"}


def _share_store_module():
    """加载 agi.selflearn.share_store；不可用返回 None。"""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from agi.selflearn import share_store
        return share_store
    except Exception:  # noqa: BLE001 - 只读观测页，失败降级
        return None


def wake_pending(limit: int = 10, contact: str = "") -> dict:
    """GET /api/wake/pending：``share_candidates`` 里 kind=="wake" 的到期候选。

    只读：候选只是"已排队"，不会自动发送；空列表也返回 ok:true,count:0。
    """
    module = _share_store_module()
    if module is None:
        return {"ok": False, "error": "wake_pending_unavailable",
                "hint": "agi.selflearn.share_store 不可用（模块未落地/导入失败）",
                "count": 0, "items": []}
    try:
        limit = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        limit = 10
    db_path = _current_db_path(contact)
    if db_path is None or not Path(db_path).is_file():
        return {"ok": True, "count": 0, "items": [],
                "note": f"联系人库不存在（{contact or 'default'}）：没有 wake 候选"}
    try:
        rows = module.list_candidates(db_path, limit=limit * 4)
    except Exception as exc:  # noqa: BLE001 - 只读观测页，失败降级
        return {"ok": False, "error": "wake_pending_unavailable",
                "hint": f"{type(exc).__name__}: {exc}", "count": 0, "items": []}
    if not isinstance(rows, list):
        rows = []
    items: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("kind") or "") != "wake":
            continue
        items.append({
            "id": str(row.get("id") or ""),
            "kind": "wake",
            "text": str(row.get("text") or "")[:300],
            "reason": str(row.get("reason") or "")[:200],
            "level": str(row.get("level") or ""),
            "score": row.get("score"),
            "created_at": row.get("created_at"),
        })
        if len(items) >= limit:
            break
    return {"ok": True, "count": len(items), "items": items,
            "note": "已排队为候选，不会自动发送"}


#: 「自动缓存聊天视频」目前只有解析能力（webui/videos.py），没有抓取/落盘管线
VIDEO_CACHE_DOC = "docs/plans/31-媒体能力-实现记录.md"


def video_cache_status() -> dict:
    """对话流「实验性：自动缓存聊天视频」的配置入口（如实返回未启用）。"""
    return {
        "ok": True,
        "enabled": False,
        "docs": VIDEO_CACHE_DOC,
        "hint": ("自动缓存聊天视频尚未实现：webui/videos.py 只负责把已存在的本机缓存"
                 "解析成可播放地址，没有抓取/落盘管线。开关保持关闭，需要时先按文档补后端。"),
    }


def media_send(body: dict, *, client: str = "") -> dict:
    body = body if isinstance(body, dict) else {}
    kind = str(body.get("type") or body.get("kind") or "text").strip().lower()
    allowed = ("text", "emoji_text", "image", "gif", "sticker", "native_emoji")
    if kind not in allowed:
        return {"ok": False, "status": "invalid", "error": f"不支持的媒体类型: {kind}"}
    item: dict = {"type": kind}
    if kind in ("text", "emoji_text"):
        item["text"] = str(body.get("text") or "")
        if not item["text"].strip():
            return {"ok": False, "status": "invalid", "error": "text 不能为空"}
    elif kind == "native_emoji":
        name = str(body.get("name") or body.get("text") or body.get("path") or "")
        if not name:
            return {"ok": False, "status": "invalid", "error": "需要系统表情 name"}
        item["name"] = name
    else:
        path = str(body.get("path") or body.get("name") or "")
        if not path:
            return {"ok": False, "status": "invalid", "error": "需要 path/name"}
        item["path"] = path
    media_mod = _gateway_media()
    if media_mod is not None and kind != "native_emoji":
        try:
            ok_item, error_code = media_mod.validate_item(item)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "invalid", "error": f"{type(exc).__name__}: {exc}"}
        if not ok_item:
            return {"ok": False, "status": "invalid", "error": error_code}
    # 强制覆盖联系人：测试发送只允许文件传输助手
    # force=True：管理员手动触发的调试发送，绕过夜间静默（P0-25：force 放行）
    payload = {"contact": MEDIA_TEST_CONTACT, "items": [item], "force": True}
    try:
        result = _post_gateway_media(payload)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 405, 501):
            return {"ok": False, "status": "unsupported", "http": exc.code,
                    "error": "media_api_not_landed",
                    "contact": MEDIA_TEST_CONTACT,
                    "hint": "网关暂无 /api/media/send，等 P5-a 落地后再试"}
        return {"ok": False, "status": "gateway_error", "http": exc.code,
                "error": f"gateway http {exc.code}", "contact": MEDIA_TEST_CONTACT}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "gateway_unavailable",
                "error": f"{type(exc).__name__}: {exc}", "contact": MEDIA_TEST_CONTACT}
    ok = bool(result.get("ok"))
    audit("media_send", {"type": kind, "contact": MEDIA_TEST_CONTACT,
                         "ok": ok, "gateway_status": result.get("status")},
          ok=ok, client=client)
    return {"ok": ok, "status": result.get("status") or ("sent" if ok else "blocked"),
            "contact": MEDIA_TEST_CONTACT, "test": True, "gateway": result,
            "note": "测试发送只允许文件传输助手；不会发给真实好友"}


# --------------------------------------------------------------------------- #
# P7-b：对话测试（ChatUI）/ 模型配置（Providers）/ 日志（Trace）
# --------------------------------------------------------------------------- #
WEB_CONFIG_PATH = ROOT / "web_config.json"
CHAT_TEST_DB = SELFLEARN_DIR / "console_test.sqlite3"
CHAT_TEST_TIMEOUT_S = 60.0

MODEL_CONFIG_DEFAULTS = {
    "llm_model": "deepseek-flash",
    "llm_temperature": 0.7,
    "llm_max_tokens": 2048,
    "soul_server_url": "http://127.0.0.1:8123",
    "soul_timeout_s": 2.0,
    "inner_schema": "slim",
    # 2026-10-29：聊天图片识图路由（本地 VL 描述 / 云端视觉）
    "chat_vision_mode": "local_caption",
}

LOG_SOURCES: dict[str, list[Path]] = {
    "gateway": [Path(GATEWAY_LOG)],
    "inner": [SELFLEARN_DIR / "inner.log"],
    "prompt": [SELFLEARN_DIR / "prompts.log", SELFLEARN_DIR / "prompt_override.log"],
    "audit": [Path(AUDIT_PATH)],
}


def _deepseek_status() -> dict:
    """DeepSeek key 只暴露"已配置/未配置"和来源，绝不回传明文。

    取值优先级与 ``agi.selflearn.llm.load_config`` 一致：env → secrets.json → web_config.json。
    """
    payload = read_json(WEB_CONFIG_PATH, {})
    llm = payload.get("llm") if isinstance(payload, dict) else {}
    llm = llm if isinstance(llm, dict) else {}
    env_key = os.environ.get("DEEPSEEK_API_KEY") or ""
    file_key = str(llm.get("api_key") or "")
    secrets_key = ""
    try:
        from agi import secrets_store
        secrets_key = secrets_store.llm_api_key()
    except Exception:  # noqa: BLE001 - 密钥库不可用时退回旧来源
        secrets_key = ""
    if env_key:
        source = "env:DEEPSEEK_API_KEY"
    elif secrets_key:
        source = "agi/data/secrets.json:llm.api_key"
    elif file_key:
        source = "web_config.json:llm.api_key"
    else:
        source = ""
    return {"configured": bool(env_key or secrets_key or file_key), "source": source,
            "env_var": "DEEPSEEK_API_KEY",
            "config_file": "agi/data/secrets.json (llm.api_key，本机密钥库；兼容 web_config.json)",
            "editable": True}


def save_deepseek_key(api_key: str, *, clear: bool = False, client: str = "") -> dict:
    """802 模型配置页保存 DeepSeek key：只写本机 ``agi/data/secrets.json``。"""
    text = "" if clear else str(api_key or "").strip()
    if text and (len(text) < 8 or len(text) > 200):
        audit("deepseek_key_update", {"configured": False}, ok=False,
              note="key 长度不合法", client=client)
        return {"ok": False, "error": "key 长度不合法（8–200 字符）"}
    if text and any(ch.isspace() or ord(ch) < 32 for ch in text):
        audit("deepseek_key_update", {"configured": False}, ok=False,
              note="key 含空白/控制字符", client=client)
        return {"ok": False, "error": "key 不能包含空白或控制字符"}
    try:
        from agi import secrets_store
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"密钥库不可用：{type(exc).__name__}: {exc}"}
    if not secrets_store.store_llm_api_key(text):
        audit("deepseek_key_update", {"configured": False}, ok=False,
              note="写入 secrets.json 失败", client=client)
        return {"ok": False, "error": "写入本机密钥库失败（检查 agi/data/secrets.json 权限）"}
    # 迁移兼容：清掉 web_config.json 里可能残留的明文 key（避免两份、避免再次告警）
    payload = read_json(WEB_CONFIG_PATH, {})
    if isinstance(payload, dict):
        llm = payload.get("llm")
        if isinstance(llm, dict) and llm.get("api_key"):
            llm = dict(llm)
            llm["api_key"] = ""
            payload = dict(payload)
            payload["llm"] = llm
            try:
                write_json_atomic(WEB_CONFIG_PATH, payload)
            except OSError:
                pass
    audit("deepseek_key_update", {"configured": bool(text)}, ok=True, client=client)
    return {"ok": True, "deepseek": _deepseek_status(),
            "note": "已保存到本机 agi/data/secrets.json（不上传；网关热加载，下一条消息生效）"}


def model_config_state() -> dict:
    raw = gateway_config()
    config = {key: raw.get(key, default) for key, default in MODEL_CONFIG_DEFAULTS.items()}
    llm = read_json(WEB_CONFIG_PATH, {})
    llm = llm.get("llm") if isinstance(llm, dict) else {}
    llm = llm if isinstance(llm, dict) else {}
    return {
        "ok": True, "config": config, "defaults": dict(MODEL_CONFIG_DEFAULTS),
        "deepseek": _deepseek_status(),
        "effective": {
            "llm_model": llm.get("model") or "deepseek-flash",
            "llm_base_url": llm.get("base_url") or "https://api.deepseek.com",
        },
        "path": str(GATEWAY_CONFIG),
        "note": "保存写入网关 config（热加载）；DeepSeek key 只显示配置状态，不回传明文",
    }


def _validate_model_config(updates: dict) -> tuple[dict, list[str]]:
    if not isinstance(updates, dict):
        return {}, ["body 必须是对象"]
    cleaned: dict = {}
    errors: list[str] = []
    for key, value in updates.items():
        if key not in MODEL_CONFIG_DEFAULTS:
            errors.append(f"not_allowed:{key}")
            continue
        try:
            if key == "llm_model":
                text = str(value or "").strip()
                if not text or len(text) > 80:
                    raise ValueError("llm_model 需 1–80 字符")
                cleaned[key] = text
            elif key == "llm_temperature":
                number = float(value)
                if not 0.0 <= number <= 2.0:
                    raise ValueError("llm_temperature 取值 0–2")
                cleaned[key] = round(number, 3)
            elif key == "llm_max_tokens":
                number = int(value)
                if not 1 <= number <= 32000:
                    raise ValueError("llm_max_tokens 取值 1–32000")
                cleaned[key] = number
            elif key == "soul_server_url":
                text = str(value or "").strip()
                if not text.startswith(("http://", "https://")) or len(text) > 200:
                    raise ValueError("soul_server_url 需要 http(s) URL")
                cleaned[key] = text.rstrip("/")
            elif key == "soul_timeout_s":
                number = float(value)
                if not 0.1 <= number <= 60.0:
                    raise ValueError("soul_timeout_s 取值 0.1–60")
                cleaned[key] = round(number, 2)
            elif key == "inner_schema":
                text = str(value or "").strip().lower()
                if text not in ("slim", "full"):
                    raise ValueError("inner_schema 只能是 slim/full")
                cleaned[key] = text
            elif key == "chat_vision_mode":
                text = str(value or "").strip().lower()
                if text not in ("local_caption", "api"):
                    raise ValueError("chat_vision_mode 只能是 local_caption/api")
                cleaned[key] = text
        except (TypeError, ValueError) as exc:
            errors.append(f"{key}: {exc}")
    return cleaned, errors


def apply_model_config(updates: dict, *, client: str = "") -> dict:
    cleaned, errors = _validate_model_config(updates)
    if errors:
        audit("model_config_update", {"keys": sorted(updates or {})}, ok=False,
              note=";".join(errors)[:200], client=client)
        return {"ok": False, "errors": errors, "config": model_config_state()["config"]}
    raw = gateway_config()
    before = {key: raw.get(key) for key in cleaned}
    raw.update(cleaned)
    try:
        write_json_atomic(GATEWAY_CONFIG, raw)
    except OSError as exc:
        audit("model_config_update", {"keys": sorted(cleaned)}, ok=False,
              note=str(exc), client=client)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    audit("model_config_update", {"keys": sorted(cleaned), "before": before,
                                  "after": cleaned}, ok=True, client=client)
    state = model_config_state()
    state["note"] = "已保存；网关热加载，下一条消息 / 下一次 proactive 生效"
    return state


# --------------------------------------------------------------------------- #
# 对话测试（隔离库）
# --------------------------------------------------------------------------- #
def _chat_agent_factory(path: Path):
    """测试可 monkeypatch；生产用真实 SelfLearnAgent（进程内、隔离库）。"""
    from agi.selflearn.chat import SelfLearnAgent
    return SelfLearnAgent(path=Path(path))


def chat_test_db() -> Path:
    return Path(CHAT_TEST_DB)


def _safe_chat_test_db() -> Path:
    target = Path(CHAT_TEST_DB).resolve()
    target.relative_to(Path(SELFLEARN_DIR).resolve())      # 白名单
    if target.name != "console_test.sqlite3":
        raise ValueError("chat-test db must be console_test.sqlite3")
    return target


def chat_test_reset() -> dict:
    try:
        target = _safe_chat_test_db()
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": f"unsafe_chat_test_db: {exc}"}
    removed = []
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(target) + suffix)
        try:
            if path.exists():
                path.unlink()
                removed.append(path.name)
        except OSError as exc:
            return {"ok": False, "error": f"{path.name}: {exc}", "removed": removed}
    return {"ok": True, "reset": True, "removed": removed,
            "db": str(target), "note": "隔离测试库已清空（真实联系人库不受影响）"}


def chat_test(text: str, *, reset: bool = False, client: str = "") -> dict:
    if reset:
        result = chat_test_reset()
        if result.get("ok") and not str(text or "").strip():
            result["skipped"] = True
            return result
        if not result.get("ok"):
            return result
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "status": "invalid", "error": "text 不能为空"}
    try:
        target = _safe_chat_test_db()
    except (ValueError, OSError) as exc:
        return {"ok": False, "status": "invalid", "error": f"unsafe_chat_test_db: {exc}"}
    target.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    executor = None
    try:
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as FutureTimeout
        agent = _chat_agent_factory(target)
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(agent.send, text)
        try:
            result = future.result(timeout=CHAT_TEST_TIMEOUT_S)
        except FutureTimeout:
            future.cancel()
            audit("chat_test", {"chars": len(text), "timeout": True}, ok=False,
                  note="timeout", client=client)
            return {"ok": False, "status": "timeout",
                    "error": f"chat-test 超时（{int(CHAT_TEST_TIMEOUT_S)}s），已放弃本次回复",
                    "db": str(target)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "error", "error": f"{type(exc).__name__}: {exc}",
                "db": str(target)}
    finally:
        if executor is not None:
            executor.shutdown(wait=False)
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    if not isinstance(result, dict):
        return {"ok": False, "status": "error", "error": "agent 返回非 dict",
                "db": str(target)}
    generation = result.get("generation") if isinstance(result.get("generation"), dict) else {}
    inner = generation.get("inner") or result.get("state") or {}
    media = (result.get("media") or result.get("reply_media")
             or generation.get("media") or {})
    bubbles = result.get("bubbles") or []
    reply = result.get("reply") or (bubbles[0] if bubbles else "")
    payload = {
        "ok": True, "reply": str(reply or ""),
        "bubbles": [str(item) if not isinstance(item, dict) else item for item in bubbles],
        "skipped": bool(result.get("skipped")),
        "skip_reason": result.get("skip_reason") or "",
        "crisis": bool(result.get("crisis")),
        "guard": result.get("guard") or {},
        "inner": inner, "media": media, "gen_meta": generation,
        "latency_ms": latency_ms, "turn_id": result.get("turn_id"),
        "db": str(target),
        "isolation": "隔离测试库 console_test.sqlite3（不影响真实联系人记忆）",
    }
    audit("chat_test", {"chars": len(text), "skipped": payload["skipped"],
                        "latency_ms": latency_ms}, ok=True, client=client)
    return payload


def chat_test_history(limit: int = 50) -> dict:
    try:
        target = _safe_chat_test_db()
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": f"unsafe_chat_test_db: {exc}", "items": []}
    if not target.is_file():
        return {"ok": True, "items": [], "db": str(target)}
    try:
        limit = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        limit = 50
    try:
        con = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT id, role, text, ts FROM messages ORDER BY ts DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {"ok": True, "items": [], "db": str(target)}
    return {"ok": True, "db": str(target),
            "items": [dict(row) for row in reversed(rows)]}


# --------------------------------------------------------------------------- #
# 日志（Trace）
# --------------------------------------------------------------------------- #
def _log_is_error(entry: dict) -> bool:
    kind = str(entry.get("kind") or "").lower()
    if any(word in kind for word in ("error", "fail", "blocked", "stale", "crisis")):
        return True
    level = str(entry.get("level") or "").lower()
    return level in ("error", "critical", "fatal")


def _parse_log_line(line: str, source: str, file_path: Path, line_no: int) -> dict | None:
    text = line.rstrip("\n")
    if not text.strip():
        return None
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        return None                      # P7-b：坏行/非 JSON 跳过
    entry = dict(value)
    entry["_source"] = source
    entry["_file"] = Path(file_path).name
    entry["_line"] = line_no
    if entry.get("ts") is None:
        entry["ts"] = None
    entry["error"] = bool(_log_is_error(entry))
    return entry


def _regen_entries() -> list[dict]:
    entries: list[dict] = []
    try:
        from agi.selflearn import media_choice
        snapshot = media_choice.regen_failed_snapshot()
        for key, count in (snapshot or {}).items():
            entries.append({"ts": None, "kind": "regen_failed", "key": key,
                            "count": int(count or 0), "error": True,
                            "_source": "regen", "_file": "media_choice", "_line": 0})
    except Exception:  # noqa: BLE001
        pass
    return entries


def read_logs(source: str = "gateway", limit: int = 200, q: str = "",
              kind: str = "") -> dict:
    source = str(source or "gateway").strip().lower()
    try:
        limit = max(1, min(1000, int(limit)))
    except (TypeError, ValueError):
        limit = 200
    needle = str(q or "").strip().casefold()
    kind_needle = str(kind or "").strip().lower()
    entries: list[dict] = []
    files: list[str] = []
    if source == "regen":
        entries = [entry for entry in _regen_entries()
                   if not kind_needle or kind_needle in str(entry.get("kind") or "")]
        if needle:
            entries = [entry for entry in entries
                       if needle in json.dumps(entry, ensure_ascii=False).casefold()]
        return {"ok": True, "source": source, "items": entries[:limit],
                "total": len(entries), "files": [], "note": "进程内 regen_failed 计数快照"}
    if source not in LOG_SOURCES:
        return {"ok": True, "source": source, "items": [], "total": 0,
                "files": [], "note": f"未知来源: {source}"}
    for file_path in LOG_SOURCES[source]:
        path = Path(file_path)
        if not path.is_file():
            continue
        files.append(str(path))
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, 1):
                    entry = _parse_log_line(line, source, path, line_no)
                    if entry is None:
                        continue
                    if kind_needle and kind_needle not in str(entry.get("kind") or "").lower():
                        continue
                    if needle:
                        blob = json.dumps(entry, ensure_ascii=False).casefold()
                        if needle not in blob:
                            continue
                    entries.append(entry)
        except OSError:
            continue
    entries.sort(key=lambda item: (float(item.get("ts") or 0), int(item.get("_line") or 0)),
                 reverse=True)
    return {"ok": True, "source": source, "items": entries[:limit],
            "total": len(entries), "files": files,
            "note": "" if files else "日志文件不存在或为空"}


# --------------------------------------------------------------------------- #
# P7-c：数据管理（备份/导出/导入）+ 记忆编辑（改/删/标记/合并）
# --------------------------------------------------------------------------- #
IMPORT_CONFIRM = "导入备份"
IMPORT_OVERWRITE_CONFIRM = "覆盖现有库"
BACKUP_DELETE_CONFIRM = "删除备份"
MEMORY_DELETE_CONFIRM = "删除记忆"
MEMORY_STATUSES = ("active", "invalid", "dormant")
MEMORY_MAX_CONTENT = 500


_SPLIT_SUFFIXES = (".memory.sqlite3", ".persona.sqlite3", ".legacy.sqlite3")


def _db_kind(path: Path) -> str:
    name = Path(path).name
    if name.endswith(".memory.sqlite3"):
        return "memory"
    if name.endswith(".persona.sqlite3"):
        return "persona"
    if name.endswith(".legacy.sqlite3"):
        return "legacy"
    return "single"


def _profile_stem(path: Path) -> str:
    name = Path(path).name
    for suffix in _SPLIT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name[:-len(".sqlite3")]


def _contact_groups() -> dict[str, dict[str, Path]]:
    """联系人 stem → {memory/persona/legacy/single: 路径}（兼容未迁移旧库）。"""
    groups: dict[str, dict[str, Path]] = {}
    try:
        paths = sorted(Path(CONTACTS_DIR).glob("*.sqlite3"))
    except OSError:
        return groups
    for path in paths:
        groups.setdefault(_profile_stem(path), {})[_db_kind(path)] = path
    return groups


def _split_helpers():
    """优先用 agi.selflearn.db.memory_path/persona_path（w2-inner 提供）；没有就自己 glob。"""
    try:
        from agi.selflearn import db as db_mod
        if hasattr(db_mod, "memory_path") and hasattr(db_mod, "persona_path"):
            return db_mod
    except Exception:  # noqa: BLE001
        return None
    return None


def _data_db_candidates() -> list[Path]:
    """所有含 messages/user_memories/claims 的库（memory + 未迁移单库；legacy 只做展示）。"""
    try:
        memory = sorted(Path(CONTACTS_DIR).glob("*.memory.sqlite3"))
        singles = [p for p in sorted(Path(CONTACTS_DIR).glob("*.sqlite3"))
                   if _db_kind(p) == "single"]
    except OSError:
        return []
    return memory + singles


def _all_contact_dbs() -> list[Path]:
    return _data_db_candidates()


def _alias_for_stem(stem: str) -> str:
    raw = gateway_config()
    names: list[str] = []
    for key in ("allowed_contacts", "proactive_contacts", "monitored_contacts"):
        for name in raw.get(key) or []:
            text = str(name or "").strip()
            if text and text not in names:
                names.append(text)
    for name in names:
        for algo in ("sha1", "md5"):
            digest = hashlib.new(algo, name.encode("utf-8")).hexdigest()
            if digest[:14] == stem or digest[:16] == stem:
                return name
    return stem


def _contact_paths(alias_or_stem: str) -> dict[str, Path]:
    raw = str(alias_or_stem or "").strip()
    if not raw:
        return {}
    safe = re.sub(r"[^\w\u4e00-\u9fff-]", "", raw)[:60]
    stems = [safe]
    for algo in ("sha1", "md5"):
        digest = hashlib.new(algo, raw.encode("utf-8")).hexdigest()
        stems.extend([digest[:14], digest[:16]])  # 旧库 14 位 / 新命名 16 位都兼容
    helper = _split_helpers()
    for stem in stems:
        found: dict[str, Path] = {}
        if helper is not None:
            base = Path(CONTACTS_DIR) / (stem + ".sqlite3")
            for kind, resolver in (("memory", helper.memory_path),
                                   ("persona", helper.persona_path)):
                try:
                    resolved = Path(resolver(base))
                    if resolved.is_file():
                        found[kind] = resolved
                except Exception:  # noqa: BLE001 - helper 签名不同/文件不存在都回退
                    continue
        for suffix, kind in ((".memory.sqlite3", "memory"),
                             (".persona.sqlite3", "persona"),
                             (".legacy.sqlite3", "legacy"),
                             (".sqlite3", "single")):
            candidate = Path(CONTACTS_DIR) / (stem + suffix)
            try:
                if candidate.is_file():
                    found[kind] = candidate
            except OSError:
                continue
        if found:
            return found
    groups = _contact_groups()
    for stem in stems:
        if stem in groups:
            return dict(groups[stem])
    for group_stem, files in groups.items():
        if group_stem == safe or group_stem.startswith(safe):
            return dict(files)
    return {}


def _contact_db_by_alias(contact: str) -> Path | None:
    """联系人 alias → 数据（记忆/对话）库：memory > 未迁移单库 > legacy。"""
    files = _contact_paths(contact)
    for kind in ("memory", "single", "legacy"):
        if files.get(kind):
            return files[kind]
    return None


def _contact_aliases() -> list[dict]:
    """配置里的联系人 alias → 库文件（用于前端下拉）。"""
    raw = gateway_config()
    names: list[str] = []
    for key in ("allowed_contacts", "proactive_contacts", "monitored_contacts"):
        for name in raw.get(key) or []:
            text = str(name or "").strip()
            if text and text not in names:
                names.append(text)
    out = []
    for name in names:
        files = _contact_paths(name)
        db_path = None
        for kind in ("memory", "single", "legacy"):
            if files.get(kind):
                db_path = files[kind]
                break
        out.append({"alias": name, "db": db_path.name if db_path else None,
                    "exists": bool(db_path)})
    return out


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for item in Path(path).rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _db_counts(path: Path, table: str) -> int:
    try:
        con = _ro_connect(path)
        try:
            return int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def _file_bytes(path: Path | None) -> int:
    try:
        return Path(path).stat().st_size if path and Path(path).is_file() else 0
    except OSError:
        return 0


_PERSONA_TABLES = ("persona", "persona_layers", "skills", "state", "trust",
                   "lessons", "proactive", "goals", "user_profile")


def _persona_rows(path: Path | None) -> int:
    if path is None:
        return 0
    total = 0
    for table in _PERSONA_TABLES:
        total += _db_counts(path, table)
    return total


def data_info() -> dict:
    groups = _contact_groups()
    details: list[dict] = []
    messages = memories = claims = 0
    for stem, files in sorted(groups.items()):
        data_db = files.get("memory") or files.get("single")
        memory_detail = None
        if data_db is not None:
            count_messages = _db_counts(data_db, "messages")
            count_memories = _db_counts(data_db, "user_memories")
            count_claims = _db_counts(data_db, "claims")
            messages += count_messages
            memories += count_memories
            claims += count_claims
            memory_detail = {
                "name": data_db.name, "path": str(data_db),
                "bytes": _file_bytes(data_db), "kind": _db_kind(data_db),
                "messages": count_messages, "memories": count_memories,
                "claims": count_claims,
            }
        persona = files.get("persona")
        persona_detail = None
        if persona is not None:
            persona_detail = {"name": persona.name, "path": str(persona),
                              "bytes": _file_bytes(persona),
                              "rows": _persona_rows(persona)}
        legacy = files.get("legacy")
        legacy_detail = None
        if legacy is not None:
            legacy_detail = {"name": legacy.name, "path": str(legacy),
                             "bytes": _file_bytes(legacy),
                             "note": "迁移前的旧库（可删除/可回滚，不参与统计）"}
        details.append({
            "stem": stem, "alias": _alias_for_stem(stem),
            "memory": memory_detail, "persona": persona_detail,
            "legacy": legacy_detail,
            "single": bool(files.get("single")) and "memory" not in files,
            "migrated": bool(files.get("memory") and files.get("persona")),
        })
    media_files = 0
    for directory in MEDIA_ASSET_DIRS:
        try:
            media_files += sum(1 for item in Path(directory).rglob("*") if item.is_file())
        except OSError:
            continue
    backups = []
    try:
        for item in Path(BACKUP_DIR).glob("*.zip"):
            try:
                stat = item.stat()
            except OSError:
                continue
            backups.append({"name": item.name, "bytes": stat.st_size,
                            "mtime": stat.st_mtime, "path": str(item)})
    except OSError:
        backups = []
    backups.sort(key=lambda row: row["mtime"], reverse=True)
    try:
        memory_count = len(list(Path(CONTACTS_DIR).glob("*.memory.sqlite3")))
        persona_count = len(list(Path(CONTACTS_DIR).glob("*.persona.sqlite3")))
        legacy_count = len(list(Path(CONTACTS_DIR).glob("*.legacy.sqlite3")))
    except OSError:
        memory_count = persona_count = legacy_count = 0
    return {
        "ok": True,
        "selflearn_bytes": _dir_size(Path(SELFLEARN_DIR)),
        "contacts": len(groups),
        "messages": messages,
        "memories": memories,
        "claims": claims,
        "memory_dbs": memory_count,
        "persona_dbs": persona_count,
        "legacy_dbs": legacy_count,
        "split_layout": bool(memory_count or persona_count),
        "contacts_detail": details,
        "gateway_log_bytes": Path(GATEWAY_LOG).stat().st_size if Path(GATEWAY_LOG).is_file() else 0,
        "gateway_log_lines": _count_file_lines(GATEWAY_LOG),
        "media_files": media_files,
        "backup_dir": str(BACKUP_DIR),
        "backups": backups[:30],
        "aliases": _contact_aliases(),
        "note": "只读统计；备份不会修改任何数据。记忆库=对话/记忆，人格库=人格/状态；legacy 为迁移前旧库",
    }
def data_backup(*, client: str = "") -> dict:
    try:
        info = create_backup(prefix="manual")
    except Exception as exc:  # noqa: BLE001
        audit("data_backup", {}, ok=False, note=str(exc), client=client)
        return {"ok": False, "error": f"backup_failed:{type(exc).__name__}: {exc}"}
    audit("data_backup", {"path": info["path"], "files": info["files"],
                          "bytes": info["bytes"]}, ok=True, client=client)
    return {"ok": True, "backup_path": info["path"], "files": info["files"],
            "bytes": info["bytes"],
            "note": "已打包运行数据（未改动任何数据）；zip 在备份目录"}


def _backup_zip_path(name: str) -> Path | None:
    """只允许备份目录下的 *.zip。"""
    raw = str(name or "").strip()
    if not raw or raw != Path(raw).name:
        return None
    target = (Path(BACKUP_DIR) / raw).resolve()
    try:
        target.relative_to(Path(BACKUP_DIR).resolve())
    except ValueError:
        return None
    if target.suffix.lower() != ".zip" or not target.is_file():
        return None
    return target


def data_backup_delete(name: str, confirm: str = "", *, client: str = "") -> dict:
    if str(confirm or "").strip() != BACKUP_DELETE_CONFIRM:
        return {"ok": False, "error": "confirm_phrase_mismatch",
                "hint": f"必须输入：{BACKUP_DELETE_CONFIRM}"}
    target = _backup_zip_path(name)
    if target is None:
        audit("data_backup_delete", {"name": name}, ok=False, note="unsafe_path",
              client=client)
        return {"ok": False, "error": "unsafe_backup_path"}
    try:
        target.unlink()
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    audit("data_backup_delete", {"name": target.name}, ok=True, client=client)
    return {"ok": True, "deleted": target.name}


def data_backup_path(name: str) -> Path | None:
    return _backup_zip_path(name)


def _collect_messages(db_path: Path, limit: int) -> list[dict]:
    try:
        con = _ro_connect(db_path)
        try:
            columns = _columns(con, "messages")
            wanted = [name for name in ("id", "role", "text", "ts", "proactive")
                      if name in columns]
            if not wanted:
                return []
            rows = con.execute(
                f"SELECT {','.join(wanted)} FROM messages ORDER BY ts ASC, rowid ASC LIMIT ?",
                (limit,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _contact_for_db(db_path: Path) -> str:
    for row in _contact_aliases():
        if row.get("db") == db_path.name:
            return str(row["alias"])
    return db_path.stem


def data_export(contact: str = "", fmt: str = "jsonl", limit: int = 1000) -> dict:
    fmt = str(fmt or "jsonl").strip().lower()
    if fmt not in ("jsonl", "md"):
        return {"ok": False, "error": "format 只能是 jsonl/md"}
    try:
        limit = max(1, min(5000, int(limit)))
    except (TypeError, ValueError):
        limit = 1000
    if contact:
        db_path = _contact_db_by_alias(contact)
        if db_path is None:
            return {"ok": False, "error": f"contact_not_found: {contact}"}
        dbs = [db_path]
    else:
        dbs = _all_contact_dbs()
    rows: list[dict] = []
    for db_path in dbs:
        alias = _contact_for_db(db_path)
        for item in _collect_messages(db_path, limit):
            rows.append({"contact": alias, "db": db_path.name,
                         "ts": item.get("ts"), "role": item.get("role"),
                         "text": item.get("text"),
                         "proactive": bool(item.get("proactive") or 0)})
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe_contact = re.sub(r"[^\w\u4e00-\u9fff-]", "", contact or "all")[:40] or "all"
    if fmt == "md":
        lines = [f"# 聊天记录导出（{contact or '全部联系人'}）", ""]
        current = None
        for row in rows:
            if row["contact"] != current:
                current = row["contact"]
                lines.append(f"## {current}")
                lines.append("")
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(row.get("ts") or 0)))
            role = "我" if row.get("role") == "assistant" else "对方"
            tag = "（主动）" if row.get("proactive") else ""
            lines.append(f"- **{when} {role}{tag}**：{row.get('text') or ''}")
        body = "\n".join(lines) + "\n"
        content_type = "text/markdown; charset=utf-8"
        filename = f"export_{safe_contact}_{stamp}.md"
    else:
        body = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else "")
        content_type = "application/x-ndjson; charset=utf-8"
        filename = f"export_{safe_contact}_{stamp}.jsonl"
    return {"ok": True, "body": body.encode("utf-8"), "content_type": content_type,
            "filename": filename, "count": len(rows),
            "note": f"导出 {len(rows)} 条消息（{len(dbs)} 个联系人库）"}


def data_export_memories(contact: str = "", limit: int = 5000) -> dict:
    try:
        limit = max(1, min(5000, int(limit)))
    except (TypeError, ValueError):
        limit = 5000
    if contact:
        db_path = _contact_db_by_alias(contact)
        if db_path is None:
            return {"ok": False, "error": f"contact_not_found: {contact}"}
        dbs = [db_path]
    else:
        dbs = _all_contact_dbs()
    rows: list[dict] = []
    for db_path in dbs:
        try:
            con = _ro_connect(db_path)
            try:
                columns = _columns(con, "user_memories")
                if not columns:
                    continue
                selected = [name for name in
                            ("id", "kind", "content", "evidence", "confidence",
                             "status", "created_at", "importance", "access_count",
                             "last_accessed", "evidence_history") if name in columns]
                data = [dict(row) for row in con.execute(
                    f"SELECT {','.join(selected)} FROM user_memories LIMIT ?",
                    (limit,)).fetchall()]
            finally:
                con.close()
        except sqlite3.Error:
            continue
        for row in data:
            row["contact"] = _contact_for_db(db_path)
            row["db"] = db_path.name
            rows.append(row)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return {"ok": True,
            "body": json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8"),
            "content_type": "application/json; charset=utf-8",
            "filename": f"memories_{contact or 'all'}_{stamp}.json",
            "count": len(rows)}


def _validate_zip_entries(names: list[str]) -> tuple[bool, str]:
    for name in names:
        normalized = str(name).replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            return False, f"unsafe_zip_entry:{name}"
    has_selflearn = any(str(name).replace("\\", "/").startswith("selflearn/") for name in names)
    has_prompts = any(str(name).replace("\\", "/") == "prompts.json" for name in names)
    if not (has_selflearn or has_prompts):
        return False, "zip_missing_selflearn_or_prompts"
    return True, ""


def data_import(zip_path: str, confirm: str = "", mode: str = "merge", *,
                overwrite_confirm: str = "", client: str = "") -> dict:
    target = _backup_zip_path(zip_path)
    if target is None:
        audit("data_import", {"zip": zip_path}, ok=False, note="unsafe_or_missing_zip",
              client=client)
        return {"ok": False, "error": "unsafe_or_missing_zip",
                "hint": "只允许导入 E:\\agent\\backups\\ 下的 .zip"}
    if str(confirm or "").strip() != IMPORT_CONFIRM:
        return {"ok": False, "error": "confirm_phrase_mismatch",
                "hint": f"必须输入：{IMPORT_CONFIRM}"}
    mode = str(mode or "merge").strip().lower()
    if mode not in ("merge", "overwrite"):
        return {"ok": False, "error": "mode 只能是 merge/overwrite"}
    if mode == "overwrite" and str(overwrite_confirm or "").strip() != IMPORT_OVERWRITE_CONFIRM:
        return {"ok": False, "error": "overwrite_confirm_required",
                "hint": f"overwrite 必须再输入：{IMPORT_OVERWRITE_CONFIRM}"}
    temp_dir = None
    try:
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            ok_zip, reason = _validate_zip_entries(names)
            if not ok_zip:
                return {"ok": False, "error": reason}
            temp_dir = tempfile.mkdtemp(prefix="console_import_")
            archive.extractall(temp_dir)
    except (zipfile.BadZipFile, OSError) as exc:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        audit("data_import", {"zip": target.name, "mode": mode}, ok=False,
              note=str(exc), client=client)
        return {"ok": False, "error": f"bad_zip:{type(exc).__name__}: {exc}"}
    imported: list[str] = []
    skipped: list[str] = []
    try:
        source_dir = Path(temp_dir) / "selflearn" / "wechat_contacts"
        target_dir = Path(CONTACTS_DIR)
        if source_dir.is_dir():
            target_dir.mkdir(parents=True, exist_ok=True)
            for source in sorted(source_dir.glob("*.sqlite3")):
                destination = target_dir / source.name
                if destination.exists() and mode != "overwrite":
                    skipped.append(source.name)
                    continue
                shutil.copy2(source, destination)
                imported.append(source.name)
    except OSError as exc:
        return {"ok": False, "error": f"import_failed:{type(exc).__name__}: {exc}",
                "imported": imported, "skipped": skipped}
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
    audit("data_import", {"zip": target.name, "mode": mode,
                          "imported": imported, "skipped": skipped}, ok=True,
          client=client)
    return {"ok": True, "mode": mode, "imported": imported, "skipped": skipped,
            "note": f"导入完成：新增 {len(imported)}，跳过 {len(skipped)}（不覆盖）"
                    if mode == "merge" else f"已覆盖导入 {len(imported)} 个库"}


# --------------------------------------------------------------------------- #
# 记忆编辑
# --------------------------------------------------------------------------- #
def _memory_db(contact: str = "") -> tuple[Path | None, dict | None]:
    if contact:
        db_path = _contact_db_by_alias(contact)
    else:
        db_path = contact_db_path()
    if db_path is None or not Path(db_path).is_file():
        return None, {"ok": False, "error": "contact_db_not_found",
                      "hint": f"找不到联系人库: {contact or 'default'}"}
    return Path(db_path), None


def _memory_cols(con: sqlite3.Connection) -> set[str]:
    return _columns(con, "user_memories")


def _memory_row(con: sqlite3.Connection, mem_id: str) -> dict | None:
    try:
        columns = _memory_cols(con)
        if not columns:
            return None
        row = con.execute("SELECT * FROM user_memories WHERE id = ?", (mem_id,)).fetchone()
        return dict(row) if row is not None else None
    except sqlite3.Error:
        return None


def _forget_meta(item: dict) -> dict:
    """给记忆行补遗忘度展示字段；不改变原库/接口语义。"""
    try:
        strength = float(item.get("strength"))
    except (TypeError, ValueError):
        strength = 1.0
    strength = max(0.0, min(1.0, strength))
    degree = round(1.0 - strength, 3)
    stage = str(item.get("forget_stage") or "").strip()
    half_life = None
    try:
        from agi.selflearn import memory_decay
        if not stage:
            stage = memory_decay.stage_for(strength)
        half_life = round(memory_decay.half_life_days(
            str(item.get("kind") or ""), float(item.get("importance") or 0.5)), 1)
    except Exception:  # noqa: BLE001 - 展示字段失败不影响记忆列表
        if not stage:
            stage = "full" if strength >= 0.5 else ("compressed" if strength >= 0.2
                                                    else ("keyword" if strength >= 0.05 else "dormant"))
        half_life = 30.0
    stage_zh = {"full": "完整", "compressed": "压缩", "keyword": "关键词",
                "dormant": "休眠"}.get(stage, stage or "未知")
    return {
        "strength": round(strength, 3),
        "forget_degree": degree,
        "forget_percent": int(round(degree * 100)),
        "forget_stage": stage,
        "forget_stage_zh": stage_zh,
        "half_life_days": half_life,
        "last_strength_at": item.get("last_strength_at"),
    }


def memory_list(contact: str = "", q: str = "", status: str = "",
                limit: int = 100, offset: int = 0) -> dict:
    db_path, error = _memory_db(contact)
    if error:
        return {**error, "items": [], "total": 0}
    try:
        limit = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    where = []
    params: list = []
    q = str(q or "").strip()
    if q:
        where.append("(content LIKE ? OR kind LIKE ? OR evidence LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    status = str(status or "").strip().lower()
    if status:
        if status not in MEMORY_STATUSES:
            return {"ok": False, "error": f"status 非法: {status}", "items": [], "total": 0}
        where.append("status = ?")
        params.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        con = _ro_connect(db_path)
        try:
            columns = _memory_cols(con)
            if not columns:
                return {"ok": True, "items": [], "total": 0, "db": str(db_path)}
            selected = [name for name in
                        ("id", "kind", "content", "importance", "confidence", "status",
                         "created_at", "evidence", "evidence_history", "strength",
                         "last_strength_at", "forget_stage", "access_count",
                         "last_accessed") if name in columns]
            total = int(con.execute(
                f"SELECT count(*) FROM user_memories{clause}", params).fetchone()[0])
            rows = con.execute(
                f"SELECT {','.join(selected)} FROM user_memories{clause} "
                "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (*params, limit, offset)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "items": [], "total": 0}
    items = []
    for row in rows:
        item = dict(row)
        history = item.get("evidence_history")
        if isinstance(history, str) and len(history) > 300:
            item["evidence_history"] = history[:300] + "…"
        item.update(_forget_meta(item))
        items.append(item)
    return {"ok": True, "items": items, "total": total, "limit": limit,
            "offset": offset, "db": str(db_path)}


def memory_claims(contact: str = "", limit: int = 100) -> dict:
    db_path, error = _memory_db(contact)
    if error:
        return {**error, "items": []}
    try:
        limit = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        limit = 100
    try:
        con = _ro_connect(db_path)
        try:
            columns = _columns(con, "claims")
            if not columns:
                return {"ok": True, "items": []}
            selected = [name for name in
                        ("id", "subject", "predicate", "object", "confidence", "status",
                         "created_at") if name in columns]
            rows = con.execute(
                f"SELECT {','.join(selected)} FROM claims "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "items": []}
    return {"ok": True, "items": [dict(row) for row in rows]}


def _memory_write(contact: str, mem_id: str, mutate) -> dict:
    """打开可写连接执行 mutate(con, row, cols)，返回更新后的行。"""
    db_path, error = _memory_db(contact)
    if error:
        return error
    try:
        con = sqlite3.connect(str(db_path), timeout=5)
        con.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        columns = _memory_cols(con)
        row = _memory_row(con, mem_id)
        if row is None:
            return {"ok": False, "error": f"memory_not_found: {mem_id}"}
        updates = mutate(con, row, columns)
        sets = ", ".join(f"{key} = ?" for key in updates)
        with con:
            con.execute(f"UPDATE user_memories SET {sets} WHERE id = ?",
                        (*updates.values(), mem_id))
        updated = _memory_row(con, mem_id)
        return {"ok": True, "item": updated, "db": str(db_path)}
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        con.close()


def create_contact_db(alias: str, *, client: str = "") -> dict:
    """为还没有库的联系人一键创建 memory/persona 库（空库，不生成训练数据）。"""
    name = str(alias or "").strip()
    if not name:
        return {"ok": False, "error": "联系人不能为空"}
    if len(name) > 60:
        return {"ok": False, "error": "联系名过长"}
    try:
        existing = _contact_db_by_alias(name)
    except Exception:  # noqa: BLE001
        existing = None
    if existing is not None and Path(existing).is_file():
        return {"ok": True, "created": False, "db": str(existing), "alias": name,
                "note": "该联系人已有数据库"}
    stem = hashlib.sha1(name.encode("utf-8")).hexdigest()[:14]
    base = Path(CONTACTS_DIR) / (stem + ".sqlite3")
    try:
        from agi.selflearn import db as db_mod
        conn = db_mod.connect(base)
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    audit("contact_db_create", {"alias": name, "db": str(base)}, ok=True, client=client)
    return {"ok": True, "created": True, "db": str(base), "alias": name,
            "note": "已创建空数据库，可直接聊天；记忆会从这里开始积累"}


def memory_clear_all(contact: str = "", confirm: str = "", *, client: str = "") -> dict:
    """清空当前联系人记忆库（先备份；人格库/提示词/日记保留）。

    旧版数据可能同时存在 .memory.sqlite3 和 .legacy.sqlite3，列表接口可能读 legacy；
    这里把同一联系人的 memory/legacy 库都清掉，避免“清了还有”。
    """
    if str(confirm or "").strip() != "清除所有记忆":
        return {"ok": False, "error": "confirm_phrase_mismatch",
                "hint": "必须确认：清除所有记忆"}
    db_path, error = _memory_db(contact)
    if error:
        return error
    base = Path(db_path)
    stem = base.name.split(".", 1)[0]
    candidates = [base]
    try:
        for sibling in base.parent.glob(stem + ".*sqlite3"):
            if sibling.name.endswith(".persona.sqlite3"):
                continue
            if sibling not in candidates:
                candidates.append(sibling)
    except OSError:
        pass
    tables = (
        "user_memories", "claims", "summaries",
        "raw_events", "sessions", "session_segments", "turns", "pairs",
        "inner_states", "feedback", "turn_traces", "media_understanding",
        "proactive_events", "research_sources", "reply_media_log",
        "emoji_usage", "bili_impressions", "urges", "user_profile",
        "conflicts", "score_records", "weak_rechecks", "search_documents",
    )
    deleted: dict[str, int] = {}
    backups: list[str] = []
    backup_dir = Path(SELFLEARN_DIR) / "db_backups"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"backup_dir_failed: {exc}"}
    for path in candidates:
        if not path.is_file():
            continue
        try:
            backup = backup_dir / f"{path.name}.clear-{time.strftime('%Y%m%d-%H%M%S')}.bak"
            shutil.copy2(path, backup)
            backups.append(str(backup))
        except OSError as exc:
            return {"ok": False, "error": f"backup_failed: {exc}"}
        try:
            con = sqlite3.connect(str(path), timeout=10)
            try:
                existing = {row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                with con:
                    for table in tables:
                        if table not in existing:
                            continue
                        try:
                            count = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                            if count:
                                con.execute(f'DELETE FROM "{table}"')
                                deleted[f"{path.name}:{table}"] = int(count)
                        except sqlite3.Error:
                            continue
            finally:
                con.close()
        except sqlite3.Error as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "backup": backups}
    # 内状态/心情在 persona 库；人格本身保留，但状态重置（否则清完记忆心情还在）
    persona_paths = []
    try:
        persona_paths = [x for x in base.parent.glob(stem + ".persona.sqlite3") if x.is_file()]
    except OSError:
        persona_paths = []
    persona_tables = ("state", "mood_state", "mood_timeline")
    for path in persona_paths:
        try:
            backup = backup_dir / f"{path.name}.clear-{time.strftime('%Y%m%d-%H%M%S')}.bak"
            shutil.copy2(path, backup)
            backups.append(str(backup))
        except OSError as exc:
            return {"ok": False, "error": f"backup_failed: {exc}", "backup": backups}
        try:
            con = sqlite3.connect(str(path), timeout=10)
            try:
                existing = {row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                with con:
                    for table in persona_tables:
                        if table not in existing:
                            continue
                        try:
                            count = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                            if count:
                                con.execute(f'DELETE FROM "{table}"')
                                deleted[f"{path.name}:{table}"] = int(count)
                        except sqlite3.Error:
                            continue
            finally:
                con.close()
        except sqlite3.Error as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "backup": backups}
    audit("memory_clear_all", {"contact": contact or "default",
                               "deleted": sum(deleted.values())},
          ok=True, client=client)
    return {"ok": True, "deleted": deleted, "total": sum(deleted.values()),
            "backup": backups, "db": str(db_path),
            "note": "已清空当前联系人的记忆/事实/摘要，并重置内状态/心情；聊天记录、人格、提示词、日记保留"}


def chat_clear_all(contact: str = "", confirm: str = "", *, client: str = "") -> dict:
    """清空当前联系人的聊天记录（先备份；记忆/人格/提示词保留）。"""
    if str(confirm or "").strip() != "清空聊天":
        return {"ok": False, "error": "confirm_phrase_mismatch", "hint": "必须确认：清空聊天"}
    db_path, error = _memory_db(contact)
    if error:
        return error
    base = Path(db_path)
    stem = base.name.split(".", 1)[0]
    candidates = [base]
    try:
        for sibling in base.parent.glob(stem + ".*sqlite3"):
            if sibling.name.endswith(".persona.sqlite3"):
                continue
            if sibling not in candidates:
                candidates.append(sibling)
    except OSError:
        pass
    tables = ("messages", "attachments", "raw_events", "sessions",
              "session_segments", "turns", "pairs")
    deleted: dict[str, int] = {}
    backups: list[str] = []
    backup_dir = Path(SELFLEARN_DIR) / "db_backups"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"backup_dir_failed: {exc}"}
    for path in candidates:
        if not path.is_file():
            continue
        try:
            backup = backup_dir / f"{path.name}.chatclear-{time.strftime('%Y%m%d-%H%M%S')}.bak"
            shutil.copy2(path, backup)
            backups.append(str(backup))
        except OSError as exc:
            return {"ok": False, "error": f"backup_failed: {exc}"}
        try:
            con = sqlite3.connect(str(path), timeout=10)
            try:
                existing = {row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                with con:
                    for table in tables:
                        if table not in existing:
                            continue
                        try:
                            count = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                            if count:
                                con.execute(f'DELETE FROM "{table}"')
                                deleted[f"{path.name}:{table}"] = int(count)
                        except sqlite3.Error:
                            continue
            finally:
                con.close()
        except sqlite3.Error as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "backup": backups}
    audit("chat_clear_all", {"contact": contact or "default", "deleted": sum(deleted.values())},
          ok=True, client=client)
    return {"ok": True, "deleted": deleted, "total": sum(deleted.values()),
            "backup": backups, "db": str(db_path),
            "note": "已清空当前联系人聊天记录；记忆/人格/提示词保留"}


def memory_update(contact: str = "", mem_id: str = "", *, content: str | None = None,
                  importance=None, status: str | None = None,
                  client: str = "") -> dict:
    mem_id = str(mem_id or "").strip()
    if not mem_id:
        return {"ok": False, "error": "id 不能为空"}
    updates: dict = {}
    if content is not None:
        text = str(content).strip()
        if not text:
            return {"ok": False, "error": "content 不能为空"}
        if len(text) > MEMORY_MAX_CONTENT:
            return {"ok": False, "error": f"content 超过 {MEMORY_MAX_CONTENT} 字"}
        updates["content"] = text
    if importance is not None:
        try:
            number = float(importance)
        except (TypeError, ValueError):
            return {"ok": False, "error": "importance 必须是数字"}
        if not 0.0 <= number <= 1.0:
            return {"ok": False, "error": "importance 取值 0–1"}
        updates["importance"] = round(number, 3)
    if status is not None:
        status_value = str(status).strip().lower()
        if status_value not in MEMORY_STATUSES:
            return {"ok": False, "error": "status 只能是 active/invalid/dormant"}
        updates["status"] = status_value
    if not updates:
        return {"ok": False, "error": "没有要修改的字段"}

    def mutate(con, row, columns):
        payload = dict(updates)
        if "content" in payload and "evidence" in columns:
            evidence = str(row.get("evidence") or "").strip()
            payload["evidence"] = ((evidence + " | ") if evidence else "") + "manual_edit"
        if "updated_at" in columns:
            payload["updated_at"] = time.time()
        return payload

    result = _memory_write(contact, mem_id, mutate)
    audit("memory_edit", {"id": mem_id, "action": "update", "fields": sorted(updates)},
          ok=bool(result.get("ok")), client=client)
    return result


def memory_delete(contact: str = "", mem_id: str = "", confirm: str = "",
                  *, client: str = "") -> dict:
    if str(confirm or "").strip() != MEMORY_DELETE_CONFIRM:
        return {"ok": False, "error": "confirm_phrase_mismatch",
                "hint": f"必须输入：{MEMORY_DELETE_CONFIRM}"}
    result = _memory_write(contact, str(mem_id or "").strip(),
                           lambda con, row, columns: {"status": "invalid"})
    audit("memory_edit", {"id": mem_id, "action": "soft_delete"},
          ok=bool(result.get("ok")), client=client)
    if result.get("ok"):
        result["note"] = "已软删除（status=invalid，不物理删除）"
    return result


def memory_mark(contact: str = "", mem_id: str = "", important: bool = True,
                *, client: str = "") -> dict:
    value = 1.0 if bool(important) else 0.5
    result = _memory_write(contact, str(mem_id or "").strip(),
                           lambda con, row, columns: {"importance": value})
    audit("memory_edit", {"id": mem_id, "action": "mark", "important": bool(important)},
          ok=bool(result.get("ok")), client=client)
    return result


def memory_merge(contact: str = "", ids: list | None = None, *, client: str = "") -> dict:
    try:
        left_id, right_id = [str(item).strip() for item in (ids or [])][:2]
    except ValueError:
        left_id = right_id = ""
    if not left_id or not right_id or left_id == right_id:
        return {"ok": False, "error": "需要两条不同的记忆 id"}
    db_path, error = _memory_db(contact)
    if error:
        return error
    try:
        con = sqlite3.connect(str(db_path), timeout=5)
        con.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        columns = _memory_cols(con)
        left = _memory_row(con, left_id)
        right = _memory_row(con, right_id)
        if left is None or right is None:
            return {"ok": False, "error": "其中一条记忆不存在"}
        keep, drop = ((left, right)
                      if float(left.get("created_at") or 0) <= float(right.get("created_at") or 0)
                      else (right, left))
        updates: dict = {"status": "active"}
        if "evidence" in columns:
            evidence = [str(item.get("evidence") or "").strip()
                        for item in (keep, drop) if str(item.get("evidence") or "").strip()]
            updates["evidence"] = " | ".join(evidence)[:1000]
        if "evidence_history" in columns:
            history = []
            try:
                history = json.loads(str(keep.get("evidence_history") or "[]"))
            except (TypeError, ValueError):
                history = []
            if not isinstance(history, list):
                history = []
            history.append({"kind": "manual_merge", "from": drop.get("id"),
                            "at": round(time.time(), 3)})
            updates["evidence_history"] = json.dumps(history, ensure_ascii=False)
        if "history" in columns:
            pass
        with con:
            con.execute("UPDATE user_memories SET status='invalid' WHERE id = ?",
                        (drop["id"],))
            sets = ", ".join(f"{key} = ?" for key in updates)
            con.execute(f"UPDATE user_memories SET {sets} WHERE id = ?",
                        (*updates.values(), keep["id"]))
        merged = _memory_row(con, keep["id"])
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        con.close()
    audit("memory_edit", {"action": "merge", "keep": keep["id"], "drop": drop["id"]},
          ok=True, client=client)
    return {"ok": True, "item": merged, "kept": keep["id"], "invalidated": drop["id"],
            "note": "保留较早的记忆，另一条标记 invalid（可恢复）"}


# --------------------------------------------------------------------------- #
# P8-a：统计（发送/阻塞/丢弃/重生成/内状态/延迟/媒体）
# --------------------------------------------------------------------------- #
STATS_MAX_LINES = 200_000


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight, 2)


def _iter_tail_lines(path: Path | str, max_lines: int = STATS_MAX_LINES):
    """从文件尾部懒加载最近 max_lines 行（不整文件读入内存，新→旧）。"""
    target = Path(path)
    if not target.is_file():
        return
    try:
        total = target.stat().st_size
    except OSError:
        return
    if max_lines <= 0 or total <= 0:
        return
    yielded = 0
    try:
        with target.open("rb") as handle:
            position = total
            buffer = b""
            while position > 0 and yielded < max_lines:
                step = min(READ_LIMIT_BYTES, position)
                position -= step
                handle.seek(position)
                buffer = handle.read(step) + buffer
                parts = buffer.split(b"\n")
                if position > 0:
                    buffer = parts[0]
                    parts = parts[1:]
                else:
                    buffer = b""
                for raw in reversed(parts):
                    if yielded >= max_lines:
                        return
                    line = raw.decode("utf-8", "replace").strip()
                    if line:
                        yielded += 1
                        yield line
    except OSError:
        return


def _stats_contact_match(event: dict, contact: str) -> bool:
    if not contact:
        return True
    needle = contact.casefold()
    for key in ("friend", "contact", "chat_name"):
        value = str(event.get(key) or "").casefold()
        if needle and needle in value:
            return True
    return False


def _stats_db_part(contact: str) -> tuple[Path | None, list[str]]:
    errors: list[str] = []
    if contact:
        db_path = _contact_db_by_alias(contact)
    else:
        db_path = contact_db_path()
    if db_path is None or not Path(db_path).is_file():
        errors.append(f"contact_db_not_found:{contact or 'default'}")
        return None, errors
    return Path(db_path), errors


def _stats_messages(path: Path | None) -> dict:
    if path is None:
        return {"total": 0, "mine": 0, "theirs": 0, "avg_len": None,
                "median_len": None, "active_24h": 0}
    try:
        con = _ro_connect(path)
        try:
            columns = _columns(con, "messages")
            if not columns:
                return {"total": 0, "mine": 0, "theirs": 0, "avg_len": None,
                        "median_len": None, "active_24h": 0}
            total = int(con.execute("SELECT count(*) FROM messages").fetchone()[0])
            mine = int(con.execute(
                "SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0])
            theirs = int(con.execute(
                "SELECT count(*) FROM messages WHERE role='user'").fetchone()[0])
            lengths = [len(str(row[0] or "")) for row in con.execute(
                "SELECT text FROM messages").fetchall()]
            active = int(con.execute(
                "SELECT count(*) FROM messages WHERE ts >= ?",
                (time.time() - 86400,)).fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error:
        return {"total": 0, "mine": 0, "theirs": 0, "avg_len": None,
                "median_len": None, "active_24h": 0}
    lengths = [item for item in lengths if item >= 0]
    averaged = round(sum(lengths) / len(lengths), 1) if lengths else None
    median = _percentile([float(item) for item in lengths], 0.5) if lengths else None
    return {"total": total, "mine": mine, "theirs": theirs,
            "avg_len": averaged, "median_len": median, "active_24h": active}


def _stats_inner(path: Path | None) -> dict:
    empty = {"count": 0, "by_stance": {}, "by_mode": {},
             "latency": {"p50": None, "p95": None, "max": None}}
    if path is None:
        return empty
    try:
        con = _ro_connect(path)
        try:
            columns = _columns(con, "inner_states")
            if not columns:
                return empty
            rows = con.execute(
                "SELECT mode, state_json, latency_ms FROM inner_states").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return empty
    by_stance: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    latencies: list[float] = []
    for row in rows:
        mode = str(row["mode"] or "unknown")
        by_mode[mode] = by_mode.get(mode, 0) + 1
        try:
            state = json.loads(str(row["state_json"] or "{}"))
        except ValueError:
            state = None
        if isinstance(state, dict):
            stance = str(state.get("stance") or "").strip()
            if stance:
                by_stance[stance] = by_stance.get(stance, 0) + 1
        try:
            latency = float(row["latency_ms"])
            if latency >= 0:
                latencies.append(latency)
        except (TypeError, ValueError):
            continue
    return {"count": len(rows), "by_stance": by_stance, "by_mode": by_mode,
            "latency": {"p50": _percentile(latencies, 0.5),
                        "p95": _percentile(latencies, 0.95),
                        "max": round(max(latencies), 2) if latencies else None}}


def _empty_stats_window(days: int) -> list[dict]:
    from datetime import datetime, timedelta
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return [{"date": (today - timedelta(days=offset)).strftime("%Y-%m-%d"),
             "proactive": 0, "outbound": 0, "total": 0}
            for offset in range(days - 1, -1, -1)]


def stats_snapshot(days: int = 7, contact: str = "") -> dict:
    try:
        days = max(1, min(90, int(days)))
    except (TypeError, ValueError):
        days = 7
    now = time.time()
    since = now - days * 86400
    window = {"days": days, "since": since, "until": now, "log_lines_scanned": 0}
    errors: list[str] = []
    sent_by_day = _empty_stats_window(days)
    day_index = {row["date"]: row for row in sent_by_day}
    by_hour = [0] * 24
    counts = {"sent": 0, "failed": 0, "blocked": 0}
    blocked: dict[str, int] = {}
    discarded: dict[str, int] = {}
    regen_reasons: dict[str, int] = {}
    skipped: dict[str, int] = {}
    proactive_schedule = 0
    schedule_intervals: list[float] = []
    media_by_type: dict[str, dict] = {}
    event_latencies: list[float] = []
    recent_blocked: list[dict] = []
    scanned = 0

    for line in _iter_tail_lines(GATEWAY_LOG, STATS_MAX_LINES):
        scanned += 1
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        event_ts = event.get("ts")
        try:
            event_ts = float(event_ts)
        except (TypeError, ValueError):
            event_ts = None
        if event_ts is not None and event_ts < since:
            continue
        if not _stats_contact_match(event, contact):
            continue
        kind = str(event.get("kind") or "")
        if kind == "outbound" or kind == "proactive_send":
            status = str(event.get("status") or "").lower()
            sent = kind == "outbound" or status == "sent"
            if sent:
                if event_ts is not None:
                    day_key = time.strftime("%Y-%m-%d", time.localtime(event_ts))
                    row = day_index.get(day_key)
                    if row is not None:
                        if kind == "proactive_send":
                            row["proactive"] += 1
                        else:
                            row["outbound"] += 1
                        row["total"] += 1
                    by_hour[int(time.strftime("%H", time.localtime(event_ts)))] += 1
                counts["sent"] += 1
            elif status in ("failed", "error", "blocked"):
                counts["failed"] += 1
            for key in ("latency_ms",):
                try:
                    value = float(event.get(key))
                    if value >= 0:
                        event_latencies.append(value)
                except (TypeError, ValueError):
                    pass
            inner = event.get("inner")
            if isinstance(inner, dict):
                try:
                    value = float(inner.get("latency_ms"))
                    if value >= 0:
                        event_latencies.append(value)
                except (TypeError, ValueError):
                    pass
            if kind == "proactive_send":
                check = event.get("text_media_check")
                if isinstance(check, dict):
                    for issue in check.get("regen_failed") or []:
                        key = str(issue)[:80]
                        regen_reasons[key] = regen_reasons.get(key, 0) + 1
        elif kind == "send_blocked":
            counts["blocked"] += 1
            reason = str(event.get("reason") or "unknown")[:80]
            blocked[reason] = blocked.get(reason, 0) + 1
            text = str(event.get("text") or event.get("drag_text") or "")[:40]
            recent_blocked.append({"ts": event_ts, "kind": "send_blocked",
                                   "reason": reason, "text": text})
        elif kind == "proactive_discarded":
            reason = str(event.get("reason") or "unknown")[:80]
            discarded[reason] = discarded.get(reason, 0) + 1
            text = str(event.get("text") or "")[:40]
            recent_blocked.append({"ts": event_ts, "kind": "proactive_discarded",
                                   "reason": reason, "text": text})
        elif kind == "proactive_skipped":
            reason = str(event.get("reason") or "unknown")[:80]
            skipped[reason] = skipped.get(reason, 0) + 1
        elif kind == "proactive_schedule":
            proactive_schedule += 1
            try:
                value = float(event.get("next_delay_s"))
                if value >= 0:
                    schedule_intervals.append(value)
            except (TypeError, ValueError):
                pass
        elif kind in ("send_media", "media_send"):
            media_type = str(event.get("type") or event.get("media_type") or "unknown")[:40]
            entry = media_by_type.setdefault(media_type, {"sent": 0, "failed": 0})
            status = str(event.get("status") or "").lower()
            if status in ("sent", "ok", "success") or event.get("ok") is True:
                entry["sent"] += 1
            else:
                entry["failed"] += 1

    window["log_lines_scanned"] = scanned
    if not Path(GATEWAY_LOG).is_file():
        errors.append("gateway_log_missing")
    success_denominator = counts["sent"] + counts["failed"] + counts["blocked"]
    success_rate = (round(counts["sent"] / success_denominator, 4)
                    if success_denominator else None)
    discarded_total = sum(discarded.values())
    semantic_total = counts["sent"] + discarded_total
    discard_rate = (round(discarded_total / semantic_total, 4)
                    if semantic_total else None)
    avg_interval = (round(sum(schedule_intervals) / len(schedule_intervals), 2)
                    if schedule_intervals else None)
    db_path, db_errors = _stats_db_part(contact)
    errors.extend(db_errors)
    inner = _stats_inner(db_path)
    messages = _stats_messages(db_path)
    recent_blocked.sort(key=lambda row: row.get("ts") or 0, reverse=True)
    return {
        "ok": True,
        "window": window,
        "contact": contact,
        "db": str(db_path) if db_path else None,
        "sent_by_day": sent_by_day,
        "by_hour": by_hour,
        "counts": counts,
        "success_rate": success_rate,
        "discard_rate": discard_rate,
        "blocked": blocked,
        "discarded": discarded,
        "regen_reasons": regen_reasons,
        "proactive": {"schedule": proactive_schedule, "skipped": skipped,
                      "sent": counts["sent"], "avg_interval_s": avg_interval},
        "inner": inner,
        "messages": messages,
        "media": media_by_type,
        "latency": {"p50": _percentile(event_latencies, 0.5),
                    "p95": _percentile(event_latencies, 0.95)},
        "recent_blocked": recent_blocked[:20],
        "errors": errors,
    }


# --------------------------------------------------------------------------- #
# P9-c：心情卡片（内状态页顶部）
# --------------------------------------------------------------------------- #
MOOD_SETTINGS_PATH = SELFLEARN_DIR / "mood.json"
MOOD_WRITABLE_KEYS = ("inject", "judge_enabled")


def _mood_store():
    try:
        from agi.selflearn import mood_judge
        return mood_judge
    except Exception:  # noqa: BLE001
        return None


def _mood_settings_view() -> dict:
    payload = read_json(MOOD_SETTINGS_PATH, {})
    return payload if isinstance(payload, dict) else {}


def _mood_snapshot_raw(contact: str = "", limit: int = 20) -> dict:
    store = _mood_store()
    if store is None:
        return {"ok": False, "error": "mood_unavailable",
                "hint": "agi.selflearn.mood_judge 不可用（P9 模块未落地）"}
    if contact:
        db_path = _contact_db_by_alias(contact)
    else:
        db_path = contact_db_path()
    if db_path is None or not Path(db_path).is_file():
        return {"ok": False, "error": "mood_unavailable",
                "hint": f"找不到联系人库: {contact or 'default'}"}
    try:
        limit = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        limit = 20
    try:
        snapshot = store.snapshot(db_path, timeline_limit=limit)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "mood_unavailable",
                "hint": f"{type(exc).__name__}: {exc}", "db": str(db_path)}
    if not isinstance(snapshot, dict):
        return {"ok": False, "error": "mood_unavailable", "hint": "snapshot 返回非 dict"}
    result = dict(snapshot)
    result["ok"] = True
    result["db"] = str(db_path)
    result["settings_path"] = str(MOOD_SETTINGS_PATH)
    result["pending_inject"] = bool(snapshot.get("pending_inject"))
    return result


MOOD_SOURCE = "mood_judge"
MOOD_AFFECT_NOTE = ("内在状态（affect 0–1）是内部连续量，不作为心情显示；"
                    "本卡只展示 mood_judge 的 0–100 判定。")


def mood_snapshot(contact: str = "", limit: int = 20) -> dict:
    """802 心情卡：mood_judge 判定 + 口径说明（C7）。

    ``source="mood_judge"`` 明确数据来源；``affect_note`` 说明 affect(0–1) 是
    内部连续量、不是心情分数——避免把内状态误读成心情。
    """
    result = _mood_snapshot_raw(contact=contact, limit=limit)
    if isinstance(result, dict):
        result.setdefault("source", MOOD_SOURCE)
        result.setdefault("affect_note", MOOD_AFFECT_NOTE)
    return result


def mood_update(updates: dict, *, client: str = "") -> dict:
    if not isinstance(updates, dict) or not updates:
        return {"ok": False, "error": "body 必须是非空对象"}
    cleaned: dict = {}
    errors: list[str] = []
    for key, value in updates.items():
        if key not in MOOD_WRITABLE_KEYS:
            errors.append(f"not_allowed:{key}")
            continue
        if not isinstance(value, bool):
            try:
                value = bool(_as_bool(value))
            except (TypeError, ValueError):
                errors.append(f"{key}: 需要布尔值")
                continue
        cleaned[key] = bool(value)
    if errors:
        audit("mood_inject_toggle", {"keys": sorted(updates)}, ok=False,
              note=";".join(errors)[:200], client=client)
        return {"ok": False, "errors": errors, "settings": _mood_settings_view()}
    before = _mood_settings_view()
    payload = dict(before)
    payload.update(cleaned)
    try:
        write_json_atomic(MOOD_SETTINGS_PATH, payload)
    except OSError as exc:
        audit("mood_inject_toggle", {"keys": sorted(cleaned)}, ok=False,
              note=str(exc), client=client)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    action = "mood_inject_toggle" if "inject" in cleaned else "mood_judge_toggle"
    audit(action, {"keys": sorted(cleaned), "before": {k: before.get(k) for k in cleaned},
                   "after": cleaned}, ok=True, client=client)
    return {"ok": True, "settings": _mood_settings_view(),
            "changed_keys": sorted(cleaned),
            "path": str(MOOD_SETTINGS_PATH),
            "note": "已写入 mood.json（原子写，保留其它键）；下一轮生效"}


# --------------------------------------------------------------------------- #
# P5-e-B：本地导入 / 愿望满足（不联网）
# --------------------------------------------------------------------------- #
def _media_acquisition():
    try:
        from agi import media_acquisition
        return media_acquisition
    except Exception:  # noqa: BLE001
        return None


def _decode_media_payload(raw: str) -> bytes:
    text = str(raw or "").strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    import base64
    return base64.b64decode(text, validate=True)


def media_import(body: dict, *, client: str = "") -> dict:
    acquisition = _media_acquisition()
    if acquisition is None:
        return {"ok": False, "error": "media_import_unavailable",
                "hint": "agi.media_acquisition 不可用"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "body 必须是对象"}
    files = body.get("files")
    if files is None:
        files = [{k: body.get(k) for k in ("filename", "name", "tags", "data")}]
    if not isinstance(files, list) or not files:
        return {"ok": False, "error": "files 必须是非空数组"}
    default_name = str(body.get("name") or "")
    default_tags = body.get("tags") if isinstance(body.get("tags"), list) else []
    results: list[dict] = []
    for index, item in enumerate(files[:20]):
        entry = item if isinstance(item, dict) else {}
        filename = str(entry.get("filename") or f"import_{index}.png")
        try:
            data = _decode_media_payload(entry.get("data") or "")
        except Exception as exc:  # noqa: BLE001
            results.append({"ok": False, "filename": filename,
                            "status": "invalid", "reason": f"base64_invalid:{exc}"})
            continue
        tags = entry.get("tags") if isinstance(entry.get("tags"), list) else default_tags
        result = acquisition.import_local(
            data, filename, name=str(entry.get("name") or default_name),
            tags=[str(tag) for tag in (tags or [])], source="console_upload")
        result["filename"] = filename
        results.append(result)
    ok_count = sum(1 for item in results if item.get("ok"))
    audit("media_import", {"count": len(results), "ok": ok_count,
                           "duplicates": sum(1 for item in results
                                             if item.get("duplicate"))},
          ok=ok_count > 0, client=client)
    return {"ok": ok_count > 0, "results": results, "count": len(results),
            "imported": sum(1 for item in results
                            if item.get("status") == "imported"),
            "duplicates": sum(1 for item in results if item.get("duplicate")),
            "failed": len(results) - ok_count,
            "note": "本地导入（不联网）；图片会复制进素材库，可作为贴纸（图片形式）发送"}


def media_import_path(body: dict, *, client: str = "") -> dict:
    acquisition = _media_acquisition()
    if acquisition is None:
        return {"ok": False, "error": "media_import_unavailable"}
    path = str(body.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "path 不能为空"}
    tags = body.get("tags") if isinstance(body.get("tags"), list) else []
    result = acquisition.import_local_file(
        path, name=str(body.get("name") or ""),
        tags=[str(tag) for tag in tags], source="console_path")
    audit("media_import_path", {"path": path, "ok": bool(result.get("ok")),
                                "reason": result.get("reason") or ""},
          ok=bool(result.get("ok")), client=client)
    return result


def media_wish_fulfill(body: dict, *, client: str = "") -> dict:
    acquisition = _media_acquisition()
    if acquisition is None:
        return {"ok": False, "error": "media_import_unavailable"}
    wish_id = str(body.get("wish_id") or "").strip()
    if not wish_id:
        return {"ok": False, "error": "wish_id 不能为空"}
    tags = body.get("tags") if isinstance(body.get("tags"), list) else []
    kwargs = {"name": str(body.get("name") or ""),
              "tags": [str(tag) for tag in tags]}
    if body.get("path"):
        result = acquisition.fulfill_wish(wish_id, path=str(body.get("path")), **kwargs)
    elif body.get("data"):
        try:
            data = _decode_media_payload(body.get("data"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"base64_invalid:{exc}"}
        result = acquisition.fulfill_wish(wish_id, data=data, **kwargs)
    else:
        return {"ok": False, "error": "需要 path 或 data"}
    audit("media_wish_fulfill", {"wish_id": wish_id, "ok": bool(result.get("ok")),
                                 "asset": result.get("path") or ""},
          ok=bool(result.get("ok")), client=client)
    return result


# --------------------------------------------------------------------------- #
# P11：B站页（扫码登录 / 配置 / 状态 / 日报）
# --------------------------------------------------------------------------- #
BILI_QR_PNG = DATA_DIR / "selflearn" / "bili_login_qr.png"
_BILI_LAST_KEY = {"key": ""}
_BILI_JAR = http.cookiejar.CookieJar()
_BILI_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_BILI_JAR))
BILI_QR_TTL_S = 180
BILI_HTTP_TIMEOUT = 8.0
BILI_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
BILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BILI_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")

BILI_FIELDS: list[dict] = [
    {"key": "perDay", "label": "每天最多挑几条", "type": "int", "min": 0, "max": 50,
     "note": "填 0 = 不自动刷（还是能手动跑）"},
    {"key": "keywords", "label": "关键词（一行一个；留空=刷首页推荐）", "type": "list"},
    {"key": "peakValley", "label": "峰谷时段自动刷", "type": "bool",
     "note": "只在谷时跑：工作日 12:00–14:00 与 18:00 之后（周末也算谷时）"},
    {"key": "noon", "label": "中午谷时（12:00–14:00）", "type": "bool"},
    {"key": "night", "label": "晚上/夜间谷时（18:00–09:00）", "type": "bool"},
    {"key": "autoComment", "label": "自动评论并 @ 你", "type": "bool"},
    {"key": "transcribe", "label": "本地转写没字幕的视频", "type": "bool"},
    {"key": "asrModel", "label": "ASR 模型", "type": "str", "options": ["tiny", "base", "small", "medium"]},
    {"key": "dmMode", "label": "投递方式", "type": "str",
     "options": ["only", "off", "both"],
     "option_labels": {"only": "只发私信", "off": "只发评论", "both": "私信 + 评论"}},
    {"key": "dmUid", "label": "私信 UID（0=按昵称自动查）", "type": "int", "min": 0, "max": 10 ** 12},
    {"key": "dmWatch", "label": "私信双向（有人私信它就回一句）", "type": "bool"},
    {"key": "dmWatchMinutes", "label": "私信轮询间隔（分钟）", "type": "int", "min": 1, "max": 120},
    {"key": "dmReplyPerDay", "label": "每天最多回几条私信", "type": "int", "min": 0, "max": 100},
    {"key": "dmOwnerUid", "label": "主人 UID（0=自动识别）", "type": "int", "min": 0, "max": 10 ** 12},
    {"key": "commentPerRound", "label": "每轮最多发几条评论", "type": "int", "min": 0, "max": 20},
    {"key": "mentionName", "label": "发给谁 / @ 谁（B站昵称）", "type": "str"},
    {"key": "replyToComments", "label": "回复评论（只回楼中楼）", "type": "bool"},
    {"key": "replyPerRound", "label": "每轮最多回几条", "type": "int", "min": 1, "max": 10},
    {"key": "reportLevel", "label": "日报写多细", "type": "str",
     "options": ["full", "brief", "off"],
     "option_labels": {"full": "完整", "brief": "薄", "off": "不写"}},
    {"key": "personaPrompt", "label": "它按什么说话（人设提示词）", "type": "text"},
    {"key": "workspace", "label": "工作区", "type": "str"},
    {"key": "favFolderId", "label": "自动收藏夹 ID（0=不收藏）", "type": "int", "min": 0,
     "max": 10 ** 12},
    {"key": "pythonPath", "label": "python 路径", "type": "str"},
    {"key": "dshBin", "label": "dsh 路径", "type": "str"},
    {"key": "headlessProfile", "label": "值班 profile", "type": "str"},
    {"key": "overlayMcp", "label": "覆盖 MCP", "type": "bool"},
]
BILI_FIELD_MAP = {field["key"]: field for field in BILI_FIELDS}
BILI_DEFAULTS = {
    "dmWatch": True, "dmWatchMinutes": 1, "dmReplyPerDay": 0, "dmOwnerUid": 0,
    "personaPrompt": "", "perDay": 5, "peakValley": True, "noon": True,
    "night": True, "autoComment": False, "transcribe": False, "asrModel": "small",
    "dmMode": "only", "dmUid": 0, "keywords": [], "commentPerRound": 1,
    "mentionName": "", "pythonPath": r"C:\Python314\python.exe", "dshBin": "",
    "headlessProfile": "bili-duty", "overlayMcp": True, "workspace": "",
    "reportLevel": "full", "replyToComments": False, "replyPerRound": 1,
    "favFolderId": 0,
}
_BILI_NAV_CACHE: dict = {"ts": 0.0, "data": None}


def bili_home() -> Path:
    raw = os.environ.get("BILI_DUTY_HOME") or str(Path.home() / ".dsh" / "bili-duty")
    return Path(raw)


def bili_config_path() -> Path:
    return bili_home() / "config.json"


def bili_state_path() -> Path:
    return bili_home() / "state.json"


def bili_credential_path() -> Path:
    return bili_home() / "bili_credential.json"


def _bili_home_error() -> dict | None:
    if not bili_home().is_dir():
        return {"ok": False, "error": "bili_home_missing",
                "hint": f"插件家目录不存在: {bili_home()}"}
    return None


def _bili_http_json(url: str, *, headers: dict | None = None) -> dict:
    """B站 HTTP GET；测试 monkeypatch 本函数（不真联网）。"""
    request = urllib.request.Request(url)
    request.add_header("User-Agent", BILI_USER_AGENT)
    request.add_header("Referer", "https://www.bilibili.com/")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _BILI_OPENER.open(request, timeout=BILI_HTTP_TIMEOUT) as response:
            raw = response.read().decode("utf-8", "replace") or "{}"
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    return json.loads(raw)


def _bili_request(path_suffix: str, *, headers: dict | None = None) -> dict:
    base = "https://passport.bilibili.com"
    if path_suffix.startswith("http"):
        url = path_suffix
    elif path_suffix.startswith("/x/"):
        url = "https://api.bilibili.com" + path_suffix
    else:
        url = base + path_suffix
    return _bili_http_json(url, headers=headers)


def _bili_read_json(path: Path, default: dict) -> dict:
    payload = read_json(path, default)
    return payload if isinstance(payload, dict) else dict(default)


def _bili_merge_config(raw: dict) -> dict:
    merged = dict(BILI_DEFAULTS)
    for key in BILI_DEFAULTS:
        if key in raw:
            merged[key] = raw[key]
    return merged


def bili_config_state() -> dict:
    raw = _bili_read_json(bili_config_path(), {})
    config = _bili_merge_config(raw)
    home_error = _bili_home_error()
    return {
        "ok": home_error is None,
        "home": str(bili_home()),
        "config": config, "defaults": dict(BILI_DEFAULTS),
        "fields": BILI_FIELDS,
        "unknown_keys": sorted(set(raw) - set(BILI_DEFAULTS)),
        **({"error": home_error["error"], "hint": home_error["hint"]}
           if home_error else {}),
    }


def _bili_validate_updates(updates: dict) -> tuple[dict, list[str]]:
    if not isinstance(updates, dict):
        return {}, ["body 必须是对象"]
    cleaned: dict = {}
    errors: list[str] = []
    for key, value in updates.items():
        field = BILI_FIELD_MAP.get(key)
        if field is None:
            errors.append(f"not_allowed:{key}")
            continue
        try:
            kind = field["type"]
            if kind == "bool":
                if not isinstance(value, bool):
                    raise ValueError("需要布尔值")
                cleaned[key] = value
            elif kind == "int":
                number = int(value)
                low = int(field.get("min", -(10 ** 12)))
                high = int(field.get("max", 10 ** 12))
                if not low <= number <= high:
                    raise ValueError(f"取值 {low}–{high}")
                cleaned[key] = number
            elif kind == "list":
                if isinstance(value, str):
                    value = [item.strip() for item in value.replace("，", ",").splitlines()
                             if item.strip()]
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError("需要字符串数组")
                items = [str(item).strip()[:50] for item in value if str(item).strip()]
                cleaned[key] = items[:100]
            else:
                text = str(value if value is not None else "")
                options = field.get("options")
                if options and text not in options:
                    raise ValueError(f"只能是 {'/'.join(options)}")
                if len(text) > 60000:
                    raise ValueError("超过 60000 字")
                cleaned[key] = text
        except (TypeError, ValueError) as exc:
            errors.append(f"{key}: {exc}")
    return cleaned, errors


def bili_config_update(updates: dict, *, client: str = "") -> dict:
    home_error = _bili_home_error()
    if home_error:
        return home_error
    cleaned, errors = _bili_validate_updates(updates)
    if errors:
        audit("bili_config_update", {"keys": sorted(updates or {})}, ok=False,
              note=";".join(errors)[:200], client=client)
        return {"ok": False, "errors": errors, "config": bili_config_state()["config"]}
    raw = _bili_read_json(bili_config_path(), {})
    before = {key: raw.get(key) for key in cleaned}
    raw.update(cleaned)
    try:
        write_json_atomic(bili_config_path(), raw)
    except OSError as exc:
        audit("bili_config_update", {"keys": sorted(cleaned)}, ok=False,
              note=str(exc), client=client)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    audit("bili_config_update", {"keys": sorted(cleaned), "before": before,
                                 "after": cleaned}, ok=True, client=client)
    state = bili_config_state()
    state["note"] = "已保存；插件下一轮生效（不改 DSH 的 local-override）"
    return state


def _render_qr_png(text: str, target: Path) -> None:
    import qrcode
    image = qrcode.make(text)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    image.save(tmp)
    os.replace(tmp, target)


def bili_login_qr(*, client: str = "") -> dict:
    home_error = _bili_home_error()
    if home_error:
        return home_error
    try:
        payload = _bili_http_json(BILI_GENERATE_URL)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "network", "hint": f"{type(exc).__name__}: {exc}"}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data.get("qrcode_key") or not data.get("url"):
        return {"ok": False, "error": "network",
                "hint": f"二维码接口返回异常: {str(payload)[:200]}"}
    key = str(data["qrcode_key"])
    _BILI_LAST_KEY["key"] = key
    try:
        _render_qr_png(str(data["url"]), Path(BILI_QR_PNG))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "qr_render_failed",
                "hint": f"{type(exc).__name__}: {exc}"}
    audit("bili_login_qr", {"key": key[:8] + "…"}, ok=True, client=client)
    return {"ok": True, "key": key,
            "image": f"/api/bili/login/qr/image?ts={int(time.time())}",
            "expires_in": BILI_QR_TTL_S}


def _bili_login_debug(kind: str, payload: dict) -> None:
    """扫码登录调试日志（不含凭证值），写 agi/data/selflearn/bili_login_debug.log。"""
    try:
        line = json.dumps({"ts": round(time.time(), 3), "kind": kind, **(payload or {})},
                          ensure_ascii=False)
        path = DATA_DIR / "selflearn" / "bili_login_debug.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def _bili_cookie_from_url(data_url: str) -> dict:
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(str(data_url or "")).query)
    lowered = {str(key).lower(): values for key, values in query.items()}
    mapping = {
        "sessdata": "sessdata", "bili_jct": "bili_jct",
        "dedeuserid": "dedeuserid", "buvid3": "buvid3",
    }
    cookies = {}
    for source, target in mapping.items():
        values = lowered.get(source) or query.get(source) or []
        if values:
            cookies[target] = str(values[0])
    return cookies


_BILI_COOKIE_NAMES = {
    "sessdata": "SESSDATA", "bili_jct": "bili_jct",
    "dedeuserid": "DedeUserID", "buvid3": "buvid3",
}


def bili_profile_path() -> Path:
    return DATA_DIR / "selflearn" / "bili_profile.json"


def _bili_profile_save(nav: dict) -> None:
    if not isinstance(nav, dict):
        return
    payload = {"uname": nav.get("uname") or "", "mid": nav.get("mid"),
               "face": nav.get("face") or "", "ts": round(time.time(), 3)}
    try:
        write_json_atomic(bili_profile_path(), payload)
    except Exception:  # noqa: BLE001
        pass


def _bili_profile() -> dict:
    payload = read_json(bili_profile_path(), {})
    return payload if isinstance(payload, dict) else {}


def bili_avatar_bytes() -> tuple[bytes, str] | None:
    """头像代理：用已存凭证取 nav 的 face 再下载图片（缓存到本地）。"""
    profile = _bili_profile()
    url = str(profile.get("face") or "")
    if not url:
        credential = _bili_credential()
        nav = _bili_nav(credential, force=True) if credential else None
        url = str((nav or {}).get("face") or "")
    if not url:
        return None
    mid = str(profile.get("mid") or "self")
    cache = DATA_DIR / "selflearn" / f"bili_avatar_{mid}.img"
    if cache.is_file():
        try:
            return cache.read_bytes(), "image/jpeg"
        except OSError:
            pass
    try:
        request = urllib.request.Request(url)
        request.add_header("User-Agent", BILI_USER_AGENT)
        request.add_header("Referer", "https://www.bilibili.com/")
        with urllib.request.urlopen(request, timeout=BILI_HTTP_TIMEOUT) as response:
            raw = response.read()
        content_type = response.headers.get("Content-Type") or "image/jpeg"
    except Exception:  # noqa: BLE001
        return None
    try:
        cache.write_bytes(raw)
    except OSError:
        pass
    return raw, content_type


def _bili_nav(cookies: dict, *, force: bool = False) -> dict | None:
    if not cookies:
        return None
    cached = _BILI_NAV_CACHE
    if not force and cached["data"] and time.time() - cached["ts"] < 60:
        return cached["data"]
    cookie_header = "; ".join(
        f"{_BILI_COOKIE_NAMES.get(str(key).lower(), key)}={value}"
        for key, value in cookies.items() if value)
    try:
        payload = _bili_http_json(BILI_NAV_URL, headers={"Cookie": cookie_header})
    except Exception:  # noqa: BLE001
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data.get("isLogin"):
        return None
    result = {"uname": str(data.get("uname") or ""), "mid": data.get("mid"),
              "face": str(data.get("face") or "")}
    _BILI_NAV_CACHE.update(ts=time.time(), data=result)
    _bili_profile_save(result)
    return result


def _bili_buvid3() -> str:
    """补 buvid3：B站 finger/spi 接口返回 b_3（多数场景可空，但补上更稳）。"""
    try:
        payload = _bili_http_json("https://api.bilibili.com/x/frontend/finger/spi")
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            return str(data.get("b_3") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _bili_write_credential(cookies: dict) -> dict:
    # 2026 版 B站登录只保证下发 sessdata/bili_jct/DedeUserID；buvid3 可能没有
    required = ("sessdata", "bili_jct", "dedeuserid")
    missing = [key for key in required if not cookies.get(key)]
    if missing:
        return {"ok": False, "error": "credential_incomplete",
                "hint": f"缺少字段: {','.join(missing)}"}
    if not cookies.get("buvid3"):
        cookies["buvid3"] = _bili_buvid3()
    target = bili_credential_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: str(cookies.get(key) or "") for key in ("sessdata", "bili_jct", "buvid3", "dedeuserid")}
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, target)
    except OSError as exc:
        return {"ok": False, "error": "credential_write_failed",
                "hint": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "path": str(target)}


def _bili_credential() -> dict:
    payload = read_json(bili_credential_path(), {})
    if not isinstance(payload, dict):
        return {}
    return {str(key).lower(): str(value) for key, value in payload.items() if value}


def bili_login_poll(key: str, *, client: str = "") -> dict:
    home_error = _bili_home_error()
    if home_error:
        return home_error
    key = str(key or "").strip()
    if not key:
        return {"ok": False, "error": "missing_key", "status": "expired"}
    try:
        payload = _bili_http_json(
            BILI_POLL_URL + "?qrcode_key=" + urllib.parse.quote(key))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "network", "hint": f"{type(exc).__name__}: {exc}"}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"ok": False, "error": "network", "hint": "轮询返回异常"}
    try:
        code = int(data.get("code"))
    except (TypeError, ValueError):
        code = -1
    _bili_login_debug("poll", {"code": code})
    if code == 86101:
        return {"ok": True, "status": "waiting"}
    if code == 86090:
        return {"ok": True, "status": "scanned"}
    if code == 86038:
        return {"ok": True, "status": "expired"}
    if code != 0:
        return {"ok": False, "error": "network", "status": "expired",
                "hint": f"未知状态码: {code}"}
    cookies = _bili_cookie_from_url(str(data.get("url") or ""))
    # 2026 版 B站接口：登录成功后 data.url 是跨域跳转（gourl/ticket），
    # 真正的凭证（SESSDATA/bili_jct/DedeUserID/buvid3）在 Set-Cookie 里。
    # 因此 url 里缺的字段一律从共享 cookie jar 补。
    jar_cookies = {}
    for cookie in _BILI_JAR:
        if cookie.name and cookie.value:
            jar_cookies.setdefault(cookie.name.lower(), cookie.value)
    for field in ("sessdata", "bili_jct", "dedeuserid", "buvid3"):
        if not cookies.get(field) and jar_cookies.get(field):
            cookies[field] = jar_cookies[field]
    _bili_login_debug("poll_ok", {"have": sorted(cookies.keys()),
                                  "jar_keys": sorted(jar_cookies.keys()),
                                  "url_keys": sorted(urllib.parse.parse_qs(
                                      urllib.parse.urlsplit(str(data.get("url") or "")).query).keys())})
    written = _bili_write_credential(cookies)
    if not written.get("ok"):
        audit("bili_login", {"ok": False}, ok=False, client=client)
        return written
    nav = _bili_nav(cookies, force=True) or {}
    audit("bili_login", {"uname": nav.get("uname"), "mid": nav.get("mid")},
          ok=True, client=client)
    return {"ok": True, "status": "ok", "uname": nav.get("uname") or "",
            "mid": nav.get("mid"), "path": written.get("path")}


def bili_status() -> dict:
    home_error = _bili_home_error()
    if home_error:
        return home_error
    config_state = bili_config_state()
    state = _bili_read_json(bili_state_path(), {})
    credential = _bili_credential()
    logged_in = bool(credential)
    nav = _bili_nav(credential, force=True) if logged_in else None
    profile = _bili_profile() if logged_in else {}
    if nav is None and profile:
        nav = {"uname": profile.get("uname") or "", "mid": profile.get("mid"),
               "face": profile.get("face") or ""}
    today = time.strftime("%Y-%m-%d")
    report_path = bili_home() / "out" / f"{today}.md"
    preview = ""
    if report_path.is_file():
        try:
            preview = report_path.read_text(encoding="utf-8", errors="replace")[:1500]
        except OSError:
            preview = ""
    return {
        "ok": True,
        "home": str(bili_home()),
        "logged_in": logged_in,
        "uname": (nav or {}).get("uname") or "",
        "mid": (nav or {}).get("mid"),
        "face": (nav or {}).get("face") or "",
        "credential_path": str(bili_credential_path()),
        "config": config_state["config"],
        "config_path": str(bili_config_path()),
        "state": {
            "day": state.get("day") or "",
            "count": state.get("count") or 0,
            "picked": state.get("picked") or 0,
            "windows": state.get("windows") or {},
            "lastRunAt": state.get("lastRunAt") or "",
            "lastWhy": state.get("lastWhy") or "",
        },
        "report": {"date": today, "path": str(report_path), "preview": preview,
                   "exists": report_path.is_file()},
    }


def bili_report(date: str = "", name: str = "") -> dict:
    home_error = _bili_home_error()
    if home_error:
        return home_error
    if name:
        return _bili_md_read(bili_home() / "reports", name)
    day = str(date or "").strip() or time.strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return {"ok": False, "error": "bad_date", "hint": "date 需要 YYYY-MM-DD"}
    path = bili_home() / "out" / f"{day}.md"
    if not path.is_file():
        return {"ok": False, "error": "no_report", "date": day,
                "path": str(path)}
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": "no_report", "hint": str(exc)}
    limit = 200 * 1024
    truncated = len(content.encode("utf-8")) > limit
    if truncated:
        content = content.encode("utf-8")[:limit].decode("utf-8", "ignore")
    return {"ok": True, "date": day, "path": str(path), "content": content,
            "truncated": truncated, "bytes": len(content.encode("utf-8"))}


# --------------------------------------------------------------------------- #
# P15-B：推理过程（路由接线；实现由 w3-latency 的 tools/console/trace_api.py 提供）
# --------------------------------------------------------------------------- #
def _load_trace_api():
    """加载同目录 trace_api.py；不存在返回 None（页面显示后端未就绪）。"""
    try:
        import importlib.util
        here = Path(__file__).resolve().parent
        target = here / "trace_api.py"
        if target.is_file():
            spec = importlib.util.spec_from_file_location("console_trace_api", target)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
    except Exception:  # noqa: BLE001
        return None
    return None


def _load_growth_api():
    """加载同目录 growth_api.py；不存在返回 None（P32 路由降级为 501）。"""
    try:
        import importlib.util
        here = Path(__file__).resolve().parent
        target = here / "growth_api.py"
        if target.is_file():
            spec = importlib.util.spec_from_file_location("console_growth_api", target)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
    except Exception:  # noqa: BLE001
        return None
    return None


def growth_dispatch(name: str, query: dict) -> dict:
    """把 /api/growth/* 转给 growth_api（只读；实现缺失时返回 501 语义）。"""
    module = _load_growth_api()
    if module is None:
        return {"ok": False, "error": "growth_backend_unavailable",
                "hint": "自我叙事 helper 尚未就绪（tools/console/growth_api.py）"}
    try:
        if name == "narrative":
            return module.growth_narrative()
        config = gateway_config()
        enabled = bool(config.get("narrative_enabled", True))
        return module.growth_status(enabled=enabled)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "growth_api_error",
                "hint": f"{type(exc).__name__}: {exc}"}


def trace_dispatch(path: str, query: dict) -> dict:
    """把 /api/trace/* 转给 trace_api.handle_trace(suffix, query)。

    约定：suffix = "/turns" | "/turn" | "/stream" | "/stats"（path 去掉 /api/trace）；
    query 为 parse_qs 的 dict（值是 list）。若实现方期望完整 path，会在 not_found 时兜底再试一次。
    """
    module = _load_trace_api()
    if module is None:
        return {"ok": False, "error": "trace_backend_unavailable",
                "hint": "推理过程后端尚未就绪（tools/console/trace_api.py 未落地）"}
    handler = getattr(module, "handle_trace", None)
    if not callable(handler):
        return {"ok": False, "error": "trace_backend_unavailable",
                "hint": "trace_api 没有 handle_trace(path, query)"}
    suffix = str(path or "")[len("/api/trace"):] or "/"
    try:
        result = handler(suffix, query)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "trace_api_error",
                "hint": f"{type(exc).__name__}: {exc}"}
    if isinstance(result, dict) and result.get("ok") is False \
            and str(result.get("error") or "").startswith("not_found"):
        try:
            retry = handler(str(path or ""), query)
            if isinstance(retry, dict):
                return retry
        except Exception:  # noqa: BLE001
            pass
    if isinstance(result, dict):
        return result
    if isinstance(result, (list, tuple)):
        return {"ok": True, "items": list(result)}
    return {"ok": True, "data": result}


# --------------------------------------------------------------------------- #
# P18-B：引擎一键启停（代理网关 /api/engine）
# --------------------------------------------------------------------------- #
ENGINE_TIMEOUT_S = 5.0


def _engine_gateway(path: str, *, method: str = "GET") -> dict:
    """代理网关 801 的引擎接口；网关不可达不抛错，返回 ok:false。"""
    base = GATEWAY_STATUS_URL.rsplit("/status", 1)[0]
    data = b"{}" if method == "POST" else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    token = admin_token()
    if token:
        request.add_header("X-Admin-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=ENGINE_TIMEOUT_S) as response:
            raw = response.read().decode("utf-8", "replace") or "{}"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        return {"ok": False, "available": True,
                "error": f"gateway_http_{exc.code}",
                "gateway_status": exc.code, "hint": detail}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "available": False,
                "error": "gateway_unreachable",
                "hint": f"{type(exc).__name__}: {exc}"}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {"ok": False, "available": True, "error": "gateway_bad_json",
                "hint": raw[:200]}
    if not isinstance(payload, dict):
        return {"ok": False, "available": True, "error": "gateway_bad_payload"}
    payload["available"] = True
    return payload


def engine_state() -> dict:
    return _engine_gateway("/api/engine")


def engine_toggle(action: str, *, client: str = "") -> dict:
    action = str(action or "").strip().lower()
    if action not in ("start", "stop"):
        return {"ok": False, "error": "bad_action"}
    result = _engine_gateway(f"/api/engine/{action}", method="POST")
    audit("engine_toggle", {"action": action, "ok": bool(result.get("ok")),
                            "enabled": result.get("enabled"),
                            "error": result.get("error") or ""},
          ok=bool(result.get("ok")), client=client)
    return result


# --------------------------------------------------------------------------- #
# P17：素材库管理（tools/console/media_store.py 的 HTTP 包装）
# --------------------------------------------------------------------------- #
def _load_media_store():
    try:
        import importlib.util
        target = Path(__file__).resolve().parent / "media_store.py"
        if not target.is_file():
            return None
        spec = importlib.util.spec_from_file_location("console_media_store", target)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    except Exception:  # noqa: BLE001
        return None
    return None


def _library_store():
    module = _load_media_store()
    if module is None:
        return None
    return module


# --------------------------------------------------------------------------- #
# P39：发送模式 / 朋友圈 / 模型管理（802 控制台新增）
# --------------------------------------------------------------------------- #
SEND_MODES = ("auto", "confirm", "off")
#: 未显式配置时的默认发送模式：2026-10-30 起与 801 ``config.DEFAULTS["send_mode"]`` 对齐为 confirm。
DEFAULT_SEND_MODE = "confirm"
SEND_MODE_LABELS = {"auto": "自动", "confirm": "草稿", "off": "关闭"}


def _load_console_feature(name: str):
    try:
        import importlib.util
        target = Path(__file__).resolve().parent / f"{name}.py"
        if not target.is_file():
            return None
        spec = importlib.util.spec_from_file_location(f"console_{name}", target)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    except Exception:  # noqa: BLE001
        return None
    return None


def moments_module():
    return _load_console_feature("moments_api")


def model_admin_module():
    return _load_console_feature("model_admin")


def send_mode_state() -> dict:
    raw = gateway_config()
    configured = str(raw.get("send_mode") or "").strip().lower()
    mode = configured or DEFAULT_SEND_MODE
    if mode not in SEND_MODES:
        mode = DEFAULT_SEND_MODE
    contact_modes = raw.get("contact_modes") if isinstance(raw.get("contact_modes"), dict) else {}
    summary = {"auto": 0, "confirm": 0, "off": 0, "other": 0}
    for value in contact_modes.values():
        text = str(value or "").strip().lower()
        summary[text if text in ("auto", "confirm", "off") else "other"] += 1
    return {"ok": True, "mode": mode, "label": SEND_MODE_LABELS[mode],
            "default_mode": DEFAULT_SEND_MODE,
            "default_label": SEND_MODE_LABELS[DEFAULT_SEND_MODE],
            "configured": configured,
            "note": "" if configured else
                    "配置文件未显式设置 send_mode，按默认 confirm（草稿：先落草稿等人工批准）",
            "contact_modes": contact_modes, "contact_summary": summary,
            "path": str(GATEWAY_CONFIG),
            "labels": SEND_MODE_LABELS}


def set_send_mode(mode: str, *, client: str = "") -> dict:
    value = str(mode or "").strip().lower()
    if value not in SEND_MODES:
        audit("send_mode", {"mode": str(mode)[:40], "ok": False}, ok=False,
              note="invalid_mode", client=client)
        return {"ok": False, "error": "invalid_mode", "allowed": list(SEND_MODES)}
    raw = gateway_config()
    before = str(raw.get("send_mode") or "")
    if before == value:
        return {**send_mode_state(), "changed": False, "note": "模式未变化"}
    backup = None
    try:
        if Path(GATEWAY_CONFIG).is_file():
            Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
            suffix = time.strftime("%Y%m%d_%H%M%S")
            backup = Path(BACKUP_DIR) / f"config_before_send_mode_{suffix}.json"
            shutil.copy2(GATEWAY_CONFIG, backup)
        raw["send_mode"] = value
        write_json_atomic(GATEWAY_CONFIG, raw)
    except OSError as exc:
        audit("send_mode", {"mode": value, "before": before, "ok": False}, ok=False,
              note=str(exc), client=client)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    audit("send_mode", {"mode": value, "before": before,
                        "backup": str(backup) if backup else ""}, ok=True, client=client)
    return {**send_mode_state(), "changed": True,
            "backup": str(backup) if backup else "",
            "note": "已保存；网关热加载，下一条消息生效"}


def models_page_payload() -> dict:
    base = models_snapshot()
    module = model_admin_module()
    if module is None:
        base["items"] = []
        base["manage_error"] = "model_admin_unavailable"
        return base
    try:
        managed = module.list_models()
        base["items"] = managed.get("items") or []
        base["counts"] = managed.get("counts") or {}
        base["manage_root"] = managed.get("root")
    except Exception as exc:  # noqa: BLE001
        base["items"] = []
        base["manage_error"] = f"{type(exc).__name__}: {exc}"
    return base


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def host_ok(host: str) -> bool:
    name = str(host or "").strip().rsplit(":", 1)[0].strip("[]").lower()
    return name in ("127.0.0.1", "localhost", "::1") or name.startswith("127.")


def _presented_token(headers) -> str:
    return str(headers.get("X-Admin-Token") or "").strip()


def token_ok(headers) -> bool:
    expected = admin_token()
    presented = _presented_token(headers)
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected, presented)


# --------------------------------------------------------------------------- #
# V1 PR4：802 登录（bot 包里有 secrets/auth.json 才启用；无则保持旧 token 模式）
# --------------------------------------------------------------------------- #
SESSION_TTL_S = 12 * 3600
_SESSIONS: dict[str, float] = {}
_LOGIN_FAILS: dict[str, list[float]] = {}
AUTH_PATH = (Path(_bot_root()) / "secrets" / "auth.json") if _bot_root() else None


def auth_enabled() -> bool:
    return bool(AUTH_PATH and Path(AUTH_PATH).is_file())


def _password_record() -> dict:
    try:
        return json.loads(Path(AUTH_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def verify_password(password: str) -> bool:
    record = _password_record()
    try:
        salt = bytes.fromhex(str(record.get("salt") or ""))
        expected = str(record.get("hash") or "")
        digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, 200_000)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def _login_allowed(client: str) -> bool:
    now = time.time()
    hits = [t for t in _LOGIN_FAILS.get(client, []) if now - t < 60]
    _LOGIN_FAILS[client] = hits
    return len(hits) < 5


def _record_login_fail(client: str) -> None:
    _LOGIN_FAILS.setdefault(client, []).append(time.time())


def new_session() -> str:
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = time.time() + SESSION_TTL_S
    return token


def session_ok(headers) -> bool:
    if not auth_enabled():
        return False
    raw = str(headers.get("Cookie") or "")
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # noqa: BLE001
        return False
    morsel = jar.get("soul_session")
    if morsel is None:
        return False
    expiry = _SESSIONS.get(str(morsel.value))
    if not expiry or expiry < time.time():
        _SESSIONS.pop(str(morsel.value), None)
        return False
    return True


def _session_cookie(token: str, *, clear: bool = False) -> str:
    if clear:
        return "soul_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
    return (f"soul_session={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={SESSION_TTL_S}")


LOGIN_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>登录 · Soul Console</title><style>
body{background:#0f1115;color:#e6e8ee;font:15px/1.6 system-ui,"Microsoft YaHei";display:flex;
min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{background:#171a21;border:1px solid #2a2f3a;border-radius:14px;padding:28px;width:340px}
h1{font-size:18px;margin:0 0 6px}.muted{color:#8b93a7;font-size:13px}
input{width:100%;padding:10px;margin:14px 0;background:#0d1016;color:#e6e8ee;
border:1px solid #2a2f3a;border-radius:8px;box-sizing:border-box}
button{width:100%;padding:10px;background:#4c8dff;color:#fff;border:0;border-radius:8px;
font-weight:600;cursor:pointer}.err{color:#ff5d5d;font-size:13px;min-height:18px}
</style></head><body><div class="card">
<h1>Soul Console</h1><div class="muted">本机单用户 · 输入 802 密码</div>
<form id="f"><input id="pw" type="password" autofocus placeholder="密码">
<button type="submit">登录</button></form><div class="err" id="err"></div></div>
<script>
document.getElementById('f').onsubmit = async (e) => {
  e.preventDefault();
  const r = await fetch('/api/auth/login', {method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:document.getElementById('pw').value})});
  const d = await r.json().catch(()=>({ok:false}));
  if (r.ok && d.ok) location.href = '/'; else document.getElementById('err').textContent =
    (d.error || '密码错误');
};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "SoulConsole/1.0"

    def log_message(self, *args):  # noqa: D401 - 静默访问日志
        return

    # ------------------------------------------------------------ helpers
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json_auth(self, code: int, payload: dict, cookie: str) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _download(self, body: bytes, content_type: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict:
        try:
            return parse_qs(urlsplit(self.path).query)
        except Exception:  # noqa: BLE001
            return {}

    def _limit(self, query: dict, default: int = 100) -> int:
        try:
            return max(1, min(MAX_LIMIT, int((query.get("limit") or [default])[0])))
        except (TypeError, ValueError):
            return default

    def _client(self) -> str:
        try:
            return str(self.client_address[0])
        except Exception:  # noqa: BLE001
            return ""

    def _read_body(self, limit: int = 8 * 1024 * 1024) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > limit:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ValueError("bad json") from exc
        if not isinstance(payload, dict):
            raise ValueError("bad json")
        return payload

    def _guard_read(self) -> bool:
        if host_ok(self.headers.get("Host") or ""):
            return True
        self._json(403, {"ok": False, "error": "host not allowed"})
        return False

    def _guard_write(self) -> bool:
        if not self._guard_read():
            return False
        if token_ok(self.headers):
            return True
        self._json(401, {"ok": False, "error": "admin token required"})
        return False

    def _serve_static(self, name: str) -> bool:
        safe = (STATIC_DIR / name).resolve()
        try:
            safe.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._json(404, {"ok": False, "error": "not found"})
            return True
        if not safe.is_file():
            return False
        content_type = mimetypes.guess_type(str(safe))[0] or "application/octet-stream"
        if safe.suffix in (".html", ".css", ".js"):
            content_type += "; charset=utf-8"
        self._bytes(200, safe.read_bytes(), content_type)
        return True

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:
        if not self._guard_read():
            return
        path = self.path.split("?", 1)[0]
        query = self._query()
        try:
            if path == "/login":
                self._bytes(200, LOGIN_HTML.encode("utf-8"),
                            "text/html; charset=utf-8")
                return
            if path == "/api/auth/state":
                self._json(200, {"ok": True, "auth_enabled": auth_enabled(),
                                 "logged_in": session_ok(self.headers)})
                return
            if auth_enabled() and not (session_ok(self.headers) or token_ok(self.headers)):
                if path.startswith("/api/"):
                    self._json(401, {"ok": False, "error": "login required",
                                     "auth_enabled": True})
                else:
                    self._redirect("/login")
                return
            if path in ("/", "/index.html"):
                if not self._serve_static("index.html"):
                    self._json(500, {"ok": False, "error": "static/index.html 缺失"})
                return
            if path.startswith("/static/"):
                if not self._serve_static(path[len("/static/"):]):
                    self._json(404, {"ok": False, "error": "not found"})
                return
            if path == "/api/health":
                self._json(200, {"ok": True, "service": "soul-console",
                                 "ts": now_ts(), "contact_db": str(contact_db_path() or "")})
                return
            if path == "/api/token":
                # 本机调试便利：仅回环地址可读本机 admin token（免手动抄 config.json）
                client = self._client()
                if client not in ("127.0.0.1", "::1", "localhost"):
                    self._json(403, {"ok": False, "error": "loopback only"})
                    return
                self._json(200, {"ok": True, "token": admin_token()})
                return
            if path == "/api/status":
                self._json(200, gateway_status()); return
            if path == "/api/feed":
                kind = str((query.get("kind") or [""])[0] or "")
                self._json(200, {"ok": True, "items": feed_events(
                    self._limit(query, 200), kind), "source": str(GATEWAY_LOG)}); return
            if path == "/api/inner":
                self._json(200, read_inner(self._limit(query, 100),
                                           str((query.get("contact") or [""])[0] or ""))); return
            if path == "/api/messages":
                contact = str((query.get("contact") or [""])[0] or "")
                self._json(200, read_messages(self._limit(query, 100), contact)); return
            if path == "/api/contacts":
                self._json(200, contacts_overview()); return
            if path == "/api/memory":
                self._json(200, memory_snapshot(
                    str((query.get("contact") or [""])[0] or ""))); return
            # P38：记忆强度/遗忘阶段（只读）
            if path == "/api/memory/strength":
                self._json(200, memory_strength(
                    str((query.get("contact") or [""])[0] or ""))); return
            if path == "/api/diary/list":
                self._json(200, diary_list(self._limit(query, 60))); return
            if path.startswith("/api/diary/"):
                self._json(200, diary_get(unquote(path.split("/api/diary/", 1)[1]))); return
            if path == "/api/tone":
                self._json(200, tone_snapshot(self._limit(query, 100))); return
            if path == "/api/control/send_mode":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                self._json(200, send_mode_state()); return
            if path.startswith("/api/moments"):
                if not token_ok(self.headers):
                    # 图片 <img> 无法带 header：仅 /api/moments/media 允许 ?token=
                    expected = admin_token()
                    presented = str((query.get("token") or [""])[0] or "")
                    if not (path == "/api/moments/media" and expected and presented
                            and hmac.compare_digest(expected, presented)):
                        self._json(401, {"ok": False, "error": "admin token required"}); return
                module = moments_module()
                if module is None:
                    self._json(501, {"ok": False, "error": "moments_backend_unavailable"}); return
                try:
                    if path == "/api/moments":
                        limit = self._limit(query, 50)
                        keyword = str((query.get("keyword") or [""])[0] or "")
                        refresh = str((query.get("refresh") or [""])[0] or "").lower() in ("1", "true", "yes")
                        result = module.fetch_moments(limit, keyword) if refresh else module.load_cache(limit, keyword)
                    elif path == "/api/moments/media":
                        md5 = str((query.get("md5") or [""])[0] or "")
                        found = module.find_media(md5)
                        if not found.get("ok"):
                            self._json(404, found); return
                        target = Path(str(found.get("path") or ""))
                        if not target.is_file():
                            self._json(404, {"ok": False, "error": "media_not_found"}); return
                        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                        self._bytes(200, target.read_bytes(), content_type); return
                    elif path == "/api/moments/analyses":
                        result = module.list_analyses(self._limit(query, 10))
                    elif path.startswith("/api/moments/analysis/"):
                        result = module.get_analysis(unquote(path.split("/api/moments/analysis/", 1)[1]))
                    elif path.startswith("/api/moments/"):
                        result = module.get_moment(unquote(path.split("/api/moments/", 1)[1]))
                    else:
                        result = {"ok": False, "error": "not_found"}
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                self._json(200 if result.get("ok") else (404 if result.get("error") == "not_found" else 400), result); return
            if path == "/api/models/download/status":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                module = model_admin_module()
                if module is None:
                    self._json(501, {"ok": False, "error": "model_admin_unavailable"}); return
                self._json(200, module.download_status()); return
            if path == "/api/models":
                self._json(200, models_page_payload()); return
            # P30：每日语料/训练门禁只读接口
            if path == "/api/train/status":
                self._json(200, train_status()); return
            # P32：自我叙事/成长只读接口（控制台「成长」页用；需 admin token）
            if path in ("/api/growth/narrative", "/api/growth/status"):
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                result = growth_dispatch(
                    "narrative" if path.endswith("/narrative") else "status", query)
                code = 501 if str(result.get("error") or "").startswith(
                    "growth_backend_unavailable") else (
                    200 if result.get("ok", True) else 500)
                self._json(code, result); return
            # C4/C5：成长页到期复测 / wake 候选（只读，鉴权口径与 /api/growth/* 一致）
            if path in ("/api/growth/weak-due", "/api/wake/pending"):
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                contact = str((query.get("contact") or [""])[0] or "")
                if path == "/api/growth/weak-due":
                    self._json(200, weak_due(contact)); return
                self._json(200, wake_pending(self._limit(query, 10), contact)); return
            if path == "/api/train/daily":
                date = str((query.get("date") or [""])[0] or "")
                result = train_daily(date)
                self._json(200 if result.get("ok") else 404, result); return
            if path == "/api/config":
                self._json(200, _config_view()); return
            if path == "/api/media/capabilities":
                self._json(200, media_capabilities()); return
            if path == "/api/media/assets":
                kind = str((query.get("kind") or [""])[0] or "")
                self._json(200, media_assets(self._limit(query, 200), kind)); return
            if path == "/api/media/library":
                self._json(200, media_library(self._limit(query, 100))); return
            if path == "/api/media/acquisition":
                self._json(200, media_acquisition()); return
            if path == "/api/media/stats":
                self._json(200, media_stats()); return
            if path == "/api/media/wishes":
                self._json(200, media_wishes()); return
            if path == "/api/media/file":
                raw_path = str((query.get("path") or [""])[0] or "")
                target = _media_file_ok(raw_path)
                if target is None or not target.is_file():
                    self._json(404, {"ok": False, "error": "file not in media dirs"}); return
                content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                self._bytes(200, target.read_bytes(), content_type); return
            if path == "/api/mood":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                self._json(200, mood_snapshot(
                    str((query.get("contact") or [""])[0] or ""),
                    self._limit(query, 20))); return
            if path == "/api/bili/avatar":
                avatar = bili_avatar_bytes()
                if not avatar:
                    self._json(404, {"ok": False, "error": "no_avatar"}); return
                raw, ctype = avatar
                self._bytes(200, raw, ctype or "image/jpeg"); return
            if path.startswith("/api/library/"):
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                store = _library_store()
                if store is None:
                    self._json(501, {"ok": False, "error": "library_store_unavailable"}); return
                if path == "/api/library/list":
                    result = store.list_assets(
                        str((query.get("type") or [""])[0] or ""),
                        str((query.get("tag") or [""])[0] or ""),
                        str((query.get("q") or [""])[0] or ""),
                        str((query.get("enabled") or [""])[0] or ""),
                        self._limit(query, 100),
                        int((query.get("offset") or ["0"])[0] or 0))
                    self._json(200 if result.get("ok", True) else 400, result); return
                if path == "/api/library/asset":
                    result = store.get_asset(str((query.get("id") or [""])[0] or ""))
                    self._json(200 if result.get("ok") else 404, result); return
                if path == "/api/library/stats":
                    self._json(200, store.stats()); return
                self._json(404, {"ok": False, "error": "not found"}); return
            if path == "/api/engine":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                self._json(200, engine_state()); return
            # P40：待审草稿列表（801 有 token 校验，代理时已透传）
            if path == "/api/drafts":
                self._json(200, drafts_pending()); return
            # B6：对话流「自动缓存聊天视频」入口（当前只有解析能力，如实返回未启用）
            if path == "/api/media/video-cache":
                self._json(200, video_cache_status()); return
            # 发送器状态（顶部「发送器」指示 / 重新检测）：透传 801 /api/sender/status
            if path == "/api/sender/status":
                force = str((query.get("force") or [""])[0] or "").strip().lower() \
                    in ("1", "true", "yes", "on")
                self._json(200, sender_status(force=force)); return
            # 801 内驱力/主动度（只读代理；contact 可选，缺省时 801 取白名单第一个）
            if path == "/api/initiative/status":
                contact = str((query.get("contact") or [""])[0] or "")
                self._json(200, initiative_status(contact)); return
            if path.startswith("/api/trace"):
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                result = trace_dispatch(path, query)
                if result.get("ok", True) is False and result.get(
                        "error") == "trace_backend_unavailable":
                    code = 501
                elif result.get("ok", True) is False:
                    code = 500
                else:
                    code = 200
                self._json(code, result); return
            if path == "/api/bili/login/qr/image":
                png = Path(BILI_QR_PNG)
                if not png.is_file():
                    self._json(404, {"ok": False, "error": "qr_not_generated"}); return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(png.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(png.read_bytes()); return
            if path in ("/api/web/research", "/api/web/records"):
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                self._json(200, web_research(
                    self._limit(query, 100),
                    str((query.get("q") or [""])[0] or ""))); return
            if path in ("/api/bili/feed", "/api/bili/videos"):
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                self._json(200, bili_feed(
                    self._limit(query, 100),
                    str((query.get("filter") or ["all"])[0] or "all"),
                    str((query.get("q") or [""])[0] or ""))); return
            if path == "/api/bili/digest":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                self._json(200, bili_digest()); return
            if path == "/api/bili/digests":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                self._json(200, bili_digests(
                    str((query.get("name") or [""])[0] or ""))); return
            if path == "/api/bili/reports":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                self._json(200, bili_reports_index()); return
            if path == "/api/bili/log":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                try:
                    tail = int((query.get("tail") or ["200"])[0] or 200)
                except (TypeError, ValueError):
                    tail = 200
                self._json(200, bili_duty_log(tail)); return
            if path == "/api/bili/status":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                self._json(200, bili_status()); return
            if path == "/api/bili/config":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                self._json(200, bili_config_state()); return
            if path == "/api/bili/report":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                self._json(200, bili_report(
                    str((query.get("date") or [""])[0] or ""),
                    str((query.get("name") or [""])[0] or ""))); return
            if path == "/api/bili/login/poll":
                # 不强制 token：这是只读状态轮询（成功登录仍需用户真机扫码），
                # 避免旧页面没有 token 时轮询 401、界面卡在"等待扫码"。
                key = str((query.get("key") or [""])[0] or "") or _BILI_LAST_KEY.get("key") or ""
                self._json(200, bili_login_poll(key)); return
            if path == "/api/stats":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                self._json(200, stats_snapshot(
                    self._limit(query, 7),
                    str((query.get("contact") or [""])[0] or ""))); return
            if path == "/api/data/info":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                self._json(200, data_info()); return
            if path == "/api/data/export":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                result = data_export(
                    str((query.get("contact") or [""])[0] or ""),
                    str((query.get("format") or ["jsonl"])[0] or "jsonl"),
                    self._limit(query, 1000))
                if not result.get("ok"):
                    self._json(400, result); return
                self._download(result["body"], result["content_type"],
                               result["filename"]); return
            if path == "/api/data/export-memories":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                result = data_export_memories(
                    str((query.get("contact") or [""])[0] or ""),
                    self._limit(query, 5000))
                if not result.get("ok"):
                    self._json(400, result); return
                self._download(result["body"], result["content_type"],
                               result["filename"]); return
            if path == "/api/data/backup/download":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                backup = data_backup_path(str((query.get("name") or [""])[0] or ""))
                if backup is None:
                    self._json(404, {"ok": False, "error": "backup_not_found"}); return
                self._download(backup.read_bytes(), "application/zip", backup.name); return
            if path == "/api/memory/list":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                result = memory_list(
                    str((query.get("contact") or [""])[0] or ""),
                    str((query.get("q") or [""])[0] or ""),
                    str((query.get("status") or [""])[0] or ""),
                    self._limit(query, 100),
                    int((query.get("offset") or ["0"])[0] or 0))
                self._json(200 if result.get("ok", True) else 400, result); return
            if path == "/api/memory/claims":
                if not token_ok(self.headers):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                self._json(200, memory_claims(
                    str((query.get("contact") or [""])[0] or ""),
                    self._limit(query, 100))); return
            if path == "/api/model-config":
                self._json(200, model_config_state()); return
            if path == "/api/chat-test/history":
                self._json(200, chat_test_history(self._limit(query, 50))); return
            if path == "/api/logs":
                self._json(200, read_logs(
                    str((query.get("source") or ["gateway"])[0] or "gateway"),
                    self._limit(query, 200),
                    str((query.get("q") or [""])[0] or ""),
                    str((query.get("kind") or [""])[0] or ""))); return
            if path == "/api/prompts":
                self._json(200, prompts_state()); return
        except Exception as exc:  # noqa: BLE001 - 页面要有错误态而不是空白
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(404, {"ok": False, "error": "not found"})

    # ------------------------------------------------------------ POST
    def do_POST(self) -> None:
        if not self._guard_read():
            return
        path = self.path.split("?", 1)[0]
        client = self._client()
        if path == "/api/auth/login":
            try:
                body = self._read_body()
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)}); return
            if not auth_enabled():
                self._json(400, {"ok": False, "error": "auth_disabled"}); return
            if not _login_allowed(client):
                self._json(429, {"ok": False, "error": "too many attempts"}); return
            if verify_password(str(body.get("password") or "")):
                token = new_session()
                audit("login", {"ok": True}, ok=True, client=client)
                self._json_auth(200, {"ok": True}, _session_cookie(token)); return
            _record_login_fail(client)
            audit("login", {"ok": False}, ok=False, client=client)
            self._json(401, {"ok": False, "error": "password incorrect"}); return
        if path == "/api/auth/logout":
            raw = str(self.headers.get("Cookie") or "")
            jar = SimpleCookie()
            try:
                jar.load(raw)
                morsel = jar.get("soul_session")
                if morsel is not None:
                    _SESSIONS.pop(str(morsel.value), None)
            except Exception:  # noqa: BLE001
                pass
            self._json_auth(200, {"ok": True}, _session_cookie("", clear=True)); return
        if auth_enabled() and not (session_ok(self.headers) or token_ok(self.headers)):
            self._json(401, {"ok": False, "error": "login required",
                             "auth_enabled": True})
            return
        if path == "/api/factory/preview":
            # 只读统计：不需要 token（But 仍需回环 Host；不删除任何东西）
            try:
                self._json(200, factory_preview())
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if not token_ok(self.headers):
            self._json(401, {"ok": False, "error": "admin token required"})
            return
        try:
            body = self._read_body()
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)}); return
        try:
            if path in ("/api/proactive/stop", "/api/proactive/resume"):
                result = set_stop(path.endswith("/stop"), client=client)
                self._json(200 if result.get("ok") else 500, result); return
            if path == "/api/config":
                updates = body.get("updates", body)
                result = apply_config(updates if isinstance(updates, dict) else {},
                                      client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/control/send_mode":
                result = set_send_mode(str(body.get("mode") or ""), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            # A4：草稿批准 / 驳回（转发 801，失败把具体 error 原样回给前端）
            if path == "/api/contacts/refresh":
                self._json(200, refresh_contacts(client=client)); return
            if path in ("/api/draft/approve", "/api/draft/reject"):
                result = draft_decide(str(body.get("id") or ""),
                                      path.endswith("approve"), client=client)
                code = int(result.get("http") or 0) or (200 if result.get("ok") else 400)
                self._json(code, result); return
            if path == "/api/moments/refresh":
                module = moments_module()
                if module is None:
                    self._json(501, {"ok": False, "error": "moments_backend_unavailable"}); return
                result = module.fetch_moments(int(body.get("limit") or 50))
                audit("moments_refresh", {"fetched": result.get("fetched"), "added": result.get("added"),
                                          "ok": bool(result.get("ok"))},
                      ok=bool(result.get("ok")), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/moments/analyze":
                module = moments_module()
                if module is None:
                    self._json(501, {"ok": False, "error": "moments_backend_unavailable"}); return
                result = module.analyze(int(body.get("limit") or 30),
                                        str(body.get("question") or ""), client=client)
                audit("moments_analyze", {"ok": bool(result.get("ok")),
                                          "name": result.get("name")},
                      ok=bool(result.get("ok")), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/models/import":
                module = model_admin_module()
                if module is None:
                    self._json(501, {"ok": False, "error": "model_admin_unavailable"}); return
                result = module.import_model(str(body.get("path") or ""),
                                             str(body.get("name") or ""),
                                             str(body.get("format") or "auto"))
                audit("model_import", {"path": body.get("path"), "name": body.get("name"),
                                       "ok": bool(result.get("ok"))},
                      ok=bool(result.get("ok")), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/models/delete":
                module = model_admin_module()
                if module is None:
                    self._json(501, {"ok": False, "error": "model_admin_unavailable"}); return
                result = module.delete_model(str(body.get("path") or ""))
                audit("model_delete", {"path": body.get("path"), "ok": bool(result.get("ok")),
                                       "moved_to": result.get("moved_to")},
                      ok=bool(result.get("ok")), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/models/download":
                module = model_admin_module()
                if module is None:
                    self._json(501, {"ok": False, "error": "model_admin_unavailable"}); return
                result = module.download_model(str(body.get("url") or ""),
                                               str(body.get("name") or ""))
                audit("model_download", {"url": body.get("url"), "name": body.get("name"),
                                         "ok": bool(result.get("ok"))},
                      ok=bool(result.get("ok")), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/models/start":
                result = start_model(str(body.get("name") or ""), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/models/stop":
                result = stop_model(str(body.get("name") or ""), client=client,
                                    force_external=bool(body.get("force_external")))
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/prompts":
                result = save_prompts(body.get("prompts") or {}, client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path in ("/api/media/send", "/api/media/test-send"):
                result = media_send(body, client=client)
                if result.get("ok"):
                    code = 200
                elif result.get("status") == "unsupported":
                    code = 501
                elif result.get("status") in ("gateway_error", "gateway_unavailable"):
                    code = 502
                else:
                    code = 400
                self._json(code, result); return
            if path == "/api/data/backup":
                result = data_backup(client=client)
                self._json(200 if result.get("ok") else 500, result); return
            if path == "/api/data/backup/delete":
                result = data_backup_delete(
                    str(body.get("name") or ""), str(body.get("confirm") or ""),
                    client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/data/import":
                result = data_import(
                    str(body.get("zip_path") or body.get("name") or ""),
                    str(body.get("confirm") or ""),
                    str(body.get("mode") or "merge"),
                    overwrite_confirm=str(body.get("overwrite_confirm") or ""),
                    client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/library/create":
                store = _library_store()
                if store is None:
                    self._json(501, {"ok": False, "error": "library_store_unavailable"}); return
                data = None
                if body.get("data"):
                    try:
                        data = _decode_media_payload(body.get("data"))
                    except Exception as exc:  # noqa: BLE001
                        self._json(400, {"ok": False, "error": f"base64_invalid:{exc}"}); return
                result = store.create_asset(
                    data=data, file_path=str(body.get("path") or "") or None,
                    type_=str(body.get("type") or "sticker"),
                    name=str(body.get("name") or ""),
                    caption=str(body.get("caption") or ""),
                    tags=body.get("tags") if isinstance(body.get("tags"), list) else [])
                audit("media_asset_create", {"ok": bool(result.get("ok")),
                                             "duplicate": bool(result.get("duplicate")),
                                             "name": body.get("name")},
                      ok=bool(result.get("ok")), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/library/update":
                store = _library_store()
                if store is None:
                    self._json(501, {"ok": False, "error": "library_store_unavailable"}); return
                raw = body.get("updates", body)
                fields = {key: value for key, value in raw.items()
                          if key not in ("id", "contact")}
                result = store.update_asset(str(body.get("id") or ""), fields)
                audit("media_asset_update", {"id": body.get("id"), "fields": sorted(fields),
                                             "ok": bool(result.get("ok"))},
                      ok=bool(result.get("ok")), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/library/delete":
                store = _library_store()
                if store is None:
                    self._json(501, {"ok": False, "error": "library_store_unavailable"}); return
                result = store.delete_asset(
                    str(body.get("id") or ""), hard=bool(body.get("hard", False)),
                    confirm=str(body.get("confirm") or ""))
                audit("media_asset_delete", {"id": body.get("id"),
                                             "hard": bool(body.get("hard", False)),
                                             "ok": bool(result.get("ok"))},
                      ok=bool(result.get("ok")), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/library/restore":
                store = _library_store()
                if store is None:
                    self._json(501, {"ok": False, "error": "library_store_unavailable"}); return
                result = store.restore_asset(str(body.get("id") or ""))
                audit("media_asset_restore", {"id": body.get("id"),
                                              "ok": bool(result.get("ok"))},
                      ok=bool(result.get("ok")), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path in ("/api/engine/start", "/api/engine/stop"):
                result = engine_toggle(path.rsplit("/", 1)[-1], client=client)
                self._json(200, result); return
            if path == "/api/bili/login/qr":
                result = bili_login_qr(client=client)
                self._json(200 if result.get("ok") else 502, result); return
            if path == "/api/bili/config":
                updates = body.get("updates", body)
                result = bili_config_update(
                    updates if isinstance(updates, dict) else {}, client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/mood/inject":
                if "inject" not in body:
                    self._json(400, {"ok": False, "error": "缺少 inject"}); return
                result = mood_update({"inject": body.get("inject")}, client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/mood/enabled":
                value = body.get("judge_enabled", body.get("enabled"))
                if value is None:
                    self._json(400, {"ok": False, "error": "缺少 enabled/judge_enabled"}); return
                result = mood_update({"judge_enabled": value}, client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/memory/update":
                result = memory_update(
                    str(body.get("contact") or ""), str(body.get("id") or ""),
                    content=body.get("content") if "content" in body else None,
                    importance=body.get("importance") if "importance" in body else None,
                    status=body.get("status") if "status" in body else None,
                    client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/contacts/create":
                result = create_contact_db(str(body.get("alias") or ""), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/chat/clear":
                result = chat_clear_all(
                    str(body.get("contact") or ""), str(body.get("confirm") or ""),
                    client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/memory/clear":
                result = memory_clear_all(
                    str(body.get("contact") or ""), str(body.get("confirm") or ""),
                    client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/memory/delete":
                result = memory_delete(
                    str(body.get("contact") or ""), str(body.get("id") or ""),
                    str(body.get("confirm") or ""), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/memory/mark":
                result = memory_mark(
                    str(body.get("contact") or ""), str(body.get("id") or ""),
                    bool(body.get("important", True)), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/memory/merge":
                result = memory_merge(
                    str(body.get("contact") or ""), body.get("ids") or [],
                    client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/model-config":
                updates = body.get("updates", body)
                result = apply_model_config(updates if isinstance(updates, dict) else {},
                                            client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/model-config/deepseek-key":
                result = save_deepseek_key(str(body.get("api_key") or ""),
                                           clear=bool(body.get("clear", False)),
                                           client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/chat-test":
                result = chat_test(str(body.get("text") or ""),
                                   reset=bool(body.get("reset", False)),
                                   client=client)
                if result.get("ok"):
                    code = 200
                elif result.get("status") == "timeout":
                    code = 504
                elif result.get("status") == "invalid":
                    code = 400
                else:
                    code = 500
                self._json(code, result); return
            if path == "/api/manual/send":
                result = manual_send(str(body.get("contact") or ""),
                                     str(body.get("text") or ""), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/media/import":
                self._json(200, media_import(body, client=client)); return
            if path == "/api/media/import-path":
                result = media_import_path(body, client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/media/wish/fulfill":
                result = media_wish_fulfill(body, client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/prompts/reset":
                raw_keys = body.get("keys") or []
                if isinstance(raw_keys, str):
                    raw_keys = [raw_keys]
                result = reset_prompts([str(k) for k in raw_keys] if isinstance(raw_keys, list) else [],
                                       all=bool(body.get("all", False)), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/factory/reset":
                result = factory_reset(str(body.get("confirm") or ""),
                                       backup=bool(body.get("backup", True)),
                                       client=client)
                self._json(200 if result.get("ok") else 400, result); return
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(404, {"ok": False, "error": "not found"})


def make_server(host: str = HOST, port: int = PORT,
                contact_db: Path | str | None = None) -> ThreadingHTTPServer:
    global DEFAULT_CONTACT_DB, HOST, PORT
    HOST, PORT = host, int(port)
    if contact_db:
        DEFAULT_CONTACT_DB = Path(contact_db)
    server = ThreadingHTTPServer((host, int(port)), Handler)
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/console/server.py")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--contact-db", default=None, help="联系人 sqlite（默认自动挑最近有新消息的）")
    args = parser.parse_args(argv)
    server = make_server(args.host, args.port, args.contact_db)
    print(f"Soul Console: http://{args.host}:{args.port}/  (Ctrl+C 停止)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
