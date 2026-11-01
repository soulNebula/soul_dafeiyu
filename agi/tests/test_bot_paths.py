# -*- coding: utf-8 -*-
"""V1 PR1：bot 工作区路径解析（`SOULAGENT_BOT_ROOT` / `bots/active`）。"""
from __future__ import annotations

import pytest

from agi import paths


def test_bot_root_env_absolute(tmp_path, monkeypatch):
    target = tmp_path / "mybot"
    monkeypatch.setenv(paths.BOT_ROOT_ENV, str(target))
    assert paths.bot_root() == target
    assert paths.bot_path("config", "wechat.json") == target / "config" / "wechat.json"


def test_bot_root_env_relative_to_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    monkeypatch.setenv(paths.BOT_ROOT_ENV, "bots/demo")
    assert paths.bot_root() == tmp_path / "bots" / "demo"


def test_bot_root_active_pointer(tmp_path, monkeypatch):
    monkeypatch.delenv(paths.BOT_ROOT_ENV, raising=False)
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    (tmp_path / "bots").mkdir()
    (tmp_path / "bots" / "active").write_text("demo\n", encoding="utf-8")
    assert paths.bot_root() == tmp_path / "bots" / "demo"


def test_bot_root_none_keeps_legacy_paths(tmp_path, monkeypatch):
    monkeypatch.delenv(paths.BOT_ROOT_ENV, raising=False)
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    assert paths.bot_root() is None
    with pytest.raises(RuntimeError):
        paths.bot_path("config")


def test_bot_path_accepts_separators(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.BOT_ROOT_ENV, str(tmp_path / "b"))
    assert paths.bot_path("db/selflearn", "x.sqlite3") == (
        tmp_path / "b" / "db" / "selflearn" / "x.sqlite3")
    # 空片段被忽略
    assert paths.bot_path("", "media", "/") == tmp_path / "b" / "media"


def test_describe_includes_bot_root(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.BOT_ROOT_ENV, str(tmp_path / "b"))
    info = paths.describe()
    assert info["bot_root"] == str(tmp_path / "b")
    assert info["bots_dir"].endswith("bots")
