# -*- coding: utf-8 -*-
"""在 wxauto4 venv 里执行一次发送；stdout 只输出一行 JSON。

P0-4：由网关通过环境变量下发发送容错配置（不改 CLI 参数、保持一行 JSON 契约）：
- ``WECHAT_SEND_PROBE_ENABLED``: 1/0，发送前句柄双验 + ``send_probe`` 审计
- ``WECHAT_SEND_RETRY_MAX``:    非确定性失败的额外重试次数（默认 1）
- ``WECHAT_GATEWAY_LOG``:       审计日志路径（send_probe/send_failed/input_clear_failed）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .sender import WxAutoSender


def _env_cfg() -> dict:
    cfg: dict = {}
    raw_probe = os.environ.get("WECHAT_SEND_PROBE_ENABLED")
    if raw_probe is not None:
        cfg["send_probe_enabled"] = str(raw_probe).strip().lower() not in ("0", "false", "no", "off", "")
    raw_retry = os.environ.get("WECHAT_SEND_RETRY_MAX")
    if raw_retry is not None:
        try:
            cfg["send_retry_max"] = int(raw_retry)
        except (TypeError, ValueError):
            pass
    return cfg


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--contact", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    sender = WxAutoSender(cfg=_env_cfg(),
                          log_path=os.environ.get("WECHAT_GATEWAY_LOG") or None)
    result = sender.send(args.contact, args.text, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
