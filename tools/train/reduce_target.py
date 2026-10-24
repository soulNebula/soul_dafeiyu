"""把 v1 数据集压成「只训行为判断」的版本。

为什么：线上 `agi/inner/inject.py` 会把 mood/energy/sharing/battery/trust/stage/crisis
全部覆盖成真实状态（模型输出被丢弃）。所以让模型回归这些数值是浪费容量和 token：
- 输出从 ~128 token 压到 ~65 token（快一倍）
- 容量集中到真正会用的 stance / intent / monologue / behavior

用法:
  E:\\agent\\venv312\\Scripts\\python.exe tools/train/reduce_target.py \
      --src agi/soul/data/soul_sft_v1_train.jsonl --dst agi/soul/data/soul_sft_v1r_train.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

KEEP = ("stance", "intent", "monologue", "behavior")


def reduce_row(row: dict) -> dict:
    target = row.get("target") or {}
    slim = {k: target[k] for k in KEEP if k in target}
    out = dict(row)
    out["target"] = slim
    out["reduced"] = True
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    args = ap.parse_args()
    src, dst = Path(args.src), Path(args.dst)
    n_in = n_out = 0
    with src.open(encoding="utf-8") as f, dst.open("w", encoding="utf-8") as w:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            n_in += 1
            slim = reduce_row(row)
            missing = [k for k in KEEP if k not in slim["target"]]
            if missing:
                continue
            w.write(json.dumps(slim, ensure_ascii=False) + "\n")
            n_out += 1
    print(f"[reduce] {src.name}: {n_in} -> {dst.name}: {n_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
