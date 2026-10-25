# -*- coding: utf-8 -*-
"""P5-a：网关媒体发送 HTTP 入口 ``POST /api/media/send`` 的最小回归。

覆盖：
- 无 token → 401
- contact 不在白名单 → 400
- items 为空/非列表 → 400
- dry-run 正常路径 → 200 且 items 条目有 type/status（不真发）
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from agi.wechat_gateway import server as S


def _gateway(**over):
    cfg = {
        "allowed_contacts": ["文件传输助手"],
        "monitored_contacts": ["文件传输助手"],
        "send_mode": "auto",
        "dry_run": True,
        "quiet_hours": [0, 24],          # 全天静默：force 必须能绕过
        "stop": False,
        "admin_token": "tok",
        "max_chars": 500,
        "media_enabled": True,
    }
    cfg.update(over)
    return S.Gateway(cfg)


@pytest.fixture()
def base_url():
    gw = _gateway()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.make_handler(gw))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield url
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(url: str, body: dict, token: str | None = "tok"):
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Admin-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def test_media_send_requires_token(base_url):
    code, body = _post(f"{base_url}/api/media/send",
                       {"contact": "文件传输助手", "items": [{"type": "emoji_text", "text": "😄"}]},
                       token=None)
    assert code == 401
    assert body.get("ok") is False


def test_media_send_contact_not_allowed(base_url):
    code, body = _post(f"{base_url}/api/media/send",
                       {"contact": "陌生人", "items": [{"type": "emoji_text", "text": "hi"}]})
    assert code == 400
    assert "白名单" in str(body.get("error") or "")


def test_media_send_empty_items(base_url):
    code, body = _post(f"{base_url}/api/media/send",
                       {"contact": "文件传输助手", "items": []})
    assert code == 400
    assert "items" in str(body.get("error") or "")


def test_media_send_dry_run_ok_and_force_bypasses_quiet(base_url):
    code, body = _post(f"{base_url}/api/media/send", {
        "contact": "文件传输助手",
        "items": [{"type": "emoji_text", "text": "测试 😄"}],
        "force": True,
    })
    assert code == 200, body
    items = body.get("items") or []
    assert items and items[0].get("type") == "emoji_text"
    # dry_run 会返回 planned（不是 blocked/quiet_hours），证明 force 生效
    assert items[0].get("status") == "planned"
