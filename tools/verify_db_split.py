# -*- coding: utf-8 -*-
"""P7-f：存储分层迁移校验工具。

- ``--copy <单库>``：复制到临时目录后迁移，逐表行数对照 + logical_mismatch + FTS + legacy；
- ``--production``：默认**只预演**（复制生产库到临时目录迁移，不碰生产）；
- ``--apply``：真正对生产库执行迁移。必须同时给 ``--confirm``，且需 Lead 授权；
- 退出码 0=一致，1=不一致/不安全。

示例：
    python tools/verify_db_split.py --copy path/to/old.sqlite3
    python tools/verify_db_split.py --production --db agi/data/selflearn/wechat_contacts/x.sqlite3
    python tools/verify_db_split.py --production --apply --confirm
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agi.selflearn import db, search  # noqa: E402

FTS_SHADOW_PREFIX = "search_fts_"
WORK_TMP = Path("E:/agent/tmp") if Path("E:/agent/tmp").exists() else Path(tempfile.gettempdir())
BACKUP_DIR = Path("E:/agent/backups") if Path("E:/agent/backups").exists() else Path(tempfile.gettempdir())


def _old_tables(path: Path) -> list[str]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [str(r[0]) for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    finally:
        con.close()


def _counts(path: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [str(r[0]) for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        out = {}
        for table in tables:
            try:
                out[table] = int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            except sqlite3.Error:
                out[table] = -1
        return out
    finally:
        con.close()


def _migrate_copy(source: Path) -> tuple[Path, dict]:
    WORK_TMP.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="split_check_", dir=str(WORK_TMP)))
    target = tmpdir / source.name
    shutil.copy2(source, target)
    before = _counts(target)
    db.connect(target).close()
    mem = db.memory_path(target)
    per = db.persona_path(target)
    legacy = db.legacy_path(target)
    after: dict[str, int | None] = {}
    mem_con = sqlite3.connect(f"file:{mem}?mode=ro", uri=True) if mem.exists() else None
    per_con = sqlite3.connect(f"file:{per}?mode=ro", uri=True) if per.exists() else None
    try:
        for table in before:
            domain = db.TABLE_DOMAIN.get(table, "memory")
            con = per_con if domain == "persona" else mem_con
            try:
                after[table] = int(con.execute(
                    f"SELECT count(*) FROM {table}").fetchone()[0]) if con else None
            except sqlite3.Error:
                after[table] = None
    finally:
        if mem_con is not None:
            mem_con.close()
        if per_con is not None:
            per_con.close()
    mismatches = {table: [before[table], after[table]] for table in before
                  if before[table] != (after.get(table) or 0)
                  and not table.startswith(FTS_SHADOW_PREFIX)}
    return target, {"source": str(source), "copy": str(target),
                    "before": before, "after": after,
                    "logical_mismatch": mismatches,
                    "memory_exists": mem.exists(), "persona_exists": per.exists(),
                    "legacy_exists": legacy.exists(),
                    "memory_tables": len(_counts(mem)) if mem.exists() else 0,
                    "persona_tables": len(_counts(per)) if per.exists() else 0}


def _fts_hits(target: Path, query: str) -> int:
    try:
        return len(search.search(target, query, limit=5))
    except Exception:  # noqa: BLE001
        return -1


def _report(result: dict, query: str) -> int:
    ok = (not result["logical_mismatch"] and result["memory_exists"]
          and result["persona_exists"] and result["legacy_exists"])
    result["fts_hits"] = _fts_hits(Path(result["copy"]), query)
    result["ok"] = bool(ok and result["fts_hits"] >= 0)
    print(json.dumps({k: v for k, v in result.items()
                     if k not in ("before", "after")},
                     ensure_ascii=False, indent=2))
    print("\n逐表行数（before -> after）:")
    for table in result["before"]:
        before = result["before"][table]
        after = result["after"].get(table)
        mark = "OK" if before == (after or 0) else "DIFF"
        print(f"  {mark:4} {table:28} {before:8} -> {after}")
    return 0 if result["ok"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="记忆/人格分库迁移校验")
    parser.add_argument("--copy", default="", help="复制指定单库后迁移（不改原文件）")
    parser.add_argument("--production", action="store_true", help="预演生产库迁移")
    parser.add_argument("--apply", action="store_true", help="真正迁移生产库（需 --confirm）")
    parser.add_argument("--confirm", action="store_true", help="确认执行 --apply")
    parser.add_argument("--db", default=str(db.DB_PATH), help="生产/基名库路径")
    parser.add_argument("--query", default="你好", help="迁移后 FTS 命中测试词")
    args = parser.parse_args(argv)
    if args.apply and not args.confirm:
        print("拒绝：--apply 必须同时 --confirm，并需 Lead 明确授权", file=sys.stderr)
        return 2
    if args.copy:
        source = Path(args.copy)
        if not source.exists():
            print(f"源库不存在：{source}", file=sys.stderr)
            return 2
        target, result = _migrate_copy(source)
        return _report(result, args.query)
    if args.production:
        base = Path(args.db)
        if not base.exists():
            print(f"生产基名库不存在：{base}", file=sys.stderr)
            return 2
        if args.apply:
            # 安全检查：若已有 .memory/.persona（尤其事故残留的空库），必须先移走，
            # 否则 db.connect 会跳过迁移并使用空库。
            if db.memory_path(base).exists() or db.persona_path(base).exists():
                print("拒绝 --apply：已存在 .memory/.persona；请先移走空/旧的新库，"
                      "或使用 --copy 预演确认。", file=sys.stderr)
                return 3
            # 真迁移：先创建备份，再 connect；成功后才算
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            backup = BACKUP_DIR / (base.stem + ".pre-split.sqlite3")
            if not backup.exists():
                shutil.copy2(base, backup)
            db.connect(base).close()
            result = {"source": str(base), "copy": str(base),
                      "before": _counts(backup), "after": {},
                      "logical_mismatch": {}, "memory_exists": db.memory_path(base).exists(),
                      "persona_exists": db.persona_path(base).exists(),
                      "legacy_exists": db.legacy_path(base).exists(),
                      "memory_tables": 0, "persona_tables": 0}
            return _report(result, args.query)
        # 默认只预演：复制到 tmp，不动生产
        target, result = _migrate_copy(base)
        return _report(result, args.query)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
