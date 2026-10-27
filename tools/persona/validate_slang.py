# -*- coding: utf-8 -*-
"""P27：真实 DeepSeek 20 条「去网感腔调」验证。"""
from __future__ import annotations

import argparse
import json
import random
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SLANG_RE = re.compile(
    r"赢了|输了|这波|拿捏|绝了|笑死|栓Q|666|开摆|绷不住|"
    r"血压(?:飙升|升高)?|算是.{0,6}(?:赢|输)|只能算")

CASES = [
    ("吃饭", "我中午吃了寿司，天天拿这个馋你"),
    ("吃饭", "今天这顿饭还行，能下饭"),
    ("吃饭", "晚上随便煮了点面"),
    ("炫耀", "我今天把活儿全干完了，还早下班了"),
    ("炫耀", "我这周瘦了两斤"),
    ("吐槽", "老板又临时改需求，烦死"),
    ("吐槽", "地铁上遇到一个特别离谱的人"),
    ("吐槽", "今天运气差到爆"),
    ("被夸", "你今天怎么这么会说话"),
    ("被夸", "还是你懂我"),
    ("被夸", "你最近状态不错啊"),
    ("互损", "你是不是有点笨"),
    ("互损", "你这想法挺离谱的"),
    ("互损", "别装了"),
    ("日常", "今天有点累"),
    ("日常", "刚看完一个讲深海的片子"),
    ("日常", "晚上好"),
    ("日常", "你在干嘛"),
    ("日常", "看到一个好笑的段子"),
    ("日常", "准备睡了"),
]


def _run(prompts_path=None):
    if prompts_path:
        import os
        os.environ["PROMPTS_PATH"] = str(prompts_path)
    from agi.selflearn import llm
    from agi.selflearn.chat import SelfLearnAgent
    db_path = Path(tempfile.mkdtemp()) / "slang_validate.sqlite3"
    agent = SelfLearnAgent(db_path, client=llm.Client(), rng=random.Random(88))
    out = []
    for category, text in CASES:
        row = agent.send(text)
        reply = row.get("reply") or ""
        hits = list(dict.fromkeys(SLANG_RE.findall(reply)))
        out.append({"category": category, "user": text, "reply": reply,
                    "slang_hits": hits,
                    "guard": (row.get("guard") or {}).get("slang_regenerated")})
    return {"count": len(out), "slang_hits": sum(len(x["slang_hits"]) for x in out),
            "replies": out}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tools.persona.validate_slang")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    result = _run()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(json.dumps({"count": result["count"], "slang_hits": result["slang_hits"]},
                         ensure_ascii=False))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
