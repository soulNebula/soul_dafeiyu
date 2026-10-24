# -*- coding: utf-8 -*-
"""G2 工具层：白名单工具执行器，不执行任意 shell。

保留旧接口 ``available()`` / ``run(name, **kwargs)`` 与 ``calc/time/python_verify``；
新增联网工具 ``web_search`` / ``web_fetch`` 与记忆检索 ``memory_query``。
``wx_core.safe_fetch`` / ``wx_core.web_search`` 由并行工作流提供，用惰性 import +
try/except 包住：模块缺失或调用失败时返回 ``ok=False``，不抛异常。
"""
from __future__ import annotations

import ast
import copy
import json
import operator
import queue
import threading
import time
from pathlib import Path
from typing import Any

from . import verifier

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}

MAX_RESULT_CHARS = 8000
MAX_TEXT_CHARS = 4000
TOOL_TIMEOUT_S = 20.0
# 可能发起网络/磁盘 IO 的工具：用独立线程兜底，超时 fail-closed。
_SLOW_TOOLS = frozenset({"web_search", "web_fetch", "memory_query"})


class ToolError(ValueError):
    """参数校验失败（会转成 ok=False，不向调用方抛出）。"""


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


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------

def _text_arg(kwargs: dict, key: str, *, max_len: int = 4000,
              allow_empty: bool = False) -> str:
    value = kwargs.get(key)
    if not isinstance(value, str):
        raise ToolError(f"参数 {key} 必须是字符串")
    value = value.strip()
    if not value and not allow_empty:
        raise ToolError(f"参数 {key} 不能为空")
    if len(value) > max_len:
        raise ToolError(f"参数 {key} 过长（最多 {max_len} 字）")
    return value


def _int_arg(kwargs: dict, key: str, default: int, low: int, high: int) -> int:
    value = kwargs.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"参数 {key} 必须是整数")
    if value < low or value > high:
        raise ToolError(f"参数 {key} 必须在 {low}..{high} 之间")
    return value


# ---------------------------------------------------------------------------
# 惰性依赖（测试可 monkeypatch 这三个 loader）
# ---------------------------------------------------------------------------

def _load_search():
    from wx_core.web_search import search
    return search


def _load_fetch():
    from wx_core.safe_fetch import fetch_text
    return fetch_text


def _load_memory():
    from . import memory
    return memory


# ---------------------------------------------------------------------------
# 新工具实现
# ---------------------------------------------------------------------------

def web_search(query: str, count: int = 5) -> dict[str, Any]:
    """调用 wx_core.web_search.search；模块缺失/异常一律 ok=False。"""
    query = str(query or "")
    try:
        search = _load_search()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "query": query, "provider": "", "results": [],
                "error": f"web_search 不可用: {type(exc).__name__}: {str(exc)[:160]}"}
    try:
        result = search(query, count=int(count))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "query": query, "provider": "", "results": [],
                "error": f"web_search 失败: {type(exc).__name__}: {str(exc)[:160]}"}
    if not isinstance(result, dict):
        return {"ok": False, "query": query, "provider": "", "results": [],
                "error": "web_search 返回格式错误（不是 dict）"}
    normalized = dict(result)
    normalized.setdefault("query", query)
    normalized.setdefault("provider", "")
    if not isinstance(normalized.get("results"), list):
        normalized["results"] = []
    if "ok" not in normalized:
        normalized["ok"] = not bool(normalized.get("error"))
    return normalized


def web_fetch(url: str, *, max_chars: int = MAX_TEXT_CHARS) -> dict[str, Any]:
    """调用 wx_core.safe_fetch.fetch_text；正文截到 4000 字再返回。"""
    url = str(url or "")
    try:
        fetch_text = _load_fetch()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "status": 0, "title": "", "text": "",
                "chars": 0,
                "error": f"web_fetch 不可用: {type(exc).__name__}: {str(exc)[:160]}"}
    try:
        result = fetch_text(url)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "status": 0, "title": "", "text": "",
                "chars": 0,
                "error": f"web_fetch 失败: {type(exc).__name__}: {str(exc)[:160]}"}
    if not isinstance(result, dict):
        return {"ok": False, "url": url, "status": 0, "title": "", "text": "",
                "chars": 0, "error": "web_fetch 返回格式错误（不是 dict）"}
    normalized = dict(result)
    normalized.setdefault("url", url)
    normalized.setdefault("title", "")
    normalized.setdefault("chars", len(normalized.get("text") or ""))
    if "ok" not in normalized:
        normalized["ok"] = not bool(normalized.get("error"))
    text = normalized.get("text")
    if isinstance(text, str) and len(text) > max_chars:
        normalized["text"] = text[:max_chars]
        normalized["truncated"] = True
        normalized["text_chars"] = len(text)
    return normalized


def memory_query(query: str, k: int = 5, *, db_path: Path | str | None = None) -> dict[str, Any]:
    """检索 selflearn 记忆库；db_path 由调用方通过 run(db_path=...) 传入。"""
    query = str(query or "")
    try:
        memory = _load_memory()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "query": query, "results": [],
                "error": f"memory_query 不可用: {type(exc).__name__}: {str(exc)[:160]}"}
    try:
        rows = memory.retrieve(db_path, query, k=int(k))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "query": query, "results": [],
                "error": f"memory_query 失败: {type(exc).__name__}: {str(exc)[:160]}"}
    if not isinstance(rows, list):
        return {"ok": False, "query": query, "results": [],
                "error": "memory_query 返回格式错误（不是 list）"}
    return {"ok": True, "query": query, "results": rows}


