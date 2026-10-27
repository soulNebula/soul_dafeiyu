# -*- coding: utf-8 -*-
"""P30 每日流水线入口：收集 → 总结 → 门禁检查（默认不训练）。

供 Windows 计划任务 / 桌面 launcher 调用（03:00）；不要动 gateway。

用法::

    python tools/train/run_daily.py                 # 今天：收集+总结+门禁
    python tools/train/run_daily.py --date 2026-10-27
    python tools/train/run_daily.py --no-summary    # 只收集（离线）
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime

try:  # 允许 `python tools/train/run_daily.py` 与 `python -m tools.train.run_daily`
    from .daily_corpus import run as run_corpus
    from .daily_summary import run as run_summary
    from .train_gate import check_ready, train_once
except ImportError:  # pragma: no cover - 脚本直接执行
    from daily_corpus import run as run_corpus
    from daily_summary import run as run_summary
    from train_gate import check_ready, train_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每日语料流水线（默认不训练）")
    parser.add_argument("--date", default="")
    parser.add_argument("--no-summary", action="store_true")
    parser.add_argument("--train-dry-run", action="store_true",
                        help="额外打印训练计划（不执行）")
    args = parser.parse_args(argv)
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    corpus = run_corpus(date_str, json_output=False)
    summary = None if args.no_summary else run_summary(date_str)
    readiness = check_ready()
    plan = None
    if args.train_dry_run:
        plan = train_once(dry_run=True)
    result = {"date": date_str, "corpus": corpus, "summary": summary,
              "gate": readiness, "train_plan": plan}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
