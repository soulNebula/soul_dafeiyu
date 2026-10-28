# -*- coding: utf-8 -*-
"""pytest 全局隔离：任何测试都不许写/读真实数据目录。

背景（2026-10-24）：
- `agi.wechat_gateway.config.contact_db("好友A")` 的 sha1 前 14 位是 043e7e3743523b，
  刚好和真实数据目录里的一个联系人库同名；
- `test_proactive_worker.py` 里部分用例直接调 `run_cycle()`，没有替换 contact_db，
  于是把测试数据写进了 `agi/data/selflearn/wechat_contacts/`。
- 这里用 autouse fixture 把 contact_db 重定向到 tmp_path，从根上堵住。

背景（2026-10-25，P0-22b）：
- 运维在机上一个 `touch agi/data/wechat/STOP`（紧急停止开关）就让
  `test_wechat_draft_flow.py` 4 条全挂——测试里的 Gateway 读的是**生产 STOP 路径**，
  被真实文件拦成 `blocked`。生产文件的存在不应该影响任何测试。
- 这里再用一个 autouse fixture 把 STOP 路径重定向到 `tmp_path/"STOP"`；
  如果测试自己把 `CONFIG_PATH` 指到 tmp（如 `test_config_hot_reload` 的 `cfg_path`），
  STOP 仍跟着 `config.json` 同目录走，保持"开关与配置同目录"的语义。
  生产数据目录（`config.DATA_DIR`）下的 STOP 路径永远不会被读到。

注意：生产代码里的 `db.connect(path=DB_PATH)` 默认参数在 import 时已绑定，
本 fixture 只负责 `contact_db` / `stop_path` 这些运行时计算路径的入口；
测试仍应显式传 tmp_path。

P0-24（测试隔离与可证伪）补充：
- 本文件在 **模块顶层** 先把 `SOULAGENT_DATA_DIR/LOG_DIR/CONFIG_PATH/WEB_CONFIG`
  指到会话级临时目录，再让任何 agi 模块 import——否则 `agi.selflearn.config.DB_PATH`
  这类「import 时求值的常量」和 `db.connect(path=DB_PATH)` 的默认参数会永久绑定
  生产 `agi/data`，fixture 之后怎么 patch 都改不回来（`test_proactive_worker` 把
  测试数据写进 `agi/data/selflearn/wechat_contacts/` 就是这个坑）。
- 会话结束做「生产文件 mtime+size 快照比对」，有变化就 fail：测试不写生产是断言，
  不是口号。守卫有效期/实现由根 `tests/conftest.py` 与 `tests/test_isolation_guard.py` 共同覆盖。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

#: 仓库根（本文件在 <repo>/agi/tests/conftest.py）
REPO_ROOT = Path(__file__).resolve().parents[2]
#: 会话级临时根：与根 tests/conftest.py 共用同一个（谁先加载都一致）
TEST_ROOT_ENV = "SOULAGENT_TEST_ROOT"
CI_ENV = "SOULAGENT_CI"
#: 生产敏感文件（相对仓库根）；精确路径不存在也监视（被创建 = 隔离失效）
GUARD_PATTERNS: tuple[str, ...] = (
    "config.json",
    "web_config.json",
    "agi/data/wechat/config.json",
    "agi/data/wechat/drafts.jsonl",
    "agi/data/wechat/gateway.log",
    "agi/data/wechat/pending_lost.jsonl",
    "agi/data/wechat/STOP",
    "log/messages.jsonl",
    "log/聊天记录.txt",
    "agi/data/selflearn/selflearn.sqlite3",
    "agi/data/selflearn/*.sqlite3",
    "agi/data/selflearn/*.sqlite3-*",
    "agi/data/selflearn/wechat_contacts/*.sqlite3",
    "agi/data/media_tags.json",
    "agi/data/web_cache.json",
    "agi/data/console_audit.log",
    "agi/data/media_wishes.jsonl",
    "agi/data/media_sources.json",
    "agi/data/media_wishes_status.json",
    # P0-24 扩展：测试能通过「运行时算出来的默认路径」写到的其它本机数据
    "agi/data/prompts.json",
    "agi/data/llm_router.json",
    "agi/data/llm_usage.jsonl",
    "agi/data/local_models_state.json",
    "agi/data/secrets.json",
    "agi/data/diary/*",
    "agi/data/selflearn/*.log",
    "agi/data/selflearn/*.jsonl",
    "agi/data/selflearn/*.json",
)


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def _bootstrap_test_root() -> Path:
    """把路径类环境变量钉到会话临时目录；返回该临时根。

    为什么必须在 conftest import 阶段做：`agi.selflearn.config` / `agi.wechat_gateway.config`
    等模块的常量与函数默认参数在 import 时就求值，错过这个窗口就只能改生产路径。
    """
    existing = str(os.environ.get(TEST_ROOT_ENV) or "").strip()
    if existing and Path(existing).is_dir():
        root = Path(existing)
    else:
        root = Path(tempfile.mkdtemp(prefix="soulagent-tests-"))
        os.environ[TEST_ROOT_ENV] = str(root)

    mapping = {
        "SOULAGENT_DATA_DIR": root / "agi" / "data",
        "SOULAGENT_LOG_DIR": root / "log",
        "SOULAGENT_CONFIG_PATH": root / "config.json",
        "SOULAGENT_WEB_CONFIG": root / "web_config.json",
    }
    for name, value in mapping.items():
        os.environ[name] = str(value)
        value.parent.mkdir(parents=True, exist_ok=True)
    (root / "agi" / "data").mkdir(parents=True, exist_ok=True)
    (root / "log").mkdir(parents=True, exist_ok=True)
    return root


#: 本会话的隔离根（先于任何 agi import 建好）
TEST_ROOT = _bootstrap_test_root()


# --------------------------------------------------------------------------- #
# 生产写盘自证守卫（与根 tests/conftest.py 同一口径）
# --------------------------------------------------------------------------- #
def guard_targets(root: Path | str = REPO_ROOT) -> list[Path]:
    """展开监视清单：精确路径永远入列（不存在记 None），通配符按当前文件系统展开。"""
    base = Path(root)
    targets: dict[str, Path] = {}
    for pattern in GUARD_PATTERNS:
        if any(ch in pattern for ch in "*?["):
            for path in sorted(base.glob(pattern)):
                targets[str(path)] = path
        else:
            # 精确路径：即使当前不存在也要入列（被创建 = 隔离失效，靠指纹 None→有值发现）
            exact = base / pattern
            targets[str(exact)] = exact
    return sorted(targets.values())


def fingerprint(paths: list[Path]) -> dict[str, list[int] | None]:
    """``{路径: [mtime_ns, size] | None}``；不存在记 None（后续被创建也能发现）。"""
    snap: dict[str, list[int] | None] = {}
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            snap[str(path)] = None
        else:
            snap[str(path)] = [int(stat.st_mtime_ns), int(stat.st_size)]
    return snap


def diff_snapshots(before: dict, after: dict) -> list[str]:
    changes: list[str] = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old == new:
            continue
        if old is None and new is not None:
            changes.append(f"[被创建] {key}（测试不该在生产默认路径下建文件）")
        elif old is not None and new is None:
            changes.append(f"[被删除] {key}（快照 {old}）")
        else:
            changes.append(f"[被改写] {key}  before={old} after={new}")
    return changes


@pytest.fixture(scope="session", autouse=True)
def _production_write_guard():
    """会话开始/结束比对生产敏感文件；有变化就 fail —— 测试不写生产的动态自证。"""
    before = fingerprint(guard_targets(REPO_ROOT))
    yield
    changes = diff_snapshots(before, fingerprint(guard_targets(REPO_ROOT)))
    if changes:
        pytest.fail(
            "测试写到了生产默认路径——说明隔离失效，请修隔离，不要关守卫：\n  "
            + "\n  ".join(changes)
        )


# --------------------------------------------------------------------------- #
# Windows-only 用例：CI / 非 Windows 统一 skip（精确到 marker，不做整文件 ignore）
# --------------------------------------------------------------------------- #
def _skip_windows_only() -> bool:
    return _truthy(os.environ.get(CI_ENV)) or not sys.platform.startswith("win")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "windows_only: 依赖 Win32/UIA/Tk 等 Windows-only 能力；SOULAGENT_CI=1 或非 Windows 时跳过",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not _skip_windows_only():
        return
    skip_marker = pytest.mark.skip(
        reason=f"Windows-only 用例（{CI_ENV}=1 或非 Windows 平台：跳过）"
    )
    for item in items:
        if item.get_closest_marker("windows_only"):
            item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def _isolate_pacing(monkeypatch):
    """P16-B：自适应节奏默认只在专项测试里开启。

    pacing 会改写主动消息的概率与间隔（按用户/agent 消息比例自适应），
    若在普通测试里默认生效会改变既有断言（skip reason、是否发送等）。
    这里统一关闭，`test_pacing.py` 等专项测试自行 `monkeypatch.setenv("PACING_ENABLED", "1")`。
    """
    monkeypatch.setenv("PACING_ENABLED", "0")
    yield


@pytest.fixture(autouse=True)
def _isolate_web_lookup(monkeypatch):
    """P24：联网实时信息默认在测试里关闭，避免任何测试真的发网络请求。

    专项测试（`test_web_lookup.py`）自己把 `WEB_LOOKUP_ENABLED` 设回 "1"。
    """
    monkeypatch.setenv("WEB_LOOKUP_ENABLED", "0")
    yield


@pytest.fixture(autouse=True)
def _isolate_contact_db(tmp_path, monkeypatch):
    """把联系人库路径重定向到临时目录，避免测试污染真实 agi/data。"""
    contacts = tmp_path / "_contacts"

    def _fake_contact_db(contact: str):
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(contact))
        return contacts / f"{safe}.sqlite3"

    monkeypatch.setattr("agi.wechat_gateway.config.contact_db", _fake_contact_db)
    yield


@pytest.fixture(autouse=True)
def _isolate_gateway_config(tmp_path, monkeypatch):
    """F1：把网关 ``CONFIG_PATH`` 重定向到 tmp，测试永不写生产 config.json。

    事故（2026-10-27 审计）：部分测试构造 ``Gateway`` 时未传 ``config_path``，
    于是 ``Gateway.__init__`` 用 ``gw_config.CONFIG_PATH``（生产
    ``agi/data/wechat/config.json``）作为引擎开关持久化/热加载路径，跑测试会改
    生产配置 mtime，甚至静默丢 ``db_keys``。

    - 显式传 ``config_path=`` 的测试不受影响；
    - ``test_config_hot_reload`` / ``test_stop_isolation`` 自己 monkeypatch
      ``CONFIG_PATH`` 的用例仍以自己注入的路径为准；
    - ``DATA_DIR`` 一并指到 tmp 的同级目录，保持「STOP 与 config.json 同目录」
      语义（``test_stop_isolation`` 依赖 CONFIG_PATH 落在 DATA_DIR 下）。
    """
    from agi.wechat_gateway import config as gw_config

    isolated_root = tmp_path / "agi" / "data" / "wechat"
    monkeypatch.setattr(gw_config, "DATA_DIR", isolated_root)
    monkeypatch.setattr(gw_config, "CONFIG_PATH", isolated_root / "config.json")
    yield


@pytest.fixture(autouse=True)
def _isolate_stop_path(tmp_path, monkeypatch):
    """P0-22b：把 STOP 路径重定向到 tmp，保证测试永远读不到生产 STOP 文件。

    - 常规测试：``config.stop_path()`` → ``tmp_path/"STOP"``；
    - 测试自己把 ``CONFIG_PATH`` 指到 tmp 时：STOP 仍为 ``CONFIG_PATH`` 同目录
      （保持"开关与配置同目录"语义，test_config_hot_reload 依赖这一点）；
    - 显式 ``config.STOP_PATH``（Path/str/callable）优先，可让单个测试自造开关；
    - 解析结果落在生产 ``config.DATA_DIR`` 下一律改写为 tmp 路径。

    另外把 ``STOP_PATH`` 复位为 None，避免上一个测试的覆盖泄漏到下一个测试。
    """
    from agi.wechat_gateway import config as gw_config

    isolated = tmp_path / "STOP"

    def _isolated_stop_path() -> Path:
        override = gw_config.STOP_PATH
        if override is not None:
            candidate = Path(override() if callable(override) else override)
        else:
            # 不含 override 的默认：与当前 CONFIG_PATH 同目录
            candidate = gw_config.default_stop_path()
        try:
            production = Path(gw_config.DATA_DIR).resolve()
            candidate.resolve().relative_to(production)
        except (ValueError, OSError):
            # 不在生产数据目录下（测试自己指到 tmp / 显式注入）→ 原样使用
            return candidate
        # 生产路径 → 重定向到 tmp，测试永远看不到真实 STOP
        return isolated

    monkeypatch.setattr(gw_config, "STOP_PATH", None)
    monkeypatch.setattr(gw_config, "stop_path", _isolated_stop_path)
    yield


@pytest.fixture(autouse=True)
def _isolate_import_time_paths(tmp_path, monkeypatch):
    """把「import 时就绑定」的路径常量再钉到每个用例自己的 tmp_path（第二层保险）。

    第一层是模块顶层的 env 重定向（整个会话的根搬到 tempfile，保证绝不碰生产）；
    这一层负责「用例内」的干净：Gateway 不传 ``log_path`` / ``drafts_path`` 时会回落到
    ``server.LOG_PATH`` / ``drafts.DEFAULT_PATH``，共用会话目录会让用例之间串写；
    ``selflearn.db`` 的运行时路径解析（``_base_path`` / ``memory_path``）读模块全局
    ``DB_PATH``，也在这里指到 tmp。

    用 ``raising=False``：这些常量归别的模块维护，改名/删除不应该让整套用例因为
    setattr 而红——隔离是兜底，不是耦合。
    """
    from agi.selflearn import config as sl_config
    from agi.selflearn import db as sl_db
    from agi.wechat_gateway import drafts as gw_drafts
    from agi.wechat_gateway import server as gw_server

    db_path = tmp_path / "selflearn.sqlite3"
    monkeypatch.setattr(sl_config, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(sl_db, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(gw_server, "LOG_PATH", tmp_path / "gateway.log", raising=False)
    monkeypatch.setattr(gw_drafts, "DEFAULT_PATH", tmp_path / "drafts.jsonl", raising=False)
    yield
