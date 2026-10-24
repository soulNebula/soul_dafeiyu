# -*- coding: utf-8 -*-
"""P0-2：主动消息同内容去重 + 同类错误熔断 + 网关死区哨兵。

对应故障（gateway.log 审计）：
1. 11:17–11:19 五个时间点各四类事件同时命中——重试导致重复发；
2. 177 次 proactive_send 失败 82 次（46%），其中 86 次是同一个
   `AttributeError: 'ProfileWnd' object has no attribute 'topui'`；
3. 13:12–16:07 三小时零事件，进程还活着——「没消息」和「死了」长得一样。
"""
from __future__ import annotations

import queue

import pytest

from agi.selflearn import ticker
from agi.wechat_gateway import config as gw_config
from agi.wechat_gateway import proactive_worker
from agi.wechat_gateway.server import Gateway

CONTACT = "好友A"


@pytest.fixture(autouse=True)
def _contact_db(tmp_path, monkeypatch):
    """把 contact_db 固定到 tmp 下一个已存在目录的库（去重/熔断要跨轮持久化）。"""
    path = tmp_path / "contact.sqlite3"
    monkeypatch.setattr("agi.wechat_gateway.config.contact_db",
                        lambda contact: path)
    return path


class FakeGateway:
    """proactive_worker.run_cycle 需要的最小网关替身。"""

    def __init__(self, **cfg):
        self.cfg = {
            "proactive_enabled": True,
            "proactive_probability": 1.0,   # 本文件只测去重/熔断，概率闸门恒开
            "allowed_contacts": [CONTACT],
            "proactive_contacts": [],
            **cfg,
        }
        self.sent: list[tuple] = []
        self.records: list[tuple] = []
        self.last_turns: dict = {}
        self.last_proactive: dict = {}
        self.send_results: list[dict] = []

    def _send(self, contact, text, **kw):
        self.sent.append((contact, text, kw))
        if self.send_results:
            return self.send_results.pop(0)
        return {"ok": True, "status": "sent"}

    def _record(self, kind, data):
        self.records.append((kind, data))


def _tick(*texts: str, turn_id: str = "T-PRO-1") -> dict:
    msgs = list(texts) or ["在吗"]
    return {"ok": True, "ran": True, "gate": {"allowed": True},
            "proactive": {"ok": True, "available": True,
                          "messages": msgs, "message": msgs[0], "turn_id": turn_id}}


def _failed(addr: str = "7f00aa11", exc: str = "AttributeError",
            key: str | None = None) -> dict:
    status = {"ok": False, "status": "failed",
              "error": f"{exc}: 'ProfileWnd' object has no attribute 'topui' at 0x{addr}"}
    if key:
        status["error_key"] = key
    return status


def _events(gateway, kind: str) -> list[dict]:
    return [data for k, data in gateway.records if k == kind]


# --------------------------------------------------------------------------- #
# 同内容去重窗口
# --------------------------------------------------------------------------- #
def test_dedupe_same_text_within_window_is_skipped(monkeypatch):
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick("在干嘛"))
    g = FakeGateway()
    first = proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert first["sent"] == 1 and first["deduped"] == 0

    second = proactive_worker.run_cycle(g, client=object(), now=200.0)
    assert second["sent"] == 0 and second["deduped"] == 1
    assert len(g.sent) == 1                    # 第二次没有调 _send
    deduped = _events(g, "proactive_deduped")
    assert len(deduped) == 1
    assert deduped[0]["hit_ts"] == 100.0       # 带命中的历史 ts
    assert deduped[0]["text"] == "在干嘛"
    assert deduped[0]["window_s"] == 1800.0


def test_dedupe_similar_text_jaccard_is_skipped(monkeypatch):
    # 字符集 {今天有点累} vs {今天有点累啊}：Jaccard = 5/6 ≈ 0.833 ≥ 0.8
    assert proactive_worker.text_similar("今天有点累", "今天有点累啊")
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick("今天有点累"))
    g = FakeGateway()
    proactive_worker.run_cycle(g, client=object(), now=100.0)
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick("今天有点累啊"))
    out = proactive_worker.run_cycle(g, client=object(), now=300.0)
    assert out["sent"] == 0 and out["deduped"] == 1
    assert len(g.sent) == 1


def test_dedupe_different_text_is_sent(monkeypatch):
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick("今天有点累"))
    g = FakeGateway()
    proactive_worker.run_cycle(g, client=object(), now=100.0)
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick("明天出去吃饭吗"))
    out = proactive_worker.run_cycle(g, client=object(), now=300.0)
    assert out["sent"] == 1 and out["deduped"] == 0
    assert [s[1] for s in g.sent] == ["今天有点累", "明天出去吃饭吗"]


