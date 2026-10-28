# -*- coding: utf-8 -*-
"""task-147：模型中心增强（下载/导入/删除/格式校验/清单）测试。

全部 tmp 路径；httpx/hf_hub 用 monkeypatch 模拟，不联网。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app import modelstudio


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "agent"
    (home / "models").mkdir(parents=True)
    state = tmp_path / ".data" / "downloads.json"
    monkeypatch.setattr(modelstudio, "DOWNLOAD_STATE", state)
    monkeypatch.setattr(modelstudio, "agent_home", lambda: home)
    monkeypatch.setattr(modelstudio, "_DL_CANCEL", set())
    audits: list[dict] = []
    monkeypatch.setattr(modelstudio.audit, "record",
                        lambda action, target, **kw: audits.append(
                            {"action": action, "target": target, **kw}))
    return {"home": home, "models": home / "models",
            "state": state, "audits": audits, "tmp": tmp_path}


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _fake_stream(files: dict[str, bytes] | None = None, error: str = ""):
    calls: list[dict] = []

    def stream(url, target, job_id, state_path=None):
        calls.append({"url": url, "target": str(target), "job_id": job_id})
        if error:
            raise RuntimeError(error)
        data = (files or {}).get(str(target), b"GGUF" + b"x" * 128)
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(data)
        return len(data)

    stream.calls = calls
    return stream


def _wait(job_id, **kwargs):
    return modelstudio.wait_download(job_id, timeout=5.0, interval=0.02, **kwargs)


# --------------------------------------------------------------------------- #
# 1. 在线下载
# --------------------------------------------------------------------------- #
def test_download_gguf_direct_success(env, monkeypatch):
    monkeypatch.setattr(modelstudio, "_stream_download", _fake_stream())
    result = modelstudio.download_model(
        {"name": "tiny-gguf", "url": "https://example.com/tiny.gguf"})
    assert result["ok"] is True and result["job_id"].startswith("DL-")
    done = _wait(result["job_id"])
    assert done["ok"] is True and done["status"] == "done"
    target = Path(done["job"]["target"])
    assert target.is_file()
    assert target.read_bytes().startswith(b"GGUF")
    assert done["job"]["bytes_done"] == target.stat().st_size
    assert done["job"]["source"] == "downloaded"


def test_download_bad_gguf_magic_fails_and_cleans(env, monkeypatch):
    def stream(url, target, job_id, state_path=None):
        Path(target).write_bytes(b"NOPE" + b"x" * 16)
        return Path(target).stat().st_size

    monkeypatch.setattr(modelstudio, "_stream_download", stream)
    result = modelstudio.download_model(
        {"name": "bad-gguf", "url": "https://example.com/bad.gguf"})
    done = _wait(result["job_id"])
    assert done["ok"] is False and done["status"] == "failed"
    assert "bad_gguf_magic" in done["error"]
    assert not Path(done["job"]["target"]).exists()


def test_download_http_error(env, monkeypatch):
    monkeypatch.setattr(modelstudio, "_stream_download",
                        _fake_stream(error="http_404"))
    result = modelstudio.download_model(
        {"name": "missing", "url": "https://example.com/x.gguf"})
    done = _wait(result["job_id"])
    assert done["ok"] is False and "http_404" in done["error"]


def test_download_cancel(env, monkeypatch):
    started = {"value": False}

    def slow(url, target, job_id, state_path=None):
        started["value"] = True
        for _ in range(200):
            if job_id in modelstudio._DL_CANCEL:
                raise modelstudio._DownloadCancelled()
            Path(target).write_bytes(b"GGUF" + b"x" * 8)
            time.sleep(0.02)
        return Path(target).stat().st_size

    monkeypatch.setattr(modelstudio, "_stream_download", slow)
    result = modelstudio.download_model(
        {"name": "slow", "url": "https://example.com/slow.gguf"})
    for _ in range(100):
        if started["value"]:
            break
        time.sleep(0.02)
    cancel = modelstudio.cancel_download(result["job_id"])
    assert cancel["ok"] is True
    done = _wait(result["job_id"])
    assert done["status"] == "cancelled"


def test_download_hf_fallback_uses_hf_mirror(env, monkeypatch):
    monkeypatch.setattr(modelstudio, "_hf_hub_download",
                        lambda repo, filename, target_dir: None)
    stream = _fake_stream({"": b'{' + b"x" * 16})
    monkeypatch.setattr(modelstudio, "_stream_download", stream)
    result = modelstudio.download_model({
        "name": "hf-model", "repo": "Qwen/Qwen2.5-0.5B-Instruct",
        "file": "model.safetensors", "mirror": "hf-mirror.com"})
    done = _wait(result["job_id"])
    assert done["ok"] is True and done["job"]["format"] == "hf"
    assert stream.calls[0]["url"] == (
        "https://hf-mirror.com/Qwen/Qwen2.5-0.5B-Instruct/resolve/main/model.safetensors")


def test_download_validation_and_conflict(env):
    assert modelstudio.download_model({"name": "a"})["error"] == "url_or_repo_required"
    assert modelstudio.download_model({"name": "a", "url": "ftp://x"})["error"] == "invalid_url"
    assert modelstudio.download_model({"name": "a/../b", "url": "https://x/y.gguf"})["error"] == "invalid_name"
    existing = env["models"] / "downloads" / "dup"
    _write(existing / "old.gguf", b"GGUF")
    result = modelstudio.download_model(
        {"name": "dup", "url": "https://x/new.gguf"})
    assert result["error"] == "target_exists"


def test_download_state_status(env):
    assert modelstudio.download_status()["ok"] is True
    assert modelstudio.download_status("DL-nope")["ok"] is False


# --------------------------------------------------------------------------- #
# 2. 格式校验
# --------------------------------------------------------------------------- #
def test_validate_gguf_and_bad_magic(env):
    good = _write(env["tmp"] / "good.gguf", b"GGUF" + b"\0" * 32)
    bad = _write(env["tmp"] / "bad.gguf", b"HTML" + b"\0" * 32)
    assert modelstudio.validate_model_path(good)["ok"] is True
    assert modelstudio.validate_model_path(good)["format"] == "gguf"
    checked = modelstudio.validate_model_path(bad)
    assert checked["ok"] is False and "bad_gguf_magic" in checked["reasons"]


def test_validate_hf_dir_and_html_safetensors(env):
    hf = env["tmp"] / "hf"
    _write(hf / "config.json", json.dumps({"model_type": "qwen2"}).encode())
    _write(hf / "model.safetensors", b"weights")
    checked = modelstudio.validate_model_path(hf)
    assert checked["ok"] is True and checked["format"] == "hf"
    missing_type = env["tmp"] / "hf2"
    _write(missing_type / "config.json", b'{"architectures": ["x"]}')
    _write(missing_type / "model.safetensors", b"weights")
    assert "config_missing_model_type" in modelstudio.validate_model_path(missing_type)["reasons"]
    html = _write(env["tmp"] / "x.safetensors", b"<!DOCTYPE html><title>404</title>")
    checked = modelstudio.validate_model_path(html)
    assert checked["ok"] is False and "looks_like_html_error_page" in checked["reasons"]


def test_validate_adapter_dir(env):
    adapter = env["tmp"] / "adapter"
    _write(adapter / "adapter_config.json", b'{"r": 8}')
    assert modelstudio.validate_model_path(adapter)["format"] == "adapter"
    bad = env["tmp"] / "adapter-bad"
    _write(bad / "adapter_config.json", b"{bad json")
    checked = modelstudio.validate_model_path(bad)
    assert checked["ok"] is False and checked["format"] == "adapter"


def test_validate_missing_path(env):
    checked = modelstudio.validate_model_path(env["tmp"] / "nope")
    assert checked["ok"] is False and checked["reasons"] == ["path_not_found"]


# --------------------------------------------------------------------------- #
# 3. 离线导入
# --------------------------------------------------------------------------- #
def _gguf_source(tmp: Path) -> Path:
    return _write(tmp / "src" / "local.gguf", b"GGUF" + b"\0" * 512)


def test_import_gguf(env):
    source = _gguf_source(env["tmp"])
    result = modelstudio.import_model(
        {"name": "imported-gguf", "path": str(source), "format": "gguf"})
    assert result["ok"] is True and result["format"] == "gguf"
    target = Path(result["path"])
    assert target.is_dir()
    assert (target / source.name).read_bytes() == source.read_bytes()
    assert result["size"] == source.stat().st_size
    inventory = modelstudio.model_inventory()
    item = next(x for x in inventory["items"] if x["name"] == "imported-gguf")
    assert item["source"] == "imported" and item["usable_for_serve"] is True


def test_import_hf_and_adapter_dir(env):
    hf = env["tmp"] / "hf-src"
    _write(hf / "config.json", json.dumps({"model_type": "qwen2"}).encode())
    _write(hf / "model.safetensors", b"weights")
    result = modelstudio.import_model({"name": "hf-imported", "path": str(hf)})
    assert result["ok"] is True and result["format"] == "hf"
    adapter = env["tmp"] / "adapter-src"
    _write(adapter / "adapter_config.json", b'{"r": 8}')
    result = modelstudio.import_model({"name": "adapter-imported", "path": str(adapter)})
    assert result["ok"] is True and result["format"] == "adapter"
    inventory = modelstudio.model_inventory()
    by_name = {item["name"]: item for item in inventory["items"]}
    assert by_name["hf-imported"]["usable_for_train"] is True
    assert by_name["adapter-imported"]["format"] == "adapter"


def test_import_errors(env):
    source = _gguf_source(env["tmp"])
    assert modelstudio.import_model({"name": "x", "path": str(source),
                                     "format": "hf"})["error"] == "format_mismatch"
    bad = env["tmp"] / "bad"
    _write(bad / "x.txt", b"x")
    result = modelstudio.import_model({"name": "bad", "path": str(bad)})
    assert result["ok"] is False and result["error"] == "invalid_model"
    assert modelstudio.import_model({"name": "x", "path": str(env["tmp"] / "nope")})["error"] == "source_not_found"
    # 同名冲突 + overwrite
    first = modelstudio.import_model({"name": "dup-import", "path": str(source)})
    assert first["ok"] is True
    second = modelstudio.import_model({"name": "dup-import", "path": str(source)})
    assert second["error"] == "target_exists"
    override = modelstudio.import_model({"name": "dup-import", "path": str(source),
                                         "overwrite": True})
    assert override["ok"] is True


# --------------------------------------------------------------------------- #
# 4. 删除（进 .trash）
# --------------------------------------------------------------------------- #
def test_delete_moves_to_trash(env):
    source = _gguf_source(env["tmp"])
    imported = modelstudio.import_model({"name": "to-delete", "path": str(source)})
    target = Path(imported["path"])
    result = modelstudio.delete_model({"name": "to-delete"})
    assert result["ok"] is True
    assert not target.exists()
    moved = Path(result["moved_to"])
    assert moved.exists()
    assert modelstudio.TRASH_DIRNAME in str(moved)
    inventory = modelstudio.model_inventory()
    assert inventory["ok"] is True
    assert not any(item["name"] == "to-delete" for item in inventory["items"])


def test_delete_rejects_path_traversal(env):
    outside = env["tmp"] / "outside.gguf"
    outside.write_bytes(b"GGUF")
    result = modelstudio.delete_model({"path": str(outside)})
    assert result["ok"] is False and result["error"] == "path_outside_models"
    result = modelstudio.delete_model({"name": "../outside"})
    assert result["ok"] is False and result["error"] == "invalid_name"
    result = modelstudio.delete_model({"name": "not-there"})
    assert result["ok"] is False and result["error"] == "not_found"


def test_delete_kind_mismatch(env):
    hf = env["tmp"] / "hf-del"
    _write(hf / "config.json", b'{"model_type":"qwen2"}')
    _write(hf / "model.safetensors", b"w")
    imported = modelstudio.import_model({"name": "hf-del", "path": str(hf)})
    result = modelstudio.delete_model({"name": "hf-del", "kind": "gguf"})
    assert result["ok"] is False and result["error"] == "kind_mismatch"
    assert Path(imported["path"]).exists()


# --------------------------------------------------------------------------- #
# 5. 清单增强
# --------------------------------------------------------------------------- #
def test_model_inventory_fields_and_sources(env):
    # builtin HF
    hf = env["models"] / "Qwen2.5-0.5B-Instruct"
    _write(hf / "config.json", b'{"model_type":"qwen2"}')
    _write(hf / "model.safetensors", b"w")
    # downloaded / imported
    _write(env["models"] / "downloads" / "dl1" / "m.gguf", b"GGUF" + b"x")
    imported = modelstudio.import_model(
        {"name": "imp1", "path": str(_gguf_source(env["tmp"]))})
    # output gguf
    _write(env["home"] / "output" / "gguf" / "out.gguf", b"GGUF" + b"x")
    inventory = modelstudio.model_inventory()
    assert inventory["ok"] is True
    formats = {item["format"] for item in inventory["items"]}
    assert {"hf", "gguf"} <= formats
    sources = {item["source"] for item in inventory["items"]}
    assert {"builtin", "downloaded", "imported"} <= sources
    hf_item = next(item for item in inventory["items"] if item["name"] == "Qwen2.5-0.5B-Instruct")
    assert hf_item["usable_for_train"] is True and hf_item["usable_for_serve"] is False
    serve_item = next(item for item in inventory["items"] if item["name"] == "out.gguf")
    assert serve_item["usable_for_serve"] is True
    assert imported["ok"] is True
    assert inventory["count"] >= 4
