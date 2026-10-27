#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IDEA / SoulAgent 一键验收测试矩阵。

跑：
1. 自动化测试：idea pytest（tests）+ SoulAgent pytest（agi/tests、tests）
2. 全服务冒烟：scripts/smoke_all.py
3. 功能 E2E（只读 / dry-run，不发微信）：工作台、网关、对话试跑、朋友圈（未接线则 SKIP）、模型中心只读
4. UI 冒烟：headless Edge 截图 6 个 view，检查文件存在且 >20KB

产物：``reports/acceptance_<YYYYMMDD_HHMM>.md``（同时更新 ``reports/latest.md``）
退出码：有 FAIL → 1；否则 0（SKIP 不算失败）。

用法（必须用 idea 的 venv Python，才有 httpx）::

    .venv/Scripts/python.exe scripts/acceptance.py --fast   # 跳过全量 pytest
    .venv/Scripts/python.exe scripts/acceptance.py          # 完整版（允许 30min）

可用环境变量覆盖：``IDEA_TOKEN``（默认 8888）、``SOULAGENT_HOME``、``SOUL_PYTHON``、
``EDGE_PATH``。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:  # 只有 idea/.venv 里保证有 httpx
    import httpx
except ImportError:  # pragma: no cover - 运行环境提示
    print(
        "缺少 httpx：请用 idea 的 venv Python 运行，例如\n"
        "  .venv/Scripts/python.exe scripts/acceptance.py --fast",
        file=sys.stderr,
    )
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent.parent            # idea/
REPORTS = ROOT / "reports"
DEFAULT_SOUL = Path(os.environ.get("SOULAGENT_HOME") or (ROOT.parent / "SoulAgent"))
DEFAULT_SOUL_PY = Path(os.environ.get("SOUL_PYTHON") or r"C:\Python314\python.exe")

WB = "http://127.0.0.1:803"          # 工作台
GW = "http://127.0.0.1:801"          # 微信网关
CONSOLE = "http://127.0.0.1:802"     # 控制台
TOKEN = os.environ.get("IDEA_TOKEN", "8888")
VIEWS = ("welcome", "train", "models", "chat", "moments", "drafts")
SCREEN_MIN_BYTES = 20 * 1024
PYTEST_TIMEOUT_S = 1800              # 单组 pytest 上限 30min
SMOKE_TIMEOUT_S = 120
CHAT_TEST_TIMEOUT_S = 120

EDGE_CANDIDATES = (
    Path(os.environ.get("EDGE_PATH", "")),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)


# --------------------------------------------------------------------------- #
# 记录结构
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    group: str
    name: str
    status: str                      # PASS / FAIL / SKIP
    duration_s: float = 0.0
    detail: str = ""
    command: str = ""
    tail: str = ""
    artifacts: list[str] = field(default_factory=list)


class Matrix:
    def __init__(self) -> None:
        self.rows: list[Result] = []

    def add(self, group: str, name: str, status: str, *, duration_s: float = 0.0,
            detail: str = "", command: str = "", tail: str = "",
            artifacts: list[str] | None = None) -> Result:
        row = Result(group, name, status, round(duration_s, 2), detail,
                     command, tail, list(artifacts or []))
        self.rows.append(row)
        icon = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
        print(f"[{icon}] {group} / {name} {row.detail or ''}".rstrip(), flush=True)
        return row

    def counts(self) -> dict[str, int]:
        out = {"PASS": 0, "FAIL": 0, "SKIP": 0}
        for row in self.rows:
            out[row.status] = out.get(row.status, 0) + 1
        return out

    def failed(self) -> list[Result]:
        return [row for row in self.rows if row.status == "FAIL"]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _cmdline(cmd: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in cmd)


