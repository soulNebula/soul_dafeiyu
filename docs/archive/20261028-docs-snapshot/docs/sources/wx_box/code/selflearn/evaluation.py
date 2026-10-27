# -*- coding: utf-8 -*-
"""13 弱验证：黄金集 + 成对比较 + 校准。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import db


def golden_set_load(path: Path | str) -> list[dict]:
    con = db.connect(path)
    try:
        row = con.execute("SELECT value FROM state WHERE key='golden_set'").fetchone()
    finally:
        con.close()
    if not row:
        return []
    try:
        data = json.loads(row[0])
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


def golden_set_save(path: Path | str, items: list[dict]) -> dict:
    con = db.connect(path)
    try:
        con.execute("INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    ("golden_set", json.dumps(items, ensure_ascii=False), __import__("time").time()))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "count": len(items)}


def paired_win_rate(wins: list[bool]) -> dict:
    n = len(wins)
    return {"ok": True, "n": n, "wins": sum(1 for w in wins if w),
            "win_rate": round(sum(1 for w in wins if w) / n, 3) if n else 0.0}


def calibrate(predicted: list[float], actual: list[bool], bins: int = 5) -> dict:
    if len(predicted) != len(actual) or not predicted:
        return {"ok": False, "error": "predicted/actual 长度不一致或为空"}
    buckets = [[] for _ in range(max(1, int(bins)))]
    for p, a in zip(predicted, actual):
        idx = min(len(buckets) - 1, max(0, int(float(p) * len(buckets))))
        buckets[idx].append((float(p), bool(a)))
    out = []
    for i, items in enumerate(buckets):
        if not items:
            out.append({"bin": i, "n": 0, "mean_pred": 0.0, "actual_rate": 0.0})
            continue
        out.append({"bin": i, "n": len(items),
                    "mean_pred": round(sum(x[0] for x in items) / len(items), 3),
                    "actual_rate": round(sum(1 for x in items if x[1]) / len(items), 3)})
    return {"ok": True, "bins": out,
            "note": "bin 内 mean_pred 与 actual_rate 差距越小越校准"}
