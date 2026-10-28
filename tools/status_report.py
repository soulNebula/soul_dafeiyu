#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可复现的状态报告：把「测试数字 / 代码规模 / 归档量」变成命令输出，而不是手抄。

为什么要有它：README / STATUS / 交接里最容易过期的是测试数字。本工具用
`pytest --collect-only` 现场数用例、用 `--run` 现场跑一遍并落盘
`docs/status_measured.json`，再把它写进 `docs/STATUS.md` 的 `STATUS:AUTO` 区块——
文档里的数字永远能对回一次真实运行。区块的表格列与 `scripts/agentb_verify.py`
的输出一致（套件 | 通过 | 失败 | 跳过 | 耗时 | 结果），全量终验的数字可以由
`--from-agentb` 直接导入，不必手抄。

用法::

    python tools/status_report.py                     # 现在就能算的事实（collect 数 / 代码规模 / 归档量）
    python tools/status_report.py --json              # 机器可读
    python tools/status_report.py --run               # 真跑两套测试并回填 STATUS.md（agi/tests 约 14 分钟）
    python tools/status_report.py --run --suites tests  # 只跑根 tests（快速验证工具本身）
    python tools/status_report.py --from-agentb evidence.json  # 导入 scripts/agentb_verify.py --json 的终验数字
    python tools/status_report.py --check             # 校验 STATUS.md 的 collect 数是否过期（过期退出 1）

口径说明（写进 STATUS 的就是这两行）：
- **collect 数**：pytest 收集到的用例数（`--collect-only`），不依赖运行环境，只表示用例面大小；
- **实测结果**（通过/失败/跳过/耗时）：`--run` 或 `scripts/agentb_verify.py` 当时的真实结果，
  环境不同会有差异，所以必须带命令与时间，不能只写一个孤立数字。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATUS_MD = ROOT / "docs" / "quality" / "STATUS.md"
MEASURED_JSON = ROOT / "docs" / "quality" / "status_measured.json"

SUITES: dict[str, str] = {
    "tests": "tests",
    "agi": "agi/tests",
}

AUTO_BEGIN = "<!-- STATUS:AUTO:BEGIN（由 tools/status_report.py 生成，勿手改）-->"
AUTO_END = "<!-- STATUS:AUTO:END -->"

_COLLECT_RE = re.compile(r"(\d+)\s+tests?\s+collected")
_COUNT_RES = {
    "passed": re.compile(r"(\d+)\s+passed"),
    "failed": re.compile(r"(\d+)\s+failed"),
    "skipped": re.compile(r"(\d+)\s+skipped"),
    "errors": re.compile(r"(\d+)\s+errors?\b"),
}
_DURATION_RE = re.compile(r"\bin\s+([\d.]+)s")


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


def _run_pytest(args: list[str], *, timeout: int = 3600) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env, timeout=timeout)


def collect_count(suite: str) -> dict[str, Any]:
    """跑 `pytest <suite> --collect-only -q`，返回用例数（失败也返回 -1 + 错误）。"""
    started = time.time()
    try:
        proc = _run_pytest([SUITES[suite], "--collect-only", "-q"], timeout=600)
    except subprocess.TimeoutExpired:
        return {"collected": -1, "error": "collect 超时", "seconds": round(time.time() - started, 1)}
    match = _COLLECT_RE.search(proc.stdout + proc.stderr)
    collected = int(match.group(1)) if match else -1
    return {"collected": collected,
            "exit_code": proc.returncode,
            "seconds": round(time.time() - started, 1),
            "stderr_tail": "" if collected >= 0 else (proc.stdout + proc.stderr)[-500:]}


def run_suite(suite: str) -> dict[str, Any]:
    """真跑一套测试，解析 passed/failed/skipped 与耗时。"""
    started = time.time()
    proc = _run_pytest([SUITES[suite], "-q"])
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-8:])
    output = proc.stdout + proc.stderr
    result: dict[str, Any] = {
        "command": f"python -m pytest {SUITES[suite]} -q",
        "exit_code": proc.returncode,
        "measured_at": datetime.now().isoformat(timespec="seconds"),
        "wall_seconds": round(time.time() - started, 1),
        "tail": tail,
    }
    # pytest 摘要行的顺序不固定（"1 failed, 280 passed" 或 "280 passed, 1 failed"），
    # 所以逐项匹配数字，而不是依赖固定顺序的正则。
    for key, pattern in _COUNT_RES.items():
        match = pattern.search(output)
        result[key] = int(match.group(1)) if match else 0
    duration = _DURATION_RE.search(output)
    result["pytest_seconds"] = float(duration.group(1)) if duration else result["wall_seconds"]
    if result["passed"] == 0 and result["failed"] == 0 and result["errors"] == 0:
        result["passed"] = -1  # 没能解析出摘要，视为未实测，避免文档写 0
    return result


