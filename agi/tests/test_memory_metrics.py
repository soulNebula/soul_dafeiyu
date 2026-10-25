# -*- coding: utf-8 -*-
"""P1-g：记忆指标（分层/休眠/合并/检索命中/预算/归档）+ 每日快照与异常告警。"""
from __future__ import annotations

import io
import json
import time

import pytest

from agi.selflearn import db, memory, metrics, nightly
from agi.selflearn import web as web_mod

DAY = 86400.0


@pytest.fixture(autouse=True)
def _reset_retrieval_buffer():
    """检索计数是进程内缓冲；每个用例从干净状态开始。"""
    memory._RETRIEVAL_PENDING.update(
        {"path": "", "calls": 0, "hits": 0, "items": 0, "dropped": 0})
    yield
    memory._RETRIEVAL_PENDING.update(
        {"path": "", "calls": 0, "hits": 0, "items": 0, "dropped": 0})


def _mem(path, mid, *, content, kind="fact", status="active", age_days=5.0,
         importance=0.5, history=None, last_accessed=0.0, now=None):
    now = time.time() if now is None else now
    con = db.connect(path)
    con.execute(
        "INSERT INTO user_memories(id,scope,kind,content,evidence,evidence_message_id,"
        "confidence,status,conflict_group,created_at,importance,access_count,"
        "last_accessed,evidence_history) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, "user", kind, content, content, None, 0.8, status, None,
         now - age_days * DAY, importance, 0, last_accessed,
         json.dumps(history or [content], ensure_ascii=False)))
    con.commit()
    con.close()


def _msg(path, mid, text, ts):
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                (mid, "user", text, ts))
    con.commit()
    con.close()


def _seed(path, now=None):
    now = time.time() if now is None else now
    for i in range(3):
        _msg(path, f"M-new-{i}", f"最近消息{i}", now - 10 * DAY)
    for i in range(2):
        _msg(path, f"M-old-{i}", f"旧消息{i}", now - 200 * DAY)
    _mem(path, "UM-hot", content="用户喜欢猫", kind="preference", age_days=5,
         importance=0.7, now=now)
    _mem(path, "UM-warm", content="用户喜欢音乐", kind="preference", age_days=45,
         importance=0.6, now=now)
    _mem(path, "UM-cold", content="用户喜欢旅行", kind="plan", age_days=200,
         importance=0.8, now=now)
    _mem(path, "UM-dormant", content="用户以前常爬山", kind="event",
         status="dormant", age_days=300, importance=0.1, now=now)
    _mem(path, "UM-merged", content="用户喜欢咖啡", kind="preference", age_days=10,
         importance=0.7, history=["用户喜欢咖啡", "我很喜欢咖啡"],
         last_accessed=now - 5 * DAY, now=now)
    con = db.connect(path)
    con.execute("INSERT INTO claims(id,subject,predicate,object,polarity,time_ref,"
                "claim_type,evidence_span,confidence,source_level,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("C-1", "user", "喜欢", "猫", 1, "", "preference", "喜欢猫", 0.8, "A",
                 "active", now - DAY))
    con.execute("INSERT INTO summaries(id,content,evidence_json,turns_count,created_at,"
                "kind,year_month,source) VALUES(?,?,?,?,?,?,?,?)",
                ("SUM-stage", "阶段摘要", "{}", 3, now - 2 * DAY, "stage", "", "llm"))
    con.execute("INSERT INTO summaries(id,content,evidence_json,turns_count,created_at,"
                "kind,year_month,source) VALUES(?,?,?,?,?,?,?,?)",
                ("SUM-m", "月要点", "{}", 5, now - 40 * DAY, "monthly", "2026-09", "rule"))
    con.commit()
    con.close()
    memory.archive_cold(path, now=now, days=90)   # 老消息进冷归档
    return now


