# -*- coding: utf-8 -*-
"""P0-2：UIA worker 性能对比（只读 title 查询，不点击、不发送）。

用法::

    python tools/wechat_uia/bench.py            # 各 5 次
    python tools/wechat_uia/bench.py --runs 10

对比「一次性 powershell -File query.ps1 -Title」与「常驻 uia_worker.ps1」。
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agi.wechat_gateway import uia_sender  # noqa: E402


def _one_shot() -> float:
    t0 = time.perf_counter()
    try:
        uia_sender._query("-Title", timeout=15)
    except Exception:  # noqa: BLE001
        pass
    return (time.perf_counter() - t0) * 1000.0


def _worker(client: "uia_sender.UIAClient") -> float:
    t0 = time.perf_counter()
    try:
        client.chat_title()
    except Exception:  # noqa: BLE001
        pass
    return (time.perf_counter() - t0) * 1000.0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="UIA one-shot vs resident worker")
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args(argv)
    runs = max(1, int(args.runs))

    print(f"runs={runs} (只读 title 查询，不发送)")
    one = [_one_shot() for _ in range(runs)]
    client = uia_sender.UIAClient(timeout=15.0)
    try:
        worker = [_worker(client) for _ in range(runs)]
    finally:
        client.close()

    def _summary(name: str, values: list[float]) -> None:
        print(f"{name:12s} mean={statistics.mean(values):8.1f}ms "
              f"median={statistics.median(values):8.1f}ms "
              f"min={min(values):8.1f}ms max={max(values):8.1f}ms")

    _summary("one-shot", one)
    _summary("worker", worker)
    if one and worker:
        print(f"speedup(median): {statistics.median(one) / statistics.median(worker):.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
