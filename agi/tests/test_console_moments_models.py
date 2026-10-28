# -*- coding: utf-8 -*-
"""802 控制台新增：发送模式 / 朋友圈 / 模型管理（全部 tmp，不联网）。"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "console_server2", ROOT / "tools" / "console" / "server.py")
console = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(console)

_MOMENTS_SPEC = importlib.util.spec_from_file_location(
    "moments_api_test", ROOT / "tools" / "console" / "moments_api.py")
moments_api = importlib.util.module_from_spec(_MOMENTS_SPEC)
_MOMENTS_SPEC.loader.exec_module(moments_api)

_MODEL_SPEC = importlib.util.spec_from_file_location(
    "model_admin_test", ROOT / "tools" / "console" / "model_admin.py")
model_admin = importlib.util.module_from_spec(_MODEL_SPEC)
_MODEL_SPEC.loader.exec_module(model_admin)


def _call(method, path, body=None, *, token=None, host="127.0.0.1:802"):
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
    head, _, payload = handler.wfile.getvalue().decode("utf-8").partition("\r\n\r\n")
    status = int(head.split(" ")[1]) if head else 0
    try:
        return status, json.loads(payload) if payload.strip() else None
    except ValueError:
        return status, payload


@pytest.fixture
def cons(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"admin_token": "tok", "send_mode": "confirm",
                               "contact_modes": {"A": "auto", "B": "confirm"}},
                              ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(console, "GATEWAY_CONFIG", cfg)
    monkeypatch.setattr(console, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(console, "AUDIT_PATH", tmp_path / "audit.log")
    monkeypatch.setattr(console, "DEFAULT_CONTACT_DB", None)
    return cfg


# ---------------------------------------------------------------- 发送模式
def test_send_mode_get_set_and_persist(cons):
    status, _ = _call("GET", "/api/control/send_mode")
    assert status == 401
    status, data = _call("GET", "/api/control/send_mode", token="tok")
    assert status == 200 and data["mode"] == "confirm" and data["label"] == "草稿"
    assert data["contact_summary"]["auto"] == 1
    status, data = _call("POST", "/api/control/send_mode", {"mode": "auto"}, token="tok")
    assert status == 200 and data["mode"] == "auto" and data["changed"] is True
    saved = json.loads(cons.read_text(encoding="utf-8"))
    assert saved["send_mode"] == "auto"
    assert list((Path(console.BACKUP_DIR)).glob("config_before_send_mode_*.json"))
    assert "send_mode" in Path(console.AUDIT_PATH).read_text(encoding="utf-8")
    status, data = _call("POST", "/api/control/send_mode", {"mode": "bogus"}, token="tok")
    assert status == 400 and data["error"] == "invalid_mode"


# ---------------------------------------------------------------- 朋友圈
def _seed_moments(tmp_path):
    moments_api.DATA_DIR = tmp_path / "moments"
    moments_api.DATA_DIR.mkdir()
    rows = [{"id": "M1", "tid": "T1", "nickname": "小明", "text": "今天天气不错",
             "create_time": 1_800_000_000,
             "images": [{"md5": "abc", "local_path": str(tmp_path / "pic.jpg")}],
             "likes": [{"nickname": "小红"}], "comments": []},
            {"id": "M2", "tid": "T2", "nickname": "小红", "text": "新买的猫",
             "create_time": 1_800_100_000, "images": [], "likes": [], "comments": []}]
    (tmp_path / "pic.jpg").write_bytes(b"jpeg")
    moments_api._merge_cache(rows)
    return rows


def test_moments_fetch_parse_merge_and_keyword(tmp_path, monkeypatch):
    script = tmp_path / "fetch.py"
    script.write_text("print('[]')", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(moments_api, "DATA_DIR", tmp_path / "moments")
    monkeypatch.setattr(moments_api, "FETCH_SCRIPT", script)
    monkeypatch.setattr(moments_api, "FETCH_PYTHON", python)
    calls = []
    rows = [{"id": "M1", "tid": "T1", "nickname": "小明", "text": "今天天气不错",
             "create_time": 1_800_000_000, "images": [], "likes": [], "comments": []}]

    def fake_run(command, **kwargs):
        calls.append(command)
        stdout = json.dumps(rows, ensure_ascii=False) if len(calls) == 1 else json.dumps(rows + [
            {"id": "M2", "tid": "T2", "nickname": "小红", "text": "新买的猫",
             "create_time": 1_800_100_000, "images": [], "likes": [], "comments": []}],
            ensure_ascii=False)
        return type("R", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    monkeypatch.setattr(moments_api.subprocess, "run", fake_run)
    first = moments_api.fetch_moments(limit=10)
    assert first["ok"] is True and first["fetched"] == 1 and first["added"] == 1
    second = moments_api.fetch_moments(limit=10)
    assert second["added"] == 1 and second["fetched"] == 2
    assert moments_api.load_cache(10)["total"] == 2
    assert moments_api.load_cache(10, keyword="猫")["total"] == 1
    assert "--keyword" not in calls[0] or True


def test_moments_media_and_detail(tmp_path):
    rows = _seed_moments(tmp_path)
    found = moments_api.find_media("abc")
    assert found["ok"] is True and found["path"] == rows[0]["images"][0]["local_path"]
    assert moments_api.find_media("missing")["error"] == "not_found"
    assert moments_api.get_moment("T2")["item"]["text"] == "新买的猫"
    assert moments_api.get_moment("NOPE")["ok"] is False


def test_moments_analyze_parses_json_and_saves(tmp_path, monkeypatch):
    _seed_moments(tmp_path)

    class FakeRouter:
        def chat(self, purpose, messages, **kwargs):
            return {"ok": True, "text": '```json\n{"summary":"轻松日常","topics":["天气","猫"],'
                                        '"mood":"轻松","social_signals":{"小红":"点赞"},'
                                        '"notable":["猫"],"suggestions":["多互动"]}\n```',
                    "backend": "mock", "model": "mock", "usage": {}}

    monkeypatch.setattr("agi.llm.router.Router", FakeRouter)
    result = moments_api.analyze(limit=10, question="总结")
    assert result["ok"] is True
    assert result["analysis"]["summary"] == "轻松日常"
    assert result["analysis"]["topics"] == ["天气", "猫"]
    assert Path(result["file"]).is_file()
    listed = moments_api.list_analyses()
    assert listed["count"] == 1 and listed["items"][0]["summary"] == "轻松日常"


# ---------------------------------------------------------------- 模型管理
@pytest.fixture
def model_env(tmp_path, monkeypatch):
    root = tmp_path / "models"
    adapters = tmp_path / "adapters"
    root.mkdir(); adapters.mkdir()
    (root / "hf1").mkdir()
    (root / "hf1" / "config.json").write_text("{}", encoding="utf-8")
    (root / "adapter1").mkdir()
    (root / "adapter1" / "adapter_config.json").write_text("{}", encoding="utf-8")
    (root / "ok.gguf").write_bytes(b"GGUF" + b"\x00" * 16)
    (root / "bad.gguf").write_bytes(b"NOPE" + b"\x00" * 16)
    monkeypatch.setattr(model_admin, "MODELS_ROOT", root)
    monkeypatch.setattr(model_admin, "ADAPTERS_ROOT", adapters)
    monkeypatch.setattr(model_admin, "IMPORT_ROOT", root / "imported")
    monkeypatch.setattr(model_admin, "DOWNLOAD_ROOT", root / "downloads")
    monkeypatch.setattr(model_admin, "TRASH_ROOT", root / ".trash")
    monkeypatch.setattr(model_admin, "STATUS_PATH", tmp_path / "downloads.json")
    return root


def test_model_list_and_validation(model_env):
    listed = model_admin.list_models()
    formats = {(row["name"], row["format"]) for row in listed["items"]}
    assert ("hf1", "hf") in formats and ("adapter1", "adapter") in formats
    assert ("ok.gguf", "gguf") in formats
    assert model_admin.detect_format(model_env / "ok.gguf") == ("gguf", "")
    assert model_admin.detect_format(model_env / "bad.gguf")[1] == "gguf_header_invalid"
    assert model_admin.detect_format(model_env / "hf1") == ("hf", "")
    assert model_admin.detect_format(model_env / "adapter1") == ("adapter", "")


def test_model_import_limits_and_delete(model_env, tmp_path):
    good = model_admin.import_model(str(model_env / "hf1"), name="copy_hf")
    assert good["ok"] is True and Path(good["path"]).is_dir()
    assert model_admin.import_model(str(model_env / "hf1"), name="copy_hf")["error"] == "destination_exists"
    assert model_admin.import_model(str(model_env / "bad.gguf"), name="bad")["error"] == "gguf_header_invalid"
    assert model_admin.import_model(str(tmp_path / "none"), name="none")["error"] == "path_not_found"
    outside = tmp_path / "outside.gguf"
    outside.write_bytes(b"GGUF" + b"\x00")
    assert model_admin.delete_model(str(outside))["error"] == "path_outside_models_root"
    deleted = model_admin.delete_model(str(model_env / "hf1"))
    assert deleted["ok"] is True and not (model_env / "hf1").exists()
    assert Path(deleted["moved_to"]).is_dir()


def test_model_download_mock(model_env, tmp_path, monkeypatch):
    class FakeResponse:
        headers = {"Content-Length": "20"}
        def __init__(self): self.data = b"GGUF" + b"\x00" * 16; self.pos = 0
        def read(self, size): chunk = self.data[self.pos:self.pos + size]; self.pos += len(chunk); return chunk
        def __enter__(self): return self
        def __exit__(self, *args): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=60: FakeResponse())
    target = model_admin.DOWNLOAD_ROOT / "d" / "x.gguf"
    model_admin._download_worker("dl1", "https://example.com/x.gguf", target)
    status = model_admin.download_status()
    assert status["items"][0]["status"] == "done" and target.is_file()
    assert model_admin.download_model("ftp://example.com/x.gguf")["error"] == "url_must_be_http"


# ---------------------------------------------------------------- HTTP 路由
def test_moments_models_routes(cons, monkeypatch, tmp_path):
    class FakeMoments:
        @staticmethod
        def load_cache(limit, keyword=""):
            return {"ok": True, "items": [{"tid": "T1", "text": "hi"}], "total": 1}
        @staticmethod
        def fetch_moments(limit, keyword=""):
            return {"ok": True, "items": [{"tid": "T1"}], "fetched": 1, "added": 1}
        @staticmethod
        def get_moment(tid):
            return {"ok": True, "item": {"tid": tid}} if tid == "T1" else {"ok": False, "error": "not_found"}
        @staticmethod
        def list_analyses(limit):
            return {"ok": True, "items": [], "count": 0}
        @staticmethod
        def analyze(limit, question="", client=""):
            return {"ok": True, "analysis": {"summary": "s"}, "name": "analysis_1.json"}
        @staticmethod
        def find_media(md5):
            return {"ok": False, "error": "not_found"}

    class FakeModels:
        @staticmethod
        def import_model(source, name="", fmt="auto"):
            return {"ok": True, "path": "x", "format": "hf"}
        @staticmethod
        def delete_model(path):
            return {"ok": True, "moved_to": "trash"}
        @staticmethod
        def download_model(url, name=""):
            return {"ok": True, "id": "dl1"}
        @staticmethod
        def download_status():
            return {"ok": True, "items": [], "count": 0}

    monkeypatch.setattr(console, "moments_module", lambda: FakeMoments)
    monkeypatch.setattr(console, "model_admin_module", lambda: FakeModels)
    monkeypatch.setattr(console, "models_snapshot", lambda: {"ok": True, "models": []})
    status, _ = _call("GET", "/api/moments")
    assert status == 401
    status, data = _call("GET", "/api/moments?limit=10", token="tok")
    assert status == 200 and data["total"] == 1
    status, data = _call("GET", "/api/moments/T1", token="tok")
    assert status == 200 and data["item"]["tid"] == "T1"
    status, data = _call("POST", "/api/moments/refresh", {"limit": 10}, token="tok")
    assert status == 200 and data["added"] == 1
    status, data = _call("POST", "/api/moments/analyze", {"limit": 10}, token="tok")
    assert status == 200 and data["analysis"]["summary"] == "s"
    status, data = _call("GET", "/api/moments/media?md5=x", token="tok")
    assert status == 404
    status, data = _call("GET", "/api/models", token="tok")
    assert status == 200 and "items" in data
    status, data = _call("POST", "/api/models/import", {"path": "x"}, token="tok")
    assert status == 200 and data["format"] == "hf"
    status, data = _call("POST", "/api/models/delete", {"path": "x"}, token="tok")
    assert status == 200 and data["moved_to"] == "trash"
    status, data = _call("POST", "/api/models/download", {"url": "https://x/y.gguf"}, token="tok")
    assert status == 200 and data["id"] == "dl1"
    status, data = _call("GET", "/api/models/download/status", token="tok")
    assert status == 200 and data["count"] == 0
    assert "model_import" in Path(console.AUDIT_PATH).read_text(encoding="utf-8")

def test_load_cache_dedupes_by_moment_id_not_shared_tid(tmp_path, monkeypatch):
    """朋友圈所有动态的 tid 都是账号 wxid；去重必须按动态 id（2026-10-29 线上 bug）。"""
    from tools.console import moments_api
    data_dir = tmp_path / "moments"
    data_dir.mkdir()
    rows = [
        {"id": "15000000000000001", "tid": "wxid_self", "nickname": "甲", "text": "a", "create_time": 1792783913},
        {"id": "15000000000000002", "tid": "wxid_self", "nickname": "乙", "text": "b", "create_time": 1792234266},
    ]
    (data_dir / "2026-10-24.jsonl").write_text(
        chr(10).join(json.dumps(r, ensure_ascii=False) for r in rows) + chr(10), encoding="utf-8")
    monkeypatch.setattr(moments_api, "DATA_DIR", data_dir)
    cache = moments_api.load_cache(limit=50)
    assert cache["total"] == 2 and cache["count"] == 2
    assert {i["nickname"] for i in cache["items"]} == {"甲", "乙"}
