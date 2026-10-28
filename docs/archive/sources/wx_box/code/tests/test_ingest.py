# -*- coding: utf-8 -*-
"""Batch 3 Agent A：JSONL 清洗入库、会话/段切分与统计。

全部使用临时 SQLite/JSONL，不读旧客户数据，不联网。
"""
from __future__ import annotations

import json
import math

import pytest

from agi.selflearn import db, ingest


def _write_jsonl(path, records) -> None:
    lines = []
    for record in records:
        if isinstance(record, str):
            lines.append(record)
        else:
            lines.append(json.dumps(record, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _table_rows(path, table, order="id"):
    con = db.connect(path)
    try:
        return [dict(row) for row in con.execute(f"SELECT * FROM {table} ORDER BY {order}")]
    finally:
        con.close()


def _ingest_records(db_path, tmp_path, records, *, source="jsonl"):
    jsonl = tmp_path / "events.jsonl"
    _write_jsonl(jsonl, records)
    return ingest.ingest_jsonl(db_path, jsonl, source=source)


def test_clean_event_normalizes_fields_and_keeps_meta():
    raw = {
        "id": "  m-1  ",
        "friend": "  Alice ",
        "sender": " Bob\n",
        "kind": "TEXT",
        "text": "\u200b 你好 \n\t 世界 \u200b ",
        "ts": "123.5",
        "media": ["a.jpg"],
        "raw_ref": "line-7",
    }

    event = ingest.clean_event(raw)

    assert event is not None
    assert event["id"] == "m-1"
    assert event["source_id"] == "m-1"
    assert event["friend"] == "Alice"
    assert event["sender"] == "Bob"
    assert event["kind"] == "text"
    assert event["text"] == "你好 世界"
    assert event["ts"] == 123.5
    assert json.loads(event["meta_json"]) == {
        "media": ["a.jpg"], "raw_ref": "line-7"
    }


def test_clean_event_missing_ts_defaults_to_zero():
    event = ingest.clean_event({
        "id": "m-2", "friend": "alice", "sender": "alice",
        "kind": "text", "text": "没有时间戳",
    })

    assert event is not None
    assert event["ts"] == 0.0


@pytest.mark.parametrize("raw", [
    None,
    [],
    "not-a-dict",
    # 缺 friend / sender / kind
    {"id": "a", "sender": "a", "kind": "text", "text": "x", "ts": 1},
    {"id": "b", "friend": "a", "kind": "text", "text": "x", "ts": 1},
    {"id": "c", "friend": "a", "sender": "a", "text": "x", "ts": 1},
    # 文本事件空文本
    {"id": "d", "friend": "a", "sender": "a", "kind": "text",
     "text": " \n\t\u200b ", "ts": 1},
    {"id": "e", "friend": "a", "sender": "a", "kind": "text", "ts": 1},
    # 坏时间
    {"id": "f", "friend": "a", "sender": "a", "kind": "text",
     "text": "x", "ts": "not-a-time"},
    {"id": "g", "friend": "a", "sender": "a", "kind": "text",
     "text": "x", "ts": float("nan")},
    {"id": "h", "friend": "a", "sender": "a", "kind": "text",
     "text": "x", "ts": -1},
])
def test_clean_event_rejects_bad_or_missing_fields(raw):
    assert ingest.clean_event(raw) is None


def test_clean_event_keeps_non_text_and_system_kinds():
    image = ingest.clean_event({
        "id": "img-1", "friend": "alice", "sender": "alice",
        "kind": "image", "text": "   ", "ts": 2,
    })
    system = ingest.clean_event({
        "id": "sys-1", "friend": "alice", "sender": "alice",
        "kind": "system", "text": " 对方撤回了一条消息 ", "ts": 3,
    })
    empty_system = ingest.clean_event({
        "id": "sys-2", "friend": "alice", "sender": "alice",
        "kind": "system", "ts": 4,
    })

    assert image is not None and image["kind"] == "image" and image["text"] == ""
    assert system is not None and system["text"] == "对方撤回了一条消息"
    assert empty_system is not None and empty_system["text"] == ""


def test_ingest_jsonl_rejects_bad_rows_and_is_idempotent(tmp_path):
    path = tmp_path / "ingest.sqlite3"
    records = [
        {"id": "e1", "friend": "alice", "sender": "alice", "kind": "text",
         "text": "  hello \n world ", "ts": 100},
        {"id": "e2", "friend": "alice", "sender": "alice", "kind": "text",
         "text": "second", "ts": 110, "extra": {"x": 1}},
        # 同一来源 id 重复：保留先到的一条
        {"id": "e1", "friend": "alice", "sender": "alice", "kind": "text",
         "text": "should be ignored", "ts": 999},
        "{this is not json",
        # 缺字段 / 空文本
        {"id": "e3", "friend": " ", "sender": "alice", "kind": "text",
         "text": "x", "ts": 120},
        {"id": "e4", "friend": "alice", "sender": "alice", "kind": "text",
         "text": " \n ", "ts": 130},
        # 非文本与系统事件保留
        {"id": "e5", "friend": "alice", "sender": "alice", "kind": "image",
         "text": "", "ts": 140},
        {"id": "e6", "friend": "alice", "sender": "alice", "kind": "system",
         "text": "撤回了一条消息", "ts": 150},
    ]
    jsonl = tmp_path / "mixed.jsonl"
    _write_jsonl(jsonl, records)

    first = ingest.ingest_jsonl(path, jsonl, source="chatlog")

    assert first["total"] == 8
    assert first["parsed"] == 7
    assert first["inserted"] == 4
    assert first["duplicates"] == 1
    assert first["rejected"] == 3
    assert first["invalid"] == 3
    assert first["ok"] is False
    assert len(first["errors"]) == 3

    rows = _table_rows(path, "raw_events", "source_id")
    assert len(rows) == 4
    by_source_id = {row["source_id"]: row for row in rows}
    assert set(by_source_id) == {"e1", "e2", "e5", "e6"}
    assert by_source_id["e1"]["text"] == "hello world"
    assert by_source_id["e1"]["source"] == "chatlog"
    assert by_source_id["e5"]["kind"] == "image"
    assert by_source_id["e5"]["text"] == ""
    assert by_source_id["e6"]["kind"] == "system"
    assert json.loads(by_source_id["e2"]["meta_json"]) == {"extra": {"x": 1}}

    second = ingest.ingest_jsonl(path, jsonl, source="chatlog")

    assert second["inserted"] == 0
    assert second["duplicates"] == 5
    assert second["rejected"] == 3
    assert second["total"] == 8
    assert len(_table_rows(path, "raw_events")) == 4


def test_ingest_jsonl_skips_blank_lines_and_empty_file(tmp_path):
    path = tmp_path / "blank.sqlite3"
    jsonl = tmp_path / "blank.jsonl"
    jsonl.write_text(
        "\n  \n"
        '{"id":"b1","friend":"a","sender":"a","kind":"text","text":"hi","ts":1}\n'
        "\n",
        encoding="utf-8",
    )

    report = ingest.ingest_jsonl(path, jsonl)

    assert report["lines"] == 4
    assert report["blank"] == 3
    assert report["total"] == 1
    assert report["inserted"] == 1
    assert report["ok"] is True

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    empty_report = ingest.ingest_jsonl(tmp_path / "empty.sqlite3", empty)
    assert empty_report["total"] == 0
    assert empty_report["inserted"] == 0
    assert empty_report["ok"] is True


def test_build_sessions_groups_by_friend_time_gap_and_text_only(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    records = [
        {"id": "a1", "friend": "alice", "sender": "alice", "kind": "text",
         "text": "a1", "ts": 100},
        {"id": "sys", "friend": "alice", "sender": "system", "kind": "system",
         "text": "系统提示", "ts": 150},
        {"id": "b1", "friend": "bob", "sender": "bob", "kind": "text",
         "text": "b1", "ts": 150},
        {"id": "a2", "friend": "alice", "sender": "alice", "kind": "text",
         "text": "a2", "ts": 200},
        {"id": "img", "friend": "alice", "sender": "alice", "kind": "image",
         "text": "caption", "ts": 250},
        {"id": "b2", "friend": "bob", "sender": "bob", "kind": "text",
         "text": "b2", "ts": 1950},
        # 与上一条恰好 1800 秒，不切；下一条 1801 秒才切
        {"id": "a3", "friend": "alice", "sender": "alice", "kind": "text",
         "text": "a3", "ts": 2000},
        {"id": "a4", "friend": "alice", "sender": "alice", "kind": "text",
         "text": "a4", "ts": 3801},
    ]
    _ingest_records(path, tmp_path, records)

    sessions = ingest.build_sessions(path)

    assert len(sessions) == 3
    assert len({item["id"] for item in sessions}) == 3
    by_friend = {}
    for item in sessions:
        by_friend.setdefault(item["friend"], []).append(item)
    assert [item["message_count"] for item in by_friend["alice"]] == [3, 1]
    assert by_friend["alice"][0]["start_ts"] == 100
    assert by_friend["alice"][0]["end_ts"] == 2000
    assert by_friend["alice"][1]["start_ts"] == 3801
    assert by_friend["bob"][0]["message_count"] == 2
    assert by_friend["bob"][0]["start_ts"] == 150
    assert by_friend["bob"][0]["end_ts"] == 1950
    assert all(item["status"] == "active" for item in sessions)

    repeat = ingest.build_sessions(path)
    assert [item["id"] for item in repeat] == [item["id"] for item in sessions]
    assert len(_table_rows(path, "sessions")) == 3

    alice_only = ingest.build_sessions(path, friend="alice")
    assert len(alice_only) == 2
    assert all(item["friend"] == "alice" for item in alice_only)
    assert len(_table_rows(path, "sessions")) == 3

    with pytest.raises(ValueError):
        ingest.build_sessions(path, gap_seconds=-1)


def test_segment_session_respects_max_chars_and_is_idempotent(tmp_path):
    path = tmp_path / "segments.sqlite3"
    records = [
        {"id": "s1", "friend": "alice", "sender": "alice", "kind": "text",
         "text": "aaaaa", "ts": 100},
        {"id": "sys", "friend": "alice", "sender": "system", "kind": "system",
         "text": "系统提示", "ts": 105},
        {"id": "s2", "friend": "alice", "sender": "alice", "kind": "text",
         "text": "bbbbb", "ts": 110},
        {"id": "s3", "friend": "alice", "sender": "alice", "kind": "text",
         "text": "x" * 50, "ts": 120},
        {"id": "s4", "friend": "alice", "sender": "alice", "kind": "text",
         "text": "end", "ts": 130},
        {"id": "img", "friend": "alice", "sender": "alice", "kind": "image",
         "text": "这不是聊天文本", "ts": 140},
    ]
    _ingest_records(path, tmp_path, records)
    sessions = ingest.build_sessions(path)
    assert len(sessions) == 1
    session_id = sessions[0]["id"]

    segments = ingest.segment_session(path, session_id, max_chars=20)

    assert len(segments) == 4
    assert [item["seq"] for item in segments] == [0, 1, 2, 3]
    assert segments[0]["text"] == "aaaaa\nbbbbb"
    assert segments[0]["start_ts"] == 100
    assert segments[0]["end_ts"] == 110
    assert segments[1]["text"] == "x" * 20
    assert segments[1]["start_ts"] == 120
    assert segments[1]["end_ts"] == 120
    assert segments[-1]["text"] == "x" * 10 + "\nend"
    assert segments[-1]["start_ts"] == 120
    assert segments[-1]["end_ts"] == 130
    assert all(0 < len(item["text"]) <= 20 for item in segments)
    assert all("系统提示" not in item["text"] for item in segments)
    assert all("这不是聊天文本" not in item["text"] for item in segments)
    assert json.loads(segments[0]["meta_json"])["message_count"] == 2
    assert len(_table_rows(path, "session_segments")) == 4

    repeat = ingest.segment_session(path, session_id, max_chars=20)
    assert [(item["id"], item["text"]) for item in repeat] == [
        (item["id"], item["text"]) for item in segments
    ]
    assert len(_table_rows(path, "session_segments")) == 4

    assert ingest.segment_session(path, "missing-session", max_chars=20) == []
    with pytest.raises(ValueError):
        ingest.segment_session(path, session_id, max_chars=0)


def test_stats_reports_counts_after_pipeline(tmp_path):
    empty_path = tmp_path / "empty-stats.sqlite3"
    empty = ingest.stats(empty_path)
    assert empty["raw_events"] == 0
    assert empty["sessions"] == 0
    assert empty["segments"] == 0
    assert empty["friends"] == 0
    assert empty["by_kind"] == {}
    assert empty["first_ts"] is None
    assert empty["last_ts"] is None

    path = tmp_path / "stats.sqlite3"
    records = [
        {"id": "t1", "friend": "alice", "sender": "alice", "kind": "text",
         "text": "hello", "ts": 1},
        {"id": "t2", "friend": "alice", "sender": "alice", "kind": "text",
         "text": "world", "ts": 2},
        {"id": "i1", "friend": "bob", "sender": "bob", "kind": "image",
         "text": "", "ts": 3},
        {"id": "sys", "friend": "bob", "sender": "bob", "kind": "system",
         "text": "withdraw", "ts": 4},
        {"id": "bad", "friend": "bob", "sender": "bob", "kind": "text",
         "text": "   ", "ts": 5},
    ]
    report = _ingest_records(path, tmp_path, records)
    assert report["inserted"] == 4
    assert report["rejected"] == 1

    raw_stats = ingest.stats(path)
    assert raw_stats["raw_events"] == 4
    assert raw_stats["events"] == 4
    assert raw_stats["total_events"] == 4
    assert raw_stats["text_events"] == 2
    assert raw_stats["non_text_events"] == 2
    assert raw_stats["friends"] == 2
    assert raw_stats["sessions"] == 0
    assert raw_stats["segments"] == 0
    assert raw_stats["chars"] == len("hello") + len("world")
    assert raw_stats["by_kind"] == {"image": 1, "system": 1, "text": 2}
    assert raw_stats["by_source"] == {"jsonl": 4}
    assert raw_stats["first_ts"] == 1
    assert raw_stats["last_ts"] == 4

    sessions = ingest.build_sessions(path)
    assert len(sessions) == 1
    segments = ingest.segment_session(path, sessions[0]["id"], max_chars=20)
    assert len(segments) == 1

    final = ingest.stats(path)
    assert final["sessions"] == 1
    assert final["segments"] == 1
    assert final["session_segments"] == 1
    assert math.isclose(final["chars"], 10)
