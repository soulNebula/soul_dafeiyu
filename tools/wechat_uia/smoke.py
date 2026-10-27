#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P0-send 只读冒烟：打印 .NET UIA 的 find_contact / find_input 结果（不发送）。

用法（在仓库根目录）::

    python tools/wechat_uia/smoke.py
    python tools/wechat_uia/smoke.py --contact 文件传输助手
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agi.wechat_gateway import uia_sender  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读 UIA 冒烟（不发消息）")
    parser.add_argument("--contact", default="文件传输助手", help="要查找的联系人")
    args = parser.parse_args(argv)
    print(f"script: {uia_sender.SCRIPT}")
    print(f"available: {uia_sender.available()}")
    contact = uia_sender.find_contact(args.contact)
    print("find_contact(" + args.contact + "):")
    print(json.dumps(contact, ensure_ascii=False, indent=2))
    print("find_input():")
    print(json.dumps(uia_sender.find_input(), ensure_ascii=False, indent=2))
    print("（只读查询，没有发送任何消息）")
    return 0 if contact.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
