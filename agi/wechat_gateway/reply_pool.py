# -*- coding: utf-8 -*-
"""A9-P2：回复生成线程池（每联系人串行、跨联系人并行）。

- 单进程里用 N 个 worker 线程（默认 2）；联系人按稳定 hash 固定到某个 worker，
  保证**同一联系人的回复生成严格串行**（不会两轮同时调 LLM/写同一个库）；
- 跨联系人并行：A 的 LLM 慢不再阻塞 B；
- 队列有界：满时返回 ``queue_full``，由网关决定回退（本阶段回退为 inline 处理）；
- 不做发送：worker 只跑 ``runner(job)``（网关的 ``_reply``），发送由发送任务池负责。

本模块不 import 网关其它模块，便于单测。
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ReplyJob:
    contact: str
    msg: dict
    texts: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)
    on_done: Callable | None = None


class ReplyPool:
    def __init__(self, runner: Callable[[ReplyJob], Any], *,
                 workers: int = 2, queue_size: int = 200,
                 name: str = "gateway-reply") -> None:
        self._runner = runner
        self.workers = max(1, int(workers))
        self.queue_size = max(1, int(queue_size))
        self.name = name
        self._queues: list[queue.Queue] = [
            queue.Queue(maxsize=max(1, self.queue_size // self.workers))
            for _ in range(self.workers)
        ]
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._inflight: dict[str, int] = {}
        self._assignment: dict[str, int] = {}
        self._worker_inflight: list[int] = [0] * self.workers
        self._lock = threading.Lock()
        self.stats: dict[str, Any] = {
            "submitted": 0, "completed": 0, "failed": 0, "rejected": 0,
            "last_error": "", "last_ms": 0.0,
        }

    # ------------------------------------------------------------- 生命周期
    def start(self) -> "ReplyPool":
        if self._threads and any(t.is_alive() for t in self._threads):
            return self
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._loop, args=(index,),
                             name=f"{self.name}-{index}", daemon=True)
            for index in range(self.workers)
        ]
        for thread in self._threads:
            thread.start()
        return self

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        for q in self._queues:
            try:
                q.put_nowait(None)  # 唤醒哨兵
            except queue.Full:
                pass
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=max(0.0, float(timeout)))

    def alive(self) -> bool:
        return bool(self._threads and any(t.is_alive() for t in self._threads))

    # ------------------------------------------------------------- 提交
    def _pick_worker(self, contact: str) -> tuple[int, bool]:
        """联系人粘性 + 最小负载：同一联系人固定 worker，新联系人挑最空的。

        只用 hash 取模在"两三个联系人"时经常撞到同一个 worker，导致跨联系人
        根本不并行（2026-10-31 灰度实测：好友A/好友B 恰好同桶）。改为提交时按
        "队列长度 + 该 worker 在跑任务数"动态分配，并在联系人空闲后解除粘性。
        """
        assigned = self._assignment.get(contact)
        if assigned is not None:
            return assigned, False
        loads = [self._queues[i].qsize() + self._worker_inflight[i]
                 for i in range(self.workers)]
        index = min(range(self.workers), key=lambda i: (loads[i], i))
        self._assignment[contact] = index
        return index, True

    def submit(self, job: ReplyJob) -> str:
        """返回 ``"ok"`` / ``"queue_full"`` / ``"stopped"``。"""
        if self._stop.is_set():
            return "stopped"
        with self._lock:
            index, created = self._pick_worker(str(job.contact or ""))
            self._worker_inflight[index] += 1
        try:
            self._queues[index].put_nowait(job)
        except queue.Full:
            with self._lock:
                self._worker_inflight[index] = max(0, self._worker_inflight[index] - 1)
                if created and not self._inflight.get(job.contact):
                    self._assignment.pop(job.contact, None)
                self.stats["rejected"] = int(self.stats["rejected"]) + 1
            return "queue_full"
        with self._lock:
            self._inflight[job.contact] = int(self._inflight.get(job.contact, 0)) + 1
            self.stats["submitted"] = int(self.stats["submitted"]) + 1
        return "ok"

    def inflight(self, contact: str | None = None) -> int:
        with self._lock:
            if contact is None:
                return sum(self._inflight.values())
            return int(self._inflight.get(str(contact), 0))

    def snapshot(self) -> dict:
        with self._lock:
            inflight = dict(self._inflight)
            stats = dict(self.stats)
        return {
            "enabled": True,
            "alive": self.alive(),
            "workers": self.workers,
            "queue_size": self.queue_size,
            "queued": sum(q.qsize() for q in self._queues),
            "inflight": sum(inflight.values()),
            "contacts_inflight": len(inflight),
            "completed": stats["completed"],
            "failed": stats["failed"],
            "rejected": stats["rejected"],
            "last_error": stats["last_error"],
            "last_ms": stats["last_ms"],
        }

    # ------------------------------------------------------------- 执行
    def _loop(self, index: int) -> None:
        q = self._queues[index]
        while not self._stop.is_set():
            try:
                job = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                continue
            started = time.time()
            error = ""
            try:
                self._runner(job)
            except Exception as exc:  # noqa: BLE001 - worker 线程不能死
                error = f"{type(exc).__name__}: {exc}"[:200]
                self.stats["failed"] = int(self.stats["failed"]) + 1
                self.stats["last_error"] = error
            else:
                self.stats["completed"] = int(self.stats["completed"]) + 1
            finally:
                self.stats["last_ms"] = round((time.time() - started) * 1000, 1)
                with self._lock:
                    left = int(self._inflight.get(job.contact, 0)) - 1
                    if left > 0:
                        self._inflight[job.contact] = left
                    else:
                        self._inflight.pop(job.contact, None)
                    self._worker_inflight[index] = max(
                        0, self._worker_inflight[index] - 1)
                    if left <= 0 and self._queues[index].empty():
                        self._assignment.pop(job.contact, None)
                done = getattr(job, "on_done", None)
                if callable(done):
                    try:
                        done(job, error)
                    except Exception:  # noqa: BLE001 - 回调失败不影响 worker
                        pass
