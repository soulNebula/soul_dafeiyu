# -*- coding: utf-8 -*-
"""P13 统一 LLM 路由层。

所有需要「模型」的功能都从这里进：按 **purpose（用途）** 选后端，配置在
``agi/data/llm_router.json``（可环境变量/参数覆盖）：

- 花钱的（需要判断力/人话）走 API（DeepSeek）；
- 高量、长文本的提取类走本地 OpenAI 兼容服务（llama-server 8125）；
- ``video_understanding`` **must_api=true**：视频真理解必须走 API，本地不可用；
- 后端失败按用途的 ``fallback`` 链降级（如 local → api / api → local / → rule）；
- 每次调用写 ``agi/data/llm_usage.jsonl``；``usage_summary()`` 给控制台统计页；
- 每日 API token 预算（``daily_api_tokens``）超限后按 ``on_exceed`` 自动降级本地。

设计约束：
- 本层**只新增**，不改现有业务调用（接线由后续任务做）；
- 后端可注入（``backend_overrides`` / ``transport``），单测不联网；
- 记账失败、探活失败、降级失败都显式返回 ``ok:False + error``，不静默成功。
"""
from __future__ import annotations

import inspect
import json
import os
import time
from pathlib import Path
from typing import Any

from . import usage as usage_mod
from .deepseek import DeepSeekBackend
from .openai_local import OpenAILocalBackend
from .rule import RuleBackend

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "agi" / "data" / "llm_router.json"
DEFAULT_USAGE_PATH = ROOT / "agi" / "data" / "llm_usage.jsonl"
KINDS = ("openai", "deepseek", "rule")
BACKEND_CLASSES = {"openai": OpenAILocalBackend, "deepseek": DeepSeekBackend,
                   "rule": RuleBackend}
DEFAULT_API_ALIAS = "api"
DEFAULT_LOCAL_ALIAS = "local"
DEFAULT_ON_EXCEED = "local"


class RouterConfigError(ValueError):
    """路由配置非法（JSON 坏 / 缺字段 / 未知 backend kind）——明确报错，不静默。"""


# ------------------------------------------------------------------ 配置
def _as_path(path: Path | str | None) -> Path:
    if path is None:
        return Path(os.environ.get("LLM_ROUTER_CONFIG") or DEFAULT_CONFIG_PATH)
    return Path(path)


def _resolve_usage_path(cfg: dict, usage_path: Path | str | None = None) -> Path:
    if usage_path is not None:
        return Path(usage_path)
    env = os.environ.get("LLM_USAGE_PATH")
    if env:
        return Path(env)
    raw = str(cfg.get("log_path") or "").strip()
    target = Path(raw) if raw else DEFAULT_USAGE_PATH
    return target if target.is_absolute() else ROOT / target


def _normalize_config(raw: Any, source: str = "config") -> dict:
    if not isinstance(raw, dict):
        raise RouterConfigError(f"{source}: 顶层必须是 JSON 对象")
    backends = raw.get("backends")
    purposes = raw.get("purposes")
    if not isinstance(backends, dict) or not backends:
        raise RouterConfigError(f"{source}: backends 必须是非空对象")
    if not isinstance(purposes, dict) or not purposes:
        raise RouterConfigError(f"{source}: purposes 必须是非空对象")
    normalized_backends: dict[str, dict] = {}
    for alias, spec in backends.items():
        if not isinstance(spec, dict):
            raise RouterConfigError(f"{source}: backend {alias!r} 必须是对象")
        kind = str(spec.get("kind") or "").strip().lower()
        if kind not in KINDS:
            raise RouterConfigError(
                f"{source}: backend {alias!r} 的 kind 非法：{kind!r}（可选 {KINDS}）")
        item = dict(spec)
        item["kind"] = kind
        if kind == "openai" and not str(item.get("base_url") or "").strip():
            raise RouterConfigError(f"{source}: openai backend {alias!r} 缺少 base_url")
        item.setdefault("model", "local-3b" if kind == "openai" else "")
        normalized_backends[str(alias)] = item
    normalized_purposes: dict[str, dict] = {}
    for purpose, spec in purposes.items():
        if not isinstance(spec, dict) or not str(spec.get("backend") or "").strip():
            raise RouterConfigError(f"{source}: purpose {purpose!r} 缺少 backend")
        item = dict(spec)
        item["backend"] = str(item["backend"]).strip()
        fallback = item.get("fallback")
        if fallback is None:
            item["fallback"] = []
        elif isinstance(fallback, str):
            item["fallback"] = [fallback]
        elif not isinstance(fallback, list):
            raise RouterConfigError(f"{source}: purpose {purpose!r} 的 fallback 必须是数组")
        else:
            item["fallback"] = [str(x) for x in fallback]
        item["must_api"] = bool(item.get("must_api"))
        normalized_purposes[str(purpose)] = item
    budget = raw.get("budget") if isinstance(raw.get("budget"), dict) else {}
    try:
        daily = int(budget.get("daily_api_tokens") or 0)
    except (TypeError, ValueError) as exc:
        raise RouterConfigError(f"{source}: budget.daily_api_tokens 必须是整数") from exc
    cfg = {
        "backends": normalized_backends,
        "purposes": normalized_purposes,
        "budget": {"daily_api_tokens": max(0, daily),
                   "on_exceed": str(budget.get("on_exceed") or DEFAULT_ON_EXCEED)},
        "log_path": str(raw.get("log_path") or ""),
    }
    return cfg


