#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""架构守卫：四条硬规则，违反即非零退出（可接 CI / release_check）。

规则
----
1. **死代码回归**：顶层目录必须在 `TOP_LEVEL_DIRS` 登记；非入口目录里的 `.py` 模块若
   **既没有 import 边、也没有字符串/路径引用**，且不在 `UNIMPORTED_ALLOWLIST` 里，判 FAIL。
   （`legacy/`、`docs/`、`Demo/`、`media/`、`agi/data/` 不参与；归档流程见 `legacy/README.md`。）
2. **agi 内部 import 环**：`agi.**` 模块之间不允许循环依赖（Tarjan 强连通分量，>1 即 FAIL）。
3. **单文件行数预算**：新增文件 > `NEW_FILE_MAX_LINES` 直接 FAIL；`LINE_BUDGET` 里的存量
   大文件各自有一个上限（基线登记，超出即 FAIL，逼迫拆分而不是继续膨胀）。
4. **分层违例**：`agi/*` 的**模块级** import 不得指向 `tools/*` 或 `webui/*`
   （函数内懒加载的可选校验不算；`agi/tests/` 与 `agi/eval_data/` 是测试/评测，不在此列）。

用法::

    python tools/check_architecture.py            # 跑四条规则，0=PASS 1=FAIL
    python tools/check_architecture.py --json     # 机器可读
    python tools/check_architecture.py --dead     # 只列「无引用模块」候选（归档审计用）

为什么要有它：项目历史上出现过「整目录复制副本」「5 套主动链并存」「单文件膨胀到几千行」
这类问题；靠人盯不住，靠这条命令在提交前拦一下。它只做静态扫描，不 import 项目代码、
不联网、不启动服务。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]

# --- 规则 1：顶层目录白名单（新增顶层目录时在这里登记，否则 FAIL） -------------
TOP_LEVEL_DIRS: dict[str, str] = {
    "agi": "核心包（selflearn/inner/llm/integrations/wechat_gateway/soul/persona）",
    "tools": "运维/控制台/训练/桌面脚本（入口集合）",
    "tests": "根测试套件",
    "scripts": "发布与验证脚本",
    "wx_core": "wxauto4 / WCDB / OCR 采集与发送底座",
    "webui": "8081 旧网页端（legacy，非主链，仍有测试与探测引用）",
    "ui": "Tk 桌面 GUI（app.py 入口）",
    "Demo": "独立工具实现（tools_impl 被 tools/ 动态 import；其余已归档到 legacy/）",
    "legacy": "死代码归档区（只归档不删除，见 legacy/README.md）",
    "docs": "文档（含历史快照 archive/、来源材料 sources/）",
    "idea": "803 IDEA 工作台（独立 git 历史，subtree 合入；有自己的一套 app/web/tests）",
    "media": "本地素材/媒体数据（运行数据，不入库）",
    "__pycache__": "字节码缓存",
    "log": "运行时日志目录（gitignore，不参与代码）",
    "reports": "运行/验收报告（gitignore/可归档）",
    "scratch": "临时草稿目录（gitignore）",
    "wechatauto_logs": "发送后端运行日志（gitignore）",
    "wxauto_logs": "旧发送后端运行日志（gitignore）",
    "_refs": "外部参考源码（只读参考，不参与运行时 import）",
}

