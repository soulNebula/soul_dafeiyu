# -*- coding: utf-8 -*-
"""V1 PR8：bot 包模型配置 → 801/802 实际 LLM 调用点。

背景
----
`docs/plans/64` 的 D1 要求「灵魂模型 local / cloud 由用户在 bot 配置页选择」，
PR3 的 `idea/app/bot_manager.py::router_config` 只是把 bot 包翻译成
`agi/llm/router.py` 的配置结构（纯函数，803 侧用）。本模块是同一份转换逻辑的
**运行时唯一实现**：

- 801 网关 / 802 控制台启动时设置 ``SOULAGENT_BOT_ROOT``（或写 ``bots/active``），
  本模块把 bot 包里的 DeepSeek key / 灵魂 base+model 翻译成路由配置；
- ``agi/llm/router.py`` 的默认实例在 bot 生效时用这份配置（按 purpose 选 chat/soul）；
- ``agi/selflearn/llm.py`` 的 key 解析在 bot 生效时优先用 ``secrets/keys.json``，
  保证 `selflearn.llm.Client`（聊天 / 记忆 / 日记等既有调用点）用的是 bot 的 key；
- ``agi/selflearn/chat.SelfLearnAgent`` 在 bot 生效且调用方没有注入 client 时，
  默认 client 换成「路由客户端」，于是 `purpose="selflearn_chat"` 等既有调用点
  自动落到 bot 选的通道上（见 ``agi/llm/client.py``）。

没设置 bot 工作区时，本模块所有入口返回空/None，801/802 行为与改动前逐字节一致
（旧安装零变化）。

安全
----
- 密钥只在本进程内解密，函数返回值不含明文（``describe()`` 只给 bot_id/通道名）；
- 日志/异常统一脱敏（``sk-***``）；
- Windows 用 DPAPI 加密 ``secrets/keys.json``，其它平台退化为本机文件权限
  （与 PR3 的 bot_manager 完全同格式，两个入口可互读）。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import time
from pathlib import Path
from typing import Any

from agi.paths import bot_root as _bot_root

#: bot 包内相对路径
META_FILE = "bot.json"
APP_CONFIG_FILE = "config/app.json"
SECRETS_FILE = "secrets/keys.json"
WECHAT_CONFIG_FILE = "config/wechat.json"

#: 灵魂/聊天通道（与 bot_manager.router_config 的 purposes 一一对应）
CATEGORIES = ("chat", "soul", "vision", "extract")
DEFAULT_SOUL_BASE = "http://127.0.0.1:8123/v1"
DEFAULT_SOUL_MODEL = "soul-local"
DEFAULT_CHAT_MODEL = "deepseek-flash"
DEFAULT_API_BASE = "https://api.deepseek.com"

#: 已经解析过的 bot 运行期摘要（避免每次调用都读盘；进程内只读一次）
_ACTIVE: dict[str, Any] = {"key": None, "info": None}


class BotRuntimeError(ValueError):
    """bot 包配置非法（缺 json / soul_provider 取值错误）。"""


# --------------------------------------------------------------------------- #
# 基础读写
# --------------------------------------------------------------------------- #
def bot_dir() -> Path | None:
    """当前 bot 工作区；未配置返回 None（=沿用旧全局路径）。"""
    return _bot_root()


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def _redact(text: Any) -> str:
    value = str(text or "")
    for marker in ("sk-", "Bearer "):
        if marker in value:
            value = value.replace(marker, "***")
    return value


# --------------------------------------------------------------------------- #
# 密钥（与 idea/app/bot_manager.py 同格式，可互读）
# --------------------------------------------------------------------------- #
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, decrypt: bool = False) -> bytes | None:
    if os.name != "nt":
        return None
    try:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _Blob()
        fn = (ctypes.windll.crypt32.CryptUnprotectData if decrypt
              else ctypes.windll.crypt32.CryptProtectData)
        ok = fn(ctypes.byref(blob_in), None, None, None, None, 1,
                ctypes.byref(blob_out))
        if not ok:
            return None
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return raw
    except Exception:  # noqa: BLE001 - 无 DPAPI 时由调用方退化
        return None


def encode_secrets(payload: dict) -> dict:
    """把明文字典编码成 bot 包 ``secrets/keys.json`` 的落盘结构。"""
    raw = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    blob = _dpapi(raw)
    if blob is not None:
        return {"scheme": "dpapi", "data": base64.b64encode(blob).decode("ascii")}
    return {"scheme": "plain", "data": base64.b64encode(raw).decode("ascii"),
            "warning": "当前平台无 DPAPI，仅本机文件权限保护"}


def decode_secrets(encoded: dict | None) -> dict:
    """解码 ``secrets/keys.json``；损坏/非 dict 一律返回空 dict（不抛）。"""
    try:
        data = base64.b64decode(str((encoded or {}).get("data") or ""))
    except Exception:  # noqa: BLE001
        return {}
    if (encoded or {}).get("scheme") == "dpapi":
        data = _dpapi(data, decrypt=True) or b""
    try:
        payload = json.loads(data.decode("utf-8")) if data else {}
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_secrets(root: Path | str | None = None) -> dict:
    """读 bot 包密钥（已解密）；文件缺失/损坏返回空 dict。"""
    root = Path(root) if root is not None else bot_dir()
    if root is None:
        return {}
    return decode_secrets(_read_json(root / SECRETS_FILE, {}))


def load_bot(root: Path | str | None = None) -> dict:
    """读 bot 元信息 + 解密后的密钥（缺 bot.json 抛 BotRuntimeError）。"""
    root = Path(root) if root is not None else bot_dir()
    if root is None:
        raise BotRuntimeError("未配置 bot 工作区")
    meta = _read_json(root / META_FILE)
    if not isinstance(meta, dict):
        raise BotRuntimeError(f"bot 缺少/损坏 {META_FILE}：{root}")
    soul = str(meta.get("soul_provider") or "cloud")
    if soul not in ("local", "cloud"):
        raise BotRuntimeError(f"bot.json soul_provider 非法：{soul!r}")
    return {
        "id": str(meta.get("id") or root.name),
        "name": str(meta.get("name") or root.name),
        "plan": str(meta.get("plan") or "cloud"),
        "soul_provider": soul,
        "root": str(root),
        "secrets": load_secrets(root),
    }


# --------------------------------------------------------------------------- #
# 路由配置（与 bot_manager.router_config 同构，运行时唯一实现）
# --------------------------------------------------------------------------- #
def router_config(root: Path | str | None = None) -> dict:
    """把 bot 包翻译成 ``agi/llm/router.py`` 的配置结构。

    - chat：云端 DeepSeek（bot 的 ``llm_api_key``；模型/地址可被 app.json 的
      ``llm.model`` / ``llm.base_url`` 覆盖，缺省与历史一致）；
    - soul：``soul_provider=local`` → 本地 OpenAI 兼容服务（llama.cpp /v1），
      ``cloud`` → 云端 API；
    - vision / extract：跟随 soul 的本地能力，失败回落。
    """
    bot = load_bot(root)
    keys = bot["secrets"]
    meta_root = Path(bot["root"])
    app = _read_json(meta_root / APP_CONFIG_FILE, {}) or {}
    llm = app.get("llm") if isinstance(app.get("llm"), dict) else {}
    chat_model = str(llm.get("model") or DEFAULT_CHAT_MODEL)
    chat_base = str(llm.get("base_url") or DEFAULT_API_BASE).rstrip("/")
    local_base = str(keys.get("soul_api_base") or llm.get("soul_base_url")
                     or DEFAULT_SOUL_BASE).rstrip("/")
    local_model = str(keys.get("soul_model") or llm.get("soul_model")
                      or DEFAULT_SOUL_MODEL)
    soul_local = bot["soul_provider"] == "local"
    backends = {
        "api": {"kind": "deepseek", "model": chat_model, "base_url": chat_base,
                "key_source": "llm", "timeout_s": 30},
        "local": {"kind": "openai", "base_url": local_base,
                  "model": local_model, "timeout_s": 60},
        "rule": {"kind": "rule"},
    }
    soul_backend = "local" if soul_local else "api"
    # 云端灵魂可以有自己的 base/model/key（D1：用户填 OpenAI 兼容端点）。
    # 只有用户真的填了 soul key/base 才单开通道，否则沿用 api（历史行为不变）。
    soul_key = str(keys.get("soul_api_key") or "")
    soul_base = str(keys.get("soul_api_base") or "")
    if not soul_local and (soul_key or soul_base):
        backends["soul_api"] = {
            "kind": "deepseek",
            "model": str(keys.get("soul_model") or chat_model),
            "base_url": soul_base or chat_base,
            "key_source": "soul",
            "timeout_s": 30,
        }
        soul_backend = "soul_api"
    return {
        "backends": backends,
        "purposes": {
            "chat": {"backend": "api", "fallback": ["local", "rule"]},
            "soul": {"backend": soul_backend, "fallback": ["rule"]},
            "vision": {"backend": soul_backend, "fallback": ["api"]},
            "extract": {"backend": "local" if soul_local else "api",
                        "fallback": ["rule"]},
        },
    }


def config_key(root: Path | str | None = None) -> tuple:
    """bot 配置指纹（文件 mtime/size），供默认 Router 缓存失效用。"""
    root = Path(root) if root is not None else bot_dir()
    if root is None:
        return ()
    parts: list[Any] = [str(root)]
    for name in (META_FILE, APP_CONFIG_FILE, SECRETS_FILE, WECHAT_CONFIG_FILE):
        path = root / name
        try:
            stat = path.stat()
            parts.extend([int(stat.st_mtime_ns), int(stat.st_size)])
        except OSError:
            parts.extend([0, 0])
    return tuple(parts)


def active_router_config() -> dict | None:
    """bot 生效时返回路由配置；否则 None（调用方保持旧默认配置）。"""
    try:
        if bot_dir() is None:
            return None
        return router_config()
    except (BotRuntimeError, OSError):
        return None


def soul_provider(root: Path | str | None = None) -> str:
    """当前 bot 的灵魂通道（local/cloud）；未配置返回空串。"""
    try:
        return load_bot(root)["soul_provider"]
    except (BotRuntimeError, OSError):
        return ""


def usage_path(root: Path | str | None = None) -> Path | None:
    """bot 生效时，路由用量记到 bot 包 ``logs/llm_usage.jsonl``（隔离、可带走）。"""
    root = Path(root) if root is not None else bot_dir()
    if root is None:
        return None
    return root / "logs" / "llm_usage.jsonl"


# --------------------------------------------------------------------------- #
# 启动接线
# --------------------------------------------------------------------------- #
def initialize() -> dict:
    """801/802 启动时调用：解析 bot 配置并给出一行可打印摘要。

    返回值只含非敏感信息（bot_id / 通道 / 是否本地），可直接打印进日志。
    幂等：同一 bot 配置只解析一次（``reset()`` 可清缓存，测试/热加载用）。
    """
    root = bot_dir()
    if root is None:
        _ACTIVE["key"], _ACTIVE["info"] = None, {"active": False}
        return dict(_ACTIVE["info"], ok=True)
    key = config_key(root)
    if _ACTIVE["info"] is not None and _ACTIVE["key"] == key:
        return dict(_ACTIVE["info"])
    try:
        cfg = router_config(root)
        bot = load_bot(root)
        info = {
            "active": True, "ok": True, "bot_id": bot["id"],
            "bot_root": str(root), "soul_provider": bot["soul_provider"],
            "chat_backend": cfg["purposes"]["chat"]["backend"],
            "soul_backend": cfg["purposes"]["soul"]["backend"],
            "has_llm_key": bool(bot["secrets"].get("llm_api_key")),
            "local_base_url": cfg["backends"]["local"]["base_url"],
        }
    except (BotRuntimeError, OSError) as exc:
        info = {"active": True, "ok": False, "bot_root": str(root),
                "error": _redact(exc)}
    _ACTIVE["key"], _ACTIVE["info"] = key, info
    return dict(info)


def reset() -> None:
    """清运行期缓存（测试 / 改了 bot 包后热加载）。"""
    _ACTIVE["key"] = None
    _ACTIVE["info"] = None


# --------------------------------------------------------------------------- #
# 模型连通测试（PR8「补模型连通测试」）
# --------------------------------------------------------------------------- #
def _backend_overrides(cfg: dict, secrets: dict, *, transport=None,
                       timeout_s: float = 15.0) -> dict:
    """构造可注入的 backend 覆盖：api/soul_api 用 bot 的 key（不改进程环境变量）。

    注意：这里必须用调用方传入的 ``secrets``，不能用 ``agi.llm.deepseek.client_config``
    ——后者靠 ``SOULAGENT_BOT_ROOT`` 读盘，803 进程里没有这个环境变量。
    """
    from agi.llm.deepseek import DeepSeekBackend
    from agi.selflearn.llm import Client as _LLMClient

    overrides: dict[str, Any] = {}
    llm_key = str(secrets.get("llm_api_key") or "")
    soul_key = str(secrets.get("soul_api_key") or llm_key)
    for alias, spec in (cfg.get("backends") or {}).items():
        if str((spec or {}).get("kind") or "") != "deepseek":
            continue
        source = str(spec.get("key_source") or "llm")
        key = soul_key if source == "soul" else llm_key
        # 即使没有 key 也覆盖：bot 自检必须 fail-closed，不能回落到本机全局密钥库
        # （否则「测的是别人/旧环境的 key」，803 页面会给出假通过）。
        client_cfg = {
            "api_key": key,
            "base_url": str(spec.get("base_url") or DEFAULT_API_BASE),
            "model": str(spec.get("model") or DEFAULT_CHAT_MODEL),
        }
        overrides[str(alias)] = DeepSeekBackend(
            str(alias), model=client_cfg["model"], timeout_s=timeout_s,
            client=_LLMClient(client_cfg, transport=transport, timeout=timeout_s))
    if transport is not None:
        from agi.llm.openai_local import OpenAILocalBackend
        overrides["local"] = OpenAILocalBackend(
            "local",
            base_url=str((cfg["backends"].get("local") or {}).get("base_url") or ""),
            model=str((cfg["backends"].get("local") or {}).get("model") or ""),
            timeout_s=timeout_s, transport=transport)
    return overrides


def model_check(root: Path | str | None = None, *, config: dict | None = None,
                purposes: tuple[str, ...] = ("chat", "soul"),
                transport=None, timeout_s: float = 15.0,
                usage_path_override: Path | str | None = None) -> dict:
    """按 bot 既有的路由做一次最小模型调用（连通性自检，可离线注入 transport）。

    - 需要真实 key 才会打网络；没 key 的后端直接判不可用并给可操作 error；
    - ``transport(messages, body, timeout) -> payload`` 供单测/CI 注入，不联网；
    - 结果不含密钥，可原样返回给 802/803 页面。
    """
    from agi.llm.router import Router

    root = Path(root) if root is not None else bot_dir()
    try:
        secrets = load_secrets(root) if root is not None else {}
        cfg = config if isinstance(config, dict) else router_config(root)
    except (BotRuntimeError, OSError) as exc:
        return {"ok": False, "error": _redact(exc), "checks": []}
    overrides = _backend_overrides(cfg, secrets, transport=transport,
                                   timeout_s=timeout_s)
    log_path = usage_path_override or (usage_path(root) if root is not None else None)
    # 连通性只看各 purpose 的**主通道**：fallback 链会把「主通道连不上」测成通过
    check_cfg = dict(cfg)
    check_cfg["purposes"] = {
        name: dict(spec, fallback=[])
        for name, spec in (cfg.get("purposes") or {}).items()
    }
    router = Router(config=check_cfg, backend_overrides=overrides,
                    usage_path=log_path if log_path else None)
    checks = []
    for purpose in purposes:
        started = time.perf_counter()
        try:
            result = router.chat(
                purpose, [{"role": "user", "content": "只回复两个字：在。"}],
                max_tokens=8, temperature=0.0, timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001 - 自检不抛
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        target = str(((cfg.get("purposes") or {}).get(purpose) or {}).get("backend") or "")
        hint = ""
        if not result.get("ok"):
            target_key_source = str(((cfg.get("backends") or {}).get(target) or {})
                                    .get("key_source") or "llm")
            missing = (not secrets.get("llm_api_key") if target_key_source == "llm"
                       else not (secrets.get("soul_api_key")
                                 or secrets.get("llm_api_key")))
            if missing:
                hint = ("未填写 API key：在 803 新建/保存 bot 时填"
                        + ("灵魂模型 key" if target_key_source == "soul" else "DeepSeek key"))
            elif target == "local":
                hint = "本地模型服务未就绪：检查 bot 的灵魂模型 base_url 与 llama-server 端口"
            else:
                hint = "检查 key / base_url / 网络"
        checks.append({
            "purpose": purpose,
            "category": purpose,
            "configured_backend": target,
            "ok": bool(result.get("ok")),
            "backend": str(result.get("backend") or ""),
            "model": str(result.get("model") or ""),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": _redact(result.get("error") or ""),
            "hint": hint,
            "warnings": [str(item) for item in (result.get("warnings") or [])][:5],
        })
    return {
        "ok": all(item["ok"] for item in checks),
        "bot_id": (load_bot(root)["id"] if root is not None else ""),
        "soul_provider": (load_bot(root)["soul_provider"] if root is not None else ""),
        "checks": checks,
    }


def describe() -> dict:
    """诊断摘要（不含密钥）：给 802/803 的「模型」卡片用。"""
    root = bot_dir()
    if root is None:
        return {"active": False, "bot_root": ""}
    try:
        bot = load_bot(root)
        cfg = router_config(root)
    except (BotRuntimeError, OSError) as exc:
        return {"active": True, "bot_root": str(root), "ok": False,
                "error": _redact(exc)}
    return {
        "active": True, "ok": True, "bot_id": bot["id"],
        "bot_root": str(root), "name": bot["name"],
        "soul_provider": bot["soul_provider"],
        "chat_model": (cfg["backends"]["api"] or {}).get("model", ""),
        "local_model": (cfg["backends"]["local"] or {}).get("model", ""),
        "local_base_url": (cfg["backends"]["local"] or {}).get("base_url", ""),
        "has_llm_key": bool(bot["secrets"].get("llm_api_key")),
        "has_soul_key": bool(bot["secrets"].get("soul_api_key")),
        "purposes": {name: spec.get("backend")
                      for name, spec in (cfg.get("purposes") or {}).items()},
    }