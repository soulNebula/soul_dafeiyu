# -*- coding: utf-8 -*-
"""M2-A ChatStudio 后端：人格/提示词编辑（备份+审计）、流水线开关白名单、
护栏规则只读、试跑（临时库 + dry run，绝不发微信）、trace 查看。

只提供纯函数，路由由 Lead 接 main.py；不改 main.py/web。
SoulAgent 的模块通过 ``_soul_module`` 懒加载，便于测试注入/隔离。
"""
from __future__ import annotations

import importlib
import json
import os
import random
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import audit, paths

PERSONA_FILES = ("values.md", "profile.json", "fewshot.json")
PROMPT_DEFAULTS_KEYS = (
    "chat.system",
    "proactive.persona_constraints",
    "proactive.anti_fabrication",
    "proactive.continuity",
    "proactive.voice_fewshot",
)
PIPELINE_SWITCHES: dict[str, dict] = {
    "inject_time_context": {"type": "bool", "default": True, "label": "时间上下文"},
    "slang_guard_enabled": {"type": "bool", "default": True, "label": "网感腔护栏"},
    "canned_guard_enabled": {"type": "bool", "default": True, "label": "金句腔护栏"},
    "media_enabled": {"type": "bool", "default": False, "label": "媒体生成"},
    "media_quota_enabled": {"type": "bool", "default": True, "label": "媒体配额"},
    "mood_judge_enabled": {"type": "bool", "default": False, "label": "每轮心情判定"},
    "web_lookup_enabled": {"type": "bool", "default": True, "label": "联网实时信息"},
    "trace_enabled": {"type": "bool", "default": True, "label": "trace 采集"},
    "inner_mode": {"type": "choice", "default": "off", "choices": ["off", "static", "rule", "model"], "label": "内状态模式"},
    "inner_shadow_only": {"type": "bool", "default": True, "label": "内状态只观察（shadow）"},
    "proactive_style": {"type": "choice", "default": "legacy", "choices": ["legacy", "simple"], "label": "主动消息生成链"},
}
MAX_PROMPT_CHARS = 64 * 1024
MAX_PERSONA_CHARS = 200_000


# ------------------------------------------------------------------ 路径
def _soul_root() -> Path:
    return Path(paths.soulagent_home()).resolve()


def _prompt_path() -> Path:
    env = os.environ.get("PROMPTS_PATH")
    return Path(env).resolve() if env else _soul_root() / "agi" / "data" / "prompts.json"


def _wechat_config_path() -> Path:
    return _soul_root() / "agi" / "data" / "wechat" / "config.json"


def _persona_root() -> Path:
    return _soul_root() / "agi" / "persona"