# --------------------------------------------------------------------------- #
# 指标数字与 SQL 复算一致
# --------------------------------------------------------------------------- #
def test_metrics_numbers_are_consistent(tmp_path):
    path = tmp_path / "m.sqlite3"
    now = _seed(path)
    result = memory.metrics(path, now=now)

    con = db.connect(path)
    try:
        total = con.execute("SELECT count(*) FROM messages").fetchone()[0]
        archived = con.execute(
            "SELECT count(*) FROM messages WHERE archived_at IS NOT NULL").fetchone()[0]
        mem_total = con.execute("SELECT count(*) FROM user_memories").fetchone()[0]
        dormant = con.execute(
            "SELECT count(*) FROM user_memories WHERE status='dormant'").fetchone()[0]
    finally:
        con.close()
    assert result["messages"]["total"] == total
    assert result["messages"]["archived"] == archived
    assert result["messages"]["active"] == total - archived
    assert result["messages"]["oldest_active_days"] == pytest.approx(10.0, abs=0.1)
    assert result["messages"]["newest_days"] == pytest.approx(10.0, abs=0.1)

    mems = result["memories"]
    assert mems["total"] == mem_total == 5
    assert mems["by_status"] == {"active": 4, "dormant": 1}
    assert mems["by_kind"] == {"preference": 3, "plan": 1, "event": 1}
    # 只有 active 参与分层/重要性统计
    assert mems["by_tier"] == {"hot": 2, "warm": 1, "cold": 1}
    assert mems["dormant_rate"] == pytest.approx(1 / 5)
    assert mems["avg_importance"] == pytest.approx((0.7 + 0.6 + 0.8 + 0.7) / 4)
    assert mems["median_importance"] == pytest.approx(0.7)
    assert mems["merged_within_30d"] == 1

    assert result["claims"]["total"] == 1
    assert result["summaries"]["total"] == 2
    assert result["summaries"]["monthly"] == 1
    assert result["summaries"]["latest_month"] == "2026-09"
    assert result["context"]["tokens"] is None
    assert result["retrieval"]["calls"] == 0


def test_empty_db_is_all_zero(tmp_path):
    path = tmp_path / "empty.sqlite3"
    result = memory.metrics(path)
    assert result["messages"]["total"] == 0
    assert result["memories"]["total"] == 0
    assert result["memories"]["by_status"] == {}
    assert result["memories"]["dormant_rate"] == 0.0
    assert result["retrieval"]["calls"] == 0
    assert result["retrieval"]["hit_rate"] is None


# --------------------------------------------------------------------------- #
# 检索计数（内存缓冲 + 批量落盘）
# --------------------------------------------------------------------------- #
def test_retrieval_counters_buffer_and_flush(tmp_path):
    path = tmp_path / "r.sqlite3"
    now = _seed(path)
    memory.retrieve_tiered(path, "喜欢猫", now=now)        # 命中
    memory.retrieve_tiered(path, "火星天气怎么样", now=now)  # 无命中
    result = memory.metrics(path, now=now)["retrieval"]
    assert result["calls"] == 2 and result["hits"] == 1
    assert result["hit_rate"] == pytest.approx(0.5)   # 只有第一条 query 真命中
    assert result["avg_items"] > 0
    assert result["pending"] == 2                       # 还没落盘

    flushed = memory.flush_retrieval_counters(path)
    assert flushed["ok"] and flushed["flushed"]["calls"] == 2
    after = memory.metrics(path, now=now)["retrieval"]
    assert after["calls"] == 2 and after["pending"] == 0
    con = db.connect(path)
    try:
        value = con.execute(
            "SELECT value FROM state WHERE key='mem_retrieval_calls'").fetchone()[0]
    finally:
        con.close()
    assert int(float(value)) == 2


# --------------------------------------------------------------------------- #
# 接口：/api/memory/stats（回环 Host 校验 + 隐私）
# --------------------------------------------------------------------------- #
class _Harness:
    @staticmethod
    def get(path: str, host: str = "127.0.0.1:8082"):
        handler = web_mod.Handler.__new__(web_mod.Handler)
        handler.path = path
        handler.command = "GET"
        handler.requestline = f"GET {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.close_connection = True
        handler.wfile = io.BytesIO()
        handler._headers_buffer = []
        handler.rfile = io.BytesIO(b"")
        handler.headers = {"Host": host}
        handler.do_GET()
        head, _, payload = handler.wfile.getvalue().decode("utf-8").partition("\r\n\r\n")
        status = int(head.split(" ")[1]) if head else 0
        return status, (json.loads(payload) if payload.strip() else None)


