# -*- coding: utf-8 -*-
"""M1-C：Git 自动提交/推送增强测试（tmp git repo + mock subprocess，不联网）。

覆盖：settings 缺省/读写/校验、secret_scan 命中与放行、commit 拦截密钥、
auto_commit 关闭/开启/auto_push、push 失败重试（mock _run）、daily 分支创建/切换、
status staged/unstaged/untracked 分类。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import gitstudio  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _repo(tmp_path: Path, monkeypatch) -> Path:
    """初始化一个 tmp git 仓库（main 分支），并重定向 repo_path/settings_path。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    assert _git(repo, "add", "-A").returncode == 0
    assert _git(repo, "commit", "-q", "-m", "init").returncode == 0
    monkeypatch.setattr(gitstudio, "repo_path", lambda: repo)
    monkeypatch.setattr(gitstudio, "settings_path",
                        lambda: tmp_path / "git.json")
    return repo


def _count_commits(repo: Path) -> int:
    out = _git(repo, "rev-list", "--count", "HEAD")
    return int((out.stdout or "0").strip() or 0)


# --------------------------------------------------------------------------- #
# 1. settings
# --------------------------------------------------------------------------- #
def test_settings_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.setattr(gitstudio, "settings_path", lambda: tmp_path / "git.json")
    cfg = gitstudio.settings()
    assert cfg == gitstudio.DEFAULT_SETTINGS
    assert cfg["auto_commit"] is True and cfg["auto_push"] is False
    assert cfg["remote"] == "origin" and cfg["branch_mode"] == "fixed"
    assert cfg["branch"] == "main" and cfg["push_retry"] == 3
    assert cfg["block_secrets"] is True
    assert not (tmp_path / "git.json").exists()


def test_update_settings_roundtrip_and_normalise(tmp_path, monkeypatch):
    monkeypatch.setattr(gitstudio, "settings_path", lambda: tmp_path / "git.json")
    cfg = gitstudio.update_settings({
        "auto_push": "true", "branch_mode": "DAILY", "push_retry": "5",
        "branch": "  wb/test  ", "unknown_key": 1,
    })
    assert cfg["auto_push"] is True and cfg["branch_mode"] == "daily"
    assert cfg["push_retry"] == 5 and cfg["branch"] == "wb/test"
    saved = json.loads((tmp_path / "git.json").read_text(encoding="utf-8"))
    assert saved["branch_mode"] == "daily" and saved["branch"] == "wb/test"
    assert "unknown_key" not in saved
    assert gitstudio.settings()["push_retry"] == 5

    bad = gitstudio.update_settings({"branch_mode": "nope", "push_retry": 99,
                                     "auto_commit": "off"})
    assert bad["branch_mode"] == "fixed" and bad["push_retry"] == 10
    assert bad["auto_commit"] is False