def _backup_dir(subdir: str = "chat_backups") -> Path:
    target = Path(paths.DATA_DIR) / subdir
    target.mkdir(parents=True, exist_ok=True)
    return target


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _backup(path: Path, *, label: str, subdir: str = "chat_backups") -> Path | None:
    if not path.is_file():
        return None
    target = _backup_dir(subdir) / f"{path.name}.{time.strftime('%Y%m%d_%H%M%S')}.{uuid.uuid4().hex[:6]}.bak"
    try:
        shutil.copy2(path, target)
        return target
    except OSError:
        return None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex[:6]}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _soul_module(name: str):
    root = str(_soul_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module(name)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


# ------------------------------------------------------------------ 提示词
def prompts_view() -> dict:
    path = _prompt_path()
    raw = _read_json(path, {})
    overrides = raw.get("prompts") if isinstance(raw, dict) else {}
    if not isinstance(overrides, dict):
        overrides = {}
    defaults: dict[str, str] = {}
    try:
        store = _soul_module("agi.selflearn.prompt_store")
        defaults = dict(store.defaults() or {})
    except Exception:  # noqa: BLE001 - 只读展示，导入失败用空默认
        pass
    effective = {key: str(overrides.get(key, defaults.get(key, "")))
                 for key in set(defaults) | set(overrides)}
    return {"ok": True, "path": str(path), "defaults": defaults,
            "overrides": overrides, "effective": effective,
            "allowed_keys": sorted(PROMPT_DEFAULTS_KEYS + tuple(defaults))}


def update_prompts(changes: dict, *, actor: str = "workbench") -> dict:
    if not isinstance(changes, dict) or not changes:
        return {"ok": False, "error": "changes_required"}
    path = _prompt_path()
    before = _read_json(path, {"version": 1, "prompts": {}})
    overrides = dict(before.get("prompts") or {}) if isinstance(before, dict) else {}
    allowed = set(PROMPT_DEFAULTS_KEYS)
    try:
        store = _soul_module("agi.selflearn.prompt_store")
        allowed |= set(store.KNOWN_KEYS)
    except Exception:  # noqa: BLE001
        pass
    for key, value in changes.items():
        if key not in allowed:
            return {"ok": False, "error": f"unknown_prompt_key:{key}"}
        text = str(value or "")
        if len(text) > MAX_PROMPT_CHARS:
            return {"ok": False, "error": f"prompt_too_long:{key}"}
        if text.strip():
            overrides[key] = text
        else:
            overrides.pop(key, None)
    payload = {"version": 1, "prompts": overrides, "updated_at": round(time.time(), 3)}
    encoded = json.dumps(payload, ensure_ascii=False, indent=1)
    if len(encoded.encode("utf-8")) > 64 * 1024:
        return {"ok": False, "error": "prompts_file_too_large"}
    backup = _backup(path, label="prompts")
    _atomic_write(path, encoded)
    audit.record("chat.prompts_update", str(path), actor=actor,
                 before=before, after=payload,
                 meta={"keys": sorted(changes), "backup": str(backup) if backup else ""})
    return {"ok": True, "path": str(path), "backup": str(backup) if backup else "",
            "restart_required": True,
            "hint": "需重启网关/重新加载提示词后生效",
            "view": prompts_view()}


def update_prompt(key: str, text: str) -> dict:
    """单键版本（路由 /prompt {key,text} 用）。"""
    return update_prompts({str(key): text})


# ------------------------------------------------------------------ 人格文件
def _persona_db_path() -> Path | None:
    base = _soul_root() / "agi" / "data" / "selflearn" / "wechat_contacts"
    if base.is_dir():
        candidates = sorted(base.glob("*.memory.sqlite3"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            return candidates[0]
    fallback = _soul_root() / "agi" / "data" / "selflearn" / "selflearn.memory.sqlite3"
    return fallback if fallback.is_file() else None


def persona_view() -> dict:
    root = _persona_root()
    items = []
    if root.is_dir():
        for directory in sorted(root.iterdir()):
            if not directory.is_dir():
                continue
            files = {}
            for name in PERSONA_FILES:
                path = directory / name
                if not path.is_file():
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                files[name] = {"path": str(path), "text": text, "size": len(text)}
            if files:
                items.append({"name": directory.name, "path": str(directory),
                              "files": files})
    core = {}
    try:
        narrative = _soul_module("agi.selflearn.self_narrative")
        db_path = _persona_db_path()
        if db_path:
            core = narrative.current(db_path=db_path)
    except Exception:  # noqa: BLE001 - 只读展示
        core = {}
    return {"ok": True, "items": items, "core": core,
            "allowed_files": list(PERSONA_FILES)}


def update_persona(persona: str, filename: str, content: str,
                   *, actor: str = "workbench") -> dict:
    if filename not in PERSONA_FILES:
        return {"ok": False, "error": "persona_file_not_allowed"}
    if not str(persona or "").strip() or "/" in persona or "\\" in persona or ".." in persona:
        return {"ok": False, "error": "invalid_persona"}
    path = (_persona_root() / persona / filename)
    if not _under(path, _soul_root()):
        return {"ok": False, "error": "path_out_of_allowed_roots"}
    text = str(content if content is not None else "")
    if len(text) > MAX_PERSONA_CHARS:
        return {"ok": False, "error": "persona_too_large"}
    if filename.endswith(".json"):
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"invalid_json:{exc}"}
    before = path.read_text(encoding="utf-8") if path.is_file() else ""
    backup = _backup(path, label="persona", subdir="persona_backups")
    _atomic_write(path, text)
    audit.record("chat.persona_update", str(path), actor=actor,
                 before=before, after=text,
                 meta={"persona": persona, "file": filename,
                       "backup": str(backup) if backup else ""})
    return {"ok": True, "path": str(path), "backup": str(backup) if backup else "",
            "persona": persona, "file": filename, "chars": len(text)}


# ------------------------------------------------------------------ 流水线开关
def switches_view() -> dict:
    raw = _read_json(_wechat_config_path(), {})
    raw = raw if isinstance(raw, dict) else {}
    items = {}
    for key, meta in PIPELINE_SWITCHES.items():
        value = raw.get(key, meta["default"])
        items[key] = {**meta, "value": value, "overridden": key in raw}
    return {"ok": True, "path": str(_wechat_config_path()), "items": items,
            "allowed_keys": sorted(PIPELINE_SWITCHES)}


def pipeline_view() -> dict:
    view = switches_view()
    items = view.get("items", {})
    stages = [
        {"key": "time", "name": "时间上下文", "switch": "inject_time_context"},
        {"key": "context", "name": "上下文/记忆", "switch": None},
        {"key": "memory", "name": "长期记忆", "switch": None},
        {"key": "mood", "name": "心情判定", "switch": "mood_judge_enabled"},
        {"key": "inner", "name": "内状态", "switch": "inner_mode"},
        {"key": "guard", "name": "护栏", "switch": "slang_guard_enabled"},
        {"key": "media", "name": "媒体生成", "switch": "media_enabled"},
        {"key": "media_choice", "name": "媒体选择/配额", "switch": "media_quota_enabled"},
        {"key": "voice_polish", "name": "口语化润色", "switch": None},
        {"key": "trace", "name": "trace 采集", "switch": "trace_enabled"},
    ]
    for stage in stages:
        key = stage["switch"]
        stage["enabled"] = items.get(key, {}).get("value") if key else None
        stage["value"] = stage["enabled"]
    return {"ok": True, "path": view.get("path"), "stages": stages,
            "switches": items, "allowed_keys": view.get("allowed_keys", [])}


def update_pipeline_switch(key: str, value) -> dict:
    return update_switches({str(key): value})


def update_switches(changes: dict, *, actor: str = "workbench") -> dict:
    if not isinstance(changes, dict) or not changes:
        return {"ok": False, "error": "changes_required"}
    path = _wechat_config_path()
    before = _read_json(path, {})
    if not isinstance(before, dict):
        before = {}
    updated = dict(before)
    for key, value in changes.items():
        meta = PIPELINE_SWITCHES.get(key)
        if not meta:
            return {"ok": False, "error": f"switch_not_allowed:{key}"}
        if meta["type"] == "bool":
            if not isinstance(value, bool):
                return {"ok": False, "error": f"switch_not_bool:{key}"}
            updated[key] = value
        else:
            if value not in meta["choices"]:
                return {"ok": False, "error": f"switch_not_choice:{key}"}
            updated[key] = value
    backup = _backup(path, label="wechat-config")
    _atomic_write(path, json.dumps(updated, ensure_ascii=False, indent=2))
    audit.record("chat.switches_update", str(path), actor=actor,
                 before=before, after=updated,
                 meta={"keys": sorted(changes), "backup": str(backup) if backup else ""})
    return {"ok": True, "path": str(path), "backup": str(backup) if backup else "",
            "view": switches_view()}


# ------------------------------------------------------------------ 护栏（只读）
def guard_rules() -> dict:
    """只读护栏规则；不提供任何修改入口。"""
    try:
        guard = _soul_module("agi.selflearn.guard")
        return {"ok": True, "readonly": True,
                "rules": [
                    {"key": "slang", "name": "网感腔", "description": "网络梗/夸张比喻/主播腔"},
                    {"key": "canned", "name": "金句腔", "description": "模板化金句/强行升华"},
                    {"key": "false_failure", "name": "编造故障", "description": "谎称网络/系统故障"},
                    {"key": "closing", "name": "主动收尾", "description": "未经对方同意主动结束对话"},
                    {"key": "net_probe", "name": "联网探测", "description": "未实际调用工具却声称查过"},
                ],
                "absolute": list(getattr(guard, "ABSOLUTE", ()) or []),
                "tool_claims": list(getattr(guard, "TOOL_CLAIMS", ()) or []),
                "forbidden": list(getattr(guard, "FORBIDDEN", ()) or []),
                "slang_pattern": getattr(getattr(guard, "SLANG_RE", None), "pattern", ""),
                "canned_pattern": getattr(getattr(guard, "CANNED_RE", None), "pattern", ""),
                "closing_pattern": getattr(getattr(guard, "CLOSING_RE", None), "pattern", ""),
                "false_failure_pattern": getattr(getattr(guard, "FALSE_FAILURE_RE", None), "pattern", "")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"guard_unavailable:{type(exc).__name__}",
                "readonly": True}


def citation_rules() -> dict:
    try:
        citation = _soul_module("agi.selflearn.citation")
        return {"ok": True, "readonly": True,
                "pattern": getattr(getattr(citation, "CITATION", None), "pattern", ""),
                "empty_memory": getattr(citation, "EMPTY_MEMORY", "")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"citation_unavailable:{type(exc).__name__}",
                "readonly": True}


# ------------------------------------------------------------------ trace
def _trace_db(db: str | None = None) -> tuple[Path | None, str]:
    if db:
        path = Path(str(db)).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() != ".sqlite3":
            return None, "invalid_trace_db"
        if not _under(path, _soul_root()) and not _under(path, Path(paths.DATA_DIR)):
            return None, "path_out_of_allowed_roots"
        return path, ""
    base = _soul_root() / "agi" / "data" / "selflearn" / "wechat_contacts"
    candidates = sorted(base.glob("*.memory.sqlite3"), key=lambda p: p.stat().st_mtime,
                        reverse=True) if base.is_dir() else []
    if not candidates:
        fallback = _soul_root() / "agi" / "data" / "selflearn" / "selflearn.memory.sqlite3"
        candidates = [fallback] if fallback.is_file() else []
    if not candidates:
        return None, "trace_db_not_found"
    return candidates[0], ""


def traces(db: str | None = None, *, limit: int = 50, contact: str = "",
           mode: str = "") -> dict:
    path, error = _trace_db(db)
    if not path:
        return {"ok": False, "error": error}
    try:
        trace = _soul_module("agi.selflearn.trace")
        items = trace.list_turns(path, limit=limit, contact=contact, mode=mode)
        for item in items:
            item.setdefault("user", item.get("user_text") or "")
            item.setdefault("reply", item.get("final_reply") or "")
            item.setdefault("inner", None)
            item.setdefault("guard", None)
            try:
                detail = trace.get_turn(path, str(item.get("id") or ""))
                if detail:
                    item["guard"] = detail.get("guard")
                    item["inner"] = detail.get("inner")
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "db": str(path), "items": items, "count": len(items)}
    except Exception:  # noqa: BLE001 - 退回直接只读查询
        import sqlite3
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT id,ts,mode,contact,user_text,final_reply,latency_ms,"
                "skipped,skip_reason,crisis FROM turn_traces "
                "ORDER BY ts DESC, created_at DESC LIMIT ?", (max(1, min(500, int(limit))),)).fetchall()
        finally:
            con.close()
        keys = ("id", "ts", "mode", "contact", "user_text", "final_reply",
                "latency_ms", "skipped", "skip_reason", "crisis")
        items = [dict(zip(keys, row)) for row in rows]
        return {"ok": True, "db": str(path), "items": items, "count": len(items)}


