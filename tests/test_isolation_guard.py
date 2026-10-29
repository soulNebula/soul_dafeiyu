# -*- coding: utf-8 -*-
"""测试隔离的可证伪断言：隔离没生效时这里必须先红。

本文件回答两个问题（不是「跑绿就行」，而是「别人怎么验证我没说谎」）：
1. **路径真的搬到仓库外了吗**——直接断言 ``agi``/``wx_core`` 侧的数据、日志、
   配置文件路径都不在 ``REPO_ROOT`` 下。路径写回仓库 = 测试会写生产。
2. **守卫本身是真的吗**——用 tmp 文件反向验证 ``(mtime_ns, size)`` 快照必须能发现
   改写/新建；否则「跑完守卫没报」只是一句空话。

另外锁住「无 env 时默认值与历史一致」（生产行为零变化的验收口径）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _inside_repo(path: Path | str) -> bool:
    try:
        Path(path).resolve().relative_to(REPO_ROOT.resolve())
    except (ValueError, OSError):
        return False
    return True


# --------------------------------------------------------------------------- #
# 1. 路径隔离：数据/日志/配置都不许落在仓库目录下
# --------------------------------------------------------------------------- #
def test_gateway_data_dir_is_redirected_out_of_repo():
    from agi.wechat_gateway import config as gw_config

    for path in (gw_config.DATA_DIR, gw_config.CONFIG_PATH, gw_config.CONTACT_DB_DIR):
        assert not _inside_repo(path), f"网关路径仍指向仓库内（测试会写生产）：{path}"


def test_selflearn_and_paths_are_redirected_out_of_repo():
    from agi import paths
    from agi.selflearn import config as sl_config

    for path in (paths.data_dir(), paths.log_dir(), paths.config_path(),
                 paths.web_config_path(), sl_config.DATA_DIR, sl_config.DB_PATH,
                 sl_config.WEB_CONFIG):
        assert not _inside_repo(path), f"路径仍指向仓库内（测试会写生产）：{path}"
    # 语义不能丢：日志根名字仍是 log，自学习库仍是 agi/data/selflearn 布局
    assert paths.log_dir().name == "log"
    assert sl_config.DATA_DIR.name == "selflearn"


def test_message_log_is_redirected_out_of_repo():
    from agi.soul import dataset

    assert not _inside_repo(dataset.MESSAGE_LOG), dataset.MESSAGE_LOG


def test_host_config_constants_are_redirected_out_of_repo():
    """根 conftest 把 wx_core 的两个 import 时常量也改到 tmp（双保险）。"""
    from wx_core import config as wx_config
    from wx_core import web_search

    assert not _inside_repo(wx_config.CONFIG_PATH), wx_config.CONFIG_PATH
    assert not _inside_repo(web_search.CONFIG_PATH), web_search.CONFIG_PATH


def test_env_overrides_are_honored(monkeypatch, tmp_path):
    """环境变量优先级：SOULAGENT_DATA_DIR / SOULAGENT_LOG_DIR 立刻生效。"""
    from agi import paths

    monkeypatch.setenv("SOULAGENT_DATA_DIR", str(tmp_path / "data_x"))
    monkeypatch.setenv("SOULAGENT_LOG_DIR", str(tmp_path / "log_x"))
    assert paths.data_dir() == tmp_path / "data_x"
    assert paths.log_dir() == tmp_path / "log_x"


def test_default_paths_without_env_unchanged(monkeypatch):
    """不带 env 时默认值必须与历史逐字节一致（生产行为零变化的验收口径）。"""
    from agi import paths

    for name in ("SOULAGENT_DATA_DIR", "SOULAGENT_LOG_DIR",
                 "SOULAGENT_CONFIG_PATH", "SOULAGENT_WEB_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    assert paths.data_dir() == REPO_ROOT / "agi" / "data"
    assert paths.log_dir() == REPO_ROOT / "log"
    assert paths.config_path() == REPO_ROOT / "config.json"
    assert paths.web_config_path() == REPO_ROOT / "web_config.json"


# --------------------------------------------------------------------------- #
# 2. 守卫自证：快照必须能发现「被改写 / 被创建」
# --------------------------------------------------------------------------- #
def test_guard_fixture_is_active(production_guard_api):
    """autouse 的 session 守卫已挂上（fixture 在 fixturenames 里）且监视清单非空。"""
    patterns = production_guard_api["patterns"]()
    assert "config.json" in patterns
    assert "web_config.json" in patterns
    assert any("log/messages.jsonl" == p for p in patterns)
    assert any(p.startswith("agi/data/wechat/") for p in patterns)
    assert any("selflearn" in p and "sqlite3" in p for p in patterns)
    # 精确路径即使当前不存在也必须入列（被创建 = 隔离失效）
    targets = {str(p).replace("\\", "/") for p in production_guard_api["targets"]()}
    assert any(t.endswith("config.json") for t in targets)


def test_guard_detects_rewrite(tmp_path, production_guard_api):
    target = tmp_path / "sensitive.json"
    target.write_text("{}", encoding="utf-8")
    before = production_guard_api["fingerprint"]([target])
    target.write_text('{"changed": true}', encoding="utf-8")
    after = production_guard_api["fingerprint"]([target])
    changes = production_guard_api["diff"](before, after)
    assert changes, "守卫必须能发现生产文件被改写（否则守卫是假的）"
    assert "[被改写]" in changes[0]


def test_guard_detects_creation(tmp_path, production_guard_api):
    target = tmp_path / "should_not_appear.json"
    before = production_guard_api["fingerprint"]([target])
    assert before[str(target)] is None
    target.write_text("{}", encoding="utf-8")
    changes = production_guard_api["diff"](before, production_guard_api["fingerprint"]([target]))
    assert changes and "[被创建]" in changes[0]


def test_guard_snapshot_is_mtime_and_size(production_guard_api):
    """快照口径 = (mtime_ns, size)，与任务验收要求一致。"""
    existing = [p for p in production_guard_api["targets"]() if p.is_file()]
    snap = production_guard_api["fingerprint"](existing)
    for path in existing:
        stat = path.stat()
        assert snap[str(path)] == [stat.st_mtime_ns, stat.st_size]


def test_ci_flag_parsing(monkeypatch, ci_policy):
    """SOULAGENT_CI 的解析口径：只有 1/true/yes/on 视为 CI（skip 规则依赖它）。"""
    skip_windows_only = ci_policy["skip_windows_only"]
    monkeypatch.setenv("SOULAGENT_CI", "1")
    assert skip_windows_only() is True
    monkeypatch.setenv("SOULAGENT_CI", "0")
    assert skip_windows_only() is (not ci_policy["is_windows"])


# --------------------------------------------------------------------------- #
# 3. 运行时算出来的默认路径也必须被 env 重定向（P0-24 扩展）
# --------------------------------------------------------------------------- #
def test_runtime_data_constants_redirected_out_of_repo():
    """import 时写死的那些 `agi/data/...` 常量也必须走 data_dir()。

    这些模块的常量是「函数默认参数」的来源（inner.log / prompts.json / llm_usage.jsonl /
    web_cache.json / media / diary / narrative ...）。只重定向 agi.paths 而不改它们，
    测试仍会把文件写进 worktree 的真实 `agi/data`——之前 inner.log 就是这么漏的。
    """
    from agi import media_acquisition, secrets_store
    from agi.inner import inject
    from agi.integrations import web_lookup
    from agi.llm import router, usage
    from agi.selflearn import (diary, media_choice, prompt_store,
                               self_narrative, simple_tick, topic_seed)
    from agi.wechat_gateway import media as gw_media

    constants = {
        "inject.INNER_LOG_PATH": inject.INNER_LOG_PATH,
        "prompt_store.PROMPTS_PATH": prompt_store.PROMPTS_PATH,
        "prompt_store.PROMPT_OVERRIDE_LOG_PATH": prompt_store.PROMPT_OVERRIDE_LOG_PATH,
        "router.DEFAULT_CONFIG_PATH": router.DEFAULT_CONFIG_PATH,
        "router.DEFAULT_USAGE_PATH": router.DEFAULT_USAGE_PATH,
        "usage.DEFAULT_USAGE_PATH": usage.DEFAULT_USAGE_PATH,
        "web_lookup.DEFAULT_CACHE_PATH": web_lookup.DEFAULT_CACHE_PATH,
        "media_acquisition.WISH_FILE": media_acquisition.WISH_FILE,
        "media_acquisition.IMPORT_DIR": media_acquisition.IMPORT_DIR,
        "media_acquisition.MEDIA_SOURCES_PATH": media_acquisition.MEDIA_SOURCES_PATH,
        "media_acquisition.WISH_STATUS_FILE": media_acquisition.WISH_STATUS_FILE,
        "diary.GATEWAY_LOG": diary.GATEWAY_LOG,
        "diary.WECHAT_CONFIG": diary.WECHAT_CONFIG,
        "media_choice.TAGS_PATH": media_choice.TAGS_PATH,
        "self_narrative.DEFAULT_DIARY_DIR": self_narrative.DEFAULT_DIARY_DIR,
        "self_narrative.DEFAULT_HISTORY_PATH": self_narrative.DEFAULT_HISTORY_PATH,
        "self_narrative.DEFAULT_DAILY_CORPUS_DIR": self_narrative.DEFAULT_DAILY_CORPUS_DIR,
        "simple_tick.WECHAT_CONFIG": simple_tick.WECHAT_CONFIG,
        "simple_tick.GATEWAY_LOG": simple_tick.GATEWAY_LOG,
        "simple_tick.VIDEO_MEDIA_DIR": simple_tick.VIDEO_MEDIA_DIR,
        "topic_seed.WECHAT_CONFIG": topic_seed.WECHAT_CONFIG,
        "gw_media.INBOUND_MEDIA_DIR": gw_media.INBOUND_MEDIA_DIR,
        "secrets_store.DEFAULT_SECRETS_PATH": secrets_store.DEFAULT_SECRETS_PATH,
    }
    offenders = [f"{name}={path}" for name, path in constants.items() if _inside_repo(path)]
    assert not offenders, "这些运行时常量没接 SOULAGENT_DATA_DIR（测试会写真生产）：\n" + "\n".join(offenders)


def test_no_hardcoded_agi_data_literal_in_agi_code():
    """静态防回归：agi/ 非测试代码里不许再出现 `"agi" / "data"` 硬编码。

    env 重定向只能覆盖「调用 data_dir() 的地方」；写死字面量的模块永远绕过隔离，
    所以这里做一次静态断言。白名单里的都是**只读本机数据**或本轮约定不改的模块，
    每条都写明原因（新增白名单必须同时说明为什么不是写路径）。
    """
    import re

    #: 白名单：路径相对仓库根 → 原因
    allowlist = {
        "agi/paths.py": "这里是 data_dir() 的默认值定义本身（唯一合法出处）",
        "agi/selflearn/model_registry.py": "本机模型注册表只读部署配置；测试按文件存在与否 skipif，不写",
        "agi/selflearn/validate_p6c.py": "真机验证脚本的只读上下文库；测试缺文件即 skip，不写",
        "agi/selflearn/experiment_p6d.py": "真机实验脚本的只读上下文库；不进 pytest 默认路径",
        "agi/wechat_gateway/server.py": "send-core 已定稿范围，本轮不动（写入路径已由守卫覆盖）",
        "agi/wechat_gateway/sender.py": "send-core 已定稿范围，本轮不动（写入路径已由守卫覆盖）",
        "agi/wechat_gateway/drafts.py": "send-core 已定稿范围，本轮不动（写入路径已由守卫覆盖）",
        "agi/bot_runtime.py": "全局 Bot 控制面指针（active_bot.json），必须固定在仓库 agi/data，不随 Bot 数据根重定向",
    }
    pattern = re.compile(r"""["']agi["']\s*/\s*["']data["']""")
    offenders: list[str] = []
    for path in sorted(REPO_ROOT.joinpath("agi").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "__pycache__" in path.parts or "/tests/" in "/" + rel:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line) and rel not in allowlist:
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, ("agi/ 非测试代码里还有写死的 agi/data 字面量（绕过 env 隔离）：\n"
                           + "\n".join(offenders))


def test_guard_patterns_cover_runtime_writes(production_guard_api):
    """守卫监视清单必须覆盖「测试可能写到的」其它本机数据（inner.log/llm_usage/...）。"""
    patterns = set(production_guard_api["patterns"]())
    for needed in ("agi/data/selflearn/*.log",
                   "agi/data/selflearn/*.jsonl",
                   "agi/data/llm_usage.jsonl",
                   "agi/data/prompts.json",
                   "agi/data/secrets.json",
                   "agi/data/diary/*"):
        assert needed in patterns, f"守卫清单缺 {needed}（写到这里不会被发现）"