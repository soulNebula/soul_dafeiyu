# -*- coding: utf-8 -*-
"""风格漂移测试。"""
from __future__ import annotations

import time

from agi.selflearn import db, drift


def _turns(path, texts):
    con = db.connect(path)
    for i, text in enumerate(texts):
        con.execute("INSERT INTO turns(id,user_message_id,assistant_message_id,reply,created_at) "
                    "VALUES(?,?,?,?,?)", (f"T-{i}", f"M-{i}", f"A-{i}", text, time.time() + i))
    con.commit(); con.close()


def test_drift_detects_longer_softer_recent(tmp_path):
    path = tmp_path / "drift.sqlite3"
    old = ["短句。"] * 20
    new = ["当然，我理解你，你已经很棒了，别担心，慢慢来，没关系的。"] * 20
    _turns(path, old + new)
    r = drift.detect(path, window=20)
    assert r["verdict"] == "drifting"
    assert "reply_length_increasing" in r["flags"] or "softening" in r["flags"]


def test_drift_stable_and_insufficient(tmp_path):
    p1 = tmp_path / "stable.sqlite3"; _turns(p1, ["好的。"] * 40)
    assert drift.detect(p1, window=20)["verdict"] == "stable"
    p2 = tmp_path / "small.sqlite3"; _turns(p2, ["短."] * 2)
    assert drift.detect(p2, window=20)["verdict"] == "insufficient"
