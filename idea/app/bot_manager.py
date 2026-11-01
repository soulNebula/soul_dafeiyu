# -*- coding: utf-8 -*-
"""V1 PR3：803 Bot Manager（bot 包创建/列表/保存/校验/启动 802）。

设计见 docs/plans/64；这里只做最小可用：
- bot 包固定在仓库 ``bots/<id>/``（单 bot 运行，多 bot 并发暂不支持）；
- 密钥写 ``secrets/keys.json``：Windows 用 DPAPI，其它平台退化为 base64（CI/开发）；
- 802 密码只存 PBKDF2 哈希，不存明文；
- 启动 802 用子进程 + ``SOULAGENT_BOT_ROOT``，不触碰 801 现有实例。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

IDEA_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = IDEA_ROOT.parent
BOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
REQUIRED_DIRS = ("config", "db/selflearn/wechat_contacts", "models", "prompts",
                 "media", "logs", "secrets")


class BotError(ValueError):
    pass


def bots_root() -> Path:
    raw = os.environ.get("SOULAGENT_BOTS_DIR", "").strip()
    return Path(raw) if raw else REPO_ROOT / "bots"


def _bot_dir(bot_id: str) -> Path:
    text = str(bot_id or "").strip()
    if not BOT_ID_RE.match(text):
        raise BotError("bot id 只能用 2–41 位小写字母/数字/连字符")
    return bots_root() / text


def _write_json(path: Path, payload: dict, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    if mode is not None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ------------------------------------------------------------------ 密钥
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, decrypt: bool = False) -> bytes | None:
    if os.name != "nt":
        return None
    try:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _Blob()
        fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
        ok = fn(ctypes.byref(blob_in), None, None, None, None, 1, ctypes.byref(blob_out))
        if not ok:
            return None
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return raw
    except Exception:  # noqa: BLE001
        return None


def _encode_secrets(payload: dict) -> dict:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    blob = _dpapi(raw)
    if blob is not None:
        return {"scheme": "dpapi", "data": base64.b64encode(blob).decode("ascii")}
    return {"scheme": "plain", "data": base64.b64encode(raw).decode("ascii"),
            "warning": "当前平台无 DPAPI，仅本机文件权限保护"}


def decode_secrets(encoded: dict) -> dict:
    try:
        data = base64.b64decode(str((encoded or {}).get("data") or ""))
    except Exception:  # noqa: BLE001
        return {}
    if (encoded or {}).get("scheme") == "dpapi":
        data = _dpapi(data, decrypt=True) or b""
    try:
        return json.loads(data.decode("utf-8")) if data else {}
    except ValueError:
        return {}


def hash_password(password: str, *, salt: bytes | None = None) -> dict:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return {"algo": "pbkdf2_sha256", "salt": salt.hex(), "hash": digest.hex()}


def verify_password(password: str, record: dict) -> bool:
    try:
        salt = bytes.fromhex(str(record.get("salt") or ""))
        expected = str(record.get("hash") or "")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        return hmac.compare_digest(digest.hex(), expected)
    except ValueError:
        return False


# ------------------------------------------------------------------ CRUD
def _safe_gateway_config() -> dict:
    return {"enabled": False, "engine_enabled": False, "auto_reply": False,
            "dry_run": True, "send_mode": "off", "stop": True,
            "allowed_contacts": [], "monitored_contacts": []}


def create_bot(bot_id: str, *, name: str = "", plan: str = "cloud",
               soul_provider: str = "cloud", llm_key: str = "",
               soul_base: str = "", soul_model: str = "", soul_key: str = "",
               password: str = "") -> dict:
    bot_id = str(bot_id or "").strip().lower()
    target = _bot_dir(bot_id)
    if target.exists():
        raise BotError(f"bot 已存在：{bot_id}")
    if soul_provider not in ("local", "cloud"):
        raise BotError("soul_provider 只能是 local/cloud")
    for sub in REQUIRED_DIRS:
        (target / sub).mkdir(parents=True, exist_ok=True)
    _write_json(target / "bot.json", {
        "id": bot_id, "name": str(name or bot_id), "plan": plan,
        "soul_provider": soul_provider, "created_at": time.time(),
        "version": 1,
    })
    _write_json(target / "config" / "wechat.json", _safe_gateway_config())
    _write_json(target / "config" / "app.json", {})
    secrets = {"llm_api_key": str(llm_key or ""),
               "soul_api_base": str(soul_base or ""),
               "soul_model": str(soul_model or ""),
               "soul_api_key": str(soul_key or "")}
    _write_json(target / "secrets" / "keys.json", _encode_secrets(secrets), 0o600)
    if password:
        _write_json(target / "secrets" / "auth.json",
                    hash_password(str(password)), 0o600)
    return {"ok": True, "bot": get_bot(bot_id)}


def get_bot(bot_id: str) -> dict:
    target = _bot_dir(bot_id)
    meta = _read_json(target / "bot.json")
    if not isinstance(meta, dict):
        raise BotError(f"bot 不存在或缺少 bot.json：{bot_id}")
    app = _read_json(target / "config" / "app.json", {}) or {}
    auth = _read_json(target / "secrets" / "auth.json", {}) or {}
    return {
        "id": bot_id, "path": str(target),
        "name": str(meta.get("name") or bot_id),
        "plan": str(meta.get("plan") or "cloud"),
        "soul_provider": str(meta.get("soul_provider") or "cloud"),
        "created_at": meta.get("created_at"),
        "has_password": bool(auth.get("hash")),
        "settings": app,
        "secrets_saved": (target / "secrets" / "keys.json").is_file(),
    }


def list_bots() -> list[dict]:
    root = bots_root()
    if not root.is_dir():
        return []
    items = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "bot.json").is_file():
            try:
                items.append(get_bot(child.name))
            except BotError:
                continue
    return items


def save_bot(bot_id: str, updates: dict) -> dict:
    target = _bot_dir(bot_id)
    if not (target / "bot.json").is_file():
        raise BotError(f"bot 不存在：{bot_id}")
    meta = _read_json(target / "bot.json", {}) or {}
    updates = dict(updates or {})
    for key in ("name", "plan", "soul_provider"):
        if key in updates and str(updates[key]).strip():
            meta[key] = str(updates[key]).strip()
    if meta.get("soul_provider") not in ("local", "cloud"):
        raise BotError("soul_provider 只能是 local/cloud")
    _write_json(target / "bot.json", meta)
    if isinstance(updates.get("settings"), dict):
        _write_json(target / "config" / "app.json", updates["settings"])
    keys = dict(decode_secrets(_read_json(target / "secrets" / "keys.json", {})))
    for field in ("llm_api_key", "soul_api_base", "soul_model", "soul_api_key"):
        if field in updates and updates[field] is not None:
            keys[field] = str(updates[field])
    if any(field in updates for field in ("llm_api_key", "soul_api_base",
                                          "soul_model", "soul_api_key")):
        _write_json(target / "secrets" / "keys.json", _encode_secrets(keys), 0o600)
    if updates.get("password"):
        _write_json(target / "secrets" / "auth.json",
                    hash_password(str(updates["password"])), 0o600)
    return {"ok": True, "bot": get_bot(bot_id)}


def validate_bot(bot_id: str) -> dict:
    target = _bot_dir(bot_id)
    checks = []
    meta = _read_json(target / "bot.json")
    checks.append({"name": "bot.json", "ok": isinstance(meta, dict)})
    for sub in REQUIRED_DIRS:
        checks.append({"name": sub, "ok": (target / sub).is_dir()})
    checks.append({"name": "config/wechat.json",
                   "ok": (target / "config" / "wechat.json").is_file()})
    checks.append({"name": "config/app.json",
                   "ok": (target / "config" / "app.json").is_file()})
    checks.append({"name": "secrets/keys.json",
                   "ok": (target / "secrets" / "keys.json").is_file()})
    ok = all(item["ok"] for item in checks)
    return {"ok": ok, "bot_id": bot_id, "checks": checks}


# ------------------------------------------------------------------ 启动 802
def _python_for_runtime() -> str:
    for candidate in (REPO_ROOT / ".venv" / "Scripts" / "python.exe",
                      REPO_ROOT / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.3):
            return True
    except OSError:
        return False


def launch_bot(bot_id: str, *, port: int = 802, timeout: float = 40.0) -> dict:
    get_bot(bot_id)
    if _port_open(port):
        return {"ok": True, "started": False, "url": f"http://127.0.0.1:{port}/",
                "note": "802 已在运行"}
    env = dict(os.environ)
    env["SOULAGENT_BOT_ROOT"] = str(_bot_dir(bot_id))
    log_dir = REPO_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"console-{port}.log"
    handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            [_python_for_runtime(), "-m", "tools.console.server", "--port", str(port)],
            cwd=str(REPO_ROOT), env=env, stdout=handle, stderr=subprocess.STDOUT)
    finally:
        handle.close()
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        if _port_open(port):
            return {"ok": True, "started": True, "pid": process.pid,
                    "url": f"http://127.0.0.1:{port}/", "log": str(log_path)}
        if process.poll() is not None:
            return {"ok": False, "error": f"802 退出码 {process.returncode}",
                    "log": str(log_path)}
        time.sleep(0.4)
    return {"ok": False, "error": "802 启动超时", "log": str(log_path)}


def load_bot(bot_id: str) -> dict:
    """V1 PR6：加载前分阶段校验（同步返回每阶段结果，供 803 进度条展示）。

    阶段：目录 → bot.json → DB → 密钥 → 配置 → 模型 → 802 端口。
    只读/不启动服务；失败时给出可操作 message。
    """
    target = _bot_dir(bot_id)
    stages: list[dict] = []

    def add(name: str, ok: bool, message: str = "") -> None:
        stages.append({"stage": name, "ok": bool(ok), "message": message})

    add("目录", target.is_dir(), str(target))
    meta = _read_json(target / "bot.json")
    add("bot.json", isinstance(meta, dict), "缺少/损坏" if not isinstance(meta, dict) else "")
    add("数据库目录", (target / "db" / "selflearn").is_dir(), "")
    keys_ok = (target / "secrets" / "keys.json").is_file()
    add("密钥文件", keys_ok, "" if keys_ok else "secrets/keys.json 缺失")
    cfg_ok = (target / "config" / "wechat.json").is_file()
    add("网关配置", cfg_ok, "" if cfg_ok else "config/wechat.json 缺失")
    soul = str((meta or {}).get("soul_provider") or "cloud")
    if soul == "local":
        models = list((target / "models" / "soul").glob("*")) if (target / "models" / "soul").is_dir() else []
        add("本地灵魂模型", bool(models), "" if models else "models/soul/ 为空；请先在 803 下载模型")
    else:
        add("云端灵魂模型", keys_ok, "" if keys_ok else "缺少云端 key")
    port_busy = _port_open(802)
    add("802 端口", True, "802 已在运行，将复用" if port_busy else "802 未运行，可启动")
    ok = all(item["ok"] for item in stages)
    return {"ok": ok, "bot_id": bot_id, "progress": 100 if ok else int(
        100 * sum(1 for item in stages if item["ok"]) / max(1, len(stages))),
        "stages": stages}


def router_config(bot_id: str) -> dict:
    """PR8：把 bot 包配置翻译成既有 ``agi/llm/router.py`` 的配置结构。

    只做纯转换（不写文件）：chat 走云端 API；soul 按 soul_provider 选 local/api；
    vision/extract 默认跟随 soul 的本地能力，否则走 api。
    """
    bot = get_bot(bot_id)
    keys = decode_secrets(_read_json(_bot_dir(bot_id) / "secrets" / "keys.json", {}))
    soul_local = bot["soul_provider"] == "local"
    backends = {
        "api": {"kind": "deepseek", "model": "deepseek-flash", "timeout_s": 30},
        "local": {"kind": "openai",
                  "base_url": str(keys.get("soul_api_base") or "http://127.0.0.1:8123/v1"),
                  "model": str(keys.get("soul_model") or "soul-local"),
                  "timeout_s": 60},
        "rule": {"kind": "rule"},
    }
    soul_backend = "local" if soul_local else "api"
    return {
        "backends": backends,
        "purposes": {
            "chat": {"backend": "api", "fallback": ["local", "rule"]},
            "soul": {"backend": soul_backend, "fallback": ["rule"]},
            "vision": {"backend": soul_backend, "fallback": ["api"]},
            "extract": {"backend": "local" if soul_local else "api", "fallback": ["rule"]},
        },
    }
