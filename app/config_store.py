# -*- coding: utf-8 -*-
"""配置中心：读取/校验/保存/备份/恢复/审计（精简版，无 Git 自动提交）。"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from . import audit, config_schema, paths

BACKUP_DIR = paths.DATA_DIR / "config_backups"


def _mask(item: dict, value):
    if item.get("secret") and isinstance(value, str) and value:
        return "******"
    return value


def load_raw() -> dict:
    p = paths.wechat_config_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def load_view() -> dict:
    raw = load_raw()
    items = []
    for item in config_schema.SCHEMA:
        value = raw.get(item["key"], item.get("default"))
        items.append({
            "key": item["key"], "group": item["group"], "type": item["type"],
            "label": item.get("label", item["key"]), "desc": item.get("desc", ""),
            "value": _mask(item, value),
            "default": item.get("default"),
            "min": item.get("min"), "max": item.get("max"),
            "enum": item.get("enum"), "secret": bool(item.get("secret")),
            "restart": bool(item.get("restart")),
        })
    return {"ok": True, "groups": config_schema.groups(), "items": items,
            "path": str(paths.wechat_config_path()),
            "mtime": (paths.wechat_config_path().stat().st_mtime
                      if paths.wechat_config_path().is_file() else None)}


def validate(patch: dict) -> dict:
    return config_schema.validate_all(patch)


def save(patch: dict, *, actor: str = "workbench") -> dict:
    """保存补丁：校验 → 写前备份 → 原子写 → 审计（不再自动 Git 提交）。"""
    if not isinstance(patch, dict) or not patch:
        return {"ok": False, "error": "empty_patch"}
    result = validate(patch)
    if not result["ok"]:
        return {"ok": False, "error": "invalid", "errors": result["errors"]}

    p = paths.wechat_config_path()
    before = load_raw()
    after = dict(before)
    after.update(patch)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if p.is_file():
        shutil.copy2(p, BACKUP_DIR / f"config.{stamp}.json")
        old = sorted(BACKUP_DIR.glob("config.*.json"))
        for extra in old[:-50]:
            try:
                extra.unlink()
            except OSError:
                pass

    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)

    audit.record("config.save", str(p), actor=actor, before=before, after=after,
                 meta={"keys": sorted(patch.keys())})

    restart_keys = [k for k in patch
                    if (config_schema.by_key(k) or {}).get("restart")]
    return {"ok": True, "changed": sorted(patch.keys()),
            "restart_required": restart_keys}


def restore_backup(name: str, *, actor: str = "workbench") -> dict:
    src = BACKUP_DIR / Path(name).name
    if not src.is_file():
        return {"ok": False, "error": "backup_not_found"}
    p = paths.wechat_config_path()
    before = load_raw()
    shutil.copy2(src, p)
    after = load_raw()
    audit.record("config.restore", str(p), actor=actor, before=before, after=after,
                 meta={"backup": src.name})
    return {"ok": True, "restored": src.name}


def backups() -> list[str]:
    if not BACKUP_DIR.is_dir():
        return []
    return sorted((p.name for p in BACKUP_DIR.glob("config.*.json")), reverse=True)