# --- 规则 1：已知「无 import 边但活的」模块（动态 import / 手动入口），带理由 ---
UNIMPORTED_ALLOWLIST: dict[str, str] = {
    "_refs/Wechat-Emoticon-Parser-4.0-plus/wechat_emoticon_export.py": "外部参考源码（手工工具，不参与运行时）",
    "_refs/wechat-4x-decrypt-main/scripts/find_wechat.py": "外部参考源码（手工工具，不参与运行时）",
    "_refs/wechat-4x-decrypt-main/scripts/query_messages.py": "外部参考源码（手工工具，不参与运行时）",
    # 手动 CLI / 评测 / 实验入口：靠 `python -m ...` 或 sys.path 注入运行，没有 import 方
    "agi/selflearn/cli.py": "手动 CLI（agi/selflearn/README.md 文档化：python -m agi.selflearn.cli ...）",
    "agi/selflearn/experiment_p6d.py": "一次性实验脚本（P6-d 对照实验，手动运行）",
    "agi/selflearn/validate_p6c.py": "一次性验收脚本（P6-c 手动运行）",
    "agi/eval_data/ablation.py": "离线评测脚本（手动运行）",
    "agi/eval_data/downstream.py": "离线评测脚本（手动运行）",
    "agi/eval_data/judge.py": "离线评测脚本（手动运行）",
    "agi/eval_data/style_prompt.py": "离线评测脚本（手动运行）",
    "agi/eval_data/tone_eval.py": "离线评测脚本（手动运行）",
    "agi/eval_data/tone_style_baselines.py": "离线评测脚本（手动运行）",
    "agi/eval_data/tone_style_data.py": "离线评测脚本（手动运行）",
    "agi/eval_data/tone_style_eval.py": "离线评测脚本（手动运行）",
    "agi/eval_data/tone_style_train.py": "离线评测脚本（手动运行）",
    "agi/eval_data/tone_train.py": "离线评测脚本（手动运行）",
    "agi/soul/dataset.py": "数据构建脚本（手动运行，产出训练集）",
    "agi/soul/evaluate.py": "离线评测脚本（手动运行）",
    "agi/soul/prompt.py": "评测/构建共用库，由同目录手动脚本使用",
    "agi/soul/scenarios.py": "评测场景数据（被手动评测脚本读取）",
    "agi/soul/scenarios_extra.py": "评测场景数据（被手动评测脚本读取）",
    # 运行期靠字符串/子进程引用的模块
    "agi/media_acquisition.py": "被 agi/tests/test_console_server.py 动态 import，且是可选能力入口",
    "wx_core/api.py": "Backend 协议定义，主程序用字符串/文档引用（app.py 校验协议）",
    "wx_core/friends.py": "只读 contact.db 的查询库，由 webui/console 按字符串路径调用",
    "webui/moments_worker.py": "子进程脚本（webui/moments.py 用 sys.executable 调起，不可 import）",
    "tools/bili_patch/apply_report_header_fix.py": "手动补丁脚本（tools/bili_patch 下）",
}

# --- 规则 3：单文件行数预算（存量基线；新增文件一律 1500 行上限） --------------
NEW_FILE_MAX_LINES = 1500
LINE_BUDGET: dict[str, int] = {
    # 基线为 2026-10-29 收口时行数；只允许小幅波动，继续膨胀请拆分文件。
    "tools/console/server.py": 5550,          # 2026-10-29 新增联系人隔离/手工发送（待拆）
    "tests/test_webui.py": 4700,              # 现有 4592：网页端集成测试
    "agi/selflearn/proactive.py": 3100,       # 现有 3056：主动消息生成（待拆）
    "agi/wechat_gateway/server.py": 3000,     # 现有 2718（并发改造中，基线 2373）
    "agi/tests/test_console_server.py": 2300,  # 2026-10-29 新增页面/联系人测试
    "webui/server.py": 2000,                  # 现有 1965
    "agi/selflearn/chat.py": 1720,            # 新增聊天图片本地识图
    "agi/soul/dataset.py": 1600,              # 现有 1510
}

# --- 通用扫描范围 -------------------------------------------------------------
SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "venv312", "node_modules",
    ".pytest_cache", ".mypy_cache", "media", "docs", "legacy", "Demo",
    # 803 工作台是独立项目（自己一套 app/web/tests，subtree 合入），不按主仓模块规则扫
    "idea",
}
ENTRY_DIR_PREFIXES = ("tools/", "tests/", "scripts/", "agi/tests/")
ENTRY_FILE_NAMES = {"__init__.py", "conftest.py", "app.py", "web_app.py", "setup.py"}
REF_SUFFIXES = {".py", ".bat", ".cmd", ".ps1", ".json"}


# --------------------------------------------------------------------------- #
# 基础：枚举 + import 图
# --------------------------------------------------------------------------- #
def _iter_files(root: Path, suffixes: Iterable[str] | None = None) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not d.startswith("."))
        for name in filenames:
            path = Path(dirpath) / name
            if suffixes is None or path.suffix.lower() in suffixes:
                out.append(path)
    return out


def _module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _import_targets(tree: ast.AST, module: str, *, module_level_only: bool = False) -> set[str]:
    """收集一个文件里的 import 目标（相对 import 展开成绝对名）。

    ``module_level_only=True`` 时只统计模块级 import：函数内懒加载是**刻意**用来打破
    加载期循环的手段（例如 ``prompt_store`` 在函数里才 import ``proactive``），
    不该被当成环或分层违例。
    """
    pkg = module.rsplit(".", 1)[0] if "." in module else ""
    nodes = tree.body if module_level_only else ast.walk(tree)
    targets: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0] if "." in base else ""
                prefix = f"{base}." if base else ""
            else:
                prefix = f"{node.module}." if node.module else ""
            if node.module:
                targets.add((prefix + node.module).lstrip(".") if not node.level
                            else prefix.rstrip("."))
            for alias in node.names:
                targets.add(prefix + alias.name)
    return {t.rstrip(".") for t in targets if t.rstrip(".")}