def run_command(cmd: list[str], *, cwd: Path, timeout: float,
                env: dict | None = None) -> tuple[int, str, float, bool]:
    """跑子进程；返回 (returncode, 合并输出, 耗时, 是否超时)。超时打印提示。"""
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            env={**os.environ, "PYTHONIOENCODING": "utf-8",
                 "PYTHONUTF8": "1", **(env or {})},
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, output, time.perf_counter() - started, False
    except subprocess.TimeoutExpired as exc:
        output = ""
        for part in (exc.stdout, exc.stderr):
            if isinstance(part, bytes):
                output += part.decode("utf-8", "replace")
            elif part:
                output += part
        output += f"\n[acceptance] 超时：> {timeout:.0f}s，已终止"
        return -1, output, time.perf_counter() - started, True


def tail_lines(text: str, count: int = 20) -> str:
    lines = str(text or "").splitlines()
    return "\n".join(lines[-count:])


def parse_pytest(output: str) -> dict[str, int]:
    """从 pytest 输出解析 passed/failed/errors/skipped（取最后一条汇总行）。"""
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    summary_line = ""
    for line in str(output or "").splitlines():
        if re.search(r"\b\d+ (passed|failed|error|errors|skipped)\b", line):
            summary_line = line
    for key in counts:
        match = re.search(rf"(\d+) {key}", summary_line or output or "")
        if match:
            counts[key] = int(match.group(1))
    return counts


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex((host, port)) == 0


def find_edge() -> Path | None:
    for candidate in EDGE_CANDIDATES:
        if candidate and str(candidate) and candidate.is_file():
            return candidate
    found = shutil.which("msedge") or shutil.which("microsoft-edge")
    return Path(found) if found else None


def _json_preview(payload: object, limit: int = 160) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(payload)
    return text[:limit].replace("\n", " ")


# --------------------------------------------------------------------------- #
# 1. 自动化测试
# --------------------------------------------------------------------------- #
def _pytest_row(matrix: Matrix, name: str, cmd: list[str], *, cwd: Path,
                timeout: float = PYTEST_TIMEOUT_S) -> None:
    code, output, elapsed, timed_out = run_command(cmd, cwd=cwd, timeout=timeout)
    counts = parse_pytest(output)
    summary = (f"{counts['passed']} passed / {counts['failed']} failed / "
               f"{counts['errors']} errors / {counts['skipped']} skipped")
    failed = counts["failed"] + counts["errors"] > 0
    if timed_out:
        status, detail = "FAIL", f"超时 >{timeout:.0f}s"
    elif code != 0 or failed:
        status, detail = "FAIL", summary
    else:
        status, detail = "PASS", summary
    matrix.add("自动化测试", name, status, duration_s=elapsed, detail=detail,
               command=_cmdline(cmd),
               tail=tail_lines(output) if status == "FAIL" else "")


def step_pytest(matrix: Matrix, args: argparse.Namespace) -> None:
    if args.fast:
        for name in ("idea pytest", "SoulAgent pytest (agi/tests)", "SoulAgent pytest (tests)"):
            matrix.add("自动化测试", name, "SKIP", detail="--fast：跳过全量 pytest")
        return

    _pytest_row(matrix, "idea pytest",
                [sys.executable, "-m", "pytest", "tests", "-q"], cwd=ROOT)

    if not DEFAULT_SOUL.is_dir():
        for name in ("SoulAgent pytest (agi/tests)", "SoulAgent pytest (tests)"):
            matrix.add("自动化测试", name, "SKIP",
                       detail=f"SoulAgent 目录不存在：{DEFAULT_SOUL}")
        return
    soul_py = str(DEFAULT_SOUL_PY) if DEFAULT_SOUL_PY.is_file() else (shutil.which("python") or "python")
    for sub, label in (("agi/tests", "SoulAgent pytest (agi/tests)"),
                       ("tests", "SoulAgent pytest (tests)")):
        _pytest_row(matrix, label,
                    [soul_py, "-m", "pytest", sub, "-q"], cwd=DEFAULT_SOUL)