def validate_config(cfg: dict) -> list[str]:
    """返回配置警告（不抛）：purpose 的 backend/fallback 指向未定义 backend 等。

    例如 ``transcribe`` 的 ``local_whisper`` 由后续任务（P14）提供——现在调用它会
    明确返回 ``unknown_backend``，而不是静默当本地处理。
    """
    warnings: list[str] = []
    backends = cfg.get("backends") or {}
    for purpose, spec in (cfg.get("purposes") or {}).items():
        backend = str(spec.get("backend") or "")
        if backend not in backends:
            warnings.append(f"purpose {purpose!r} 的 backend {backend!r} 未定义")
        for alias in spec.get("fallback") or []:
            if alias not in backends:
                warnings.append(f"purpose {purpose!r} 的 fallback {alias!r} 未定义")
    return warnings


def load_config(path: Path | str | None = None) -> dict:
    """读取并校验路由配置；坏 JSON / 缺字段 / 未知 kind 抛 ``RouterConfigError``。"""
    target = _as_path(path)
    # 运行时配置（agi/data/llm_router.json）缺失时回退仓库自带的默认配置，
    # 保证新机器/干净 checkout 也能直接跑（默认配置只读、不含密钥）。
    default_path = Path(__file__).with_name("llm_router.default.json")
    if path is None and not target.is_file() and default_path.is_file():
        target = default_path
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RouterConfigError(f"路由配置不存在：{target}") from exc
    except (OSError, ValueError) as exc:
        raise RouterConfigError(f"路由配置读取/解析失败：{target}: {exc}") from exc
    return _normalize_config(raw, source=str(target))


# ------------------------------------------------------------------ Router
def _backend_available(backend) -> bool:
    try:
        return bool(backend.available())
    except Exception:  # noqa: BLE001
        return False


