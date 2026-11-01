# -*- coding: utf-8 -*-
"""V1 PR8：bot 包模型配置 → 801/802 实际 LLM 调用点。

全部用 tmp bot 包 + 假后端 / transport，不联网、不碰真实数据目录。
覆盖：
- bot 包 → 路由配置（chat/soul/vision/extract、云端灵魂独立 key）；
- 具体 purpose（selflearn_chat / inner_state …）落回 bot 通道；
- `agi.llm.default_router()` 在 bot 生效时用 bot 配置（含用量路径隔离）；
- `agi.llm.client.RouterClient` 兼容 selflearn Client 接口；
- `agi.selflearn.llm.load_config()` 的 key 优先级（bot > 全局）；
- `SelfLearnAgent` 默认 client 在 bot 生效时走路由；
- 模型连通测试（真实 key/离线 transport/缺 key 的可操作错误）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agi import bot_runtime
from agi.llm import router as router_mod
from agi.llm.client import RouterClient


# --------------------------------------------------------------------------- #
# 工具 / fixtures
# --------------------------------------------------------------------------- #
def make_bot(root: Path, *, soul_provider: str = "cloud",
             llm_key: str = "sk-test-bot", soul_base: str = "",
             soul_model: str = "", soul_key: str = "",
             llm_model: str = "", llm_base: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(exist_ok=True)
    (root / "secrets").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    (root / "bot.json").write_text(json.dumps({
        "id": root.name, "name": "小鱼", "plan": "cloud",
        "soul_provider": soul_provider, "version": 1,
    }, ensure_ascii=False), encoding="utf-8")
    app = {}
    llm = {}
    if llm_model:
        llm["model"] = llm_model
    if llm_base:
        llm["base_url"] = llm_base
    if llm:
        app["llm"] = llm
    (root / "config" / "app.json").write_text(json.dumps(app), encoding="utf-8")
    secrets = {"llm_api_key": llm_key, "soul_api_base": soul_base,
               "soul_model": soul_model, "soul_api_key": soul_key}
    (root / "secrets" / "keys.json").write_text(
        json.dumps(bot_runtime.encode_secrets(secrets)), encoding="utf-8")
    return root


@pytest.fixture()
def bot(tmp_path, monkeypatch):
    root = make_bot(tmp_path / "bots" / "xiaoyu")
    monkeypatch.setenv("SOULAGENT_BOT_ROOT", str(root))
    bot_runtime.reset()
    router_mod.reset_default_router()
    yield root
    bot_runtime.reset()
    router_mod.reset_default_router()


class FakeBackend:
    kind = "fake"

    def __init__(self, alias: str, *, ok: bool = True, text: str = "在。"):
        self.alias = alias
        self.ok = ok
        self.text = text
        self.calls: list[str] = []

    def available(self) -> bool:
        return self.ok

    def chat(self, messages, *, purpose="chat", max_tokens=800,
             temperature=0.7, timeout=None, json_mode=False):
        self.calls.append(purpose)
        if not self.ok:
            return {"ok": False, "text": "", "error": "fake_down"}
        return {"ok": True, "text": self.text, "model": self.alias,
                "usage": {"tokens_in": 3, "tokens_out": 1}}


def transport_ok(messages, body, timeout=None):
    return {"choices": [{"message": {"content": "在。"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1}}


# --------------------------------------------------------------------------- #
# 1. bot 包 → 路由配置
# --------------------------------------------------------------------------- #
def test_router_config_cloud_soul_uses_api(bot):
    cfg = bot_runtime.router_config(bot)
    assert cfg["purposes"]["soul"]["backend"] == "api"
    assert cfg["purposes"]["chat"]["backend"] == "api"
    assert cfg["backends"]["api"]["key_source"] == "llm"
    assert "soul_api" not in cfg["backends"]


def test_router_config_local_soul_and_custom_endpoint(tmp_path):
    root = make_bot(tmp_path / "bots" / "local", soul_provider="local",
                    soul_base="http://127.0.0.1:9000/v1", soul_model="soul-x")
    cfg = bot_runtime.router_config(root)
    assert cfg["purposes"]["soul"]["backend"] == "local"
    assert cfg["backends"]["local"]["base_url"].endswith(":9000/v1")
    assert cfg["backends"]["local"]["model"] == "soul-x"
    assert cfg["purposes"]["extract"]["backend"] == "local"


def test_router_config_cloud_soul_with_own_key(tmp_path):
    root = make_bot(tmp_path / "bots" / "soulapi",
                    soul_base="https://soul.example.com/v1",
                    soul_model="soul-cloud", soul_key="sk-soul")
    cfg = bot_runtime.router_config(root)
    assert cfg["purposes"]["soul"]["backend"] == "soul_api"
    assert cfg["backends"]["soul_api"]["key_source"] == "soul"
    assert cfg["backends"]["soul_api"]["base_url"] == "https://soul.example.com/v1"


def test_llm_override_from_app_json(bot):
    root = make_bot(bot, llm_model="deepseek-chat", llm_base="https://proxy.example.com")
    cfg = bot_runtime.router_config(root)
    assert cfg["backends"]["api"]["model"] == "deepseek-chat"
    assert cfg["backends"]["api"]["base_url"] == "https://proxy.example.com"


# --------------------------------------------------------------------------- #
# 2. 具体 purpose 落回 bot 通道
# --------------------------------------------------------------------------- #
def _router_for(cfg):
    overrides = {
        "api": FakeBackend("api", text="chat"),
        "local": FakeBackend("local", text="soul"),
        "rule": FakeBackend("rule", text="rule"),
    }
    if "soul_api" in cfg["backends"]:
        overrides["soul_api"] = FakeBackend("soul_api", text="soul_api")
    return router_mod.Router(config=cfg, backend_overrides=overrides,
                             usage_path=None)


def test_purpose_category_routing(bot):
    cfg = bot_runtime.router_config(bot)
    r = _router_for(cfg)
    chat = r.chat("selflearn_chat", [{"role": "user", "content": "hi"}])
    assert chat["ok"] and chat["backend"] == "api" and chat["category"] == "chat"
    soul = r.chat("inner_state", [{"role": "user", "content": "hi"}])
    assert soul["ok"] and soul["backend"] == "api" and soul["category"] == "soul"
    vision = r.chat("selflearn_image_understanding", [{"role": "user", "content": "hi"}])
    assert vision["ok"] and vision["category"] == "vision"
    extract = r.chat("keyword_extract", [{"role": "user", "content": "hi"}])
    assert extract["ok"] and extract["category"] == "extract"


def test_unknown_purpose_falls_back_to_chat_channel(bot):
    cfg = bot_runtime.router_config(bot)
    r = _router_for(cfg)
    result = r.chat("some_unregistered_purpose", [{"role": "user", "content": "hi"}])
    assert result["ok"] is True and result["backend"] == "api"
    assert result["category"] == "chat"


def test_unknown_purpose_still_errors_without_bot_categories(tmp_path):
    cfg = {"backends": {"rule": {"kind": "rule"}},
           "purposes": {"relevance_score": {"backend": "rule", "fallback": []}}}
    r = router_mod.Router(config=cfg,
                          backend_overrides={"rule": FakeBackend("rule")},
                          usage_path=None)
    result = r.chat("nope", [{"role": "user", "content": "hi"}])
    assert result["ok"] is False and "unknown_purpose" in result["error"]


# --------------------------------------------------------------------------- #
# 3. 默认 Router / 用量隔离
# --------------------------------------------------------------------------- #
def test_default_router_uses_bot_config(bot):
    r = router_mod.default_router()
    assert "chat" in r.purposes() and "soul" in r.purposes()
    assert r.purposes()["soul"]["backend"] == "api"
    assert str(r.log_path).endswith("llm_usage.jsonl")
    assert str(bot) in str(r.log_path)


def test_default_router_keeps_env_override(tmp_path, monkeypatch, bot):
    cfg_path = tmp_path / "router.json"
    cfg_path.write_text(json.dumps({
        "backends": {"rule": {"kind": "rule"}},
        "purposes": {"only": {"backend": "rule"}},
    }), encoding="utf-8")
    monkeypatch.setenv("LLM_ROUTER_CONFIG", str(cfg_path))
    router_mod.reset_default_router()
    r = router_mod.default_router()
    assert "only" in r.purposes() and "chat" not in r.purposes()


# --------------------------------------------------------------------------- #
# 4. RouterClient 兼容 selflearn Client
# --------------------------------------------------------------------------- #
def test_router_client_matches_client_interface(bot):
    cfg = bot_runtime.router_config(bot)
    client = RouterClient(_router_for(cfg), default_purpose="selflearn_chat")
    assert client.available() is True
    result = client.chat([{"role": "user", "content": "hi"}],
                         purpose="selflearn_chat", max_tokens=16)
    assert result["ok"] is True and result["text"] == "chat"
    assert result["usage"]["tokens_in"] == 3
    assert result["usage"]["prompt_tokens"] == 3
    assert result["backend"] == "api"


# --------------------------------------------------------------------------- #
# 5. selflearn 的 key / 默认 client
# --------------------------------------------------------------------------- #
def test_load_config_prefers_bot_key(bot, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    from agi.selflearn.llm import load_config
    cfg = load_config()
    assert cfg["api_key"] == "sk-test-bot"
    assert cfg["api_key_source"] == "bot"


def test_selflearn_agent_defaults_to_router_client(bot):
    from agi.selflearn.chat import SelfLearnAgent
    agent = SelfLearnAgent(bot / "db" / "x.sqlite3")
    assert isinstance(agent.client, RouterClient)
    assert agent.client.default_purpose == "selflearn_chat"


def test_selflearn_agent_without_bot_keeps_plain_client(monkeypatch, tmp_path):
    monkeypatch.delenv("SOULAGENT_BOT_ROOT", raising=False)
    bot_runtime.reset()
    from agi.selflearn.chat import SelfLearnAgent
    from agi.selflearn.llm import Client
    agent = SelfLearnAgent(tmp_path / "y.sqlite3")
    assert isinstance(agent.client, Client)


# --------------------------------------------------------------------------- #
# 6. 模型连通测试
# --------------------------------------------------------------------------- #
def test_model_check_offline_transport(bot, tmp_path):
    result = bot_runtime.model_check(
        bot, purposes=("chat", "soul"), transport=transport_ok,
        usage_path_override=tmp_path / "usage.jsonl")
    assert result["ok"] is True
    assert result["bot_id"] == "xiaoyu"
    assert [c["purpose"] for c in result["checks"]] == ["chat", "soul"]
    assert all(c["ok"] for c in result["checks"])


def test_model_check_missing_key_is_actionable(tmp_path):
    root = make_bot(tmp_path / "bots" / "nokey", llm_key="")
    result = bot_runtime.model_check(root, purposes=("chat",),
                                     usage_path_override=tmp_path / "u.jsonl")
    assert result["ok"] is False
    check = result["checks"][0]
    assert check["ok"] is False and check["error"]


def test_model_check_never_leaks_key(bot, tmp_path):
    result = bot_runtime.model_check(bot, purposes=("chat",),
                                     transport=transport_ok,
                                     usage_path_override=tmp_path / "u.jsonl")
    assert "sk-test-bot" not in json.dumps(result, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 7. 启动摘要 / 未启用 bot 时零变化
# --------------------------------------------------------------------------- #
def test_initialize_summary_is_redacted(bot):
    info = bot_runtime.initialize()
    assert info["active"] is True and info["bot_id"] == "xiaoyu"
    assert info["soul_backend"] == "api" and info["has_llm_key"] is True
    assert "sk-test-bot" not in json.dumps(info, ensure_ascii=False)


def test_no_bot_root_is_noop(monkeypatch, tmp_path):
    monkeypatch.delenv("SOULAGENT_BOT_ROOT", raising=False)
    # bots/active 指针也不存在时才是真正的旧路径
    bot_runtime.reset()
    assert bot_runtime.active_router_config() is None
    info = bot_runtime.initialize()
    assert info["active"] is False
    described = bot_runtime.describe()
    assert described["active"] is False