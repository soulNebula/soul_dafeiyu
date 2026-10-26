# -*- coding: utf-8 -*-
"""P6-d：主动消息「做减法」A/B/C 真实验证（实验脚本，不改生产逻辑）。

A = 当前生产后处理（proactive.tick，P6-a 全套）
B = 简化：只保留人格边界/不编造/像微信/禁元话题；违规整条丢弃，不重写/不兜底
C = 极简：只保留人格边界/不编造；违规丢弃

media 全程关闭，隔离变量。真实 DeepSeek 生成 + DeepSeek pairwise 裁判。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

from agi.selflearn import db

ROOT = Path(__file__).resolve().parents[2]
CONTACT_DB = ROOT / "agi" / "data" / "selflearn" / "wechat_contacts" / "4260fd457d8355.sqlite3"
FEWSHOT = ROOT / "agi" / "eval_data" / "human_tone" / "fewshot.json"
TARGETS = (
    ("real-1017", 1792981038.2920806),
    ("real-1033a", 1792981990.0),
    ("real-1033b", 1792981990.0),
    ("real-1108", 1792984135.6322355),
)
SYNTHETIC_USER = (
    "今天加班到九点，脑子都木了",
    "周末想去附近走走，一直没定下来",
    "最近在学做手冲咖啡，还是有点苦",
    "刚看完一部老电影，结尾没太看懂",
    "电脑风扇一直响，有点烦",
    "想换个工作方向，但还没想清楚",
    "晚上总睡不好，白天没精神",
    "周末下雨，在家待了两天",
    "在整理旧照片，翻到很多以前的东西",
    "刚跑完步，腿有点酸",
    "看了个搞笑视频，笑到现在",
    "在学一门新语言，进度很慢",
    "朋友推荐了一家面馆，还没去",
    "最近在控制饮食，有点饿",
    "有个游戏卡关好几天了",
    "想买个头戴耳机，看了好久",
)
META_RE = re.compile(r"表情包|素材|清单|表情库|系统|指令|记忆库|记忆这回事|图片内容表情")
FRAGMENT_RE = re.compile(r"^(换个话题|聊点别的|说个事|对了|嗯|哦|行|好)[，,。…\s]*$")
NEGATIVE = ("烦", "累", "难受", "失望", "难过", "闷", "焦虑", "委屈", "心累", "没劲")
LAUGH = ("哈哈哈", "哈哈", "笑死", "笑死我了", "hhh", "233")
JUDGE_SYSTEM = (
    "你是微信聊天真实性裁判。下面给你参考：一个真实用户在微信里的说话样例，"
    "以及两条主动消息 A/B。请判断哪条更像真人在微信里主动发的话：自然、不尴尬、"
    "不碎片、不表演。只输出 JSON：{\"winner\":\"A\"|\"B\"|\"tie\",\"reason\":\"一句话\"}。"
)


def _human_examples(limit: int = 10) -> list[str]:
    try:
        data = json.loads(FEWSHOT.read_text(encoding="utf-8"))
        raws = [str(item.get("text") or "").strip()
                for item in data.get("examples") or []]
        raws = [text for text in raws if text]
        return raws[:limit]
    except Exception:  # noqa: BLE001
        return []


def _read_context(db_path: str, target_ts: float) -> dict:
    con = db.memory_connect(db_path)
    try:
        rows = con.execute(
            "SELECT role,text,ts FROM messages WHERE ts < ? ORDER BY ts DESC LIMIT 8",
            (target_ts - 60,)).fetchall()
    finally:
        con.close()
    recent = list(reversed([(str(role), str(text)) for role, text, _ in rows]))
    user_text = ""
    for role, text in reversed(recent):
        if role == "user":
            user_text = text
            break
    return {"recent": recent, "user_text": user_text or "今天有点累"}


def _copy_real_context(target_ts: float) -> tuple[str, dict]:
    if not CONTACT_DB.exists():
        raise FileNotFoundError(f"找不到真机上下文库：{CONTACT_DB}")
    tmp = tempfile.mkdtemp(prefix="p6d_real_")
    path = os.path.join(tmp, "ctx.sqlite3")
    shutil.copy2(CONTACT_DB, path)
    from agi.selflearn import db
    con = db.connect(path)
    try:
        for table, column in (("proactive", "created_at"), ("messages", "ts"),
                              ("turns", "created_at")):
            con.execute(f"DELETE FROM {table} WHERE {column} >= ?", (target_ts - 60,))
        con.commit()
    finally:
        con.close()
    return path, _read_context(path, target_ts)


def _seed_synthetic(user_text: str) -> tuple[str, dict]:
    from agi.selflearn import db
    path = os.path.join(tempfile.mkdtemp(prefix="p6d_syn_"), "ctx.sqlite3")
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", user_text, time.time() - 3600))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}), time.time()))
    con.commit()
    con.close()
    from agi.selflearn import trust
    trust.set_value(path, 0.6)
    return path, {"recent": [("user", user_text)], "user_text": user_text}


def _contexts() -> list[dict]:
    cases = []
    for name, ts in TARGETS:
        path, ctx = _copy_real_context(ts)
        cases.append({"name": name, "kind": "real", "path": path,
                      "recent": ctx["recent"], "user_text": ctx["user_text"],
                      "target_ts": ts})
    for index, user_text in enumerate(SYNTHETIC_USER):
        path, ctx = _seed_synthetic(user_text)
        cases.append({"name": f"syn-{index + 1:02d}", "kind": "synthetic",
                      "path": path, "recent": ctx["recent"],
                      "user_text": user_text, "target_ts": time.time()})
    return cases[:20]


def _make_client(dry: bool):
    if dry:
        return None
    from agi.selflearn import llm
    client = llm.Client()
    if not client.available():
        raise RuntimeError("DeepSeek key 不可用")
    return client


def _simple_prompt(case: dict, variant: str) -> tuple[str, str]:
    from agi.selflearn import proactive
    recent = "\n".join(f"{'用户' if role == 'user' else '你'}：{text}"
                       for role, text in case["recent"][-8:])
    system = proactive.PERSONA_CONSTRAINTS + "\n" + proactive.GROUNDING_CONSTRAINTS
    system += ("\n你是在微信里主动给朋友发一句自然的话：短、口语、不加句号，"
               "不要像客服/作文/独白。")
    if variant == "B":
        system += ("\n正文禁止出现：表情包/素材/清单/表情库/系统/指令/记忆库/记忆 这些词；"
                   "也不要解释你在做什么。")
    prompt = ("最近对话：\n" + recent +
              "\n\n请只输出你要主动发的这一句，不要输出 choice/分析/引号。")
    return system, prompt


def _generate_simple(client, case: dict, variant: str, dry: bool) -> dict:
    if dry:
        text = ("看到一个说法，挺有意思" if variant == "B"
                else "最近在整理旧资料，有点累")
        return {"text": text, "raw": text, "discarded": False, "reason": ""}
    from agi.selflearn import proactive
    system, prompt = _simple_prompt(case, variant)
    try:
        result = client.chat([{"role": "system", "content": system},
                              {"role": "user", "content": prompt}],
                             purpose="p6d_simple", max_tokens=300, temperature=0.8)
    except Exception as exc:  # noqa: BLE001
        return {"text": "", "raw": "", "discarded": True,
                "reason": f"call_failed:{type(exc).__name__}"}
    raw = str(result.get("text") or "") if result.get("ok") else ""
    _choice, text = proactive.parse_voice_choice(raw)
    text = text.strip().strip('"“”').strip()
    reason = ""
    if not text:
        return {"text": "", "raw": raw, "discarded": True, "reason": "empty"}
    if proactive._has_shared_experience_claim(text):
        reason = "shared_experience"
    if variant == "B" and META_RE.search(text):
        reason = "meta_topic"
    if reason:
        return {"text": "", "raw": raw, "discarded": True, "reason": reason}
    return {"text": text, "raw": raw, "discarded": False, "reason": ""}


def _run_a(client, case: dict, dry: bool) -> dict:
    from agi.selflearn import proactive, media_choice
    if dry:
        text = "资料还没理完，有点烦"
        return {"text": text, "raw": text, "discarded": False, "reason": "",
                "fallback": text in proactive._SAFE_FALLBACK_POOL}
    old_decide = proactive.initiative.decide
    proactive.initiative.decide = lambda *a, **k: {}
    proactive._ACTIVE_DB_PATH = case["path"]
    try:
        now = case["target_ts"] - 1
        row = proactive.tick(case["path"], client=client, now=now, cooldown=0,
                             min_score=0, respect_state=False)
        if not row.get("available"):
            row = proactive.tick(case["path"], client=client, now=now + 120,
                                 cooldown=0, min_score=0, respect_state=False)
    finally:
        proactive.initiative.decide = old_decide
        proactive._ACTIVE_DB_PATH = None
    text = str(row.get("message") or "")
    return {"text": text, "raw": text, "discarded": not bool(row.get("available")),
            "reason": str(row.get("reason") or ""),
            "fallback": text in proactive._SAFE_FALLBACK_POOL,
            "media": row.get("media"), "check": row.get("text_media_check")}


def _metrics(rows: list[dict]) -> dict:
    texts = [row["text"] for row in rows if not row["discarded"] and row["text"]]
    fragments = sum(1 for text in texts
                    if len(text.strip()) < 6 or FRAGMENT_RE.match(text.strip()))
    meta = sum(1 for text in texts if META_RE.search(text))
    contradictions = sum(1 for text in texts
                         if any(word in text for word in NEGATIVE)
                         and any(word in text for word in LAUGH))
    duplicates = len(texts) - len(set(texts))
    return {
        "count": len(rows),
        "kept": len(texts),
        "discarded": sum(1 for row in rows if row["discarded"]),
        "fragment_rate": round(fragments / max(1, len(texts)), 3),
        "meta_hits": meta,
        "contradictions": contradictions,
        "duplicates": duplicates,
        "avg_len": round(statistics.mean([len(text) for text in texts]), 1) if texts else 0,
    }


def _judge(client, a: str, b: str, human: list[str], dry: bool) -> str:
    if dry:
        return "TIE"
    prompt = ("参考真人语料：\n" + "\n".join(human[:8]) +
              f"\n\nA：{a}\nB：{b}\n\n哪条更像真人在微信聊天？只输出 JSON。")
    try:
        result = client.chat([{"role": "system", "content": JUDGE_SYSTEM},
                              {"role": "user", "content": prompt}],
                             purpose="p6d_judge", max_tokens=80, temperature=0)
        from agi.selflearn import llm
        parsed = llm.parse_json(str(result.get("text") or "")) or {}
        winner = str(parsed.get("winner") or "").upper()
        return winner if winner in ("A", "B", "TIE") else "TIE"
    except Exception:  # noqa: BLE001
        return "TIE"


def run_experiment(*, dry: bool = False, samples: int = 20) -> dict:
    os.environ["MEDIA_ENABLED"] = "0"
    os.environ.setdefault("INNER_MODE", "off")
    os.environ.setdefault("PROACTIVE_INNER", "off")
    from agi.selflearn import proactive
    cases = _contexts()[:samples]
    client = _make_client(dry)
    human = _human_examples()
    groups = {"A": [], "B": [], "C": []}
    for index, case in enumerate(cases):
        groups["A"].append(_run_a(client, case, dry))
        groups["B"].append(_generate_simple(client, case, "B", dry))
        groups["C"].append(_generate_simple(client, case, "C", dry))
    stats = {name: _metrics(rows) for name, rows in groups.items()}
    pairwise = {"A_vs_B": {"A": 0, "B": 0, "TIE": 0},
                "B_vs_C": {"B": 0, "C": 0, "TIE": 0}}
    for index in range(len(cases)):
        if groups["A"][index]["text"] and groups["B"][index]["text"]:
            winner = _judge(client, groups["A"][index]["text"],
                            groups["B"][index]["text"], human, dry)
            pairwise["A_vs_B"][winner] += 1
        if groups["B"][index]["text"] and groups["C"][index]["text"]:
            raw = _judge(client, groups["B"][index]["text"],
                         groups["C"][index]["text"], human, dry)
            winner = {"A": "B", "B": "C"}.get(raw, "TIE")
            pairwise["B_vs_C"][winner] += 1
    return {"stats": stats, "pairwise": pairwise,
            "human_examples": human[:5],
            "groups": {name: [{"case": cases[i]["name"], "user": cases[i]["user_text"],
                               **row} for i, row in enumerate(rows)]
                       for name, rows in groups.items()}}


def main() -> int:
    parser = argparse.ArgumentParser(description="P6-d A/B/C 主动消息实验")
    parser.add_argument("--dry", action="store_true", help="fake，不调 API")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    payload = run_experiment(dry=args.dry, samples=args.samples)
    print(json.dumps({"stats": payload["stats"], "pairwise": payload["pairwise"]},
                     ensure_ascii=False, indent=2))
    for name in ("A", "B", "C"):
        print(f"\n=== {name} 组 10 条 ===")
        for row in payload["groups"][name][:10]:
            extra = " [DISCARD:" + row["reason"] + "]" if row["discarded"] else ""
            print(f"[{row['case']}] {row['text']!r}{extra}")
    if args.out:
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
