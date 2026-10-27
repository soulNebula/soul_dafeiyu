# -*- coding: utf-8 -*-
"""P30 控制台只读接口：/api/train/status 与 /api/train/daily。"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

from tools.console import server as console


def _call(path: str):
    handler = console.Handler.__new__(console.Handler)
    handler.path = path
    handler.command = "GET"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.close_connection = True
    handler.wfile = io.BytesIO()
    handler._headers_buffer = []
    handler.rfile = io.BytesIO(b"")
    handler.headers = {"Host": "127.0.0.1:802"}
    handler.do_GET()
    raw = handler.wfile.getvalue().decode("utf-8")
    head, _, payload = raw.partition("\r\n\r\n")
    status = int(head.split(" ")[1]) if head else 0
    return status, (json.loads(payload) if payload.strip() else None)


def test_train_status_and_daily_helpers(tmp_path, monkeypatch):
    daily = tmp_path / "daily"
    daily.mkdir()
    today = time.strftime("%Y-%m-%d")
    (daily / f"{today}.jsonl").write_text(
        json.dumps({"messages": [{"role": "system", "content": "s"},
                                 {"role": "user", "content": "u"}],
                    "response": "r", "meta": {"quality": "positive"}},
                   ensure_ascii=False) + "\n", encoding="utf-8")
    (daily / f"{today}.md").write_text("# 日报\n", encoding="utf-8")
    (daily / f"{today}.summary.md").write_text("# 总结\n", encoding="utf-8")
    monkeypatch.setattr(console, "TRAIN_DAILY_DIR", daily)
    monkeypatch.setattr(console, "TRAIN_HISTORY_PATH", tmp_path / "history.jsonl")
    status = console.train_status()
    assert status["ok"] is True
    assert status["today"]["samples"] == 1
    assert status["today"]["usable"] == 1
    daily_data = console.train_daily(today)
    assert daily_data["ok"] is True
    assert daily_data["report"].startswith("# 日报")
    assert daily_data["summary"].startswith("# 总结")


def test_train_http_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "train_status",
                        lambda: {"ok": True, "today": {"samples": 1}})
    monkeypatch.setattr(console, "train_daily",
                        lambda date: {"ok": True, "date": date, "report": "# 日报"})
    status, data = _call("/api/train/status")
    assert status == 200 and data["today"]["samples"] == 1
    status, data = _call("/api/train/daily?date=2026-10-27")
    assert status == 200 and data["report"] == "# 日报"
    monkeypatch.setattr(console, "train_daily",
                        lambda date: {"ok": False, "error": "not_found"})
    status, data = _call("/api/train/daily?date=2020-01-01")
    assert status == 404 and data["error"] == "not_found"
