# -*- coding: utf-8 -*-
"""A9-P1：发送任务池的调度语义（优先级 / per-contact FIFO / TTL / 背压）。

只测 SenderPool 纯调度，不碰 Gateway/微信；runner 用可控函数模拟真实发送。
"""
from __future__ import annotations

import threading
import time

from agi.wechat_gateway.send_async import (
    PRIORITY_MANUAL,
    PRIORITY_PROACTIVE,
    PRIORITY_REACTIVE,
    SendJob,
    SenderPool,
)


def _job(contact, text, priority=PRIORITY_REACTIVE, ttl_s=180.0):
    return SendJob(contact=contact, text=text, kwargs={}, priority=priority,
                   ttl_s=ttl_s)


def _wait(pool, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pool.pending() == 0:
            return True
        time.sleep(0.01)
    return False


def test_same_contact_keeps_fifo_order():
    order: list[str] = []
    lock = threading.Lock()

    def runner(job):
        with lock:
            order.append(job.text)
        time.sleep(0.03)
        return {"ok": True, "status": "sent"}

    pool = SenderPool(runner).start()
    try:
        pool.submit(_job("A", "A1"))
        pool.submit(_job("A", "A2"))
        pool.submit(_job("A", "A3"))
        assert pool.submit(_job("B", "B1")) == "ok"
        assert _wait(pool)
        assert [t for t in order if t.startswith("A")] == ["A1", "A2", "A3"]
        assert "B1" in order
    finally:
        pool.stop()


def test_priority_cross_contact_picks_manual_first():
    order: list[str] = []

    def runner(job):
        order.append(job.text)
        return {"ok": True, "status": "sent"}

    pool = SenderPool(runner).start()
    try:
        pool.submit(_job("B", "proactive", PRIORITY_PROACTIVE))
        pool.submit(_job("C", "reactive", PRIORITY_REACTIVE))
        pool.submit(_job("A", "manual", PRIORITY_MANUAL))
        assert _wait(pool)
        assert order[0] == "manual"
        assert order.index("reactive") < order.index("proactive")
    finally:
        pool.stop()


def test_ttl_expired_job_is_not_sent():
    gate = threading.Event()
    seen: list[str] = []

    def runner(job):
        seen.append(job.text)
        if job.text == "slow":
            gate.wait(2.0)
        return {"ok": True, "status": "sent"}

    pool = SenderPool(runner).start()
    try:
        slow = _job("A", "slow")
        pool.submit(slow)
        while "slow" not in seen:
            time.sleep(0.01)
        stale = _job("A", "stale", PRIORITY_REACTIVE, ttl_s=0.05)
        pool.submit(stale)
        time.sleep(0.1)
        gate.set()
        assert stale.done.wait(2.0)
        assert stale.result["error"] == "send_expired_ttl"
        assert "stale" not in seen
        assert pool.snapshot()["expired"] >= 1
    finally:
        gate.set()
        pool.stop()


def test_global_backpressure_rejects_when_pending_full():
    gate = threading.Event()
    entered = threading.Event()

    def runner(job):
        entered.set()
        gate.wait(2.0)
        return {"ok": True, "status": "sent"}

    pool = SenderPool(runner, max_pending=1).start()
    try:
        pool.submit(_job("A", "running"))
        assert entered.wait(1.0)
        assert pool.submit(_job("A", "queued")) == "ok"
        assert pool.submit(_job("B", "overflow")) == "queue_full"
    finally:
        gate.set()
        pool.stop()


def test_per_contact_backpressure():
    gate = threading.Event()
    entered = threading.Event()

    def runner(job):
        entered.set()
        gate.wait(2.0)
        return {"ok": True, "status": "sent"}

    pool = SenderPool(runner, max_pending=10, max_per_contact=1).start()
    try:
        pool.submit(_job("A", "running"))
        assert entered.wait(1.0)
        assert pool.submit(_job("A", "queued")) == "ok"
        assert pool.submit(_job("A", "overflow")) == "contact_queue_full"
        assert pool.submit(_job("B", "other-contact")) == "ok"
    finally:
        gate.set()
        pool.stop()


def test_stop_cancels_pending_jobs():
    gate = threading.Event()
    entered = threading.Event()

    def runner(job):
        entered.set()
        gate.wait(2.0)
        return {"ok": True, "status": "sent"}

    pool = SenderPool(runner).start()
    queued = _job("A", "queued")
    try:
        pool.submit(_job("A", "running"))
        assert entered.wait(1.0)
        pool.submit(queued)
        pool.stop(timeout=0.5)
        assert queued.done.is_set()
        assert queued.result["status"] == "cancelled"
    finally:
        gate.set()