# --------------------------------------------------------------------------- #
# 2. status staged/unstaged
# --------------------------------------------------------------------------- #
def test_status_classifies_staged_unstaged_untracked(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")      # unstaged
    (repo / "b.py").write_text("print('b')\n", encoding="utf-8")
    _git(repo, "add", "b.py")                                       # staged
    (repo / "c.md").write_text("# c\n", encoding="utf-8")           # untracked
    st = gitstudio.status()
    assert st["ok"] is True and st["dirty"] is True
    assert "b.py" in st["staged"] and "a.txt" not in st["staged"]
    assert "a.txt" in st["unstaged"] and "b.py" not in st["unstaged"]
    assert "c.md" in st["untracked"]
    by_path = {item["path"]: item for item in st["changes"]}
    assert by_path["a.txt"]["unstaged"] is True and by_path["a.txt"]["staged"] is False
    assert by_path["b.py"]["staged"] is True


# --------------------------------------------------------------------------- #
# 3. secret_scan / commit 拦截
# --------------------------------------------------------------------------- #
def test_secret_scan_detects_and_commit_blocks(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    (repo / "secret.py").write_text(
        'OPENAI = "sk-abcdefghijklmnopqrstuvwx"\n'
        'GITHUB = "ghp_' + "a" * 24 + '"\n', encoding="utf-8")
    scan = gitstudio.secret_scan()
    assert scan["ok"] is False
    assert [item["path"] for item in scan["files"]] == ["secret.py"]
    names = {m["pattern"] for m in scan["files"][0]["matches"]}
    assert {"sk_token", "github_pat"} <= names

    before = _count_commits(repo)
    out = gitstudio.commit("should be blocked")
    assert out["ok"] is False and out["error"] == "secret_detected"
    assert _count_commits(repo) == before

    # .txt 不在扫描后缀内 → 放行
    (repo / "secret.py").unlink()
    (repo / "notes.txt").write_text('token = "sk-abcdefghijklmnopqrstuvwx"',
                                    encoding="utf-8")
    assert gitstudio.secret_scan()["ok"] is True
    ok = gitstudio.commit("txt with token is not scanned")
    assert ok["ok"] is True and _count_commits(repo) == before + 1


def test_secret_scan_patterns_and_skip_dirs(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    (repo / ".env").write_text("SESSDATA=abc; api_key: xyz\n"
                               "Bearer " + "a" * 24 + "\n", encoding="utf-8")
    scan = gitstudio.secret_scan([".env"])
    assert scan["ok"] is False
    names = {m["pattern"] for m in scan["files"][0]["matches"]}
    assert {"sessdata", "api_key", "bearer"} <= names

    big_dir = repo / "agi" / "data"
    big_dir.mkdir(parents=True)
    (big_dir / "x.json").write_text('{"key": "sk-' + "b" * 24 + '"}',
                                    encoding="utf-8")
    # 显式传路径也会被跳过（agi/data 大目录）
    assert gitstudio.secret_scan(["agi/data/x.json"])["ok"] is True


# --------------------------------------------------------------------------- #
# 4. auto_commit / auto_push
# --------------------------------------------------------------------------- #
def test_auto_commit_disabled_skips(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    gitstudio.update_settings({"auto_commit": False})
    (repo / "new.py").write_text("print(1)\n", encoding="utf-8")
    before = _count_commits(repo)
    out = gitstudio.auto_commit("files", "write", "new.py")
    assert out["ok"] is True and out.get("skipped") is True
    assert out["reason"] == "auto_commit_disabled"
    assert _count_commits(repo) == before
    assert gitstudio.status()["dirty"] is True


def test_auto_commit_commits_and_auto_push(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    (repo / "new.py").write_text("print(1)\n", encoding="utf-8")
    gitstudio.update_settings({"auto_commit": True, "auto_push": False})
    out = gitstudio.auto_commit("files", "write", "new.py")
    assert out["ok"] is True and out["sha"]
    assert "push" not in out
    assert _git(repo, "log", "-1", "--pretty=%s").stdout.strip().startswith("wb(files)")

    pushed: list[dict] = []
    monkeypatch.setattr(gitstudio, "push_default",
                        lambda *a, **k: pushed.append({}) or {
                            "ok": True, "attempts": 2, "branch": "main"})
    gitstudio.update_settings({"auto_push": True})
    (repo / "new2.py").write_text("print(2)\n", encoding="utf-8")
    out = gitstudio.auto_commit("files", "write", "new2.py")
    assert pushed and out["push"]["ok"] is True and out["push_attempts"] == 2


# --------------------------------------------------------------------------- #
# 5. push_default：fetch / 重试 / daily 分支
# --------------------------------------------------------------------------- #
def test_push_retries_with_backoff(tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch)
    gitstudio.update_settings({"branch_mode": "fixed", "remote": "origin",
                               "branch": "main", "push_retry": 3})
    state = {"push": 0}

    def fake_run(args, **kwargs):
        cmd = args[0]
        if cmd == "rev-parse":
            return {"ok": True, "stdout": "main\n", "stderr": "", "code": 0}
        if cmd == "fetch":
            return {"ok": True, "stdout": "", "stderr": "", "code": 0}
        if cmd == "show-ref":
            return {"ok": True, "stdout": "", "stderr": "", "code": 0}
        if cmd == "push":
            state["push"] += 1
            if state["push"] < 3:
                return {"ok": False, "stdout": "", "stderr": "network down", "code": 1}
            return {"ok": True, "stdout": "", "stderr": "", "code": 0}
        return {"ok": True, "stdout": "", "stderr": "", "code": 0}

    monkeypatch.setattr(gitstudio, "_run", fake_run)
    sleeps: list[float] = []
    monkeypatch.setattr(gitstudio.time, "sleep", lambda s: sleeps.append(s))
    out = gitstudio.push_default()
    assert out["ok"] is True and out["attempts"] == 3
    assert sleeps == [1, 2] and out["fetched"] is True


def test_push_failure_returns_detail_after_retries(tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch)
    gitstudio.update_settings({"push_retry": 2})
    monkeypatch.setattr(gitstudio.time, "sleep", lambda s: None)

    def fake_run(args, **kwargs):
        cmd = args[0]
        if cmd == "rev-parse":
            return {"ok": True, "stdout": "main\n", "stderr": "", "code": 0}
        if cmd == "fetch":
            return {"ok": False, "stdout": "", "stderr": "no remote", "code": 1}
        if cmd == "push":
            return {"ok": False, "stdout": "", "stderr": "rejected", "code": 1}
        return {"ok": True, "stdout": "", "stderr": "", "code": 0}

    monkeypatch.setattr(gitstudio, "_run", fake_run)
    out = gitstudio.push_default()
    assert out["ok"] is False and out["attempts"] == 2
    assert out["detail"] == "rejected" and out["fetched"] is False


def test_push_daily_branch_created_and_reused(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.setattr(gitstudio.time, "sleep", lambda s: None)
    gitstudio.update_settings({"branch_mode": "daily", "push_retry": 1,
                               "remote": "origin"})
    first = gitstudio.push_default()
    expected = time.strftime("wb/%Y-%m-%d")
    assert first["created_branch"] is True and first["branch"] == expected
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == expected
    # 推送失败（没有配置远端）不算创建失败，但 ok=False；第二次不再创建
    second = gitstudio.push_default()
    assert second["created_branch"] is False and second["branch"] == expected
