# -*- coding: utf-8 -*-
"""在 wxauto4 venv 里执行一次发送；stdout 只输出一行 JSON。

P0-4：由网关通过环境变量下发发送容错配置（不改 CLI 参数、保持一行 JSON 契约）：
- ``WECHAT_SEND_PROBE_ENABLED``: 1/0，发送前句柄双验 + ``send_probe`` 审计
- ``WECHAT_SEND_RETRY_MAX``:    非确定性失败的额外重试次数（默认 1）
- ``WECHAT_GATEWAY_LOG``:       审计日志路径（send_probe/send_failed/input_clear_failed）

P5-a：新增 ``--items``（JSON 数组）发多媒体多气泡（text/emoji_text/image/gif/sticker），
媒体配置同样通过环境变量下发：
- ``WECHAT_MEDIA_ENABLED``（1/0）、``WECHAT_MEDIA_DIRS``（JSON 数组）、
  ``WECHAT_MEDIA_MAX_MB``、``WECHAT_MEDIA_INTERVAL_MS``、``WECHAT_STICKER_FALLBACK_EMOJI``
stdout 契约：items 模式输出 ``{"ok": bool, "items": [每条结果]}`` 一行。
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys

from .sender import WxAutoSender


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _env_cfg() -> dict:
    cfg: dict = {}
    probe = _env_bool("WECHAT_SEND_PROBE_ENABLED")
    if probe is not None:
        cfg["send_probe_enabled"] = probe
    raw_retry = os.environ.get("WECHAT_SEND_RETRY_MAX")
    if raw_retry is not None:
        try:
            cfg["send_retry_max"] = int(raw_retry)
        except (TypeError, ValueError):
            pass
    # P5-a 媒体配置
    media_enabled = _env_bool("WECHAT_MEDIA_ENABLED")
    if media_enabled is not None:
        cfg["media_enabled"] = media_enabled
    raw_dirs = os.environ.get("WECHAT_MEDIA_DIRS")
    if raw_dirs:
        try:
            dirs = json.loads(raw_dirs)
        except ValueError:
            dirs = None
        if isinstance(dirs, list):
            cfg["media_dirs"] = [str(x) for x in dirs]
    max_mb = _env_float("WECHAT_MEDIA_MAX_MB")
    if max_mb is not None:
        cfg["media_max_mb"] = max_mb
    interval_ms = _env_float("WECHAT_MEDIA_INTERVAL_MS")
    if interval_ms is not None:
        cfg["media_send_interval_ms"] = interval_ms
    fallback_emoji = os.environ.get("WECHAT_STICKER_FALLBACK_EMOJI")
    if fallback_emoji:
        cfg["sticker_fallback_emoji"] = fallback_emoji
    media_audit = _env_bool("WECHAT_MEDIA_AUDIT")
    if media_audit is not None:
        cfg["media_audit"] = media_audit
    return cfg


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--contact", required=True)
    p.add_argument("--text")
    p.add_argument("--items", help="P5-a：JSON 数组，多气泡媒体 items")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    module_name = (os.environ.get("WECHAT_SEND_MODULE") or "").strip()
    if not module_name:
        # 自动探测：优先 wechatauto（当前可用），退回 wxauto4（历史方案）
        for cand in ("wechatauto", "wxauto4"):
            try:
                importlib.import_module(cand)
                module_name = cand
                break
            except Exception:  # noqa: BLE001
                continue
        module_name = module_name or "wxauto4"
    sender = WxAutoSender(cfg=_env_cfg(),
                          module_name=module_name,
                          log_path=os.environ.get("WECHAT_GATEWAY_LOG") or None)
    if args.items is not None:
        try:
            items = json.loads(args.items)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": f"bad_items_json: {exc}"},
                             ensure_ascii=False))
            return 2
        if not isinstance(items, list):
            print(json.dumps({"ok": False, "error": "items 必须是 JSON 数组"},
                             ensure_ascii=False))
            return 2
        results = sender.send_bubbles(args.contact, items, dry_run=args.dry_run)
        output = {"ok": bool(results) and all(r.get("ok") for r in results),
                  "items": results}
        print(json.dumps(output, ensure_ascii=False, default=str))
        return 0 if output["ok"] else 1

    if not args.text:
        print(json.dumps({"ok": False, "error": "需要 --text 或 --items"},
                         ensure_ascii=False))
        return 2
    result = sender.send(args.contact, args.text, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
