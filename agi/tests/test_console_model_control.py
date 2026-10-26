# -*- coding: utf-8 -*-
"""P20 控制台模型启停：中文 hint/force_external/真实 PID/静态资产。"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tools.console import server as console


@pytest.fixture
def write_env(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"admin_token": "tok", "proactive_enabled": True},
                                 ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(console, "GATEWAY_CONFIG", config)
    monkeypatch.setattr(console, "STOP_PATH", tmp_path / "STOP")
    monkeypatch.setattr(console, "AUDIT_PATH", tmp_path / "audit.log")
    monkeypatch.setattr(console, "PROMPTS_PATH", tmp_path / "prompts.json")
    return {"config": config, "tmp": tmp_path}


def _call(method: str, path: str, body=None, *, token=None,
          host="127.0.0.1:802"):
    raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler = console.Handler.__new__(console.Handler)
    handler.path = path
    handler.command = method
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.close_connection = True
    handler.wfile = io.BytesIO()
    handler._headers_buffer = []
    handler.rfile = io.BytesIO(raw)
    handler.headers = {"Host": host, "Content-Length": str(len(raw))}
    if token:
        handler.headers["X-Admin-Token"] = token
    getattr(handler, "do_" + method)()
    raw_out = handler.wfile.getvalue().decode("utf-8")
    head, _, payload = raw_out.partition("\r\n\r\n")
    status = int(head.split(" ")[1]) if head else 0
    return status, (json.loads(payload) if payload.strip() else None)


def _fake_registry(external: bool = True):
    class FakeRegistry:
        calls: list[dict] = []

        @staticmethod
        def list_models():
            return [{"name": "soul-05b", "port": 8123, "role": "soul",
                     "status": "ready", "purpose": "灵魂"}]

        @staticmethod
        def health(name):
            return True

        @staticmethod
        def runtime_info(name):
            return {"ok": True, "running": True, "pid": 26264,
                    "port": 8123, "external": external, "healthy": True}

        @staticmethod
        def start(name):
            return {"ok": True, "already_running": True, "pid": 26264,
                    "port": 8123, "external": external, "healthy": True,
                    "hint": "端口 8123 已有 llama-server 在运行（PID 26264，外部启动），未重复启动。"}

        @staticmethod
        def stop(name, force_external=False):
            FakeRegistry.calls.append({"name": name, "force_external": force_external})
            if not force_external:
                return {"ok": False, "status": "external", "external": True,
                        "needs_confirm": True, "pid": 26264, "port": 8123,
                        "hint": "检测到 soul-05b 是外部启动的 llama-server（PID 26264，端口 8123，"
                                "注册表无记录）。停止前请二次确认；确认后带 force_external=true 再调用。"}
            return {"ok": True, "pid": 26264, "port": 8123, "external": True,
                    "hint": "已停止外部启动的 soul-05b（PID 26264，端口 8123）"}

    return FakeRegistry


def test_snapshot_has_runtime_external_badge_fields(write_env, monkeypatch):
    monkeypatch.setattr(console, "_model_registry", lambda: _fake_registry(True))
    snapshot = console.models_snapshot()
    item = snapshot["models"][0]
    assert item["running"] is True
    assert item["pid"] == 26264
    assert item["external"] is True
    assert "外部启动" in item["hint"]


def test_stop_model_external_hint_then_force(write_env, monkeypatch):
    fake = _fake_registry(True)
    monkeypatch.setattr(console, "_model_registry", lambda: fake)
    first = console.stop_model("soul-05b")
    assert first["ok"] is False and first["status"] == "external"
    assert "二次确认" in first["hint"]
    forced = console.stop_model("soul-05b", force_external=True)
    assert forced["ok"] is True and forced["external"] is True
    assert {"name": "soul-05b", "force_external": False} in fake.calls
    assert {"name": "soul-05b", "force_external": True} in fake.calls


def test_start_model_returns_real_pid_and_chinese_hint(write_env, monkeypatch):
    monkeypatch.setattr(console, "_model_registry", lambda: _fake_registry(True))
    result = console.start_model("soul-05b")
    assert result["ok"] is True
    assert result["pid"] == 26264
    assert "PID 26264" in result["hint"]


def test_http_stop_route_passes_force_external(write_env, monkeypatch):
    fake = _fake_registry(True)
    monkeypatch.setattr(console, "_model_registry", lambda: fake)
    status, data = _call("POST", "/api/models/stop",
                         {"name": "soul-05b", "force_external": True}, token="tok")
    assert status == 200 and data["ok"] is True
    assert {"name": "soul-05b", "force_external": True} in fake.calls


def test_http_stop_external_returns_hint_payload(write_env, monkeypatch):
    monkeypatch.setattr(console, "_model_registry", lambda: _fake_registry(True))
    status, data = _call("POST", "/api/models/stop", {"name": "soul-05b"}, token="tok")
    assert status == 400
    assert data["status"] == "external" and data["needs_confirm"] is True
    assert "hint" in data and "二次确认" in data["hint"]


def test_static_app_has_external_badge_confirm_and_no_question_pid():
    app_js = (Path(console.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
    assert "运行中（外部启动）" in app_js
    assert "不是控制台启动的" in app_js
    assert "force_external: true" in app_js
    assert "modelBusy" in app_js
    assert "PID ' + (result.pid || '?" not in app_js
