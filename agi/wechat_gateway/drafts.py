# -*- coding: utf-8 -*-
"""801 待发草稿（draft）：confirm 模式下先落盘，人工批准后才真正发送。

存储：``agi/data/wechat/drafts.jsonl``，一行一条 JSON，原子重写；
进程重启不丢草稿。纯本地文件，不涉及微信。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "agi" / "data" / "wechat" / "drafts.jsonl"

#: 草稿默认保留天数（配置 ``draft_ttl_days`` 可覆盖）。
#: 为什么要有 TTL：confirm 模式下每条被拦下的回复都会落一条草稿，长期不清会导致
#: ``drafts.jsonl`` 无限长（读改写成本线性上升），也会让人工审批页被陈年草稿淹没。
#: 7 天足够覆盖「周末不在电脑前」的场景，再久的草稿基本已经失去时效（对话早翻篇）。
DEFAULT_TTL_DAYS = 7.0

_LOCK = threading.Lock()


def _path(path: Path | str | None) -> Path:
    return Path(path) if path else DEFAULT_PATH


def _read_all(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("id"):
                rows.append(item)
    except OSError:
        return []
    return rows


def _write_all(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, path)


def add(contact: str, text: str, reason: str = "confirm",
        *, path: Path | str | None = None,
        ttl_days: float | None = None) -> dict:
    """新增一条草稿；返回草稿本身（含 id/ts）。

    写入前顺手 prune 一次（默认 7 天）：调用方（网关/发送失败兜底）不需要记得清理，
    草稿文件不会因为长期运行无限增长。``ttl_days=None`` 用
    :data:`DEFAULT_TTL_DAYS`；传 0/负数表示关闭 prune（调试/测试用）。
    """
    target = _path(path)
    item = {
        "id": "D-" + time.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3),
        "ts": time.time(),
        "contact": str(contact or ""),
        "text": str(text or ""),
        "reason": str(reason or "confirm"),
        "status": "pending",
    }
    days = DEFAULT_TTL_DAYS if ttl_days is None else float(ttl_days)
    with _LOCK:
        rows = _read_all(target)
        if days > 0:
            cutoff = item["ts"] - days * 86400.0
            rows = [row for row in rows
                    if float(row.get("ts") or 0) >= cutoff]
        rows.append(item)
        _write_all(target, rows[-200:])
    return item


def prune(*, max_age_days: float = DEFAULT_TTL_DAYS,
          path: Path | str | None = None,
          now: float | None = None) -> int:
    """删除超过 TTL 的草稿，返回删除条数（幂等；空文件/缺失文件返回 0）。

    - 只按 ``ts`` 判断，不区分 reason/status：草稿都是「等人工决定」的待办；
    - 没有 ``ts`` 的坏行按 0 处理（视为很老）→ 会被清掉，避免坏行永远留着；
    - ``max_age_days <= 0`` 表示不清理，直接返回 0。
    """
    days = float(max_age_days)
    if days <= 0:
        return 0
    target = _path(path)
    stamp = time.time() if now is None else float(now)
    cutoff = stamp - days * 86400.0
    with _LOCK:
        rows = _read_all(target)
        kept = [row for row in rows if float(row.get("ts") or 0) >= cutoff]
        removed = len(rows) - len(kept)
        if removed:
            _write_all(target, kept)
    return removed


def list_all(*, path: Path | str | None = None, limit: int = 50) -> list[dict]:
    rows = _read_all(_path(path))
    return rows[-max(1, int(limit)):]


def get(draft_id: str, *, path: Path | str | None = None) -> dict | None:
    target = str(draft_id or "").strip()
    for row in _read_all(_path(path)):
        if row.get("id") == target:
            return row
    return None


def discard(draft_id: str, *, path: Path | str | None = None) -> dict | None:
    """删除一条草稿（批准或拒绝后调用）；返回被删除的草稿。"""
    target_path = _path(path)
    target = str(draft_id or "").strip()
    with _LOCK:
        rows = _read_all(target_path)
        kept, removed = [], None
        for row in rows:
            if row.get("id") == target and removed is None:
                removed = row
                continue
            kept.append(row)
        if removed is not None:
            _write_all(target_path, kept)
    return removed
