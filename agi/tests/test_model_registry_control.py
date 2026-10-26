# -*- coding: utf-8 -*-
"""P20：模型注册表启停修复（外部进程识别/确认/安全拒绝；全部 mock，绝不真 kill）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agi.selflearn import model_registry as registry


@pytest.fixture
def cfg_path(tmp_path):
    state_path = tmp_path / "local_models_state.json"
    path = tmp_path / "models.json"
    path.write_text(json.dumps({
        "models": [{
            "name": "soul-05b", "base": "base", "adapter": "adapter",
            "gguf": "model.gguf", "port": 18123, "role": "soul", "status": "ready",
        }],
        "extra_adapters": [],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def _identity(safe: bool = True, command: str = "llama-server.exe -m model.gguf") -> dict:
    return {"pid": 26264, "command": command,
            "is_llama": "llama-server" in command, "has_gguf": ".gguf" in command,
            "safe": safe}


def _mock_kill(monkeypatch) -> list[int]:
    calls: list[int] = []

    def _kill(pid, timeout=8.0):
        calls.append(int(pid))
        return {"ok": True}

    monkeypatch.setattr(registry, "_kill_pid", _kill)
    return calls


def test_stop_external_needs_confirm_and_does_not_kill(cfg_path, monkeypatch):
    monkeypatch.setattr(registry, "_port_open", lambda port: True)
    monkeypatch.setattr(registry, "_port_pid", lambda port: 26264)
    monkeypatch.setattr(registry, "_process_identity", lambda pid: _identity(True))
    calls = _mock_kill(monkeypatch)

    out = registry.stop("soul-05b", path=cfg_path)
    assert out["ok"] is False
    assert out["status"] == "external"
    assert out["external"] is True and out["needs_confirm"] is True
    assert out["pid"] == 26264 and out["port"] == 18123
    assert "二次确认" in out["hint"]
    assert calls == []                      # 未确认，绝不 kill


def test_stop_external_force_kills_after_safety(cfg_path, monkeypatch):
    monkeypatch.setattr(registry, "_port_open", lambda port: True)
    monkeypatch.setattr(registry, "_port_pid", lambda port: 26264)
    monkeypatch.setattr(registry, "_process_identity", lambda pid: _identity(True))
    calls = _mock_kill(monkeypatch)

    out = registry.stop("soul-05b", path=cfg_path, force_external=True)
    assert out["ok"] is True and out["external"] is True
    assert out["pid"] == 26264
    assert calls == [26264]


def test_stop_refuses_unsafe_process(cfg_path, monkeypatch):
    monkeypatch.setattr(registry, "_port_open", lambda port: True)
    monkeypatch.setattr(registry, "_port_pid", lambda port: 999)
    monkeypatch.setattr(registry, "_process_identity",
                        lambda pid: _identity(False, "notepad.exe"))
    calls = _mock_kill(monkeypatch)

    out = registry.stop("soul-05b", path=cfg_path, force_external=True)
    assert out["ok"] is False
    assert out["status"] == "refused"
    assert out["error"] == "unsafe_process"
    assert "拒绝停止" in out["hint"]
    assert calls == []


def test_stop_no_process_returns_not_running(cfg_path, monkeypatch):
    monkeypatch.setattr(registry, "_port_open", lambda port: False)
    calls = _mock_kill(monkeypatch)
    out = registry.stop("soul-05b", path=cfg_path)
    assert out["ok"] is False
    assert out["status"] == "not_running"
    assert out["error"] == "not_started_by_registry"
    assert calls == []


def test_stop_registry_process_kills_and_clears_state(cfg_path, monkeypatch):
    state_path = cfg_path.with_name("local_models_state.json")
    state_path.write_text(json.dumps({"soul-05b": {"pid": 111, "port": 18123}}),
                          encoding="utf-8")
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(registry, "_port_open", lambda port: True)
    monkeypatch.setattr(registry, "_port_pid", lambda port: 111)
    monkeypatch.setattr(registry, "_process_identity", lambda pid: _identity(True))
    calls = _mock_kill(monkeypatch)

    out = registry.stop("soul-05b", path=cfg_path)
    assert out["ok"] is True and out["external"] is False
    assert calls == [111]
    assert not state_path.exists() or "soul-05b" not in json.loads(
        state_path.read_text(encoding="utf-8"))


def test_stop_registry_pid_mismatch_does_not_kill_wrong_process(cfg_path, monkeypatch):
    state_path = cfg_path.with_name("local_models_state.json")
    state_path.write_text(json.dumps({"soul-05b": {"pid": 111, "port": 18123}}),
                          encoding="utf-8")
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(registry, "_port_open", lambda port: True)
    monkeypatch.setattr(registry, "_port_pid", lambda port: 222)
    monkeypatch.setattr(registry, "_process_identity", lambda pid: _identity(True))
    calls = _mock_kill(monkeypatch)

    out = registry.stop("soul-05b", path=cfg_path)  # 未确认
    assert out["status"] == "external" and out["pid"] == 222
    assert calls == []                              # 不 kill 错误 PID
    assert "soul-05b" not in json.loads(state_path.read_text(encoding="utf-8"))


def test_start_port_listening_returns_real_pid(cfg_path, monkeypatch):
    monkeypatch.setattr(registry, "_port_open", lambda port: True)
    monkeypatch.setattr(registry, "_port_pid", lambda port: 26264)
    monkeypatch.setattr(registry, "_process_identity", lambda pid: _identity(True))
    out = registry.start("soul-05b", path=cfg_path)
    assert out["ok"] is True
    assert out["already_running"] is True
    assert out["pid"] == 26264
    assert out["external"] is True
    assert "PID 26264" in out["hint"]


def test_start_port_in_use_by_other_process_refused(cfg_path, monkeypatch):
    monkeypatch.setattr(registry, "_port_open", lambda port: True)
    monkeypatch.setattr(registry, "_port_pid", lambda port: 999)
    monkeypatch.setattr(registry, "_process_identity",
                        lambda pid: _identity(False, "other.exe"))
    out = registry.start("soul-05b", path=cfg_path)
    assert out["ok"] is False
    assert out["error"] == "port_in_use_by_other_process"
    assert out["pid"] == 999
    assert "非 llama-server" in out["hint"]
