# -*- coding: utf-8 -*-
"""P10：只读解析「刷B站」日报，供主动消息话题种子使用。"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = Path.home() / ".dsh" / "bili-duty" / "out"
FAILURE_HINTS = ("没跑起来", "退出码", "没注册驱动", "NO_ADAPTER", "ModuleNotFoundError")
_SECTION_RE = re.compile(r"^###\s+(BV[0-9A-Za-z]+)\s*(.*)$")
_WHY_RE = re.compile(r"为什么值得[：:\s]*(.*)$")
_QUOTE_RE = re.compile(r"它自己的一句话[：:\s]*(.*)$")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def report_dir() -> Path:
    """日报目录：env BILI_DUTY_OUT_DIR 优先。"""
    raw = os.environ.get("BILI_DUTY_OUT_DIR")
    return Path(raw).expanduser() if raw else DEFAULT_DIR


def _sentence(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip().strip("*` ")
    if not value:
        return ""
    match = re.match(r"(.+?[。！？!?])", value)
    return (match.group(1) if match else value).strip()


def _section_lines(lines: list[str], start: int) -> tuple[list[str], int]:
    body: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if line.startswith("### ") or line.startswith("## "):
            break
        body.append(line)
        index += 1
    return body, index


def parse_report(text: str, date: str = "") -> list[dict]:
    """解析 Markdown 日报；失败报告/坏文本返回 []。"""
    try:
        value = str(text or "")
    except Exception:  # noqa: BLE001
        return []
    if not value.strip():
        return []
    lines = value.splitlines()
    sections: list[dict] = []
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line.strip())
        if not match:
            continue
        bv = match.group(1)
        title = re.sub(r"^[\s*#【（(]+|[）)】*]+$", "", match.group(2) or "").strip()
        if not title:
            for candidate in lines[index + 1:index + 4]:
                value_line = candidate.strip().lstrip("-* ").strip()
                if value_line and not value_line.startswith(("为什么值得", "它自己的一句话", ">", "```")):
                    title = value_line
                    break
        body, _next = _section_lines(lines, index + 1)
        why = ""
        quote = ""
        joined = "\n".join(body)
        why_match = _WHY_RE.search(joined)
        if why_match:
            why = _sentence(why_match.group(1).split("\n", 1)[0])
        quote_match = _QUOTE_RE.search(joined)
        if quote_match:
            quote = _sentence(quote_match.group(1).split("\n", 1)[0])
        if not why:
            for body_line in body:
                candidate = body_line.strip().lstrip("-*> ").strip()
                if candidate and not candidate.startswith(("为什么值得", "它自己的一句话", "```")):
                    why = _sentence(candidate)
                    break
        title = re.sub(r"^[\s*#【（(]+|[）)】*]+$", "", title).strip()
        if title:
            sections.append({"bv": bv, "title": title, "why": why,
                             "quote": quote, "date": str(date or "")})
    if not sections and any(hint in value for hint in FAILURE_HINTS):
        return []
    return sections


def _date_from_name(path: Path) -> str:
    match = _DATE_RE.search(path.stem)
    return match.group(1) if match else ""


def latest_topics(days: int = 2, limit: int = 3, *, now: float | None = None,
                  directory: Path | str | None = None) -> dict:
    """按 mtime 取近 N 天日报，最新在前、BV 去重；返回 topics + report 信息。"""
    base = Path(directory) if directory else report_dir()
    now = time.time() if now is None else float(now)
    empty = {"topics": [], "report_path": "", "report_date": "",
             "reports": []}
    try:
        if not base.is_dir():
            return empty
        files = [p for p in base.glob("*.md") if p.is_file()]
    except Exception:  # noqa: BLE001
        return empty
    reports = []
    for path in files:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < now - max(1, int(days)) * 86400:
            continue
        reports.append((mtime, path))
    reports.sort(key=lambda item: item[0], reverse=True)
    topics: list[dict] = []
    seen_bv: set[str] = set()
    used_reports: list[dict] = []
    latest_path = ""
    latest_date = ""
    for mtime, path in reports:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        date = _date_from_name(path)
        if not latest_path:
            latest_path, latest_date = str(path), date
        parsed = parse_report(text, date=date)
        used_reports.append({"path": str(path), "date": date,
                             "mtime": round(mtime, 3), "items": len(parsed),
                             "ok": bool(parsed)})
        for item in parsed:
            if item["bv"] in seen_bv:
                continue
            seen_bv.add(item["bv"])
            topics.append(item)
            if len(topics) >= max(1, int(limit)):
                break
        if len(topics) >= max(1, int(limit)):
            break
    return {"topics": topics,
            "report_path": latest_path,
            "report_date": latest_date,
            "reports": used_reports}


def clean_title(title: Any, limit: int = 30) -> str:
    """标题清洗：去【】/（）推广词、压缩空白，截断到 limit 字。"""
    value = re.sub(r"[【（(][^】）)]{0,30}[】）)]", "", str(title or ""))
    value = re.sub(r"\s+", " ", value).strip(" -*#")
    return value[:max(1, int(limit))].strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agi.integrations.bili_digest")
    parser.add_argument("--show", action="store_true", help="打印最新解析结果")
    parser.add_argument("--dir", default="", help="覆盖日报目录")
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args(argv)
    base = Path(args.dir) if args.dir else report_dir()
    payload = latest_topics(days=args.days, limit=args.limit, directory=base)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
