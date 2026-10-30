# -*- coding: utf-8 -*-
"""803 工作台自身配置（``idea/config.json``）：admin token + 本机路径覆盖项。

为什么单独一个文件：
- 工作台自己的 token/路径设置，与它管理的 SoulAgent 配置（``agi/data/wechat/config.json``）
  必须分开，避免把 803 的管理密钥写进主链路配置；
- ``idea/config.json`` 已被 ``.gitignore`` 忽略（见 ``idea/.gitignore``），不会进仓库。

token 策略（B7 安全收口）：
1. ``IDEA_TOKEN`` 环境变量显式设置 → 直接使用（显式设置即视为用户确认，哪怕偏弱）；
2. ``idea/config.json`` 的 ``admin_token`` 非弱口令 → 复用；
3. 否则（文件缺失/空/仍是 ``8888`` 等弱口令）→ 生成 32 位随机 token，
   原子写回 ``idea/config.json``，并由启动日志打印一次带 token 的访问 URL。

路径设置策略（``train_python`` / ``train_model`` / ``gguf_dir`` / ``output_root`` 等）：
``IDEA_*`` 环境变量 > ``idea/config.json`` 对应键 > 调用方给的旧默认值。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path

from . import paths

CONFIG_PATH = paths.IDEA_ROOT / "config.json"

#: 历史默认口令与常见弱口令：不再被自动接受（显式 env 设置除外）
DEFAULT_TOKEN = "8888"
WEAK_TOKENS = frozenset({
    "", "0", "0000", "1111", "111111", "1234", "12345", "123456", "12345678",
    "654321", "8888", "888888", "admin", "administrator", "password", "passwd",
    "root", "test", "token", "qwerty", "abc123", "letmein",
})

_LOCK = threading.Lock()
_CACHE: dict[str, str] = {}


def load() -> dict:
    """读取 ``idea/config.json``；缺失/损坏一律返回空 dict（不抛）。"""
    try:
        if not CONFIG_PATH.is_file():
            return {}
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(patch: dict) -> bool:
    """原子写回（tmp + os.replace）；失败返回 False，不抛。"""
    if not isinstance(patch, dict):
        return False
    data = load()
    data.update(patch)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def is_weak_token(token: object) -> bool:
    return str(token or "").strip().lower() in WEAK_TOKENS


def admin_token() -> str:
    """解析当前 803 管理 token（env → config → 生成随机并写回），带进程内缓存。"""
    with _LOCK:
        env = os.environ.get("IDEA_TOKEN")
        if env is not None and env.strip():
            return env.strip()
        cached = _CACHE.get("token")
        if cached:
            return cached
        token = str(load().get("admin_token") or "").strip()
        if token and not is_weak_token(token):
            _CACHE["token"] = token
            return token
        token = secrets.token_hex(16)  # 32 hex 字符
        if not save({"admin_token": token}):
            print(f"[803] 警告：无法写入 {CONFIG_PATH}，本次随机 token 仅在内存有效"
                  "（重启会变）；请检查目录权限。", flush=True)
        _CACHE["token"] = token
        return token


def reset_cache() -> None:
    """清缓存（测试用：配合 monkeypatch CONFIG_PATH / IDEA_TOKEN）。"""
    with _LOCK:
        _CACHE.clear()


def get_setting(key: str, env: str, default: str) -> str:
    """``IDEA_*`` 环境变量 > ``idea/config.json[key]`` > ``default``。"""
    env_name = str(env or "").strip()
    if env_name:
        value = os.environ.get(env_name)
        if value is not None and str(value).strip():
            return str(value).strip()
    value = load().get(key)
    if value is not None and str(value).strip():
        return str(value).strip()
    return str(default)


def config_path() -> Path:
    return CONFIG_PATH
