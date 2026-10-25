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
import hmac
import json
import mimetypes
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:            # 以脚本方式启动时也能 import agi.*
    sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "agi" / "data"
GATEWAY_DIR = DATA_DIR / "wechat"
GATEWAY_LOG = GATEWAY_DIR / "gateway.log"
GATEWAY_CONFIG = GATEWAY_DIR / "config.json"
STOP_PATH = GATEWAY_DIR / "STOP"
GATEWAY_STATUS_URL = "http://127.0.0.1:801/status"
CONTACTS_DIR = DATA_DIR / "selflearn" / "wechat_contacts"
PROMPTS_PATH = DATA_DIR / "prompts.json"
AUDIT_PATH = DATA_DIR / "console_audit.log"
TONE_PROFILE = ROOT / "agi" / "eval_data" / "human_tone" / "profile.md"
STATIC_DIR = Path(__file__).resolve().parent / "static"

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
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "gateway_reachable": False}


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


def contact_db_path() -> Path | None:
    if DEFAULT_CONTACT_DB:
        return Path(DEFAULT_CONTACT_DB)
    try:
        candidates = sorted(Path(CONTACTS_DIR).glob("*.sqlite3"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
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


def read_inner(limit: int = 100) -> dict:
    path = contact_db_path()
    if not path:
        return {"ok": False, "error": "contact_db_not_found", "items": []}
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


def read_messages(limit: int = 100) -> dict:
    path = contact_db_path()
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
                                        "archived_at") if name in cols]
            rows = con.execute(
                f"SELECT {','.join(wanted)} FROM messages "
                "ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "items": []}
    return {"ok": True, "db": str(path),
            "items": [dict(row) for row in reversed(rows)]}


def memory_snapshot() -> dict:
    path = contact_db_path()
    if not path:
        return {"ok": False, "error": "contact_db_not_found", "metrics": {},
                "recent": []}
    metrics: dict = {}
    recent: list[dict] = []
    try:
        from agi.selflearn import memory as memory_mod
        metrics = memory_mod.metrics(path)
    except Exception as exc:  # noqa: BLE001 - 指标失败不影响页面
        metrics = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        con = _ro_connect(path)
        try:
            cols = _columns(con, "user_memories")
            if cols:
                wanted = [name for name in
                          ("id", "kind", "content", "importance", "status",
                           "created_at", "access_count") if name in cols]
                recent = [dict(row) for row in con.execute(
                    f"SELECT {','.join(wanted)} FROM user_memories "
                    "ORDER BY created_at DESC LIMIT 50").fetchall()]
        finally:
            con.close()
    except sqlite3.Error:
        recent = []
    return {"ok": True, "db": str(path), "metrics": metrics, "recent": recent}


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
def models_snapshot() -> dict:
    try:
        from agi.selflearn import model_registry
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "models": []}
    try:
        models = model_registry.list_models()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "models": []}
    items = []
    for model in models:
        name = str(model.get("name") or "")
        try:
            healthy = bool(model_registry.health(name))
        except Exception:  # noqa: BLE001
            healthy = False
        items.append({
            "name": name, "port": model.get("port"), "role": model.get("role"),
            "status": model.get("status"), "purpose": model.get("purpose"),
            "healthy": healthy, "editable": name in MODEL_ALLOWLIST,
            "reserved": str(model.get("role")) == "reserved"
                        or str(model.get("status")) == "reserved",
            "gguf": model.get("gguf"),
        })
    return {"ok": True, "models": items,
            "allow_start": sorted(MODEL_ALLOWLIST)}


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


def prompts_state() -> dict:
    payload = read_json(PROMPTS_PATH, {})
    prompts_raw = payload.get("prompts") if isinstance(payload, dict) else {}
    prompts = {str(k): str(v) for k, v in (prompts_raw or {}).items()
               if isinstance(k, str)} if isinstance(prompts_raw, dict) else {}
    builtin = {}
    try:
        from agi.selflearn.chat import SYSTEM
        builtin["chat_system"] = SYSTEM
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "version": int(payload.get("version") or 1)
            if isinstance(payload, dict) else 1,
            "prompts": prompts, "builtin": builtin, "path": str(PROMPTS_PATH),
            "note": "w2-inner 的 prompt_store 接入后由它接管；当前直接读写 prompts.json，"
                    "保存后下一条消息生效"}