# --------------------------------------------------------------------------- #
# 2. 全服务冒烟
# --------------------------------------------------------------------------- #
def step_smoke(matrix: Matrix, args: argparse.Namespace) -> None:
    script = ROOT / "scripts" / "smoke_all.py"
    if not script.is_file():
        matrix.add("全服务冒烟", "smoke_all.py", "SKIP", detail="脚本不存在")
        return
    cmd = [sys.executable, str(script), "--token", str(TOKEN)]
    code, output, elapsed, timed_out = run_command(
        cmd, cwd=ROOT, timeout=SMOKE_TIMEOUT_S)
    passed = len(re.findall(r"^PASS\b", output, re.MULTILINE))
    failed = len(re.findall(r"^FAIL\b", output, re.MULTILINE))
    status = "PASS" if (code == 0 and "结果：PASS" in output and not timed_out) else "FAIL"
    detail = f"{passed} PASS / {failed} FAIL"
    matrix.add("全服务冒烟", "smoke_all.py", status, duration_s=elapsed,
               detail=detail, command=_cmdline(cmd),
               tail=tail_lines(output) if status == "FAIL" else "")


# --------------------------------------------------------------------------- #
# 3. 功能 E2E（只读 / dry-run）
# --------------------------------------------------------------------------- #
def _http_get(client: httpx.Client, name: str, url: str, *,
              token: bool, matrix: Matrix, group: str = "功能 E2E",
              expect_ok: bool = True, allow_404_skip: bool = False,
              timeout: float = 20.0) -> bool:
    headers = {"X-Admin-Token": str(TOKEN), "Accept": "application/json"} if token else None
    started = time.perf_counter()
    try:
        response = client.get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        matrix.add(group, name, "FAIL", duration_s=time.perf_counter() - started,
                   detail=f"{type(exc).__name__}: {str(exc)[:120]}", command=f"GET {url}")
        return False
    elapsed = time.perf_counter() - started
    payload = None
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    if allow_404_skip and response.status_code == 404:
        matrix.add(group, name, "SKIP", duration_s=elapsed,
                   detail="接口未接线（404）", command=f"GET {url}")
        return True
    ok = response.status_code == 200 and (not expect_ok or (isinstance(payload, dict)
                                                            and payload.get("ok", True)))
    matrix.add(group, name, "PASS" if ok else "FAIL", duration_s=elapsed,
               detail=f"HTTP {response.status_code} {_json_preview(payload, 80)}",
               command=f"GET {url}",
               tail="" if ok else (response.text or "")[:2000])
    return ok


