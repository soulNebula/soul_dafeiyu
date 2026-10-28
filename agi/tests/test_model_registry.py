# -*- coding: utf-8 -*-
"""模型注册表单测（P3-c）：预留位/健康检查/缺文件/安全启停。

可移植性说明（P0-24）：``agi/data/local_models.json`` 是本机部署配置、被
``.gitignore`` 排除，干净 checkout / CI 里不存在。下面这批判例验证的是「本机注册表
里 qwen15 预留位/tone 适配器不能被误删」，没有该文件时它们只能验证 None——
这里用 skipif 精确跳过（原因写清楚、条数可见），而不是让 CI 报假红，也绝不改成
放宽断言。纯逻辑用例（缺文件/坏 JSON/缺权重）不受影响，照常在任何机器上跑。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agi.selflearn import model_registry as registry

#: 本机注册表配置是否存在（不存在 → 依赖它的「防误删」用例精确 skip）
_LOCAL_CONFIG_MISSING = not Path(registry.CONFIG_PATH).is_file()
requires_local_registry = pytest.mark.skipif(
    _LOCAL_CONFIG_MISSING,
    reason="需要本机 agi/data/local_models.json（gitignore，不在仓库里；CI/干净 checkout 无此文件）",
)


@requires_local_registry
def test_base_reference_entry_exists():
    """防误删：0.5B 基座参考条目必须存在（合并/再训用），无端口。"""
    entry = registry.get("qwen05-base")
    assert entry is not None
    assert entry["role"] == "reference"
    assert not entry.get("port")
    assert str(entry["base"]).endswith("Qwen2.5-0.5B-Instruct")


@requires_local_registry
def test_extra_adapters_registered():
    adapters = {item["name"]: item for item in registry.list_extra_adapters()}
    assert "tone_qwen05_v1" in adapters
    assert "tone_style_qwen05_v3" in adapters
    assert adapters["tone_qwen05_v1"]["path"].endswith("tone_qwen05_v1")
    assert adapters["tone_style_qwen05_v3"]["path"].endswith("tone_style_qwen05_v3")


def test_missing_or_corrupt_config_returns_empty(tmp_path):
    missing = tmp_path / "nope.json"
    assert registry.load_config(missing)["models"] == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert registry.load_config(broken)["models"] == []
    assert registry.list_models(broken) == []


@requires_local_registry
def test_get_and_resolve_by_role():
    assert registry.get("soul-05b")["role"] == "soul"
    assert registry.get("no-such-model") is None
    soul = registry.resolve_by_role("soul")
    assert soul and all(item["role"] == "soul" for item in soul)
    assert registry.resolve_by_role("no-such-role") == []


def test_missing_files_detects_missing_paths(tmp_path):
    config = {
        "models": [{"name": "ghost", "base": str(tmp_path / "missing_base"),
                    "adapter": None, "gguf": None, "port": None,
                    "role": "test", "status": "ready"}],
        "extra_adapters": [],
    }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    missing = registry.missing_files(path=path)
    assert missing
    assert missing[0]["name"] == "ghost"
    assert missing[0]["kind"] == "base"


@requires_local_registry
def test_health_closed_port_and_unknown_do_not_raise():
    # 参考条目没有端口 → False，不抛异常
    assert registry.health("qwen05-base") is False
    assert registry.health("no-such-model") is False


@requires_local_registry
def test_start_reserved_and_unknown_refused():
    no_port = registry.start("qwen05-base")
    assert no_port["ok"] is False
    assert no_port["error"] == "model_has_no_port"
    unknown = registry.start("no-such-model")
    assert unknown["ok"] is False
    assert "unknown_model" in unknown["error"]


@requires_local_registry
def test_stop_unstarted_refused():
    result = registry.stop("qwen05-base")
    assert result["ok"] is False
    assert result["error"] in ("not_started_by_registry", "pid_not_alive",
                               "model_has_no_port")


@requires_local_registry
def test_cli_list_returns_json(capsys):
    exit_code = registry.main(["--list"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert isinstance(payload, list)
    assert any(item["name"] == "qwen05-base" for item in payload)


@requires_local_registry
def test_validate_registry_no_duplicate():
    result = registry.validate_registry()
    assert result["models"] >= 3
    assert result["duplicate_names"] == []
    assert result["duplicate_ports"] == []
    assert result["missing_files"] == []
