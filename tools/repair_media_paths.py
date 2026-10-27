# -*- coding: utf-8 -*-
"""修复素材库 media_assets.path：把旧的 wx_box 绝对路径映射到 SoulAgent。

策略：
1. 旧 -> 新前缀替换（Desktop\\wx_box -> Desktop\\SoulAgent），文件存在则改；
2. 否则按 basename 在 SoulAgent 常见素材目录里找唯一匹配；
3. 找不到保留原值并报告。
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

ROOT = Path(r"C:\Users\Administrator\Desktop\SoulAgent")
DB = ROOT / "agi" / "data" / "selflearn" / "selflearn.memory.sqlite3"
OLD = r"C:\Users\Administrator\Desktop\wx_box"
NEW = str(ROOT)
SEARCH_DIRS = [
    ROOT / "wx_core" / "emoji_assets",
    ROOT / "media",
    ROOT / "media" / "stickers",
    ROOT / "media_chat",
    ROOT / "agi" / "data" / "selflearn" / "media",
]


def main() -> int:
    backup = DB.with_suffix(f".sqlite3.bak_{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(DB, backup)
    print("backup:", backup)

    con = sqlite3.connect(DB)
    rows = con.execute("SELECT id, path, name FROM media_assets").fetchall()
    fixed = kept = 0
    missing = []
    for rid, path, name in rows:
        p = str(path or "")
        if p and Path(p).is_file():
            kept += 1
            continue
        cand = Path(p.replace(OLD, NEW)) if OLD in p else Path(p)
        if cand.is_file():
            con.execute("UPDATE media_assets SET path=? WHERE id=?", (str(cand), rid))
            fixed += 1
            continue
        base = Path(p).name if p else ""
        hits = []
        if base:
            for d in SEARCH_DIRS:
                f = d / base
                if f.is_file():
                    hits.append(f)
        if len(hits) >= 1:
            con.execute("UPDATE media_assets SET path=? WHERE id=?", (str(hits[0]), rid))
            fixed += 1
            continue
        missing.append((rid, name, p))
    con.commit()
    con.close()
    print(f"fixed={fixed} kept={kept} missing={len(missing)}")
    for m in missing[:10]:
        print("  miss:", m)
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
