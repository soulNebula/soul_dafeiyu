#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读朋友圈抓取：用 wechatauto MomentDB 读本机 sns.db，输出 JSON 数组到 stdout。

由工作台用 ``E:/agent/venv_wx312/Scripts/python.exe`` 调用；本脚本绝不点赞/
评论/发圈，也不修改任何微信数据。单条解析失败会跳过，不拖累整批。
"""
from __future__ import annotations

import argparse
import json
import sys


def _clean_image(item) -> dict:
    if not isinstance(item, dict):
        return {}
    return {
        "md5": str(item.get("md5") or ""),
        "url": str(item.get("url") or ""),
        "size": item.get("size"),
        "width": item.get("width"),
        "height": item.get("height"),
        "local_path": item.get("local_path") or "",
    }


def _clean_row(row: dict) -> dict:
    """只保留前端需要的字段；任何单条异常由调用方跳过。"""
    return {
        "id": str(row.get("id") or ""),
        "tid": str(row.get("tid") or ""),
        "username": str(row.get("username") or ""),
        "nickname": str(row.get("nickname") or ""),
        "text": str(row.get("text") or ""),
        "create_time": int(row.get("create_time") or 0),
        "images": [_clean_image(x) for x in (row.get("images") or []) if _clean_image(x)],
        "videos": row.get("videos") or [],
        "likes": row.get("likes") or [],
        "comments": row.get("comments") or [],
        "location": row.get("location") or {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读导出朋友圈 JSON")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--username", default="")
    parser.add_argument("--since", type=int, default=0)
    parser.add_argument("--until", type=int, default=0)
    parser.add_argument("--keyword", default="")
    args = parser.parse_args(argv)

    try:
        import wechatauto
        db = wechatauto.WeChatDB()
        mdb = wechatauto.MomentDB(db)
        rows = mdb.get_moments(
            limit=max(0, int(args.limit)), offset=max(0, int(args.offset)),
            username=args.username or None,
            since=args.since or None,
            until=args.until or None,
            keyword=args.keyword or None,
        )
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False), file=sys.stderr)
        return 1

    out = []
    for row in rows or []:
        try:
            out.append(_clean_row(row))
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖累整批
            print(f"[moments_fetch] skip row: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
