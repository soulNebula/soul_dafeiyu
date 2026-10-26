# -*- coding: utf-8 -*-
"""P6-c：P6-a 规则真实模型离线验证 + 兜底多样性检查。

用法：
    python -m agi.selflearn.validate_p6c --dry      # 不调 API，fake 可重复
    python -m agi.selflearn.validate_p6c            # 真实 DeepSeek（读根 web_config.json）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONTACT_DB = ROOT / "agi" / "data" / "selflearn" / "wechat_contacts" / "4260fd457d8355.sqlite3"
# 今天真机 4 条的原 proactive 时间点（10:17 / 10:33 两条 / 11:08）
TARGETS = (
    ("case1-1017", 1792981038.2920806),
    ("case2-1033a", 1792981990.0),
    ("case3-1033b", 1792981990.0),
    ("case4-1108", 1792984135.6322355),
)
#: 去掉未来事件，保留原上下文
TRUNCATE = (
    ("proactive", "created_at"),
    ("messages", "ts"),
    ("turns", "created_at"),
)


def _copy_context(target_ts: float) -> str:
    if not CONTACT_DB.exists():
        raise FileNotFoundError(f"找不到真机上下文库：{CONTACT_DB}")
    temp = tempfile.mkdtemp(prefix="p6c_")
    path = os.path.join(temp, "ctx.sqlite3")
    shutil.copy2(CONTACT_DB, path)
    from agi.selflearn import db
    con = db.connect(path)
    try:
        for table, column in TRUNCATE:
            con.execute(f"DELETE FROM {table} WHERE {column} >= ?", (target_ts - 60,))
        con.commit()
    finally:
        con.close()
    return path


class _FakeClient:
    def __init__(self, variant: int = 0):
        self.variant = variant

    def available(self) -> bool:
        return True

    def chat(self, messages, **kwargs):
        texts = (
            "资料还没理完，有点烦",
            "看到一个说法，挺有意思",
            "刚整理完一批笔记，有点累",
            "翻到以前存的一篇文章，有点感慨",
        )
        return {"ok": True, "text": texts[self.variant % len(texts)]}


def _make_client(dry: bool):
    if dry:
        return _FakeClient()
    from agi.selflearn import llm
    client = llm.Client()
    if not client.available():
        raise RuntimeError("DeepSeek API key 不可用（检查根 web_config.json 的 llm.api_key）")
    return client


def _stats(results: list[dict]) -> dict:
    from agi.selflearn import media_choice, proactive
    unique = {row["text"] for row in results if row.get("text")}
    fallbacks = {text for text in proactive._SAFE_FALLBACK_POOL}
    fallback = sum(1 for row in results
                   if any(text in str(row.get("text") or "") for text in fallbacks))
    media_count = sum(1 for row in results if row.get("media_count", 0) > 0)
    media_directives = sum(1 for row in results if row.get("directive_count", 0) > 0)
    issues = 0
    for row in results:
        if media_choice.text_media_issues(row.get("text") or "",
                                          row.get("media_plan")):
            issues += 1
    return {
        "count": len(results),
        "unique": len(unique),
        "fallback": fallback,
        "issues": issues,
        "regen_failed": sum(1 for row in results if row.get("regen_failed")),
        "media_bubbles": media_count,
        "media_directives": media_directives,
    }


def run_validation(*, dry: bool = False, repeats: int = 3) -> dict:
    # 验证期间关闭 inner，只验证主动生成；media 开关读 agi/selflearn/web_config.json
    os.environ.setdefault("INNER_MODE", "off")
    os.environ.setdefault("PROACTIVE_INNER", "off")
    os.environ.pop("MEDIA_ENABLED", None)
    from agi.selflearn import media_choice, proactive

    old_decide = proactive.initiative.decide
    proactive.initiative.decide = lambda *a, **k: {}
    results: list[dict] = []
    try:
        for name, target_ts in TARGETS:
            for run in range(repeats):
                path = _copy_context(target_ts)
                client = _make_client(dry)
                now = target_ts - 1
                proactive._ACTIVE_DB_PATH = path
                row = proactive.tick(path, client=client, now=now,
                                     cooldown=0, min_score=0, respect_state=False)
                if not row.get("available"):
                    row = proactive.tick(path, client=client, now=now + 60,
                                         cooldown=0, min_score=0, respect_state=False)
                text = str(row.get("message") or "")
                media_plan = row.get("media") or None
                check = row.get("text_media_check") or {}
                media_count = len([item for item in (media_plan or {}).get("bubbles", [])
                                   if isinstance(item, dict) and item.get("type") in
                                   ("sticker", "gif", "image")])
                results.append({
                    "case": name, "run": run, "text": text,
                    "available": bool(row.get("available")),
                    "reason": row.get("reason"),
                    "media_plan": media_plan,
                    "media_count": media_count,
                    "directive_count": 1 if media_plan else 0,
                    "text_media_check": check,
                    "regen_failed": check.get("regen_failed") or [],
                })
    finally:
        proactive.initiative.decide = old_decide
        proactive._ACTIVE_DB_PATH = None
    return {"stats": _stats(results), "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="P6-c P6-a 规则验证")
    parser.add_argument("--dry", action="store_true", help="fake client，不调 API")
    parser.add_argument("--out", default="", help="保存 JSON 结果路径")
    args = parser.parse_args()
    payload = run_validation(dry=args.dry)
    print(json.dumps(payload["stats"], ensure_ascii=False, indent=2))
    print("\n=== 12 条样例 ===")
    for index, row in enumerate(payload["results"], 1):
        print(f"{index:02d}. [{row['case']}] {row['text']!r} "
              f"| media={row['media_count']} | issues={row['text_media_check'].get('issues')} "
              f"| fixed={row['text_media_check'].get('fixed')} "
              f"| regen_failed={row['regen_failed']} | reason={row['reason']}")
    if args.out:
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
