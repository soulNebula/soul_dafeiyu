# -*- coding: utf-8 -*-
"""P13 统一 LLM 路由层 —— 用量记账与日预算。

每次 ``router.chat`` 调用追加一行 JSONL（``agi/data/llm_usage.jsonl``）：
``ts / purpose / backend / backend_kind / tokens_in / tokens_out / latency_ms /
ok / fallback / budget_exceeded / error``。JSONL 便于控制台统计页按天/用途/后端聚合，
也便于 ``api_tokens_today`` 做每日 API token 预算判断。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_USAGE_PATH = ROOT / "agi" / "data" / "llm_usage.jsonl"
DAY_SECONDS = 86400.0

#: 计入「API token 预算」的后端 kind（本地 openai / rule 不计费）
API_BACKEND_KINDS = frozenset({"deepseek"})


def _now(now: float | None = None) -> float:
    return time.time() if now is None else float(now)


def day_start(now: float | None = None) -> float:
    """本地时区当天 00:00 的时间戳（预算按自然日重置）。"""
    ts = _now(now)
    local = time.localtime(ts)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                        0, 0, 0, 0, 0, -1))


def append_record(path: Path | str | None = None, record: dict | None = None) -> None:
    """追加一条用量记录；写盘失败静默（记账失败不能影响业务调用）。"""
    target = Path(path or DEFAULT_USAGE_PATH)
    payload = dict(record or {})
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 - 记账失败不影响调用
        pass


def read_records(path: Path | str | None = None, *, since_ts: float | None = None,
                 until_ts: float | None = None) -> list[dict]:
    """读 JSONL；坏行跳过（半行/被截断的最后一行不炸）。"""
    target = Path(path or DEFAULT_USAGE_PATH)
    if not target.exists():
        return []
    records: list[dict] = []
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(item, dict):
                    continue
                ts = item.get("ts")
                try:
                    ts_value = float(ts)
                except (TypeError, ValueError):
                    continue
                if since_ts is not None and ts_value < float(since_ts):
                    continue
                if until_ts is not None and ts_value > float(until_ts):
                    continue
                records.append(item)
    except OSError:
        return []
    return records


def api_tokens_today(path: Path | str | None = None, *,
                     now: float | None = None) -> int:
    """今天已经消耗的 API token（tokens_in + tokens_out，只算 deepseek 后端）。"""
    start = day_start(now)
    total = 0
    for record in read_records(path, since_ts=start):
        if str(record.get("backend_kind") or "") not in API_BACKEND_KINDS:
            continue
        try:
            total += int(record.get("tokens_in") or 0) + int(record.get("tokens_out") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _blank_bucket() -> dict:
    return {"calls": 0, "ok": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0,
            "latency_ms_sum": 0.0, "fallbacks": 0, "budget_exceeded": 0}


def _add(bucket: dict, record: dict) -> None:
    bucket["calls"] += 1
    ok = bool(record.get("ok"))
    bucket["ok" if ok else "failed"] += 1
    try:
        bucket["tokens_in"] += int(record.get("tokens_in") or 0)
        bucket["tokens_out"] += int(record.get("tokens_out") or 0)
        bucket["latency_ms_sum"] += float(record.get("latency_ms") or 0.0)
    except (TypeError, ValueError):
        pass
    if record.get("fallback"):
        bucket["fallbacks"] += 1
    if record.get("budget_exceeded"):
        bucket["budget_exceeded"] += 1


def _finish(bucket: dict) -> dict:
    out = dict(bucket)
    out["latency_ms_avg"] = round(
        out.pop("latency_ms_sum") / out["calls"], 2) if out["calls"] else 0.0
    return out


def summary(path: Path | str | None = None, *, days: int = 7,
            now: float | None = None) -> dict:
    """按天/用途/后端汇总最近 N 天用量（控制台统计页用）。"""
    days = max(1, int(days or 7))
    now_ts = _now(now)
    since = now_ts - days * DAY_SECONDS
    records = read_records(path, since_ts=since)
    total = _blank_bucket()
    by_purpose: dict[str, dict] = {}
    by_backend: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    for record in records:
        _add(total, record)
        purpose = str(record.get("purpose") or "unknown")
        backend = str(record.get("backend") or "unknown")
        _add(by_purpose.setdefault(purpose, _blank_bucket()), record)
        _add(by_backend.setdefault(backend, _blank_bucket()), record)
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(float(record["ts"])))
        except (KeyError, TypeError, ValueError, OSError):
            day = "unknown"
        bucket = by_day.setdefault(day, {"calls": 0, "tokens_in": 0,
                                         "tokens_out": 0, "ok": 0})
        bucket["calls"] += 1
        bucket["ok" if record.get("ok") else "failed"] = (
            bucket.get("ok" if record.get("ok") else "failed", 0) + 1)
        try:
            bucket["tokens_in"] += int(record.get("tokens_in") or 0)
            bucket["tokens_out"] += int(record.get("tokens_out") or 0)
        except (TypeError, ValueError):
            pass
    return {
        "window": {"days": days, "since": round(since, 3), "until": round(now_ts, 3)},
        "total": _finish(total),
        "by_purpose": {key: _finish(value) for key, value in by_purpose.items()},
        "by_backend": {key: _finish(value) for key, value in by_backend.items()},
        "by_day": dict(sorted(by_day.items())),
    }
