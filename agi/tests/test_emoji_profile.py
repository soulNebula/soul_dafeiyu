# -*- coding: utf-8 -*-
"""S5 表情包人格档案测试。"""
from __future__ import annotations

import json
import time

from agi.selflearn import db, emoji_profile


def test_emoji_profile_counts(tmp_path):
    path = tmp_path / "e.sqlite3"
    con = db.connect(path)
    now = time.time()
    for i, (name, tags, n) in enumerate([("开心猫", ["happy"], 3), ("无语狗", ["speechless"], 1)]):
        eid = f"E-{i}"
        con.execute("INSERT INTO emoji_archive(id,name,path,tags_json,usage_count,created_at) VALUES(?,?,?,?,?,?)",
                    (eid, name, f"/{name}.png", json.dumps(tags), n, now))
        for j in range(n):
            con.execute("INSERT INTO emoji_usage(id,emoji_id,message_id,context,created_at) VALUES(?,?,?,?,?)",
                        (f"U-{i}-{j}", eid, "", "", now + j))
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M-1", "user", "hi", now, 0))
    con.commit(); con.close()
    p = emoji_profile.build_profile(path)
    assert p["total_used"] == 4
    assert p["emotion_dist"]["happy"] > p["emotion_dist"]["speechless"]
    assert p["top_emoji"][0]["name"] == "开心猫"
    assert p["text_emoji_ratio"] > 0
