# -*- coding: utf-8 -*-
"""A9-P1：发送任务池（优先级 + 每联系人 FIFO + TTL + 背压）。

设计边界（与 A9 方案一致）：

- 微信窗口/UIA 是全局资源 → **只有一个执行线程**（``gateway-sender``）；
- **同一联系人严格 FIFO**：同一会话的气泡顺序不能乱（跨联系人允许插队）；
- 跨联系人按优先级调度：``manual/approve(0) > reactive(1) > proactive(2)``；
- 任务带 **TTL**：排队超时直接返回 ``send_expired_ttl``（proactive 等同放弃），不再发送；
- **背压**：全局 pending 上限 + 每联系人 pending 上限，拒绝时同步返回错误码；
- 超时/取消的语义与 P0 一致：调用方拿到 ``unverified`` 时**不得自动重发**。

本模块不 import 网关其它模块（纯调度），便于单测与复用。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

PRIORITY_MANUAL = 0
PRIORITY_REACTIVE = 1
PRIORITY_PROACTIVE = 2
PRIORITY_NAMES = {PRIORITY_MANUAL: "manual", PRIORITY_REACTIVE: "reactive",
                  PRIORITY_PROACTIVE: "proactive"}


@dataclass
class SendJob:
    """一次待发送任务（text/kwargs 由 runner 解释，池只负责调度）。"""

    contact: str
    text: str
    kwargs: dict
    priority: int = PRIORITY_REACTIVE
    ttl_s: float = 180.0
    created_at: float = field(default_factory=time.time)
    seq: int = 0
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None

    def expired(self, now: float | None = None) -> bool:
        if self.ttl_s <= 0:
            return False
        now = time.time() if now is None else float(now)
        return (now - self.created_at) > self.ttl_s


class SenderPool:
    """单执行线程 + per-contact FIFO + 跨联系人优先级 + TTL/背压。"""

    def __init__(self, runner: Callable[[SendJob], dict], *,
                 max_pending: int = 200, max_per_contact: int = 20,
                 name: str = "gateway-sender") -> None:
        self._runner = runner
        self.max_pending = max(1, int(max_pending))
        self.max_per_contact = max(1, int(max_per_contact))
        self.name = name
        self._cond = threading.Condition()
        self._queues: dict[str, deque[SendJob]] = {}
        self._pending = 0
        self._seq = 0
        self._stop = False
        self._thread: threading.Thread | None = None
        self.stats: dict[str, Any] = {
            "submitted": 0, "completed": 0, "failed": 0, "expired": 0,
            "rejected_full": 0, "rejected_contact_full": 0, "cancelled": 0,
            "by_priority": {name: 0 for name in PRIORITY_NAMES.values()},
            "last_wait_ms": 0.0, "last_error": "",
        }

    # ------------------------------------------------------------- 生命周期
    def start(self) -> "SenderPool":
        with self._cond:
            if self._thread and self._thread.is_alive():
                return self
            self._stop = False
            self._thread = threading.Thread(target=self._loop, name=self.name,
                                            daemon=True)
            self._thread.start()
        return self

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def stop(self, timeout: float = 3.0) -> None:
        with self._cond:
            self._stop = True
            self._drain_locked()
            self._cond.notify_all()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))

    # ------------------------------------------------------------- 提交
    def submit(self, job: SendJob) -> str:
        """入池；返回 ``"ok"`` / ``"queue_full"`` / ``"contact_queue_full"`` / ``"stopped"``。"""
        with self._cond:
            if self._stop:
                return "stopped"
            if self._pending >= self.max_pending:
                self.stats["rejected_full"] = int(self.stats["rejected_full"]) + 1
                return "queue_full"
            queue = self._queues.get(job.contact)
            if queue is None:
                queue = deque()
                self._queues[job.contact] = queue
            if len(queue) >= self.max_per_contact:
                self.stats["rejected_contact_full"] = int(
                    self.stats["rejected_contact_full"]) + 1
                return "contact_queue_full"
            self._seq += 1
            job.seq = self._seq
            queue.append(job)
            self._pending += 1
            self.stats["submitted"] = int(self.stats["submitted"]) + 1
            key = PRIORITY_NAMES.get(job.priority, "reactive")
            by_priority = self.stats["by_priority"]
            by_priority[key] = int(by_priority.get(key, 0)) + 1
            self._cond.notify()
            return "ok"

    def pending(self) -> int:
        with self._cond:
            return self._pending

    def snapshot(self) -> dict:
        with self._cond:
            queues = {contact: len(queue) for contact, queue in self._queues.items()
                      if queue}
            stats = dict(self.stats)
            stats["by_priority"] = dict(self.stats["by_priority"])
            stats["pending"] = self._pending
            stats["contacts_queued"] = len(queues)
            stats["queues"] = dict(list(queues.items())[:20])
            stats["alive"] = self.alive()
            return stats

    # ------------------------------------------------------------- 调度
    def _pick_locked(self) -> SendJob | None:
        """选：优先级最小 → 同优先级 seq 最小；每联系人只出队头（保序）。"""
        best: tuple[int, int] | None = None
        best_contact = ""
        for contact, queue in self._queues.items():
            if not queue:
                continue
            head = queue[0]
            key = (int(head.priority), int(head.seq))
            if best is None or key < best:
                best = key
                best_contact = contact
        if best is None:
            return None
        queue = self._queues[best_contact]
        job = queue.popleft()
        if not queue:
            self._queues.pop(best_contact, None)
        self._pending -= 1
        return job

    def _any_locked(self) -> bool:
        return any(self._queues.values())

    def _drain_locked(self) -> None:
        for contact, queue in list(self._queues.items()):
            while queue:
                job = queue.popleft()
                self._finish_locked(job, {"ok": False, "status": "cancelled",
                                          "error": "send_async_stopped"},
                                    cancelled=True)
        self._queues.clear()
        self._pending = 0

    def _finish(self, job: SendJob, result: dict) -> None:
        with self._cond:
            self._finish_locked(job, result)

    def _finish_locked(self, job: SendJob, result: dict, *,
                       cancelled: bool = False) -> None:
        if job.done.is_set():
            return
        job.result = result
        job.done.set()
        if cancelled:
            self.stats["cancelled"] = int(self.stats["cancelled"]) + 1
        else:
            self.stats["completed"] = int(self.stats["completed"]) + 1
            if not result.get("ok"):
                self.stats["failed"] = int(self.stats["failed"]) + 1
                self.stats["last_error"] = str(result.get("error") or "")[:200]
        self._cond.notify_all()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while True:
                    if self._stop:
                        self._drain_locked()
                        return
                    job = self._pick_locked()
                    if job is not None:
                        break
                    self._cond.wait(0.2)
            if job.expired():
                self.stats["expired"] = int(self.stats["expired"]) + 1
                self._finish(job, {"ok": False, "status": "blocked",
                                   "error": "send_expired_ttl",
                                   "expired": True})
                continue
            started = time.time()
            try:
                result = self._runner(job)
                if not isinstance(result, dict):
                    result = {"ok": False, "status": "failed",
                              "error": "bad_runner_result"}
            except Exception as exc:  # noqa: BLE001 - 执行线程不能死
                result = {"ok": False, "status": "failed",
                          "error": f"{type(exc).__name__}: {exc}"[:200]}
            self.stats["last_wait_ms"] = round((time.time() - started) * 1000, 1)
            self._finish(job, result)