# ---------------------------------------------------------------------------
# 工具处理器（类型校验 -> 调用实现）
# ---------------------------------------------------------------------------

def _tool_calc(**kwargs: Any) -> Any:
    expression = _text_arg(kwargs, "expression", max_len=200)
    return calc(expression)


def _tool_time(**kwargs: Any) -> float:
    return now()


def _tool_python_verify(**kwargs: Any) -> dict:
    code = _text_arg(kwargs, "code", max_len=20000)
    test_code = kwargs.get("test_code", "")
    if test_code is None:
        test_code = ""
    if not isinstance(test_code, str):
        raise ToolError("参数 test_code 必须是字符串")
    timeout = _int_arg(kwargs, "timeout", 5, 1, 60)
    return python_verify(code, test_code, timeout=timeout)


def _tool_web_search(**kwargs: Any) -> dict:
    query = _text_arg(kwargs, "query", max_len=500)
    count = _int_arg(kwargs, "count", 5, 1, 20)
    return web_search(query, count=count)


def _tool_web_fetch(**kwargs: Any) -> dict:
    url = _text_arg(kwargs, "url", max_len=2000)
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ToolError("参数 url 必须是 http(s) 地址")
    return web_fetch(url)


def _tool_memory_query(db_path: Path | str | None = None, **kwargs: Any) -> dict:
    query = _text_arg(kwargs, "query", max_len=500)
    k = _int_arg(kwargs, "k", 5, 1, 20)
    if db_path is None or not str(db_path).strip():
        raise ToolError("缺少 db_path（记忆库路径由调用方传入）")
    try:
        path = Path(db_path)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"db_path 非法: {exc}") from exc
    if not path.is_file():
        raise ToolError("记忆库不存在")
    return memory_query(query, k=k, db_path=path)


TOOLS = {
    "calc": _tool_calc,
    "time": _tool_time,
    "python_verify": _tool_python_verify,
    "web_search": _tool_web_search,
    "web_fetch": _tool_web_fetch,
    "memory_query": _tool_memory_query,
}


def available() -> list[str]:
    """旧接口：已注册的全部工具名（含仅供旧调用方使用的 time）。"""
    return sorted(TOOLS)


# ---------------------------------------------------------------------------
# OpenAI tools schema
# ---------------------------------------------------------------------------

_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索；用于事实、新闻、时效性信息。返回标题/链接/摘要列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "count": {"type": "integer", "description": "返回条数，默认 5，范围 1-20",
                              "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "抓取指定网页正文；用于核实搜索结果里的具体页面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http(s) 网页地址"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_query",
            "description": "检索长期记忆，了解用户偏好/过往约定；不用于联网事实。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                    "k": {"type": "integer", "description": "返回条数，默认 5，范围 1-20",
                          "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calc",
            "description": "精确计算简单四则运算表达式。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "如 1+2*3"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "python_verify",
            "description": "在受限子进程里验证一小段 Python 代码或断言测试。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "待验证代码"},
                    "test_code": {"type": "string", "description": "可选断言测试"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 5",
                                "minimum": 1, "maximum": 60},
                },
                "required": ["code"],
            },
        },
    },
]


def specs() -> list[dict]:
    """OpenAI tools schema（返回副本，调用方可自由修改）。"""
    return copy.deepcopy(_SPECS)


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def _cap_value(value: Any, limit: int = MAX_RESULT_CHARS) -> Any:
    """单次结果超过 limit 字时截断为可读 preview，避免撑爆上下文。"""
    try:
        raw = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)[:limit]
    if len(raw) <= limit:
        return value
    return {"_truncated": True, "chars": len(raw), "preview": raw[:limit]}


def _call_with_timeout(fn, timeout: float = TOOL_TIMEOUT_S) -> Any:
    """在守护线程里执行；超时抛 TimeoutError（不阻塞进程退出）。"""
    result: queue.Queue = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result.put((True, fn()))
        except BaseException as exc:  # noqa: BLE001 - 原样带回主线程
            result.put((False, exc))

    thread = threading.Thread(target=worker, name="selflearn-tool", daemon=True)
    thread.start()
    try:
        ok, value = result.get(timeout=max(0.01, float(timeout)))
    except queue.Empty:
        raise TimeoutError(f"工具执行超时（>{timeout:g}s）") from None
    if ok:
        return value
    raise value


def run(name: str, *, db_path: Path | str | None = None, **kwargs: Any) -> dict[str, Any]:
    """白名单执行；未知工具拒绝、参数校验、异常/超时 fail-closed、结果截断。

    旧调用方式 ``run("calc", expression="1+2*3")`` 仍然返回
    ``{"ok": True, "tool": "calc", "result": 7}``。
    """
    key = str(name or "").strip()
    if key not in TOOLS:
        return {"ok": False, "error": "工具不在白名单", "available": available()}
    handler = TOOLS[key]
    try:
        if key in _SLOW_TOOLS:
            result = _call_with_timeout(
                lambda: handler(db_path=db_path, **kwargs), TOOL_TIMEOUT_S)
        else:
            result = handler(db_path=db_path, **kwargs)
    except ToolError as exc:
        return {"ok": False, "tool": key, "error": str(exc), "available": available()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "tool": key,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "available": available()}
    # 工具自身返回 ok=False（模块缺失/网络失败/验证失败）时，顶层同步为失败，
    # 这样 agent 事件与调用方都能直接判断。
    if isinstance(result, dict) and result.get("ok") is False:
        return {"ok": False, "tool": key,
                "error": str(result.get("error") or "工具执行失败"),
                "result": _cap_value(result)}
    return {"ok": True, "tool": key, "result": _cap_value(result)}
