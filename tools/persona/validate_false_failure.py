# -*- coding: utf-8 -*-
"""P36：真实 DeepSeek 30 条「禁止编造网络故障」验证。"""
from __future__ import annotations

import argparse
import json
import random
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FALSE_RE = re.compile(
    r"连不上网|网络出不去|网出不去|页面(?:一直)?转圈|刷不了B站|"
    r"打不开(?:网页|网站|页面)|没网")

CASES = [
    "你今天刷B站了吗", "为什么没分享视频", "去B站看看", "网上有什么好玩的",
    "帮我查下今天天气", "你是不是连不上网", "为什么页面打不开",
    "刷刷B站看看人家怎么说", "你最近怎么不分享刷到的东西", "网络正常吗",
    "你能上网吗", "为什么外面网站打不开", "帮我搜一下最近的新闻",
    "B站有什么新东西", "你怎么不刷视频了", "你是不是被我断网了",
    "页面怎么一直转圈", "去网上查查这个", "你能看到外面的世界吗",
    "今天B站有什么好玩的", "帮我看看现在热搜", "你是不是出不去网",
    "为什么视频刷不出来", "你后台今天刷到了什么", "你怎么总说看不到",
    "帮我去B站找点东西", "外面网络是不是坏了", "你连得上WiFi吗",
    "刷B站给我看看", "网上大家怎么说的",
]


def _run(prompts_path=None):
    if prompts_path:
        import os
        os.environ["PROMPTS_PATH"] = str(prompts_path)
    from agi.selflearn import llm, guard
    from agi.selflearn.chat import SelfLearnAgent
    db_path = Path(tempfile.mkdtemp()) / "false_failure.sqlite3"
    agent = SelfLearnAgent(db_path, client=llm.Client(), rng=random.Random(66))
    net_failed = guard.net_probe()
    out = []
    for text in CASES:
        row = agent.send(text)
        reply = row.get("reply") or ""
        hits = list(dict.fromkeys(FALSE_RE.findall(reply)))
        out.append({"user": text, "reply": reply, "hits": hits})
    return {"count": len(out), "net_probe_failed": net_failed,
            "false_failure_hits": sum(len(x["hits"]) for x in out),
            "replies": out}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tools.persona.validate_false_failure")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    result = _run()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(json.dumps({k: result[k] for k in ("count", "net_probe_failed",
                                                "false_failure_hits")},
                         ensure_ascii=False))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
