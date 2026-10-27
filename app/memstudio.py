# -*- coding: utf-8 -*-
"""M1-B：Memory Studio 后端（只读总览 + 规则/衰减 + revive/purge + 时间线）。

不注册 FastAPI 路由（由 main.py 统一接线）；本模块只提供纯函数端点。
- 读取被管理的 SoulAgent 记忆库（默认第一个联系人 `.memory.sqlite3`）；
- 配置读写复用 idea.app.config_store（校验/备份/审计），不自己写配置；
- 衰减参数/revive/purge 只调用 SoulAgent 的 memory_decay，不改其逻辑。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import config_store, paths

RULE_KEYS = (
    "memory_decay_enabled",
    "memory_purge_enabled",
    "memory_sleep_hour",
    "trace_enabled",
    "trace_max_rows",
)
PURGE_CONFIRM = "永久删除休眠记忆"
STAGE_NAMES = ("full", "compressed", "keyword", "dormant")
STRENGTH_BUCKETS = ("0-0.2", "0.2-0.5", "0.5-0.8", "0.8-1")


# ------------------------------------------------------------------ SoulAgent
def _soul():
    """把被管理的 SoulAgent 加入 sys.path，返回其 db/memory_decay 模块。

    测试会把 soulagent_home 指到 tmp（不带 agi/），此时回退到真实仓库根，
    避免 import agi.selflearn 失败。
    """
    root = Path(paths.soulagent_home())
    if not (root / "agi" / "selflearn").is_dir():
        real = Path(os.environ.get("SOULAGENT_HOME")
                    or (Path.home() / "Desktop" / "SoulAgent"))
        if (real / "agi" / "selflearn").is_dir():
            root = real
    root_s = str(root)
    if root_s in sys.path:
        sys.path.remove(root_s)
    sys.path.insert(0, root_s)
    # 若 agi 包已被别处（如测试临时目录）以 namespace package 形式导入，
    # 其 __path__ 只指向那个目录；这里补上真实仓库的 agi 路径。
    real_agi = str(Path(root_s) / "agi")
    pkg = sys.modules.get("agi")
    if pkg is not None and hasattr(pkg, "__path__") and real_agi not in list(pkg.__path__):
        pkg.__path__.append(real_agi)
    from agi.selflearn import db, memory_decay  # type: ignore
    return db, memory_decay


def _contact_dir() -> Path:
    return paths.soulagent_home() / "agi" / "data" / "selflearn" / "wechat_contacts"


def _resolve_base(contact: str | None = None) -> Path:
    """返回联系人库基名（db.connect 用）；无联系人取第一个现有联系人。"""
    directory = _contact_dir()
    if contact:
        safe = hashlib.sha1(str(contact).encode("utf-8")).hexdigest()[:14]
        return directory / f"{safe}.sqlite3"
    if directory.is_dir():
        files = sorted(directory.glob("*.memory.sqlite3"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            return files[0].with_name(files[0].name[:-len(".memory.sqlite3")] + ".sqlite3")
    return paths.soulagent_home() / "agi" / "data" / "selflearn" / "selflearn.sqlite3"


def _memory_file(contact: str | None = None) -> Path:
    db, _ = _soul()
    base = _resolve_base(contact)
    return db.memory_path(base)


def _ro(contact: str | None = None) -> sqlite3.Connection | None:
    path = _memory_file(contact)
    if not path.is_file():
        return None
    con = sqlite3.connect(str(path), timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _columns(con) -> set[str]:
    try:
        return {str(row[1]) for row in con.execute(
            "PRAGMA table_info(user_memories)").fetchall()}
    except Exception:  # noqa: BLE001
        return set()


def _row_value(row: dict, key: str, default: Any = "") -> Any:
    value = row.get(key)
    return default if value is None else value


def _stage(row: dict) -> str:
    stage = str(_row_value(row, "forget_stage", "full") or "full")
    return stage if stage in STAGE_NAMES else "full"


def _strength(row: dict) -> float:
    try:
        return max(0.0, min(1.0, float(_row_value(row, "strength", 1.0) or 1.0)))
    except (TypeError, ValueError):
        return 1.0


def _updated_at(row: dict) -> float:
    values = []
    for key in ("last_strength_at", "last_accessed", "created_at"):
        try:
            values.append(float(row.get(key) or 0))
        except (TypeError, ValueError):
            pass
    return max(values) if values else 0.0


def _bucket(strength: float) -> str:
    if strength < 0.2:
        return "0-0.2"
    if strength < 0.5:
        return "0.2-0.5"
    if strength < 0.8:
        return "0.5-0.8"
    return "0.8-1"


def _row_public(row: dict, *, with_full: bool = False) -> dict:
    item = {
        "id": str(row.get("id") or ""),
        "kind": str(row.get("kind") or ""),
        "content": str(row.get("content") or "")[:300],
        "stage": _stage(row),
        "strength": round(_strength(row), 4),
        "importance": round(float(_row_value(row, "importance", 0.5) or 0.5), 3),
        "status": str(row.get("status") or "active"),
        "revive_count": int(_row_value(row, "revive_count", 0) or 0),
        "created_at": float(_row_value(row, "created_at", 0) or 0),
        "updated_at": _updated_at(row),
    }
    if with_full:
        item["content_full"] = str(
            row.get("content_full") or row.get("content") or "")
        item["evidence"] = str(row.get("evidence") or "")
        item["confidence"] = float(_row_value(row, "confidence", 0) or 0)
    return item


# ------------------------------------------------------------------ 总览
def overview(contact: str | None = None) -> dict:
    con = _ro(contact)
    if con is None:
        return {"ok": False, "error": "memory_db_not_found",
                "contact": contact or "", "db": str(_memory_file(contact))}
    try:
        cols = _columns(con)
        rows = [dict(row) for row in con.execute(
            "SELECT * FROM user_memories").fetchall()]
    finally:
        con.close()
    kinds = Counter(str(_row_value(row, "kind", "unknown") or "unknown")
                    for row in rows)
    stages = Counter(_stage(row) for row in rows)
    buckets = Counter(_bucket(_strength(row)) for row in rows)
    recent = sorted(rows, key=_updated_at, reverse=True)[:20]
    return {
        "ok": True,
        "contact": contact or "",
        "db": str(_memory_file(contact)),
        "total": len(rows),
        "by_kind": dict(kinds),
        "by_stage": {name: int(stages.get(name, 0)) for name in STAGE_NAMES},
        "strength": {name: int(buckets.get(name, 0)) for name in STRENGTH_BUCKETS},
        "recent": [_row_public(row) for row in recent],
        "columns": sorted(cols),
    }


# ------------------------------------------------------------------ 列表/详情
def list_memories(contact: str | None = None, *, kind: str = "",
                  stage: str = "", q: str = "", limit: int = 50,
                  offset: int = 0) -> dict:
    con = _ro(contact)
    if con is None:
        return {"ok": False, "error": "memory_db_not_found", "items": [], "total": 0}
    try:
        cols = _columns(con)
        where: list[str] = []
        params: list[Any] = []
        if kind:
            where.append("kind = ?")
            params.append(str(kind))
        if stage and "forget_stage" in cols:
            where.append("forget_stage = ?")
            params.append(str(stage))
        elif stage and str(stage) != "full":
            # 旧库没有 forget_stage 列：所有记忆都算 full，其余阶段为空
            return {"ok": True, "items": [], "total": 0, "contact": contact or "",
                    "limit": limit, "offset": offset}
        if q:
            if "content_full" in cols:
                where.append("(content LIKE ? OR content_full LIKE ?)")
            else:
                where.append("content LIKE ?")
            like = f"%{str(q)}%"
            params.extend([like, like] if "content_full" in cols else [like])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        try:
            limit = max(1, min(200, int(limit)))
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            limit, offset = 50, 0
        total = int(con.execute(
            f"SELECT count(*) FROM user_memories{clause}", params).fetchone()[0])
        # 不同版本的 memory 库列不一致：优先 last_strength_at，其次 updated_at/created_at
        if "last_strength_at" in cols:
            order = "COALESCE(last_strength_at, created_at) DESC, created_at DESC"
        elif "updated_at" in cols:
            order = "COALESCE(updated_at, created_at) DESC, created_at DESC"
        else:
            order = "created_at DESC"
        rows = con.execute(
            f"SELECT * FROM user_memories{clause} ORDER BY {order} "
            "LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        return {"ok": True, "contact": contact or "", "items": [
            _row_public(dict(row)) for row in rows], "total": total,
            "limit": limit, "offset": offset}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "items": [], "total": 0}
    finally:
        con.close()


def get_memory(memory_id: str, contact: str | None = None) -> dict:
    con = _ro(contact)
    if con is None:
        return {"ok": False, "error": "memory_db_not_found"}
    try:
        row = con.execute("SELECT * FROM user_memories WHERE id=?",
                          (str(memory_id or ""),)).fetchone()
    finally:
        con.close()
    if row is None:
        return {"ok": False, "error": "memory_not_found", "id": memory_id}
    return {"ok": True, "memory": _row_public(dict(row), with_full=True)}


# ------------------------------------------------------------------ 规则
def rules() -> dict:
    view = config_store.load_view()
    by_key = {item["key"]: item for item in view.get("items") or []
              if item.get("key") in RULE_KEYS}
    return {"ok": True, "keys": list(RULE_KEYS),
            "items": [by_key[key] for key in RULE_KEYS if key in by_key],
            "path": view.get("path", ""), "mtime": view.get("mtime")}


def save_rules(patch: dict, *, actor: str = "workbench") -> dict:
    if not isinstance(patch, dict) or not patch:
        return {"ok": False, "error": "empty_patch"}
    unknown = [key for key in patch if key not in RULE_KEYS]
    if unknown:
        return {"ok": False, "error": "not_allowed", "keys": unknown}
    result = config_store.save(patch, actor=actor, auto_commit=False)
    if result.get("ok"):
        result["items"] = [item for item in config_store.load_view().get("items") or []
                           if item.get("key") in patch]
    return result


# ------------------------------------------------------------------ 衰减参数
def decay_params() -> dict:
    _, md = _soul()
    view = config_store.load_view()
    current = {item["key"]: item.get("value") for item in view.get("items") or []
               if item.get("key") in RULE_KEYS}
    return {
        "ok": True,
        "half_life_days": dict(md.HALF_LIFE_DAYS),
        "default_half_life": md.DEFAULT_HALF_LIFE,
        "stage_thresholds": {"full": 0.5, "compressed": 0.2,
                             "keyword": 0.05, "dormant": 0.0},
        "importance_multipliers": [
            {"min": 0.8, "multiplier": 2.0},
            {"min": 0.3, "multiplier": 1.0},
            {"min": 0.0, "multiplier": 0.5},
        ],
        "revive_strength": md.REVIVE_STRENGTH,
        "purge_after_days": md.DORMANT_PURGE_DAYS,
        "rules": current,
        "readonly": ["half_life_days", "stage_thresholds",
                     "importance_multipliers", "purge_after_days"],
        "writable": list(RULE_KEYS),
    }


# ------------------------------------------------------------------ 操作
def revive_memory(memory_id: str, contact: str | None = None,
                  *, boost: float | None = None) -> dict:
    _, md = _soul()
    base = _resolve_base(contact)
    kwargs = {"boost": float(boost)} if boost is not None else {}
    try:
        result = md.revive(base, str(memory_id or ""), **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if result.get("ok"):
        result["memory"] = get_memory(memory_id, contact).get("memory")
        result["contact"] = contact or ""
    return result


def purge_dry_run(contact: str | None = None, *, days: int | None = None) -> dict:
    _, md = _soul()
    kwargs = {"days": int(days)} if days is not None else {}
    result = md.purge_dormant(_resolve_base(contact), dry_run=True, **kwargs)
    result["contact"] = contact or ""
    result["confirm_required"] = PURGE_CONFIRM
    return result


def purge(contact: str | None = None, *, confirm: str = "",
          days: int | None = None) -> dict:
    if str(confirm or "").strip() != PURGE_CONFIRM:
        return {"ok": False, "error": "confirm_required",
                "confirm_required": PURGE_CONFIRM}
    _, md = _soul()
    kwargs = {"days": int(days)} if days is not None else {}
    result = md.purge_dormant(_resolve_base(contact), dry_run=False, **kwargs)
    result["contact"] = contact or ""
    return result


# ------------------------------------------------------------------ 时间线
def timeline(contact: str | None = None, *, days: int = 30) -> dict:
    con = _ro(contact)
    if con is None:
        return {"ok": False, "error": "memory_db_not_found", "items": []}
    try:
        try:
            days = max(1, min(365, int(days)))
        except (TypeError, ValueError):
            days = 30
        now = time.time()
        cutoff = now - days * 86400
        cols = _columns(con)
        rows = con.execute(
            "SELECT * FROM user_memories WHERE created_at>=?", (cutoff,)).fetchall()
    finally:
        con.close()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        data = dict(row)
        day = time.strftime("%Y-%m-%d",
                            time.localtime(float(data.get("created_at") or 0)))
        grouped.setdefault(day, []).append(data)
    items = []
    for day in sorted(grouped):
        values = grouped[day]
        items.append({"date": day, "count": len(values),
                      "avg_strength": round(sum(_strength(v) for v in values)
                                            / max(1, len(values)), 4)})
    return {"ok": True, "contact": contact or "", "days": days, "items": items}


#: main.py 接线清单（由 Lead 统一注册）
ROUTES = (
    ("GET", "/api/wb/mem/overview", "overview(contact)"),
    ("GET", "/api/wb/mem/list", "list_memories(contact,kind,stage,q,limit,offset)"),
    ("GET", "/api/wb/mem/get", "get_memory(id,contact)"),
    ("GET", "/api/wb/mem/rules", "rules()"),
    ("POST", "/api/wb/mem/rules", "save_rules(patch)"),
    ("GET", "/api/wb/mem/decay", "decay_params()"),
    ("POST", "/api/wb/mem/decay", "save_rules(patch)"),
    ("POST", "/api/wb/mem/revive", "revive_memory(id,contact,boost)"),
    ("POST", "/api/wb/mem/purge-dry-run", "purge_dry_run(contact,days)"),
    ("POST", "/api/wb/mem/purge", "purge(contact,confirm,days)"),
    ("GET", "/api/wb/mem/timeline", "timeline(contact,days)"),
)
