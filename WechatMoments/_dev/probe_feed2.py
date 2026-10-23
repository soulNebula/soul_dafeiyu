# -*- coding: utf-8 -*-
"""开发期探针 4：SnsTimeLine 结构统计（每条含几个 TimelineObject、评论在哪）。"""
from __future__ import annotations

import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Desktop\wx_box\WechatMoments\_dev")

from probe_db import ref  # noqa: E402

db = ref.WeChatDB()
rel = next(r for r, _, _ in db._db_files if r.lower().startswith("sns"))
conn = db._open(rel)
cur = conn.cursor()
cur.execute("SELECT tid, user_name, content FROM SnsTimeLine ORDER BY tid DESC")
for tid, user, content in cur.fetchall():
    print(f"tid={tid} user={user} len={len(content)} TimelineObject={content.count('<TimelineObject>')} "
          f"CommentList={content.count('<CommentList>')} comment_tags={content.count('<Comment')} likes={content.count('<Like')}")
print("\n== 单条 XML 里的自有关键标签 ==")
c = cur.execute("SELECT content FROM SnsTimeLine ORDER BY tid DESC LIMIT 1").fetchone()[0]
print(sorted(set(re.findall(r"<([A-Za-z0-9_]+)[ >/]", c))))
print("\n== 尾部 800 字符 ==")
print(c[-800:])
print("\n== 全库表里包含 feed/comment 的行 ==")
for t in ("SnsMessage_tmp3",):
    print(t, cur.execute(f"SELECT * FROM {t} LIMIT 3").fetchall())