def test_dedupe_window_expiry_allows_same_text(monkeypatch):
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick("在干嘛"))
    g = FakeGateway(proactive_dedupe_window_s=60)
    proactive_worker.run_cycle(g, client=object(), now=100.0)
    # 窗口 60s：now=161 时 first(ts=100) 已在窗口外 → 允许重发
    out = proactive_worker.run_cycle(g, client=object(), now=161.0)
    assert out["sent"] == 1 and out["deduped"] == 0
    assert len(g.sent) == 2


def test_dedupe_not_triggered_by_failed_send(monkeypatch):
    """只有真正发出去（status=sent）的文本才参与去重；失败/草稿不算。"""
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick("在干嘛"))
    g = FakeGateway()
    g.send_results = [_failed(), {"ok": True, "status": "sent"}]
    proactive_worker.run_cycle(g, client=object(), now=100.0)
    out = proactive_worker.run_cycle(g, client=object(), now=200.0)
    assert out["sent"] == 1 and out["deduped"] == 0


# --------------------------------------------------------------------------- #
# 同类错误连续 3 次熔断
# --------------------------------------------------------------------------- #
def test_breaker_opens_after_three_same_errors(monkeypatch):
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick("在吗"))
    g = FakeGateway()
    # 同一句 topui 错误，只是对象地址不同（真实日志就是 86 次不同地址）
    g.send_results = [_failed("7f00aa11"), _failed("7f00bb22"), _failed("7f00cc33")]
    for i in range(3):
        out = proactive_worker.run_cycle(g, client=object(), now=100.0 + i * 10)
        assert out["sent"] == 0, out
    opens = _events(g, "proactive_breaker_open")
    assert len(opens) == 1, g.records
    assert opens[0]["consecutive"] == 3
    assert opens[0]["error_key"].startswith("AttributeError")
    assert "0x7f00" not in opens[0]["error_key"]      # 地址已归一化
    assert proactive_worker.breaker_snapshot(g, now=130.0)["open"] is True

    # 第 4 轮：熔断期内不发送，只记 proactive_breaker_open
    sends_before = len(g.sent)
    out4 = proactive_worker.run_cycle(g, client=object(), now=200.0)
    assert out4["ran"] is False and out4["reason"] == "breaker_open"
    assert len(g.sent) == sends_before
    assert len(_events(g, "proactive_breaker_open")) == 2


def test_breaker_cooldown_elapsed_closes_and_resumes(monkeypatch):
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick("在吗"))
    g = FakeGateway()
    g.send_results = [_failed(), _failed(), _failed()]
    for i in range(3):
        proactive_worker.run_cycle(g, client=object(), now=100.0 + i)
    opens = _events(g, "proactive_breaker_open")
    resume_at = opens[0]["until"] + 1.0

    out = proactive_worker.run_cycle(g, client=object(), now=resume_at)
    assert any(k == "proactive_breaker_close" for k, _ in g.records)
    assert out["sent"] == 1
    snapshot = proactive_worker.breaker_snapshot(g, now=resume_at)
    assert snapshot["open"] is False and snapshot["consecutive"] == 0


def test_breaker_success_resets_consecutive(monkeypatch):
    # 每轮用不同文本，避免命中同内容去重（这里只测熔断计数）
    texts = [f"第{i}句话" for i in range(1, 6)]
    calls = {"n": 0}

    def tick_text(*a, **k):
        text = texts[calls["n"]]
        calls["n"] += 1
        return _tick(text)

    monkeypatch.setattr(ticker, "run_once", tick_text)
    g = FakeGateway()
    g.send_results = [_failed(), _failed(), {"ok": True, "status": "sent"},
                      _failed(), _failed()]
    for i in range(5):
        proactive_worker.run_cycle(g, client=object(), now=100.0 + i)
    assert _events(g, "proactive_breaker_open") == []
    assert proactive_worker.breaker_snapshot(g, now=200.0)["consecutive"] == 2


def test_breaker_uses_error_key_field_from_task18(monkeypatch):
    """task-18 会写 error_key：不同原文但同 error_key 必须算「同类」。"""
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick("在吗"))
    g = FakeGateway()
    g.send_results = [
        {"ok": False, "status": "failed", "error": f"variant {i} at 0x{i:08x}",
         "error_key": "ProfileWnd.topui"}
        for i in range(3)
    ]
    for i in range(3):
        proactive_worker.run_cycle(g, client=object(), now=100.0 + i)
    opens = _events(g, "proactive_breaker_open")
    assert opens and opens[0]["error_key"] == "ProfileWnd.topui"


