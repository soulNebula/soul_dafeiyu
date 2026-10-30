# -*- coding: utf-8 -*-
"""A9-P2：回复生成池（每联系人串行、跨联系人并行、背压、stop）与网关接入。"""
from __future__ import annotations

import threading
import time

from agi.wechat_gateway import config as gw_config
from agi.wechat_gateway.reply_pool import ReplyJob, ReplyPool
from agi.wechat_gateway.server import Gateway


def _job(contact, text="x"):
    return ReplyJob(contact=contact, msg={"friend": contact, "text": text},
                    texts=[text])


def _gw(tmp_path, **overrides) -> Gateway:
    cfg = {
        "enabled": True, "allowed_contacts": ["好友A", "好友B"],
        "monitored_contacts": ["好友A", "好友B"], "admin_token": "tok",
        "dry_run": True, "send_mode": "auto", "quiet_hours": [0, 0],
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
    }
    cfg.update(overrides)
    return Gateway(cfg)


def test_p2_defaults_off_and_worker_count():
    assert gw_config.DEFAULTS["reply_pool_enabled"] is False
    assert gw_config.DEFAULTS["reply_workers"] == 2


def test_same_contact_is_serialized_and_order_kept():
    order: list[str] = []
    active = {"now": 0, "max_same": 0}
    same_lock = threading.Lock()

    def runner(job):
        if job.contact == "A":
            with same_lock:
                active["now"] += 1
                active["max_same"] = max(active["max_same"], active["now"])
            time.sleep(0.05)
            with same_lock:
                active["now"] -= 1
        order.append(job.texts[0] if job.texts else "")

    pool = ReplyPool(runner, workers=2).start()
    try:
        pool.submit(_job("A", "A1"))
        pool.submit(_job("A", "A2"))
        pool.submit(_job("A", "A3"))
        deadline = time.time() + 3
        while pool.inflight() > 0 and time.time() < deadline:
            time.sleep(0.01)
        assert [t for t in order if t.startswith("A")] == ["A1", "A2", "A3"]
        assert active["max_same"] == 1
    finally:
        pool.stop()


def test_different_contacts_can_run_in_parallel():
    entered = threading.Barrier(2, timeout=2)

    def runner(job):
        try:
            entered.wait()
        except threading.BrokenBarrierError:
            raise AssertionError("两个联系人没有并行执行（生成池退化了）")

    pool = ReplyPool(runner, workers=2).start()
    try:
        # 灰度回归：真实昵称（好友A/好友B）曾经被 hash 撞到同一个 worker
        assert pool.submit(_job("好友A")) == "ok"
        assert pool.submit(_job("好友B")) == "ok"
        deadline = time.time() + 3
        while pool.inflight() > 0 and time.time() < deadline:
            time.sleep(0.01)
        assert pool.inflight() == 0
        assert pool.snapshot()["completed"] == 2
    finally:
        pool.stop()


def test_queue_full_returns_rejected():
    gate = threading.Event()
    entered = threading.Event()

    def runner(job):
        entered.set()
        gate.wait(2.0)

    pool = ReplyPool(runner, workers=1, queue_size=1).start()
    try:
        assert pool.submit(_job("A")) == "ok"
        assert entered.wait(1.0)
        assert pool.submit(_job("A")) == "ok"       # 队列里 1 条
        assert pool.submit(_job("A")) == "queue_full"
    finally:
        gate.set()
        pool.stop()


def test_on_done_reports_error_and_success():
    seen: list[tuple[str, str]] = []

    def runner(job):
        if job.texts and job.texts[0] == "bad":
            raise RuntimeError("boom")

    pool = ReplyPool(runner, workers=1).start()
    try:
        ok = ReplyJob(contact="A", msg={}, texts=["ok"],
                      on_done=lambda job, err: seen.append(("ok", err)))
        bad = ReplyJob(contact="A", msg={}, texts=["bad"],
                       on_done=lambda job, err: seen.append(("bad", err)))
        pool.submit(ok)
        deadline = time.time() + 3
        while len(seen) < 1 and time.time() < deadline:
            time.sleep(0.01)
        pool.submit(bad)
        deadline = time.time() + 3
        while len(seen) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert ("ok", "") in seen
        assert any(kind == "bad" and "boom" in err for kind, err in seen)
    finally:
        pool.stop()


def test_gateway_flush_submits_to_pool_and_drains(monkeypatch, tmp_path):
    gw = _gw(tmp_path, reply_pool_enabled=True, reply_workers=2,
             reply_debounce_ms=50)
    called: list[tuple[str, str]] = []
    lock = threading.Lock()

    def fake_reply(msg):
        with lock:
            called.append((threading.current_thread().name, str(msg.get("text"))))
        return {"ok": True}

    monkeypatch.setattr(gw, "_reply", fake_reply)
    assert gw._start_reply_pool() is True
    try:
        gw.pending["好友A"] = {"msg": {"friend": "好友A", "kind": "text"},
                               "texts": ["你好"], "deadline": 0}
        gw._flush_pending(force=True)
        assert gw._reply_inflight == {"好友A"}
        deadline = time.time() + 3
        while gw._reply_inflight and time.time() < deadline:
            gw._drain_reply_done()
            time.sleep(0.01)
        gw._drain_reply_done()
        assert gw.pending == {}
        assert called and called[0][0].startswith("gateway-reply")
        assert called[0][1] == "你好"
    finally:
        gw.stop()


def test_gateway_default_off_stays_inline(tmp_path, monkeypatch):
    gw = _gw(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(gw, "_reply", lambda msg: calls.append(msg.get("text")))
    gw.pending["好友A"] = {"msg": {"friend": "好友A", "kind": "text"},
                           "texts": ["内联"], "deadline": 0}
    gw._flush_pending(force=True)
    assert calls == ["内联"]
    assert gw._reply_inflight == set()