def static_facts() -> dict[str, Any]:
    """不跑测试的事实：计划份数、归档量、代码规模。"""
    plans_dir = ROOT / "docs" / "plans"
    plans = [p for p in plans_dir.glob("*.md") if p.name != "README.md"] if plans_dir.is_dir() else []
    legacy = ROOT / "legacy"

    def _py_files(base: Path, *, exclude: tuple[str, ...] = ()) -> int:
        if not base.is_dir():
            return 0
        return sum(1 for p in base.rglob("*.py")
                   if "__pycache__" not in p.parts and not any(x in p.parts for x in exclude))

    legacy_files = [p for p in legacy.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts
                    and p.suffix not in (".pyc", ".pyo")] if legacy.is_dir() else []
    legacy_lines = 0
    for path in legacy_files:
        try:
            legacy_lines += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    return {
        "plans": len(plans),
        "legacy_files": len(legacy_files),
        "legacy_lines": legacy_lines,
        "py_modules": _py_files(ROOT, exclude=("legacy", "docs", "Demo")),
        "agi_py": _py_files(ROOT / "agi", exclude=("tests",)),
        "agi_tests_py": _py_files(ROOT / "agi" / "tests"),
        "selflearn_py": _py_files(ROOT / "agi" / "selflearn"),
        "root_tests_py": _py_files(ROOT / "tests"),
    }


def load_measured() -> dict[str, Any]:
    if not MEASURED_JSON.is_file():
        return {}
    try:
        return json.loads(MEASURED_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def build_report(*, measure_collect: bool = True) -> dict[str, Any]:
    measured = load_measured()
    collect = {}
    if measure_collect:
        for suite in SUITES:
            collect[suite] = collect_count(suite)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "collect": collect,
        "measured": measured.get("suites", {}),
        "facts": static_facts(),
    }


