# -*- coding: utf-8 -*-
"""A9-P0：发送异步化（sender 线程 + job future）的离线测试。

设计约束：
- 默认 ``send_async=false`` 时必须与旧行为逐字节一致（当前线程持锁直发）；
- 开启后所有真实发送由单一 ``gateway-sender`` 线程执行（窗口/UIA 只能单实例操作）；
- ``_send`` 对外签名/返回契约不变，调用方无感；
- 队列满 → ``blocked/send_queue_full``；等待超时 → ``unverified`` 且**不重发**。
"""
from __future__ import annotations

import queue
import threading
import time

from agi.wechat_gateway.server import Gateway


def _gw(tmp_path, **overrides) -> Gateway:
    cfg = {
        "enabled": True, "allowed_contacts": ["好友A"],
        "monitored_contacts": ["好友A"], "admin_token": "tok",
        "send_mode": "auto", "quiet_hours": [0, 0],
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
    }
    cfg.update(overrides)
    return Gateway(cfg)


def test_send_async_default_off_keeps_direct_path(tmp_path, monkeypatch):
    gw = _gw(tmp_path)
    assert gw._start_send_worker() is False
    assert gw._send_async_enabled is False
    monkeypatch.setattr(gw, "_send_locked",
                        lambda contact, text, **kw: {"ok": True, "status": "sent"})
    assert gw._send("好友A", "hi")["status"] == "sent"
    assert gw.send_async_status()["enabled"] is False


def test_send_async_runs_sender_thread_and_returns_result(tmp_path, monkeypatch):
    gw = _gw(tmp_path, send_async=True)
    seen: list[str] = []

    def fake_locked(contact, text, **kw):
        seen.append(threading.current_thread().name)
        return {"ok": True, "status": "sent", "text": text}

    monkeypatch.setattr(gw, "_send_locked", fake_locked)
    assert gw._start_send_worker() is True
    try:
        out = gw._send("好友A", "异步一句", reactive=True)
        assert out["ok"] is True and out["status"] == "sent"
        assert seen == ["gateway-sender"]
        stats = gw.send_async_status()
        assert stats["enabled"] is True and stats["queue_max"] >= 1
        assert stats["completed"] >= 1
    finally:
        gw.stop()


def test_send_async_serializes_concurrent_calls(tmp_path, monkeypatch):
    gw = _gw(tmp_path, send_async=True)
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def fake_locked(contact, text, **kw):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        with lock:
            active["now"] -= 1
        return {"ok": True, "status": "sent"}

    monkeypatch.setattr(gw, "_send_locked", fake_locked)
    gw._start_send_worker()
    threads = [threading.Thread(target=gw._send, args=("好友A", f"m{i}"))
               for i in range(3)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert active["max"] == 1, "sender 线程必须串行（窗口只能单实例操作）"
    finally:
        gw.stop()


def test_send_async_wait_timeout_is_unverified_and_not_retried(tmp_path, monkeypatch):
    gw = _gw(tmp_path, send_async=True, send_async_wait_timeout_s=1)
    calls = {"n": 0}

    def slow_locked(contact, text, **kw):
        calls["n"] += 1
        time.sleep(1.6)
        return {"ok": True, "status": "sent"}

    monkeypatch.setattr(gw, "_send_locked", slow_locked)
    gw._start_send_worker()
    try:
        out = gw._send("好友A", "超时一句")
        assert out["ok"] is False and out["status"] == "unverified"
        assert out["error"] == "send_async_timeout"
        assert out["possible_sent"] is True
        assert calls["n"] == 1, "超时不允许自动重发"
        assert gw.send_async_status()["timeouts"] >= 1
    finally:
        gw.stop()


def test_send_async_stop_joins_worker(tmp_path):
    gw = _gw(tmp_path, send_async=True)
    gw._start_send_worker()
    assert gw.send_async_status()["alive"] is True
    gw.stop()
    assert gw.send_async_status()["alive"] is False
