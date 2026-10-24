# -*- coding: utf-8 -*-
"""pytest 全局隔离：任何测试都不许写真实数据目录。

背景（2026-10-24）：
- `agi.wechat_gateway.config.contact_db("好友A")` 的 sha1 前 14 位是 043e7e3743523b，
  刚好和真实数据目录里的一个联系人库同名；
- `test_proactive_worker.py` 里部分用例直接调 `run_cycle()`，没有替换 contact_db，
  于是把测试数据写进了 `agi/data/selflearn/wechat_contacts/`。
- 这里用 autouse fixture 把 contact_db 重定向到 tmp_path，从根上堵住。

注意：生产代码里的 `db.connect(path=DB_PATH)` 默认参数在 import 时已绑定，
本 fixture 只负责 `contact_db` 这个运行时计算路径的入口；测试仍应显式传 tmp_path。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_contact_db(tmp_path, monkeypatch):
    """把联系人库路径重定向到临时目录，避免测试污染真实 agi/data。"""
    contacts = tmp_path / "_contacts"

    def _fake_contact_db(contact: str):
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(contact))
        return contacts / f"{safe}.sqlite3"

    monkeypatch.setattr("agi.wechat_gateway.config.contact_db", _fake_contact_db)
    yield
