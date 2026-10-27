# -*- coding: utf-8 -*-
"""P23：把误落素材库的聊天图片迁移到 media_chat，并清理误导入的 media_assets。

默认 dry-run；真实执行需 --apply。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

from agi.selflearn import config, db

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LIBRARY = config.DATA_DIR / "media"
DEFAULT_CHAT = config.DATA_DIR / "media_chat"
DEFAULT_BACKUP = Path("E:/agent/backups")


def _norm(value) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(value or "")))
    except Exception:  # noqa: BLE001
        return str(value or "")


def _under(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


def _asset_rows(con) -> list[dict]:
    try:
        return [dict(row) for row in con.execute(
            "SELECT id,path,builtin FROM media_assets").fetchall()]
    except Exception:  # noqa: BLE001
        return []


def _target_for(src: Path, chat_dir: Path, sha: str = "") -> Path:
    target = chat_dir / src.name
    if not target.exists():
        return target
    # 同名冲突：用 sha 前缀区分；内容相同时直接复用
    try:
        if src.read_bytes() == target.read_bytes():
            return target
    except OSError:
        pass
    suffix = "".join(src.suffixes)
    stem = src.name[:-len(suffix)] if suffix else src.name
    digest = sha or __import__("hashlib").sha256(src.read_bytes()).hexdigest()
    return chat_dir / f"{stem}-{digest[:12]}{suffix}"


def migrate(*, db_path: Path | str | None = None,
            library_db: Path | str | None = None,
            library_dir: Path | str | None = None,
            chat_dir: Path | str | None = None,
            dry_run: bool = True,
            backup_dir: Path | str | None = None,
            now: float | None = None) -> dict:
    library = Path(library_dir) if library_dir else DEFAULT_LIBRARY
    chat = Path(chat_dir) if chat_dir else DEFAULT_CHAT
    base = Path(db_path) if db_path else Path(config.DB_PATH)
    lib_base = Path(library_db) if library_db else base
    now = time.time() if now is None else float(now)

    astore = db.connect(base)
    try:
        attachments = [dict(row) for row in astore.execute(
            "SELECT id,path FROM attachments").fetchall()]
    finally:
        astore.close()

    source_rows = [item for item in attachments
                   if item.get("path") and _under(Path(str(item["path"])), library)]
    assets_con = db.connect(lib_base)
    try:
        assets = _asset_rows(assets_con)
    finally:
        assets_con.close()
    source_norm = {_norm(item["path"]) for item in source_rows}

    moved: list[dict] = []
    missing: list[dict] = []
    for item in source_rows:
        src = Path(str(item["path"]))
        target = _target_for(src, chat, str(item.get("sha256") or ""))
        action = "move" if src.exists() else "missing"
        if action == "missing":
            missing.append({"id": item["id"], "source": str(src), "target": str(target)})
        moved.append({"id": item["id"], "source": str(src), "target": str(target),
                      "action": action})
    deleted_assets = [asset for asset in assets
                      if _norm(asset.get("path")) in source_norm]
    remaining_assets = len(assets) - len(deleted_assets)

    backup_path = ""
    if not dry_run:
        chat.mkdir(parents=True, exist_ok=True)
        for item in moved:
            if item["action"] != "move":
                continue
            src = Path(item["source"])
            target = Path(item["target"])
            if src != target:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.read_bytes() != src.read_bytes():
                    target = _target_for(src, chat)
                shutil.move(str(src), str(target))
                item["target"] = str(target)
        # 更新 attachments 路径（missing 也迁移语义路径，便于后续补文件）
        astore = db.connect(base)
        try:
            for item in moved:
                astore.execute("UPDATE attachments SET path=? WHERE id=?",
                               (item["target"], item["id"]))
            astore.commit()
        finally:
            astore.close()
        # 删除误导入的 media_assets 行
        assets_con = db.connect(lib_base)
        try:
            for asset in deleted_assets:
                assets_con.execute("DELETE FROM media_assets WHERE id=?", (asset["id"],))
            assets_con.commit()
        finally:
            assets_con.close()
        # 回溯备份
        target_dir = Path(backup_dir) if backup_dir else DEFAULT_BACKUP
        target_dir.mkdir(parents=True, exist_ok=True)
        backup_path = str(target_dir / f"chat_media_migration_{int(now)}.json")
        Path(backup_path).write_text(json.dumps({
            "at": now, "library_dir": str(library), "chat_dir": str(chat),
            "db": str(base), "library_db": str(lib_base),
            "moved": moved, "deleted_assets": deleted_assets,
            "missing": missing, "remaining_assets": remaining_assets,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"ok": True, "dry_run": bool(dry_run),
            "scanned_attachments": len(attachments),
            "move_files": sum(1 for item in moved if item["action"] == "move"),
            "missing_files": len(missing),
            "update_attachments": len(moved),
            "delete_assets": len(deleted_assets),
            "remaining_assets": remaining_assets,
            "backup_path": backup_path,
            "items": moved,
            "missing": missing,
            "deleted_asset_ids": [asset["id"] for asset in deleted_assets]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.migrate_chat_media")
    parser.add_argument("--db", default="", help="attachments 所在库基名")
    parser.add_argument("--library-db", default="", help="media_assets 所在库基名")
    parser.add_argument("--library-dir", default="")
    parser.add_argument("--chat-dir", default="")
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--apply", action="store_true", help="真实移动/删除（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认）")
    args = parser.parse_args(argv)
    result = migrate(
        db_path=args.db or None,
        library_db=args.library_db or None,
        library_dir=args.library_dir or None,
        chat_dir=args.chat_dir or None,
        dry_run=not bool(args.apply),
        backup_dir=args.backup_dir or None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