def trace_detail(turn_id: str, db: str | None = None) -> dict:
    path, error = _trace_db(db)
    if not path:
        return {"ok": False, "error": error}
    try:
        trace = _soul_module("agi.selflearn.trace")
        item = trace.get_turn(path, str(turn_id or ""))
        if item is None:
            return {"ok": False, "error": "trace_not_found"}
        return {"ok": True, "db": str(path), "trace": item}
    except Exception:
        import sqlite3
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            cursor = con.execute("SELECT * FROM turn_traces WHERE id=?",
                                 (str(turn_id or ""),))
            row = cursor.fetchone()
            names = [d[0] for d in cursor.description or []]
        finally:
            con.close()
        if row is None:
            return {"ok": False, "error": "trace_not_found"}
        item = dict(zip(names, row))
        for field in ("memories_json", "inner_json", "mood_json", "generation_json",
                      "polish_json", "guard_json", "proactive_json"):
            try:
                item[field] = json.loads(item.get(field) or "null")
            except (TypeError, json.JSONDecodeError):
                pass
        return {"ok": True, "db": str(path), "trace": item}


# ------------------------------------------------------------------ 试跑（dry）
class TrialClient:
    """试跑用确定性假模型：不联网、不写生产库、不发微信。"""

    def __init__(self, reply: str = "（试跑）听到了，今天想聊点什么？"):
        self.reply = reply

    def available(self) -> bool:
        return True

    def chat(self, messages, **kwargs):
        return {"ok": True, "text": self.reply}


