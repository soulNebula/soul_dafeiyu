# -*- coding: utf-8 -*-
"""P28：真实 DeepSeek 30 条「去金句腔/不主动收尾」验证。"""
from __future__ import annotations

import argparse
import json
import random
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANNED_RE = re.compile(
    r"不高级.{0,8}耐看|耐看|挺稳|蛮稳|不会出错|默契|收下了|接住了|记下了|存着|"
    r"踏实了|心里舒坦|花在刀刃上|这波不亏|值了|算下来")
CLOSING_RE = re.compile(r"那先这样|忙你的去|你先忙|回聊|改天聊|不打扰了")
ALLOW_RE = re.compile(r"忙|睡|晚安|再见|bye|出门|上班|下班|走")

CASES = [
    ("吃饭", "我中午吃了寿司，天天拿这个馋你"),
    ("吃饭", "今天这顿饭还行，能下饭"),
    ("吃饭", "晚上随便煮了点面"),
    ("吃饭", "这番茄味挺特别的"),
    ("炫耀", "我今天把活儿全干完了，还早下班了"),
    ("炫耀", "我这周瘦了两斤"),
    ("炫耀", "刚拿到一个offer"),
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
    ("话题结束", "今天聊了好多"),
    ("话题结束", "有点晚了"),
    ("话题结束", "这周事情真多"),
    ("用户要忙", "我去忙了"),
    ("用户要忙", "先去开会"),
    ("用户要睡", "准备睡了，晚安"),
    ("图片评论", "刚看到一张挺有意思的图"),
    ("图片评论", "这张图拍得真好"),
]


def _run(prompts_path=None):
    if prompts_path:
        import os
        os.environ["PROMPTS_PATH"] = str(prompts_path)
    from agi.selflearn import llm
    from agi.selflearn.chat import SelfLearnAgent
    db_path = Path(tempfile.mkdtemp()) / "canned_validate.sqlite3"
    agent = SelfLearnAgent(db_path, client=llm.Client(), rng=random.Random(77))
    out = []
    for category, text in CASES:
        row = agent.send(text)
        reply = row.get("reply") or ""
        canned = list(dict.fromkeys(CANNED_RE.findall(reply)))
        closing = list(dict.fromkeys(CLOSING_RE.findall(reply)))
        allowed = bool(ALLOW_RE.search(text))
        bad_closing = [x for x in closing if not allowed]
        out.append({"category": category, "user": text, "reply": reply,
                    "canned_hits": canned, "closing_hits": closing,
                    "bad_closing": bad_closing,
                    "question": bool(re.search(r"[?？]", reply)),
                    "guard": (row.get("guard") or {}).get("canned_regenerated")})
    n = len(out) or 1
    return {
        "count": len(out),
        "canned_hits": sum(len(x["canned_hits"]) for x in out),
        "bad_closing": sum(len(x["bad_closing"]) for x in out),
        "question_rate": round(sum(1 for x in out if x["question"]) / n, 3),
        "replies": out,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tools.persona.validate_canned")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    result = _run()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(json.dumps({k: result[k] for k in ("count", "canned_hits",
                                                "bad_closing", "question_rate")},
                         ensure_ascii=False))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
