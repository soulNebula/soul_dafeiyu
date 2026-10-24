# -*- coding: utf-8 -*-
"""801 发送控制面：鉴权、confirm/draft、投诉熔断、按联系人策略。

安全默认（2026-10-24 M0/M1）：
* ``admin_token`` 为空时 **拒绝一切管理请求**（不再"空即放行"）；服务启动时会自动
  生成一个 token 并写回 ``config.json``，只打印一次。
* 未配置 ``send_mode`` 时默认 ``confirm``（先存草稿、人工批准才发）；
  某个联系人可以在 ``contact_modes`` 里单独升到 ``auto``。
"""
from __future__ import annotations

import hmac
import re
import secrets
from typing import Any

COMPLAINT = re.compile(r"(投诉|举报|骗人|骗子|机器人|不是真人|骚扰|再发就拉黑)")

SEND_MODES = ("auto", "confirm", "off")
DEFAULT_SEND_MODE = "confirm"

#: 回环 Host 白名单（防 DNS rebinding）
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")


def is_complaint(text: str) -> bool:
    return bool(COMPLAINT.search(str(text or "")))


def ensure_admin_token(cfg: dict[str, Any]) -> tuple[str, bool]:
    """确保有管理 token；返回 (token, 是否新生成)。调用方负责写回配置。"""
    token = str(cfg.get("admin_token") or "").strip()
    if token:
        return token, False
    token = secrets.token_hex(16)
    cfg["admin_token"] = token
    return token, True


def auth_ok(headers: dict[str, str], cfg: dict[str, Any],
            query_token: str = "") -> bool:
    """恒定时间比较；未配置 token 时 fail-closed。"""
    token = str(cfg.get("admin_token") or "").strip()
    if not token:
        return False
    got = str((headers or {}).get("X-Admin-Token") or "").strip()
    if not got:
        got = str(query_token or "").strip()
    if not got:
        return False
    return hmac.compare_digest(got, token)


def host_ok(host_header: str, port: int) -> bool:
    """Host 头必须是回环地址 + 实际端口（防 DNS rebinding）。"""
    host = str(host_header or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):  # [::1]:801
        name, _, p = host.partition("]:")
        name = name.strip("[")
    elif host.count(":") == 1:  # 127.0.0.1:801
        name, _, p = host.partition(":")
    else:  # 裸主机名/异常形态
        name, p = host, ""
    if name not in LOOPBACK_HOSTS and name != "::1":
        return False
    return p.isdigit() and int(p) == int(port)


def send_mode(cfg: dict[str, Any], contact: str | None = None) -> str:
    """生效的发送模式：按联系人覆盖 > 全局 > 默认 confirm。"""
    modes = cfg.get("contact_modes") or {}
    if contact and isinstance(modes, dict):
        value = str(modes.get(contact) or "").strip().lower()
        if value in SEND_MODES:
            return value
    mode = str(cfg.get("send_mode") or DEFAULT_SEND_MODE).strip().lower()
    return mode if mode in SEND_MODES else DEFAULT_SEND_MODE
