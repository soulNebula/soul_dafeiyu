# -*- coding: utf-8 -*-
"""最小 DeepSeek V4.1 Flash 客户端（selflearn 独立实现）。

密钥来源（优先级从高到低，见 :func:`load_config`）：

1. 环境变量 ``DEEPSEEK_API_KEY`` —— 推荐。只存在于进程环境，不落盘、不进 git；
2. 本机密钥库 ``agi/data/secrets.json`` 的 ``llm.api_key``（``SOULAGENT_SECRETS``
   可换路径；由 ``tools/rotate_secrets.py`` 生成）；
3. ``web_config.json`` 的 ``llm.api_key`` —— 兼容旧部署。命中时**告警一次**，
   建议迁移/轮换（告警文案不含 key 本身）。

配置文件路径本身可用 ``SOULAGENT_WEB_CONFIG`` 覆盖（测试/多实例）。显式设置后
**不会回退**到默认 ``web_config.json``：路径写错属于配置事故，静默回退会让
「我以为换了配置」的假设悄悄失效（fail-closed）。

为什么 env 在最前：这是唯一「不落盘」的来源。让它可以压过磁盘上的明文 key，
用户才能在**不修改文件**的前提下停用/轮换一把已经泄漏的 key（改文件需要写权限，
而写权限本身可能就是泄漏路径）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .. import secrets_store
from .config import WEB_CONFIG, DEFAULT_MODEL

#: DeepSeek/OpenAI 风格 key 的来源环境变量
API_KEY_ENV = "DEEPSEEK_API_KEY"
#: 覆盖 web_config.json 路径的环境变量（相对路径按仓库根解析）
WEB_CONFIG_ENV = "SOULAGENT_WEB_CONFIG"

_LOGGER = logging.getLogger("agi.selflearn.llm")
#: 已经告警过的配置文件路径（同一路径只提示一次，避免每次 load_config 刷屏）
_WARNED_PLAINTEXT: set[str] = set()

#: 脱敏规则：任何进入日志/错误返回的文本都要过一遍。宁可多打码，也不漏 key。
_REDACT_RULES = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{6,}"), "sk-***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{6,}"), "Bearer ***"),
)


def redact(text: Any) -> str:
    """把疑似密钥从任意文本里打码（用于日志/错误信息，绝不返回原文）。"""
    value = str(text or "")
    for pattern, replacement in _REDACT_RULES:
        value = pattern.sub(replacement, value)
    return value


def web_config_path() -> Path:
    """当前 web_config.json 路径（``SOULAGENT_WEB_CONFIG`` 覆盖；显式设置后不回退）。"""
    raw = str(os.environ.get(WEB_CONFIG_ENV) or "").strip()
    if not raw:
        return Path(WEB_CONFIG)
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.is_absolute():
        path = secrets_store.REPO_ROOT / path
    return path


def _read_llm_section(path: Path) -> dict[str, Any]:
    """读 web_config.json 的 ``llm`` 段；缺失/损坏一律空 dict（不抛、不回显内容）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}
    section = raw.get("llm") if isinstance(raw, dict) else None
    return dict(section) if isinstance(section, dict) else {}


def _warn_plaintext_key(path: Path, source: str) -> None:
    """明文 key 落在可被复制/提交的文件里 → 提示迁移。

    只提示一次、**只打路径与来源名**：这条日志本身也可能被采集走，绝不能带 key。
    """
    key = str(path)
    if key in _WARNED_PLAINTEXT:
        return
    _WARNED_PLAINTEXT.add(key)
    _LOGGER.warning(
        "[安全] %s 里有明文 API key（本次取值来源：%s）。"
        "建议迁移并轮换：先 python tools/rotate_secrets.py --dry-run 看计划，"
        "再设 DEEPSEEK_API_KEY 环境变量；该文件不要提交/外发。",
        path, source,
    )


def bot_api_key() -> str:
    """V1 PR8：bot 工作区生效时，bot 包 ``secrets/keys.json`` 的 ``llm_api_key``。

    只在本进程内解密；未启用 bot / 解密失败一律空串（调用方继续走旧来源链）。
    """
    try:
        from agi import bot_runtime
        if bot_runtime.bot_dir() is None:
            return ""
        return str(bot_runtime.load_secrets().get("llm_api_key") or "").strip()
    except Exception:  # noqa: BLE001 - 旧部署/测试环境不因 bot 模块异常而失效
        return ""