def render_auto_block(report: dict[str, Any]) -> str:
    """生成 STATUS.md 的自动区块（纯事实，无形容词）。

    表格列与 ``scripts/agentb_verify.py`` 一致：套件 | 通过 | 失败 | 跳过 | 耗时 | 结果；
    collect 数单列一行（它是用例面大小，不是通过数，混在一张表里最容易被误读）。
    """
    lines = [AUTO_BEGIN, ""]
    lines.append(f"最后生成：{report['generated_at']}（Python {report['python']}）")
    lines.append("")
    lines.append("| 套件 | 通过 | 失败 | 跳过 | 耗时 | 结果 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for suite in ("tests", "agi"):
        name = SUITES[suite]
        measured = report["measured"].get(suite)
        if measured and measured.get("passed", -1) >= 0:
            duration = measured.get("pytest_seconds") or measured.get("wall_seconds") or 0
            lines.append(
                f"| `{name}/` | {measured['passed']} | {measured.get('failed', 0)} "
                f"| {measured.get('skipped', 0)} | {duration:.2f}s "
                f"| {'PASS' if measured.get('exit_code', 1) == 0 else 'FAIL'} |")
        else:
            lines.append(f"| `{name}/` | 未实测 | — | — | — | — |")
    collect_bits = []
    for suite, name in (("tests", "tests"), ("agi", "agi/tests")):
        collected = report["collect"].get(suite, {}).get("collected", -1)
        collect_bits.append(f"`{name}/`={collected if collected >= 0 else 'collect 失败'}")
    lines.append("")
    lines.append("collect 数（现场 `pytest --collect-only -q`，只表示用例面大小、不代表通过数）："
                 + "、".join(collect_bits) + "。")
    lines.append("")
    lines.append("数据来源：`python scripts/agentb_verify.py`（全量终验，两套 + 静态检查 + 生产文件未被污染）"
                 "或 `python tools/status_report.py --run`（单套件实测）；"
                 "`--from-agentb` 可直接导入前者的 JSON，不手抄。")
    lines.append("")
    lines.append(f"代码规模（同一命令可复算）：Python 模块 {report['facts']['py_modules']} 个"
                 f"（agi {report['facts']['agi_py']} / selflearn {report['facts']['selflearn_py']}"
                 f" / agi/tests {report['facts']['agi_tests_py']}）；"
                 f"计划文档 {report['facts']['plans']} 份；"
                 f"legacy 归档 {report['facts']['legacy_files']} 个文件 / "
                 f"{report['facts']['legacy_lines']} 行。")
    lines.append("")
    lines.append(AUTO_END)
    return "\n".join(lines)


def write_status_block(report: dict[str, Any]) -> bool:
    """把自动区块写进 STATUS.md（保留区块外的手写内容）。"""
    block = render_auto_block(report)
    if STATUS_MD.is_file():
        text = STATUS_MD.read_text(encoding="utf-8")
    else:
        text = "# 当前状态（docs/STATUS）\n\n"
    if AUTO_BEGIN in text and AUTO_END in text:
        head = text.split(AUTO_BEGIN, 1)[0]
        tail = text.split(AUTO_END, 1)[1]
        new_text = head + block + tail
    else:
        new_text = text.rstrip() + "\n\n## 自动生成的状态数字\n\n" + block + "\n"
    STATUS_MD.write_text(new_text, encoding="utf-8")
    return new_text != text


def save_measured(suite: str, result: dict[str, Any]) -> dict[str, Any]:
    data = load_measured()
    data.setdefault("suites", {})[suite] = result
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    MEASURED_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8")
    return data


def check_status(report: dict[str, Any]) -> list[str]:
    """校验 STATUS.md 自动区块里的 collect 数与现场一致。"""
    problems: list[str] = []
    if not STATUS_MD.is_file():
        return ["docs/STATUS.md 不存在"]
    text = STATUS_MD.read_text(encoding="utf-8")
    if AUTO_BEGIN not in text or AUTO_END not in text:
        return ["docs/STATUS.md 缺少 STATUS:AUTO 区块（先跑 --run 或手工插入标记）"]
    block = text.split(AUTO_BEGIN, 1)[1].split(AUTO_END, 1)[0]
    for suite, info in report["collect"].items():
        collected = info.get("collected", -1)
        if collected < 0:
            problems.append(f"{suite}: 现场 collect 失败，无法校验")
            continue
        if f"`{SUITES[suite]}/`={collected}" not in block:
            problems.append(f"{suite}: STATUS.md 的 collect 数不是最新的 {collected}"
                            "（跑 python tools/status_report.py --run 或 --refresh）")
    return problems


def import_agentb(path: Path) -> dict[str, Any]:
    """导入 ``scripts/agentb_verify.py --json`` 的终验结果，转成 measured 记录。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    generated = str(data.get("generated_at") or datetime.now().isoformat(timespec="seconds"))
    imported: dict[str, Any] = {}
    for suite, info in (data.get("suites") or {}).items():
        counts = info.get("counts") or {}
        result = {
            "command": "python scripts/agentb_verify.py",
            "exit_code": 0 if info.get("ok") else 1,
            "measured_at": generated,
            "wall_seconds": info.get("elapsed_s", 0),
            "pytest_seconds": info.get("elapsed_s", 0),
            "passed": int(counts.get("passed", -1)),
            "failed": int(counts.get("failed", 0)),
            "skipped": int(counts.get("skipped", 0)),
            "errors": int(counts.get("errors", 0)),
            "source": "scripts/agentb_verify.py",
        }
        save_measured(suite, result)
        imported[suite] = result
    return imported


def main(argv: list[str] | None = None) -> int:
    global ROOT, STATUS_MD, MEASURED_JSON
    parser = argparse.ArgumentParser(description="可复现状态报告（测试数字/代码规模/归档量）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    parser.add_argument("--run", action="store_true", help="真跑测试并回填 docs/STATUS.md")
    parser.add_argument("--suites", default="tests,agi",
                        help="--run 时跑哪些套件：tests,agi（默认两个都跑）")
    parser.add_argument("--refresh", action="store_true", help="只刷新 STATUS.md 的 collect 数/静态事实")
    parser.add_argument("--check", action="store_true", help="校验 STATUS.md 数字是否过期（过期退出 1）")
    parser.add_argument("--from-agentb", default="",
                        help="导入 scripts/agentb_verify.py --json 生成的终验 JSON 并回填 STATUS.md")
    parser.add_argument("--root", default=str(ROOT), help="仓库根目录（测试用）")
    args = parser.parse_args(argv)
    _setup_console()
    ROOT = Path(args.root).resolve()
    STATUS_MD = ROOT / "docs" / "quality" / "STATUS.md"
    MEASURED_JSON = ROOT / "docs" / "quality" / "status_measured.json"

    if args.from_agentb:
        source = Path(args.from_agentb)
        if not source.is_absolute():
            source = ROOT / source
        if not source.is_file():
            print(f"[from-agentb] 找不到 JSON：{source}", file=sys.stderr)
            return 2
        imported = import_agentb(source)
        print(f"[from-agentb] 已导入 {list(imported)}：{source}")

    if args.run:
        wanted = [name.strip() for name in args.suites.split(",") if name.strip()]
        unknown = [name for name in wanted if name not in SUITES]
        if unknown:
            print(f"未知套件：{unknown}（可选 {list(SUITES)}）", file=sys.stderr)
            return 2
        for suite in wanted:
            print(f"[run] {SUITES[suite]} ...", file=sys.stderr, flush=True)
            result = run_suite(suite)
            save_measured(suite, result)
            status = "PASS" if result["exit_code"] == 0 and result.get("failed", 1) == 0 else "FAIL"
            print(f"[run] {suite}: {status} passed={result.get('passed')} "
                  f"failed={result.get('failed')} skipped={result.get('skipped')} "
                  f"({result['wall_seconds']}s)", file=sys.stderr, flush=True)

    report = build_report()
    if args.run or args.refresh or args.from_agentb:
        write_status_block(report)
        label = "run" if args.run else ("from-agentb" if args.from_agentb else "refresh")
        print(f"[{label}] 已回填 {STATUS_MD.relative_to(ROOT)} 的 STATUS:AUTO 区块")
    if args.check:
        problems = check_status(report)
        if problems:
            print("[check] STATUS.md 数字过期：")
            for item in problems:
                print("  -", item)
            return 1
        print("[check] STATUS.md 的 collect 数与现场一致")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    if args.run or args.refresh or args.check or args.from_agentb:
        return 0

    # 默认：人类可读摘要
    print("=" * 66)
    print("SoulAgent 状态报告（tools/status_report.py）")
    print("=" * 66)
    print(f"生成时间：{report['generated_at']}  Python {report['python']}")
    print()
    print("测试（collect 数 = pytest 收集到的用例数；实测结果需 --run 现场跑）")
    for suite in ("tests", "agi"):
        info = report["collect"][suite]
        measured = report["measured"].get(suite)
        collected = info.get("collected", -1)
        measured_text = "未实测"
        if measured and measured.get("passed", -1) >= 0:
            measured_text = (f"{measured['passed']} passed / {measured.get('failed', 0)} failed"
                             f" / {measured.get('skipped', 0)} skipped"
                             f"（{measured.get('measured_at', '')}）")
        print(f"  {SUITES[suite]:9s} collect={collected if collected >= 0 else '失败'}"
              f"    实测={measured_text}")
    print()
    print("静态事实")
    facts = report["facts"]
    print(f"  Python 模块 {facts['py_modules']} 个（agi {facts['agi_py']} / "
          f"selflearn {facts['selflearn_py']} / agi/tests {facts['agi_tests_py']} / "
          f"根 tests {facts['root_tests_py']}）")
    print(f"  计划文档 {facts['plans']} 份（docs/plans/*.md，不含 README）")
    print(f"  legacy 归档 {facts['legacy_files']} 个文件 / {facts['legacy_lines']} 行")
    print()
    print("下一步：")
    print("  python tools/status_report.py --run     # 真跑两套测试并回填 docs/STATUS.md")
    print("  python tools/status_report.py --check   # 校验文档数字是否过期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())