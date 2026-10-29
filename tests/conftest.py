# -*- coding: utf-8 -*-
"""根 ``tests/`` 的全局隔离：环境变量重定向 + 跑完自证守卫。

为什么隔离必须写在模块顶层（import 时）
---------------------------------------
生产代码把数据/日志/配置路径写成 **import 时求值** 的常量：
``agi.wechat_gateway.config.DATA_DIR`` / ``CONFIG_PATH``、
``agi.selflearn.config.DB_PATH``、``agi.soul.dataset.MESSAGE_LOG``、
``agi.wechat_gateway.server.LOG_PATH``、``agi.wechat_gateway.drafts.DEFAULT_PATH``……
模块一旦被 import，fixture 再怎么 monkeypatch 也改不回函数默认参数里已经绑定的对象。
所以本文件在 **模块顶层、任何 agi 模块被 import 之前** 把
``SOULAGENT_DATA_DIR`` / ``SOULAGENT_LOG_DIR`` / ``SOULAGENT_CONFIG_PATH`` /
``SOULAGENT_WEB_CONFIG`` 指到 ``tempfile.mkdtemp()`` 的会话级临时目录，
让 ``agi.paths.data_dir()/log_dir()/config_path()/web_config_path()`` 解析到 tmp。

为什么还要「跑完守卫」
----------------------
仅断言路径不在仓库里是「静态可信」；守卫是「动态自证」：测试会话开始对生产敏感文件
取 ``(mtime_ns, size)`` 快照，会话结束时再取一次，任何文件被改写/删除/新建都会让整个
会话 fail 并打印是哪个文件。这样「测试不写生产」不是口号，而是每次跑测试都会验证的断言。
守卫本身的有效性由 ``tests/test_isolation_guard.py`` 反向验证（改一个临时文件必须能被发现）。

Windows-only 用例的 CI 口径
---------------------------
``SOULAGENT_CI=1`` 或非 Windows 平台时，带 ``pytest.mark.windows_only`` 的用例统一 skip；
不再靠 ``pytest --ignore=整文件`` 掩盖（ignore 连「被跳过了多少条」都看不见）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

#: 非 Windows 下整文件不收集：这些模块顶层 import wx_core.wcdb/UIA，Linux 会直接
#: AttributeError（ctypes.WinDLL），不能靠 pytest.mark 在 import 后跳过。
#: CI 仍是“纯逻辑 + 离线测试”；Windows 真机跑完整两套。
collect_ignore: list[str] = []
if sys.platform != "win32":
    collect_ignore += ["test_core.py", "test_webui.py"]

#: 仓库根（本文件在 <repo>/tests/conftest.py）
REPO_ROOT = Path(__file__).resolve().parents[1]
#: 会话级临时根：agi/tests/conftest.py 与本文件共用同一个（避免两个 conftest 各建一份）
TEST_ROOT_ENV = "SOULAGENT_TEST_ROOT"
CI_ENV = "SOULAGENT_CI"
#: 生产敏感文件（相对仓库根）。精确路径即使当前不存在也要监视（被创建 = 隔离失效）；
#: 带通配符的用来覆盖按账号/按库滚动生成的 sqlite 文件。
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

    与 ``agi/tests/conftest.py`` 共用 ``SOULAGENT_TEST_ROOT``：一次 ``pytest agi/tests tests``
    只会建一个临时根，两个 conftest 设置的环境变量完全一致（谁先加载都不会互相覆盖）。
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


#: 本会话的隔离根（模块 import 时就建好，先于任何 agi import）
TEST_ROOT = _bootstrap_test_root()


# --------------------------------------------------------------------------- #
# 生产写盘自证守卫
# --------------------------------------------------------------------------- #
def guard_targets(root: Path | str = REPO_ROOT) -> list[Path]:
    """展开监视清单：精确路径永远入列（不存在也记 None），通配符按当前文件系统展开。"""
    base = Path(root)
    targets: dict[str, Path] = {}
    for pattern in GUARD_PATTERNS:
        if any(ch in pattern for ch in "*?["):
            for path in sorted(base.glob(pattern)):
                targets[str(path)] = path
        else:
            path = base / pattern
            targets[str(path)] = path
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
    """返回人话差异列表；空列表 = 生产文件一个都没动。"""
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


def _repo_rel(path: Path) -> str:
    try:
        return str(Path(path).relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


@pytest.fixture(scope="session", autouse=True)
def _production_write_guard():
    """会话开始/结束比对生产敏感文件；有变化就 fail —— 这是「测试不写生产」的自证。"""
    before = fingerprint(guard_targets(REPO_ROOT))
    yield
    after = fingerprint(guard_targets(REPO_ROOT))
    changes = diff_snapshots(before, after)
    if changes:
        pytest.fail(
            "测试写到了生产默认路径——说明隔离失效，请修隔离，不要关守卫：\n  "
            + "\n  ".join(changes)
        )


@pytest.fixture(scope="session")
def production_guard_api() -> dict:
    """把守卫实现暴露给 tests/test_isolation_guard.py 做反向自测（守卫能被证伪才可信）。"""
    return {
        "root": REPO_ROOT,
        "patterns": lambda: list(GUARD_PATTERNS),
        "targets": lambda: guard_targets(REPO_ROOT),
        "fingerprint": fingerprint,
        "diff": diff_snapshots,
    }


@pytest.fixture(scope="session")
def ci_policy() -> dict:
    """把 CI/平台判定暴露给测试（避免测试文件 ``import conftest`` 这种脆弱写法）。"""
    return {
        "is_windows": sys.platform.startswith("win"),
        "skip_windows_only": _skip_windows_only,
        "ci_env": CI_ENV,
    }


@pytest.fixture(scope="session", autouse=True)
def _redirect_host_config_constants():
    """根 ``config.json`` / ``web_config.json`` 的读取方在 import 时绑定了路径常量。

    ``agi.paths`` 的眼睛只覆盖 agi 侧；``wx_core.config.CONFIG_PATH``（含 db_keys）与
    ``wx_core.web_search.CONFIG_PATH``（含 LLM key）是根 tests 实际会碰到的两个读取方，
    它们在 import 时把 ``<repo>/config.json`` / ``<repo>/web_config.json`` 写死。
    这里在会话开始时把模块常量改到 tmp（end 还原），与 SOULAGENT_*_PATH 环境变量双保险。
    """
    import wx_core.config as wx_config
    import wx_core.web_search as wx_search

    originals = {
        "wx_core.config.CONFIG_PATH": wx_config.CONFIG_PATH,
        "wx_core.web_search.CONFIG_PATH": wx_search.CONFIG_PATH,
    }
    wx_config.CONFIG_PATH = TEST_ROOT / "config.json"
    wx_search.CONFIG_PATH = TEST_ROOT / "web_config.json"
    try:
        yield
    finally:
        wx_config.CONFIG_PATH = originals["wx_core.config.CONFIG_PATH"]
        wx_search.CONFIG_PATH = originals["wx_core.web_search.CONFIG_PATH"]


# --------------------------------------------------------------------------- #
# Windows-only 用例：CI / 非 Windows 统一 skip（精确到标记，不做整文件 ignore）
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