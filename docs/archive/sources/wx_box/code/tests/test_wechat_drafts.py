# -*- coding: utf-8 -*-
"""801 待发草稿（draft）存储测试。"""
from __future__ import annotations

from agi.wechat_gateway import drafts


def test_add_list_discard(tmp_path):
    path = tmp_path / "drafts.jsonl"
    d1 = drafts.add("好友A", "第一条", "confirm", path=path)
    d2 = drafts.add("好友B", "第二条", "send_failed", path=path)
    assert d1["id"] != d2["id"]

    rows = drafts.list_all(path=path)
    assert [r["text"] for r in rows] == ["第一条", "第二条"]

    got = drafts.get(d1["id"], path=path)
    assert got and got["contact"] == "好友A"

    removed = drafts.discard(d1["id"], path=path)
    assert removed and removed["text"] == "第一条"
    assert [r["id"] for r in drafts.list_all(path=path)] == [d2["id"]]
    assert drafts.discard("不存在", path=path) is None


def test_tolerates_broken_lines(tmp_path):
    path = tmp_path / "drafts.jsonl"
    path.write_text('{"id":"D-1","text":"ok"}\nnot-json\n{"no_id":1}\n',
                    encoding="utf-8")
    rows = drafts.list_all(path=path)
    assert len(rows) == 1 and rows[0]["text"] == "ok"


def test_empty_file_and_missing_file(tmp_path):
    assert drafts.list_all(path=tmp_path / "nope.jsonl") == []
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert drafts.list_all(path=path) == []
