# -*- coding: utf-8 -*-
"""B站扫码登录（V1 PR9）：生成二维码 / 轮询结果 / 登录状态。

接口与 802 B站页现有的「扫码登录」一致（passport web qrcode），但凭证落仓库/bot，
不再依赖 ``~/.dsh/bili-duty``。状态码：0=成功 86101=未扫码 86090=已扫码未确认
86038=二维码过期。
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from .common import BiliError, http_json, load_credential, save_credential

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

#: B站扫码状态码 → 状态名
POLL_STATES = {0: "ok", 86101: "waiting", 86090: "scanned", 86038: "expired"}
COOKIE_KEYS = ("sessdata", "bili_jct", "dedeuserid", "buvid3")


def qr_generate(*, timeout: float = 8.0) -> dict:
    """生成登录二维码：``{ok, url, key, expires_in_s}``（url 交给 802 渲染 PNG）。"""
    payload = http_json(GENERATE_URL, timeout=timeout)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data.get("qrcode_key") or not data.get("url"):
        raise BiliError("qr_generate_failed")
    return {"ok": True, "url": str(data["url"]),
            "key": str(data["qrcode_key"]), "expires_in_s": 180}


def _cookies_from_url(url: str) -> dict:
    query = parse_qs(urlsplit(str(url or "")).query)
    lowered = {str(key).lower(): values for key, values in query.items()}
    cookies = {}
    for name in COOKIE_KEYS:
        values = lowered.get(name) or query.get(name) or []
        if values:
            cookies[name] = str(values[0])
    return cookies


def qr_poll(key: str, *, timeout: float = 8.0) -> dict:
    """轮询扫码结果；成功时**不**落盘，返回 ``credential`` 由调用方决定保存。"""
    if not str(key or "").strip():
        raise BiliError("qrcode_key_missing")
    payload = http_json(f"{POLL_URL}?qrcode_key={key}", timeout=timeout)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise BiliError("qr_poll_failed")
    code = int(data.get("code") or 0)
    state = POLL_STATES.get(code, "unknown")
    result = {"ok": state == "ok", "state": state, "code": code,
              "message": str(data.get("message") or "")}
    if state == "ok":
        cookies = _cookies_from_url(str(data.get("url") or ""))
        if not cookies.get("sessdata"):
            raise BiliError("qr_poll_missing_sessdata")
        result["credential"] = cookies
    return result


def save_login(credential: dict) -> dict:
    """把扫码得到的凭证加密落盘（bot 包优先）。"""
    path = save_credential(credential)
    return {"ok": True, "credential_path": str(path),
            "keys": sorted(credential)}


def login_status(credential: dict | None = None, *, timeout: float = 8.0) -> dict:
    """当前登录状态（nav 接口）；凭证缺失时 ``logged_in=False`` 且说明原因。"""
    cred = credential if credential is not None else load_credential()
    if not cred or not cred.get("sessdata"):
        return {"ok": True, "logged_in": False, "reason": "no_credential"}
    from .common import cookie_header
    try:
        payload = http_json(NAV_URL, headers={"Cookie": cookie_header(cred)},
                            timeout=timeout)
    except BiliError as exc:
        return {"ok": False, "logged_in": False, "error": str(exc)}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data.get("isLogin"):
        return {"ok": True, "logged_in": False, "reason": "credential_expired"}
    return {"ok": True, "logged_in": True,
            "uname": str(data.get("uname") or ""), "mid": data.get("mid"),
            "face": str(data.get("face") or "")}