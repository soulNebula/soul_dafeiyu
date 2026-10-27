# -*- coding: utf-8 -*-
"""P30 训练门禁骨架：检查增量/间隔、准备 LoRA 数据、评估+候选 gate。

默认**只做计划，不真训练**（GPU 任务由 Lead 显式触发）。训练历史：
``agi/data/train/history.jsonl``。新版本只写新目录，不覆盖旧版；不自动切换线上。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRAIN_ROOT = ROOT / "agi" / "data" / "train"
DAILY_DIR = TRAIN_ROOT / "daily"
STAGED_DIR = TRAIN_ROOT / "staged"
HISTORY_PATH = TRAIN_ROOT / "history.jsonl"
GOLDEN_PATH = ROOT / "agi" / "eval_data" / "golden_v1.jsonl"
OUTPUT_ROOT = Path(r"E:\agent\output")
SMOKE_LORA = ROOT / "tools" / "train" / "smoke_lora.py"
MIN_SAMPLES = 300
MIN_DAYS = 3


def _as_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def read_history(path: Path | None = None) -> list[dict]:
    target = Path(path) if path else HISTORY_PATH
    if not target.exists():
        return []
    rows = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_history(entry: dict, path: Path | None = None) -> None:
    target = Path(path) if path else HISTORY_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _last_training(history: list[dict]) -> dict | None:
    candidates = [row for row in history if row.get("status") in ("trained", "candidate", "rejected")]
    return candidates[-1] if candidates else None


def daily_files(daily_dir: Path | None = None) -> list[Path]:
    target = Path(daily_dir) if daily_dir else DAILY_DIR
    return sorted(target.glob("*.jsonl")) if target.exists() else []


def count_incremental(history: list[dict] | None = None,
                      daily_dir: Path | None = None) -> dict:
    """统计上次训练之后的可用 SFT 样本（positive/correction）。"""
    history = history if history is not None else read_history()
    last = _last_training(history)
    last_date = _as_date(last.get("date", "")) if last else None
    files = daily_files(daily_dir)
    usable = 0
    files_used = []
    for path in files:
        day = _as_date(path.stem)
        if not day:
            continue
        if last_date and day <= last_date:
            continue
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            quality = str(row.get("meta", {}).get("quality") or "")
            if quality in ("positive", "correction"):
                count += 1
        if count:
            usable += count
            files_used.append({"date": path.stem, "samples": count})
    return {"samples": usable, "files": files_used,
            "last_training": last, "last_date": last_date.strftime("%Y-%m-%d")
            if last_date else None}


def check_ready(*, now: datetime | None = None, history_path: Path | None = None,
                daily_dir: Path | None = None) -> dict:
    now = now or datetime.now()
    history = read_history(history_path)
    stats = count_incremental(history, daily_dir)
    last = stats["last_training"]
    last_date = _as_date(last.get("date", "")) if last else None
    days = (now.date() - last_date.date()).days if last_date else 9999
    reasons = []
    if stats["samples"] < MIN_SAMPLES:
        reasons.append(f"samples<{MIN_SAMPLES}")
    if days < MIN_DAYS:
        reasons.append(f"days_since_last_training<{MIN_DAYS}")
    ready = not reasons
    next_ready = ""
    if not ready:
        need_days = max(0, MIN_DAYS - days) if last_date else 0
        next_ready = (now + timedelta(days=need_days)).strftime("%Y-%m-%d")
    return {"ready": ready, "samples": stats["samples"], "days_since_last": days,
            "reasons": reasons, "last_training": last, "next_ready": next_ready,
            "files": stats["files"], "checked_at": now.strftime("%Y-%m-%d %H:%M:%S")}


def _target_from_row(row: dict) -> dict | None:
    inner = row.get("meta", {}).get("inner_json")
    if not inner:
        return None
    try:
        value = json.loads(inner) if isinstance(inner, str) else inner
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def stage_training_data(*, history_path: Path | None = None,
                        daily_dir: Path | None = None,
                        staged_dir: Path | None = None) -> dict:
    """把增量 daily SFT 转成 smoke_lora 需要的 context/user_msg/target。"""
    history = read_history(history_path)
    stats = count_incremental(history, daily_dir)
    rows = []
    skipped = 0
    for file_info in stats["files"]:
        path = (Path(daily_dir) if daily_dir else DAILY_DIR) / f"{file_info['date']}.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            meta = row.get("meta") or {}
            if meta.get("quality") not in ("positive", "correction"):
                continue
            target = _target_from_row(row)
            if not target:
                skipped += 1
                continue
            messages = row.get("messages") or []
            system = next((m.get("content") for m in messages
                           if m.get("role") == "system"), "")
            user = next((m.get("content") for m in messages
                         if m.get("role") == "user"), "")
            rows.append({
                "id": row.get("id") or f"D-{len(rows)}",
                "context": [{"role": "system", "text": system},
                            {"role": "user", "text": user}],
                "user_msg": user,
                "target": target,
                "meta": {"source": meta.get("source"), "quality": meta.get("quality"),
                         "date": meta.get("date")},
            })
    target_dir = Path(staged_dir) if staged_dir else STAGED_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = target_dir / f"staged_{stamp}.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                            for row in rows), encoding="utf-8")
    return {"path": str(path), "samples": len(rows), "skipped_no_target": skipped,
            "source_samples": stats["samples"]}


def next_version(history: list[dict] | None = None,
                 output_root: Path | None = None) -> str:
    history = history if history is not None else read_history()
    root = Path(output_root) if output_root else OUTPUT_ROOT
    versions = []
    for row in history:
        value = str(row.get("version") or "")
        if value.startswith("v") and value[1:].isdigit():
            versions.append(int(value[1:]))
    for path in root.glob("soul_qwen25_05b_v*"):
        suffix = path.name.rsplit("_v", 1)[-1]
        if suffix.isdigit():
            versions.append(int(suffix))
    return f"v{max(versions) + 1 if versions else 1}"


def train_once(*, dry_run: bool = True, history_path: Path | None = None,
               daily_dir: Path | None = None, staged_dir: Path | None = None,
               output_root: Path | None = None) -> dict:
    """准备新版本训练；dry_run=True 只返回命令/路径，不执行 GPU。"""
    ready = check_ready(history_path=history_path, daily_dir=daily_dir)
    if not ready["ready"]:
        return {"ok": False, "status": "not_ready", **ready}
    history = read_history(history_path)
    version = next_version(history, output_root)
    staged = stage_training_data(history_path=history_path, daily_dir=daily_dir,
                                 staged_dir=staged_dir)
    if staged["samples"] < MIN_SAMPLES:
        return {"ok": False, "status": "not_ready", "reason": "staged_target_samples",
                "staged": staged, **ready}
    adapter_dir = (Path(output_root) if output_root else OUTPUT_ROOT) / f"soul_qwen25_05b_{version}"
    if adapter_dir.exists():
        return {"ok": False, "status": "version_exists", "version": version,
                "path": str(adapter_dir)}
    command = [
        "E:\\agent\\venv312\\Scripts\\python.exe", str(SMOKE_LORA),
        "--data", staged["path"], "--save-adapter", str(adapter_dir),
        "--max-seq-len", "512", "--epochs", "1",
    ]
    result = {"ok": True, "status": "planned", "version": version,
              "samples": staged["samples"], "adapter_dir": str(adapter_dir),
              "command": command, "dry_run": bool(dry_run), "staged": staged}
    if dry_run:
        return result
    proc = subprocess.run(command)
    result["returncode"] = proc.returncode
    result["status"] = "trained" if proc.returncode == 0 else "failed"
    if proc.returncode == 0:
        append_history({"ts": time.time(), "date": datetime.now().strftime("%Y-%m-%d"),
                        "version": version, "samples": staged["samples"],
                        "status": "trained"}, history_path)
    return result


def evaluate(version: str, *, output_root: Path | None = None) -> dict:
    """黄金集评估骨架：读取已落盘的 eval 结果；没有就返回待评估清单。"""
    golden = GOLDEN_PATH
    golden_n = sum(1 for line in golden.read_text(encoding="utf-8").splitlines() if line.strip()) \
        if golden.exists() else 0
    eval_path = (Path(output_root) if output_root else OUTPUT_ROOT) / f"soul_qwen25_05b_{version}" / "eval.json"
    if eval_path.exists():
        try:
            return {"ok": True, "version": version, "score": json.loads(
                eval_path.read_text(encoding="utf-8")).get("score"),
                "eval_path": str(eval_path), "golden_n": golden_n}
        except Exception:  # noqa: BLE001
            pass
    return {"ok": False, "version": version, "score": None,
            "reason": "evaluation_not_run", "golden_n": golden_n,
            "manual_checklist": ["随机抽 10 条看是否像人", "检查是否客服腔/总结腔",
                                 "检查是否复读/答非所问", "检查危机样本是否安全"]}


def gate(version: str, *, score: float | None = None, history_path: Path | None = None,
         output_root: Path | None = None) -> dict:
    """新分 ≥ 上一版 - 2 → candidate；绝不自动切换线上。"""
    history = read_history(history_path)
    # 只和「已训练版本」比；candidate/rejected 不自动成为基线（需 Lead 决定上线）
    previous = [row for row in history
                if row.get("score") is not None and row.get("status") == "trained"]
    if not previous:
        previous = [row for row in history if row.get("score") is not None]
    prev = previous[-1] if previous else None
    if score is None:
        evaluated = evaluate(version, output_root=output_root)
        score = evaluated.get("score")
    if score is None:
        return {"ok": False, "status": "evaluation_missing", "version": version,
                "candidate": False, "switched": False,
                "hint": "先跑黄金集并写 eval.json，再 gate"}
    prev_score = float(prev["score"]) if prev and prev.get("score") is not None else None
    passed = prev_score is None or float(score) >= prev_score - 2.0
    entry = {"ts": time.time(), "date": datetime.now().strftime("%Y-%m-%d"),
             "version": version, "score": float(score),
             "prev_score": prev_score, "status": "candidate" if passed else "rejected",
             "switched": False}
    append_history(entry, history_path)
    return {"ok": True, "status": entry["status"], "version": version,
            "score": float(score), "prev_score": prev_score,
            "candidate": passed, "switched": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每日增量训练门禁（默认只计划）")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--train-once", action="store_true")
    parser.add_argument("--execute", action="store_true",
                        help="真的跑 GPU 训练（默认 dry-run；需要 Lead 明确批准）")
    parser.add_argument("--evaluate", metavar="VERSION")
    parser.add_argument("--gate", metavar="VERSION")
    parser.add_argument("--score", type=float, default=None)
    parser.add_argument("--history", default="")
    args = parser.parse_args(argv)
    history = Path(args.history) if args.history else None
    if args.check:
        print(json.dumps(check_ready(history_path=history), ensure_ascii=False, indent=1))
    elif args.train_once:
        print(json.dumps(train_once(dry_run=not args.execute, history_path=history),
                         ensure_ascii=False, indent=1))
    elif args.evaluate:
        print(json.dumps(evaluate(args.evaluate), ensure_ascii=False, indent=1))
    elif args.gate:
        print(json.dumps(gate(args.gate, score=args.score, history_path=history),
                         ensure_ascii=False, indent=1))
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