def test_normalize_error_strips_addresses_and_caps_length():
    a = proactive_worker.normalize_error(
        "AttributeError: foo at 0x7f00112233   boom")
    b = proactive_worker.normalize_error(
        "AttributeError: foo at 0xdeadbeef boom")
    assert a == b
    assert "0x7f00" not in a and "0xdead" not in b
    assert len(a) <= 60
    assert proactive_worker.error_key(
        {"ok": False, "error": "x at 0x1234"}) == "x at 0xADDR"
    assert proactive_worker.error_key(
        {"ok": False, "error": "whatever", "error_key": "K-1"}) == "K-1"


def test_breaker_config_defaults():
    assert gw_config.DEFAULTS["proactive_dedupe_window_s"] == 1800
    assert gw_config.DEFAULTS["proactive_breaker_threshold"] == 3
    assert gw_config.DEFAULTS["proactive_breaker_cooldown_s"] == 1800
    assert gw_config.DEFAULTS["gateway_stale_alert_s"] == 1800


# --------------------------------------------------------------------------- #
# 死区哨兵（gateway_stale）
# --------------------------------------------------------------------------- #
def _gw(tmp_path, **overrides) -> Gateway:
    cfg = {
        "enabled": True,
        "port": 801,
        "dry_run": True,
        "allowed_contacts": [CONTACT],
        "send_mode": "auto",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
    }
    cfg.update(overrides)
    return Gateway(cfg)


def test_gateway_stale_alert_written_once_per_30min(tmp_path):
    g = _gw(tmp_path)
    g.last_event_ts = 1000.0
    assert g._check_stale(now=1000.0 + 1801) is True
    stale = [e for e in g.recent if e["kind"] == "gateway_stale"]
    assert len(stale) == 1
    assert stale[0]["last_event_ts"] == 1000.0
    assert stale[0]["idle_s"] == 1801.0
    assert stale[0]["threshold_s"] == 1800.0

    # 同 30 分钟内不重复写
    assert g._check_stale(now=1000.0 + 1801 + 600) is False
    assert len([e for e in g.recent if e["kind"] == "gateway_stale"]) == 1

    # 距上次告警满 30 分钟才允许再写
    assert g._check_stale(now=1000.0 + 1801 + 1801) is True
    assert len([e for e in g.recent if e["kind"] == "gateway_stale"]) == 2


def test_gateway_stale_not_written_when_recent_event(tmp_path):
    g = _gw(tmp_path)
    now = 5000.0
    g.last_event_ts = now - 100.0     # 100 秒前有活动 < 1800
    assert g._check_stale(now=now) is False
    assert not [e for e in g.recent if e["kind"] == "gateway_stale"]


def test_record_refreshes_last_event_ts_but_stale_does_not(tmp_path):
    g = _gw(tmp_path)
    g.last_event_ts = 0.0
    g._record("inbound", {"friend": CONTACT, "text": "hi"})
    assert g.last_event_ts > 0
    before = g.last_event_ts
    g.last_event_ts = 1.0
    g._record("gateway_stale", {"idle_s": 3600.0})   # 心跳不算活动
    assert g.last_event_ts == 1.0
    g.last_event_ts = before


def test_work_loop_calls_stale_check(tmp_path, monkeypatch):
    g = _gw(tmp_path)
    called: list[int] = []

    def fake_check():
        called.append(1)
        g.stop_event.set()
        return False

    monkeypatch.setattr(g, "_check_stale", fake_check)
    monkeypatch.setattr(g, "_flush_pending", lambda *a, **k: None)
    g._work()
    assert called == [1]


# --------------------------------------------------------------------------- #
# server.py 兜底：调度异常不能退回 5 分钟旧密度
# --------------------------------------------------------------------------- #
def test_proactive_loop_schedule_error_falls_back_to_one_hour(tmp_path, monkeypatch):
    g = _gw(tmp_path, proactive_enabled=True)
    monkeypatch.setattr(g, "_proactive_cycle", lambda: {"ok": True})

    def boom(*a, **k):
        raise RuntimeError("schedule boom")

    monkeypatch.setattr(proactive_worker, "next_interval", boom)
    delays: list[float] = []

    def fake_wait(delay):
        delays.append(delay)
        g.stop_event.set()

    monkeypatch.setattr(g.stop_event, "wait", fake_wait)
    g._proactive_loop()
    assert delays and delays[0] == 3600.0
    errors = [e for e in g.recent if e["kind"] == "proactive_schedule_error"]
    assert errors
