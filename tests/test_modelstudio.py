# -*- coding: utf-8 -*-
"""M3-B 模型中心后端测试：清单/运行中/启停/合并/转 GGUF/磁盘/安全边界。

全部 tmp_path 假文件；端口探测、子进程、audit 全部 mock，不联网、不真起进程。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import modelstudio


# --------------------------------------------------------------------------- #
# 测试环境
# --------------------------------------------------------------------------- #
def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "agent"
    soul = tmp_path / "SoulAgent"
    # base HF 模型
    base = home / "models" / "Qwen2.5-0.5B-Instruct"
    _write(base / "config.json", b'{"model_type":"qwen2"}')
    _write(base / "model.safetensors", b"weights" * 100)
    # gguf
    gguf = _write(home / "output" / "gguf" / "soul_v1.gguf", b"g" * 2048)
    # adapters（标准位置 + 历史 output/ 直放）
    adapter = home / "output" / "adapters" / "tone_v1"
    _write(adapter / "adapter_config.json",
           json.dumps({"base_model_name_or_path": str(base), "r": 16,
                       "lora_alpha": 32, "target_modules": ["q_proj"]}).encode())
    _write(adapter / "adapter_model.safetensors", b"a" * 512)
    bad_adapter = home / "output" / "adapters" / "bad"
    _write(bad_adapter / "adapter_config.json", b"{not json")
    legacy = home / "output" / "legacy_tone"
    _write(legacy / "adapter_config.json",
           json.dumps({"base_model_name_or_path": str(base)}).encode())
    # llama runtime
    server = _write(home / "llama" / "cuda" / "llama-server.exe", b"MZ")
    _write(home / "llama" / "cuda" / "llama-quantize.exe", b"MZ")
    (home / "llama" / "convert").mkdir(parents=True, exist_ok=True)
    _write(home / "llama" / "convert" / "convert_hf_to_gguf.py", b"# fake")
    # soulagent tools
    _write(soul / "tools" / "soul_serve" / "merge_adapter.py", b"# fake")
    _write(soul / "tools" / "soul_serve" / "convert.py", b"# fake")
    # 默认路径/端口/审计/日志全部指向 tmp
    monkeypatch.setattr(modelstudio, "agent_home", lambda: home)
    monkeypatch.setattr(modelstudio, "soulagent_home", lambda: soul)
    monkeypatch.setattr(modelstudio, "llama_server_path", lambda: server)
    monkeypatch.setattr(modelstudio, "MODEL_LOG_DIR", tmp_path / "modellogs")
    monkeypatch.setattr(modelstudio, "_venv_python",
                        lambda: tmp_path / "venv312" / "python.exe")
    audits: list[dict] = []
    monkeypatch.setattr(modelstudio.audit, "record",
                        lambda action, target, **kw: audits.append(
                            {"action": action, "target": target, **kw}))
    return {"home": home, "soul": soul, "base": base, "gguf": gguf,
            "adapter": adapter, "bad_adapter": bad_adapter, "legacy": legacy,
            "server": server, "audits": audits, "tmp": tmp_path}


# --------------------------------------------------------------------------- #
# 1. 模型清单
# --------------------------------------------------------------------------- #
def test_list_models_scans_base_gguf_adapters(env):
    result = modelstudio.list_models(env["home"])
    assert result["ok"] is True
    kinds = {item["kind"] for item in result["items"]}
    assert kinds == {"base", "gguf", "adapter"}
    assert result["counts"]["base"] == 1
    assert result["counts"]["gguf"] == 1
    # 标准 adapters + 历史 output 直放都找到
    adapter_names = {item["name"] for item in result["items"]
                     if item["kind"] == "adapter"}
    assert {"tone_v1", "bad", "legacy_tone"} <= adapter_names
    gguf = next(item for item in result["items"] if item["kind"] == "gguf")
    assert gguf["size"] == 2048 and gguf["path"].endswith("soul_v1.gguf")
    base = next(item for item in result["items"] if item["kind"] == "base")
    assert base["files"] >= 2


def test_list_models_adapter_meta_and_bad_json(env):
    result = modelstudio.list_models(env["home"])
    tone = next(item for item in result["items"] if item["name"] == "tone_v1")
    assert tone["r"] == 16 and tone["lora_alpha"] == 32
    assert tone["base_model"] == str(env["base"])
    bad = next(item for item in result["items"] if item["name"] == "bad")
    assert "error" in bad                       # 坏配置不炸，标错误


def test_runtime_info(env):
    info = modelstudio.runtime_info(env["home"])
    assert info["llama_server_exists"] is True
    assert info["llama_quantize_exists"] is True
    assert info["convert_dir_exists"] is True


def test_disk_usage(env):
    result = modelstudio.disk_usage(env["home"])
    assert result["ok"] is True
    by_kind = {item["kind"]: item for item in result["items"]}
    assert by_kind["models"]["size"] > 0
    assert by_kind["gguf"]["size"] == 2048
    assert by_kind["adapters"]["size"] > 0
    assert result["total"] >= by_kind["gguf"]["size"]


def test_list_adapters_only_adapter_dirs(env):
    result = modelstudio.list_adapters(env["home"])
    names = {item["name"] for item in result["items"]}
    assert names == {"tone_v1", "bad", "legacy_tone"}
    assert result["count"] == 3


# --------------------------------------------------------------------------- #
# 2. 运行中模型
# --------------------------------------------------------------------------- #
def test_running_models_reuses_port_state(env, monkeypatch):
    def fake_port_state(port):
        if port == 8123:
            return True, 1001
        return False, None

    monkeypatch.setattr(modelstudio.services, "port_state", fake_port_state)
    monkeypatch.setattr(modelstudio.services, "SERVICES", {
        "soul": {"port": 8123, "cmd": ["llama-server.exe", "-m", "E:/x/v1.gguf"]},
        "local3b": {"port": 8125, "cmd": ["python", "serve.py"]},
    })
    result = modelstudio.running_models(env["home"])
    assert result["ok"] is True and result["running"] == 1
    soul = next(item for item in result["items"] if item["port"] == 8123)
    assert soul["running"] is True and soul["pid"] == 1001
    assert soul["model_path"].endswith("v1.gguf")


# --------------------------------------------------------------------------- #
# 3. 启停
# --------------------------------------------------------------------------- #
class _FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.terminated = False

    def terminate(self):
        self.terminated = True


def test_start_model_success(env, monkeypatch):
    calls: list[dict] = []

    def fake_popen(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return _FakeProc()

    probes = iter([(False, None), (True, 4242)])
    monkeypatch.setattr(modelstudio.time, "sleep", lambda seconds: None)
    result = modelstudio.start_model(
        env["gguf"], 8124, ngl=50, ctx=2048, home=env["home"],
        wait_s=5, popen=fake_popen, port_state=lambda port: next(probes))
    assert result["ok"] is True and result["pid"] == 4242
    cmd = calls[0]["cmd"]
    assert str(env["server"]) == cmd[0]
    assert "-m" in cmd and str(env["gguf"]) in cmd
    assert "--port" in cmd and "8124" in cmd
    assert "-ngl" in cmd and "50" in cmd and "2048" in cmd
    log = modelstudio.MODEL_LOG_DIR / "8124.log"
    assert log.is_file() and result["log"] == str(log)


def test_start_model_rejects_ports_outside_whitelist(env):
    for port in (8000, 8099, 8200, 9000):
        result = modelstudio.start_model(env["gguf"], port, home=env["home"])
        assert result["ok"] is False and "port_out_of_range" in result["error"]
    assert modelstudio.start_model(env["gguf"], "abc",
                                   home=env["home"])["error"] == "invalid_port"


def test_start_model_rejects_path_outside_home(env):
    outside = env["tmp"] / "outside.gguf"
    outside.write_bytes(b"g")
    result = modelstudio.start_model(outside, 8124, home=env["home"])
    assert result["ok"] is False and result["error"] == "path_outside_agent_home"


def test_start_model_rejects_non_gguf_and_bad_params(env):
    txt = env["tmp"] / "model.txt"
    txt.write_text("x")
    inside = env["home"] / "model.txt"
    inside.write_text("x")
    outside = env["tmp"] / "x.gguf"
    outside.write_bytes(b"g")
    assert modelstudio.start_model(env["gguf"], 8124, ngl=1000,
                                   home=env["home"])["error"].startswith("ngl_out_of_range")
    assert modelstudio.start_model(env["gguf"], 8124, ctx=10,
                                   home=env["home"])["error"].startswith("ctx_out_of_range")
    assert modelstudio.start_model(env["gguf"], 8124, ngl="x",
                                   home=env["home"])["error"] == "invalid_ngl_or_ctx"
    assert modelstudio.start_model(inside, 8124,
                                   home=env["home"])["error"] == "model_path_must_be_gguf_file"


def test_start_model_port_in_use_and_missing_server(env, monkeypatch):
    result = modelstudio.start_model(
        env["gguf"], 8124, home=env["home"],
        port_state=lambda port: (True, 9))
    assert result["ok"] is False and result["error"] == "port_in_use"
    monkeypatch.setattr(modelstudio, "llama_server_path",
                        lambda: env["tmp"] / "nope.exe")
    result = modelstudio.start_model(env["gguf"], 8124, home=env["home"])
    assert result["ok"] is False and result["error"] == "llama_server_missing"


def test_start_model_timeout_terminates(env, monkeypatch):
    proc = _FakeProc()
    monkeypatch.setattr(modelstudio.time, "sleep", lambda seconds: None)
    clock = iter([0.0, 100.0, 100.0])
    monkeypatch.setattr(modelstudio.time, "time", lambda: next(clock))
    result = modelstudio.start_model(
        env["gguf"], 8124, home=env["home"], wait_s=0.1,
        popen=lambda cmd, **kw: proc, port_state=lambda port: (False, None))
    assert result["ok"] is False and result["error"] == "start_timeout"
    assert proc.terminated is True


def test_stop_model_validation_and_stopped(env, monkeypatch):
    assert modelstudio.stop_model(8000)["ok"] is False
    monkeypatch.setattr(modelstudio.services, "port_state",
                        lambda port: (False, None))
    result = modelstudio.stop_model(8124)
    assert result["ok"] is True and result["already_stopped"] is True


def test_stop_model_kills_pid(env, monkeypatch):
    probes = iter([(True, 4242), (False, None)])
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "SUCCESS"
        stderr = ""

    def fake_taskkill(args, **kwargs):
        calls.append(args)
        return _Result()

    result = modelstudio.stop_model(
        8124, port_state=lambda port: next(probes), taskkill=fake_taskkill)
    assert result["ok"] is True and result["pid"] == 4242
    assert calls[0][:3] == ["taskkill", "/PID", "4242"] and "/F" in calls[0]


def test_stop_model_pid_unknown(env, monkeypatch):
    monkeypatch.setattr(modelstudio.services, "port_state",
                        lambda port: (True, None))
    result = modelstudio.stop_model(8124)
    assert result["ok"] is False and result["error"] == "pid_unknown"


# --------------------------------------------------------------------------- #
# 4. 合并 adapter
# --------------------------------------------------------------------------- #
def test_merge_adapter_builds_real_cli(env):
    calls: list[dict] = []

    class _Result:
        returncode = 0
        stdout = "[merge] done"
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return _Result()

    result = modelstudio.merge_adapter(env["adapter"], home=env["home"],
                                       run=fake_run)
    assert result["ok"] is True
    args = calls[0]["args"]
    assert str(env["soul"] / "tools" / "soul_serve" / "merge_adapter.py") in args
    assert "--base" in args and str(env["base"]) in args
    assert "--adapter" in args and str(env["adapter"]) in args
    assert "--out" in args and str(env["home"] / "output" / "merged_tone_v1") in args
    assert "--device" in args and "cpu" in args
    assert calls[0]["kwargs"]["cwd"] == str(env["soul"])


def test_merge_adapter_errors(env):
    outside = env["tmp"] / "adapter"
    outside.mkdir()
    (outside / "adapter_config.json").write_text("{}")
    assert modelstudio.merge_adapter(outside, home=env["home"])["error"] == "path_outside_agent_home"
    assert modelstudio.merge_adapter(env["home"] / "missing",
                                     home=env["home"])["error"] == "adapter_not_found"
    assert modelstudio.merge_adapter(env["bad_adapter"],
                                     home=env["home"])["error"].startswith("base_")
    existing = env["home"] / "output" / "used"
    existing.mkdir(parents=True)
    (existing / "x").write_text("x")
    assert modelstudio.merge_adapter(env["adapter"], out=existing,
                                     home=env["home"])["error"] == "out_exists_not_empty"
    assert modelstudio.merge_adapter(env["adapter"], device="tpu",
                                     home=env["home"])["error"] == "invalid_device"


def test_merge_adapter_missing_tool(env, monkeypatch):
    monkeypatch.setattr(modelstudio, "soulagent_home",
                        lambda: env["tmp"] / "empty_soul")
    result = modelstudio.merge_adapter(env["adapter"], home=env["home"],
                                       run=lambda *a, **k: None)
    assert result["ok"] is False and result["error"] == "tool_script_missing"


# --------------------------------------------------------------------------- #
# 5. 转 GGUF
# --------------------------------------------------------------------------- #
def test_convert_gguf_builds_real_cli(env):
    merged = env["home"] / "output" / "merged_tone_v1"
    merged.mkdir(parents=True)
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append(args)
        return _Result()

    result = modelstudio.convert_gguf(merged, "Q4_K_M", home=env["home"],
                                      run=fake_run)
    assert result["ok"] is True and result["name"] == "tone_v1"
    args = calls[0]
    assert "--hf-dir" in args and str(merged) in args
    assert "--name" in args and "tone_v1" in args
    assert "--quant" in args and "Q4_K_M" in args
    assert "--out-dir" in args and str(env["home"] / "output" / "gguf") in args


def test_convert_gguf_validation(env):
    merged = env["home"] / "output" / "merged_x"
    merged.mkdir(parents=True)
    assert modelstudio.convert_gguf(merged, "bad quant", home=env["home"])["error"] == "invalid_quant"
    assert modelstudio.convert_gguf(merged, "Q4_K_M", name="a/b",
                                    home=env["home"])["error"] == "invalid_name"
    assert modelstudio.convert_gguf(env["tmp"] / "nope", home=env["home"])["error"] == "path_outside_agent_home"
    outside = env["tmp"] / "merged_outside"
    outside.mkdir()
    assert modelstudio.convert_gguf(outside, home=env["home"])["error"] == "path_outside_agent_home"


def test_status_aggregates(env, monkeypatch):
    monkeypatch.setattr(modelstudio.services, "port_state",
                        lambda port: (False, None))
    monkeypatch.setattr(modelstudio.services, "SERVICES", {})
    result = modelstudio.status(env["home"])
    assert result["ok"] is True
    assert set(result) >= {"catalog", "running", "disk", "adapters"}
    assert result["catalog"]["count"] >= 3


def test_port_boundaries():
    assert modelstudio._validate_port(8100)[0] == 8100
    assert modelstudio._validate_port(8199)[0] == 8199
    assert modelstudio._validate_port(8099)[1].startswith("port_out_of_range")
    assert modelstudio._validate_port(8200)[1].startswith("port_out_of_range")


def test_merge_adapter_resolves_hf_id_base(env):
    adapter = env["home"] / "output" / "adapters" / "hf_id_adapter"
    adapter.mkdir(parents=True)
    (adapter / "adapter_config.json").write_text(json.dumps({
        "base_model_name_or_path": "Qwen/Qwen2.5-0.5B-Instruct"}))
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    result = modelstudio.merge_adapter(
        adapter, home=env["home"],
        run=lambda args, **kw: calls.append(args) or _Result())
    assert result["ok"] is True
    args = calls[0]
    assert str(env["base"]) in args          # 回退到 <models>/<basename>


def test_running_models_includes_managed_ports(env, monkeypatch):
    modelstudio._MANAGED[8199] = {"pid": 6552,
                                  "model_path": "E:/x/soul.gguf",
                                  "log": "x.log"}
    try:
        monkeypatch.setattr(modelstudio.services, "port_state",
                            lambda port: (True, 6552) if port == 8199 else (False, None))
        monkeypatch.setattr(modelstudio.services, "SERVICES", {})
        result = modelstudio.running_models(env["home"])
        ports = [item["port"] for item in result["items"]]
        assert 8199 in ports
        managed = next(item for item in result["items"] if item["port"] == 8199)
        assert managed["model_path"] == "E:/x/soul.gguf"
        assert managed["pid"] == 6552
    finally:
        modelstudio._MANAGED.pop(8199, None)
