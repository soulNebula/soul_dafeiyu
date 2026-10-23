# -*- coding: utf-8 -*-
"""夜间 runner 测试：不联网，只验证编排。"""
from __future__ import annotations

import time

from agi.selflearn import db, nightly


class FakeClient:
    def available(self):
        return True

    def chat(self, messages, **kwargs):
        return {"ok": True, "text": "{}"}


def test_nightly_no_client_still_reports(tmp_path):
    path = tmp_path / "n.sqlite3"
    db.connect(path).close()
    r = nightly.run(path, None)
    assert r["ok"] is True and r["auto_send"] is False
    assert r["steps"]["summary"]["skipped"] == "no_client"


def test_nightly_with_fake_client_does_not_raise(tmp_path):
    path = tmp_path / "n2.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-1", "user", "我喜欢咖啡", time.time(), 0))
    con.commit(); con.close()
    r = nightly.run(path, FakeClient(), force=True)
    assert r["auto_send"] is False
    assert "steps" in r and "reflect" in r["steps"]
