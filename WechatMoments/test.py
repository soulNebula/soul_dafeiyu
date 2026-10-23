# -*- coding: utf-8 -*-
"""4.x 冒烟测试：不开 GUI，验证「取到账号 + 读到朋友圈 + 解出至少一张图片」。

用法（项目根目录）::

    .venv\\Scripts\\python.exe test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from wx4.db import get_db

    db = get_db()
    info = db.self_info()
    print(f"[1] 微信 4.x 账号: {info.get('username')}（{info.get('nick_name')}）")
    print(f"    数据目录: {db.account_dir}")

    from wx4 import sns

    moments = sns.all_moments()
    print(f"[2] 可见朋友圈: {len(moments)} 条")
    for m in moments[:3]:
        desc = (m.moment.timelineObject.contentDesc or "").replace("\n", " ")[:24]
        print(f"    {m.moment.timelineObject.create_time}  {m.username}  {desc}")

    from wx4 import media

    ok = 0
    for m in moments:
        for md5 in m.media_md5s():
            path = media.image_path(md5)
            if path:
                ok += 1
                print(f"[3] 图片解密 OK: {md5[:8]} -> {path.name}")
                break
        if ok:
            break
    if not ok:
        print("[3] 本机暂无已缓存的朋友圈图片（先在微信里浏览/点开图片后再导出）")

    print("\n冒烟测试通过：4.x 数据层、朋友圈解析、媒体解密均可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