def _build_import_graph(root: Path, *, module_level_only: bool = False
                        ) -> tuple[dict[str, Path], dict[str, set[str]], dict[str, set[str]]]:
    """返回 (模块名→文件, 模块→依赖, 模块→被谁依赖)。

    ``module_level_only=False``（默认）用于死代码/可达性：函数内懒加载也算活引用；
    ``True`` 用于 import 环：只认模块级 import。
    """
    files = _iter_files(root, {".py"})
    mods = {_module_name(root, p): p for p in files}
    edges: dict[str, set[str]] = {m: set() for m in mods}
    importers: dict[str, set[str]] = {}
    for module, path in mods.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:  # 语法错误交给 pytest，不在守卫里重复报
            print(f"[warn] 跳过无法解析的文件 {path.relative_to(root)}: {exc}")
            continue
        for target in _import_targets(tree, module, module_level_only=module_level_only):
            parts = target.split(".")
            for i in range(len(parts), 0, -1):
                candidate = ".".join(parts[:i])
                if candidate in mods and candidate != module:
                    edges[module].add(candidate)
                    importers.setdefault(candidate, set()).add(module)
                    break
    return mods, edges, importers


def _reference_corpus(root: Path) -> str:
    """非 import 引用语料：脚本/配置里出现的模块名或相对路径（抓动态 import / 子进程）。"""
    chunks: list[str] = []
    for path in _iter_files(root, REF_SUFFIXES):
        if path.suffix.lower() in {".py"} and path.relative_to(root).as_posix() in {
            "tools/check_architecture.py"}:
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


# --------------------------------------------------------------------------- #
# 规则 1：死代码回归
# --------------------------------------------------------------------------- #
def scan_dead_code(root: Path, *,
                   top_dirs: dict[str, str] | None = None,
                   allow_unimported: dict[str, str] | None = None) -> list[str]:
    top_dirs = TOP_LEVEL_DIRS if top_dirs is None else top_dirs
    allow_unimported = UNIMPORTED_ALLOWLIST if allow_unimported is None else allow_unimported
    violations: list[str] = []

    # (a) 未登记的顶层目录
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith(".") or child.name == "__pycache__":
                continue
            if child.name not in top_dirs:
                violations.append(
                    f"顶层目录未登记：{child.name}/（新增目录必须在 tools/check_architecture.py "
                    f"的 TOP_LEVEL_DIRS 登记用途；若为死代码请归档到 legacy/）")

    # (b) 无 import 边、无字符串引用的模块
    mods, _edges, importers = _build_import_graph(root)
    corpus = _reference_corpus(root)
    for module, path in sorted(mods.items()):
        rel = path.relative_to(root).as_posix()
        if module in importers:
            continue
        if path.name in ENTRY_FILE_NAMES:
            continue
        if rel.startswith(ENTRY_DIR_PREFIXES):
            continue
        if rel in allow_unimported:
            continue
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            continue
        # 字符串/路径引用：模块全名，或仓库相对路径
        if module in corpus or rel in corpus:
            continue
        violations.append(
            f"疑似死代码：{rel}（无 import 方、无字符串引用；"
            f"若为手动/动态入口请在 UNIMPORTED_ALLOWLIST 登记，否则归档到 legacy/）")
    return violations


# --------------------------------------------------------------------------- #
# 规则 2：agi 内部 import 环
# --------------------------------------------------------------------------- #
def scan_cycles(root: Path) -> list[str]:
    mods, edges, _importers = _build_import_graph(root, module_level_only=True)
    agi_mods = {m for m in mods if m == "agi" or m.startswith("agi.")}
    graph = {m: sorted(d for d in edges.get(m, ()) if d in agi_mods) for m in agi_mods}
    violations: list[str] = []
    for comp in _strongly_connected(graph):
        if len(comp) > 1:
            chain = " → ".join(sorted(comp))
            violations.append(f"agi 内部 import 环：{chain}（拆依赖或下沉公共实现）")
    return violations


