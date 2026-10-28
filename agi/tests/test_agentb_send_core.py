# -*- coding: utf-8 -*-
"""task-1（Agent B 主链健壮性）：发送锁 / 去重键 / 冲刷重试 / 队列丢弃 / 兜底口径。

覆盖验收项：
1) 全局发送锁（_send + send_media_batch 串行化，RLock 可递归）
4) 去重键修正（idem_key：同 turn 重试拦、不同 turn 同文本放行）
2) _flush_pending 处理成功才 pop；失败保留并重试；超上限落盘 pending_lost.jsonl
6) 队列满不静默丢（queue_overflow + dropped_inbound + /status）
7) 窗口/UIA 兜底必定 verified=False/unverified=True、不计入 sent，/status 可观测
A) 媒体路径白名单 fail-closed（Lead 追加）
B) 单 worker 取舍文档 + queue_size / in_flight_contact 可观测（Lead 追加）
C) 日志路径调用时解析（env SOULAGENT_LOG_DIR / 数据目录），不在 import 时写死

全部离线：dry_run 或 monkeypatch sender/subprocess，不 import 真实微信、不联网。
"""
from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from agi.wechat_gateway.server import FLUSH_RETRY_MAX, Gateway


def _gw(tmp_path, **over):
    cfg = {
        "enabled": True, "port": 801, "dry_run": True, "send_mode": "auto",
        "allowed_contacts": ["好友A"], "monitored_contacts": ["好友A"],
        "admin_token": "tok_secure_123",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "quiet_hours": [0, 0], "min_interval_s": 0,
        "rate_per_minute": 50, "reactive_rate_per_minute": 50,
        "hourly_limit": 40, "daily_limit": 200,
    }
    cfg.update(over)
    return Gateway(cfg, stop_path=tmp_path / "STOP",
                   config_path=tmp_path / "config.json")


def _kinds(gw, kind):
    return [e for e in gw.recent if e.get("kind") == kind]


def _pending_buf(gw, contact="好友A", texts=None):
    gw.pending[contact] = {
        "msg": {"friend": contact, "kind": "text", "id": "m-1"},
        "texts": list(texts or ["攒着的第一句"]),
        "deadline": time.time() - 1.0,
    }


# --------------------------------------------------------------------------- #
# 1. 全局发送锁：并发 _send 的最大并发数必须是 1
# --------------------------------------------------------------------------- #
def test_global_send_lock_serializes_concurrent_sends(tmp_path):
    gw = _gw(tmp_path)
    state = {"active": 0, "max": 0, "calls": []}

    class _SlowSender:
        def send(self, contact, text, dry_run=True):
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            state["calls"].append(text)
            time.sleep(0.05)          # 放大竞态窗口：没有锁时必然 >1
            state["active"] -= 1
            return {"ok": True, "status": "planned"}

    gw.sender = _SlowSender()
    results: list[dict] = []

    def worker(index):
        results.append(gw._send("好友A", f"并发送达-{index}", reactive=True))

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not any(thread.is_alive() for thread in threads), "发送锁可能死锁"
    assert state["max"] == 1, f"发送没有串行化（max_concurrency={state['max']}）"
    assert len(results) == 6 and all(r.get("ok") for r in results)


def test_send_lock_is_reentrant_for_nested_paths(tmp_path):
    """_send → _on_send_failure（兜底发送）在同一线程内嵌套，普通 Lock 会自锁。"""
    gw = _gw(tmp_path)
    done = {}

    def run():
        with gw._send_lock:                      # 模拟外层已持有
            done["out"] = gw._send("好友A", "递归闸门", reactive=True)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "RLock 递归调用死锁了"
    assert done["out"].get("ok") is True


def test_media_batch_waits_for_global_send_lock(tmp_path):
    """send_media_batch 与 _send 共用同一把锁（子进程/UIA 阶段也被串行化）。"""
    gw = _gw(tmp_path, media_enabled=True)
    started, finished = threading.Event(), threading.Event()

    def run():
        started.set()
        gw.send_media_batch("好友A", [{"type": "text", "text": "媒体一句"}])
        finished.set()

    with gw._send_lock:
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert started.wait(2)
        time.sleep(0.15)
        assert not finished.is_set(), "媒体发送没有等全局发送锁"
    assert finished.wait(5)
    thread.join(2)