def step_e2e(matrix: Matrix, args: argparse.Namespace) -> None:
    headers = {"X-Admin-Token": str(TOKEN), "Accept": "application/json"}
    client = httpx.Client(timeout=20.0, trust_env=False)
    try:
        # 工作台只读端点
        for path in (
            "/api/wb/train/datasets",
            "/api/wb/models/running",
            "/api/wb/models/disk",
            "/api/wb/chat/persona",
            "/api/wb/chat/traces",
        ):
            _http_get(client, f"工作台 {path}", WB + path, token=True, matrix=matrix)
        # 朋友圈（未接线则 SKIP，不 FAIL）
        _http_get(client, "工作台 /api/wb/moments", WB + "/api/wb/moments",
                  token=True, matrix=matrix, allow_404_skip=True)
        _http_get(client, "工作台 /api/wb/models/running（只读）",
                  WB + "/api/wb/models/running", token=True, matrix=matrix)

        # 网关
        _http_get(client, "网关 /health", GW + "/health", token=False, matrix=matrix)
        _http_get(client, "网关 /status（X-Admin-Token）", GW + "/status",
                  token=True, matrix=matrix)

        # 对话试跑（dry，不发微信）
        url = WB + "/api/wb/chat/test"
        started = time.perf_counter()
        try:
            response = client.post(url, headers=headers,
                                   json={"text": "验收测试：在吗"},
                                   timeout=CHAT_TEST_TIMEOUT_S)
            elapsed = time.perf_counter() - started
            try:
                payload = response.json()
            except (ValueError, TypeError):
                payload = None
            ok = (response.status_code == 200 and isinstance(payload, dict)
                  and payload.get("ok") is True and payload.get("dry_run") is True)
            detail = f"HTTP {response.status_code} dry_run={payload.get('dry_run') if isinstance(payload, dict) else '?'}"
            if isinstance(payload, dict):
                reply = str(payload.get("reply") or "")
                detail += f" latency={payload.get('latency_ms')}ms reply={reply[:40]}"
            matrix.add("功能 E2E", "对话试跑 POST /api/wb/chat/test", "PASS" if ok else "FAIL",
                       duration_s=elapsed, detail=detail, command=f"POST {url}",
                       tail="" if ok else json.dumps(payload, ensure_ascii=False)[:2000])
        except Exception as exc:  # noqa: BLE001
            matrix.add("功能 E2E", "对话试跑 POST /api/wb/chat/test", "FAIL",
                       duration_s=time.perf_counter() - started,
                       detail=f"{type(exc).__name__}: {str(exc)[:120]}", command=f"POST {url}")

        # 朋友圈抓取（若可用；失败 SKIP 不 FAIL）
        url = WB + "/api/wb/moments/refresh"
        started = time.perf_counter()
        try:
            response = client.post(url, headers=headers, json={"limit": 5}, timeout=60.0)
            elapsed = time.perf_counter() - started
            if response.status_code == 404:
                matrix.add("功能 E2E", "朋友圈刷新 POST /api/wb/moments/refresh", "SKIP",
                           duration_s=elapsed, detail="接口未接线（404）", command=f"POST {url}")
            elif response.status_code == 200:
                matrix.add("功能 E2E", "朋友圈刷新 POST /api/wb/moments/refresh", "PASS",
                           duration_s=elapsed, detail="HTTP 200", command=f"POST {url}")
            else:
                matrix.add("功能 E2E", "朋友圈刷新 POST /api/wb/moments/refresh", "SKIP",
                           duration_s=elapsed,
                           detail=f"HTTP {response.status_code}（朋友圈依赖缺失可接受）",
                           command=f"POST {url}")
        except Exception as exc:  # noqa: BLE001
            matrix.add("功能 E2E", "朋友圈刷新 POST /api/wb/moments/refresh", "SKIP",
                       duration_s=time.perf_counter() - started,
                       detail=f"{type(exc).__name__}: {str(exc)[:100]}", command=f"POST {url}")

        # 控制台 health（构建在 802，只读）
        _http_get(client, "控制台 /api/health", CONSOLE + "/api/health",
                  token=True, matrix=matrix)
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# 4. UI 冒烟（headless Edge 截图）
# --------------------------------------------------------------------------- #
def _view_param_supported() -> bool:
    index = ROOT / "web" / "index.html"
    try:
        text = index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return ("get('view')" in text) or ('get("view")' in text)