def test_memory_stats_api_readonly_and_private(tmp_path, monkeypatch):
    path = tmp_path / "api.sqlite3"
    now = _seed(path)
    # 放入真实昵称和消息原文，断言接口不回传
    _mem(path, "UM-private", content="罗煜莹最喜欢珍珠奶茶", kind="preference",
         age_days=3, now=now)
    _msg(path, "M-private", "真实消息原文：明天见面", now)
    monkeypatch.setattr(web_mod, "DB_PATH", path)

    status, data = _Harness.get("/api/memory/stats?contact=朋友A")
    assert status == 200 and data["ok"] is True
    assert data["contact_alias"] == "朋友A"
    assert "messages" in data and "memories" in data and "retrieval" in data
    body = json.dumps(data, ensure_ascii=False)
    assert "罗煜莹" not in body and "珍珠奶茶" not in body
    assert "真实消息原文" not in body

    status, err = _Harness.get("/api/memory/stats", host="evil.example.com:8082")
    assert status == 403 and err["ok"] is False


def test_memory_stats_alias_is_sanitized(tmp_path, monkeypatch):
    path = tmp_path / "alias.sqlite3"
    _seed(path)
    monkeypatch.setattr(web_mod, "DB_PATH", path)
    status, data = _Harness.get("/api/memory/stats?contact=../../etc/passwd")
    assert status == 200
    assert data["contact_alias"] == "etcpasswd"     # 路径字符被剔除，不回传真实路径


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_metrics_cli_json(tmp_path, capsys):
    path = tmp_path / "cli.sqlite3"
    _seed(path)
    code = metrics.main(["--path", str(path), "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["memories"]["total"] == 5


# --------------------------------------------------------------------------- #
# 每日快照 + 三条异常阈值
# --------------------------------------------------------------------------- #
def test_nightly_snapshot_idempotent(tmp_path):
    path = tmp_path / "nightly.sqlite3"
    now = _seed(path)
    first = nightly.run(path, None, force=True)
    step = first["steps"]["memory_metrics"]
    assert step["ok"] is True
    snapshot_file = metrics.snapshot_dir(path) / time.strftime("%Y-%m-%d")
    files = list(metrics.snapshot_dir(path).glob("*.json"))
    assert len(files) == 1                       # 同日只一份
    assert str(snapshot_file) + ".json" == step["path"]
    assert "memory_warnings" in first
    second = nightly.run(path, None, force=True)
    assert second["steps"]["memory_metrics"]["ok"] is True
    assert len(list(metrics.snapshot_dir(path).glob("*.json"))) == 1  # 覆盖不新增


def test_anomaly_thresholds_each_trigger():
    now = 1_800_000_000.0
    base = {"now": now, "messages": {"active": 100},
            "memories": {"dormant_rate": 0.1},
            "retrieval": {"calls": 10, "hit_rate": 0.5}}

    healthy = metrics.anomalies(base, [])
    assert healthy == []

    dormant = metrics.anomalies(
        {**base, "memories": {"dormant_rate": 0.55}}, [])
    assert any("dormant_rate" in w for w in dormant)

    drop = metrics.anomalies(
        {**base, "messages": {"active": 60}},
        [{"now": now - 5 * DAY, "messages": {"active": 100}}])
    assert any("一周净减" in w for w in drop)
    # 超过 7 天的旧快照不参与比较
    no_drop = metrics.anomalies(
        {**base, "messages": {"active": 60}},
        [{"now": now - 10 * DAY, "messages": {"active": 100}}])
    assert not any("一周净减" in w for w in no_drop)

    hit = metrics.anomalies(
        {**base, "retrieval": {"calls": 10, "hit_rate": 0.02}}, [])
    assert any("hit_rate" in w for w in hit)
    # 没有调用时不报命中率
    no_calls = metrics.anomalies(
        {**base, "retrieval": {"calls": 0, "hit_rate": None}}, [])
    assert not any("hit_rate" in w for w in no_calls)
