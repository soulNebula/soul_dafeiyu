# -*- coding: utf-8 -*-
"""M3-A TrainLab 后端测试：数据/资产发现、训练计划、后台任务、导出链（全 mock）。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from app import trainstudio


@pytest.fixture
def env(tmp_path, monkeypatch):
    soul = tmp_path / "SoulAgent"
    train = soul / "agi" / "data" / "train"
    evald = soul / "agi" / "eval_data"
    train.mkdir(parents=True)
    evald.mkdir(parents=True)
    for path, rows in ((train / "daily.jsonl", 3), (evald / "golden.jsonl", 2)):
        path.write_text("".join(json.dumps({"i": i}) + "\n" for i in range(rows)),
                        encoding="utf-8")
    agent = tmp_path / "agent"
    adapter = agent / "output" / "adapter_a"
    adapter.mkdir(parents=True)
    (adapter / "adapter_config.json").write_text("{}", encoding="utf-8")
    base = agent / "models" / "Qwen2.5-0.5B-Instruct"
    base.mkdir(parents=True)
    (base / "config.json").write_text("{}", encoding="utf-8")
    gguf = agent / "output" / "gguf"
    gguf.mkdir()
    (gguf / "m-q4_k_m.gguf").write_bytes(b"GGUF")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    data_dir = tmp_path / "idea" / ".data"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(trainstudio.paths, "soulagent_home", lambda: soul)
    monkeypatch.setattr(trainstudio.paths, "agent_home", lambda: agent)
    monkeypatch.setattr(trainstudio.paths, "DATA_DIR", data_dir)
    audits = []
    monkeypatch.setattr(trainstudio.audit, "record",
                        lambda action, target, **kw: audits.append(
                            {"action": action, "target": target, **kw}))
    return {"soul": soul, "train": train, "eval": evald, "agent": agent,
            "adapter": adapter, "base": base, "gguf": gguf,
            "outside": outside, "data": data_dir, "audits": audits}


def test_dataset_discovery_and_path_guard(env):
    items = trainstudio.list_datasets()
    assert items["ok"] is True and items["count"] == 2
    paths = {item["path"] for item in items["items"]}
    assert str((env["train"] / "daily.jsonl").resolve()) in paths
    assert str((env["eval"] / "golden.jsonl").resolve()) in paths
    assert trainstudio.resolve_dataset(str(env["outside"]))[1] == "path_out_of_allowed_roots"
    info = trainstudio.dataset_info(str(env["train"] / "daily.jsonl"))
    assert info["ok"] is True and info["rows"] == 3


def test_filter_length_uses_content_not_raw_json(env):
    # 行很长但正文很短，长度 4–500 应保留
    long_row = {"user_msg": "你好世界", "context": [{"role": "system", "text": "x" * 2000}],
                "target": {"a": "y" * 2000}}
    path = env["train"] / "long.jsonl"
    path.write_text(json.dumps(long_row, ensure_ascii=False) + "\n", encoding="utf-8")
    result = trainstudio.filter_dataset(str(path), name="long_filtered",
                                        min_chars=4, max_chars=500)
    assert result["ok"] is True and result["kept"] == 1


def test_assets(env):
    assets = trainstudio.list_assets()
    assert assets["ok"] is True
    assert any(item["name"] == "adapter_a" for item in assets["adapters"])
    assert any(item["name"] == "m-q4_k_m.gguf" for item in assets["gguf"])
    assert any(item["name"] == "Qwen2.5-0.5B-Instruct" for item in assets["bases"])


def test_preview_filter_and_discover(env):
    preview = trainstudio.preview_dataset(str(env["train"] / "daily.jsonl"), limit=2)
    assert preview["ok"] is True and preview["count"] == 2
    filtered = trainstudio.filter_dataset(
        str(env["train"] / "daily.jsonl"), name="filtered",
        min_chars=3, max_chars=1000, blacklist=["never"], dedupe=True)
    assert filtered["ok"] is True and filtered["kept"] == 3
    assert Path(filtered["output"]).is_file()
    again = trainstudio.filter_dataset(
        str(env["train"] / "daily.jsonl"), name="filtered", dedupe=True)
    assert again["removed"] == 0
    discovered = trainstudio.discover()
    assert discovered["ok"] is True
    assert len(discovered["datasets"]) >= 2
    assert any(item["name"] == "adapter_a" for item in discovered["adapters"])


def test_create_start_stop_job(env, monkeypatch):
    created = trainstudio.create_training_job(
        str(env["train"] / "daily.jsonl"), out_name="job_adapter")
    assert created["ok"] is True and created["status"] == "created"
    job_id = created["job"]["id"]
    assert trainstudio.job_status(job_id)["status"] == "created"
    stopped = trainstudio.stop_job(job_id)
    assert stopped["ok"] is True and stopped["status"] == "stopped"

    class FakeProc:
        pid = 4343

        def wait(self):
            return 0

    monkeypatch.setattr(trainstudio.subprocess, "Popen",
                        lambda *a, **k: FakeProc())
    created = trainstudio.create_training_job(
        str(env["train"] / "daily.jsonl"), out_name="job_adapter2")
    assert trainstudio.start_job(created["job_id"])["ok"] is True
    deadline = time.time() + 2
    while time.time() < deadline and trainstudio.job_status(created["job_id"])["status"] != "done":
        time.sleep(0.02)
    assert trainstudio.job_status(created["job_id"])["status"] == "done"
    assert any(item["action"] == "train.create" for item in env["audits"])
    assert any(item["action"] == "train.start" for item in env["audits"])
    assert any(item["action"] == "train.stop" for item in env["audits"])


def test_training_plan_uses_real_cli(env):
    plan = trainstudio.training_plan(str(env["train"] / "daily.jsonl"),
                                    output=str(env["agent"] / "output" / "new_adapter"),
                                    epochs=2, max_seq_len=256, lora_r=8)
    assert plan["ok"] is True
    cmd = plan["cmd"]
    assert "smoke_lora.py" in " ".join(cmd)
    assert "--data" in cmd and "--model-path" in cmd and "--save-adapter" in cmd
    assert "--epochs" in cmd and "2" in cmd
    assert "--max-seq-len" in cmd and "256" in cmd
    assert "--lora-r" in cmd and "8" in cmd
    # 没有随手编的未确认参数
    assert all(flag in cmd for flag in ("--data", "--model-path", "--save-adapter"))


def test_empty_model_path_falls_back_to_default(env):
    plan = trainstudio.training_plan(str(env["train"] / "daily.jsonl"), model_path="")
    assert plan["ok"] is True
    index = plan["cmd"].index("--model-path")
    assert plan["cmd"][index + 1] == trainstudio.DEFAULT_MODEL


def test_start_training_background_and_status(env, monkeypatch):
    class FakeProc:
        pid = 4242

        def wait(self):
            return 0

    monkeypatch.setattr(trainstudio.subprocess, "Popen",
                        lambda *a, **k: FakeProc())
    result = trainstudio.start_training(
        str(env["train"] / "daily.jsonl"),
        output=str(env["agent"] / "output" / "new_adapter"))
    assert result["ok"] is True and result["job"]
    job_id = result["job"]["id"]
    deadline = time.time() + 2
    while time.time() < deadline:
        status = trainstudio.job_status(job_id)
        if status.get("status") == "done":
            break
        time.sleep(0.02)
    status = trainstudio.job_status(job_id)
    assert status["ok"] is True and status["status"] == "done"
    assert status["returncode"] == 0 and status["pid"] == 4242
    assert any(item["action"] == "train.run" for item in env["audits"])
    assert trainstudio.list_jobs()["ok"] is True
    log = trainstudio.tail_log(job_id)
    assert log["ok"] is True and log["status"] == "done"


def test_job_metrics_from_summary_log(env):
    created = trainstudio.create_training_job(
        str(env["train"] / "daily.jsonl"), out_name="metrics_adapter")
    assert created["ok"] is True
    log = Path(created["log"])
    log.write_text('[smoke] SUMMARY {"loss_last": 1.25, "steps": 5}\n', encoding="utf-8")
    status = trainstudio.job_status(created["job"]["id"])
    assert status["metrics"]["loss_last"] == 1.25
    assert status["metrics"]["steps"] == 5


def test_export_plan_and_background(env, monkeypatch):
    plan = trainstudio.export_plan(str(env["adapter"]), str(env["base"]),
                                   "idea_test", out_dir=str(env["gguf"]))
    assert plan["ok"] is True
    assert "merge_adapter.py" in " ".join(plan["merge_cmd"])
    assert "--base" in plan["merge_cmd"] and "--adapter" in plan["merge_cmd"]
    assert "convert.py" in " ".join(plan["convert_cmd"])
    assert "--hf-dir" in plan["convert_cmd"] and "--name" in plan["convert_cmd"]
    assert plan["gguf"].endswith("idea_test-q4_k_m.gguf")
    assert trainstudio.export_plan(str(env["adapter"]), str(env["base"]),
                                   "bad name")["error"] == "invalid_name"

    class FakeRun:
        returncode = 0

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        # 模拟 convert 生成最终产物，避免被 product_missing 判失败
        if "--name" in cmd:
            out_dir = cmd[cmd.index("--out-dir") + 1]
            name = cmd[cmd.index("--name") + 1]
            Path(out_dir, f"{name}-q4_k_m.gguf").write_bytes(b"GGUF")
        return FakeRun()

    monkeypatch.setattr(trainstudio.subprocess, "run", fake_run)
    result = trainstudio.start_export(str(env["adapter"]), str(env["base"]),
                                      "idea_test", out_dir=str(env["gguf"]))
    assert result["ok"] is True
    job_id = result["job"]["id"]
    deadline = time.time() + 2
    while time.time() < deadline:
        if trainstudio.job_status(job_id).get("status") in ("done", "failed"):
            break
        time.sleep(0.02)
    status = trainstudio.job_status(job_id)
    assert status["status"] == "done"
    assert len(calls) == 2
    assert any(item["action"] == "train.export" for item in env["audits"])
