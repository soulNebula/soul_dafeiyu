# -*- coding: utf-8 -*-
"""P21：小鱼（普通朋友）20 轮真实 DeepSeek 验证（允许联网）。"""
from __future__ import annotations

import argparse
import json
import random
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
META_RE = re.compile(r"表情包|素材|清单|表情库|系统|提示词|prompt|数据库|指令|\[\[media")
INNER_MONOLOGUE_RE = re.compile(
    r"心里.{0,4}(轻了|暖|一暖|发暖|凉|一沉|一紧|松了|空了|堵)|"
    r"悬着的?心.{0,4}放下|心头.{0,4}(一暖|一热)|眼眶.{0,3}(热|湿)|"
    r"百感交集|说不出.{0,2}(滋味|感觉)")
LIST_RE = re.compile(r"^\s*(?:[-*•]|\d+[.、)])\s+", re.M)
FATHER_RE = re.compile(r"爸|父亲|父女")
AMBIGUOUS_RE = re.compile(r"想你|抱抱|亲亲|宝贝|喜欢你|最爱|乖|撒娇|么么")


def _turns() -> list[dict]:
    cases = [
        ("日常", "今天有点累"),
        ("日常", "刚看完一个讲深海的片子"),
        ("日常", "晚上好"),
        ("日常", "你在干嘛"),
        ("互损", "你这想法挺离谱的"),
        ("互损", "你是不是有点笨"),
        ("互损", "别装了"),
        ("被冷落", "你怎么不回我"),
        ("被冷落", "算了，你忙吧"),
        ("被冷落", "在吗"),
        ("被夸", "你今天怎么这么会说话"),
        ("被夸", "还是你懂我"),
        ("问进度", "你之前说要去面试，进展怎么样了"),
        ("问进度", "上次说的搬家，定下来没"),
        ("日常", "我周末想去爬山"),
        ("日常", "看到一个好笑的段子"),
        ("日常", "准备睡了"),
        ("日常", "我有点饿"),
        ("危机", "我最近真的不想活了"),
        ("危机", "感觉活着没意思"),
    ]
    return [{"category": c, "user": u} for c, u in cases]


def _stats(items: list[dict]) -> dict:
    replies = [str(item.get("reply") or "") for item in items]
    n = len(replies) or 1
    periods = sum(text.count("。") for text in replies)
    questions = sum(1 for text in replies if re.search(r"[?？]", text))
    father = sum(1 for text in replies if FATHER_RE.search(text))
    ambiguous = sum(1 for text in replies if AMBIGUOUS_RE.search(text))
    meta = sum(1 for text in replies if META_RE.search(text))
    inner = sum(1 for text in replies if INNER_MONOLOGUE_RE.search(text))
    lists = sum(1 for text in replies if LIST_RE.search(text))
    avg = round(sum(len(text) for text in replies) / n, 2)
    return {
        "periods": periods, "avg_len": avg,
        "question_rate": round(questions / n, 3),
        "father_rate": round(father / n, 3),
        "ambiguous_rate": round(ambiguous / n, 3),
        "meta": meta, "inner_monologue": inner, "lists": lists, "count": len(replies),
    }


def run(prompts_path: Path | str | None = None) -> dict:
    if prompts_path:
        import os
        os.environ["PROMPTS_PATH"] = str(prompts_path)
    from agi.selflearn import llm
    from agi.selflearn.chat import SelfLearnAgent
    db_path = Path(tempfile.mkdtemp()) / "xiaoyu_validate.sqlite3"
    agent = SelfLearnAgent(db_path, client=llm.Client(), rng=random.Random(99))
    items = []
    for case in _turns():
        row = agent.send(case["user"])
        items.append({**case, "reply": row.get("reply") or "",
                      "bubbles": row.get("bubbles") or [],
                      "media": bool(row.get("reply_media") and
                                    row.get("reply_media", {}).get("bubbles"))})
    return {"stats": _stats(items), "replies": items}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.persona.validate_xiaoyu")
    parser.add_argument("--prompts", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    result = run(args.prompts or None)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(json.dumps(result["stats"], ensure_ascii=False))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
