#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""密钥/凭证扫描：只扫 **git 已跟踪** 的文件，判断仓库里有没有把真凭证提交进去。

这是发布前的硬闸门（CI 与 ``scripts/release_check.py`` 都会跑）：

- 只读标准库，不安装依赖；
- 只扫 ``git ls-files`` 里的文件（不做全盘扫描，避免把本地未跟踪的配置当泄漏）；
- 二进制文件跳过；单文件最多读 ``MAX_SCAN_BYTES``；
- 命中一行就 ``exit 1``，输出会自动打码（只留首尾几位）；
- 测试夹具里的假 key（如 ``sk-test-...`` / ``SESSDATA=sess-abc``）不会命中：
  所有规则都要求足够的长度与字符集，宁可漏报伪 key，也不让 CI 天天误报。

用法::

    python tools/check_secrets.py            # 仓库根 = 当前脚本的上一级
    python tools/check_secrets.py --root .   # 指定仓库根
    python tools/check_secrets.py --json     # 机器可读输出
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MAX_SCAN_BYTES = 4 * 1024 * 1024
#: 这些后缀不扫（二进制/媒体/编译产物）
BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tif", ".tiff",
    ".mp3", ".wav", ".flac", ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".zip", ".gz", ".tar", ".tgz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".onnx", ".pt", ".pth",
    ".pyc", ".pyo", ".pkl", ".pickle", ".npy", ".npz", ".parquet",
    ".ttf", ".ttc", ".woff", ".woff2", ".otf",
    ".sqlite", ".sqlite3", ".db", ".log",
})
#: (人读名字, 正则, 是否需要忽略大小写)
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # DeepSeek / OpenAI 风格：sk- 后面至少 20 位纯字母数字（真实 key 通常 32+）
    ("api-key(sk-)", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    # GitHub PAT（经典格式 36 位；细粒度 token 以 github_pat_ 开头）
    ("github-pat(ghp_)", re.compile(r"\bghp_[A-Za-z0-9]{30,}")),
    ("github-pat(github_pat_)", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}")),
    # B 站登录 Cookie（SESSDATA 是 32 位 hex/URL 编码串，测试里的 sess-abc 不会命中）
    ("bilibili-SESSDATA", re.compile(r"(?i)\bSESSDATA\s*=\s*[A-Za-z0-9%_.-]{16,}")),
    # Authorization: Bearer <token>（测试里的短假 token 不会命中）
    ("bearer-token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{24,}")),
    # 私钥块
    ("private-key", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    # 常见「赋值式」凭证：api_key/secret/access_token/client_secret = "长串"
    ("credential-assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|client[_-]?secret|"
        r"app[_-]?secret)\b\s*[:=]\s*[\"'][A-Za-z0-9/+_-]{24,}[\"']")),
)
#: 允许出现的「已知占位符」前缀（文档/示例里常见，不算泄漏）
ALLOW_PREFIXES = (
    "sk-xxxx", "sk-your", "sk-test", "sk-demo", "sk-example", "sk-placeholder",
    "ghp_xxxx", "ghp_your", "ghp_example",
    "your_", "your-", "<your", "REPLACE_ME", "CHANGE_ME",
)


def _mask(value: str) -> str:
    text = str(value)
    if len(text) <= 10:
        return text[:2] + "***"
    return f"{text[:6]}…{text[-4:]}"


#: 明显的占位符片段（文档/测试夹具常用顺序字母/数字串，不算真凭证）
PLACEHOLDER_FRAGMENTS = (
    "abcdefghijklmnopqrstuvwxyz",
    "abcdefghijklmnopqrstuvwx",
    "01234567890123456789",
    "12345678901234567890",
    "xxxxxxxxxxxxxxxxxxxx",
)


def _is_text_file(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return False
    try:
        with path.open("rb") as fh:
            chunk = fh.read(8192)
    except OSError:
        return False
    return b"\x00" not in chunk


def _tracked_files(root: Path) -> list[Path]:
    """git ls-files 的绝对路径；不是 git 仓库时返回空 + 说明。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    raw = result.stdout.split(b"\x00")
    files: list[Path] = []
    for item in raw:
        if not item:
            continue
        try:
            name = item.decode("utf-8")
        except UnicodeDecodeError:
            name = item.decode("latin-1", errors="replace")
        files.append(root / name.replace("/", os.sep))
    return files


def _read_text(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size <= 0 or size > MAX_SCAN_BYTES:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _allowed(line: str) -> bool:
    lowered = line.lower()
    if any(prefix.lower() in lowered for prefix in ALLOW_PREFIXES):
        return True
    return any(fragment in lowered for fragment in PLACEHOLDER_FRAGMENTS)


def scan(root: Path) -> list[dict]:
    """返回命中列表：``[{pattern, path, line, text}]``（text 已打码）。"""
    findings: list[dict] = []
    for path in _tracked_files(root):
        if not _is_text_file(path):
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        text = _read_text(path)
        if not text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            for name, pattern in RULES:
                match = pattern.search(line)
                if not match:
                    continue
                if _allowed(line):
                    continue
                findings.append({"pattern": name, "path": str(rel),
                                 "line": lineno, "text": _mask(match.group(0))})
                break
    return findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="扫描 git 已跟踪文件里的密钥/凭证（标准库实现，CI 硬闸门）")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                        help="仓库根目录（默认本脚本上一级）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not (root / ".git").exists():
        payload = {"ok": False, "error": f"不是 git 仓库：{root}", "findings": []}
        print(json.dumps(payload, ensure_ascii=False) if args.json
              else f"[FAIL] {payload['error']}")
        return 1
    findings = scan(root)
    if args.json:
        print(json.dumps({"ok": not findings, "root": str(root),
                          "findings": findings}, ensure_ascii=False, indent=2))
    elif not findings:
        print(f"[PASS] secrets scan: {root} 的已跟踪文件里没发现密钥/凭证")
    else:
        print(f"[FAIL] secrets scan: 发现 {len(findings)} 处疑似密钥")
        for item in findings:
            print(f"  - {item['path']}:{item['line']} [{item['pattern']}] {item['text']}")
        print("  处理：删除并轮换该凭证；测试夹具请用明显的假值（如 sk-test-xxx）。")
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
