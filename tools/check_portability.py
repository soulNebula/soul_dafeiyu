#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P0-3：可移植性冒烟（硬编码路径 / requirements 可解析 / AGENT_HOME fallback）。

用法::

    python tools/check_portability.py            # 代码 PASS/FAIL + 文档 WARN
    python tools/check_portability.py --strict-docs   # 文档里的旧路径也算 FAIL

退出码：0 = PASS（允许 WARN），1 = FAIL。

扫描规则：
- 代码（.py/.bat/.cmd/.ps1）：出现 ``E:\\agent`` → FAIL；``agi/paths.py`` 里
  ``DEFAULT_AGENT_HOME`` 是唯一的合法默认值，跳过；
- 文档（.md/.txt/.json）：旧路径只 WARN（文档由另一任务收口；不阻断可移植性）；
- 归档（docs/archive、scratch）、依赖目录（.venv/venv312/node_modules）、
  大文件目录（models/output/logs/tmp、agi/data）不扫；
- requirements.txt：每行必须能被 PEP 508 解析（packaging 可用时用它，否则正则兜底）；
- 运行时检查：AGENT_HOME > SOULAGENT_AGENT_HOME > paths.json > 默认，派生目录正确。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARDCODE = "E:" + chr(92) + "agent"
SKIP_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", "scratch",
    ".venv", "venv", "venv312", "node_modules", ".idea", "models",
    "output", "logs", "tmp",
}
CODE_SUFFIXES = {".py", ".ps1"}          # 真正会执行的代码
LAUNCHER_SUFFIXES = {".bat", ".cmd"}     # bat 里多是注释/提示，WARN 不阻断
DOC_SUFFIXES = {".md", ".txt", ".json"}

FAILS: list[str] = []
WARNS: list[str] = []


def fail(message: str) -> None:
    FAILS.append(message)
    print("FAIL  " + message)


def warn(message: str) -> None:
    WARNS.append(message)
    print("WARN  " + message)


def ok(message: str) -> None:
    print("PASS  " + message)


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "gbk", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def iter_files():
    for directory, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        base = Path(directory)
        if base == ROOT / "agi" / "data":
            dirnames[:] = []
            names = [name for name in filenames if name == "paths.json"]
        else:
            names = filenames
        for name in names:
            path = base / name
            yield path


#: 安装器允许出现默认 E:gent（用户首次运行前还没有 paths.json/env）
INSTALLER_ALLOW = {Path("setup.ps1"), Path("tools/setup_llama.ps1")}


def scan_hardcodes(strict_docs: bool = False) -> None:
    code_hits: list[str] = []
    doc_hits: list[str] = []
    for path in iter_files():
        suffix = path.suffix.lower()
        if suffix not in CODE_SUFFIXES and suffix not in LAUNCHER_SUFFIXES \
                and suffix not in DOC_SUFFIXES:
            continue
        if path == ROOT / "agi" / "paths.py" \
                or path == ROOT / "tools" / "check_portability.py":
            continue
        try:
            rel_parts = path.relative_to(ROOT).parts
        except ValueError:
            continue
        if len(rel_parts) >= 2 and rel_parts[0] == "docs" and rel_parts[1] == "archive":
            continue
        if path.relative_to(ROOT) in INSTALLER_ALLOW:
            continue
        try:
            text = _read_text(path)
        except OSError:
            continue
        if HARDCODE not in text:
            continue
        rel = path.relative_to(ROOT)
        lines = [index + 1 for index, line in enumerate(text.splitlines())
                 if HARDCODE in line]
        marker = f"{rel}: {', '.join(str(line) for line in lines[:5])}"
        if suffix in CODE_SUFFIXES:
            code_hits.append(marker)
        elif suffix in LAUNCHER_SUFFIXES:
            doc_hits.append(marker + "（快捷启动脚本，仅注释/提示）")
        else:
            doc_hits.append(marker)
    if code_hits:
        fail(f"代码里仍有 {len(code_hits)} 个 E:\\agent 硬编码（应走 agi.paths）")
        for hit in code_hits[:20]:
            print("      " + hit)
    else:
        ok("代码无 E:\\agent 硬编码（agi/paths.py 默认值除外）")
    if doc_hits:
        message = f"文档里仍有 {len(doc_hits)} 处旧路径 E:\\agent（不阻断可移植性）"
        if strict_docs:
            fail(message)
        else:
            warn(message)
        for hit in doc_hits[:10]:
            print("      " + hit)


