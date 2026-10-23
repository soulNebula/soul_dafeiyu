# -*- coding: utf-8 -*-
"""开发期探针：复用 wx_box/recon/ref_db.py 验证微信 4.x 数据库密钥提取与 sns.db 解密。"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REF = r"C:\Users\Administrator\Desktop\wx_box\recon\ref_db.py"

# 打桩 wechatauto.logger，让参考实现可独立运行
pkg = types.ModuleType("wechatauto")
pkg.__path__ = []
logger_mod = types.ModuleType("wechatauto.logger")


class _L:
    def __getattr__(self, name):
        return lambda *a, **k: None


logger_mod.wxlog = _L()
sys.modules["wechatauto"] = pkg
sys.modules["wechatauto.logger"] = logger_mod

spec = importlib.util.spec_from_file_location("ref_db", REF)
ref = importlib.util.module_from_spec(spec)
sys.modules["ref_db"] = ref
spec.loader.exec_module(ref)

print("[1] db_dir =", ref.auto_detect_db_dir())
db = ref.WeChatDB()
print("[2] account =", db.account, "wxid =", db.wxid)
print("[3] db files =", len(db._db_files))
print("[4] master_key =", db.master_key)
ok = [rel for rel, _, _ in db._db_files if db._key_works(rel)]
print(f"[5] keys OK: {len(ok)}/{len(db._db_files)}")
for rel in ok:
    print("     OK", rel)
print("[6] sns db in files:", [r for r, _, _ in db._db_files if "sns" in r])
try:
    print("[7] self info =", json.dumps(db.get_self_info(), ensure_ascii=False)[:300])
except Exception as e:
    print("[7] self info FAIL", type(e).__name__, e)
