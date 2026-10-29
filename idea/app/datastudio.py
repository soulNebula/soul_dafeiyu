# -*- coding: utf-8 -*-
"""M1-A Data Studio 后端：SQLite 发现 / 表浏览 / 分页 / 只读 SQL / 写 SQL / 行编辑 / 备份。

只提供纯函数（路由由 Lead 统一接 main.py，本任务不改 main.py/web）。
安全边界：
- 只允许 SOULAGENT_HOME 与 idea/.data 下的 .sqlite3；
- 读连接一律 ``file:...?mode=ro``；禁用 ATTACH/DETACH；SQL ≤20KB；
- 写 SQL 必须确认短语「确认写入」；写前自动快照到 idea/.data/db_backups/；写操作 audit.record。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import audit, paths

SQLITE_MAGIC = b"SQLite format 3\x00"
SQL_MAX_BYTES = 20 * 1024
CONFIRM_WRITE = "确认写入"
PAGE_MAX = 500
READ_ROW_MAX = 1000
BACKUP_DIR = paths.DATA_DIR / "db_backups"
_IDENT_RE = re.compile(r"^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*$")
_ATTACH_RE = re.compile(r"\b(ATTACH|DETACH)\b", re.I)
_WRITE_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|VACUUM|REINDEX|ANALYZE)\b", re.I)
_READ_RE = re.compile(r"^\s*(SELECT|WITH|EXPLAIN|PRAGMA|VALUES)\b", re.I)


def _roots() -> list[Path]:
    roots = [Path(paths.soulagent_home()).resolve(), Path(paths.DATA_DIR).resolve()]
    # 活跃 Bot 的 data 目录也纳入数据页（Bot 数据库在 <Bot>/data/selflearn）
    try:
        import sys as _sys
        from pathlib import Path as _Path
        repo = _Path(__file__).resolve().parents[2]
        if str(repo) not in _sys.path:
            _sys.path.insert(0, str(repo))
        from agi import bot_runtime
        active = bot_runtime.read_active()
        if active and active.get("path"):
            roots.append((_Path(active["path"]) / "data").resolve())
    except Exception:  # noqa: BLE001
        pass
    out: list[Path] = []
    for root in roots:
        if root not in out:
            out.append(root)
    return out


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _allowed_path(path: Path) -> bool:
    return any(_is_under(path, root) for root in _roots())


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(16) == SQLITE_MAGIC
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 数据文件友好化：给普通用户看得懂的名字，测试/备份/旧库默认折叠
# --------------------------------------------------------------------------- #
_DOMAIN_LABELS = {
    "audit": "操作审计",
    "memory": "聊天记忆",
    "persona": "人物画像",
    "wechat": "微信数据",
    "idea": "工作台数据",
    "moments": "朋友圈数据",
    "train": "训练数据",
}
#: 路径里出现这些片段 → 视为测试/临时/旧库，默认隐藏
_HIDDEN_MARKERS = ("chattrials", "pytest-", "console_test", "_mood_probe", "legacy",
                   "db_backups", ".trash", "\tmp\\", "/tmp/", "scratch")


def _known_contacts() -> dict[str, str]:
    """微信配置里的联系人名 → sha1(name)[:14]（= 每联系人库文件名前缀）。"""
    cfg_path = Path(paths.soulagent_home()) / "agi" / "data" / "wechat" / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    names: set[str] = set()
    for key in ("allowed_contacts", "monitored_contacts", "proactive_contacts",
                "friends", "contacts"):
        for name in cfg.get(key) or []:
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    for key in ("contact_modes", "contact_profiles", "contact_nicknames"):
        mapping = cfg.get(key)
        if isinstance(mapping, dict):
            names.update(str(k).strip() for k in mapping if str(k).strip())
    return {hashlib.sha1(name.encode("utf-8")).hexdigest()[:14]: name for name in names}


_CONTACT_CACHE: dict[str, str] | None = None


def _contact_of(path: Path, domain: str) -> str:
    """联系人名：优先 sessions.friend，其次按库文件名sha1前缀反查微信联系人。"""
    if domain not in ("memory", "persona"):
        return ""
    try:
        con = sqlite3.connect(f"file:{quote(str(path).replace(chr(92), '/'))}?mode=ro", uri=True, timeout=3)
        try:
            row = con.execute("SELECT friend FROM sessions WHERE friend IS NOT NULL "
                              "AND friend != '' LIMIT 1").fetchone()
            if row and str(row[0]).strip():
                return str(row[0]).strip()
        finally:
            con.close()
    except sqlite3.Error:
        pass
    global _CONTACT_CACHE
    if _CONTACT_CACHE is None:
        _CONTACT_CACHE = _known_contacts()
    stem = path.name.split(".")[0]
    if re.fullmatch(r"[0-9a-f]{14}", stem):
        return _CONTACT_CACHE.get(stem, "")
    return ""


def _friendly(path: Path, domain: str) -> dict:
    lowered = str(path).lower()
    name = path.name
    hidden = any(marker in lowered for marker in _HIDDEN_MARKERS)
    hidden = hidden or bool(re.match(r"^audit_\d{8}_", name)) or name.startswith("_")
    base = _DOMAIN_LABELS.get(domain) or "其它数据"
    contact = _contact_of(path, domain)
    # sha1 前缀的库但已反查不到联系人 = 旧联系人/已删除会话的库 → 默认折叠
    if not hidden and not contact and re.fullmatch(r"[0-9a-f]{14}\.\w+\.sqlite3", name):
        hidden = True
        base = base + "（旧联系人库）"
    if contact:
        label = f"{base} · {contact}"
    elif domain in ("memory", "persona") and not hidden and "." in name             and not re.fullmatch(r"[0-9a-f]{14}.*", name):
        label = f"{base}（默认库）"
    elif hidden:
        label = f"{base}（临时/备份）"
    else:
        label = base
    return {"label_zh": label, "contact": contact, "hidden": hidden}


def _domain_of(path: Path) -> str:
    name = path.name.lower()
    if "audit" in name:
        return "audit"
    if ".memory." in name:
        return "memory"
    if ".persona." in name:
        return "persona"
    return "other"


def database_id(path: Path | str) -> str:
    return str(Path(path).resolve())


def resolve_database(db: str) -> tuple[Path | None, str]:
    """把前端传的绝对/相对路径解析成允许的 .sqlite3；返回 (path, error)。"""
    raw = str(db or "").strip()
    if not raw:
        return None, "database_required"
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        tried = [Path(paths.soulagent_home()) / raw, Path(paths.DATA_DIR) / raw]
        candidate = next((item for item in tried if item.exists()), tried[0])
    try:
        target = candidate.resolve()
    except OSError:
        return None, "bad_path"
    if target.suffix.lower() != ".sqlite3" or not target.is_file():
        return None, "not_a_sqlite_file"
    if not _allowed_path(target):
        return None, "path_out_of_allowed_roots"
    if not _is_sqlite(target):
        return None, "not_a_sqlite_file"
    return target, ""


def _scan(root: Path, *, under: Path | None = None) -> list[Path]:
    base = under or root
    if not base.is_dir():
        return []
    found = []
    try:
        for path in base.rglob("*.sqlite3"):
            try:
                if path.is_file() and _is_sqlite(path):
                    found.append(path.resolve())
            except OSError:
                continue
    except OSError:
        return found
    return found


def list_databases() -> dict:
    """发现 SoulAgent 的 agi/data 与 idea/.data 下的 SQLite。"""
    soul = Path(paths.soulagent_home()).resolve()
    data = Path(paths.DATA_DIR).resolve()
    candidates = _scan(soul, under=soul / "agi" / "data")
    candidates += _scan(data)
    # 活跃 Bot 的 data 目录：Bot 数据库/记忆都在这里，数据页也要能浏览
    active_data = None
    try:
        from agi import bot_runtime
        active = bot_runtime.read_active()
        if active and active.get("path"):
            active_data = (Path(active["path"]) / "data").resolve()
            candidates += _scan(active_data)
    except Exception:  # noqa: BLE001
        active_data = None
    seen: set[str] = set()
    items = []
    for path in sorted(candidates, key=lambda p: str(p).lower()):
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            stat = path.stat()
        except OSError:
            continue
        try:
            label = path.relative_to(soul).as_posix()
        except ValueError:
            try:
                label = path.relative_to(data).as_posix()
            except ValueError:
                label = path.relative_to(active_data).as_posix() if active_data else path.name
            except ValueError:
                label = path.name
        domain = _domain_of(path)
        friendly = _friendly(path, domain)
        items.append({"id": str(path), "name": path.name, "label": label,
                      "path": str(path), "size": stat.st_size,
                      "mtime": stat.st_mtime, "domain": domain,
                      "root": "soulagent" if _is_under(path, soul) else "idea",
                      **friendly})
    _priority = {"audit": 0, "memory": 1, "persona": 2}
    items.sort(key=lambda it: (bool(it.get("hidden")),
                               _priority.get(it.get("domain") or "", 9),
                               str(it.get("contact") or ""),
                               str(it.get("label_zh") or "")))
    return {"ok": True, "items": items, "count": len(items),
            "allowed_roots": [str(item) for item in _roots()]}


def _ro_uri(path: Path) -> str:
    return "file:" + quote(str(path).replace("\\", "/")) + "?mode=ro"


def _ro_connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(_ro_uri(path), uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row
    return con


def _rw_connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=10.0)
    con.row_factory = sqlite3.Row
    return con


def _quote_ident(name: str) -> str:
    value = str(name or "")
    if not _IDENT_RE.match(value):
        raise ValueError("invalid_identifier")
    return '"' + value.replace('"', '""') + '"'


def _tables(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        "SELECT name,type,sql FROM sqlite_master "
        "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [{"name": row["name"], "type": row["type"], "sql": row["sql"]} for row in rows]


def _table_info(con: sqlite3.Connection, table: str) -> list[dict]:
    _quote_ident(table)
    rows = con.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    return [{"cid": row["cid"], "name": row["name"], "type": row["type"],
             "notnull": bool(row["notnull"]), "default": row["dflt_value"],
             "pk": int(row["pk"] or 0)} for row in rows]


def schema(db: str) -> dict:
    path, error = resolve_database(db)
    if not path:
        return {"ok": False, "error": error}
    con = _ro_connect(path)
    try:
        tables = _tables(con)
        for table in tables:
            try:
                cols = _table_info(con, table["name"])
                table["columns"] = cols
                table["primary_key"] = [col["name"] for col in cols if col["pk"]]
                if table["type"] == "table":
                    count = con.execute(
                        f"SELECT count(*) FROM {_quote_ident(table['name'])}").fetchone()[0]
                    table["rows"] = int(count)
            except sqlite3.Error as exc:
                table["error"] = str(exc)[:200]
        return {"ok": True, "db": str(path), "tables": tables}
    finally:
        con.close()


def read_rows(db: str, table: str, *, page: int = 1, page_size: int = 50,
              order_by: str = "", direction: str = "asc",
              filters: dict | None = None, q: str = "") -> dict:
    path, error = resolve_database(db)
    if not path:
        return {"ok": False, "error": error}
    try:
        page = max(1, int(page))
        page_size = max(1, min(PAGE_MAX, int(page_size)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_paging"}
    con = _ro_connect(path)
    try:
        infos = _table_info(con, table)
        if not infos:
            return {"ok": False, "error": "table_not_found"}
        cols = [col["name"] for col in infos]
        where, params = "", []
        for key, value in (filters or {}).items():
            if key not in cols:
                return {"ok": False, "error": f"unknown_filter:{key}"}
            where += (" AND " if where else " WHERE ") + f"{_quote_ident(key)}=?"
            params.append(value)
        if str(q or "").strip():
            text_cols = [col["name"] for col in infos
                         if "CHAR" in str(col["type"] or "").upper()
                         or "TEXT" in str(col["type"] or "").upper()
                         or not str(col["type"] or "").strip()]
            if not text_cols:
                return {"ok": False, "error": "no_text_column_for_q"}
            clauses = " OR ".join(f"{_quote_ident(col)} LIKE ?" for col in text_cols)
            where += (" AND " if where else " WHERE ") + "(" + clauses + ")"
            params.extend([f"%{str(q).strip()}%"] * len(text_cols))
        order = ""
        if order_by:
            if order_by not in cols:
                return {"ok": False, "error": "unknown_order_by"}
            direction = "DESC" if str(direction).lower() == "desc" else "ASC"
            order = f" ORDER BY {_quote_ident(order_by)} {direction}"
        total = con.execute(
            f"SELECT count(*) FROM {_quote_ident(table)}{where}", params).fetchone()[0]
        rows = con.execute(
            f"SELECT * FROM {_quote_ident(table)}{where}{order} LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size]).fetchall()
        return {"ok": True, "db": str(path), "table": table,
                "columns": infos, "rows": [dict(row) for row in rows],
                "page": page, "page_size": page_size, "total": int(total),
                "pages": max(1, (int(total) + page_size - 1) // page_size),
                "primary_key": [col["name"] for col in infos if col["pk"]]}
    except ValueError:
        return {"ok": False, "error": "invalid_identifier"}
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"sqlite:{str(exc)[:200]}"}
    finally:
        con.close()


def _sql_kind(sql: str) -> str:
    value = str(sql or "").strip()
    if not value:
        return ""
    if _WRITE_RE.match(value):
        return "write"
    if _READ_RE.match(value):
        return "read"
    return ""


def _check_sql(sql: str) -> str:
    value = str(sql or "").strip()
    if not value:
        return "sql_required"
    if len(value.encode("utf-8")) > SQL_MAX_BYTES:
        return "sql_too_large"
    if _ATTACH_RE.search(value):
        return "attach_not_allowed"
    # 只允许单条语句（保守：分号只允许在末尾）
    body = value.rstrip(";").strip()
    if ";" in body:
        return "multiple_statements_not_allowed"
    if not _sql_kind(body):
        return "unsupported_statement"
    return ""


def _backup_path(db_path: Path) -> Path:
    target_dir = Path(BACKUP_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    digest = hashlib.sha1(str(db_path).encode("utf-8")).hexdigest()[:8]
    return target_dir / f"{db_path.stem}.{stamp}.{digest}.sqlite3"


def backup_database(db: str, *, actor: str = "local", reason: str = "manual") -> dict:
    path, error = resolve_database(db)
    if not path:
        return {"ok": False, "error": error}
    target = _backup_path(path)
    try:
        source = _ro_connect(path)
        try:
            dest = sqlite3.connect(str(target))
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
        audit.record("database.backup", str(path), actor=actor,
                     meta={"backup": str(target), "reason": reason})
        return {"ok": True, "db": str(path), "backup": str(target),
                "size": target.stat().st_size}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def list_backups(limit: int = 50) -> dict:
    target_dir = Path(BACKUP_DIR)
    items = []
    if target_dir.is_dir():
        for path in sorted(target_dir.glob("*.sqlite3"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, int(limit))]:
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append({"name": path.name, "path": str(path),
                          "size": stat.st_size, "mtime": stat.st_mtime})
    return {"ok": True, "items": items, "count": len(items)}


def run_sql(db: str, sql: str, *, confirm: str = "", write: bool = False,
            actor: str = "local") -> dict:
    path, error = resolve_database(db)
    if not path:
        return {"ok": False, "error": error}
    sql = str(sql or "").strip()
    check = _check_sql(sql)
    if check:
        return {"ok": False, "error": check}
    kind = _sql_kind(sql)
    if kind == "write" and not write:
        return {"ok": False, "error": "write_not_allowed",
                "hint": "写语句需要 write=true（并带 confirm 确认短语）"}
    if kind == "write" and str(confirm or "").strip() != CONFIRM_WRITE:
        return {"ok": False, "error": "confirm_required",
                "hint": f"写操作需要确认短语「{CONFIRM_WRITE}」"}
    backup = None
    if kind == "write":
        snapshot = backup_database(str(path), actor=actor, reason="before_sql_write")
        if not snapshot.get("ok"):
            return {"ok": False, "error": "backup_failed", "detail": snapshot}
        backup = snapshot.get("backup")
    started = time.time()
    try:
        con = _rw_connect(path) if kind == "write" else _ro_connect(path)
        try:
            cursor = con.execute(sql)
            if kind == "write":
                con.commit()
            if cursor.description:
                columns = [item[0] for item in cursor.description]
                rows = [dict(zip(columns, row)) for row in cursor.fetchmany(READ_ROW_MAX)]
                total = len(rows)
                return {"ok": True, "kind": "write" if kind == "write" else "read",
                        "columns": columns, "rows": rows, "rowcount": total,
                        "truncated": total >= READ_ROW_MAX,
                        "backup": backup,
                        "elapsed_ms": round((time.time() - started) * 1000, 2)}
            rowcount = int(cursor.rowcount if cursor.rowcount is not None else 0)
            if kind == "write":
                audit.record("database.sql_write", str(path), actor=actor,
                             after={"sql": sql[:2000], "rowcount": rowcount},
                             meta={"backup": backup})
            return {"ok": True, "kind": "write" if kind == "write" else "read",
                    "columns": [], "rows": [], "rowcount": rowcount,
                    "backup": backup,
                    "elapsed_ms": round((time.time() - started) * 1000, 2)}
        finally:
            con.close()
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"sqlite:{str(exc)[:300]}",
                "backup": backup}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "backup": backup}


def update_row(db: str, table: str, keys: dict, values: dict, *,
               confirm: str = "", actor: str = "local") -> dict:
    path, error = resolve_database(db)
    if not path:
        return {"ok": False, "error": error}
    if str(confirm or "").strip() != CONFIRM_WRITE:
        return {"ok": False, "error": "confirm_required",
                "hint": f"行编辑需要确认短语「{CONFIRM_WRITE}」"}
    if not isinstance(keys, dict) or not keys or not isinstance(values, dict) or not values:
        return {"ok": False, "error": "keys_values_required"}
    con = _ro_connect(path)
    try:
        infos = _table_info(con, table)
    finally:
        con.close()
    if not infos:
        return {"ok": False, "error": "table_not_found"}
    cols = [col["name"] for col in infos]
    pk = [col["name"] for col in infos if col["pk"]]
    if not pk:
        return {"ok": False, "error": "table_has_no_primary_key"}
    if set(keys) != set(pk):
        return {"ok": False, "error": "primary_key_required",
                "primary_key": pk}
    for name in list(keys) + list(values):
        if name not in cols:
            return {"ok": False, "error": f"unknown_column:{name}"}
    if any(name in pk for name in values):
        return {"ok": False, "error": "cannot_update_primary_key"}
    snapshot = backup_database(str(path), actor=actor, reason="before_row_edit")
    if not snapshot.get("ok"):
        return {"ok": False, "error": "backup_failed", "detail": snapshot}
    where = " AND ".join(f"{_quote_ident(name)}=?" for name in keys)
    params = list(keys.values())
    try:
        con = _rw_connect(path)
        try:
            before_rows = [dict(row) for row in con.execute(
                f"SELECT * FROM {_quote_ident(table)} WHERE {where}", params).fetchall()]
            if len(before_rows) != 1:
                return {"ok": False, "error": "row_not_unique_or_not_found",
                        "matched": len(before_rows), "backup": snapshot.get("backup")}
            assignments = ", ".join(f"{_quote_ident(name)}=?" for name in values)
            con.execute(
                f"UPDATE {_quote_ident(table)} SET {assignments} WHERE {where}",
                [*values.values(), *params])
            con.commit()
            after = dict(before_rows[0])
            after.update(values)
            audit.record("db.update", f"{path}:{table}",
                         actor=actor,
                         before=before_rows[0], after=after,
                         meta={"keys": keys, "backup": snapshot.get("backup")})
            return {"ok": True, "db": str(path), "table": table,
                    "before": before_rows[0], "after": after,
                    "backup": snapshot.get("backup")}
        finally:
            con.close()
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"sqlite:{str(exc)[:300]}",
                "backup": snapshot.get("backup")}