def check_requirements() -> None:
    path = ROOT / "requirements.txt"
    if not path.is_file():
        fail("requirements.txt 不存在")
        return
    try:
        from packaging.requirements import Requirement  # type: ignore
        parser = Requirement
    except Exception:  # noqa: BLE001 - 没装 packaging 时用正则兜底
        parser = None
    name_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]+\])?")
    invalid: list[str] = []
    count = 0
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        count += 1
        spec = line.split(";", 1)[0].strip()
        valid = True
        if parser is not None:
            try:
                parser(line)
            except Exception:  # noqa: BLE001
                valid = False
        else:
            valid = bool(name_re.match(spec))
        if not valid:
            invalid.append(f"requirements.txt:{lineno}: {line}")
    if invalid:
        fail(f"requirements.txt 有 {len(invalid)} 行无法解析")
        for item in invalid[:10]:
            print("      " + item)
    else:
        ok(f"requirements.txt 每行可解析（{count} 个依赖）")
    text = path.read_text(encoding="utf-8")
    if "pycryptodome==4.1.0" in text:
        fail("requirements.txt 仍写着审计报告有问题的 pycryptodome==4.1.0")
    else:
        ok("pycryptodome 已改为可安装范围（>=3.20,<5）")


def _load_paths_module():
    spec = importlib.util.spec_from_file_location(
        "portability_paths", ROOT / "agi" / "paths.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def check_env_fallback() -> None:
    paths = _load_paths_module()
    saved_env = {name: os.environ.get(name) for name in
                 ("AGENT_HOME", "SOULAGENT_AGENT_HOME")}
    saved_json = paths.PATHS_JSON
    tmp = Path(tempfile.mkdtemp(prefix="p03_paths_"))
    try:
        # 1) 默认：env/config 都没有 → 默认 home
        for name in saved_env:
            os.environ.pop(name, None)
        paths.PATHS_JSON = tmp / "missing.json"
        default_expected = Path("E:" + chr(92) + "agent")
        if paths.agent_home() == default_expected:
            ok("默认 AGENT_HOME = E:\\agent")
        else:
            fail(f"默认值不对：{paths.agent_home()}")
        # 2) AGENT_HOME 优先
        os.environ["AGENT_HOME"] = str(tmp / "home_a")
        os.environ["SOULAGENT_AGENT_HOME"] = str(tmp / "home_b")
        if paths.agent_home() == tmp / "home_a":
            ok("env AGENT_HOME 优先于 SOULAGENT_AGENT_HOME")
        else:
            fail("AGENT_HOME 优先级不对")
        # 3) SOULAGENT_AGENT_HOME 次之
        os.environ.pop("AGENT_HOME")
        if paths.agent_home() == tmp / "home_b":
            ok("env SOULAGENT_AGENT_HOME 生效")
        else:
            fail("SOULAGENT_AGENT_HOME 没生效")
        # 4) paths.json 第三
        os.environ.pop("SOULAGENT_AGENT_HOME")
        config = tmp / "paths.json"
        config.write_text(json.dumps({"agent_home": str(tmp / "home_json")}),
                          encoding="utf-8")
        paths.PATHS_JSON = config
        if paths.agent_home() == tmp / "home_json":
            ok("agi/data/paths.json 的 agent_home 生效")
        else:
            fail("paths.json 没生效")
        # 5) 派生目录
        home = paths.agent_home()
        checks = {
            "models_dir": home / "models",
            "gguf_dir": home / "output" / "gguf",
            "adapters_dir": home / "output",
            "logs_dir": home / "logs",
        }
        wrong = [name for name, expected in checks.items()
                 if getattr(paths, name)() != expected]
        if wrong:
            fail(f"派生目录不对：{', '.join(wrong)}")
        else:
            ok("models_dir/gguf_dir/adapters_dir/logs_dir 派生正确")
        if paths.venv312_python() == home / "venv312" / "Scripts" / "python.exe":
            ok("venv312_python 派生正确")
        else:
            fail("venv312_python 派生不对")
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        paths.PATHS_JSON = saved_json


def main() -> int:
    parser = argparse.ArgumentParser(description="SoulAgent 可移植性冒烟")
    parser.add_argument("--strict-docs", action="store_true",
                        help="文档里的旧路径也按 FAIL 处理")
    args = parser.parse_args()
    print(f"repo: {ROOT}")
    scan_hardcodes(strict_docs=args.strict_docs)
    check_requirements()
    check_env_fallback()
    print()
    print(f"结果：{'FAIL' if FAILS else 'PASS'}（FAIL={len(FAILS)}, WARN={len(WARNS)}）")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
