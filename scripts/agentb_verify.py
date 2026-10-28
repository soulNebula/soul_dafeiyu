# -*- coding: utf-8 -*-
"""Agent B 终验脚本：一次跑完「两套测试 + 静态检查 + 生产文件未被测试污染」并输出证据表。

为什么要有它：
- 本项目的核心治理问题是「文档/口头数字不可复现」。这个脚本把最终验收固化成一条命令，
  任何人在任意时刻重跑都能得到同一组数字（写进 docs/STATUS.md 与 docs/agent-b/SCORECARD.md）。
- 测试隔离修复的验收标准是「测试跑完，生产敏感文件一个字节都没变」。脚本在跑测试前后
  对生产配置做 sha256 快照比对，把「没污染」做成可执行断言（live 日志不算，因为生产
  服务在跑、日志本来就会被写）。

用法::

    python scripts/agentb_verify.py                # 全量：根 tests + agi/tests + 全部静态检查
    python scripts/agentb_verify.py --quick        # 只跑根 tests + 静态检查（agi/tests 约 15-20 分钟）
    python scripts/agentb_verify.py --json out.json

注意：脚本本身不修改任何项目/生产文件；只读 + 跑 pytest。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

# Windows 控制台默认 GBK：证据表里有 ✅/❌，直接 print 会 UnicodeEncodeError。
# 统一把 stdout/stderr 切到 UTF-8（不可重配时降级 replace，绝不让终验脚本自己崩）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 老 Python/被重定向的流
        pass


# 生产敏感文件（测试绝不允许改）：只放「不应被测试写」的配置类文件。
# 不含 log/messages.jsonl、gateway.log 等 live 运行日志——生产服务正在写它们，
# 变化不能归因于测试。
PROD_SENSITIVE = (
    "config.json",
    "web_config.json",
    "agi/data/wechat/config.json",
)


def _prod_root() -> Path:
    """worktree 场景下生产树是同级的 SoulAgent；否则就是仓库自身。"""
    sibling = ROOT.parent / "SoulAgent"
    if ROOT.name.endswith("-b") and sibling.is_dir():
        return sibling
    return ROOT


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError:
        return "unreadable"


def snapshot_prod() -> dict[str, str | None]:
    prod = _prod_root()
    return {rel: _digest(prod / rel) for rel in PROD_SENSITIVE}


def _run(cmd: list[str], *, timeout: int | None = None) -> dict:
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)
        return {
            "cmd": " ".join(cmd),
            "returncode": int(proc.returncode),
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "elapsed_s": round(time.time() - started, 1),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "cmd": " ".join(cmd),
            "returncode": -1,
            "stdout": (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            "stderr": f"TIMEOUT after {timeout}s",
            "elapsed_s": round(time.time() - started, 1),
        }


_COUNT_RE = re.compile(
    r"(?:(?P<passed>\d+) passed)?(?:, )?(?:(?P<failed>\d+) failed)?"
    r"(?:, )?(?:(?P<skipped>\d+) skipped)?"
    r"(?:, )?(?:(?P<errors>\d+) error)?"
)


def parse_pytest_summary(stdout: str) -> dict:
    """从 pytest 最后几行提取计数（中文环境/英文环境都能吃）。"""
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    tail = "\n".join((stdout or "").strip().splitlines()[-5:])
    for key in counts:
        match = re.search(rf"(\d+) {key}", tail)
        if match:
            counts[key] = int(match.group(1))
    return counts


def _check_tool(rel: str, *args: str) -> dict:
    path = ROOT / rel
    if not path.is_file():
        return {"cmd": rel, "returncode": None, "skipped": "not_found",
                "elapsed_s": 0.0}
    return _run([PY, str(path), *args], timeout=300)


def run_suite(name: str, args: list[str]) -> dict:
    result = _run([PY, "-m", "pytest", *args, "-q"], timeout=None)
    result["suite"] = name
    result["counts"] = parse_pytest_summary(result.get("stdout", ""))
    result["ok"] = result["returncode"] == 0
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="跳过 agi/tests（约 15-20 分钟）")
    parser.add_argument("--json", dest="json_path", default="",
                        help="把完整证据写成 JSON")
    args = parser.parse_args(argv)

    before = snapshot_prod()
    report: dict = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "root": str(ROOT),
        "python": PY,
        "suites": {},
        "checks": {},
        "prod_files": {"before": before, "after": None, "unchanged": None},
    }

    report["suites"]["tests"] = run_suite("tests", ["tests"])
    if not args.quick:
        report["suites"]["agi"] = run_suite("agi", ["agi/tests"])

    for rel, extra in (
        ("tools/check_docs_links.py", ()),
        ("tools/check_portability.py", ()),
        ("tools/check_secrets.py", ()),
        ("tools/check_architecture.py", ()),
    ):
        report["checks"][rel] = _check_tool(rel, *extra)

    after = snapshot_prod()
    report["prod_files"]["after"] = after
    report["prod_files"]["unchanged"] = before == after

    # ---- 人类可读证据表 ----
    lines = ["# Agent B 终验证据", "",
             f"> 生成：{report['generated_at']}  |  仓库：{ROOT}", "",
             "## 测试", "", "| 套件 | 通过 | 失败 | 跳过 | 耗时 | 结果 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for key in ("tests", "agi"):
        suite = report["suites"].get(key)
        if not suite:
            continue
        counts = suite["counts"]
        lines.append(
            f"| {key} | {counts['passed']} | {counts['failed']} | "
            f"{counts['skipped']} | {suite['elapsed_s']}s | "
            f"{'PASS' if suite['ok'] else 'FAIL'} |")
    lines += ["", "## 静态检查", "", "| 检查 | 退出码 | 结果 |", "| --- | --- | --- |"]
    for rel, result in report["checks"].items():
        code = result.get("returncode")
        status = "not_found" if code is None else ("PASS" if code == 0 else "FAIL")
        lines.append(f"| {rel} | {code} | {status} |")
    lines += ["", "## 生产文件未被测试污染", "",
              f"- 快照文件：{', '.join(PROD_SENSITIVE)}",
              f"- 结论：{'未被修改 ✅' if report['prod_files']['unchanged'] else '发生变化 ❌'}"]

    text = "\n".join(lines)
    print(text)

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 证据已写入 {args.json_path}")

    failed = [s for s in report["suites"].values() if not s["ok"]]
    failed += [c for c in report["checks"].values()
               if c.get("returncode") not in (0, None)]
    if not report["prod_files"]["unchanged"]:
        failed.append({"cmd": "prod_untouched"})
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())