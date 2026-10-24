# -*- coding: utf-8 -*-
"""ACL 加固工具（tools/harden_acl.py）的离线单元测试。

不真的改 ACL：subprocess 全部 monkeypatch，验证命令构造与盘点解析。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools import harden_acl  # noqa: E402

FILE_ACL = """config.json WIN-20251030KZR\\Administrator:(F)
            NT AUTHORITY\\SYSTEM:(F)
            BUILTIN\\Administrators:(F)

已成功处理 1 个文件; 处理 0 个文件时失败"""

DIR_ACL_INHERITED = """agi/data BUILTIN\\Administrators:(OI)(CI)(F)
         NT AUTHORITY\\Authenticated Users:(OI)(CI)(I)(M)
         NT AUTHORITY\\SYSTEM:(OI)(CI)(F)"""


def test_file_vs_dir_grant_flags(monkeypatch):
    monkeypatch.setattr(harden_acl, "allowed_sids", lambda: ["S-1-5-21-1", "S-1-5-18"])
    file_args = harden_acl.icacls_apply_args(pathlib.Path("config.json"))
    assert "/inheritance:r" in file_args
    assert "*S-1-5-21-1:F" in file_args
    dir_args = harden_acl.icacls_apply_args(pathlib.Path("log"))
    assert "*S-1-5-21-1:(OI)(CI)F" in dir_args


def test_parse_detects_inheritance_and_principals():
    hardened = harden_acl.parse_icacls(FILE_ACL)
    assert hardened["inherited"] is False
    assert any("Administrator" in p for p in hardened["principals"])

    inherited = harden_acl.parse_icacls(DIR_ACL_INHERITED)
    assert inherited["inherited"] is True


def test_check_target_marks_unhardened(monkeypatch):
    monkeypatch.setattr(harden_acl, "run_icacls",
                        lambda *a, **k: (0, DIR_ACL_INHERITED))
    info = harden_acl.check_target(pathlib.Path("agi/data"))
    assert info["ok"] is True and info["hardened"] is False


def test_apply_target_builds_inheritance_removal(monkeypatch):
    monkeypatch.setattr(harden_acl, "allowed_sids", lambda: ["S-1-5-18"])
    monkeypatch.setattr(harden_acl, "run_icacls", lambda *a, **k: (0, "ok"))
    out = harden_acl.apply_target(pathlib.Path("web_config.json"))
    assert out["ok"] is True
    assert "/inheritance:r" in out["command"]
    assert "*S-1-5-18:F" in out["command"]


def test_main_skips_on_non_windows(monkeypatch, capsys):
    monkeypatch.setattr(harden_acl, "is_windows", lambda: False)
    assert harden_acl.main(["--check"]) == 0
    assert "跳过" in capsys.readouterr().out


def test_main_check_mode_never_writes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(harden_acl, "is_windows", lambda: True)
    monkeypatch.setattr(harden_acl, "current_user_sid", lambda: "S-1-5-21-9")
    monkeypatch.setattr(harden_acl, "ROOT", tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(args, **_kw):
        calls.append(list(args))
        return 0, FILE_ACL

    monkeypatch.setattr(harden_acl, "run_icacls", fake_run)
    assert harden_acl.main(["--check"]) == 0
    assert all("/inheritance:r" not in " ".join(c) for c in calls)  # 只读，不含收紧命令
