#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V1 迁移：把旧的全局数据目录复制成 ``bots/<id>/`` 工作区（幂等、只复制不删除）。

用法::

    python scripts/migrate_to_bot.py                 # 只打印计划（dry-run）
    python scripts/migrate_to_bot.py --apply         # 真正复制到 bots/default/
    python scripts/migrate_to_bot.py --apply --target bots/mybot

安全约定：
- **只复制、不移动、不删除**旧数据；旧路径仍是回滚点；
- 目标已存在的文件默认跳过；`--force` 才覆盖；
- 建议停 801/802 后执行（SQLite WAL 才会一致）；运行中执行只保证"尽力复制"。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agi.paths import data_dir as _data_dir  # noqa: E402


def _repo_web_config() -> Path | None:
    for candidate in (ROOT / "agi" / "selflearn" / "web_config.json",
                      ROOT / "web_config.json"):
        if candidate.is_file():
            return candidate
    return None


def build_plan(legacy_root: Path | str, target_root: Path | str) -> list[dict]:
    """返回 ``[{src, dst, kind}]``；只列实际存在的源文件。"""
    legacy = Path(legacy_root)
    target = Path(target_root)
    entries: list[dict] = []

    def add(src: Path, dst: Path, kind: str) -> None:
        if src.is_file():
            entries.append({"src": src, "dst": dst, "kind": kind})

    add(legacy / "wechat" / "config.json",
        target / "config" / "wechat.json", "wechat_config")
    web = _repo_web_config()
    if web is not None:
        add(web, target / "config" / "app.json", "app_config")
    add(legacy / "selflearn" / "selflearn.sqlite3",
        target / "db" / "selflearn" / "selflearn.sqlite3", "selflearn_db")
    contacts = legacy / "selflearn" / "wechat_contacts"
    if contacts.is_dir():
        for src in sorted(contacts.glob("*.sqlite3*")):
            add(src, target / "db" / "selflearn" / "wechat_contacts" / src.name,
                "contact_db")
    return entries


def execute(plan: list[dict], *, apply: bool = False, force: bool = False) -> dict:
    copied: list[str] = []
    skipped: list[str] = []
    for entry in plan:
        src, dst = Path(entry["src"]), Path(entry["dst"])
        if dst.exists() and not force:
            skipped.append(str(dst))
            continue
        if not apply:
            copied.append(str(dst))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(str(dst))
    return {"ok": True, "apply": apply, "copied": copied, "skipped": skipped}


def write_bot_meta(target_root: Path | str, *, force: bool = False) -> Path:
    """写 ``bots/<id>/bot.json``（不覆盖已有）。"""
    target = Path(target_root)
    meta = target / "bot.json"
    if meta.exists() and not force:
        return meta
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({
        "id": target.name,
        "created_at": time.time(),
        "migrated_from": str(_repo_web_config() or ""),
        "version": 1,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把旧全局数据复制成 bots/<id>/ 工作区")
    parser.add_argument("--legacy", default=str(_data_dir()),
                        help="旧数据根（默认 agi.paths.data_dir()）")
    parser.add_argument("--target", default=str(ROOT / "bots" / "default"),
                        help="bot 工作区目标目录（默认 bots/default）")
    parser.add_argument("--apply", action="store_true", help="真正复制（默认 dry-run）")
    parser.add_argument("--force", action="store_true", help="覆盖目标已存在文件")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)
    plan = build_plan(args.legacy, args.target)
    result = execute(plan, apply=args.apply, force=args.force)
    if args.apply and plan:
        result["bot_json"] = str(write_bot_meta(args.target))
    result["plan"] = [{"src": str(p["src"]), "dst": str(p["dst"]),
                       "kind": p["kind"]} for p in plan]
    result["target"] = str(args.target)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] legacy={args.legacy} -> target={args.target}")
        for item in result["plan"]:
            print(f"  {item['kind']}: {item['src']} -> {item['dst']}")
        print(f"copied={len(result['copied'])} skipped={len(result['skipped'])}")
        if not args.apply:
            print("（dry-run：加 --apply 才真正复制；旧数据不会被删除）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
