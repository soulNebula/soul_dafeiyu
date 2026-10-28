#!/usr/bin/env python3
"""导出微信本地表情包（移植自 _refs/Wechat-Emoticon-Parser）。

用法（在项目根目录运行）：
    python tools/export_emoticons.py                 # 导出到 media/emoticons_export
    python tools/export_emoticons.py --out D:/emo    # 指定输出目录

输出结构：
    <out>/store/<包名>/<标题>.gif|png|...   # 商店表情包（标题命名）
    <out>/favorite/<标题>.gif|...           # 收藏/自定义表情
    <out>/unknown/<md5>.gif|...             # 解不开包名的
    <out>/manifest.json                     # 完整清单
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
import emoticon_export  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="导出微信本地表情包")
    ap.add_argument("--out", default="", help="输出目录（默认 media/emoticons_export）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个源文件（调试用，0=全部）")
    args = ap.parse_args(argv)
    config = cfg_mod.load()
    out = Path(args.out) if args.out else None

    def log(text, level="info"):
        print(f"[{level}] {text}")

    try:
        manifest = emoticon_export.export_emoticons(
            config, out, log=log, limit=(args.limit or None))
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 导出失败: {exc}", file=sys.stderr)
        return 1
    print("\n包清单:")
    for name, pack in list(manifest["packs"].items())[:10]:
        print(f"  {name}: {len(pack['stickers'])} 个")
    if len(manifest["packs"]) > 10:
        print(f"  ... 共 {len(manifest['packs'])} 个包")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
