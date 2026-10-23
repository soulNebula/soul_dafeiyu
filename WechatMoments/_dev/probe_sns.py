# -*- coding: utf-8 -*-
"""开发期探针 2：解密 sns.db 并 dump 4.x 朋友圈库结构。"""
from __future__ import annotations

import importlib.util
import sys
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Desktop\wx_box\WechatMoments\_dev")

from probe_db import ref  # noqa: E402  (复用打桩+加载)

db = ref.WeChatDB()
rel = next(r for r, _, _ in db._db_files if r.lower().startswith("sns"))
conn = db._open(rel)
cur = conn.cursor()
print("== tables ==")
cur.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name")
tables = cur.fetchall()
for name, sql in tables:
    print("\n---", name)
    print((sql or "").replace("\n", " ")[:400])
print("\n== row counts ==")
for name, _ in tables:
    try:
        cur.execute(f'SELECT COUNT(*) FROM "{name}"')
        print(f"{name}: {cur.fetchone()[0]}")
    except Exception as e:
        print(f"{name}: ERR {e}")