def run_trial(text: str, *, client: Any = None, cleanup: bool = True,
              agent_factory: Any = None) -> dict:
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "text_required"}
    trial_dir = Path(paths.DATA_DIR) / "chattrials" / uuid.uuid4().hex[:12]
    trial_dir.mkdir(parents=True, exist_ok=True)
    db_path = trial_dir / "selflearn.sqlite3"
    env_keys = {"INNER_MODE": "off", "MOOD_JUDGE_ENABLED": "0",
                "WEB_LOOKUP_ENABLED": "0", "MEMORY_CITATION_ENABLED": "0",
                "TRACE_ENABLED": "1"}
    saved = {key: os.environ.get(key) for key in env_keys}
    started = time.perf_counter()
    try:
        os.environ.update(env_keys)
        if agent_factory is not None:
            agent = agent_factory(db_path)
        else:
            chat = _soul_module("agi.selflearn.chat")
            agent = chat.SelfLearnAgent(db_path, client=client or TrialClient(),
                                        rng=random.Random(7))
        result_holder: dict[str, Any] = {}

        def _worker():
            result_holder["result"] = agent.send(text)

        worker = __import__("threading").Thread(target=_worker, daemon=True)
        worker.start()
        worker.join(60)
        if worker.is_alive():
            return {"ok": False, "dry_run": True, "error": "trial_timeout_60s"}
        result = result_holder.get("result") or {}
        trace_item = None
        try:
            mem_path = db_path.with_name(db_path.stem + ".memory.sqlite3")
            path, _err = _trace_db(str(mem_path if mem_path.is_file() else db_path))
            if path:
                rows = traces(str(path), limit=1, contact="")
                if rows.get("items"):
                    trace_item = rows["items"][0]
        except Exception:  # noqa: BLE001
            trace_item = None
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        return {"ok": bool(result.get("ok")), "dry_run": True,
                "reply": result.get("reply") or result.get("text") or "",
                "latency_ms": result.get("latency_ms") or elapsed,
                "bubbles": result.get("bubbles") or [],
                "guard": result.get("guard") or {},
                "memory_hits": result.get("memory_ids") or [],
                "result": result, "trace": trace_item,
                "elapsed_ms": elapsed, "db": str(db_path)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dry_run": True,
                "error": f"trial_failed:{type(exc).__name__}: {exc}"}
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if cleanup:
            shutil.rmtree(trial_dir, ignore_errors=True)
