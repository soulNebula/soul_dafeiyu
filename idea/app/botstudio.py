# -*- coding: utf-8 -*-
"""803 · Bot 管理（Phase 1B）。

Bot 包定义在仓库 `tools/bot_profile.py`；这里只做 803 的注册表与 HTTP 层：
- 用户自选目录创建 Bot（当前只允许云端方案）；
- 记录用户选过的 Bot 路径（`idea/.data/bots.json`，不是仓库数据）；
- 列表/校验/详情/移除登记（不删除文件）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import paths

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # 让 803 能复用主仓的 Bot schema（不复制一份实现）
    sys.path.insert(0, str(ROOT))

from tools import bot_profile  # noqa: E402

REGISTRY = paths.DATA_DIR / "bots.json"


def _norm(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _load_registry() -> list[dict[str, Any]]:
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if isinstance(item, dict) and item.get("path"):
            out.append({"path": str(item["path"]), "added_at": item.get("added_at")})
        elif isinstance(item, str):
            out.append({"path": item, "added_at": None})
    return out


def _save_registry(items: list[dict[str, Any]]) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps({"schema": 1, "items": items},
                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def register(path: str | Path) -> dict[str, Any]:
    root = _norm(path)
    items = _load_registry()
    if not any(_norm(item["path"]) == root for item in items):
        items.append({"path": root, "added_at": int(time.time())})
        _save_registry(items)
    return {"ok": True, "path": root}


def unregister(path: str | Path) -> dict[str, Any]:
    root = _norm(path)
    items = [item for item in _load_registry() if _norm(item["path"]) != root]
    _save_registry(items)
    return {"ok": True, "path": root, "removed": True}


def _bot_database_files(path: str | Path) -> list[Path]:
    root = Path(path)
    return sorted((root / "data" / "selflearn" / "wechat_contacts").glob("*.memory.sqlite3"))


def active_bot_info() -> dict[str, Any]:
    try:
        from agi import bot_runtime
        active = bot_runtime.read_active()
    except Exception:  # noqa: BLE001
        active = None
    if not active or not active.get("path"):
        return {"ok": True, "active": False}
    path = str(active["path"])
    return {"ok": True, "active": True, "path": path,
            "name": active.get("name") or Path(path).name,
            "has_database": bool(_bot_database_files(path))}


def list_bots() -> dict[str, Any]:
    items = []
    for item in _load_registry():
        root = item["path"]
        try:
            result = bot_profile.validate_bot(root)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "errors": [f"{type(exc).__name__}: {exc}"],
                      "warnings": [], "root": root}
        summary = {"path": root, "added_at": item.get("added_at")}
        try:
            bot = bot_profile.load_bot(root)
            summary.update({"name": bot.get("name"), "bot_id": bot.get("bot_id"),
                            "mode": bot.get("mode"), "schema": bot.get("schema")})
        except Exception:  # noqa: BLE001
            summary.update({"name": Path(root).name, "mode": "", "schema": None})
        summary["valid"] = bool(result.get("ok"))
        summary["has_database"] = bool(_bot_database_files(root))
        summary["errors"] = result.get("errors") or []
        summary["warnings"] = result.get("warnings") or []
        items.append(summary)
    return {"ok": True, "items": items, "registry": str(REGISTRY)}


def active_packages() -> dict[str, Any]:
    """当前活跃 Bot 的训练数据包（没激活 Bot 时读仓库默认 data/train/packages）。"""
    try:
        from agi import bot_runtime
        active = bot_runtime.read_active()
    except Exception:  # noqa: BLE001
        active = None
    if active and active.get("path"):
        base = Path(active["path"]) / "data" / "train" / "packages"
        source = "active_bot"
        bot_name = active.get("name")
    else:
        base = ROOT / "agi" / "data" / "train" / "packages"
        source = "default"
        bot_name = None
    items = []
    if base.is_dir():
        for path in sorted(base.glob("*.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(item, dict):
                item["path"] = str(path)
                items.append(item)
    items.sort(key=lambda x: int(x.get("created_at") or 0), reverse=True)
    return {"ok": True, "source": source, "bot": bot_name, "root": str(base),
            "items": items[:50]}


def add_database(path: str, alias: str = "") -> dict[str, Any]:
    """给 Bot 创建一套联系人数据库（已有则拒绝，前端按钮置灰）。"""
    root = _norm(path)
    validation = bot_profile.validate_bot(root)
    if not validation.get("ok"):
        return {"ok": False, "error": "invalid_bot", "validation": validation}
    if _bot_database_files(root):
        return {"ok": False, "error": "database_exists",
                "hint": "该 Bot 已有数据库，不能重复添加"}
    name = str(alias or "").strip()
    if not name:
        try:
            bot = bot_profile.load_bot(root)
            contacts = ((bot.get("wechat") or {}).get("allowed_contacts") or [])
            name = str(contacts[0]) if contacts else "新联系人"
        except Exception:  # noqa: BLE001
            name = "新联系人"
    import hashlib
    stem = hashlib.sha1(name.encode("utf-8")).hexdigest()[:14]
    target = Path(root) / "data" / "selflearn" / "wechat_contacts" / f"{stem}.memory.sqlite3"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from agi.selflearn import db as selflearn_db
        con = selflearn_db.connect(target)
        con.close()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"create_failed: {type(exc).__name__}: {exc}"}
    # 顺手登记到 Bot 的可选联系人，方便 802 找到
    try:
        bot = bot_profile.load_bot(root)
        bot.pop("_root", None)
        wechat = bot.setdefault("wechat", {})
        contacts = wechat.setdefault("contacts", [])
        if name not in contacts:
            contacts.append(name)
        (Path(root) / "bot.json").write_text(
            json.dumps(bot, ensure_ascii=False, indent=2) + chr(10),
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "path": str(target), "alias": name, "has_database": True}


def inspect_bot(path: str) -> dict[str, Any]:
    root = _norm(path)
    validation = bot_profile.validate_bot(root)
    info: dict[str, Any] = {}
    try:
        info = bot_profile.describe_bot(root)
    except Exception as exc:  # noqa: BLE001
        info = {"error": f"{type(exc).__name__}: {exc}"}
    return {"ok": bool(validation.get("ok")), "validation": validation, "bot": info}


def create_bot(body: dict[str, Any]) -> dict[str, Any]:
    path = str(body.get("path") or "").strip()
    name = str(body.get("name") or "").strip()
    if not path:
        return {"ok": False, "error": "path_required"}
    if not name:
        return {"ok": False, "error": "name_required"}
    mode = str(body.get("mode") or "cloud").strip().lower()
    password = str(body.get("password") or "")
    if len(password) < 6:
        return {"ok": False, "error": "password_required",
                "hint": "802 登录需要 Bot 密码（至少 6 位）"}
    chat = {
        "base_url": str(body.get("base_url") or "https://api.deepseek.com").strip(),
        "model": str(body.get("model") or "deepseek-flash").strip(),
        "api_key": str(body.get("api_key") or "").strip(),
    }
    soul = {
        "base_url": str(body.get("soul_base_url") or chat["base_url"]).strip(),
        "model": str(body.get("soul_model") or chat["model"]).strip(),
        "api_key": str(body.get("soul_api_key") or chat["api_key"]).strip(),
    }
    vision = None
    if body.get("vision_model"):
        vision = {
            "base_url": str(body.get("vision_base_url") or chat["base_url"]).strip(),
            "model": str(body["vision_model"]).strip(),
            "api_key": str(body.get("vision_api_key") or chat["api_key"]).strip(),
        }
    # 防呆：Bot 必须是独立目录；不能直接选 AGENT_HOME 根，也不覆盖已有内容
    try:
        root = Path(path).expanduser().resolve()
        home = Path(os.environ.get("AGENT_HOME") or str(ROOT / "agi" / "data")).resolve()
        if root == home:
            return {"ok": False, "error": "path_is_agent_home",
                    "hint": "不要选 AGENT_HOME 根目录；请选它的子目录，例如 E:/agent/bots/xiaoyu"}
        if root.exists() and any(root.iterdir()):
            return {"ok": False, "error": "path_not_empty",
                    "hint": "Bot 目录必须为空（或尚不存在）；已有 Bot 请用「添加已有目录」"}
    except OSError as exc:
        return {"ok": False, "error": f"path_invalid: {exc}"}
    try:
        bot = bot_profile.create_bot(path, name, mode=mode, password=password,
                                     chat=chat, soul=soul, vision=vision)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    register(path)
    return {"ok": True, "bot": bot_profile.describe_bot(path),
            "validation": bot_profile.validate_bot(path), "bot_id": bot.get("bot_id")}


def change_password(body: dict[str, Any]) -> dict[str, Any]:
    path = str(body.get("path") or "").strip()
    old = str(body.get("old_password") or "")
    new = str(body.get("new_password") or "")
    if not path:
        return {"ok": False, "error": "path_required"}
    try:
        return bot_profile.change_password(path, old, new)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def export_current(body: dict[str, Any]) -> dict[str, Any]:
    """把当前 802 数据根导出成 Bot 包（含配置/联系人/人格/日记；不含明文 secrets）。"""
    path = str(body.get("path") or "").strip()
    name = str(body.get("name") or "").strip() or "802 当前配置"
    password = str(body.get("password") or "")
    if not path:
        return {"ok": False, "error": "path_required"}
    if len(password) < 6:
        return {"ok": False, "error": "password_required"}
    try:
        from agi import bot_runtime
        from tools import export_bot
        active = bot_runtime.read_active()
        source = None
        if active and active.get("path"):
            source = Path(active["path"]) / "data"
        result = export_bot.export(source_data=source, path=path, name=name,
                                   password=password,
                                   with_media=bool(body.get("with_media", False)))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if result.get("ok"):
        register(path)
    return result


def add_existing(path: str) -> dict[str, Any]:
    root = _norm(path)
    result = bot_profile.validate_bot(root)
    if not result.get("ok"):
        return {"ok": False, "error": "invalid_bot", "validation": result}
    register(root)
    return {"ok": True, "path": root, "validation": result}
