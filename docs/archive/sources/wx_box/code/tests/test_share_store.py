# -*- coding: utf-8 -*-
"""分享持久化测试。"""
from agi.selflearn import share_store


def test_share_store_records(tmp_path):
    p = tmp_path / "sh.sqlite3"
    c = share_store.record_candidate(p, level="observation", kind="share_self",
                                     text="刚看到晚霞", score=0.6)
    assert c["ok"]
    share_store.record_event(p, c["id"], "spoken")
    assert share_store.stats(p) == {"ok": True, "candidates": 1, "events": 1}
    assert share_store.list_candidates(p)[0]["text"] == "刚看到晚霞"
