# -*- coding: utf-8 -*-
"""Bot 配置包（可移植目录）的创建/校验/读取。

开源上手的核心概念（见 docs/plans/63）：一个 Bot = 用户自选目录里的独立配置包，
包含配置、密钥、数据库、数据、模型清单；802/801 同一时刻只激活一个 Bot。

目录结构（v1）：
    <bot>/
    ├── bot.json          # 非敏感配置 + schema 版本
    ├── secrets.json      # API key（本机、gitignore 语义；权限尽量收紧）
    ├── db/               # 联系人库/记忆/人格（后续由 802 写入）
    ├── data/             # 日记/媒体/导出
    ├── datasets/         # 数据打包（802 自动打包 → 803 后训练）
    ├── models/models.json# 模型清单（路径/来源/校验和；模型文件不复制进来）
    └── logs/
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets as pysecrets
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
BOT_FILE = "bot.json"
SECRETS_FILE = "secrets.json"
PBKDF2_ROUNDS = 200_000

DEFAULT_SETTINGS: dict[str, Any] = {
    "inner_mode": "off",
    "inner_shadow_only": True,
    "inner_schema": "slim",
    "chat_vision_mode": "local_caption",
    "send_mode": "confirm",
    "dry_run": False,
    "proactive_enabled": False,
    "proactive_probability_min": 0.3,
    "proactive_probability_max": 0.9,
    "proactive_interval_min_s": 60,
    "proactive_interval_max_s": 7200,
    "proactive_daily_cap": 12,
    "proactive_max_bubbles": 3,
    "proactive_method": "pacing",          # pacing / schedule / topic
    "proactive_ratio": "1:1",              # 用户:LLM
    "bili_enabled": False,
    "moments_enabled": False,
    "web_enabled": False,
    "bili_ingest": True,
    "web_ingest": True,
    "bili_keyframe_vision": "api",         # api / local
    "image_vision": "api",                 # api / local
    "soul_inject": False,
}

DEFAULT_PROVIDERS: dict[str, Any] = {
    # 任意 OpenAI 兼容服务：base_url 指向 /v1 之前即可（和 DeepSeek 一致）
    "chat": {"mode": "cloud", "base_url": "https://api.deepseek.com", "model": "deepseek-flash"},
    "soul": {"mode": "cloud", "base_url": "https://api.deepseek.com", "model": "deepseek-flash"},
    "vision": {"mode": "cloud", "base_url": "https://api.deepseek.com", "model": "deepseek-flash"},
    "inner_state": {"mode": "cloud", "base_url": "https://api.deepseek.com",
                    "model": "deepseek-flash"},
    "text_extract": {"mode": "cloud", "base_url": "https://api.deepseek.com",
                     "model": "deepseek-flash"},
}


# --------------------------------------------------------------------------- #
# 密码
# --------------------------------------------------------------------------- #
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    """PBKDF2-SHA256，返回 ``pbkdf2$rounds$salt$hash``。"""
    text = str(password or "")
    if len(text) < 6:
        raise ValueError("Bot 密码至少 6 位")
    salt = pysecrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", text.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_b64, hash_b64 = str(stored).split("$", 3)
        if algo != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"),
                                     salt, int(rounds))
        return hmac.compare_digest(digest, expected)
    except Exception:  # noqa: BLE001
        return False


def new_admin_token() -> str:
    return pysecrets.token_urlsafe(24)


# --------------------------------------------------------------------------- #
# 创建 / 读取 / 校验
# --------------------------------------------------------------------------- #
def _mkdir_retry(path: Path) -> None:
    """目录刚创建时 Windows Defender/索引可能短暂锁父目录，重试几次。"""
    last: Exception | None = None
    for _ in range(6):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(0.4)
    assert last is not None
    raise last


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    _mkdir_retry(path.parent)
    data = json.dumps(payload, ensure_ascii=False, indent=2) + chr(10)
    last: Exception | None = None
    for _ in range(6):
        try:
            path.write_text(data, encoding="utf-8")
            break
        except PermissionError as exc:
            last = exc
            time.sleep(0.4)
    else:
        assert last is not None
        raise last
    try:
        # Windows 上真正的 ACL 需要 harden_acl；这里先尽量收紧位
        os.chmod(path, 0o600)
    except OSError:
        pass


def create_bot(path: Path | str, name: str, *, mode: str = "cloud",
               password: str = "", chat: dict[str, Any] | None = None,
               soul: dict[str, Any] | None = None,
               vision: dict[str, Any] | None = None,
               inner_state: dict[str, Any] | None = None,
               settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """创建一个 Bot 包并返回 ``bot.json`` 内容。

    ``mode``：cloud（当前只允许云端；local 留给 Phase 2）。
    ``chat/soul/...``：``{base_url, model, api_key, mode}``；不传用默认值。
    """
    root = Path(path).expanduser().resolve()
    if mode not in ("cloud",):
        raise ValueError("当前版本只允许 cloud 模式（本地模型见 Phase 2）")
    if not str(name or "").strip():
        raise ValueError("Bot 名称不能为空")
    _mkdir_retry(root)
    for sub in ("db", "data", "datasets", "logs", "models"):
        _mkdir_retry(root / sub)

    providers = json.loads(json.dumps(DEFAULT_PROVIDERS))  # deep copy
    secrets_payload: dict[str, Any] = {"version": 1, "providers": {}}
    for key, override in (("chat", chat), ("soul", soul), ("vision", vision),
                          ("inner_state", inner_state)):
        if not override:
            continue
        target = providers[key]
        for field in ("base_url", "model", "mode"):
            if override.get(field):
                target[field] = str(override[field]).strip()
        if override.get("api_key") is not None:
            secrets_payload["providers"][key] = {"api_key": str(override["api_key"] or "")}

    bot = {
        "schema": SCHEMA_VERSION,
        "bot_id": "bot-" + pysecrets.token_hex(6),
        "name": str(name).strip(),
        "mode": mode,
        "created_at": int(time.time()),
        "paths": {"db": "db", "data": "data", "datasets": "datasets",
                  "models": "models/models.json", "logs": "logs"},
        "providers": providers,
        "settings": {**DEFAULT_SETTINGS, **(settings or {})},
        "wechat": {"account": "", "wxid": "", "nickname": "", "contacts": []},
        "auth": {"password_hash": hash_password(password) if password else "",
                 "admin_token": new_admin_token()},
        "notes": "由 tools/bot_profile.py create 生成；密钥在 secrets.json（勿提交/共享）",
    }
    _write_private(root / BOT_FILE, bot)
    _write_private(root / SECRETS_FILE, secrets_payload)
    models = {"schema": 1, "local": {}, "downloaded_at": None,
              "note": "模型文件不复制进 Bot；这里只登记路径/来源/校验和"}
    _write_private(root / "models" / "models.json", models)
    return bot


def load_bot(path: Path | str) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    target = root / BOT_FILE
    if not target.is_file():
        raise FileNotFoundError(f"不是 Bot 包（缺少 {BOT_FILE}）：{root}")
    data = json.loads(target.read_text(encoding="utf-8"))
    data["_root"] = str(root)
    return data


def load_secrets(path: Path | str) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    target = root / SECRETS_FILE
    if not target.is_file():
        return {}
    return json.loads(target.read_text(encoding="utf-8"))


def validate_bot(path: Path | str) -> dict[str, Any]:
    """校验 Bot 包；返回 ``{ok, errors, warnings, root, name, mode}``。"""
    root = Path(path).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    result: dict[str, Any] = {"ok": False, "errors": errors, "warnings": warnings,
                              "root": str(root)}
    bot_file = root / BOT_FILE
    if not root.is_dir():
        errors.append("目录不存在")
        return result
    if not bot_file.is_file():
        errors.append(f"缺少 {BOT_FILE}")
        return result
    try:
        bot = json.loads(bot_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{BOT_FILE} 不是合法 JSON：{type(exc).__name__}: {exc}")
        return result
    result.update({"name": bot.get("name"), "mode": bot.get("mode"),
                   "schema": bot.get("schema")})
    if bot.get("schema") != SCHEMA_VERSION:
        errors.append(f"schema 版本不支持：{bot.get('schema')}（期望 {SCHEMA_VERSION}）")
    if bot.get("mode") not in ("cloud",):
        errors.append("当前只支持 cloud 模式")
    for sub in ("db", "data", "datasets", "logs", "models"):
        if not (root / sub).is_dir():
            errors.append(f"缺少目录 {sub}/")
    if not (root / SECRETS_FILE).is_file():
        warnings.append("缺少 secrets.json（key 需要重新配置）")
    else:
        secrets_payload = load_secrets(root)
        providers = secrets_payload.get("providers") or {}
        for key in ("chat", "soul", "vision", "inner_state"):
            text = str(((providers.get(key) or {}).get("api_key")) or "")
            if bot.get("mode") == "cloud" and key in ("chat", "soul") and not text:
                warnings.append(f"{key} 还没有配置 api_key")
            if text and not (text.startswith("sk-") or len(text) >= 16):
                warnings.append(f"{key}.api_key 看起来不像有效 key（长度/前缀异常）")
    models_path = root / "models" / "models.json"
    if not models_path.is_file():
        warnings.append("缺少 models/models.json（本地模型未登记）")
    result["ok"] = not errors
    return result


def change_password(path: Path | str, old_password: str, new_password: str) -> dict[str, Any]:
    """校验旧密码后修改 Bot 登录密码（只改 auth.password_hash）。"""
    root = Path(path).expanduser().resolve()
    bot = load_bot(root)
    auth = bot.get("auth") or {}
    stored = str(auth.get("password_hash") or "")
    if stored and not verify_password(str(old_password or ""), stored):
        return {"ok": False, "error": "bad_password"}
    if not stored:
        return {"ok": False, "error": "password_not_set"}
    if len(str(new_password or "")) < 6:
        return {"ok": False, "error": "password_too_short"}
    auth["password_hash"] = hash_password(str(new_password))
    bot["auth"] = auth
    bot.pop("_root", None)
    _write_private(root / BOT_FILE, bot)
    return {"ok": True, "path": str(root)}


def describe_bot(path: Path | str) -> dict[str, Any]:
    bot = load_bot(path)
    secrets_payload = load_secrets(path)
    providers = secrets_payload.get("providers") or {}
    safe = {
        "root": bot.get("_root"),
        "bot_id": bot.get("bot_id"),
        "name": bot.get("name"),
        "mode": bot.get("mode"),
        "created_at": bot.get("created_at"),
        "providers": {},
        "settings": bot.get("settings"),
        "wechat": bot.get("wechat"),
    }
    for key, cfg in (bot.get("providers") or {}).items():
        if not isinstance(cfg, dict):
            continue
        item = dict(cfg)
        item["configured"] = bool(((providers.get(key) or {}).get("api_key")) or "")
        safe["providers"][key] = item
    return safe


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bot 配置包管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser("create", help="创建 Bot 包")
    create.add_argument("--path", required=True, help="Bot 目录（用户自选）")
    create.add_argument("--name", required=True, help="Bot 名称")
    create.add_argument("--mode", default="cloud", choices=("cloud",))
    create.add_argument("--password", default="", help="802 登录密码（≥6 位；不填=不设密码）")
    create.add_argument("--base-url", default="https://api.deepseek.com")
    create.add_argument("--model", default="deepseek-flash")
    create.add_argument("--api-key", default="", help="聊天/灵魂默认共用；可用 --soul-key 分开")
    create.add_argument("--soul-base-url", default="")
    create.add_argument("--soul-model", default="")
    create.add_argument("--soul-key", default="")
    create.add_argument("--vision-base-url", default="")
    create.add_argument("--vision-model", default="")
    create.add_argument("--vision-key", default="")

    validate = sub.add_parser("validate", help="校验 Bot 包")
    validate.add_argument("--path", required=True)
    show = sub.add_parser("show", help="显示 Bot 摘要（key 打码）")
    show.add_argument("--path", required=True)

    args = parser.parse_args(argv)
    if args.cmd == "create":
        chat = {"base_url": args.base_url, "model": args.model, "api_key": args.api_key}
        soul = {"base_url": args.soul_base_url or args.base_url,
                "model": args.soul_model or args.model,
                "api_key": args.soul_key if args.soul_key else args.api_key}
        vision = {}
        if args.vision_model:
            vision = {"base_url": args.vision_base_url or args.base_url,
                      "model": args.vision_model, "api_key": args.vision_key or args.api_key}
        try:
            bot = create_bot(args.path, args.name, mode=args.mode, password=args.password,
                             chat=chat, soul=soul, vision=vision or None)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "root": str(Path(args.path).expanduser().resolve()),
                          "bot_id": bot["bot_id"], "name": bot["name"]}, ensure_ascii=False))
        return 0
    if args.cmd == "validate":
        result = validate_bot(args.path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    if args.cmd == "show":
        print(json.dumps(describe_bot(args.path), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