def load_config() -> dict[str, Any]:
    """解析 LLM 配置（api_key 取值顺序：env → secrets.json → web_config.json）。

    额外返回 ``api_key_source``（``env`` / ``secrets`` / ``web_config`` / ``none``），
    只用于诊断，不含密钥；其余字段与旧实现逐字节一致。
    """
    config = {"api_key": "", "model": DEFAULT_MODEL,
              "base_url": "https://api.deepseek.com", "api_key_source": "none"}
    path = web_config_path()
    llm = _read_llm_section(path)
    for key in ("model", "base_url"):
        value = llm.get(key)
        if isinstance(value, str) and value.strip():
            config[key] = value.strip()
    file_key = str(llm.get("api_key") or "").strip()
    if file_key:
        # 无论最终用不用它，磁盘上有明文 key 这件事都值得提示一次。
        _warn_plaintext_key(path, "web_config.json")

    env_key = os.environ.get(API_KEY_ENV)
    if env_key is not None:
        # 显式设置（含空串）就是最终结论：空串 = 用户主动停用/轮换中，
        # 此时不能再回落去读磁盘上的旧 key（那正是要淘汰的东西）。
        config["api_key"] = env_key.strip()
        config["api_key_source"] = "env"
    elif bot_api_key():
        # V1 PR8：bot 包（SOULAGENT_BOT_ROOT）里的 key 优先于全局密钥库/明文配置，
        # 这样 801/802 的既有 LLM 调用点实际用的是当前 bot 的模型凭证。
        config["api_key"] = bot_api_key()
        config["api_key_source"] = "bot"
    elif secrets_store.llm_api_key():
        config["api_key"] = secrets_store.llm_api_key()
        config["api_key_source"] = "secrets"
    elif file_key:
        config["api_key"] = file_key
        config["api_key_source"] = "web_config"
    config["base_url"] = config["base_url"].rstrip("/")
    return config


def parse_json(text: str) -> dict | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


