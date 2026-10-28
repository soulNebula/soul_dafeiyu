# -*- coding: utf-8 -*-
"""Agent B / task-3 安全回归：密钥外部化、留存 TTL、ACL 复核、明文扫描。

覆盖点（每条都对应一个「用户能看见的承诺」）：

1. ``agi/selflearn/llm.py``：env > secrets.json > web_config.json 三级来源、
   ``SOULAGENT_WEB_CONFIG`` 覆盖、错误/日志不泄 key、明文命中告警；
2. ``agi/secrets_store.py``：路径覆盖、损坏文件 fail-closed、写入幂等、诊断不返回值；
3. reader 接入：``wx_core/config.py``（db_keys 等）、``webui/llm.py``、
   ``wx_core/web_search.py`` —— 默认行为不变、迁移后仍拿得到 key、env 仍能覆盖；
4. ``tools/rotate_secrets.py``：dry-run 不改文件、``--apply`` 只复制、``--purge-plaintext``
   校验后清空、幂等、写失败不清空、用法错误退出码；
5. ``tools/prune_chat_logs.py``：dry-run 不改、超期行/文件清理、无法判时的行保留、中文往返；
6. ``tools/check_secrets.py``：新规则能扫出 sk-/db_keys/aes_key，占位符不误报；
7. ``tools/harden_acl.py``：TARGETS 覆盖、Authenticated Users 识别、``--verify`` 非零退出；
8. ``.gitignore`` / ``requirements.txt`` 的可移植性与「不提交密钥」底线。

全部离线：不联网、不碰真实微信、不写生产目录（路径一律 tmp_path + env 覆盖）。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agi import secrets_store  # noqa: E402
from agi.selflearn import llm  # noqa: E402
from tools import check_secrets, harden_acl, prune_chat_logs, rotate_secrets  # noqa: E402

FAKE_API_KEY = "sk-AgentB0123456789abcdefXYZ0123"
FAKE_DB_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
FAKE_AES_KEY = "kZ8Qm2Xr7Tp1Lw9N"


# --------------------------------------------------------------------------- #
# 公共夹具
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离宿主机环境：三个覆盖变量都默认不存在，缓存清空。"""
    for name in ("DEEPSEEK_API_KEY", "SOULAGENT_SECRETS", "SOULAGENT_WEB_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    secrets_store.reset_cache()
    llm._WARNED_PLAINTEXT.clear()
    yield
    secrets_store.reset_cache()
    llm._WARNED_PLAINTEXT.clear()


def _write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _secrets_file(tmp_path: Path, *, api_key: str = "", config: dict | None = None) -> Path:
    payload: dict = {"version": 1}
    if api_key:
        payload["llm"] = {"api_key": api_key}
    if config:
        payload["config"] = config
    return _write_json(tmp_path / "secrets" / "secrets.json", payload)


# --------------------------------------------------------------------------- #
# 1) agi/selflearn/llm.py：三级来源 + 不泄 key
# --------------------------------------------------------------------------- #
def test_llm_env_beats_secrets_and_file(tmp_path, monkeypatch):
    web = _write_json(tmp_path / "web_config.json", {"llm": {"api_key": "sk-fileOnly0000000000", "model": "deepseek-flash"}})
    _secrets_file(tmp_path, api_key="sk-secretsOnly00000000")
    monkeypatch.setenv("SOULAGENT_WEB_CONFIG", str(web))
    monkeypatch.setenv("SOULAGENT_SECRETS", str(tmp_path / "secrets" / "secrets.json"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_API_KEY)

    config = llm.load_config()
    assert config["api_key"] == FAKE_API_KEY
    assert config["api_key_source"] == "env"


def test_llm_secrets_beats_file(tmp_path, monkeypatch):
    web = _write_json(tmp_path / "web_config.json", {"llm": {"api_key": "sk-fileOnly0000000000"}})
    secrets_path = _secrets_file(tmp_path, api_key=FAKE_API_KEY)
    monkeypatch.setenv("SOULAGENT_WEB_CONFIG", str(web))
    monkeypatch.setenv("SOULAGENT_SECRETS", str(secrets_path))

    config = llm.load_config()
    assert config["api_key"] == FAKE_API_KEY
    assert config["api_key_source"] == "secrets"


def test_llm_falls_back_to_plaintext_file(tmp_path, monkeypatch):
    """没有 env/密钥库时行为与迁移前一致（读 web_config.json）。"""
    web = _write_json(tmp_path / "web_config.json",
                      {"llm": {"api_key": FAKE_API_KEY, "base_url": "https://api.deepseek.com/"}})
    monkeypatch.setenv("SOULAGENT_WEB_CONFIG", str(web))
    monkeypatch.setenv("SOULAGENT_SECRETS", str(tmp_path / "nope" / "secrets.json"))

    config = llm.load_config()
    assert config["api_key"] == FAKE_API_KEY
    assert config["api_key_source"] == "web_config"
    assert config["base_url"] == "https://api.deepseek.com"


def test_llm_explicit_web_config_path_does_not_fall_back(tmp_path, monkeypatch):
    """显式指定的配置路径不存在时 fail-closed，不偷偷回退默认文件。"""
    monkeypatch.setenv("SOULAGENT_WEB_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setenv("SOULAGENT_SECRETS", str(tmp_path / "nope" / "secrets.json"))
    config = llm.load_config()
    assert config["api_key"] == ""
    assert config["api_key_source"] == "none"


def test_llm_empty_env_disables_key(tmp_path, monkeypatch):
    """显式空 env = 主动停用，不能再回落读磁盘上的旧 key。"""
    web = _write_json(tmp_path / "web_config.json", {"llm": {"api_key": FAKE_API_KEY}})
    monkeypatch.setenv("SOULAGENT_WEB_CONFIG", str(web))
    monkeypatch.setenv("SOULAGENT_SECRETS", str(tmp_path / "nope" / "secrets.json"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    config = llm.load_config()
    assert config["api_key"] == ""
    assert config["api_key_source"] == "env"


def test_llm_plaintext_warning_does_not_leak_key(tmp_path, monkeypatch, caplog):
    web = _write_json(tmp_path / "web_config.json", {"llm": {"api_key": FAKE_API_KEY}})
    monkeypatch.setenv("SOULAGENT_WEB_CONFIG", str(web))
    monkeypatch.setenv("SOULAGENT_SECRETS", str(tmp_path / "nope" / "secrets.json"))
    with caplog.at_level(logging.WARNING, logger="agi.selflearn.llm"):
        llm.load_config()
        llm.load_config()  # 第二次不应重复告警
    messages = [record.getMessage() for record in caplog.records]
    joined = "\n".join(messages)
    assert "明文" in joined and str(web) in joined
    assert FAKE_API_KEY not in joined
    assert len([m for m in messages if "明文" in m]) == 1


def test_llm_error_redacts_key_from_transport_exception():
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"

    def boom(_messages, _body):
        raise RuntimeError(f"upstream rejected header Authorization: Bearer {secret}")

    client = llm.Client({"api_key": secret, "model": "m"}, transport=boom)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result["ok"] is False
    assert secret not in result["error"]
    assert "sk-***" in result["error"]


def test_llm_client_with_tool_transport_error_redacts():
    secret = "sk-zyxwvutsrqponmlkjihgfedcba9876"

    def boom(_messages, _body):
        raise RuntimeError(f"bad key {secret}")

    client = llm.Client({"api_key": secret, "model": "m"}, transport=boom)
    result = client.chat_with_tools([{"role": "user", "content": "hi"}], [])
    assert result["ok"] is False
    assert secret not in result["error"]


def test_redact_helper_covers_sk_and_bearer():
    text = llm.redact("key=sk-0123456789abcdef token=Bearer abcdefghijklmnopqrst")
    assert "sk-0123456789abcdef" not in text
    assert "abcdefghijklmnopqrst" not in text
    assert "sk-***" in text


# --------------------------------------------------------------------------- #
# 2) agi/secrets_store.py
# --------------------------------------------------------------------------- #
def test_secrets_store_path_override_and_load(tmp_path, monkeypatch):
    path = _secrets_file(tmp_path, api_key=FAKE_API_KEY,
                         config={"db_keys": {"db_storage/a.db": {"key": FAKE_DB_KEY}}})
    monkeypatch.setenv("SOULAGENT_SECRETS", str(path))
    assert secrets_store.secrets_path() == path
    assert secrets_store.llm_api_key() == FAKE_API_KEY
    assert secrets_store.db_keys()["db_storage/a.db"]["key"] == FAKE_DB_KEY


def test_secrets_store_relative_path_resolves_under_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("SOULAGENT_SECRETS", "agi/data/__agentb_missing__.json")
    assert secrets_store.secrets_path() == ROOT / "agi" / "data" / "__agentb_missing__.json"


def test_secrets_store_corrupt_file_is_empty(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("SOULAGENT_SECRETS", str(bad))
    assert secrets_store.load() == {}
    assert secrets_store.llm_api_key() == ""


def test_secrets_store_describe_never_returns_values(tmp_path, monkeypatch):
    path = _secrets_file(tmp_path, api_key=FAKE_API_KEY,
                         config={"image_aes_key": FAKE_AES_KEY, "db_keys": {}})
    monkeypatch.setenv("SOULAGENT_SECRETS", str(path))
    described = secrets_store.describe()
    dumped = json.dumps(described, ensure_ascii=False)
    assert FAKE_API_KEY not in dumped and FAKE_AES_KEY not in dumped
    assert described["exists"] is True
    assert "api_key" in described["llm_fields"]


def test_secrets_store_write_is_idempotent(tmp_path, monkeypatch):
    path = _write_json(tmp_path / "agi" / "data" / "secrets.json", {"version": 1, "config": {}})
    monkeypatch.setenv("SOULAGENT_SECRETS", str(path))
    assert secrets_store.store_config_section({"db_keys": {"a": {"key": FAKE_DB_KEY}}}) is True
    first = path.read_text(encoding="utf-8")
    assert secrets_store.store_config_section({"db_keys": {"a": {"key": FAKE_DB_KEY}}}) is True
    assert path.read_text(encoding="utf-8") == first  # 内容一致就不动盘


def test_secrets_store_refuses_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SOULAGENT_SECRETS", str(tmp_path / "absent.json"))
    assert secrets_store.store_config_section({"db_keys": {"a": 1}}) is False


# --------------------------------------------------------------------------- #
# 3) reader 接入：wx_core/config.py
# --------------------------------------------------------------------------- #
def _wx_config(tmp_path, monkeypatch):
    from wx_core import config as wxcfg

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(wxcfg, "CONFIG_PATH", config_path)
    return wxcfg, config_path


def test_wxconfig_default_unchanged_without_secrets(tmp_path, monkeypatch):
    wxcfg, config_path = _wx_config(tmp_path, monkeypatch)
    monkeypatch.setenv("SOULAGENT_SECRETS", str(tmp_path / "nope" / "secrets.json"))
    _write_json(config_path, {"db_keys": {"db_storage/a.db": {"key": FAKE_DB_KEY}},
                              "image_aes_key": FAKE_AES_KEY})
    cfg = wxcfg.load()
    assert cfg["db_keys"]["db_storage/a.db"]["key"] == FAKE_DB_KEY
    assert cfg["image_aes_key"] == FAKE_AES_KEY


def test_wxconfig_reads_secrets_when_file_fields_empty(tmp_path, monkeypatch):
    wxcfg, config_path = _wx_config(tmp_path, monkeypatch)
    secrets_path = _secrets_file(
        tmp_path,
        config={"db_keys": {"db_storage/a.db": {"key": FAKE_DB_KEY}},
                "image_aes_key": FAKE_AES_KEY, "image_xor_key": "82"})
    monkeypatch.setenv("SOULAGENT_SECRETS", str(secrets_path))
    _write_json(config_path, {"db_keys": {}, "image_aes_key": ""})
    cfg = wxcfg.load()
    assert cfg["db_keys"]["db_storage/a.db"]["key"] == FAKE_DB_KEY
    assert cfg["image_aes_key"] == FAKE_AES_KEY
    assert str(cfg["image_xor_key"]) == "82"


def test_wxconfig_file_wins_over_secrets(tmp_path, monkeypatch):
    wxcfg, config_path = _wx_config(tmp_path, monkeypatch)
    secrets_path = _secrets_file(tmp_path, config={"image_aes_key": "0000000000000000"})
    monkeypatch.setenv("SOULAGENT_SECRETS", str(secrets_path))
    _write_json(config_path, {"image_aes_key": FAKE_AES_KEY})
    assert wxcfg.load()["image_aes_key"] == FAKE_AES_KEY


def test_wxconfig_save_routes_secrets_and_keeps_plaintext_clean(tmp_path, monkeypatch):
    """迁移后 monitor 保存配置时，敏感字段只进密钥库，不回写明文 config.json。"""
    wxcfg, config_path = _wx_config(tmp_path, monkeypatch)
    secrets_path = tmp_path / "agi" / "data" / "secrets.json"
    monkeypatch.setenv("SOULAGENT_SECRETS", str(secrets_path))
    _write_json(config_path, {"theme": "dark"})
    _write_json(secrets_path, {"version": 1, "config": {}})  # 先让密钥库存在

    cfg = wxcfg.load()
    cfg["db_keys"] = {"db_storage/a.db": {"key": FAKE_DB_KEY}}
    cfg["image_aes_key"] = FAKE_AES_KEY
    wxcfg.save(cfg)

    plaintext = config_path.read_text(encoding="utf-8")
    assert FAKE_DB_KEY not in plaintext and FAKE_AES_KEY not in plaintext
    saved = json.loads(plaintext)
    assert "db_keys" not in saved
    # 读取方仍然拿得到
    again = wxcfg.load()
    assert again["db_keys"]["db_storage/a.db"]["key"] == FAKE_DB_KEY
    assert again["image_aes_key"] == FAKE_AES_KEY


def test_wxconfig_save_without_secrets_keeps_legacy_behavior(tmp_path, monkeypatch):
    """没有密钥库（未迁移）时，save 仍把字段写回 config.json —— 默认行为不变。"""
    wxcfg, config_path = _wx_config(tmp_path, monkeypatch)
    monkeypatch.setenv("SOULAGENT_SECRETS", str(tmp_path / "nope" / "secrets.json"))
    _write_json(config_path, {"theme": "dark"})
    cfg = dict(wxcfg.DEFAULTS)
    cfg["db_keys"] = {"db_storage/a.db": {"key": FAKE_DB_KEY}}
    wxcfg.save(cfg)
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["db_keys"]["db_storage/a.db"]["key"] == FAKE_DB_KEY


# --------------------------------------------------------------------------- #
# 4) reader 接入：webui/llm.py + wx_core/web_search.py
# --------------------------------------------------------------------------- #
def test_webui_config_store_uses_secrets_when_file_empty(tmp_path, monkeypatch):
    from webui.llm import LLMConfigStore

    secrets_path = _secrets_file(tmp_path, api_key=FAKE_API_KEY)
    monkeypatch.setenv("SOULAGENT_SECRETS", str(secrets_path))
    store = LLMConfigStore(tmp_path / "web_config.json")
    loaded = store.load()
    assert loaded["api_key"] == FAKE_API_KEY
    public = store.public()
    assert public["configured"] is True
    assert FAKE_API_KEY not in json.dumps(public, ensure_ascii=False)


def test_webui_config_store_env_still_works_and_file_wins(tmp_path, monkeypatch):
    from webui.llm import LLMConfigStore

    monkeypatch.setenv("SOULAGENT_SECRETS", str(tmp_path / "nope" / "secrets.json"))
    store = LLMConfigStore(tmp_path / "web_config.json")
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_API_KEY)
    assert store.load()["api_key"] == FAKE_API_KEY  # 迁移前的 env 兜底保持不变

    _write_json(tmp_path / "web_config.json", {"llm": {"api_key": "sk-fromFile00000000000"}})
    assert store.load()["api_key"] == "sk-fromFile00000000000"  # 文件里的 key 优先


def test_web_search_credentials_precedence(tmp_path, monkeypatch):
    from wx_core import web_search

    monkeypatch.setattr(web_search, "CONFIG_PATH", tmp_path / "web_config.json")
    monkeypatch.setenv("SOULAGENT_SECRETS", str(tmp_path / "nope" / "secrets.json"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-envOnly000000000000")
    key, base = web_search._resolve_credentials()
    assert key == "sk-envOnly000000000000" and base == web_search.DEFAULT_BASE_URL

    secrets_path = _secrets_file(tmp_path, api_key=FAKE_API_KEY)
    monkeypatch.setenv("SOULAGENT_SECRETS", str(secrets_path))
    secrets_store.reset_cache()
    key, _ = web_search._resolve_credentials()
    assert key == FAKE_API_KEY  # 密钥库压过 env（迁移后的新位置）

    _write_json(tmp_path / "web_config.json", {"llm": {"api_key": "sk-file00000000000000",
                                                       "base_url": "https://x.example/"}})
    key, base = web_search._resolve_credentials()
    assert key == "sk-file00000000000000" and base == "https://x.example"


# --------------------------------------------------------------------------- #
# 5) tools/rotate_secrets.py
# --------------------------------------------------------------------------- #
def _migration_root(tmp_path: Path) -> Path:
    _write_json(tmp_path / "config.json", {
        "db_keys": {"db_storage/message/message_0.db": {"key": FAKE_DB_KEY, "salt": "ab"}},
        "db_keys_by_account": {"wxid_x": {"db_storage/a.db": {"key": FAKE_DB_KEY}}},
        "image_aes_key": FAKE_AES_KEY,
        "image_xor_key": 82,
        "image_cfg_dword": 123456,
        "theme": "dark",
    })
    _write_json(tmp_path / "web_config.json", {
        "llm": {"api_key": FAKE_API_KEY, "model": "deepseek-flash",
                "base_url": "https://api.deepseek.com"}})
    return tmp_path


@pytest.fixture()
def _no_acl(monkeypatch):
    """迁移测试不碰真实 ACL（Windows 上 icacls 会改 tmp 文件权限）。"""
    monkeypatch.setattr(rotate_secrets, "_tighten", lambda path: "ok(test)")


def test_rotate_dryrun_does_not_touch_files(tmp_path, monkeypatch, _no_acl):
    root = _migration_root(tmp_path)
    secrets_path = tmp_path / "agi" / "data" / "secrets.json"
    before_cfg = (root / "config.json").read_text(encoding="utf-8")
    before_web = (root / "web_config.json").read_text(encoding="utf-8")
    code = rotate_secrets.main(["--root", str(root), "--secrets", str(secrets_path)])
    assert code == 0
    assert not secrets_path.exists()
    assert (root / "config.json").read_text(encoding="utf-8") == before_cfg
    assert (root / "web_config.json").read_text(encoding="utf-8") == before_web


def test_rotate_apply_copies_but_keeps_plaintext(tmp_path, monkeypatch, _no_acl):
    root = _migration_root(tmp_path)
    secrets_path = tmp_path / "agi" / "data" / "secrets.json"
    code = rotate_secrets.main(["--root", str(root), "--secrets", str(secrets_path), "--apply"])
    assert code == 0
    payload = json.loads(secrets_path.read_text(encoding="utf-8"))
    assert payload["llm"]["api_key"] == FAKE_API_KEY
    assert payload["config"]["db_keys"]["db_storage/message/message_0.db"]["key"] == FAKE_DB_KEY
    assert payload["config"]["image_aes_key"] == FAKE_AES_KEY
    # 默认不清空旧文件：生产进程还在跑
    assert json.loads((root / "config.json").read_text(encoding="utf-8"))["image_aes_key"] == FAKE_AES_KEY
    assert json.loads((root / "web_config.json").read_text(encoding="utf-8"))["llm"]["api_key"] == FAKE_API_KEY


def test_rotate_purge_clears_plaintext_and_reader_still_works(tmp_path, monkeypatch, _no_acl):
    root = _migration_root(tmp_path)
    secrets_path = tmp_path / "agi" / "data" / "secrets.json"
    code = rotate_secrets.main(["--root", str(root), "--secrets", str(secrets_path),
                                "--apply", "--purge-plaintext"])
    assert code == 0
    cfg_after = json.loads((root / "config.json").read_text(encoding="utf-8"))
    web_after = json.loads((root / "web_config.json").read_text(encoding="utf-8"))
    assert cfg_after["db_keys"] == {} and cfg_after["image_aes_key"] == ""
    assert web_after["llm"]["api_key"] == ""
    assert cfg_after["theme"] == "dark"  # 非敏感字段原样保留

    # 迁移后读取方（wx_core.config）仍能拿到 key
    from wx_core import config as wxcfg
    monkeypatch.setattr(wxcfg, "CONFIG_PATH", root / "config.json")
    monkeypatch.setenv("SOULAGENT_SECRETS", str(secrets_path))
    cfg = wxcfg.load()
    assert cfg["db_keys"]["db_storage/message/message_0.db"]["key"] == FAKE_DB_KEY
    assert cfg["image_aes_key"] == FAKE_AES_KEY

    # llm.py 也能从密钥库读
    monkeypatch.setenv("SOULAGENT_WEB_CONFIG", str(root / "web_config.json"))
    assert llm.load_config()["api_key"] == FAKE_API_KEY


def test_rotate_apply_is_idempotent(tmp_path, _no_acl):
    root = _migration_root(tmp_path)
    secrets_path = tmp_path / "agi" / "data" / "secrets.json"
    assert rotate_secrets.main(["--root", str(root), "--secrets", str(secrets_path), "--apply"]) == 0
    first = secrets_path.read_text(encoding="utf-8")
    assert rotate_secrets.main(["--root", str(root), "--secrets", str(secrets_path), "--apply"]) == 0
    assert secrets_path.read_text(encoding="utf-8") == first


def test_rotate_purge_requires_apply(tmp_path, capsys):
    root = _migration_root(tmp_path)
    code = rotate_secrets.main(["--root", str(root), "--purge-plaintext"])
    assert code == 2
    assert "必须和 --apply" in capsys.readouterr().err


def test_rotate_purge_refuses_unconfirmed_values(tmp_path, monkeypatch, _no_acl):
    """密钥库里没有确认过的副本时，宁可保留明文也不清空（避免丢密钥）。"""
    root = _migration_root(tmp_path)
    secrets_path = tmp_path / "agi" / "data" / "secrets.json"
    monkeypatch.setattr(rotate_secrets, "_confirmed", lambda payload, entry: False)
    code = rotate_secrets.main(["--root", str(root), "--secrets", str(secrets_path),
                                "--apply", "--purge-plaintext"])
    assert code == 3
    assert json.loads((root / "config.json").read_text(encoding="utf-8"))["image_aes_key"] == FAKE_AES_KEY


def test_rotate_write_failure_keeps_plaintext(tmp_path, monkeypatch):
    root = _migration_root(tmp_path)
    secrets_path = tmp_path / "agi" / "data" / "secrets.json"
    monkeypatch.setattr(rotate_secrets, "_atomic_write_json", lambda path, data: False)
    before = (root / "config.json").read_text(encoding="utf-8")
    code = rotate_secrets.main(["--root", str(root), "--secrets", str(secrets_path), "--apply"])
    assert code == 1
    assert (root / "config.json").read_text(encoding="utf-8") == before
    assert not secrets_path.exists()


def test_rotate_dry_run_flag_is_explicit_and_exclusive(tmp_path, capsys):
    root = _migration_root(tmp_path)
    assert rotate_secrets.main(["--root", str(root), "--dry-run"]) == 0
    assert rotate_secrets.main(["--root", str(root), "--dry-run", "--apply"]) == 2
    assert "互斥" in capsys.readouterr().err


def test_rotate_no_secret_fields_is_noop(tmp_path, _no_acl):
    root = tmp_path / "clean"
    _write_json(root / "config.json", {"theme": "dark"})
    _write_json(root / "web_config.json", {"llm": {"api_key": ""}})
    secrets_path = tmp_path / "agi" / "data" / "secrets.json"
    assert rotate_secrets.main(["--root", str(root), "--secrets", str(secrets_path), "--apply"]) == 0
    assert not secrets_path.exists()


# --------------------------------------------------------------------------- #
# 6) tools/prune_chat_logs.py
# --------------------------------------------------------------------------- #
OLD = 40  # 天
NOW = time.time()
CUTOFF_TS = NOW - 40 * 86400


def _make_prune_tree(tmp_path: Path, *, chat_text_age_days: float = 45) -> Path:
    root = tmp_path / "repo"
    log_dir = root / "log"
    media_dir = root / "media"
    log_dir.mkdir(parents=True)
    media_dir.mkdir(parents=True)

    lines = [
        {"ts": NOW - 50 * 86400, "text": "很久以前的旧消息"},
        {"ts": NOW - 2 * 86400, "text": "昨天的新消息"},
        {"text": "没有时间戳但也要保留"},
    ]
    (log_dir / "messages.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in lines),
        encoding="utf-8")

    chat = log_dir / "聊天记录.txt"
    chat.write_text("好友A: 你好呀\n我: 你好\n", encoding="utf-8")
    old_stamp = NOW - chat_text_age_days * 86400
    os.utime(chat, (old_stamp, old_stamp))

    old_media = media_dir / "old.png"
    new_media = media_dir / "new.png"
    old_media.write_bytes(b"old")
    new_media.write_bytes(b"new")
    os.utime(old_media, (CUTOFF_TS - 86400, CUTOFF_TS - 86400))
    os.utime(new_media, (NOW, NOW))

    (root / "agi" / "data" / "desktop_profile").mkdir(parents=True)
    return root


def test_prune_dryrun_does_not_change_files(tmp_path, capsys):
    root = _make_prune_tree(tmp_path)
    before = (root / "log" / "messages.jsonl").read_text(encoding="utf-8")
    code = prune_chat_logs.main(["--root", str(root), "--days", "30"])
    assert code == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "超期 1 行" in out
    assert (root / "log" / "messages.jsonl").read_text(encoding="utf-8") == before
    assert (root / "log" / "聊天记录.txt").exists()
    assert (root / "media" / "old.png").exists()


def test_prune_apply_removes_expired_and_keeps_rest(tmp_path, capsys):
    root = _make_prune_tree(tmp_path)
    code = prune_chat_logs.main(["--root", str(root), "--days", "30", "--apply"])
    assert code == 0
    remaining = [json.loads(line) for line in
                 (root / "log" / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
    texts = [item["text"] for item in remaining]
    assert "很久以前的旧消息" not in texts
    assert "昨天的新消息" in texts and "没有时间戳但也要保留" in texts
    assert not (root / "log" / "聊天记录.txt").exists()  # 整文件超期
    assert not (root / "media" / "old.png").exists()
    assert (root / "media" / "new.png").exists()


def test_prune_keeps_recent_chat_text(tmp_path):
    root = _make_prune_tree(tmp_path, chat_text_age_days=1)
    assert prune_chat_logs.main(["--root", str(root), "--days", "30", "--apply"]) == 0
    assert (root / "log" / "聊天记录.txt").exists()
    assert "好友A" in (root / "log" / "聊天记录.txt").read_text(encoding="utf-8")


def test_prune_rejects_zero_days(tmp_path, capsys):
    root = _make_prune_tree(tmp_path)
    assert prune_chat_logs.main(["--root", str(root), "--days", "0"]) == 2
    assert "必须 >= 1" in capsys.readouterr().err


def test_prune_dry_run_flag_is_explicit_and_exclusive(tmp_path, capsys):
    root = _make_prune_tree(tmp_path)
    assert prune_chat_logs.main(["--root", str(root), "--dry-run"]) == 0
    assert prune_chat_logs.main(["--root", str(root), "--dry-run", "--apply"]) == 2
    assert "互斥" in capsys.readouterr().err


def test_prune_reports_desktop_profile_note(tmp_path):
    root = _make_prune_tree(tmp_path)
    note = prune_chat_logs.desktop_profile_note(root)
    assert note and "desktop_profile" in note


def test_prune_missing_dirs_are_skipped(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    assert prune_chat_logs.main(["--root", str(root), "--apply"]) == 0


# --------------------------------------------------------------------------- #
# 7) tools/check_secrets.py：能扫出真密钥，不误报占位符
# --------------------------------------------------------------------------- #
def _hits(line: str) -> list[str]:
    found = []
    for name, pattern in check_secrets.RULES:
        if pattern.search(line) and not check_secrets._allowed(line, name):
            found.append(name)
    return found


def test_check_secrets_detects_sk_and_db_keys():
    assert "api-key(sk-)" in _hits(f'api_key = "{FAKE_API_KEY}"')
    assert "wcdb-key(db_keys)" in _hits(f'  "key": "{FAKE_DB_KEY}",')
    assert "image-aes-key" in _hits(f'"image_aes_key": "{FAKE_AES_KEY}"')
    assert "bearer-token" in _hits("Authorization: Bearer Q2hhbmdlTWVQbGVhc2VUb2tlbjEyMzQ1Ng")


def test_check_secrets_ignores_placeholders():
    assert _hits('"image_aes_key": "0123456789abcdef"') == []
    assert _hits(f'  "key": "{"a" * 64}",') == []
    assert _hits('api_key = "sk-test-placeholder"') == []


def test_check_secrets_masks_findings():
    masked = check_secrets._mask(FAKE_API_KEY)
    assert FAKE_API_KEY not in masked
    assert masked.startswith("sk-Age")
    assert masked.endswith("0123")


# --------------------------------------------------------------------------- #
# 8) tools/harden_acl.py：TARGETS + --verify
# --------------------------------------------------------------------------- #
UNTRUSTED_ACL = (
    "C:\\data\\config.json NT AUTHORITY\\Authenticated Users:(OI)(CI)(I)(M)\n"
    "                       WIN-X\\bob:(F)\n"
    "                       NT AUTHORITY\\SYSTEM:(F)"
)
CLEAN_ACL = (
    "C:\\data\\config.json WIN-X\\bob:(F)\n"
    "                    NT AUTHORITY\\SYSTEM:(F)\n"
    "                    BUILTIN\\Administrators:(F)"
)


def test_harden_targets_cover_sensitive_paths():
    for rel in ("config.json", "web_config.json", "log", "media", "agi/data",
                "agi/data/desktop_profile", ".data", "star"):
        assert rel in harden_acl.TARGETS


def test_harden_detects_authenticated_users():
    target = Path("C:\\data\\config.json")
    entries = harden_acl.untrusted_entries(UNTRUSTED_ACL, target)
    assert [item["principal"] for item in entries] == ["NT AUTHORITY\\Authenticated Users"]
    assert harden_acl.untrusted_entries(CLEAN_ACL, target) == []


def test_harden_trusted_administrator_is_not_untrusted():
    """`Administrator` 里含 `Users` 子串这件事不能让判定反了。"""
    assert harden_acl.untrusted_entries(r"WIN\Administrator:(F)") == []
    assert harden_acl.untrusted_entries(r"BUILTIN\Users:(F)") != []


def test_harden_parse_flags_inheritance():
    parsed = harden_acl.parse_icacls(UNTRUSTED_ACL)
    assert parsed["inherited"] is True
    assert any(item["inherited"] for item in parsed["entries"])


def test_harden_verify_fails_on_untrusted(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(harden_acl, "is_windows", lambda: True)
    monkeypatch.setattr(harden_acl, "current_user_sid", lambda: "S-1-5-21-9")
    monkeypatch.setattr(harden_acl, "ROOT", tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(harden_acl, "run_icacls", lambda *a, **k: (0, UNTRUSTED_ACL))
    calls: list[list[str]] = []
    original = harden_acl.run_icacls
    monkeypatch.setattr(harden_acl, "run_icacls",
                        lambda args, **kw: (calls.append(list(args)), (0, UNTRUSTED_ACL))[1])
    assert harden_acl.main(["--verify"]) == 1
    out = capsys.readouterr().out
    assert "Authenticated Users" in out
    assert all("/inheritance:r" not in " ".join(call) for call in calls)  # 只读
    monkeypatch.setattr(harden_acl, "run_icacls", original)


def test_harden_detects_sid_form_and_unknown_writers():
    # 中文系统/沙箱环境里 icacls 可能把 Authenticated Users 显示成裸 SID
    allow = {"S-1-5-18", "S-1-5-32-544", "S-1-5-21-9"}
    assert harden_acl._principal_kind("S-1-5-11", allow) == "untrusted"
    assert harden_acl._principal_kind("S-1-1-0", allow) == "untrusted"
    assert harden_acl._principal_kind("S-1-5-21-9", allow) == "trusted"
    assert harden_acl._principal_kind("S-1-4-123-456", allow) == "unknown"
    # 白名单之外但可写 → 风险；只读（RX）不算
    unsafe = 'C:\\x S-1-4-123-456:(I)(W,D,DC)'
    risky = harden_acl.unknown_write_entries(unsafe, Path("C:\\x"))
    assert [item["principal"] for item in risky] == ["S-1-4-123-456"]
    read_only = 'C:\\x S-1-4-123-456:(I)(RX)'
    assert harden_acl.unknown_write_entries(read_only, Path("C:\\x")) == []


def test_harden_verify_passes_when_clean(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(harden_acl, "is_windows", lambda: True)
    monkeypatch.setattr(harden_acl, "current_user_sid", lambda: "S-1-5-21-9")
    monkeypatch.setattr(harden_acl, "current_user_name", lambda: "WIN-X\\bob")
    monkeypatch.setattr(harden_acl, "ROOT", tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(harden_acl, "run_icacls", lambda *a, **k: (0, CLEAN_ACL))
    assert harden_acl.main(["--verify"]) == 0
    assert "[PASS]" in capsys.readouterr().out


def test_harden_apply_grants_oi_ci_on_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(harden_acl, "allowed_sids", lambda: ["S-1-5-18"])
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    assert "*S-1-5-18:(OI)(CI)F" in " ".join(harden_acl.icacls_apply_args(log_dir))
    assert "*S-1-5-18:F" in " ".join(harden_acl.icacls_apply_args(config_file))


# --------------------------------------------------------------------------- #
# 9) .gitignore / requirements 的底线
# --------------------------------------------------------------------------- #
def test_gitignore_covers_local_secrets_and_residue():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("config.json", "web_config.json", "log/", "media/", "agi/data/",
                    ".data/", "pip-*", "star/*", "agi/data/secrets.json",
                    "agi/data/desktop_profile/"):
        assert pattern in text, pattern


def test_requirements_has_no_hardcoded_interpreter_path():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "Python314" not in text
    assert "python -m pip install -r requirements.txt" in text
    assert "pycryptodome>=3.20,<5" in text