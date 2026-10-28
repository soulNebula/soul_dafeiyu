# -*- coding: utf-8 -*-
"""把「刷B站」值班轮的日报/报告同步进记忆库（bili_impressions）。

背景（2026-10-29）：B站值班轮写的是插件目录里的 digest/*.md、reports/*.md；
她的聊天库 ``bili_impressions`` 一直是空的，所以在微信里只能含糊说"看到摘要"。
本脚本把日报里的每条视频（BV/标题/UP/挑中或跳过的理由）写进所有联系人库，
聊天时 ``chat._bili_context`` 会把最近刷到的视频注入上下文，她就能聊细节。

用法::

    python tools/bili_memory_sync.py            # 同步
    python tools/bili_memory_sync.py --dry      # 只解析不写库
    python tools/bili_memory_sync.py --home D:\\bili-duty
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HOME = Path(__file__).resolve().parents[1]
DEFAULT_BILI_HOME = Path(os.environ.get("BILI_DUTY_DIR")
                         or (Path.home() / ".dsh" / "bili-duty"))
CONTACT_DIR = HOME / "agi" / "data" / "selflearn" / "wechat_contacts"

_LINE_RE = re.compile(
    r"^-\s*(\d{4}-\d{2}-\d{2})\s*·\s*(BV[0-9A-Za-z]+)\s*·\s*(.*?)\s*·\s*(.*?)\s*·\s*(.*?)\s*·\s*(https?://\S+)\s*$")
_BV_RE = re.compile(r"BV[0-9A-Za-z]+")


def _keywords(title: str, limit: int = 4) -> list[str]:
    tokens = re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z][A-Za-z0-9.+#-]{1,}", title or "")
    seen, out = set(), []
    for token in tokens:
        token = token.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= limit:
            break
    return out


def parse_digest(home: Path) -> list[dict]:
    """解析 digest/*.md → 视频条目（按 bvid 去重，新日期优先）。"""
    entries: dict[str, dict] = {}
    digest_dir = home / "digest"
    if not digest_dir.is_dir():
        return []
    for path in sorted(digest_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = _LINE_RE.match(line.strip())
            if not match:
                continue
            date, bvid, title, owner, reason, url = match.groups()
            picked = reason.startswith("挑")
            try:
                created = time.mktime(datetime.strptime(date, "%Y-%m-%d").timetuple()) + 12 * 3600
            except ValueError:
                created = time.time()
            entry = {
                "bvid": bvid, "title": title.strip(), "owner": owner.strip(),
                "reason": reason.strip(), "url": url.strip(), "date": date,
                "created_at": created, "picked": picked,
                "topics": _keywords(title),
                "source_file": path.name,
            }
            old = entries.get(bvid)
            if old is None or created >= old["created_at"]:
                entries[bvid] = entry
    return list(entries.values())


def _report_excerpt(home: Path, bvid: str, limit: int = 240) -> str:
    reports = home / "reports"
    if not reports.is_dir():
        return ""
    for path in reports.glob(f"{bvid}-*.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # 去掉标题行与 markdown 符号，取正文第一段像人话的内容
        lines = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-") and len(line) < 4:
                continue
            lines.append(re.sub(r"^[-*]\s*", "", line))
        body = " ".join(lines).strip()
        return body[:limit]
    return ""


def _row(entry: dict, report: str) -> dict:
    reason = entry["reason"]
    return {
        "id": "BI-" + hashlib.md5(entry["bvid"].encode()).hexdigest()[:10],
        "bvid": entry["bvid"], "title": entry["title"], "owner": entry["owner"],
        "duration_s": 0, "cover_url": "",
        "summary": reason, "topics_json": json.dumps(entry["topics"], ensure_ascii=False),
        "emotion": "neutral", "value": 0.8 if entry["picked"] else 0.35,
        "why": reason, "my_take": report or reason, "share_line": "",
        "subtitle_len": 0, "subtitle_missing": 0,
        "comments_json": "[]", "danmaku_json": "[]",
        "meta_json": json.dumps({"source": "bili-duty-digest", "url": entry["url"],
                                 "date": entry["date"], "picked": entry["picked"],
                                 "digest": entry["source_file"]}, ensure_ascii=False),
        "created_at": float(entry["created_at"]), "shared": 0, "shared_at": None,
    }


_INSERT = """
INSERT OR REPLACE INTO bili_impressions(
 id,bvid,title,owner,owner_mid,duration_s,cover_url,summary,topics_json,emotion,value,
 why,my_take,share_line,subtitle_len,subtitle_missing,comments_json,danmaku_json,
 meta_json,created_at,shared,shared_at)
VALUES(:id,:bvid,:title,:owner,'',:duration_s,:cover_url,:summary,:topics_json,:emotion,
 :value,:why,:my_take,:share_line,:subtitle_len,:subtitle_missing,:comments_json,
 :danmaku_json,:meta_json,:created_at,:shared,:shared_at)
"""


def _dbs() -> list[Path]:
    if not CONTACT_DIR.is_dir():
        return []
    return sorted(CONTACT_DIR.glob("*.memory.sqlite3"))


def _ensure_schema(con: sqlite3.Connection) -> None:
    try:
        from agi.selflearn import db as learn_db
        schema = getattr(learn_db, "MEMORY_SCHEMA", "")
        if schema:
            con.executescript(schema)
    except Exception:  # noqa: BLE001 - 已经在跑的项目里表一定存在；新库才需要
        pass


def sync(home: Path = DEFAULT_BILI_HOME, *, dry: bool = False) -> dict:
    entries = parse_digest(home)
    if not entries:
        return {"ok": False, "error": "digest_empty", "home": str(home)}
    rows = []
    for entry in entries:
        report = _report_excerpt(home, entry["bvid"])
        rows.append(_row(entry, report))
    if dry:
        return {"ok": True, "dry": True, "parsed": len(rows),
                "sample": [{"bvid": r["bvid"], "title": r["title"], "picked": r["shared"] == 0}
                           for r in rows[:5]]}
    inserted = 0
    touched = []
    errors: list[str] = []
    for path in _dbs():
        try:
            con = sqlite3.connect(str(path), timeout=10)
            con.row_factory = sqlite3.Row
            try:
                _ensure_schema(con)
                for row in rows:
                    con.execute(_INSERT, row)
                con.commit()
                inserted += len(rows)
                touched.append(path.name)
            finally:
                con.close()
        except sqlite3.Error as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {str(exc)[:160]}")
            continue
    return {"ok": True, "parsed": len(rows), "inserted": inserted,
            "databases": touched, "errors": errors, "home": str(home)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把B站日报同步进记忆库（bili_impressions）")
    parser.add_argument("--home", default=str(DEFAULT_BILI_HOME))
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args(argv)
    result = sync(Path(args.home), dry=args.dry)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
