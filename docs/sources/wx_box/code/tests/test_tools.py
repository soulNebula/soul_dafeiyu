# -*- coding: utf-8 -*-
"""工具层测试。"""
from __future__ import annotations

from agi.selflearn import tools


def test_calc_and_whitelist():
    assert tools.run("calc", expression="1+2*3")["result"] == 7
    assert tools.run("none", x=1)["ok"] is False
    assert tools.run("calc", expression="__import__('os')")["ok"] is False


def test_python_verify_tool():
    r = tools.run("python_verify", code="print(1+1)", test_code="assert 1+1==2")
    assert r["ok"] and r["result"]["passed"] is True
