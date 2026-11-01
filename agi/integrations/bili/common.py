# -*- coding: utf-8 -*-
"""B站模块公共件：HTTP、限频、凭证（加密）存取。

设计约束（docs/plans/64 §10）：
- 只依赖标准库，缺依赖不报 ImportError（新 clone 也能 import）；
- 凭证优先落 bot 包 ``secrets/bili.json``（DPAPI 加密，可随 bot 包带走）；
  未启用 bot 时落 ``agi/data/bili/credential.json``；
- 兼容仓库外 ``~/.dsh/bili-duty/bili_credential.json``：只读迁移，不写回插件；
- 所有网络调用过 :func:`rate_limit`（默认 1.5s 一次，可用环境变量调）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124 Safari/537.36")
REFERER = "https://www.bilibili.com/"
DEFAULT_TIMEOUT_S = 8.0
DEFAULT_RATE_S = 1.5
#: 仓库外插件的旧凭证路径（只读迁移源）
LEGACY_CREDENTIAL = Path.home() / ".dsh" / "bili-duty" / "bili_credential.json"

_RATE: dict[str, Any] = {"last": 0.0, "interval": None}


class BiliError(RuntimeError):
    """B站接口/凭证错误（调用方转成页面提示，不静默成功）。"""


# --------------------------------------------------------------------------- #
# 限频
# --------------------------------------------------------------------------- #
def _rate_interval() -> float:
    if _RATE["interval"] is not None:
        return float(_RATE["interval"])
    raw = os.environ.get("BILI_RATE_LIMIT_S")
    try:
        return max(0.0, float(raw)) if raw not in (None, "") else DEFAULT_RATE_S
    except ValueError:
        return DEFAULT_RATE_S


def configure_rate_limit(interval_s: float | None) -> None:
    """设置最小请求间隔（秒）；None = 回环境变量/默认值。测试里设 0。"""
    _RATE["interval"] = None if interval_s is None else max(0.0, float(interval_s))


def reset_rate_limit() -> None:
    """清掉「上次调用时间」（测试用）。"""
    _RATE["last"] = 0.0
    _RATE["interval"] = None


def rate_limit(interval_s: float | None = None) -> None:
    """按最小间隔节流（单进程内，够 V1 用；多进程不共享）。"""
    interval = _rate_interval() if interval_s is None else max(0.0, float(interval_s))
    if interval <= 0:
        return
    wait = interval - (time.time() - float(_RATE["last"] or 0.0))
    if wait > 0:
        time.sleep(wait)
    _RATE["last"] = time.time()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _headers(extra: dict | None = None) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Referer": REFERER,
               "Accept": "application/json, text/plain, */*"}
    headers.update({str(k): str(v) for k, v in (extra or {}).items() if v})
    return headers


def http_json(url: str, *, headers: dict | None = None,
              timeout: float = DEFAULT_TIMEOUT_S, throttle: bool = True) -> dict:
    """GET JSON；网络/解析失败抛 BiliError（带脱敏原因）。"""
    if throttle:
        rate_limit()
    request = urllib.request.Request(url, headers=_headers(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - 统一成 BiliError
        raise BiliError(f"http_failed:{type(exc).__name__}") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise BiliError("http_bad_json") from exc
    if not isinstance(payload, dict):
        raise BiliError("http_bad_payload")
    return payload


def http_bytes(url: str, *, headers: dict | None = None,
               timeout: float = DEFAULT_TIMEOUT_S, throttle: bool = True) -> bytes:
    """GET 原始字节（弹幕 XML 用；B站会对 list.so 做 deflate）。"""
    if throttle:
        rate_limit()
    request = urllib.request.Request(url, headers=_headers(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except Exception as exc:  # noqa: BLE001
        raise BiliError(f"http_failed:{type(exc).__name__}") from exc


# --------------------------------------------------------------------------- #
# 凭证
# --------------------------------------------------------------------------- #
def _data_root() -> Path:
    """数据目录统一走 agi.paths（测试隔离/环境变量重定向都靠它）。"""
    from agi.paths import data_dir
    return data_dir()


def credential_path() -> Path:
    """凭证落盘路径：``BILI_CREDENTIAL_PATH`` > bot 包 ``secrets/bili.json`` > 仓库数据目录。"""
    raw = str(os.environ.get("BILI_CREDENTIAL_PATH") or "").strip()
    if raw:
        return Path(os.path.expanduser(os.path.expandvars(raw)))
    try:
        from agi import bot_runtime
        bot = bot_runtime.bot_dir()
        if bot is not None:
            return bot / "secrets" / "bili.json"
    except Exception:  # noqa: BLE001 - 旧部署不受 bot 模块影响
        pass
    return _data_root() / "bili" / "credential.json"


def _normalize(credential: dict | None) -> dict:
    """统一成小写键 ``sessdata/bili_jct/dedeuserid/buvid3``（兼容插件原格式）。"""
    source = {str(k).lower(): v for k, v in (credential or {}).items()}
    alias = ("sessdata", "bili_jct", "dedeuserid", "buvid3")
    result = {}
    for name in alias:
        value = source.get(name)
        if value:
            result[name] = str(value)
    if not result and credential:
        # 插件里可能直接是 Cookie 字符串
        text = str(credential.get("cookie") or "")
        for part in text.split(";"):
            name, _, value = part.strip().partition("=")
            if name.lower() in ("sessdata", "bili_jct", "dedeuserid", "buvid3") and value:
                result[name.lower()] = value
    return result


def load_credential() -> dict:
    """读凭证：仓库/bot 优先，缺失时只读迁移旧插件文件；返回小写键 dict。"""
    path = credential_path()
    payload = _read_json(path)
    if isinstance(payload, dict) and payload.get("scheme"):
        from agi import bot_runtime
        payload = bot_runtime.decode_secrets(payload)
    normalized = _normalize(payload if isinstance(payload, dict) else {})
    if normalized:
        return normalized
    legacy = _read_json(LEGACY_CREDENTIAL)
    if isinstance(legacy, dict):
        return _normalize(legacy)
    return {}


def credential_state() -> dict:
    """凭证来源：repo（仓库/bot 包）/ legacy（旧插件，只读）/ none。"""
    path = credential_path()
    payload = _read_json(path)
    normalized = {}
    if isinstance(payload, dict):
        decoded = payload
        if payload.get("scheme"):
            try:
                from agi import bot_runtime
                decoded = bot_runtime.decode_secrets(payload)
            except Exception:  # noqa: BLE001 - 解不开就当没有
                decoded = {}
        normalized = _normalize(decoded if isinstance(decoded, dict) else {})
    if normalized:
        return {"source": "repo", "path": str(path)}
    if _normalize(_read_json(LEGACY_CREDENTIAL)):
        return {"source": "legacy", "path": str(LEGACY_CREDENTIAL)}
    return {"source": "none", "path": str(path)}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def save_credential(credential: dict, *, path: Path | None = None) -> Path:
    """加密写凭证（bot 生效走 DPAPI，同 secrets 口径）；返回落盘路径。"""
    normalized = _normalize(credential)
    if not normalized.get("sessdata"):
        raise BiliError("credential_incomplete: 缺少 SESSDATA")
    normalized["saved_at"] = round(time.time(), 3)
    target = Path(path) if path is not None else credential_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from agi import bot_runtime
        encoded = bot_runtime.encode_secrets(normalized)
    except Exception:  # noqa: BLE001 - 极端情况下退化为明文（仅本机权限保护）
        encoded = {"scheme": "plain", "data": normalized}
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(encoded, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def import_legacy_credential(*, path: Path | None = None) -> dict:
    """把旧插件凭证迁进仓库/bot（幂等）；返回 ``{ok, path, imported}``。"""
    source = Path(path) if path is not None else LEGACY_CREDENTIAL
    legacy = _read_json(source)
    normalized = _normalize(legacy if isinstance(legacy, dict) else {})
    if not normalized:
        return {"ok": False, "error": "legacy_credential_missing", "path": str(source)}
    target = save_credential(normalized)
    return {"ok": True, "path": str(target), "imported": sorted(normalized)}


def cookie_header(credential: dict | None = None) -> str:
    """小写键凭证 → Cookie 头（B站用大写名）。"""
    cred = credential if credential is not None else load_credential()
    names = {"sessdata": "SESSDATA", "bili_jct": "bili_jct",
             "dedeuserid": "DedeUserID", "buvid3": "buvid3"}
    return "; ".join(f"{names[key]}={value}"
                     for key, value in _normalize(cred).items() if value)


def redact(text: Any) -> str:
    """错误信息脱敏（凭证值绝不进日志/接口）。"""
    value = str(text or "")
    for key in ("SESSDATA", "sessdata", "bili_jct", "DedeUserID", "dedeuserid"):
        if key in value:
            value = value.replace(key, "***")
    return value