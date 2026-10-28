# -*- coding: utf-8 -*-
"""蒸馏语义数据工作室测试：数据集列表 / 任务创建 / 确认短语 / 日志。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app import distillstudio as ds


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    data = tmp_path / "agi" / "soul" / "data"
    data.mkdir(parents=True)
    (data / "soul_sft_v1.jsonl").write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    (data / "soul_sft_v1.jsonl.usage.json").write_text(
        json.dumps({"by_source": {"golden": 2}, "calls": 2},
                   ensure_ascii=False), encoding="utf-8")
    (data / "soul_sft_v1_train.jsonl").write_text('{"a":1}\n', encoding="utf-8")
    return tmp_path


def test_list_datasets(root):
    out = ds.list_datasets(root=root)
    assert out["ok"] is True
    names = [it["name"] for it in out["items"]]
    assert "soul_sft_v1.jsonl" in names
    main = next(it for it in out["items"] if it["name"] == "soul_sft_v1.jsonl")
    assert main["rows"] == 2 and main["derived"] is False
    assert main["sources"] == {"golden": 2}
    derived = next(it for it in out["items"] if it["name"].endswith("_train.jsonl"))
    assert derived["derived"] is True


def test_estimate_cost():
    free = ds.estimate(300, real=False)
    paid = ds.estimate(300, real=True)
    assert free["cost_cny"] == 0 and paid["cost_cny"] > 0
    assert ds.estimate(-5, real=True)["limit"] == 1


def test_create_mock_job_and_query(root):
    calls = []

    def runner(meta, root_arg):
        calls.append(meta["id"])
        meta["status"] = "done"
        ds._write_meta(meta, root_arg)

    out = ds.create_job(limit=5, root=root, runner=runner)
    assert out["ok"] is True
    job = out["job"]
    assert job["real"] is False and job["limit"] == 5
    assert calls == [job["id"]]
    got = ds.get_job(job["id"], root=root)
    assert got["ok"] is True and got["job"]["status"] == "done"
    logs = ds.read_log(job["id"], root=root)
    assert logs["ok"] is True


def test_real_requires_confirm(root):
    out = ds.create_job(limit=3, real=True, out_name="x", root=root)
    assert out["ok"] is False and out["error"] == "confirm_required"
    out2 = ds.create_job(limit=3, real=True, out_name="x", confirm="确认生成",
                         root=root, runner=lambda m, r: ds._write_meta(
                             {**m, "status": "done"}, r))
    assert out2["ok"] is True


def test_name_and_overwrite_guard(root):
    assert ds.create_job(limit=3, out_name="bad name!", root=root)["error"] == "name_invalid"
    (root / "agi" / "soul" / "data" / "taken.jsonl").write_text("{}\n", encoding="utf-8")
    assert ds.create_job(limit=3, out_name="taken", root=root)["error"] == "out_exists"
    out = ds.create_job(limit=3, out_name="taken", overwrite=True, root=root,
                        runner=lambda m, r: ds._write_meta({**m, "status": "done"}, r))
    assert out["ok"] is True


def test_cancel_job(tmp_path):
    root = tmp_path
    out = ds.create_job(limit=3, root=root,
                        runner=lambda m, r: ds._write_meta(
                            {**m, "pid": 999999, "status": "running"}, r))
    job = out["job"]
    res = ds.cancel_job(job["id"], root=root)
    # 假 pid taskkill 会失败，但仍应给出结构化错误而不是抛异常
    assert res["ok"] in (True, False)
    assert "error" in res or res["ok"] is True
