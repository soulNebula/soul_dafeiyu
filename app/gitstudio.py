# -*- coding: utf-8 -*-
"""Git Studio：对「被管理的 SoulAgent 仓库」做 status/diff/commit/push。

设计：工作台自己是一个仓库（idea/），被管理的项目是另一个仓库（SoulAgent/）。
这里的操作默认作用在 SoulAgent 上（可用 GIT_REPO 覆盖）。

M1-C 增强（2026-10-28）：
- 工作台级 Git 设置 ``.data/git.json``（auto_commit / auto_push / remote /
  branch_mode(fixed|daily|per-change) / branch / push_retry / block_secrets）；
- push_default 支持 daily 分支自动创建/切换、推送前 fetch、失败重试；
- 提交前 secret_scan 拦截密钥（sk-/ghp_/api_key/SESSDATA/Bearer）；
- auto_commit 按设置决定是否 push；
- status 增加 staged/unstaged/untracked 分类。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import paths

DEFAULT_TIMEOUT = 30

#: 工作台 Git 设置的默认值（.data/git.json 不存在时使用）
DEFAULT_SETTINGS: dict = {
    "auto_commit": True,
    "auto_push": False,
    "remote": "origin",
    "branch_mode": "fixed",      # fixed | daily | per-change
    "branch": "main",
    "push_retry": 3,
    "block_secrets": True,
}
BRANCH_MODES = ("fixed", "daily", "per-change")

#: secret_scan 只扫这些后缀（``.env`` 文件名单独判断）
SECRET_SCAN_SUFFIXES = (".py", ".json", ".env", ".md", ".yaml", ".yml", ".toml", ".ini")
#: 跳过的目录片段：大目录/数据目录/虚拟环境（相对路径匹配）
SECRET_SKIP_PARTS = (
    ".git", ".data", "node_modules", ".venv", "venv", "__pycache__",
    "agi/data", "agi\\data", "data/wechat", "data\\wechat",
)
SECRET_MAX_BYTES = 1_000_000
SECRET_PATTERNS = (
    ("sk_token", re.compile(r"sk-[A-Za-z0-9]{16,}")),
    ("github_pat", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    # 按任务口径：api_key = / api-key: 均命中
    ("api_key", re.compile(r"(?i)api[_-]?key\s*[:=]")),
    ("sessdata", re.compile(r"(?i)SESSDATA\s*=")),
    ("bearer", re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]{20,}")),
)


def repo_path() -> Path:
    raw = os.environ.get("GIT_REPO", "")
    return Path(raw) if raw else paths.soulagent_home()


def _run(args: list[str], *, cwd: Path | None = None, timeout: int = DEFAULT_TIMEOUT):
    cwd = cwd or repo_path()
    try:
        p = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return {"ok": p.returncode == 0, "stdout": p.stdout, "stderr": p.stderr,
                "code": p.returncode}
    except FileNotFoundError:
        return {"ok": False, "stdout": "", "stderr": "git 未安装或不在 PATH", "code": -1}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "git 超时", "code": -2}


def is_repo() -> bool:
    return _run(["rev-parse", "--git-dir"])["ok"]


def _current_branch() -> str:
    r = _run(["rev-parse", "--abbrev-ref", "HEAD"])
    return (r["stdout"] or "").strip() if r["ok"] else ""


# --------------------------------------------------------------------------- #
# 设置：.data/git.json
# --------------------------------------------------------------------------- #
def settings_path() -> Path:
    """工作台 Git 设置文件（idea/.data/git.json，gitignore）。"""
    return paths.DATA_DIR / "git.json"


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    return default


def _normalise_settings(raw) -> dict:
    """把任意输入规整成完整、类型正确的设置（非法值回默认）。"""
    data = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        for key in DEFAULT_SETTINGS:
            if key in raw:
                data[key] = raw[key]
    data["auto_commit"] = _as_bool(data.get("auto_commit"), True)
    data["auto_push"] = _as_bool(data.get("auto_push"), False)
    data["block_secrets"] = _as_bool(data.get("block_secrets"), True)
    remote = str(data.get("remote") or "").strip()
    data["remote"] = remote or str(DEFAULT_SETTINGS["remote"])
    mode = str(data.get("branch_mode") or "").strip().lower()
    data["branch_mode"] = mode if mode in BRANCH_MODES else str(DEFAULT_SETTINGS["branch_mode"])
    branch = str(data.get("branch") or "").strip()
    data["branch"] = branch or str(DEFAULT_SETTINGS["branch"])
    try:
        retry = int(data.get("push_retry"))
    except (TypeError, ValueError):
        retry = int(DEFAULT_SETTINGS["push_retry"])
    data["push_retry"] = max(0, min(10, retry))
    return data


def settings() -> dict:
    """读取工作台 Git 设置（缺文件/坏 JSON → 默认值，不写盘）。"""
    path = settings_path()
    raw = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return _normalise_settings(raw)


def update_settings(patch: dict) -> dict:
    """合并写入设置；未知字段忽略、非法值回默认；返回完整设置。"""
    current = settings()
    if isinstance(patch, dict):
        current.update(patch)
    data = _normalise_settings(current)
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return data
    return data


# --------------------------------------------------------------------------- #
# status / diff
# --------------------------------------------------------------------------- #
def status() -> dict:
    if not is_repo():
        return {"ok": False, "error": "not_a_repo", "path": str(repo_path())}
    s = _run(["status", "--porcelain=v1", "--branch"])
    lines = [ln for ln in (s["stdout"] or "").splitlines() if ln.strip()]
    branch = ""
    changes = []
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    for ln in lines:
        if ln.startswith("##"):
            branch = ln[2:].strip()
            continue
        code = ln[:2]
        x, y = (code[0] if len(code) > 0 else " "), (code[1] if len(code) > 1 else " ")
        path = ln[3:].strip()
        is_untracked = code == "??"
        is_staged = (not is_untracked) and x not in (" ", "?")
        is_unstaged = (not is_untracked) and y not in (" ", "?")
        changes.append({"code": code.strip() or "??", "x": x, "y": y,
                        "path": path, "staged": is_staged,
                        "unstaged": is_unstaged, "untracked": is_untracked})
        if is_untracked:
            untracked.append(path)
        else:
            if is_staged:
                staged.append(path)
            if is_unstaged:
                unstaged.append(path)
    return {"ok": True, "path": str(repo_path()), "branch": branch,
            "ahead_behind": branch, "changes": changes,
            "staged": staged, "unstaged": unstaged, "untracked": untracked,
            "dirty": bool(changes)}


def diff(path: str | None = None, *, staged: bool = False) -> dict:
    args = ["diff", "--stat"]
    if staged:
        args.insert(1, "--cached")
    if path:
        args.append("--")
        args.append(path)
    stat = _run(args)
    body_args = ["diff", "--unified=3"]
    if staged:
        body_args.insert(1, "--cached")
    if path:
        body_args.extend(["--", path])
    body = _run(body_args)
    return {"ok": stat["ok"], "stat": stat["stdout"], "diff": body["stdout"],
            "error": stat["stderr"] or body["stderr"]}


# --------------------------------------------------------------------------- #
# secret_scan：提交前扫描密钥
# --------------------------------------------------------------------------- #
def _scan_skip(rel: str) -> bool:
    norm = str(rel or "").replace("\\", "/")
    lowered = norm.lower()
    for part in SECRET_SKIP_PARTS:
        needle = part.replace("\\", "/").lower()
        if needle and (needle in lowered or lowered.startswith(needle)):
            return True
    return False


def _scan_candidates() -> list[str]:
    """待扫描文件：staged + 工作区改动 + untracked（相对路径）。"""
    st = status()
    if not st.get("ok"):
        return []
    paths_out: list[str] = []
    for item in st.get("changes") or []:
        path = str(item.get("path") or "").strip()
        if path and path not in paths_out:
            paths_out.append(path)
    return paths_out


def secret_scan(files: list[str] | None = None) -> dict:
    """扫描待提交文件中的密钥；命中返回 ``ok=False`` + files 列表。

    只扫 ``SECRET_SCAN_SUFFIXES`` 后缀（``.env`` 文件名也算），跳过
    ``agi/data``、``.venv``、``node_modules`` 等大目录和 >1MB 文件。
    """
    repo = repo_path()
    candidates = list(files) if files is not None else _scan_candidates()
    hits: list[dict] = []
    scanned = 0
    for rel in candidates:
        text = str(rel or "").strip()
        if not text or _scan_skip(text):
            continue
        name = text.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if not (name == ".env" or name.endswith(SECRET_SCAN_SUFFIXES)):
            continue
        target = Path(text)
        if not target.is_absolute():
            target = Path(repo) / target
        try:
            if not target.is_file() or target.stat().st_size > SECRET_MAX_BYTES:
                continue
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        matches: list[dict] = []
        for pattern_name, pattern in SECRET_PATTERNS:
            for lineno, line in enumerate(content.splitlines(), start=1):
                if pattern.search(line):
                    matches.append({"pattern": pattern_name, "line": lineno})
        if matches:
            hits.append({"path": text, "matches": matches[:20]})
    return {"ok": not hits, "files": hits, "scanned": scanned,
            "patterns": [name for name, _ in SECRET_PATTERNS]}


# --------------------------------------------------------------------------- #
# commit / push / auto_commit
# --------------------------------------------------------------------------- #
def commit(message: str, *, add_all: bool = True, push: bool = False,
           block_secrets: bool | None = None) -> dict:
    if not message.strip():
        return {"ok": False, "error": "empty_message"}
    cfg = settings()
    block = cfg.get("block_secrets", True) if block_secrets is None else bool(block_secrets)
    if block:
        scan = secret_scan()
        if not scan["ok"]:
            return {"ok": False, "error": "secret_detected",
                    "files": scan["files"], "scanned": scan["scanned"],
                    "detail": "提交被拦截：检测到疑似密钥（可用 block_secrets=false 关闭）"}
    if add_all:
        a = _run(["add", "-A"])
        if not a["ok"]:
            return {"ok": False, "error": "git_add_failed", "detail": a["stderr"]}
    c = _run(["commit", "-m", message])
    if not c["ok"]:
        # 没有变更时 git 会报 "nothing to commit"
        if "nothing to commit" in (c["stdout"] + c["stderr"]):
            return {"ok": True, "noop": True, "message": "没有变更可提交"}
        return {"ok": False, "error": "commit_failed", "detail": c["stderr"] or c["stdout"]}
    sha = _run(["rev-parse", "--short", "HEAD"])["stdout"].strip()
    out = {"ok": True, "sha": sha, "message": message}
    if push:
        out["push"] = push_default()
    return out


def _daily_branch() -> str:
    return time.strftime("wb/%Y-%m-%d")


def _checkout_target(target: str) -> dict:
    """切到 target；不存在则 ``checkout -b`` 创建；返回 {ok, created, detail}。"""
    current = _current_branch()
    if current == target:
        return {"ok": True, "created": False, "detail": "already_on_branch"}
    exists = _run(["show-ref", "--verify", "--quiet", f"refs/heads/{target}"])
    if exists["ok"]:
        r = _run(["checkout", target])
        return {"ok": r["ok"], "created": False,
                "detail": (r["stderr"] or r["stdout"]).strip()}
    r = _run(["checkout", "-b", target])
    return {"ok": r["ok"], "created": r["ok"],
            "detail": (r["stderr"] or r["stdout"]).strip()}


def push_default(retries: int | None = None) -> dict:
    """推送当前/配置分支：daily 自动建分支、推送前 fetch、失败按 settings 重试。"""
    cfg = settings()
    remote = str(cfg.get("remote") or "origin")
    mode = str(cfg.get("branch_mode") or "fixed")
    created_branch = False
    fetch_detail = ""
    branch = _current_branch()

    # 推送前 fetch（失败不阻塞，只记录 detail）
    fetched = _run(["fetch", remote])
    if not fetched["ok"]:
        fetch_detail = (fetched["stderr"] or fetched["stdout"]).strip()[:300]

    if mode == "daily":
        target = _daily_branch()
        if branch != target:
            moved = _checkout_target(target)
            if not moved["ok"]:
                return {"ok": False, "error": "checkout_failed", "branch": branch,
                        "remote": remote, "detail": moved["detail"],
                        "created_branch": False, "fetched": fetched["ok"],
                        "fetch_detail": fetch_detail, "attempts": 0}
            created_branch = bool(moved["created"])
            branch = target
    elif mode == "fixed":
        target = str(cfg.get("branch") or "").strip()
        if target and target != branch:
            exists = _run(["show-ref", "--verify", "--quiet", f"refs/heads/{target}"])
            if exists["ok"]:
                moved = _checkout_target(target)
                if moved["ok"]:
                    branch = target
    # per-change：直接推当前分支

    branch = branch or _current_branch() or str(cfg.get("branch") or "main")
    try:
        base_attempts = int(retries) if retries is not None else int(
            cfg.get("push_retry") or 0)
    except (TypeError, ValueError):
        base_attempts = 0
    attempts_allowed = max(1, min(10, base_attempts))
    last_detail = ""
    attempts = 0
    for index in range(attempts_allowed):
        attempts = index + 1
        p = _run(["push", "-u", remote, branch])
        if p["ok"]:
            return {"ok": True, "branch": branch, "remote": remote,
                    "detail": (p["stderr"] or p["stdout"]).strip(),
                    "created_branch": created_branch, "fetched": fetched["ok"],
                    "fetch_detail": fetch_detail, "attempts": attempts}
        last_detail = (p["stderr"] or p["stdout"]).strip()
        if index + 1 < attempts_allowed:
            time.sleep(min(2 ** index, 8))     # 1s / 2s / 4s / 8s…
    return {"ok": False, "branch": branch, "remote": remote,
            "detail": last_detail, "created_branch": created_branch,
            "fetched": fetched["ok"], "fetch_detail": fetch_detail,
            "attempts": attempts}


def log(limit: int = 20) -> dict:
    r = _run(["log", f"-{int(limit)}", "--pretty=%h|%ad|%s",
              "--date=format:%Y-%m-%d %H:%M"])
    if not r["ok"]:
        return {"ok": False, "error": r["stderr"]}
    items = []
    for line in (r["stdout"] or "").splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            items.append({"sha": parts[0], "date": parts[1], "subject": parts[2]})
    return {"ok": True, "items": items}


def auto_commit(module: str, action: str, summary: str, *, actor: str = "workbench") -> dict:
    """工作台写操作后的自动提交（按 settings 决定是否提交/推送）。"""
    cfg = settings()
    if not cfg.get("auto_commit", True):
        return {"ok": True, "skipped": True, "reason": "auto_commit_disabled",
                "module": module, "action": action}
    msg = (f"wb({module}): {action} — {summary}\n\n"
           f"actor: {actor}\ntime: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    out = commit(msg, add_all=True, push=False)
    if out.get("ok") and not out.get("noop") and cfg.get("auto_push", False):
        push = push_default()
        out["push"] = push
        out["push_attempts"] = push.get("attempts")
    return out


# ---------------------------------------------------------------- 分支与暂存
def branches() -> dict:
    """本地+远程分支列表，标记当前分支。"""
    cur = _run(["rev-parse", "--abbrev-ref", "HEAD"])
    current = (cur["stdout"] or "").strip() if cur["ok"] else ""
    fmt = _run(["branch", "-a", "--format=%(refname:short)|%(objectname:short)|"
                "%(committerdate:short)|%(subject)"])
    items = []
    for line in (fmt["stdout"] or "").splitlines():
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        name = parts[0]
        if name.endswith("/HEAD"):
            continue
        items.append({"name": name, "sha": parts[1], "date": parts[2],
                      "subject": parts[3], "current": name == current})
    return {"ok": True, "current": current, "items": items}


def checkout(name: str, *, create: bool = False) -> dict:
    if not name.strip():
        return {"ok": False, "error": "empty_branch"}
    args = ["checkout", "-b", name] if create else ["checkout", name]
    r = _run(args)
    return {"ok": r["ok"], "branch": name,
            "detail": (r["stderr"] or r["stdout"]).strip()}


def stage(patterns: list[str]) -> dict:
    r = _run(["add", "--"] + list(patterns or ["-A"]))
    return {"ok": r["ok"], "detail": (r["stderr"] or r["stdout"]).strip()}


def unstage(patterns: list[str]) -> dict:
    r = _run(["reset", "HEAD", "--"] + list(patterns or ["."]))
    return {"ok": r["ok"], "detail": (r["stderr"] or r["stdout"]).strip()}


def staged() -> dict:
    r = _run(["diff", "--cached", "--name-only"])
    return {"ok": r["ok"], "items": [x for x in (r["stdout"] or "").splitlines() if x]}
