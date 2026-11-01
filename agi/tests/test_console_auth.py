# -*- coding: utf-8 -*-
"""V1 PR4：802 密码登录（bot 包有 auth.json 才启用；否则旧 token 模式）。"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _auth_record(password: str = "pw123456") -> dict:
    salt = b"0123456789abcdef"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return {"algo": "pbkdf2_sha256", "salt": salt.hex(), "hash": digest.hex()}


def _run(tmp_path: Path, bot: Path | None, code: str) -> list[str]:
    env = dict(os.environ)
    for key in ("SOULAGENT_BOT_ROOT", "SOULAGENT_DATA_DIR", "SOULAGENT_LOG_DIR",
                "SOULAGENT_CONFIG_PATH", "SOULAGENT_WEB_CONFIG"):
        env.pop(key, None)
    env["SOULAGENT_DATA_DIR"] = str(tmp_path / "data")
    if bot is not None:
        env["SOULAGENT_BOT_ROOT"] = str(bot)
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                         capture_output=True, text=True, timeout=90)
    assert out.returncode == 0, out.stderr[-800:]
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def test_auth_disabled_without_bot(tmp_path):
    lines = _run(tmp_path, None,
                 "from tools.console import server\n"
                 "print(server.auth_enabled())\n"
                 "print(server.session_ok({'Cookie': ''}))\n")
    assert lines == ["False", "False"]


def _code(lines):
    return chr(10).join(lines)


def test_auth_enabled_and_password_roundtrip(tmp_path):
    bot = tmp_path / "bots" / "demo"
    (bot / "secrets").mkdir(parents=True)
    (bot / "secrets" / "auth.json").write_text(
        json.dumps(_auth_record()), encoding="utf-8")
    lines = _run(tmp_path, bot, _code([
        "from tools.console import server",
        "print(server.auth_enabled())",
        "print(server.verify_password('pw123456'))",
        "print(server.verify_password('wrong'))",
        "s = server.new_session()",
        "print(server.session_ok({'Cookie': 'soul_session=' + s}))",
        "print(server.session_ok({'Cookie': 'soul_session=bad'}))",
    ]))
    assert lines == ["True", "True", "False", "True", "False"]


def test_request_authed_cookie_or_token(tmp_path):
    bot = tmp_path / "bots" / "demo2"
    (bot / "secrets").mkdir(parents=True)
    (bot / "secrets" / "auth.json").write_text(
        json.dumps(_auth_record()), encoding="utf-8")
    lines = _run(tmp_path, bot, _code([
        "from tools.console import server",
        "server.session_ok = lambda h: h.get('Cookie') == 'ok'",
        "server.token_ok = lambda h: h.get('X-Admin-Token') == 'tok'",
        "print(server.request_authed({'Cookie': 'ok'}))",
        "print(server.request_authed({'X-Admin-Token': 'tok'}))",
        "print(server.request_authed({}))",
    ]))
    assert lines == ["True", "True", "False"]
