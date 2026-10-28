# -*- coding: utf-8 -*-
"""架构守卫 + 主动链登记表的可证伪测试。

为什么放在根 ``tests/``：这两件事都是「文档/架构不许和代码走散」的守门规则，
属于交付质量而非某个包内部行为；用 tmp 目录构造最小仓库，正反两面都测
（规则必须能 FAIL，也必须能在合法结构上 PASS），避免守卫本身变成摆设。

运行：``python -m pytest tests/test_architecture.py -q``
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # python -m pytest 时 CWD 已在 path，直接跑文件时兜底
    sys.path.insert(0, str(ROOT))

from agi import proactive_registry as registry  # noqa: E402
from tools import check_architecture as arch  # noqa: E402


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 规则 1：死代码回归
# --------------------------------------------------------------------------- #
def test_unregistered_top_level_dir_is_flagged(tmp_path):
    _write(tmp_path / "newtop" / "tool.py", "x = 1\n")
    violations = arch.scan_dead_code(tmp_path, top_dirs={"agi"}, allow_unimported={})
    assert any("顶层目录未登记：newtop/" in item for item in violations)


def test_unreferenced_module_is_flagged(tmp_path):
    _write(tmp_path / "pkg" / "orphan.py", "VALUE = 1\n")
    violations = arch.scan_dead_code(tmp_path, top_dirs={"pkg"}, allow_unimported={})
    assert any("pkg/orphan.py" in item and "疑似死代码" in item for item in violations)


def test_referenced_or_allowlisted_module_passes(tmp_path):
    _write(tmp_path / "pkg" / "used.py", "VALUE = 1\n")
    _write(tmp_path / "pkg" / "manual_cli.py", "VALUE = 2\n")
    _write(tmp_path / "main.py", "from pkg import used\nprint(used.VALUE)\n")
    violations = arch.scan_dead_code(
        tmp_path, top_dirs={"pkg"},
        allow_unimported={"pkg/manual_cli.py": "手动 CLI"},
    )
    # used.py 有 import 方，manual_cli.py 已登记；两者都不该报死代码
    assert not any("used.py" in item or "manual_cli.py" in item for item in violations)


# --------------------------------------------------------------------------- #
# 规则 2：agi 内部 import 环
# --------------------------------------------------------------------------- #
def test_import_cycle_is_flagged(tmp_path):
    _write(tmp_path / "agi" / "__init__.py", "")
    _write(tmp_path / "agi" / "a.py", "from . import b\n")
    _write(tmp_path / "agi" / "b.py", "from . import a\n")
    violations = arch.scan_cycles(tmp_path)
    assert violations and "agi 内部 import 环" in violations[0]


def test_function_level_import_is_not_a_cycle(tmp_path):
    # 函数内懒加载是刻意打破加载期循环的手段，不应判环
    _write(tmp_path / "agi" / "__init__.py", "")
    _write(tmp_path / "agi" / "a.py", "from . import b\n")
    _write(tmp_path / "agi" / "b.py", "def need():\n    from . import a\n    return a\n")
    assert arch.scan_cycles(tmp_path) == []


# --------------------------------------------------------------------------- #
# 规则 3：单文件行数预算
# --------------------------------------------------------------------------- #
def test_oversized_new_file_is_flagged(tmp_path):
    _write(tmp_path / "huge.py", "x = 1\n" * (arch.NEW_FILE_MAX_LINES + 1))
    _write(tmp_path / "small.py", "x = 1\n" * 10)
    violations = arch.scan_line_budget(tmp_path, budget={})
    assert any("huge.py" in item for item in violations)
    assert not any("small.py" in item for item in violations)


def test_allowlisted_budget_is_enforced(tmp_path):
    _write(tmp_path / "legacy_big.py", "x = 1\n" * 20)
    violations = arch.scan_line_budget(
        tmp_path, budget={"legacy_big.py": 10}, new_file_max=1500)
    assert any("legacy_big.py" in item and "存量文件超预算" in item for item in violations)


# --------------------------------------------------------------------------- #
# 规则 4：分层（agi 不得模块级依赖 tools/webui）
# --------------------------------------------------------------------------- #
def test_agi_module_level_import_of_tools_is_flagged(tmp_path):
    _write(tmp_path / "agi" / "__init__.py", "")
    _write(tmp_path / "agi" / "bad.py", "from tools import helper\n")
    _write(tmp_path / "tools" / "__init__.py", "")
    _write(tmp_path / "tools" / "helper.py", "x = 1\n")
    violations = arch.scan_layering(tmp_path)
    assert any("agi/bad.py" in item and "tools" in item for item in violations)


def test_agi_function_level_import_of_webui_is_allowed(tmp_path):
    _write(tmp_path / "agi" / "__init__.py", "")
    _write(tmp_path / "agi" / "ok.py",
           "def maybe():\n    from webui import server\n    return server\n")
    assert arch.scan_layering(tmp_path) == []


# --------------------------------------------------------------------------- #
# 真实仓库：守卫必须 PASS（否则 CI 直接拦）
# --------------------------------------------------------------------------- #
def test_repo_passes_architecture_guard():
    result = arch.run_checks(ROOT)
    assert result["ok"], "架构守卫在真实仓库上失败：\n" + "\n".join(result["violations"])


# --------------------------------------------------------------------------- #
# 主动链登记表：主链唯一 + 与代码实际调用一致
# --------------------------------------------------------------------------- #
def test_proactive_registry_has_exactly_one_primary():
    primaries = [entry for entry in registry.ENTRIES if entry.role == "primary"]
    assert len(primaries) == 1, f"主链必须唯一，实际 {[entry.name for entry in primaries]}"
    assert primaries[0].name == registry.PRIMARY
    assert registry.primary_entry().name == registry.PRIMARY


def test_proactive_registry_covers_active_legacy_manual():
    statuses = {entry.status for entry in registry.ENTRIES}
    assert statuses == {"active", "legacy", "manual"}
    # 「5 套主动链」的收敛目标：登记表至少覆盖 5 条入口，且现役主链可顺序描述
    assert len(registry.ENTRIES) >= 5
    chain = registry.describe_chain()
    assert chain[0].startswith("agi/wechat_gateway/server.py::")
    assert any("simple_tick" in item for item in chain)


def test_proactive_registry_matches_code():
    """登记表里的模块/入口/证据必须能在代码里静态搜到——防止文档再次凭想象写。"""
    for entry in registry.ENTRIES:
        path = ROOT / entry.module
        assert path.is_file(), f"登记模块不存在：{entry.module}"
        source = path.read_text(encoding="utf-8", errors="replace")
        for evidence in entry.evidence:
            assert evidence in source, (
                f"{entry.name} 的登记证据在 {entry.module} 里找不到：{evidence!r}")

    # 主链每一环的下一跳必须是真实存在的实现
    chain = {entry.name: entry for entry in registry.ENTRIES}
    assert "run_cycle" in (ROOT / chain["worker_run_cycle"].module).read_text(encoding="utf-8")
    assert "ticker.run_once" in (ROOT / chain["worker_run_cycle"].module).read_text(
        encoding="utf-8")
    assert "autonomy.gate" in (ROOT / chain["ticker_run_once"].module).read_text(
        encoding="utf-8")
    assert "proactive.tick(" in (ROOT / chain["ticker_run_once"].module).read_text(
        encoding="utf-8")
    simple = (ROOT / chain["selflearn_tick"].module).read_text(encoding="utf-8")
    assert "simple_tick" in simple and "proactive_style" in simple


def test_run_proactive_tick_is_the_only_definition():
    """主链唯一对外入口：全仓库只允许一处 `def run_proactive_tick(`。"""
    hits = []
    for path in ROOT.rglob("*.py"):
        parts = set(path.parts)
        if "__pycache__" in parts or "legacy" in parts or "docs" in parts or "tests" in parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "def run_proactive_tick(" in text:
            hits.append(path.relative_to(ROOT).as_posix())
    assert hits == ["agi/proactive_registry.py"], hits


def test_run_proactive_tick_respects_engine_switch():
    class _Gateway:
        engine_enabled = False

    result = registry.run_proactive_tick(_Gateway())
    assert result["ran"] is False
    assert result["reason"] == "engine_stopped"


def test_run_proactive_tick_delegates_to_worker(monkeypatch):
    calls = []

    class _FakeWorker:
        @staticmethod
        def run_cycle(gateway):
            calls.append(gateway)
            return {"ok": True, "ran": True, "sent": 0}

    class _Gateway:
        engine_enabled = True

    import agi.wechat_gateway as gateway_pkg
    monkeypatch.setitem(sys.modules, "agi.wechat_gateway.proactive_worker", _FakeWorker)
    monkeypatch.setattr(gateway_pkg, "proactive_worker", _FakeWorker, raising=False)
    gateway = _Gateway()
    result = registry.run_proactive_tick(gateway)
    assert result["ran"] is True and calls == [gateway]


def test_gateway_cycle_matches_registry_entry():
    """server._proactive_cycle 必须委托登记表的唯一入口（不能各自实现一套）。"""
    source = (ROOT / "agi" / "wechat_gateway" / "server.py").read_text(encoding="utf-8")
    assert "def _proactive_cycle(self) -> dict:" in source
    assert "proactive_registry.run_proactive_tick(self)" in source
    # 反向约束：server 里不允许再出现直连 worker 的旧写法（防止有人改回去）
    assert "proactive_worker.run_cycle(self)" not in source