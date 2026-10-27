# -*- coding: utf-8 -*-
"""Git Studio：对「被管理的 SoulAgent 仓库」做 status/diff/commit/push。

设计：工作台自己是一个仓库（idea/），被管理的项目是另一个仓库（SoulAgent/）。
这里的操作默认作用在 SoulAgent 上（可用 GIT_REPO 覆盖）。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import paths

DEFAULT_TIMEOUT = 30


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


def status() -> dict:
    if not is_repo():
        return {"ok": False, "error": "not_a_repo", "path": str(repo_path())}
    s = _run(["status", "--porcelain=v1", "--branch"])
    lines = [ln for ln in (s["stdout"] or "").splitlines() if ln.strip()]
    branch = ""
    changes = []
    for ln in lines:
        if ln.startswith("##"):
            branch = ln[2:].strip()
            continue
        code, _, path = ln[:2], ln[2:3], ln[3:].strip()
        changes.append({"code": code.strip(), "path": path})
    return {"ok": True, "path": str(repo_path()), "branch": branch,
            "ahead_behind": branch, "changes": changes, "dirty": bool(changes)}


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


def commit(message: str, *, add_all: bool = True, push: bool = False) -> dict:
    if not message.strip():
        return {"ok": False, "error": "empty_message"}
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


def push_default() -> dict:
    branch = _run(["rev-parse", "--abbrev-ref", "HEAD"])["stdout"].strip()
    remote = os.environ.get("GIT_REMOTE", "origin")
    p = _run(["push", "-u", remote, branch])
    return {"ok": p["ok"], "branch": branch, "remote": remote,
            "detail": (p["stderr"] or p["stdout"]).strip()}


def log(limit: int = 20) -> dict:
    r = _run(["log", f"-{int(limit)}", "--pretty=%h|%ad|%s", "--date=format:%Y-%m-%d %H:%M"])
    if not r["ok"]:
        return {"ok": False, "error": r["stderr"]}
    items = []
    for line in (r["stdout"] or "").splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            items.append({"sha": parts[0], "date": parts[1], "subject": parts[2]})
    return {"ok": True, "items": items}


def auto_commit(module: str, action: str, summary: str, *, actor: str = "workbench") -> dict:
    """工作台写操作后的自动提交（同一次操作内由调用方合并）。"""
    msg = f"wb({module}): {action} — {summary}\n\nactor: {actor}\ntime: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    return commit(msg, add_all=True, push=False)


# ---------------------------------------------------------------- 分支与暂存
def branches() -> dict:
    """本地+远程分支列表，标记当前分支。"""
    cur = _run(["rev-parse", "--abbrev-ref", "HEAD"])
    current = (cur["stdout"] or "").strip() if cur["ok"] else ""
    fmt = _run(["branch", "-a", "--format=%(refname:short)|%(objectname:short)|%(committerdate:short)|%(subject)"])
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
    return {"ok": r["ok"], "branch": name, "detail": (r["stderr"] or r["stdout"]).strip()}


def stage(patterns: list[str]) -> dict:
    r = _run(["add", "--"] + list(patterns or ["-A"]))
    return {"ok": r["ok"], "detail": (r["stderr"] or r["stdout"]).strip()}


def unstage(patterns: list[str]) -> dict:
    r = _run(["reset", "HEAD", "--"] + list(patterns or ["."]))
    return {"ok": r["ok"], "detail": (r["stderr"] or r["stdout"]).strip()}


def staged() -> dict:
    r = _run(["diff", "--cached", "--name-only"])
    return {"ok": r["ok"], "items": [x for x in (r["stdout"] or "").splitlines() if x]}
