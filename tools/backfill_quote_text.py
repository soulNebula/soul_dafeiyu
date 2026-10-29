# -*- coding: utf-8 -*-
"""回填历史引用消息缺失的「实际回复文字」。

2026-10-29 前的解析 bug：WeChat 引用消息的 `<title>`（用户自己写的那句话）被丢掉，
只保留了 `<refermsg>` 的引用内容，导致 802 对话流只显示「对方引用了这条消息：…」。

本脚本从微信原始数据库按时间匹配引用消息，把 `<title>` 追加回已处理的联系人库。
默认 dry-run；`--apply` 才会写库（写前自动留 .quote_bak 备份）。

用法：
    python tools/backfill_quote_text.py            # 看看能补多少
    python tools/backfill_quote_text.py --apply    # 真写
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wx_core import db_source  # noqa: E402
from wx_core import wcdb  # noqa: E402

CONTACTS_DIR = ROOT / "agi" / "data" / "selflearn" / "wechat_contacts"
GATEWAY_CONFIG = ROOT / "agi" / "data" / "wechat" / "config.json"


def _aliases() -> list[str]:
    try:
        cfg = json.loads(GATEWAY_CONFIG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    names: list[str] = []
    for key in ("proactive_contacts", "monitored_contacts", "allowed_contacts"):
        for name in cfg.get(key) or []:
            name = str(name or "").strip()
            if name and name not in names:
                names.append(name)
    return names


def _processed_db(alias: str) -> Path | None:
    stem = hashlib.sha1(alias.encode("utf-8")).hexdigest()[:14]
    for suffix in (".memory.sqlite3", ".sqlite3", ".legacy.sqlite3"):
        candidate = CONTACTS_DIR / (stem + suffix)
        if candidate.is_file():
            return candidate
    return None


def _raw_quotes(bridge: db_source.DbBridge, alias: str, limit: int) -> list[dict]:
    """原始库里的引用消息：{ts, quote, title}。"""
    bridge.add_friend(alias)
    state = bridge.sources.get(alias)
    if not state:
        return []
    rows = bridge.reader.read_messages(state["username"], 0, limit=limit)
    out: list[dict] = []
    for row in rows:
        local_type = int(row.get("local_type", 0) or 0) & 0xFFFFFFFF
        if local_type != 49:
            continue
        content = row.get("content")
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        if not isinstance(content, str) or "<refermsg>" not in content:
            continue
        title = db_source._plain_from_xml(db_source._xml_tag("title", content)).strip()
        if not title:
            continue
        m = re.search(r"<refermsg>(.*?)</refermsg>", content, re.S)
        blob = m.group(1) if m else ""
        quote = db_source._plain_from_xml(db_source._xml_tag("content", blob)).strip()
        out.append({"ts": int(float(row.get("create_time") or 0)),
                    "quote": quote[:200], "title": title[:500]})
    return out


def _match_title(ts: int, text: str, quotes: list[dict]) -> str:
    # 1) 引用内容能在处理后的文本里找到（最可靠）
    hits = [q for q in quotes
            if q["quote"] and len(q["quote"]) >= 4 and q["quote"] in text]
    if hits:
        hits.sort(key=lambda q: abs(q["ts"] - ts))
        return hits[0]["title"]
    # 2) 时间最近（±300s 内）
    near = [q for q in quotes if abs(q["ts"] - ts) <= 300]
    if near:
        near.sort(key=lambda q: abs(q["ts"] - ts))
        return near[0]["title"]
    return ""


def backfill(alias: str, quotes: list[dict], *, apply: bool) -> dict:
    db = _processed_db(alias)
    if db is None:
        return {"alias": alias, "db": None, "updated": 0}
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id,text,ts FROM messages WHERE text LIKE '[引用]%' "
            "AND instr(text, char(10)) = 0"
        ).fetchall()
        updated = 0
        planned: list[tuple[str, str]] = []
        for row in rows:
            ts = int(float(row["ts"] or 0))
            title = _match_title(ts, str(row["text"]), quotes)
            if not title:
                continue
            new_text = str(row["text"]) + "\n" + title
            planned.append((new_text, str(row["id"])))
            updated += 1
        if apply and planned:
            backup = db.with_suffix(db.suffix + ".quote_bak")
            if not backup.exists():
                shutil.copy2(db, backup)
            con.executemany("UPDATE messages SET text=? WHERE id=?", planned)
            con.commit()
        return {"alias": alias, "db": str(db), "candidates": len(rows),
                "updated": updated}
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填引用消息缺失的回复文字")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    parser.add_argument("--limit", type=int, default=100000, help="每联系人扫描原始消息上限")
    args = parser.parse_args(argv)
    cfg = json.loads(GATEWAY_CONFIG.read_text(encoding="utf-8"))
    bridge = db_source.DbBridge(cfg, log=lambda *a, **k: None)
    results = []
    try:
        for alias in _aliases():
            quotes = _raw_quotes(bridge, alias, max(1000, args.limit))
            results.append(backfill(alias, quotes, apply=args.apply))
    finally:
        try:
            bridge.close()
        except Exception:  # noqa: BLE001
            pass
    for row in results:
        print(f"{row['alias']}: 引用候选 {row.get('candidates', 0)}，"
              f"可/已补 {row['updated']} 条" + ("（已写库）" if args.apply else "（dry-run）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
