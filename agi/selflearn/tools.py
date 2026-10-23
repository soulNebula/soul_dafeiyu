# -*- coding: utf-8 -*-
"""G2 工具层：白名单工具执行器，不执行任意 shell/网络。"""
from __future__ import annotations

import ast
import operator
import time
from typing import Any

from . import verifier

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _calc(node):
    if isinstance(node, ast.Expression):
        return _calc(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        return _BIN[type(node.op)](_calc(node.left), _calc(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_calc(node.operand))
    raise ValueError("只支持数字四则运算")


def calc(expression: str) -> float:
    if len(str(expression or "")) > 200:
        raise ValueError("表达式过长")
    return _calc(ast.parse(str(expression), mode="eval"))


def now() -> float:
    return time.time()


def python_verify(code: str, test_code: str = "", timeout: int = 5) -> dict:
    return verifier.verify_python(code, test_code, timeout=timeout)


TOOLS = {
    "calc": lambda **kw: calc(str(kw.get("expression") or "")),
    "time": lambda **kw: now(),
    "python_verify": lambda **kw: python_verify(str(kw.get("code") or ""),
                                               str(kw.get("test_code") or ""),
                                               int(kw.get("timeout") or 5)),
}


def available() -> list[str]:
    return sorted(TOOLS)


def run(name: str, **kwargs) -> dict[str, Any]:
    key = str(name or "").strip()
    if key not in TOOLS:
        return {"ok": False, "error": "工具不在白名单", "available": available()}
    try:
        return {"ok": True, "tool": key, "result": TOOLS[key](**kwargs)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "tool": key, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
