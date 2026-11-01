# -*- coding: utf-8 -*-
"""DeepSeek API 后端 —— 直接复用 ``agi.selflearn.llm.Client``（不重复造轮子）。

审计点：这是路由层里唯一会**花钱**的后端；``chat_reply`` / ``mood_judge`` /
``video_understanding`` 等需要判断力的用途走它，高量提取类走本地 8125。

V1 PR8：``key_source`` 让同一个后端取不同来源的凭证——
``llm`` 用当前 bot 的聊天 key（未启用 bot 时与历史一致：env / 密钥库 / web_config），
``soul`` 用 bot 包的灵魂 key/base/model（云端灵魂方案）。密钥只在本进程内解密，
不写日志、不进配置字典。
"""
from __future__ import annotations

from typing import Any

LAYER_NAME = "deepseek"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_BASE_URL = "https://api.deepseek.com"


def client_config(key_source: str, base_url: str, model: str) -> dict[str, Any]:
    """按 key_source 解析 client 配置；bot 未启用时返回空 dict（走旧 Client 路径）。"""
    source = str(key_source or "llm")
    try:
        from agi import bot_runtime
        if bot_runtime.bot_dir() is None:
            return {}
        secrets = bot_runtime.load_secrets()
    except Exception:  # noqa: BLE001 - bot 模块异常不影响旧部署
        return {}
    if source == "soul":
        return {
            "api_key": str(secrets.get("soul_api_key")
                           or secrets.get("llm_api_key") or ""),
            "base_url": str(secrets.get("soul_api_base") or base_url
                            or DEFAULT_BASE_URL),
            "model": str(secrets.get("soul_model") or model or ""),
        }
    return {
        "api_key": str(secrets.get("llm_api_key") or ""),
        "base_url": str(base_url or DEFAULT_BASE_URL),
        "model": str(model or ""),
    }


class DeepSeekBackend:
    """包装 ``agi.selflearn.llm.Client``（生产同款：读 web_config.json 的 key/model）。"""

    kind = "deepseek"

    def __init__(self, name: str = "api", *, model: str | None = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S, client=None,
                 base_url: str = "", key_source: str = "llm") -> None:
        self.name = str(name)
        self.timeout_s = float(timeout_s or DEFAULT_TIMEOUT_S)
        self.model = str(model or "")
        self.base_url = str(base_url or "")
        self.key_source = str(key_source or "llm")
        if client is not None:
            self.client = client
        else:  # 延迟 import：llm 路由包被 import 时不强行拉 selflearn 依赖
            from ..selflearn.llm import Client
            cfg = client_config(self.key_source, self.base_url, self.model)
            self.client = Client(cfg, timeout=self.timeout_s) if cfg else Client(
                timeout=self.timeout_s)

    def available(self) -> bool:
        try:
            return bool(self.client.available())
        except Exception:  # noqa: BLE001
            return False

    def chat(self, messages: list[dict], *, purpose: str = "chat",
             max_tokens: int = 800, temperature: float = 0.7,
             timeout: float | None = None, json_mode: bool = False) -> dict:
        """调用 DeepSeek；失败返回 ``{ok: False, error}``（不抛异常）。

        ``timeout`` 由 client 构造时的 timeout 决定（Client.chat 不接受逐次 timeout），
        这里保留参数只为与其它后端签名一致；需要短超时就传 ``timeout_s`` 构造 client。
        """
        try:
            result = self.client.chat(
                list(messages or []), purpose=str(purpose or "chat"),
                max_tokens=int(max_tokens), temperature=float(temperature),
                json_mode=bool(json_mode))
        except Exception as exc:  # noqa: BLE001 - 路由层统一兜底
            return {"ok": False, "text": "",
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "model": self.model}
        result = result if isinstance(result, dict) else {}
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        text = str(result.get("text") or "")
        return {
            "ok": bool(result.get("ok")) and bool(text),
            "text": text,
            "error": "" if text else str(result.get("error") or "空回复"),
            "usage": {
                "tokens_in": int(usage.get("prompt_tokens") or 0),
                "tokens_out": int(usage.get("completion_tokens") or 0),
            },
            "model": str(result.get("model") or self.model),
            "elapsed": result.get("elapsed"),
        }