#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A9-P3 压测（纯离线、不连微信、不发送）：发送池吞吐 + 生成池并行加速比。

用法：
    C:/Python314/python.exe scripts/bench_send_pipeline.py

输出 JSON：send_pool（N 条任务、每联系人顺序校验、吞吐）、reply_pool（并行 vs 串行）、
rollback（开关关闭时是否仍走旧路径由单测覆盖，这里只测池本身）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agi.wechat_gateway.reply_pool import ReplyJob, ReplyPool  # noqa: E402
from agi.wechat_gateway.send_async import (  # noqa: E402
    PRIORITY_MANUAL, PRIORITY_PROACTIVE, PRIORITY_REACTIVE, SendJob, SenderPool)


def bench_send(total: int = 300, contacts: int = 6, exec_s: float = 0.002) -> dict:
    order: dict[str, list[str]] = {}

    def runner(job: SendJob):
        order.setdefault(job.contact, []).append(job.text)
        time.sleep(exec_s)
        return {"ok": True, "status": "sent"}

    pool = SenderPool(runner, max_pending=total + 10).start()
    t0 = time.time()
    submitted = 0
    for index in range(total):
        contact = f"c{index % contacts}"
        priority = (PRIORITY_MANUAL if index % 10 == 0
                    else PRIORITY_PROACTIVE if index % 3 == 0
                    else PRIORITY_REACTIVE)
        job = SendJob(contact=contact, text=f"{contact}-{index:04d}", kwargs={},
                      priority=priority, ttl_s=60)
        if pool.submit(job) != "ok":
            continue
        submitted += 1
        job.done.wait(timeout=10)
    elapsed = time.time() - t0
    pool.stop()
    fifo_ok = all(seq == sorted(seq) for seq in order.values())
    return {"submitted": submitted, "elapsed_s": round(elapsed, 3),
            "throughput_per_s": round(submitted / elapsed, 1) if elapsed else None,
            "per_contact_fifo_ok": fifo_ok,
            "contacts": len(order)}


def bench_reply(workers: int = 2, contacts: int = 4, exec_s: float = 0.05) -> dict:
    pool = ReplyPool(lambda job: time.sleep(exec_s), workers=workers).start()
    t0 = time.time()
    for index in range(contacts):
        pool.submit(ReplyJob(contact=f"r{index}", msg={}, texts=[str(index)]))
    while pool.inflight() > 0 and time.time() - t0 < 10:
        time.sleep(0.005)
    elapsed = time.time() - t0
    pool.stop()
    theoretical_serial = contacts * exec_s
    return {"workers": workers, "contacts": contacts,
            "elapsed_s": round(elapsed, 3),
            "theoretical_serial_s": round(theoretical_serial, 3),
            "speedup": round(theoretical_serial / elapsed, 2) if elapsed else None}


def main() -> int:
    result = {
        "mode": "offline_benchmark",
        "send_pool": bench_send(),
        "reply_pool": bench_reply(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
