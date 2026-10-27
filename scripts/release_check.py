#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布前检查（release gate）：必备文件 + 密钥扫描 + 大文件 + 全量离线测试。

输出逐项 ``[PASS]/[FAIL]``，最后一行 ``RESULT: PASS|FAIL``；退出码 0/1。

用法::

    python scripts/release_check.py                 # 完整检查（会跑全量测试）
    python scripts/release_check.py --skip-tests    # 只查文件/密钥/大文件（快）
    python scripts/release_check.py --max-mb 20     # 收紧单文件上限
    python scripts/release_check.py --json

设计：只用标准库；在仓库根跑（默认取本脚本上一级），不改任何文件。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Windows 控制台/重定向默认是 cp936，统一成 UTF-8，避免中文在 CI 日志里变乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

#: 开源基线必备文件（缺一个就不算「别人能 clone 能参与」）
REQUIRED_FILES = (
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "docs/OPENSOURCE.md",
    ".github/workflows/ci.yml",
    "tools/check_secrets.py",
    "scripts/release_check.py",
)
#: 百度网盘/网盘分享等大文件阈值（MB）
DEFAULT_MAX_MB = 50.0
#: 测试超时（秒）；CI 是 30 分钟，这里留同样量级
DEFAULT_TIMEOUT_S = 1800
TEST_ARGS = ("agi/tests", "tests", "-q", "--ignore=tests/test_webui.py",
             "-p", "no:cacheprovider")


class Step:
    def __init__(self, name: str):
        self.name = name
        self.ok = False
        self.detail = ""
        self.extra: dict = {}

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": bool(self.ok),
                "detail": self.detail, **self.extra}


def _run(cmd: list[str], *, cwd: Path, timeout: float) -> tuple[int, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        result = subprocess.run(cmd, cwd=str(cwd), capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + "\n" + (exc.stderr or "")
        return 124, f"timeout after {timeout}s\n{output}"[-4000:]
    except OSError as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    return result.returncode, output.strip()


def _tail(text: str, lines: int = 25) -> str:
    rows = [row for row in str(text or "").splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


def check_required_files(root: Path) -> Step:
    step = Step("required files")
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    step.ok = not missing
    step.detail = ("缺少：" + "、".join(missing)) if missing else f"{len(REQUIRED_FILES)} 个文件齐全"
    step.extra = {"missing": missing}
    return step


def check_secrets(root: Path, timeout: float) -> Step:
    step = Step("secrets scan")
    script = root / "tools" / "check_secrets.py"
    if not script.is_file():
        step.ok = False
        step.detail = "tools/check_secrets.py 不存在"
        return step
    code, output = _run([sys.executable, str(script), "--root", str(root)],
                        cwd=root, timeout=timeout)
    step.ok = code == 0
    line = output.splitlines()[-1] if output else f"exit={code}"
    line = line.replace("[PASS]", "").replace("[FAIL]", "").strip()
    if line.lower().startswith("secrets scan:"):
        line = line[len("secrets scan:"):].strip()
    step.detail = line
    if not step.ok:
        step.extra = {"output_tail": _tail(output)}
    return step


def check_large_files(root: Path, max_mb: float) -> Step:
    step = Step(f"tracked files <= {max_mb:g} MB")
    code, output = _run(["git", "-C", str(root), "ls-files", "-z"],
                        cwd=root, timeout=120)
    if code != 0:
        step.detail = f"git ls-files 失败：{_tail(output, 3)}"
        return step
    limit = int(max_mb * 1024 * 1024)
    offenders: list[dict] = []
    for raw in output.split("\x00"):
        if not raw.strip():
            continue
        rel = raw.strip()
        path = root / rel.replace("/", os.sep)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > limit:
            offenders.append({"path": rel, "mb": round(size / 1024 / 1024, 2)})
    step.ok = not offenders
    step.detail = ("未入库大文件：" + "、".join(
        f"{item['path']}({item['mb']}MB)" for item in offenders)
        if offenders else f"已跟踪文件均 ≤ {max_mb:g} MB")
    step.extra = {"offenders": offenders}
    return step


def check_binary_artifacts(root: Path) -> Step:
    """模型/编译产物不应入库（即使体积没超）。"""
    step = Step("no model/binary artifacts")
    bad_suffix = {".onnx", ".pt", ".pth", ".safetensors", ".exe", ".dll", ".so",
                  ".dylib", ".ffmpeg", ".pyc"}
    code, output = _run(["git", "-C", str(root), "ls-files", "-z"],
                        cwd=root, timeout=120)
    if code != 0:
        step.detail = "git ls-files 失败"
        return step
    found: list[str] = []
    for raw in output.split("\x00"):
        if not raw.strip():
            continue
        rel = raw.strip()
        if Path(rel).suffix.lower() in bad_suffix:
            found.append(rel)
    step.ok = not found
    step.detail = ("发现二进制/模型文件：" + "、".join(found[:10])) if found else \
        "已跟踪文件里没有模型/二进制产物（.onnx/.pt/.pth/.exe/.dll/.pyc 等）"
    step.extra = {"found": found[:50]}
    return step


def check_tests(root: Path, timeout: float) -> Step:
    step = Step("offline tests")
    cmd = [sys.executable, "-m", "pytest", *TEST_ARGS]
    code, output = _run(cmd, cwd=root, timeout=timeout)
    step.ok = code == 0
    summary = ""
    for line in reversed(output.splitlines()):
        if " passed" in line or " failed" in line or " error" in line:
            summary = line.strip()
            break
    step.detail = f"exit={code}｜{summary or '见输出'}"
    if not step.ok:
        step.extra = {"output_tail": _tail(output, 30)}
    return step


def run_all(root: Path, *, skip_tests: bool = False, max_mb: float = DEFAULT_MAX_MB,
            timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    steps = [
        check_required_files(root),
        check_secrets(root, timeout=300),
        check_large_files(root, max_mb=max_mb),
        check_binary_artifacts(root),
    ]
    if not skip_tests:
        steps.append(check_tests(root, timeout=timeout))
    ok = all(step.ok for step in steps)
    return {"ok": ok, "root": str(root), "steps": [step.to_dict() for step in steps],
            "skipped_tests": bool(skip_tests)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="发布前检查：必备文件 + 密钥 + 大文件 + 离线测试（PASS/FAIL）")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                        help="仓库根目录（默认本脚本上一级）")
    parser.add_argument("--skip-tests", "--fast", dest="skip_tests",
                        action="store_true",
                        help="快速模式：不跑全量测试（只查必备文件/密钥/大文件）")
    parser.add_argument("--max-mb", type=float, default=DEFAULT_MAX_MB,
                        help=f"单文件大小上限 MB（默认 {DEFAULT_MAX_MB:g}）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help=f"测试超时秒数（默认 {DEFAULT_TIMEOUT_S}）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    report = run_all(root, skip_tests=bool(args.skip_tests),
                     max_mb=float(args.max_mb), timeout=float(args.timeout))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    print(f"release_check · {root}")
    for step in report["steps"]:
        mark = "PASS" if step["ok"] else "FAIL"
        print(f"  [{mark}] {step['name']}: {step['detail']}")
        tail = step.get("output_tail")
        if tail:
            for line in str(tail).splitlines()[-25:]:
                print(f"         {line}")
    if report.get("skipped_tests"):
        print("  [SKIP] offline tests（--fast / --skip-tests：发布前仍应跑一次完整检查）")
    print("RESULT: " + ("PASS" if report["ok"] else "FAIL"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
