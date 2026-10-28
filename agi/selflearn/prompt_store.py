# -*- coding: utf-8 -*-
"""P4-b：提示词覆盖存储（可编辑 / 热加载 / 可校验 / 可回滚）。

文件：``agi/data/prompts.json``（gitignore），结构固定为::

    {"version": 1, "prompts": {"<key>": "<text>"}, "updated_at": 1730000000.0}

- 只允许白名单键；value 必须是 str，单条 ≤4000 字，文件 ≤64KB；
- 空字符串等价于删除覆盖（回退代码默认值）；
- 原子写（同目录 tmp + os.replace），写失败不破坏旧文件；
- ``load()`` 按 mtime 缓存，第三方直接改文件后，下一条消息/下一次 tick 自动生效；
- 文件损坏/非法 → 返回空覆盖并 warning，不影响主链；
- ``defaults()`` 从现有代码常量导出（不复制一份），前端展示“默认 vs 覆盖”用。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any
from agi.paths import data_dir as _default_data_dir

ROOT = Path(__file__).resolve().parents[2]
PROMPTS_PATH = _default_data_dir() / "prompts.json"
PROMPT_OVERRIDE_LOG_PATH = _default_data_dir() / "selflearn" / "prompt_override.log"

MAX_CHARS_PER_ENTRY = 4000
MAX_FILE_BYTES = 64 * 1024
VERSION = 1

KNOWN_KEYS: tuple[str, ...] = (
    "proactive.persona_constraints",
    "proactive.kind.care",
    "proactive.kind.share",
    "proactive.kind.light",
    "proactive.kind.followup",
    "proactive.kind.silence_ack",
    "proactive.anti_fabrication",
    "proactive.punctuation",
    "proactive.continuity",
    "proactive.voice_fewshot",
    "chat.system",
)
KNOWN_KEY_SET = set(KNOWN_KEYS)

_LOGGER = logging.getLogger("agi.prompt_store")
_LOCK = threading.RLock()
_CACHE_MTIME: float | None = None
_CACHE_PAYLOAD: dict | None = None
_LAST_LOGGED_VERSION: int | None = None
_LAST_ERROR_TS = 0.0


def prompts_path() -> Path:
    """当前提示词文件路径（测试可 monkeypatch 模块属性/环境变量）。"""
    env = os.environ.get("PROMPTS_PATH")
    return Path(env) if env else Path(PROMPTS_PATH)


def _empty_payload() -> dict:
    return {"version": VERSION, "prompts": {}}


def _warn(message: str, *, now: float | None = None) -> None:
    """prompt_store_error：节流 warning（避免每轮刷日志）。"""
    global _LAST_ERROR_TS
    now = time.time() if now is None else float(now)
    if now - _LAST_ERROR_TS < 600.0:
        return
    _LAST_ERROR_TS = now
    try:
        _LOGGER.warning("prompt_store_error: %s", message)
    except Exception:  # noqa: BLE001
        pass


def _log_override(payload: dict) -> None:
    """prompt_override：按 version 去重，JSONL 一行（写失败不影响主链）。"""
    global _LAST_LOGGED_VERSION
    version = int(payload.get("version") or 0)
    prompts = payload.get("prompts") or {}
    if not prompts or version == _LAST_LOGGED_VERSION:
        return
    _LAST_LOGGED_VERSION = version
    entry = {"ts": round(time.time(), 3), "kind": "prompt_override",
             "version": version, "keys": sorted(prompts)}
    try:
        target = Path(PROMPT_OVERRIDE_LOG_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _normalize_payload(raw: Any) -> dict:
    """非法/未知键 → 丢弃；只保留白名单里的字符串覆盖。"""
    if not isinstance(raw, dict):
        raise ValueError("prompts.json 顶层必须是对象")
    version_raw = raw.get("version", VERSION)
    try:
        version = int(version_raw)
    except (TypeError, ValueError):
        version = VERSION
    prompts = raw.get("prompts", {})
    if not isinstance(prompts, dict):
        raise ValueError("prompts 必须是对象")
    clean: dict[str, str] = {}
    for key, value in prompts.items():
        if key not in KNOWN_KEY_SET:
            _warn(f"unknown key: {key}")
            continue
        if not isinstance(value, str):
            _warn(f"non-str value: {key}")
            continue
        if len(value) > MAX_CHARS_PER_ENTRY:
            _warn(f"too long: {key}")
            continue
        if value == "":
            continue  # 空串 = 删除覆盖
        clean[key] = value
    payload = {"version": version, "prompts": clean}
    if "updated_at" in raw:
        payload["updated_at"] = raw.get("updated_at")
    return payload


def load(*, force: bool = False) -> dict:
    """读取覆盖（mtime 缓存）；损坏/缺失返回空覆盖。"""
    global _CACHE_MTIME, _CACHE_PAYLOAD
    path = prompts_path()
    with _LOCK:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            _CACHE_MTIME = None
            _CACHE_PAYLOAD = _empty_payload()
            return dict(_CACHE_PAYLOAD)
        if (not force and _CACHE_MTIME == mtime and _CACHE_PAYLOAD is not None):
            return {"version": _CACHE_PAYLOAD["version"],
                    "prompts": dict(_CACHE_PAYLOAD.get("prompts") or {})}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            payload = _normalize_payload(raw)
        except Exception as exc:  # noqa: BLE001 - 损坏回退默认
            _warn(f"load failed: {type(exc).__name__}: {exc}")
            payload = _empty_payload()
        _CACHE_MTIME = mtime
        _CACHE_PAYLOAD = dict(payload)
        _log_override(payload)
        return {"version": payload["version"],
                "prompts": dict(payload.get("prompts") or {})}


def overrides() -> dict[str, str]:
    return dict(load().get("prompts") or {})


def get(key: str, default: str = "") -> str:
    """取覆盖值；未配置/空串 → default（代码内置默认）。"""
    value = (load().get("prompts") or {}).get(key)
    return value if isinstance(value, str) and value else default


def defaults() -> dict[str, str]:
    """从现有代码常量导出默认提示词（唯一真源，不复制）。"""
    from . import proactive
    from . import chat

    try:
        from . import prompt_store  # noqa: F401 - 自引用无副作用
    except Exception:  # noqa: BLE001
        pass
    values = {
        "proactive.persona_constraints": proactive.PERSONA_CONSTRAINTS,
        "proactive.anti_fabrication": proactive.GROUNDING_CONSTRAINTS,
        "proactive.punctuation": proactive.RUNON_CONSTRAINTS,
        "proactive.continuity": proactive.CONTINUITY_CONSTRAINTS,
        "proactive.voice_fewshot": proactive.VOICE_FEWSHOT_SECTION,
        "chat.system": chat.SYSTEM,
    }
    for kind in ("care", "share", "light", "followup", "silence_ack"):
        values[f"proactive.kind.{kind}"] = proactive.KIND_INSTRUCTION[kind]
    return values


def validate(payload: Any) -> tuple[bool, list[str]]:
    """校验文件结构（前端保存前调用；返回 (ok, errors)）。"""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return False, ["payload 必须是对象"]
    if "prompts" not in payload or not isinstance(payload.get("prompts"), dict):
        return False, ["缺少 prompts 对象"]
    if "version" in payload:
        try:
            int(payload.get("version"))
        except (TypeError, ValueError):
            errors.append("version 必须是整数")
    prompts = payload.get("prompts") or {}
    total = 0
    for key, value in prompts.items():
        if key not in KNOWN_KEY_SET:
            errors.append(f"未知键: {key}")
            continue
        if not isinstance(value, str):
            errors.append(f"{key}: value 必须是字符串")
            continue
        if len(value) > MAX_CHARS_PER_ENTRY:
            errors.append(f"{key}: 超过 {MAX_CHARS_PER_ENTRY} 字")
        total += len(key) + len(value)
    if total > MAX_FILE_BYTES:
        errors.append("文件总大小超过 64KB")
    return (not errors), errors


def _atomic_write(payload: dict) -> None:
    path = prompts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:  # noqa: BLE001
            pass


def set_many(items: dict[str, str]) -> dict:
    """批量覆盖（空串=删除）；原子写 + 返回新 payload；非法抛 ValueError。"""
    if not isinstance(items, dict):
        raise ValueError("items 必须是 dict")
    payload = load()
    prompts = dict(payload.get("prompts") or {})
    for key, value in items.items():
        if key not in KNOWN_KEY_SET:
            raise ValueError(f"未知键: {key}")
        if not isinstance(value, str):
            raise ValueError(f"{key}: value 必须是字符串")
        if len(value) > MAX_CHARS_PER_ENTRY:
            raise ValueError(f"{key}: 超过 {MAX_CHARS_PER_ENTRY} 字")
        if value == "":
            prompts.pop(key, None)
        else:
            prompts[key] = value
    new_payload = {"version": int(payload.get("version") or VERSION) + 1,
                   "prompts": prompts, "updated_at": round(time.time(), 3)}
    ok, errors = validate(new_payload)
    if not ok:
        raise ValueError("; ".join(errors))
    if len(json.dumps(new_payload, ensure_ascii=False).encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("文件总大小超过 64KB")
    _atomic_write(new_payload)
    return new_payload


def delete(key: str) -> dict:
    if key not in KNOWN_KEY_SET:
        raise ValueError(f"未知键: {key}")
    return set_many({key: ""})


def delete_all() -> dict:
    """删除整个覆盖文件（回滚到代码默认）。"""
    path = prompts_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    global _CACHE_MTIME, _CACHE_PAYLOAD
    with _LOCK:
        _CACHE_MTIME = None
        _CACHE_PAYLOAD = _empty_payload()
    return _empty_payload()


def snapshot() -> dict:
    """前端读取：默认值 + 覆盖值 + 当前生效值。"""
    payload = load()
    defaults_map = defaults()
    overrides_map = payload.get("prompts") or {}
    return {"version": payload.get("version") or VERSION,
            "path": str(prompts_path()),
            "defaults": defaults_map,
            "overrides": overrides_map,
            "effective": {key: overrides_map.get(key, defaults_map.get(key, ""))
                          for key in defaults_map}}
