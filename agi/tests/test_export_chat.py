# -*- coding: utf-8 -*-
"""export_chat.py 测试：SQLite 读取/窗口/同角色合并/过滤/脱敏/JSONL 转换/切分。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.train import export_chat as ex  # noqa: E402


def _db(path: Path, rows: list[tuple[str, str, float]]) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE messages(id TEXT PRIMARY KEY, role TEXT, text TEXT, "
                "ts REAL, proactive INTEGER DEFAULT 0, archived_at REAL)")
    for index, (role, text, ts) in enumerate(rows):
        con.execute("INSERT INTO messages(id,role,text,ts) VALUES(?,?,?,?)",
                    (f"M-{index}", role, text, ts))
    con.commit()
    con.close()
    return path


def test_read_merge_window_and_filters(tmp_path):
    db = _db(tmp_path / "a.memory.sqlite3", [
        ("user", "你好", 1000),
        ("user", "今天有点累", 1001),
        ("assistant", "在呢", 1002),
        ("assistant", "怎么了？", 1003),
        ("user", "wxid_abcdef:123456:1:1234567890abcdef", 1004),
        ("user", "短", 1005),
        ("assistant", "嗯", 1006),
    ])
    messages, stats = ex.load_sqlite_messages(db, min_chars=2)
    assert stats["rows"] == 7
    assert stats["skip_internal"] == 1
    assert stats["skip_short"] >= 1
    merged = ex.merge_consecutive(messages)
    assert merged[0]["text"] == "你好\n今天有点累"
    assert merged[1]["text"] == "在呢\n怎么了？"
    samples = ex.build_samples(merged, window=6, redact=False)
    assert len(samples) == 1
    roles = [m["role"] for m in samples[0]["messages"]]
    assert roles == ["system", "user", "assistant"]
    assert samples[0]["messages"][-1]["content"].startswith("在呢")


def test_time_window_filter(tmp_path):
    db = _db(tmp_path / "b.memory.sqlite3", [
        ("user", "旧消息", 100), ("assistant", "旧回复", 101),
        ("user", "新消息", 200), ("assistant", "新回复", 201),
    ])
    messages, stats = ex.load_sqlite_messages(db, since=150, until=250)
    assert [m["text"] for m in messages] == ["新消息", "新回复"]
    assert stats["skip_time"] == 2
    samples = ex.build_samples(messages, redact=False)
    assert len(samples) == 1


def test_redact_internal_phone_email_url():
    text = "wxid_abcdef 电话 13812345678 邮箱 a@b.com 链接 https://x.com/a?token=secret"
    out = ex.redact_text(text)
    assert "wxid_" not in out
    assert "13812345678" not in out and "[手机号]" in out
    assert "a@b.com" not in out and "[邮箱]" in out
    assert "https://" not in out and "[链接]" in out
    assert "电话" in out and "邮箱" in out


def test_convert_jsonl_formats_and_bad_rows(tmp_path):
    path = tmp_path / "in.jsonl"
    lines = [
        {"messages": [{"role": "system", "content": "系统"},
                      {"role": "user", "content": "你好"},
                      {"role": "assistant", "content": "嗨"}]},
        {"user": "问题", "assistant": "回答"},
        {"q": "问", "a": "答"},
        {"instruction": "翻译", "input": "hello", "output": "你好"},
        {"nonsense": "x"},
        "not a dict",
    ]
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) if not isinstance(x, str) else x
                             for x in lines) + "\n", encoding="utf-8")
    samples, stats = ex.load_jsonl(path, redact=True)
    assert stats["rows"] == 6
    assert stats["skip_bad"] >= 1
    assert stats["skip_empty"] >= 1
    assert len(samples) == 4
    assert samples[1]["messages"][0]["role"] == "user"
    assert "翻译\nhello" in samples[3]["messages"][0]["content"]


def test_generic_sqlite(tmp_path):
    path = tmp_path / "third.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE chat(role TEXT, body TEXT, ts REAL)")
    con.executemany("INSERT INTO chat VALUES(?,?,?)", [
        ("user", "早", 1), ("assistant", "早呀", 2),
    ])
    con.commit()
    con.close()
    messages, stats = ex.load_sqlite_generic(path, table="chat", role_col="role",
                                             text_col="body", ts_col="ts")
    assert [m["text"] for m in messages] == ["早", "早呀"]
    samples = ex.build_samples(messages, redact=False)
    assert len(samples) == 1


def test_exclude_test_and_split(tmp_path):
    test_db = _db(tmp_path / "测试.memory.sqlite3", [
        ("user", "测试消息", 1), ("assistant", "测试回复", 2),
    ])
    sources = ex.source_list(db=str(test_db), exclude_test=True)
    assert sources == []
    samples = [{"messages": [{"role": "user", "content": str(i)}]} for i in range(10)]
    train, val = ex.split_samples(samples, 0.2, seed=1)
    assert len(val) == 2 and len(train) == 8
    assert sorted(json.dumps(x, ensure_ascii=False) for x in train + val) == \
           sorted(json.dumps(x, ensure_ascii=False) for x in samples)
