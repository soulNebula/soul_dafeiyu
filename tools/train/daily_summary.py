# -*- coding: utf-8 -*-
"""P30 每日汇总：本地 3B（8125）总结"今天哪里像人/哪里出戏"，失败规则兜底。

只读当天 daily jsonl，写 ``agi/data/train/daily/YYYY-MM-DD.summary.md``；
不训练、不写生产库、不联网（localhost 8125 除外）。
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRAIN_ROOT = ROOT / "agi" / "data" / "train"
DAILY_DIR = TRAIN_ROOT / "daily"
LOCAL3B_URL = "http://127.0.0.1:8125/v1/chat/completions"


def load_samples(date_str: str) -> list[dict]:
    path = DAILY_DIR / f"{date_str}.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _local3b_available() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8125/health", timeout=1.0) as resp:
            return 200 <= int(resp.status) < 300
    except Exception:  # noqa: BLE001
        return False


def _call_local3b(prompt: str) -> str:
    body = json.dumps({
        "model": "qwen2.5-3b-instruct",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2, "max_tokens": 500, "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        LOCAL3B_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return str(payload["choices"][0]["message"]["content"] or "").strip()


def build_prompt(samples: list[dict]) -> str:
    good = [row for row in samples if row.get("meta", {}).get("quality") in ("positive", "correction")]
    bad = [row for row in samples if row.get("meta", {}).get("quality") == "negative"]
    lines = ["下面是小鱼今天和用户的聊天样本（SFT 数据）。请用中文总结 3–5 条：",
             "1）今天她哪里像真人、哪里接得好；2）哪里出戏/像客服/让人不舒服；",
             "每条一句话，直接给结论，不要客套。"]
    lines.append("【像人的样本】")
    for row in good[:8]:
        lines.append(f"- 用户：{row['messages'][1]['content']} / 回复：{row['response']}")
    lines.append("【出戏/被否定的样本】")
    for row in bad[:8]:
        lines.append(f"- 用户：{row['messages'][1]['content']} / 回复：{row['response']}")
    lines.append("【输出格式】- 像人：...\\n- 出戏：...")
    return "\n".join(lines)


def rule_summary(samples: list[dict], date_str: str) -> str:
    good = [row for row in samples if row.get("meta", {}).get("quality") in ("positive", "correction")]
    bad = [row for row in samples if row.get("meta", {}).get("quality") == "negative"]
    reasons = Counter(row.get("meta", {}).get("reason", "") for row in bad)
    lines = [f"# {date_str} 每日语气总结（规则兜底）", "",
             f"- 样本：{len(samples)} 条（正/修正 {len(good)}、负 {len(bad)}）"]
    if good:
        lines.append(f"- 像人：有 {len(good)} 条接住并让用户继续延伸；"
                     f"例：{good[0]['messages'][1]['content'][:30]} → {good[0]['response'][:40]}")
    if bad:
        top = "、".join(f"{name}({count})" for name, count in reasons.most_common(3))
        lines.append(f"- 出戏：{len(bad)} 条被规则标负；主要模式：{top}")
    if not good and not bad:
        lines.append("- 今天没有可总结的样本（可能没聊天或全被过滤）。")
    lines.append("- 建议：负样本优先看「用户否定/重复问」；正样本保持短句和接梗。")
    return "\n".join(lines) + "\n"


def _trim_items(text: str, max_items: int = 5) -> str:
    """本地 3B 偶尔列太多条；只保留前 5 条 bullet，保证 3–5 条。"""
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    bullets = [line for line in lines if line.strip().startswith(("-", "*", "•"))]
    if len(bullets) <= max_items:
        return "\n".join(lines) + "\n"
    kept = [line for line in lines if line in bullets[:max_items]]
    return "\n".join(kept) + "\n"


def _negative_fallback_line(samples: list[dict]) -> str:
    bad = [row for row in samples if row.get("meta", {}).get("quality") == "negative"]
    if not bad:
        return "- 出戏：今天没有规则命中的负样本"
    row = bad[0]
    return (f"- 出戏：{row['messages'][1]['content'][:30]} → "
            f"{row['response'][:40]}（被规则标负/像客服）")


def run(date_str: str | None = None, *, use_llm: bool = True) -> dict:
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    samples = load_samples(date_str)
    summary = ""
    used = "rule"
    if use_llm and samples and _local3b_available():
        try:
            summary = _call_local3b(build_prompt(samples))
            used = "local3b"
        except Exception:  # noqa: BLE001
            summary = ""
    if not summary:
        summary = rule_summary(samples, date_str)
    if "出戏" not in summary and samples:
        summary = summary.rstrip() + "\n" + _negative_fallback_line(samples) + "\n"
    summary = _trim_items(summary)
    if not summary.startswith("#"):
        summary = f"# {date_str} 每日语气总结（本地 3B）\n\n{summary}\n"
    path = DAILY_DIR / f"{date_str}.summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(summary, encoding="utf-8")
    return {"date": date_str, "samples": len(samples), "used": used,
            "path": str(path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每日语气/出戏总结（本地 3B 优先）")
    parser.add_argument("--date", default="")
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.date or None, use_llm=not args.no_llm),
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
