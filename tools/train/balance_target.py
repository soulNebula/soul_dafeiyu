"""对 v1 训练集做「少数类过采样」，解决 length/advice 坍缩。

背景（2026-10-25 实测）：v1 训练集里 length=normal 只占 4.3%、advice=true 只占 3.8%，
模型直接学成「永远输出多数类」。这两个字段不是学不会，是没信号。

做法：把命中规则的行复制 factor 倍（原行保留），得到平衡后的训练集。
验证集 **不动**（保证评测口径一致）。

用法:
  E:\\agent\\venv312\\Scripts\\python.exe tools/train/balance_target.py \
      --src agi/soul/data/soul_sft_v1_train.jsonl \
      --dst agi/soul/data/soul_sft_v1b_train.jsonl \
      --factor 5
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def hit_rare(row: dict) -> str | None:
    b = (row.get("target") or {}).get("behavior") or {}
    if str(b.get("length")) == "normal":
        return "length=normal"
    if b.get("advice") is True:
        return "advice=true"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--factor", type=int, default=5, help="稀有类复制倍数（含原行共 factor 份）")
    args = ap.parse_args()

    rows = []
    with Path(args.src).open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))

    out = []
    stats = {"length=normal": 0, "advice=true": 0}
    for r in rows:
        out.append(r)
        tag = hit_rare(r)
        if tag:
            stats[tag] += 1
            for k in range(max(0, args.factor - 1)):
                dup = dict(r)
                dup["id"] = f"{r.get('id')}-x{k + 2}"
                dup["oversampled"] = tag
                out.append(dup)

    with Path(args.dst).open("w", encoding="utf-8") as w:
        for r in out:
            w.write(json.dumps(r, ensure_ascii=False) + "\n")

    total = len(rows)
    print(f"[balance] {total} -> {len(out)}  (factor={args.factor})")
    print(f"[balance] 稀有行: length=normal {stats['length=normal']} -> "
          f"{stats['length=normal'] * args.factor}; advice=true {stats['advice=true']} -> "
          f"{stats['advice=true'] * args.factor}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
