# -*- coding: utf-8 -*-
"""llama.cpp llama-server 的 HTTP 客户端（InnerState 蒸馏用）。

接口与 ``agi.selflearn.llm.Client`` 保持一致（鸭子类型）：

    client = make_client("http://127.0.0.1:8123")
    result = client.chat(messages, purpose="soul_sft", max_tokens=300,
                         temperature=0.0, json_mode=True)
    # -> {"ok": bool, "text": str, "usage": {"prompt_tokens": n, "completion_tokens": m},
    #     "timings": {...}, "elapsed": float, "model": str} 失败时 {"ok": False, "error": ...}

JSON 约束的落地方式（本仓库实测结论，见 tools/soul_serve/README.md）
---------------------------------------------------------------------
llama.cpp build b11857（0.7.0-dev）的 llama-server **在服务端原生支持 JSON schema 约束**：

    llama-server.exe -m model.gguf --jinja -jf agi/soul/inner_state_schema.json ...

实测结论（b11857 + Qwen2.5 的 jinja chat template）：**服务端 ``-jf/--json-schema-file`` 会
在带 chat template 时初始化 grammar 失败**（`Failed to initialize samplers: Unexpected empty
grammar stack after accepting piece: <|im_start|>`），请求直接 400；而**请求级
``response_format={"type":"json_schema",...}`` 正常工作**（HTTP 200，输出被 grammar 约束）。
因此客户端在 ``json_mode=True`` 时默认附加请求级 response_format（``use_response_format=True``），
server 只需 ``--jinja``，不要带 ``-jf``。若后续 llama.cpp 修复了 ``-jf``，可用
``use_response_format=False`` 交给 server 端约束。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "agi" / "soul" / "inner_state_schema.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8123"
DEFAULT_TIMEOUT_S = 30.0


def load_schema(path: Path | str | None = None) -> dict:
    """读取 InnerState JSON schema（server 端 -jf 与请求级 response_format 共用）。"""
    return json.loads(Path(path or SCHEMA_PATH).read_text(encoding="utf-8"))


class LlamaClient:
    """llama-server 的 OpenAI 兼容 ``/v1/chat/completions`` 客户端。"""

    #: W4-D1：标记为本地灵魂 client，让 agi/inner/inject._model_ctx 用
    #: agi/soul/prompt.build_messages 构造与训练同源的 messages（而不是旧的单条拼接）。
    local_soul = True

    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                 model: str = "local-soul", schema: dict | None = None,
                 use_response_format: bool = True) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_s = float(timeout_s)
        self.model = str(model or "local-soul")
        self.schema = schema if schema is not None else load_schema()
        self.use_response_format = bool(use_response_format)
        self.calls = 0

    # -- 与 llm.Client 对齐的接口 -----------------------------------------
    def available(self) -> bool:
        """GET /health 返回 200 且 {"status": "ok"} 即可用。"""
        try:
            with urllib.request.urlopen(self.base_url + "/health",
                                        timeout=min(5.0, self.timeout_s)) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            return resp.status == 200 and str(payload.get("status") or "ok") == "ok"
        except Exception:  # noqa: BLE001 - 探活失败就是不可用
            return False

    def chat(self, messages: list[dict], *, purpose: str = "chat",
             max_tokens: int = 300, temperature: float = 0.0,
             json_mode: bool = False) -> dict:
        """一次 chat completion；失败返回 ``{"ok": False, "error": ...}``，不抛异常。"""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "stream": False,
        }
        if json_mode and self.use_response_format:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "inner_state", "schema": self.schema},
            }
        started = time.time()
        req = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 - HTTP/超时/连接失败都算一次失败
            return {"ok": False, "text": "", "model": self.model, "usage": {},
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "elapsed": round(time.time() - started, 3)}
        self.calls += 1
        choices = payload.get("choices") if isinstance(payload, dict) else None
        choice = (choices or [{}])[0] if isinstance(choices, list) else {}
        message = choice.get("message") if isinstance(choice, dict) else None
        text = ""
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):  # 兼容多段 content
                text = "".join(
                    str(part.get("text") or "") for part in content
                    if isinstance(part, dict) and part.get("type") in (None, "text")
                )
        usage_raw = payload.get("usage") if isinstance(payload, dict) else None
        usage = usage_raw if isinstance(usage_raw, dict) else {}
        usage = {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
        # llama-server 额外的性能字段（tok/s、首 token 延迟）放 timings，便于延迟分析
        timings = payload.get("timings") if isinstance(payload, dict) else None
        result = {
            "ok": bool(text),
            "text": text,
            "model": str(payload.get("model") or self.model) if isinstance(payload, dict) else self.model,
            "usage": usage,
            "timings": timings if isinstance(timings, dict) else {},
            "finish_reason": str(choice.get("finish_reason") or "") if isinstance(choice, dict) else "",
            "elapsed": round(time.time() - started, 3),
        }
        if not text:
            result["error"] = "空回复（检查 server 是否 OOM / grammar 是否拒绝全部 token）"
        return result


def make_client(base_url: str = DEFAULT_BASE_URL, timeout_s: float = DEFAULT_TIMEOUT_S,
                **kwargs: Any) -> LlamaClient:
    """工厂入口：evaluate.py 等上层按这个签名构造。"""
    return LlamaClient(base_url, timeout_s=timeout_s, **kwargs)
