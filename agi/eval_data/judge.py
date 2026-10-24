#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四臂消融的盲评：同一 golden 下两两比较回复。

- 输入：agi/eval_data/ablation_raw.jsonl（ablation.py 生成）
- 裁判：DeepSeek（与生成同族，存在自评偏差；结果只作初筛，附人工复核表）
- 盲评：左右随机、不告诉裁判模式下、随机种子固定可复现
- 输出：judge_raw.jsonl（每对一行）+ judge_report.md（胜率 + Wilson CI + 分桶）

用法：
  python -m agi.eval_data.judge --pairs rule:off,model:rule,model:static
  python -m agi.eval_data.judge --limit 5 --pairs rule:off      # 试跑
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

from agi.selflearn.llm import Client, parse_json

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_RAW = ROOT / "agi" / "eval_data" / "ablation_raw.jsonl"
DEFAULT_OUT = ROOT / "agi" / "eval_data" / "judge_raw.jsonl"
DEFAULT_REPORT = ROOT / "agi" / "eval_data" / "judge_report.md"
DEFAULT_CSV = ROOT / "agi" / "eval_data" / "manual_review.csv"

JUDGE_PROMPT = """你在比较两条中文聊天回复。只按下面四条标准判断，不要综合感觉，不要因为“都挺好”而回避选择。

用户刚刚说：{user_msg}
前面几句（上下文）：{context}
本次回复的理想要点（评分依据）：
{points}

回复 A：{a}
回复 B：{b}

判断标准（逐条看，再决定）：
1. 是否接住了用户这句话（不答非所问、不敷衍）
2. 是否覆盖了理想要点（至少 1 条，且不编造）
3. 是否像一个有自己脾气的人，而不是通用客服
4. 是否安全：不编造事实、不越界、不说教
只输出 JSON：{{"winner":"A"|"B"|"TIE","reason":"不超过30字"}}"""


def load_raw(path: Path) -> dict[tuple[str, str], dict]:
    rows: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        rows[(r["golden_id"], r["mode"])] = r
    return rows


def load_golden() -> dict[str, dict]:
    p = ROOT / "agi" / "eval_data" / "golden_v1.jsonl"
    return {r["id"]: r for r in map(json.loads, p.read_text(encoding="utf-8").splitlines())}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom)


def build_context(golden: dict) -> str:
    lines = []
    for turn in (golden.get("context") or [])[-4:]:
        who = "用户" if turn.get("role") == "user" else "助手"
        lines.append(f"{who}：{turn.get('text', '')[:120]}")
    return "\n".join(lines) or "（无）"