class Router:
    """按 purpose 路由的后端调度器（可注入后端/时钟/记账路径，便于单测）。"""

    def __init__(self, config: dict | None = None, *,
                 config_path: Path | str | None = None,
                 backend_overrides: dict[str, Any] | None = None,
                 usage_path: Path | str | None = None,
                 now_fn=None) -> None:
        if config is None:
            cfg = load_config(config_path)
        else:
            cfg = _normalize_config(config, source="config")
        self.config = cfg
        self.warnings = validate_config(cfg)
        self.log_path = _resolve_usage_path(cfg, usage_path)
        self._overrides = dict(backend_overrides or {})
        self._backends: dict[str, Any] = {}
        self._now = now_fn or time.time
        self._api_aliases = [alias for alias, spec in cfg["backends"].items()
                             if spec.get("kind") == "deepseek"]

    # -- 查询 -------------------------------------------------------------
    def purposes(self) -> dict:
        return dict(self.config.get("purposes") or {})

    def available_backends(self) -> dict:
        out = {}
        for alias, spec in (self.config.get("backends") or {}).items():
            backend = self._backend(alias)
            out[alias] = {"kind": spec.get("kind"),
                          "available": _backend_available(backend) if backend else False}
        return out

    def _api_alias(self) -> str:
        if DEFAULT_API_ALIAS in self._api_aliases:
            return DEFAULT_API_ALIAS
        return self._api_aliases[0] if self._api_aliases else ""

    def _local_alias(self) -> str:
        backends = self.config.get("backends") or {}
        if DEFAULT_LOCAL_ALIAS in backends and backends[DEFAULT_LOCAL_ALIAS].get("kind") == "openai":
            return DEFAULT_LOCAL_ALIAS
        for alias, spec in backends.items():
            if spec.get("kind") == "openai":
                return alias
        return ""

    def _backend(self, alias: str):
        if not alias:
            return None
        if alias in self._overrides:
            return self._overrides[alias]
        if alias in self._backends:
            return self._backends[alias]
        spec = (self.config.get("backends") or {}).get(alias)
        if not isinstance(spec, dict):
            return None
        cls = BACKEND_CLASSES.get(str(spec.get("kind") or ""))
        if cls is None:
            return None
        params = set(inspect.signature(cls.__init__).parameters) - {"self", "name"}
        kwargs = {key: value for key, value in spec.items()
                  if key != "kind" and key in params}
        try:
            backend = cls(name=alias, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 构造失败视为不可用
            self.warnings.append(f"backend {alias!r} 构造失败：{type(exc).__name__}: {exc}")
            return None
        self._backends[alias] = backend
        return backend

    def backend_kind(self, alias: str) -> str:
        if alias in self._overrides:
            return str(getattr(self._overrides[alias], "kind", "") or "")
        spec = (self.config.get("backends") or {}).get(alias) or {}
        return str(spec.get("kind") or "")

    def daily_api_tokens(self, now: float | None = None) -> int:
        return usage_mod.api_tokens_today(self.log_path, now=self._now() if now is None else now)

    # -- 核心入口 ---------------------------------------------------------
    def chat(self, purpose: str, messages: list[dict], *,
             max_tokens: int | None = None, temperature: float | None = None,
             timeout: float | None = None, json_mode: bool = False) -> dict:
        started = time.perf_counter()
        purpose = str(purpose or "")
        pcfg = (self.config.get("purposes") or {}).get(purpose)
        if not isinstance(pcfg, dict):
            error = f"unknown_purpose:{purpose}"
            record = self._base_record(purpose, "", "", 0, 0, started, False, error)
            usage_mod.append_record(self.log_path, record)
            return {"ok": False, "text": "", "backend": "", "purpose": purpose,
                    "usage": {"tokens_in": 0, "tokens_out": 0},
                    "latency_ms": self._elapsed(started), "fallback_used": False,
                    "budget_exceeded": False, "error": error,
                    "warnings": list(self.warnings), "attempts": []}

        call_warnings: list[str] = []
        must_api = bool(pcfg.get("must_api"))
        configured = str(pcfg.get("backend") or "")
        chosen = configured
        if must_api:
            chosen = self._api_alias()
            if not chosen:
                return self._fail(purpose, "must_api_no_api_backend", started,
                                  must_api=True, warnings=call_warnings)
            if configured != chosen:
                call_warnings.append(
                    f"must_api_forced_api: purpose={purpose} configured={configured}")
        max_tokens = int(max_tokens if max_tokens is not None
                         else pcfg.get("max_tokens") or 800)
        temperature = float(temperature if temperature is not None
                            else pcfg.get("temperature", 0.7))

        chain = [chosen] + [alias for alias in (pcfg.get("fallback") or [])
                            if alias and alias != chosen]
        budget_exceeded = False
        budget_cfg = self.config.get("budget") or {}
        daily = int(budget_cfg.get("daily_api_tokens") or 0)
        if daily > 0 and self.backend_kind(chosen) == "deepseek" \
                and self.daily_api_tokens() >= daily:
            budget_exceeded = True
            if must_api:
                return self._fail(purpose, "budget_exceeded:must_api", started,
                                  must_api=True, budget_exceeded=True,
                                  warnings=call_warnings)
            on_exceed = str(budget_cfg.get("on_exceed") or DEFAULT_ON_EXCEED)
            if on_exceed == "local":
                local = self._local_alias()
                if local:
                    call_warnings.append(
                        f"budget_exceeded:degrade_to_local purpose={purpose}")
                    chain = [local] + [alias for alias in chain if alias != local]
                else:
                    call_warnings.append("budget_exceeded:no_local_backend")
            elif on_exceed == "none":
                return self._fail(purpose, "budget_exceeded", started,
                                  budget_exceeded=True, warnings=call_warnings)
            # 其它值按 local 处理（安全默认）

        attempts: list[dict] = []
        fallback_used = budget_exceeded
        for index, alias in enumerate(chain):
            backend = self._backend(alias)
            if backend is None:
                attempts.append({"backend": alias, "ok": False,
                                 "error": "unknown_backend"})
                continue
            if not _backend_available(backend):
                attempts.append({"backend": alias, "ok": False,
                                 "error": "unavailable"})
                continue
            if index > 0:
                fallback_used = True
            try:
                result = backend.chat(messages, purpose=purpose,
                                      max_tokens=max_tokens,
                                      temperature=temperature,
                                      timeout=timeout, json_mode=json_mode)
            except Exception as exc:  # noqa: BLE001 - 后端异常统一降级
                result = {"ok": False, "text": "",
                          "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            result = result if isinstance(result, dict) else {}
            text = str(result.get("text") or "")
            if result.get("ok") and text:
                tokens_in, tokens_out = self._tokens(result.get("usage"))
                latency = self._elapsed(started)
                record = self._base_record(purpose, alias, self.backend_kind(alias),
                                           tokens_in, tokens_out, started, True, "")
                record.update({"fallback": bool(fallback_used),
                               "budget_exceeded": bool(budget_exceeded),
                               "must_api": must_api,
                               "warnings": call_warnings})
                usage_mod.append_record(self.log_path, record)
                return {"ok": True, "text": text, "backend": alias,
                        "backend_kind": self.backend_kind(alias),
                        "purpose": purpose,
                        "usage": {"tokens_in": tokens_in, "tokens_out": tokens_out},
                        "latency_ms": latency, "fallback_used": bool(fallback_used),
                        "budget_exceeded": bool(budget_exceeded),
                        "error": "", "model": result.get("model", ""),
                        "warnings": list(self.warnings) + call_warnings,
                        "attempts": attempts}
            attempts.append({"backend": alias, "ok": False,
                             "error": str(result.get("error") or "empty_text")[:200]})
        error = "all_backends_failed: " + "; ".join(
            f"{item['backend']}={item['error']}" for item in attempts) or "no_backend"
        return self._fail(purpose, error, started, attempts=attempts,
                          fallback_used=fallback_used,
                          budget_exceeded=budget_exceeded, must_api=must_api,
                          warnings=call_warnings)

    def _fail(self, purpose: str, error: str, started: float, *,
              attempts: list[dict] | None = None, fallback_used: bool = False,
              budget_exceeded: bool = False, must_api: bool = False,
              warnings: list[str] | None = None) -> dict:
        latency = self._elapsed(started)
        record = self._base_record(purpose, "", "", 0, 0, started, False, error)
        record.update({"fallback": bool(fallback_used),
                       "budget_exceeded": bool(budget_exceeded),
                       "must_api": bool(must_api),
                       "warnings": list(warnings or [])})
        usage_mod.append_record(self.log_path, record)
        return {"ok": False, "text": "", "backend": "", "purpose": purpose,
                "usage": {"tokens_in": 0, "tokens_out": 0},
                "latency_ms": latency, "fallback_used": bool(fallback_used),
                "budget_exceeded": bool(budget_exceeded), "error": error,
                "warnings": list(self.warnings) + list(warnings or []),
                "attempts": list(attempts or [])}

    # -- 小工具 -----------------------------------------------------------
    def _elapsed(self, started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    @staticmethod
    def _tokens(raw: Any) -> tuple[int, int]:
        data = raw if isinstance(raw, dict) else {}
        try:
            return int(data.get("tokens_in") or 0), int(data.get("tokens_out") or 0)
        except (TypeError, ValueError):
            return 0, 0

    def _base_record(self, purpose: str, backend: str, backend_kind: str,
                     tokens_in: int, tokens_out: int, started: float,
                     ok: bool, error: str) -> dict:
        return {
            "ts": round(float(self._now()), 3),
            "purpose": str(purpose),
            "backend": str(backend),
            "backend_kind": str(backend_kind),
            "tokens_in": int(tokens_in),
            "tokens_out": int(tokens_out),
            "latency_ms": self._elapsed(started),
            "ok": bool(ok),
            "fallback": False,
            "budget_exceeded": False,
            "error": str(error or "")[:300],
        }


# ------------------------------------------------------------------ 默认实例/便捷函数
_DEFAULT_ROUTER: dict[str, Any] = {"key": None, "router": None}


def _config_key(path: Path | None = None) -> tuple:
    target = _as_path(path)
    try:
        stat = target.stat()
        return (str(target), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return (str(target), 0, 0)


def default_router() -> Router:
    key = _config_key()
    if _DEFAULT_ROUTER["router"] is None or _DEFAULT_ROUTER["key"] != key:
        _DEFAULT_ROUTER["router"] = Router()
        _DEFAULT_ROUTER["key"] = key
    return _DEFAULT_ROUTER["router"]


def reset_default_router() -> None:
    """清掉默认实例缓存（测试/改配置后用）。"""
    _DEFAULT_ROUTER["router"] = None
    _DEFAULT_ROUTER["key"] = None


def chat(purpose: str, messages: list[dict], **kwargs) -> dict:
    """模块级入口：按用途路由一次模型调用（用默认配置的 Router）。"""
    return default_router().chat(purpose, messages, **kwargs)


def purposes(config_path: Path | str | None = None) -> dict:
    """调试用：返回用途 → 路由配置。"""
    return load_config(config_path).get("purposes") or {}


def available_backends(config_path: Path | str | None = None) -> dict:
    """调试用：返回 backend alias → {kind, available}（会做一次探活）。"""
    return Router(config_path=config_path).available_backends()


def usage_summary(days: int = 7, *, path: Path | str | None = None,
                  now: float | None = None) -> dict:
    """最近 N 天用量汇总（按天/用途/后端）；供控制台统计页。"""
    target = path
    if target is None:
        env = os.environ.get("LLM_USAGE_PATH")
        if env:
            target = Path(env)
        else:
            try:
                cfg = load_config()
                target = _resolve_usage_path(cfg)
            except RouterConfigError:
                target = DEFAULT_USAGE_PATH
    return usage_mod.summary(target, days=days, now=now)
