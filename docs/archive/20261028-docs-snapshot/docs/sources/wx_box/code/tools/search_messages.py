#!/usr/bin/env python3
"""全库消息检索（移植自 _refs/wechat-4x-decrypt 的 Skill）。

用法（在项目根目录运行）：
    python tools/search_messages.py --keyword 会议              # 关键词检索
    python tools/search_messages.py --keyword 文件 --since 2026-10-01
    python tools/search_messages.py --contact 刘工 --limit 50
    python tools/search_messages.py --keyword 合同 --out log/合同检索.txt --json log/合同检索.json

说明：
* 数据源是微信的全文索引库 message_fts.db（只读解密到临时目录后查询）；
* 只支持本机、当前登录账号；不写微信任何文件。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:  # Windows 控制台默认 GBK，统一成 UTF-8 避免中文乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wx_core import config as cfg_mod  # noqa: E402

sys.path.insert(0, str(ROOT / "Demo" / "tools_impl"))
import message_search  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="检索微信本地历史消息")
    ap.add_argument("--keyword", default="", help="关键词，多个用逗号分隔")
    ap.add_argument("--since", default="", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--until", default="", help="结束日期 YYYY-MM-DD")
    ap.add_argument("--contact", default="", help="只看某个联系人/群（显示名或 wxid）")
    ap.add_argument("--limit", type=int, default=100, help="最多返回条数（默认 100）")
    ap.add_argument("--out", default="", help="导出 txt 路径（可选）")
    ap.add_argument("--json", default="", help="导出 json 路径（可选）")
    args = ap.parse_args(argv)

    config = cfg_mod.load()

    def log(text, level="info"):
        print(f"[{level}] {text}")

    try:
        rows = message_search.search_messages(
            config, keyword=args.keyword, since=args.since, until=args.until,
            contact=args.contact, limit=args.limit, log=log)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 检索失败: {exc}", file=sys.stderr)
        return 1

    for r in rows[:50]:
        who = r["session"] + (f"（{r['sender']}）" if r["sender"] and r["sender"] != r["session"] else "")
        print(f"{r['time']}  {who}: {r['text'][:120]}")
    if len(rows) > 50:
        print(f"... 其余 {len(rows) - 50} 条未打印（可加 --out 导出）")

    if args.out:
        p = message_search.export_txt(rows, Path(args.out))
        print(f"[info] 已导出 {len(rows)} 条 -> {p}")
    if args.json:
        p = message_search.export_json(rows, Path(args.json))
        print(f"[info] 已导出 JSON -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