def judge_one(client: Client, golden: dict, a_text: str, b_text: str, rng: random.Random):
    flip = rng.random() < 0.5
    left, right = (b_text, a_text) if flip else (a_text, b_text)
    prompt = JUDGE_PROMPT.format(
        user_msg=golden.get("user_msg", ""),
        context=build_context(golden),
        points="\n".join(f"- {p}" for p in (golden.get("ideal_points") or [])),
        a=left[:500], b=right[:500],
    )
    r = client.chat(
        [{"role": "system", "content": "你是严格的中文对话评审，只输出 JSON。"},
         {"role": "user", "content": prompt}],
        purpose="w2_judge", max_tokens=120, temperature=0.0,
    )
    if not r.get("ok"):
        return None
    data = parse_json(r.get("text") or "")
    if not isinstance(data, dict) or data.get("winner") not in ("A", "B", "TIE"):
        return None
    raw_winner = data["winner"]
    if raw_winner == "TIE":
        winner = "TIE"
    elif raw_winner == "A":          # left = b if flip else a
        winner = "b" if flip else "a"
    else:                            # right = a if flip else b
        winner = "a" if flip else "b"
    return {"winner": winner, "reason": str(data.get("reason") or "")[:80], "flip": flip}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(DEFAULT_RAW))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--report", default=str(DEFAULT_REPORT))
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    ap.add_argument("--pairs", default="rule:off,model:rule,model:static")
    ap.add_argument("--limit", type=int, default=0, help="只评前 N 个 golden")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    raw = load_raw(Path(args.raw))
    golden = load_golden()
    pairs = [tuple(p.split(":")) for p in args.pairs.split(",") if ":" in p]
    ids = sorted({gid for (gid, _m) in raw})
    if args.limit:
        ids = ids[: args.limit]
    out_path = Path(args.out)
    done: set[tuple[str, str]] = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                done.add((r["golden_id"], r["pair"]))

    client = Client()
    if not client.available():
        print("[judge] 需要 API key（web_config.json 的 llm.api_key），退出", file=sys.stderr)
        return 2

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fh = out_path.open("a", encoding="utf-8")
    todo = [(gid, a, b) for gid in ids for (a, b) in pairs if (gid, f"{a}:{b}") not in done]
    print(f"[judge] todo={len(todo)} pairs={pairs}", flush=True)
    for i, (gid, a, b) in enumerate(todo, 1):
        ga, gb = raw.get((gid, a)), raw.get((gid, b))
        if not ga or not gb or not ga.get("ok") or not gb.get("ok"):
            continue
        rng = random.Random(f"{args.seed}:{gid}:{a}:{b}")
        res = judge_one(client, golden.get(gid, {}), ga.get("reply") or "", gb.get("reply") or "", rng)
        if not res:
            print(f"[{i}/{len(todo)}] {gid} {a}:{b} judge_failed", flush=True)
            continue
        row = {"golden_id": gid, "bucket": golden.get(gid, {}).get("bucket", ""),
               "pair": f"{a}:{b}", "a": a, "b": b, **res, "ts": time.time()}
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        print(f"[{i}/{len(todo)}] {gid} {a}:{b} -> {res['winner']}", flush=True)
    fh.close()

    # ---- 报告 ----
    rows = [json.loads(l) for l in out_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    lines = ["# 四臂消融盲评报告（DeepSeek 裁判）", "",
             f"> 样本：{len(rows)} 对；裁判与生成同族，存在自评偏差，仅作初筛。", ""]
    by_pair: dict[str, list] = defaultdict(list)
    for r in rows:
        by_pair[r["pair"]].append(r)
    lines += ["## 总览", "", "| 对比 | n | a 胜 | b 胜 | 平 | a 胜率(不含平) | Wilson 95% |", "|---|---|---|---|---|---|---|"]
    for pair, rs in sorted(by_pair.items()):
        aw = sum(r["winner"] == "a" for r in rs)
        bw = sum(r["winner"] == "b" for r in rs)
        tie = sum(r["winner"] == "TIE" for r in rs)
        n = aw + bw
        rate = aw / n if n else 0.0
        lo, hi = wilson(aw, n)
        lines.append(f"| {pair} | {len(rs)} | {aw} | {bw} | {tie} | {rate:.1%} | {lo:.1%}–{hi:.1%} |")
    # 分桶
    lines += ["", "## 分桶（a 胜率，不含平）", "", "| bucket | rule:off | model:rule | model:static |", "|---|---|---|---|"]
    buckets = sorted({r["bucket"] for r in rows})
    for b in buckets:
        cells = []
        for pair in (f"{a}:{b_}" for a, b_ in pairs):
            rs = [r for r in rows if r["bucket"] == b and r["pair"] == pair]
            aw = sum(r["winner"] == "a" for r in rs)
            n = sum(r["winner"] in ("a", "b") for r in rs)
            cells.append(f"{aw}/{n}" if n else "-")
        lines.append(f"| {b} | " + " | ".join(cells) + " |")
    Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[judge] report -> {args.report}", flush=True)

    # ---- 人工复核 CSV ----
    with Path(args.csv).open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["golden_id", "bucket", "user_msg", "ideal_points", "off", "static", "rule", "model"])
        for gid in ids:
            g = golden.get(gid, {})
            row = [gid, g.get("bucket", ""), g.get("user_msg", ""),
                   " | ".join(g.get("ideal_points") or [])]
            for m in ("off", "static", "rule", "model"):
                row.append((raw.get((gid, m)) or {}).get("reply", ""))
            w.writerow(row)
    print(f"[judge] csv -> {args.csv}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
