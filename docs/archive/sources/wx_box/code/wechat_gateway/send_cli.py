# -*- coding: utf-8 -*-
"""在 wxauto4 venv 里执行一次发送；stdout 只输出一行 JSON。"""
from __future__ import annotations

import argparse
import json
import sys

from .sender import WxAutoSender


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--contact", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    result = WxAutoSender().send(args.contact, args.text, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