class Client:
    def __init__(self, config: dict[str, Any] | None = None,
                 transport=None, timeout: float = 60.0) -> None:
        self.config = config or load_config()
        self.api_key = str(self.config.get("api_key") or "")
        self.model = str(self.config.get("model") or DEFAULT_MODEL)
        self.base_url = str(self.config.get("base_url") or "https://api.deepseek.com").rstrip("/")
        self.transport = transport
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key) or self.transport is not None

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        started = time.time()
        try:
            if self.transport is not None:
                payload = self.transport(body["messages"], body)
            else:
                if not self.api_key:
                    raise RuntimeError("缺少 DeepSeek API key")
                req = urllib.request.Request(
                    self.base_url + "/chat/completions",
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    method="POST",
                    headers={"Authorization": "Bearer " + self.api_key,
                             "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "replace"))
            choice = (payload.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            text = message.get("content") or ""
            # P15-A：reasoning_content 原文（DeepSeek reasoner / 兼容字段）。
            # 只是**额外返回**：没有该字段时为空串，不影响 ok/text/usage 等既有行为。
            reasoning = ""
            if isinstance(message, dict):
                reasoning = str(message.get("reasoning_content")
                                or message.get("reasoning") or "")
            return {"ok": bool(text), "text": text, "model": self.model,
                    "reasoning": reasoning,
                    "usage": payload.get("usage") or {},
                    "finish_reason": choice.get("finish_reason") or "",
                    "elapsed": round(time.time() - started, 3)}
        except Exception as exc:  # noqa: BLE001
            # 错误信息可能被写进日志/网页/草稿，统一脱敏：即使底层异常里带了
            # Authorization 片段，也只会看到 sk-***。
            return {"ok": False, "text": "", "model": self.model, "usage": {},
                    "reasoning": "",
                    "error": redact(f"{type(exc).__name__}: {str(exc)[:200]}"),
                    "elapsed": round(time.time() - started, 3)}

    def _request_raw(self, body: dict[str, Any], *,
                     timeout: float | None = None) -> dict[str, Any]:
        """与 ``_post`` 相同的传输层，但返回原始 payload。

        多步工具循环需要读取 ``choices[0].message.tool_calls``，而
        ``_post`` 只保留文本；这里复用同一套 transport 注入约定
        （``transport(body["messages"], body)``），不改变 ``_post`` 行为。
        """
        if self.transport is not None:
            return self.transport(body["messages"], body)
        if not self.api_key:
            raise RuntimeError("缺少 DeepSeek API key")
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and part.get("type") in (None, "text"):
                    if isinstance(part.get("text"), str):
                        parts.append(part["text"])
            return "".join(parts)
        return ""

    @staticmethod
    def _parse_arguments(raw: Any, name: str, errors: list[str]) -> dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return {}
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
            except (TypeError, ValueError) as exc:
                errors.append(f"{name} 的 arguments 不是合法 JSON: {str(exc)[:120]}")
                return {}
            if isinstance(value, dict):
                return value
            errors.append(f"{name} 的 arguments 不是 JSON 对象")
            return {}
        errors.append(f"{name} 的 arguments 类型不支持: {type(raw).__name__}")
        return {}

    def chat_with_tools(self, messages: list[dict], tools: list[dict], *,
                        max_tokens: int = 800, temperature: float = 0.3,
                        timeout: float | None = None) -> dict[str, Any]:
        """OpenAI 兼容的工具调用请求；不解析/执行工具，只返回候选调用。"""
        started = time.time()
        body: dict[str, Any] = {
            "model": self.model, "messages": messages, "stream": False,
            "max_tokens": int(max_tokens), "temperature": float(temperature),
        }
        specs = list(tools or [])
        if specs:
            body["tools"] = specs
            body["tool_choice"] = "auto"
        try:
            payload = self._request_raw(body, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "", "tool_calls": [], "usage": {},
                    "finish_reason": "",
                    "error": redact(f"{type(exc).__name__}: {str(exc)[:200]}"),
                    "elapsed": round(time.time() - started, 3)}
        if not isinstance(payload, dict):
            return {"ok": False, "text": "", "tool_calls": [], "usage": {},
                    "finish_reason": "", "error": "响应不是 JSON 对象",
                    "elapsed": round(time.time() - started, 3)}
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            detail = payload.get("error")
            if isinstance(detail, dict):
                detail = detail.get("message") or str(detail)
            return {"ok": False, "text": "", "tool_calls": [],
                    "usage": payload.get("usage") or {}, "finish_reason": "",
                    "error": f"响应缺少 choices: {str(detail or '')[:160]}",
                    "elapsed": round(time.time() - started, 3)}
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message")
        if not isinstance(message, dict):
            message = {}
        text = self._message_text(message)
        errors: list[str] = []
        calls: list[dict[str, Any]] = []
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list):
            for index, item in enumerate(raw_calls):
                if not isinstance(item, dict):
                    errors.append(f"第 {index} 个 tool_call 不是对象")
                    continue
                function = item.get("function")
                if not isinstance(function, dict):
                    errors.append(f"第 {index} 个 tool_call 缺少 function")
                    continue
                name = str(function.get("name") or "").strip()
                if not name:
                    errors.append(f"第 {index} 个 tool_call 缺少工具名")
                    continue
                calls.append({
                    "id": str(item.get("id") or f"call_{index}"),
                    "name": name,
                    "arguments": self._parse_arguments(
                        function.get("arguments"), name, errors),
                })
        if not calls and isinstance(message.get("function_call"), dict):
            # 兼容旧式 message.function_call（单工具调用）。
            function = message["function_call"]
            name = str(function.get("name") or "").strip()
            if name:
                calls.append({
                    "id": "call_legacy_0",
                    "name": name,
                    "arguments": self._parse_arguments(
                        function.get("arguments"), name, errors),
                })
            else:
                errors.append("function_call 缺少工具名")
        return {"ok": True, "text": text, "tool_calls": calls,
                "usage": payload.get("usage") or {},
                "finish_reason": str(choice.get("finish_reason") or ""),
                "error": "; ".join(errors),
                "elapsed": round(time.time() - started, 3)}

    def chat(self, messages: list[dict], *, purpose: str = "chat",
             max_tokens: int = 800, temperature: float = 0.7,
             json_mode: bool = False) -> dict[str, Any]:
        body = {"model": self.model, "messages": messages, "stream": False,
                "max_tokens": int(max_tokens), "temperature": float(temperature)}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        result = self._post(body)
        # deepseek-flash 可能把预算耗在 reasoning tokens 上，正文为空；
        # 真实网络请求遇到这种情况自动扩容重试一次（fake transport 不重试）。
        usage = result.get("usage") or {}
        reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        if (not result.get("ok") and self.transport is None
                and (reasoning >= int(max_tokens) * 0.8
                     or result.get("finish_reason") == "length")):
            retry = dict(body)
            retry["max_tokens"] = min(8192, max(int(max_tokens) * 3, 2048))
            second = self._post(retry)
            second["retried"] = True
            second["retry_max_tokens"] = retry["max_tokens"]
            return second
        return result
