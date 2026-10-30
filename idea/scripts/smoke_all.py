# -*- coding: utf-8 -*-
"""IDEA 全服务冒烟（本机 HTTP GET，纯 httpx，不导入业务代码）。

覆盖：
- 803 工作台：health/overview/config/services/db/train/models
- 801 网关：health/status（X-Admin-Token）
- 802 控制台：api/health
- 8123 llama-server /v1/models
- 8125 local3b /v1/models

用法：
    python scripts/smoke_all.py
    python scripts/smoke_all.py --token <idea/config.json 里的 admin_token>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from dataclasses import dataclass

import httpx


def _workbench_token() -> str:
    """工作台 token：``IDEA_TOKEN`` → ``idea/config.json`` 的 ``admin_token``（B7 起无弱默认）。"""
    env = os.environ.get("IDEA_TOKEN")
    if env is not None and str(env).strip():
        return str(env).strip()
    cfg = Path(__file__).resolve().parents[1] / "config.json"
    try:
        raw = json.loads(cfg.read_text(encoding="utf-8"))
        return str(raw.get("admin_token") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


DEFAULT_TOKEN = _workbench_token()


def _gateway_token() -> str:
    """网关 token 从 SoulAgent 生产配置读（F3 轮换后不再是 8888）。"""
    env = os.environ.get("GATEWAY_TOKEN")
    if env:
        return env
    try:
        home = Path(os.environ.get("SOULAGENT_HOME",
                                   r"C:\Users\Administrator\Desktop\SoulAgent"))
        cfg = json.loads((home / "agi" / "data" / "wechat" / "config.json")
                         .read_text(encoding="utf-8"))
        return str(cfg.get("admin_token") or DEFAULT_TOKEN)
    except Exception:  # noqa: BLE001
        return DEFAULT_TOKEN


GATEWAY_TOKEN = _gateway_token()


@dataclass
class Check:
    service: str
    method: str
    url: str
    token: bool = False
    require_ok: bool = True
    gateway: bool = False


def _checks() -> list[Check]:
    wb = "http://127.0.0.1:803"
    gw = "http://127.0.0.1:801"
    console = "http://127.0.0.1:802"
    paths = [
        ("health", "/health", False),
        ("overview", "/api/wb/overview", True),
        ("config", "/api/wb/config", True),
        ("services", "/api/wb/services", True),
        ("db/databases", "/api/wb/db/databases", True),
        ("train/datasets", "/api/wb/train/datasets", True),
        ("models/running", "/api/wb/models/running", True),
        ("models/disk", "/api/wb/models/disk", True),
    ]
    out = [Check("workbench", "GET", wb + path, token) for _name, path, token in paths]
    out.extend([
        Check("gateway", "GET", gw + "/health", False),
        Check("gateway", "GET", gw + "/status", True, require_ok=False,
              gateway=True),
        Check("console", "GET", console + "/api/health", True),
        Check("llama8123", "GET", "http://127.0.0.1:8123/v1/models", False),
        Check("local3b8125", "GET", "http://127.0.0.1:8125/v1/models", False),
    ])
    return out


def _label(check: Check) -> str:
    path = check.url.split("127.0.0.1", 1)[-1]
    return f"{check.service} {path}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IDEA 全服务冒烟")
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--gateway-token", default=GATEWAY_TOKEN)
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args(argv)
    token = str(args.token or "").strip()
    if not token:
        print("缺少工作台 token：先启动 803（随机 token 会写入 idea/config.json），"
              "或用 IDEA_TOKEN / --token 显式指定。", file=sys.stderr)
        return 2
    gw_token = str(args.gateway_token or GATEWAY_TOKEN)
    headers = {"X-Admin-Token": token, "Accept": "application/json"}
    gw_headers = {"X-Admin-Token": gw_token, "Accept": "application/json"}
    rows: list[tuple[str, bool, str]] = []
    client = httpx.Client(timeout=args.timeout, trust_env=False)
    try:
        for check in _checks():
            hdrs = gw_headers if check.gateway else headers
            started = time.perf_counter()
            try:
                response = client.get(check.url, headers=hdrs if check.token else None)
                elapsed = (time.perf_counter() - started) * 1000
                payload = None
                try:
                    payload = response.json()
                except Exception:  # noqa: BLE001
                    payload = None
                ok = response.status_code == 200
                if ok and check.require_ok:
                    ok = isinstance(payload, dict) and bool(payload.get("ok", True))
                # /v1/models 是 OpenAI 格式：data/list 均可；200 即通过
                detail = ""
                if response.status_code != 200:
                    detail = f"HTTP {response.status_code}"
                elif check.require_ok and isinstance(payload, dict) and payload.get("ok") is False:
                    detail = str(payload.get("error") or "ok=false")
                elif payload is None:
                    detail = "非 JSON"
                rows.append((_label(check), ok, f"{detail or 'OK'}  {elapsed:.0f}ms"))
            except Exception as exc:  # noqa: BLE001
                elapsed = (time.perf_counter() - started) * 1000
                rows.append((_label(check), False,
                             f"{type(exc).__name__}: {str(exc)[:100]}  {elapsed:.0f}ms"))
    finally:
        client.close()

    width = max(len(label) for label, _ok, _detail in rows) + 2
    print("=" * (width + 46))
    print(f"{'RESULT':<6} {'CHECK':<{width}} DETAIL")
    print("-" * (width + 46))
    for label, ok, detail in rows:
        print(f"{'PASS' if ok else 'FAIL':<6} {label:<{width}} {detail}")
    failed = [row for row in rows if not row[1]]
    print("-" * (width + 46))
    print(f"结果：{'PASS' if not failed else 'FAIL'}  "
          f"通过 {len(rows) - len(failed)}/{len(rows)}，失败 {len(failed)}")
    print("=" * (width + 46))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
