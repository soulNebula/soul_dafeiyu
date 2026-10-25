# -*- coding: utf-8 -*-
"""P1-g：记忆指标 —— CLI / 每日快照 / 异常告警。

指标本体在 :func:`agi.selflearn.memory.metrics`；这里负责：
- `python -m agi.selflearn.metrics --path <db> [--json] [--snapshot]`
- 每日快照 ``agi/data/selflearn/metrics/<YYYY-MM-DD>.json``（幂等覆盖当天）
- 连续 7 天快照的三条异常阈值检测（dormant 率 / active 一周净减 / 检索命中率）
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from . import memory
from .config import DB_PATH

#: 三条异常阈值
DORMANT_RATE_WARN = 0.40          # 休眠记忆占全部 > 40%
ACTIVE_WEEK_DROP_WARN = 0.30      # active messages 一周净减 > 30%
RETRIEVAL_HIT_RATE_WARN = 0.05    # 检索命中率 < 5%（有调用时才判）


def snapshot_dir(db_path: Path | str | None = None) -> Path:
    """快照目录：与 selflearn 库同级的 ``metrics/``。"""
    base = Path(db_path or DB_PATH)
    return base.parent / "metrics"


def write_snapshot(db_path: Path | str | None = None,
                   now: float | None = None) -> dict:
    """写当天快照（原子覆盖；同一天重跑幂等）。"""
    now = time.time() if now is None else float(now)
    payload = memory.metrics(db_path or DB_PATH, now)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    target = snapshot_dir(db_path) / f"{day}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    os.replace(tmp, target)
    return {"ok": True, "path": str(target), "snapshot": payload}


def load_history(db_path: Path | str | None = None, days: int = 7,
                 now: float | None = None) -> list[dict]:
    """最近 days 天的快照（新→旧）；读不出来的跳过。"""
    now = time.time() if now is None else float(now)
    out: list[dict] = []
    for offset in range(max(0, int(days))):
        day = time.strftime("%Y-%m-%d", time.localtime(now - offset * 86400))
        target = snapshot_dir(db_path) / f"{day}.json"
        if not target.exists():
            continue
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def anomalies(snapshot: dict, history: list[dict] | None = None) -> list[str]:
    """三条异常阈值检测；返回中文 warning 列表（空 = 健康）。"""
    snapshot = snapshot or {}
    history = history or []
    warnings: list[str] = []

    mem = snapshot.get("memories") or {}
    try:
        dormant_rate = float(mem.get("dormant_rate") or 0.0)
    except (TypeError, ValueError):
        dormant_rate = 0.0
    if dormant_rate > DORMANT_RATE_WARN:
        warnings.append(
            f"dormant_rate={dormant_rate:.0%} > {DORMANT_RATE_WARN:.0%}："
            "休眠记忆过多，检查 nightly decay / importance 初始化（P1-a）")

    try:
        today_active = int((snapshot.get("messages") or {}).get("active"))
    except (TypeError, ValueError):
        today_active = None
    now = snapshot.get("now")
    if today_active is not None and today_active >= 0 and isinstance(now, (int, float)):
        for old in reversed(history):            # 从最早的一份开始比
            old_active = (old.get("messages") or {}).get("active")
            old_now = old.get("now")
            if (not isinstance(old_active, int) or old_active <= 0
                    or not isinstance(old_now, (int, float))):
                continue
            if float(now) - float(old_now) > 7 * 86400:
                continue
            drop = (old_active - today_active) / float(old_active)
            if drop > ACTIVE_WEEK_DROP_WARN:
                warnings.append(
                    f"active messages 一周净减 {drop:.0%} > {ACTIVE_WEEK_DROP_WARN:.0%}"
                    f"（{old_active}→{today_active}）：检查冷归档阈值/是否误归档（P1-d）")
            break

    retrieval = snapshot.get("retrieval") or {}
    try:
        calls = int(retrieval.get("calls") or 0)
        hit_rate = retrieval.get("hit_rate")
        if calls and isinstance(hit_rate, (int, float)) \
                and float(hit_rate) < RETRIEVAL_HIT_RATE_WARN:
            warnings.append(
                f"retrieval hit_rate={float(hit_rate):.1%} < {RETRIEVAL_HIT_RATE_WARN:.0%}"
                f"（calls={calls}）：检查 retrieve_tiered 分层预算/时间层级（P1-b）")
    except (TypeError, ValueError):
        pass
    return warnings


def snapshot_and_check(db_path: Path | str | None = None,
                       now: float | None = None) -> dict:
    """nightly 用：先落盘内存检索计数 → 写当天快照 → 检测最近 7 天异常。"""
    now = time.time() if now is None else float(now)
    db = db_path or DB_PATH
    memory.flush_retrieval_counters(db)
    written = write_snapshot(db, now)
    history = load_history(db, days=7, now=now)
    warns = anomalies(written["snapshot"], history)
    return {"ok": True, "path": written["path"], "warnings": warns,
            "snapshot": written["snapshot"]}


def _pretty(data: dict) -> str:
    def _fmt(value) -> str:
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, default=str)
        return str(value)

    lines = ["== 记忆指标 =="]
    for section, value in data.items():
        if section in ("ok", "now"):
            continue
        lines.append(f"{section}: {_fmt(value)}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agi.selflearn.metrics")
    parser.add_argument("--path", default=str(DB_PATH), help="selflearn 库路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--snapshot", action="store_true",
                        help="写每日快照并检测异常阈值")
    args = parser.parse_args(argv)
    if args.snapshot:
        result = snapshot_and_check(args.path)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"snapshot: {result['path']}")
            print(f"warnings: {result['warnings'] or '[]'}")
        return 0
    data = memory.metrics(args.path)
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str)
          if args.json else _pretty(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