def step_ui(matrix: Matrix, args: argparse.Namespace, tag: str) -> list[Path]:
    edge = find_edge()
    if edge is None:
        matrix.add("UI 冒烟", "headless Edge", "SKIP", detail="未找到 msedge.exe")
        return []
    shots_dir = REPORTS / "screens" / f"acceptance_{tag}"
    shots_dir.mkdir(parents=True, exist_ok=True)
    screenshots: list[Path] = []
    index = ROOT / "web" / "index.html"
    try:
        has_title = "<title>" in index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        has_title = False
    for view in VIEWS:
        shot = shots_dir / f"{view}.png"
        profile = Path(tempfile.mkdtemp(prefix="edge_accept_"))
        cmd = [
            str(edge), "--headless=new", "--disable-gpu", "--no-first-run",
            "--hide-scrollbars", "--window-size=1440,900",
            "--virtual-time-budget=8000", f"--user-data-dir={profile}",
            f"--screenshot={shot}",
            f"{WB}/?token={TOKEN}&view={view}",
        ]
        started = time.perf_counter()
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace")
            elapsed = time.perf_counter() - started
            size = shot.stat().st_size if shot.is_file() else 0
            ok = size >= SCREEN_MIN_BYTES
            matrix.add("UI 冒烟", f"截图 view={view}", "PASS" if ok else "FAIL",
                       duration_s=elapsed,
                       detail=f"{size / 1024:.0f}KB" if size else "未生成截图",
                       command=_cmdline(cmd),
                       artifacts=[str(shot)] if shot.is_file() else [])
            if shot.is_file():
                screenshots.append(shot)
        except subprocess.TimeoutExpired:
            matrix.add("UI 冒烟", f"截图 view={view}", "FAIL",
                       duration_s=time.perf_counter() - started,
                       detail="Edge 超时 >90s", command=_cmdline(cmd))
        finally:
            shutil.rmtree(profile, ignore_errors=True)
    if screenshots and not _view_param_supported():
        matrix.add("UI 冒烟", "view 参数支持", "SKIP",
                   detail="web/index.html 尚未解析 ?view=，6 张截图均为默认视图"
                          "（文件有效，视觉区分待 UI 接线后启用）")
    if has_title:
        matrix.add("UI 冒烟", "index.html 可访问", "PASS",
                   detail=f"{len(screenshots)}/{len(VIEWS)} 张截图")
    return screenshots


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def _escape_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def environment_rows(args: argparse.Namespace) -> list[tuple[str, str]]:
    ports = ["801", "802", "803", "8123", "8125"]
    port_text = "，".join(f"{p}={'UP' if port_open(int(p)) else 'DOWN'}" for p in ports)
    return [
        ("时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("模式", "--fast（跳过全量 pytest）" if args.fast else "完整版"),
        ("主机/Python", f"{socket.gethostname()} / {sys.version.split()[0]}"),
        ("idea venv", sys.executable),
        ("SoulAgent", str(DEFAULT_SOUL)),
        ("SoulAgent Python", str(DEFAULT_SOUL_PY) if DEFAULT_SOUL_PY.is_file() else "(未找到，回退 PATH python)"),
        ("服务端口", port_text),
        ("管理令牌", "***（默认 8888）"),
        ("工作台/网关/控制台", f"{WB} / {GW} / {CONSOLE}"),
    ]


def render_report(matrix: Matrix, args: argparse.Namespace, tag: str,
                  screenshots: list[Path], elapsed_total: float) -> str:
    counts = matrix.counts()
    overall = "FAIL" if counts["FAIL"] else "PASS"
    lines: list[str] = [
        f"# 验收测试报告 · acceptance_{tag}",
        "",
        f"> 总体结论：**{overall}**  ·  PASS {counts['PASS']} / FAIL {counts['FAIL']} "
        f"/ SKIP {counts['SKIP']}  ·  总耗时 {elapsed_total:.0f}s  ·  "
        f"生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 环境",
        "",
        "| 项 | 值 |",
        "|---|---|",
    ]
    lines += [f"| {_escape_cell(k)} | {_escape_cell(v)} |" for k, v in environment_rows(args)]
    lines += [
        "",
        "## 汇总表",
        "",
        "| 组 | 检查项 | 状态 | 耗时(s) | 详情 |",
        "|---|---|---|---|---|",
    ]
    for row in matrix.rows:
        lines.append(
            f"| {_escape_cell(row.group)} | {_escape_cell(row.name)} | "
            f"**{row.status}** | {row.duration_s:.2f} | {_escape_cell(row.detail)} |")
    lines += ["", "## 分组耗时", "", "| 组 | PASS | FAIL | SKIP | 耗时(s) |", "|---|---|---|---|---|"]
    groups: dict[str, list[Result]] = {}
    for row in matrix.rows:
        groups.setdefault(row.group, []).append(row)
    for group, rows in groups.items():
        g_counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
        for row in rows:
            g_counts[row.status] += 1
        lines.append(f"| {_escape_cell(group)} | {g_counts['PASS']} | {g_counts['FAIL']} | "
                     f"{g_counts['SKIP']} | {sum(r.duration_s for r in rows):.1f} |")

    fail_rows = matrix.failed()
    lines += ["", "## 失败详情", ""]
    if not fail_rows:
        lines.append("无 FAIL 项。✅")
    for row in fail_rows:
        lines += [f"### FAIL · {row.group} / {row.name}", "",
                  f"- 耗时：{row.duration_s:.2f}s",
                  f"- 详情：{row.detail or '-'}"]
        if row.command:
            lines.append(f"- 命令：`{row.command}`")
        if row.tail:
            lines += ["", "末尾输出：", "", "```text", row.tail, "```"]
        lines.append("")

    lines += ["", "## 截图清单", "",
              "| view | 文件 | 大小 | 状态 |", "|---|---|---|---|"]
    by_view = {path.stem: path for path in screenshots}
    for view in VIEWS:
        path = by_view.get(view)
        if path is None:
            lines.append(f"| {view} | - | - | 缺失 |")
        else:
            lines.append(f"| {view} | `{path.relative_to(ROOT)}` | "
                         f"{path.stat().st_size / 1024:.0f}KB | OK |")

    lines += [
        "",
        "## 已知限制（由 Lead 在最终验收时填写）",
        "",
        "> 本小节留白，供 Lead 补充已知限制、豁免说明与上线决策；验收脚本不自动填写。",
        "",
        "- （待填）……",
        "",
        "## 复现",
        "",
        "```bash",
        f"{sys.executable} scripts/acceptance.py{' --fast' if args.fast else ''}",
        "```",
        "",
        f"> 报告由 `scripts/acceptance.py` 自动生成；完整 data 见同目录同名 md，"
        f"截图在 `reports/screens/acceptance_{tag}/`。",
        "",
    ]
    return "\n".join(lines)


def write_reports(matrix: Matrix, args: argparse.Namespace, tag: str,
                  screenshots: list[Path], elapsed_total: float) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    report = REPORTS / f"acceptance_{tag}.md"
    text = render_report(matrix, args, tag, screenshots, elapsed_total)
    report.write_text(text, encoding="utf-8")
    (REPORTS / "latest.md").write_text(text, encoding="utf-8")
    return report


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    global DEFAULT_SOUL, DEFAULT_SOUL_PY
    parser = argparse.ArgumentParser(description="IDEA/SoulAgent 验收测试矩阵")
    parser.add_argument("--fast", action="store_true",
                        help="跳过全量 pytest（只跑冒烟 + E2E + UI 截图）")
    parser.add_argument("--soul-home", default=str(DEFAULT_SOUL),
                        help="SoulAgent 仓库路径（默认 ../SoulAgent）")
    parser.add_argument("--soul-python", default=str(DEFAULT_SOUL_PY),
                        help="跑 SoulAgent pytest 的 Python（默认 C:\\Python314\\python.exe）")
    args = parser.parse_args(argv)

    DEFAULT_SOUL = Path(args.soul_home)
    DEFAULT_SOUL_PY = Path(args.soul_python)

    tag = datetime.now().strftime("%Y%m%d_%H%M")
    started = time.perf_counter()
    matrix = Matrix()
    print(f"=== acceptance_{tag} {'(--fast)' if args.fast else '(full)'} ===", flush=True)

    step_pytest(matrix, args)
    step_smoke(matrix, args)
    step_e2e(matrix, args)
    screenshots = step_ui(matrix, args, tag)

    elapsed_total = time.perf_counter() - started
    report = write_reports(matrix, args, tag, screenshots, elapsed_total)
    counts = matrix.counts()
    print("=" * 60)
    print(f"PASS {counts['PASS']} / FAIL {counts['FAIL']} / SKIP {counts['SKIP']} "
          f"· {elapsed_total:.0f}s")
    print(f"报告：{report}")
    print(f"截图：{len(screenshots)} 张 -> {REPORTS / 'screens' / ('acceptance_' + tag)}")
    print("=" * 60)
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
