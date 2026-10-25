# -*- coding: utf-8 -*-
"""P2-b 线 B：语气改写（style transfer）平行数据。

思路：**内容由 DeepSeek 决定，语气由小模型套**。
- target = 用户本人真实 self 发言（P2-a/P2-b 的 sft.jsonl 里的 target）；
- input = DeepSeek 把「同一件事」改写成**中性书面表述**（保留原意/说话类型/占位符，不加事实）；
- 上下文：target 前面 2–4 条真实对话（原样保留）。

默认是 **preview 模式**：`--limit 100`，产出 `style_sft_preview.jsonl` + `style_preview.md` 供人工确认质量；
确认后加 `--full` 全量生成 `style_sft.jsonl`，再用 `--train` 训练（0.5B 起步）。

用法::

    # 预览 100 条（DeepSeek API，约 1–2 分钟）
    E:\\agent\\venv312\\Scripts\\python.exe agi/eval_data/tone_style_data.py --limit 100
    # 全量（确认后）
    E:\\agent\\venv312\\Scripts\\python.exe agi/eval_data/tone_style_data.py --full
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import build_human_tone as ht  # noqa: E402
if str(ROOT / "agi" / "eval_data") not in sys.path:
    sys.path.insert(0, str(ROOT / "agi" / "eval_data"))
from style_prompt import build_ai_style_messages, prompt_fingerprint  # noqa: E402

DATA_PATH = ROOT / "agi" / "eval_data" / "human_tone" / "sft.jsonl"
OUT_DIR = ROOT / "agi" / "eval_data" / "human_tone"
PREVIEW_JSONL = OUT_DIR / "style_preview_ai.jsonl"
PREVIEW_MD = OUT_DIR / "style_preview_ai.md"
FULL_JSONL = OUT_DIR / "style_sft_ai.jsonl"
PROGRESS = OUT_DIR / "style_progress_ai.jsonl"

def build_prompt(context: list[dict], target: str) -> str:
    lines = []
    for message in context[-4:]:
        who = "对方" if message["role"] == "user" else "我"
        lines.append(f"{who}：{message['content']}")
    return (
        "对话上下文：\n" + ("\n".join(lines) if lines else "（无）") + "\n\n"
        f"要改写的是「我」刚说的这句：{target}\n"
        "请输出 JSON。"
    )


def parse_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}
    return {}


def rewrite_one(client, row: dict) -> dict:
    """让 DeepSeek 用「线上 AI 人格口吻」把同一件事说出来（作为改写器的 input）。"""
    target = row["messages"][-1]["content"]
    context = [{"role": m["role"], "content": m["content"]}
               for m in row["messages"][:-1]]
    messages = build_ai_style_messages(context, target)
    last_error = ""
    for attempt in range(3):
        result = client.chat(messages, purpose="style_ai_input",
                             max_tokens=200, temperature=0.45, json_mode=True)
        if not result.get("ok"):
            last_error = str(result.get("error") or "api_error")
            time.sleep(0.5)
            continue
        data = parse_json(str(result.get("text") or ""))
        ai_input = str(data.get("ai_input") or data.get("content") or "").strip()
        if not ai_input:
            last_error = "empty_ai_input"
            continue
        # 长度约束：>40 字或像解释/清单 → 重试
        if len(ai_input) > 40:
            last_error = "too_long"
            continue
        problems = ht.privacy_violations(ai_input)
        return {"ai_input": ai_input, "privacy_problems": problems,
                "error": "", "attempts": attempt + 1}
    return {"ai_input": "", "privacy_problems": [], "error": last_error, "attempts": 3}


def load_base_rows(path: Path = DATA_PATH) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


_JUNK_CHARS = set("囗冫丶彳丨「」�")
_TIME_RE = re.compile(r"\d{1,2}[:：]\d{2}")
_SYS_RE = re.compile(r"\[system\]|\[quote\]|撤回了一条消息|拍了拍")


def looks_junk(text: str) -> bool:
    """过滤 OCR 残片/时间戳/系统消息，避免污染中性改写上下文。"""
    value = str(text or "")
    if not value:
        return True
    if set(value) & _JUNK_CHARS:
        return True
    if _TIME_RE.search(value) or _SYS_RE.search(value):
        return True
    if value.startswith("[") and "] " not in value:
        return True
    return False


def prepare_row(row: dict) -> dict | None:
    """target 必须干净；上下文只保留干净消息，至少留 2 条。"""
    messages = row.get("messages") or []
    if len(messages) < 2:
        return None
    target = str(messages[-1].get("content") or "").strip()
    if not target or looks_junk(target):
        return None
    context = [m for m in messages[:-1]
               if not looks_junk(str(m.get("content") or ""))]
    if len(context) < 2:
        return None
    prepared = dict(row)
    prepared["messages"] = context + [messages[-1]]
    return prepared


def eligible(row: dict) -> bool:
    messages = row.get("messages") or []
    if len(messages) < 2:
        return False
    target = str(messages[-1].get("content") or "").strip()
    if not target or target.startswith("["):
        return False
    if len(target) > 60:
        return False
    if any(ht.privacy_violations(str(m.get("content") or "")) for m in messages):
        return False
    return True


def pick_candidates(rows: list[dict], limit: int, seed: int) -> list[dict]:
    """分层抽样：渠道、句尾、长度，保证 preview 覆盖多样。"""
    rng = random.Random(seed)
    buckets: dict[tuple, list[dict]] = {}
    for row in rows:
        row = prepare_row(row)
        if row is None or not eligible(row):
            continue
        target = row["messages"][-1]["content"]
        ending = ht.ending_category(target)
        length_bucket = "<=8" if len(target) <= 8 else ("9-15" if len(target) <= 15 else ">15")
        key = (row.get("channel", "wechat"), ending, length_bucket)
        buckets.setdefault(key, []).append(row)
    for items in buckets.values():
        rng.shuffle(items)
    chosen: list[dict] = []
    keys = sorted(buckets)
    while len(chosen) < limit and keys:
        for key in list(keys):
            if buckets[key]:
                chosen.append(buckets[key].pop())
                if len(chosen) >= limit:
                    break
            else:
                keys.remove(key)
    return chosen


def write_preview(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def render_md(records: list[dict], stats: dict, path: Path) -> None:
    lines = ["# 语气改写预览（AI 人格口吻 → 你的原话；target = 真实 self 发言）", ""]
    lines.append(f"- 生成 {stats['generated']} / 失败 {stats['failed']}；"
                 f"隐私问题 {stats['privacy_issues']}；"
                 f"过长重试 {stats.get('too_long_retries', 0)}")
    lines.append(f"- 渠道分布：{stats['by_channel']}；句尾分布：{stats['by_ending']}")
    lines.append("")
    lines.append("| # | 上下文（节选） | AI 本来会这么说（input） | 你的原话（target） |")
    lines.append("| --- | --- | --- | --- |")
    for index, record in enumerate(records, 1):
        context = " / ".join(f"{'对方' if m['role'] == 'user' else '我'}：{m['content']}"
                             for m in record["context_last"])
        original = record["original"].replace("|", "\\|")
        ai_input = record["ai_input"].replace("|", "\\|")
        lines.append(f"| {index} | {context} | {ai_input} | {original} |")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语气改写平行数据（DeepSeek 中性化）")
    parser.add_argument("--data", default=str(DATA_PATH))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--full", action="store_true", help="全量生成 style_sft_ai.jsonl")
    parser.add_argument("--dry-run", action="store_true", help="只预览（默认行为，兼容用）")
    parser.add_argument("--workers", type=int, default=4, help="DeepSeek 并发数（默认 4）")
    args = parser.parse_args(argv)

    from agi.selflearn import llm
    client = llm.Client()
    if not client.available():
        raise SystemExit("[style] 没有 DeepSeek key，无法生成中性改写")

    rows = load_base_rows(Path(args.data))
    candidates = pick_candidates(rows, 10**9 if args.full else args.limit, args.seed)
    target_path = FULL_JSONL if args.full else PREVIEW_JSONL
    existing = {}
    if target_path.exists():
        for line in target_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                existing[record["id"]] = record
    if PROGRESS.exists():
        for line in PROGRESS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                existing.setdefault(record["id"], record)

    def make_record(row: dict, result: dict) -> dict:
        context = [{"role": m["role"], "content": m["content"]}
                   for m in row["messages"][:-1]]
        return {
            "id": f"style-{row['id']}",
            "source": row.get("source"),
            "session": row.get("session"),
            "channel": row.get("channel", "wechat"),
            "split": row.get("split", "train"),
            "context": context,
            "context_last": context[-2:],
            "original": row["messages"][-1]["content"],
            "ai_input": result["ai_input"],
            "privacy_problems": result["privacy_problems"],
            "attempts": result.get("attempts", 1),
        }

    def record_id(row: dict) -> str:
        return f"style-{row['id']}"

    records_by_id: dict[str, dict] = {}
    for row in candidates:
        rid = record_id(row)
        if rid in existing and existing[rid].get("ai_input"):
            records_by_id[rid] = existing[rid]
    pending = [row for row in candidates if record_id(row) not in records_by_id]
    failures = 0
    started = time.perf_counter()
    lock = threading.Lock()
    progress = PROGRESS.open("a", encoding="utf-8")

    def work(row: dict) -> tuple[dict, dict]:
        return row, rewrite_one(client, row)

    print(f"[style] pending={len(pending)} (existing={len(records_by_id)}) "
          f"workers={args.workers}", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(work, row): row for row in pending}
        for done, future in enumerate(as_completed(futures), 1):
            row, result = future.result()
            if not result["ai_input"]:
                failures += 1
                print(f"[style] {done}/{len(pending)} failed: {result['error']}", flush=True)
                continue
            record = make_record(row, result)
            records_by_id[record["id"]] = record
            with lock:
                progress.write(json.dumps(record, ensure_ascii=False) + "\n")
                progress.flush()
            if done % 20 == 0:
                print(f"[style] {done}/{len(pending)} generated", flush=True)
    progress.close()
    records = [records_by_id[record_id(row)] for row in candidates
               if record_id(row) in records_by_id]
    if args.full:
        # session 级切分：val ≥60 全微信；QQ 会话整体留 train
        wechat = [r for r in records if r.get("channel") == "wechat"]
        by_session: dict[str, list[dict]] = {}
        for record in wechat:
            by_session.setdefault(str(record.get("session")), []).append(record)
        total_wechat = len(wechat)
        target_val = max(60, round(total_wechat * 0.15))
        val_sessions: set[str] = set()
        count = 0
        for session, items in sorted(by_session.items(), key=lambda kv: (len(kv[1]), kv[0])):
            if count >= target_val:
                break
            val_sessions.add(session)
            count += len(items)
        for record in records:
            is_val = (record.get("channel") == "wechat"
                      and str(record.get("session")) in val_sessions)
            record["split"] = "val" if is_val else "train"
    write_preview(records, target_path)

    current = [r for r in records if r.get("ai_input")]
    stats = {
        "generated": len(current),
        "failed": failures,
        "too_long_retries": sum(1 for r in current if r.get("attempts", 1) > 1),
        "privacy_issues": sum(len(r.get("privacy_problems") or []) for r in current),
        "by_channel": dict(Counter(r.get("channel") for r in current)),
        "by_ending": dict(Counter(ht.ending_category(r["original"]) for r in current)),
        "seconds": round(time.perf_counter() - started, 1),
    }
    if not args.full:
        render_md(records, stats, PREVIEW_MD)
    print("[style] " + json.dumps(stats, ensure_ascii=False), flush=True)
    print(f"[style] output -> {target_path}"
          + (f" / {PREVIEW_MD}" if not args.full else ""), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