def _strongly_connected(graph: dict[str, list[str]]) -> list[list[str]]:
    """迭代版 Tarjan（避免深递归）。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0
    for start in graph:
        if start in index:
            continue
        work: list[tuple[str, int]] = [(start, 0)]
        while work:
            node, child_i = work.pop()
            if child_i == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            recurse = False
            children = graph.get(node, ())
            for i in range(child_i, len(children)):
                child = children[i]
                if child not in index:
                    work.append((node, i + 1))
                    work.append((child, 0))
                    recurse = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if recurse:
                continue
            if low[node] == index[node]:
                comp = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    comp.append(member)
                    if member == node:
                        break
                result.append(comp)
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return result


# --------------------------------------------------------------------------- #
# 规则 3：单文件行数预算
# --------------------------------------------------------------------------- #
def scan_line_budget(root: Path, *,
                     budget: dict[str, int] | None = None,
                     new_file_max: int = NEW_FILE_MAX_LINES) -> list[str]:
    budget = LINE_BUDGET if budget is None else budget
    violations: list[str] = []
    for path in _iter_files(root, {".py"}):
        rel = path.relative_to(root).as_posix()
        lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        cap = budget.get(rel, new_file_max)
        if lines > cap:
            hint = ("存量文件超预算，请拆分" if rel in budget
                    else f"新增文件超过 {new_file_max} 行上限，请拆分")
            violations.append(f"行数超预算：{rel} {lines} 行 > {cap}（{hint}）")
    return violations


# --------------------------------------------------------------------------- #
# 规则 4：分层违例（模块级 import 才计数）
# --------------------------------------------------------------------------- #
def scan_layering(root: Path) -> list[str]:
    violations: list[str] = []
    for path in _iter_files(root / "agi", {".py"}):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(("agi/tests/", "agi/eval_data/")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in tree.body:  # 只看模块级语句：函数内懒加载不算分层违例
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".", 1)[0]
                if top in {"tools", "webui"}:
                    violations.append(
                        f"分层违例：{rel}:{getattr(node, 'lineno', '?')} 模块级 import {name}"
                        f"（agi 不得依赖 tools/webui；可以改用函数内懒加载或下沉接口）")
    return violations


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def run_checks(root: Path) -> dict[str, Any]:
    checks = {
        "dead_code": scan_dead_code(root),
        "import_cycles": scan_cycles(root),
        "line_budget": scan_line_budget(root),
        "layering": scan_layering(root),
    }
    violations = [f"[{name}] {msg}" for name, items in checks.items() for msg in items]
    mods, edges, _ = _build_import_graph(root)
    return {
        "ok": not violations,
        "violations": violations,
        "checks": checks,
        "stats": {"modules": len(mods), "import_edges": sum(len(v) for v in edges.values())},
    }


def _setup_console() -> None:
    """Windows 控制台/管道下的中文不乱码（与 tools/audit_compliance.py 同款兜底）。"""
    try:
        import ctypes
        if sys.stdout is not None and sys.stdout.isatty():
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="架构守卫（死代码/环/行数/分层）")
    parser.add_argument("--root", default=str(ROOT), help="仓库根目录（测试用）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--dead", action="store_true", help="只列无引用模块候选（归档审计）")
    args = parser.parse_args(argv)
    _setup_console()
    root = Path(args.root).resolve()

    if args.dead:
        mods, _edges, importers = _build_import_graph(root)
        corpus = _reference_corpus(root)
        rows = []
        for module, path in sorted(mods.items()):
            rel = path.relative_to(root).as_posix()
            if module in importers or path.name in ENTRY_FILE_NAMES:
                continue
            if rel.startswith(ENTRY_DIR_PREFIXES) or path.name.startswith("test_"):
                continue
            referenced = module in corpus or rel in corpus
            reason = ("字符串引用" if referenced else
                      ("已登记" if rel in UNIMPORTED_ALLOWLIST else "无引用"))
            rows.append({"module": module, "path": rel, "reason": reason})
        if args.json:
            print(json.dumps({"candidates": rows}, ensure_ascii=False, indent=1))
        else:
            print(f"[dead] 无 import 方的模块候选 {len(rows)} 个：")
            for row in rows:
                print(f"  {row['reason']:6s}  {row['path']}")
            print("\n提示：确认无动态/手动入口后，mv 到 legacy/ 并在 legacy/README.md 登记。")
        return 0

    result = run_checks(root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print("=" * 66)
        print("架构守卫 tools/check_architecture.py")
        print("=" * 66)
        print(f"扫描：{result['stats']['modules']} 个模块 / {result['stats']['import_edges']} 条 import 边")
        if result["ok"]:
            print("[PASS] 四条规则全部通过：")
            print("  1) 死代码回归：无未登记的顶层目录/无引用模块")
            print("  2) agi 内部 import 环：无")
            print("  3) 单文件行数预算：未超")
            print("  4) 分层：agi 未模块级依赖 tools/webui")
        else:
            print(f"[FAIL] 发现 {len(result['violations'])} 项问题：")
            for item in result["violations"]:
                print(f"  {item}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())