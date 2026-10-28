# -*- coding: utf-8 -*-
"""14 ticker：按闸门执行一次/多轮 proactive，不新增发送通道。"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from . import autonomy, proactive


def run_once(path: Path | str, client=None, *, now: float | None = None) -> dict[str, Any]:
    gate = autonomy.gate(path, now)
    if not gate.get("allowed"):
        return {"ok": True, "ran": False, "reason": gate.get("reason"), "gate": gate}
    cfg = autonomy._config(path)
    result = proactive.tick(
        path, client=client, now=now,
        idle_seconds=float(cfg.get("idle_seconds", 30)),
        cooldown=float(cfg.get("cooldown_seconds", 900)),
        min_score=cfg.get("min_score"),
        respect_state=bool(cfg.get("respect_state", True)),
    )
    return {"ok": True, "ran": True, "gate": gate, "proactive": result}


def loop(path: Path | str, client=None, *, interval: float = 300,
         max_ticks: int | None = None, stop: threading.Event | None = None) -> dict[str, Any]:
    stop = stop or threading.Event()
    ticks = 0
    results = []
    while not stop.is_set():
        results.append(run_once(path, client))
        ticks += 1
        if max_ticks is not None and ticks >= int(max_ticks):
            break
        stop.wait(max(1.0, float(interval)))
    return {"ok": True, "ticks": ticks, "stopped": stop.is_set(), "results": results[-10:]}
