# -*- coding: utf-8 -*-
"""训练样本打包（tdd）：pending/progress/去重/自动打包。"""
from __future__ import annotations

import json
from pathlib import Path

from tools.train import packages


def _daily(root: Path, rows: list[dict]) -> None:
    directory = root / "daily"
    directory.mkdir(parents=True)
    (directory / "2026-10-30.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")


def test_summary_and_package(tmp_path):
    rows = [{"messages": [{"role": "user", "content": f"hi-{i}"}]} for i in range(5)]
    _daily(tmp_path, rows)
    state = packages.summary(tmp_path)
    assert state["pending"] == 5 and state["ready"] is False
    result = packages.create_package(tmp_path, label="manual")
    assert result["ok"] is True
    assert result["package"]["samples"] == 5
    after = packages.summary(tmp_path)
    assert after["packaged"] == 5 and after["pending"] == 0
    assert (tmp_path / "packages" / result["package"]["file"]).is_file()
    assert packages.create_package(tmp_path)["ok"] is False


def test_auto_package_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(packages, "THRESHOLD", 2)
    _daily(tmp_path, [{"messages": [{"role": "user", "content": "x"}]} for _ in range(2)])
    result = packages.auto_package(tmp_path)
    assert result["created"] is True
    assert result["package"]["label"] == "auto"
    assert packages.auto_package(tmp_path)["created"] is False


def test_dedup(tmp_path):
    row = {"messages": [{"role": "user", "content": "dup"}]}
    _daily(tmp_path, [row, row, {"messages": []}])
    result = packages.create_package(tmp_path)
    assert result["package"]["samples"] == 2