# --------------------------------------------------------------------------- #
# 4. 去重键：同一句话来自两个不同 turn 都要发；同一 idem_key 60s 内重复要拦
# --------------------------------------------------------------------------- #
def test_same_text_from_different_turns_is_not_deduped(tmp_path):
    gw = _gw(tmp_path)
    first = gw._send("好友A", "在呢", reactive=True, idem_key="T-100:0")
    second = gw._send("好友A", "在呢", reactive=True, idem_key="T-101:0")
    assert first.get("ok") is True and second.get("ok") is True
    assert not [e for e in _kinds(gw, "send_blocked") if e.get("reason") == "dedupe"]


def test_same_idem_key_within_window_is_blocked(tmp_path):
    gw = _gw(tmp_path)
    assert gw._send("好友A", "重试的一句", reactive=True,
                    idem_key="T-200:0").get("ok") is True
    again = gw._send("好友A", "重试的一句", reactive=True, idem_key="T-200:0")
    assert again["status"] == "blocked" and again["error"] == "dedupe"
    assert [e for e in _kinds(gw, "send_blocked") if e.get("reason") == "dedupe"]


def test_legacy_text_dedupe_still_works_without_idem_key(tmp_path):
    """向后兼容：不传 idem_key 的旧调用方仍按文本 60s 去重（既有测试语义）。"""
    gw = _gw(tmp_path)
    assert gw._send("好友A", "旧调用一句", reactive=True).get("ok") is True
    again = gw._send("好友A", "旧调用一句", reactive=True)
    assert again["error"] == "dedupe"


# --------------------------------------------------------------------------- #
# 2. _flush_pending：成功才 pop；失败保留重试；超上限落盘
# --------------------------------------------------------------------------- #
def test_flush_pending_keeps_and_retries_on_failure(tmp_path, monkeypatch):
    gw = _gw(tmp_path)
    calls = {"n": 0}

    def boom(msg):
        calls["n"] += 1
        raise RuntimeError("LLM 崩了")

    monkeypatch.setattr(gw, "_reply", boom)
    _pending_buf(gw)
    gw._flush_pending()

    buf = gw.pending.get("好友A")
    assert buf is not None, "失败的攒批消息被丢掉了"
    assert buf["texts"] == ["攒着的第一句"]          # 文本没丢
    assert buf["attempt"] == 1
    assert buf["deadline"]                          # deadline 原样保留
    assert _kinds(gw, "flush_retry"), "失败重试没有审计事件"
    assert gw.flush_retry == 1

    # 退避期内（retry_at 未到）不重复打 LLM
    gw._flush_pending()
    assert calls["n"] == 1, "退避没有生效，0.2s 一轮地打 LLM"

    # 第二次失败 → attempt=2，仍在内存
    gw._flush_pending(force=True)
    assert gw.pending["好友A"]["attempt"] == 2

    # 第三次成功 → 才 pop
    seen = {}
    monkeypatch.setattr(gw, "_reply", lambda msg: seen.setdefault("text", msg["text"]))
    gw._flush_pending(force=True)
    assert "好友A" not in gw.pending
    assert seen["text"] == "攒着的第一句"


