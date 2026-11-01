# -*- coding: utf-8 -*-
"""V1 PR1：设置 bot 工作区后，网关/自学习配置路径切换到 bots/<id>/。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run_with_bot(bot: Path, code: str) -> list[str]:
    env = dict(os.environ)
    for key in ("SOULAGENT_DATA_DIR", "SOULAGENT_LOG_DIR", "SOULAGENT_CONFIG_PATH",
                "SOULAGENT_WEB_CONFIG", "AGENT_HOME", "SOULAGENT_AGENT_HOME"):
        env.pop(key, None)
    env["SOULAGENT_BOT_ROOT"] = str(bot)
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def test_gateway_config_paths_follow_bot_root(tmp_path):
    bot = tmp_path / "mybot"
    lines = _run_with_bot(bot, (
        "from agi.wechat_gateway import config as c\n"
        "print(c.DATA_DIR)\nprint(c.CONFIG_PATH)\nprint(c.CONTACT_DB_DIR)\n"))
    assert Path(lines[0]) == bot
    assert Path(lines[1]) == bot / "config" / "wechat.json"
    assert Path(lines[2]) == bot / "db" / "selflearn" / "wechat_contacts"


def test_selflearn_config_paths_follow_bot_root(tmp_path):
    bot = tmp_path / "mybot"
    lines = _run_with_bot(bot, (
        "from agi.selflearn import config as c\n"
        "print(c.DATA_DIR)\nprint(c.DB_PATH)\nprint(c.WEB_CONFIG)\n"
        "print(c._WECHAT_CONFIG)\n"))
    assert Path(lines[0]) == bot / "db" / "selflearn"
    assert Path(lines[1]) == bot / "db" / "selflearn" / "selflearn.sqlite3"
    assert Path(lines[2]) == bot / "config" / "app.json"
    assert Path(lines[3]) == bot / "config" / "wechat.json"
