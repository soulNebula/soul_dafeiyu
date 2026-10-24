# -*- coding: utf-8 -*-
"""P0-2 回归测试：tick() 内局部变量 sharing 遮蔽了模块 sharing。

现象：tick() 里 `sharing = float(own.get("sharing", 0.45))` 让后面的
`sharing.choose_level(path)` 实际是在 float 上调方法 → AttributeError，
被同段 except 静默吞掉 → share_info 恒为 {}，三级分享永不生效。
"""
from __future__ import annotations

import json
import time

from agi.selflearn import db, proactive


class _SayClient:
    """最小可用 client：available() 为真，chat() 回固定文本。"""

    def __init__(self, text):
        self.text = text

    def available(self):
        return True

    def chat(self, *a, **k):
        return {"ok": True, "text": self.text}


def _seed(path, *, ts_age=120, mood=0.8, sharing=0.8):
    """建库 + 一条 120 秒前的用户消息，并写入自状态 mood/sharing。"""
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-1", "user", "最近在忙项目", time.time() - ts_age, 0))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('self_state',?,?)",
                (json.dumps({"self": {"mood": mood, "sharing": sharing}}), time.time()))
    con.commit()
    con.close()


def test_tick_passes_module_sharing_level_through(tmp_path, monkeypatch):
    """tick 返回的 sharing_level 必须是模块 choose_level 的结果，而不是 {}。"""
    path = tmp_path / "p.sqlite3"
    _seed(path)
    monkeypatch.setattr(proactive.sharing, "choose_level",
                        lambda path: {"level": "L2", "hint": "test"})

    r = proactive.tick(path, client=_SayClient("今天想起你说的事"),
                       respect_state=False, cooldown=0, min_score=0)

    assert r["available"] is True, r
    # 修复前：AttributeError 被吞 → share_info == {} → KeyError
    assert r["sharing_level"]["level"] == "L2", r.get("sharing_level")
    # 局部变量改名后，返回体里的数值字段不能被改坏
    assert r["sharing"] == 0.8, r.get("sharing")