_PROMPT_KEY = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")


def save_prompts(prompts: dict, *, client: str = "") -> dict:
    if not isinstance(prompts, dict) or not prompts:
        return {"ok": False, "error": "prompts 必须是非空对象"}
    cleaned: dict[str, str] = {}
    for key, value in prompts.items():
        if not isinstance(key, str) or not _PROMPT_KEY.match(key):
            return {"ok": False, "error": f"非法 key: {key!r}"}
        text = str(value or "")
        if len(text) > 20000:
            return {"ok": False, "error": f"{key} 超过 20000 字"}
        cleaned[key] = text
    payload = read_json(PROMPTS_PATH, {})
    current = payload.get("prompts") if isinstance(payload, dict) else {}
    merged = dict(current) if isinstance(current, dict) else {}
    merged.update(cleaned)
    try:
        write_json_atomic(PROMPTS_PATH, {"version": 1, "prompts": merged})
    except OSError as exc:
        audit("prompt_save", {"keys": sorted(cleaned)}, ok=False, note=str(exc),
              client=client)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    audit("prompt_save", {"keys": sorted(cleaned)}, ok=True, client=client)
    return {"ok": True, "prompts": merged, "note": "已保存；下一条消息生效"}


def start_model(name: str, *, client: str = "") -> dict:
    name = str(name or "").strip()
    if name not in MODEL_ALLOWLIST:
        audit("model_start", {"name": name}, ok=False, note="not_allowlisted",
              client=client)
        return {"ok": False, "error": "model_not_editable",
                "hint": f"只允许启停 {sorted(MODEL_ALLOWLIST)}；reserved/研究模型不可动"}
    try:
        from agi.selflearn import model_registry
        result = model_registry.start(name)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    audit("model_start", {"name": name, "result": result}, ok=bool(result.get("ok")),
          client=client)
    return result


def stop_model(name: str, *, client: str = "") -> dict:
    name = str(name or "").strip()
    if name not in MODEL_ALLOWLIST:
        audit("model_stop", {"name": name}, ok=False, note="not_allowlisted",
              client=client)
        return {"ok": False, "error": "model_not_editable",
                "hint": f"只允许启停 {sorted(MODEL_ALLOWLIST)}"}
    try:
        from agi.selflearn import model_registry
        result = model_registry.stop(name)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    audit("model_stop", {"name": name, "result": result}, ok=bool(result.get("ok")),
          client=client)
    return result


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

    def _bytes(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
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

    def _read_body(self, limit: int = 262144) -> dict:
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
            if path == "/api/status":
                self._json(200, gateway_status()); return
            if path == "/api/feed":
                kind = str((query.get("kind") or [""])[0] or "")
                self._json(200, {"ok": True, "items": feed_events(
                    self._limit(query, 200), kind), "source": str(GATEWAY_LOG)}); return
            if path == "/api/inner":
                self._json(200, read_inner(self._limit(query, 100))); return
            if path == "/api/messages":
                self._json(200, read_messages(self._limit(query, 100))); return
            if path == "/api/memory":
                self._json(200, memory_snapshot()); return
            if path == "/api/tone":
                self._json(200, tone_snapshot(self._limit(query, 100))); return
            if path == "/api/models":
                self._json(200, models_snapshot()); return
            if path == "/api/config":
                self._json(200, _config_view()); return
            if path == "/api/prompts":
                self._json(200, prompts_state()); return
        except Exception as exc:  # noqa: BLE001 - 页面要有错误态而不是空白
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(404, {"ok": False, "error": "not found"})

    # ------------------------------------------------------------ POST
    def do_POST(self) -> None:
        if not self._guard_write():
            return
        path = self.path.split("?", 1)[0]
        client = self._client()
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
            if path == "/api/models/start":
                result = start_model(str(body.get("name") or ""), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/models/stop":
                result = stop_model(str(body.get("name") or ""), client=client)
                self._json(200 if result.get("ok") else 400, result); return
            if path == "/api/prompts":
                result = save_prompts(body.get("prompts") or {}, client=client)
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