def test_flush_pending_drops_after_limit_but_persists(tmp_path, monkeypatch):
    gw = _gw(tmp_path)

    def always_boom(msg):
        raise RuntimeError("always boom")

    monkeypatch.setattr(gw, "_reply", always_boom)
    _pending_buf(gw, texts=["补发的第一句", "补发的第二句"])
    for _ in range(FLUSH_RETRY_MAX):
        gw._flush_pending(force=True)

    assert "好友A" not in gw.pending
    assert gw.flush_dropped == 1
    dropped = _kinds(gw, "flush_dropped")
    assert dropped and dropped[-1]["attempt"] == FLUSH_RETRY_MAX
    assert dropped[-1]["lost_path"] == str(gw.pending_lost_path)
    # 落盘（可人工捞回来补发），不是静默丢
    lines = [json.loads(line) for line in
             gw.pending_lost_path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert len(lines) == 1
    assert lines[0]["texts"] == ["补发的第一句", "补发的第二句"]
    assert lines[0]["attempt"] == FLUSH_RETRY_MAX
    assert "always boom" in lines[0]["error"]


def test_flush_pending_engine_off_still_clears_buffer(tmp_path):
    """引擎停 = 不补发旧内容（P18-A 既有语义不变）。"""
    gw = _gw(tmp_path)
    _pending_buf(gw)
    gw.engine_enabled = False
    gw._flush_pending()
    assert gw.pending == {}


# --------------------------------------------------------------------------- #
# 6. 队列满：queue_overflow + 计数 + /status
# --------------------------------------------------------------------------- #
def test_queue_overflow_is_counted_and_visible(tmp_path):
    gw = _gw(tmp_path, auto_reply=True)
    while True:
        try:
            gw.queue.put_nowait({"friend": "好友A", "text": "占位"})
        except queue.Full:
            break

    gw.on_inbound({"friend": "好友A", "text": "溢出的这一条", "id": "x1"})
    assert gw.dropped_inbound == 1
    overflow = _kinds(gw, "queue_overflow")
    assert overflow and overflow[-1]["dropped_inbound"] == 1
    status = gw.status()
    assert status["dropped_inbound"] == 1
    assert status["queue_size"] == gw.queue.maxsize


# --------------------------------------------------------------------------- #
# 7. 兜底口径：verified=False / unverified=True，不计 sent，/status 可见
# --------------------------------------------------------------------------- #
def _fake_wxauto_python(tmp_path, monkeypatch):
    import agi.wechat_gateway.server as server_mod
    py = tmp_path / "python.exe"
    py.write_text("", encoding="utf-8")
    monkeypatch.setattr(server_mod, "WXAUTO_PYTHON", py)
    return server_mod


def test_window_text_fallback_is_unverified_not_sent(tmp_path, monkeypatch):
    from agi.wechat_gateway import win_send

    server_mod = _fake_wxauto_python(tmp_path, monkeypatch)

    class _Proc:
        stdout = ""
        stderr = "wxauto boom"

    monkeypatch.setattr(server_mod.subprocess, "run", lambda *a, **k: _Proc())
    # 兜底实现没给 verified → 绝不能因为 ok=True 就算送达
    monkeypatch.setattr(win_send, "send_via_window",
                        lambda text, cfg: {"ok": True, "status": "sent"})
    gw = _gw(tmp_path, dry_run=False, fallback_enabled=True, fallback_mode="manual")

    out = gw._send("好友A", "窗口兜底的一句")
    assert out["ok"] is True
    assert out["verified"] is False and out["unverified"] is True
    assert out["classification"] == "unverified"
    assert gw.sent_today == 0 and len(gw.send_times) == 0   # 不占发送预算


def test_uia_text_fallback_is_unverified_not_sent(tmp_path, monkeypatch):
    from agi.wechat_gateway import uia_sender

    server_mod = _fake_wxauto_python(tmp_path, monkeypatch)

    class _Proc:
        stdout = ""
        stderr = "wxauto boom"

    monkeypatch.setattr(server_mod.subprocess, "run", lambda *a, **k: _Proc())
    # uia_sender 主通道会自称 verified=True；作为**兜底**不能被采信
    monkeypatch.setattr(uia_sender, "send_to_contact",
                        lambda contact, text, cfg: {"ok": True, "status": "sent",
                                                    "verified": True})
    gw = _gw(tmp_path, dry_run=False, fallback_enabled=True,
             fallback_send_mode="uia")

    out = gw._send("好友A", "UIA 兜底的一句")
    assert out["classification"] == "unverified"
    assert gw.sent_today == 0 and len(gw.send_times) == 0
    assert _kinds(gw, "fallback_unverified")


def test_uia_media_fallback_counted_as_unverified_in_status(tmp_path, monkeypatch):
    from agi.wechat_gateway import uia_sender

    server_mod = _fake_wxauto_python(tmp_path, monkeypatch)
    image = tmp_path / "pic.png"
    image.write_bytes(b"png")

    class _Proc:
        stdout = ('{"items":[{"ok":false,"status":"failed","type":"image",'
                  '"error":"wxauto boom"}]}')
        stderr = ""

    monkeypatch.setattr(server_mod.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(uia_sender, "send_media",
                        lambda contact, item, cfg: {"ok": True, "status": "sent",
                                                    "verified": True, "method": "uia"})
    gw = _gw(tmp_path, dry_run=False, media_enabled=True,
             fallback_enabled=True, fallback_send_mode="uia")

    batch = gw.send_media_batch("好友A", [{"type": "image", "path": str(image)}])
    item = batch["items"][0]
    assert item["ok"] is True
    assert item["verified"] is False and item["unverified"] is True
    assert item["warning"] == "fallback_send_unverified"
    # 网关口径：不计 sent/预算，单独计 unverified 且 /status 能看到
    assert gw.sent == 0 and gw.sent_today == 0 and len(gw.send_times) == 0
    assert gw.unverified == 1
    assert gw.status()["unverified"] == 1
    assert _kinds(gw, "send_media_unverified")


# --------------------------------------------------------------------------- #
# A) 媒体白名单与 _send 一致：fail-closed（Lead 追加）
# --------------------------------------------------------------------------- #
def test_media_batch_empty_allowlist_is_fail_closed(tmp_path):
    gw = _gw(tmp_path, media_enabled=True, allowed_contacts=[])
    out = gw.send_media_batch("好友A", [{"type": "text", "text": "媒体一句"}])
    assert out["status"] == "blocked" and out["error"] == "no_allowlist"
    assert any(e.get("kind") == "send_blocked"
               and e.get("reason") == "no_allowlist" for e in gw.recent)


def test_media_batch_unknown_contact_still_blocked(tmp_path):
    gw = _gw(tmp_path, media_enabled=True)
    out = gw.send_media_batch("陌生人", [{"type": "text", "text": "hi"}])
    assert out["status"] == "blocked" and out["error"] == "not_allowed"


# --------------------------------------------------------------------------- #
# B) 单 worker 取舍：文档如实 + queue_size / in_flight_contact 可观测
# --------------------------------------------------------------------------- #
def test_single_worker_tradeoff_documented():
    from agi.wechat_gateway.server import Gateway as _G

    work_doc = _G._work.__doc__ or ""
    reply_doc = _G._reply.__doc__ or ""
    assert "单 worker" in work_doc and "排队" in work_doc
    assert "60" in work_doc or "最坏" in work_doc
    assert "单 worker" in reply_doc


def test_status_exposes_queue_and_in_flight(tmp_path):
    gw = _gw(tmp_path)
    gw.in_flight_contact = "好友A"
    status = gw.status()
    assert status["in_flight_contact"] == "好友A"
    assert status["queue_size"] == 0
    assert "dropped_inbound" in status
    gw.in_flight_contact = ""
    assert gw.status()["in_flight_contact"] == ""


# --------------------------------------------------------------------------- #
# C) 日志路径不在 import 时写死（Lead 约束）：env 在调用时生效
# --------------------------------------------------------------------------- #
def test_default_log_path_resolves_at_call_time(tmp_path, monkeypatch):
    from agi.wechat_gateway import server as server_mod

    # 1) SOULAGENT_LOG_DIR 调用时生效：构造 Gateway 不传 log_path 也会落在隔离目录
    monkeypatch.setenv("SOULAGENT_LOG_DIR", str(tmp_path / "logs"))
    gw = _gw(tmp_path, log_path=None)
    assert gw.log_path == tmp_path / "logs" / "gateway.log"
    # 丢弃消息的落盘文件跟着日志目录走（不写死生产路径）
    assert gw.pending_lost_path.parent == gw.log_path.parent
    # 模块级 _log 的默认目标同样延迟解析
    server_mod._log({"kind": "unit_probe"})
    assert (tmp_path / "logs" / "gateway.log").exists()

    # 2) 没有 env 时回到数据目录下的历史路径（生产行为零变化）
    monkeypatch.delenv("SOULAGENT_LOG_DIR", raising=False)
    monkeypatch.delenv("SOULAGENT_DATA_DIR", raising=False)
    assert server_mod._default_log_path() == (
        server_mod.ROOT / "agi" / "data" / "wechat" / "gateway.log")