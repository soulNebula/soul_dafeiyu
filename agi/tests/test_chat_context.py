# -*- coding: utf-8 -*-
"""P29：聊天上下文/拍一拍诊断脚本测试（tmp 库 + mock 日志，不联网）。"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

from agi.selflearn import db, memory

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "chat_context_probe", ROOT / "tools" / "chat_context_probe.py")
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)

DAY = 1_000_000.0


def _db(tmp_path: Path) -> Path:
    base = tmp_path / "contact.sqlite3"
    db.connect(base)
    return base


def _write_log(tmp_path: Path) -> Path:
    log = tmp_path / "gateway.log"
    rows = [
        {"ts": DAY - 60, "kind": "inbound", "friend": "好友A",
         "text": "现在几点了"},
        {"ts": DAY - 50, "kind": "outbound", "friend": "好友A",
         "text": "三点零六"},
        {"ts": DAY - 10, "kind": "inbound", "friend": "好友A",
         "text": '"好友A" 拍了拍我'},
        {"ts": DAY - 5, "kind": "outbound", "friend": "好友A",
         "text": "刚才那句没组织好，你再说一次？"},
        {"ts": DAY - 3, "kind": "inbound", "friend": "别人",
         "text": "别人的消息"},
    ]
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    return log


def test_detect_inbound_variants():
    for raw in ('"好友A" 拍了拍我', '"好友A"拍了拍我', "「好友A」拍了拍我",
                "拍了拍你", "我拍了拍你"):
        result = probe.detect_inbound(raw)
        assert result["pat"] is True, raw
        assert result["as_text"].startswith("[拍一拍]"), raw
        assert result["as_system"].startswith("[拍一拍]"), raw

    normal = probe.detect_inbound("今天拍了拍照，光线不错")
    assert normal["pat"] is False
    assert normal["as_text"] == "今天拍了拍照，光线不错"

    quote = probe.detect_inbound("对方引用了这条消息：原话")
    assert quote["quote_hint"] is True
    assert probe.detect_inbound("[引用消息] 原话")["as_system"].startswith("[引用]")


def test_parse_gateway_events_and_guard_fallback(tmp_path):
    log = _write_log(tmp_path)
    events = probe.parse_gateway_events(log, contact="好友A", limit=10)
    assert [e["kind"] for e in events] == ["inbound", "outbound", "inbound", "outbound"]
    pat = events[2]
    assert pat["pat"] is True and pat["as_text"] == "[拍一拍] 对方拍了拍你"
    assert events[1].get("guard_fallback") is False
    assert events[3].get("guard_fallback") is True

    # --around 按「今天的」时刻过滤：用当前时刻重写一份日志再验证
    now = time.time()
    around_log = tmp_path / "gateway_now.log"
    rows = [
        {"ts": now - 60, "kind": "inbound", "friend": "好友A", "text": "在吗"},
        {"ts": now + 600, "kind": "inbound", "friend": "好友A", "text": "很久以后"},
    ]
    around_log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                          encoding="utf-8")
    around = probe.parse_gateway_events(
        around_log, contact="好友A", limit=10,
        around=time.strftime("%H:%M", time.localtime(now)))
    assert len(around) == 1 and around[0]["text"] == "在吗"


def test_load_history_readonly(tmp_path):
    base = _db(tmp_path)
    memory.store_message(base, "user", "你好")
    memory.store_message(base, "assistant", "在呢")
    rows = probe.load_history(base, limit=12)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[-1]["text"] == "在呢"
    assert probe.load_history(tmp_path / "missing.sqlite3") == []


def test_build_report_summary(tmp_path):
    log = _write_log(tmp_path)
    base = _db(tmp_path)
    memory.store_message(base, "user", "你好")
    memory.store_message(base, "assistant", "在呢")
    events = probe.parse_gateway_events(log, contact="好友A", limit=10)
    history = probe.load_history(base, limit=12)
    report = probe.build_report(events, history, probe.chat_history_limits(),
                                contact="好友A")
    assert report["summary"]["inbound"] == 2
    assert report["summary"]["pat_inbound"] == 1
    assert report["summary"]["guard_fallback_replies"] == 1
    assert report["history_count"] == 2
    assert report["history_chars"] == len("你好") + len("在呢")
    limits = report["prompt_history_limits"]["limits"]
    assert limits and (6 in limits or 12 in limits)


def test_probe_main_json(tmp_path, capsys):
    log = _write_log(tmp_path)
    base = _db(tmp_path)
    memory.store_message(base, "user", "你好")
    code = probe.main(["--contact", "好友A", "--log", str(log),
                       "--db", str(base), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["summary"]["pat_inbound"] == 1
    assert payload["summary"]["guard_fallback_replies"] == 1
