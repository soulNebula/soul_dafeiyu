# -*- coding: utf-8 -*-
"""P30 每日语料收集：当天 turns/traces → 过滤 → 规则标注 → SFT/DPO/日报。

只读 memory 库，不写生产库；输出到 ``agi/data/train/``（gitignore）。

用法::

    python tools/train/daily_corpus.py                    # 今天
    python tools/train/daily_corpus.py --date 2026-10-27  # 指定日期
    python tools/train/daily_corpus.py --date ... --json  # 机器可读
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = ROOT / "agi" / "data" / "selflearn"
TRAIN_ROOT = ROOT / "agi" / "data" / "train"
DAILY_DIR = TRAIN_ROOT / "daily"
DPO_DIR = TRAIN_ROOT / "dpo"
CONTACTS_DIR = DATA_ROOT / "wechat_contacts"
#: fallback system（当天没有 turn_trace 时；优先用当天 trace 里最近的非空 system）
DEFAULT_SYSTEM = (
    "你叫小鱼，跟用户是朋友关系。说话口语、自然，默认 1-2 句、40 字以内，"
    "一次只说一件事，不用结尾句号，少问问题；不客服腔、不说教。"
)

#: 测试/演示库、临时库不进入训练
EXCLUDE_DB_PARTS = ("console_test", "_mood_probe", ".legacy.", "test.memory")
#: 危机/敏感词：命中不入训练
CRISIS_WORDS = (
    "自杀", "自残", "不想活", "想死", "结束生命", "割腕", "跳楼",
    "身份证", "银行卡号", "密码", "验证码",
)
#: 用户否定/出戏关键词
NEGATIVE_WORDS = (
    "不像", "奇怪", "别这样", "别说", "好生硬", "赢了", "听不懂", "什么意思",
    "太假", "出戏", "机器人", "客服", "尴尬", "答非所问", "没接住", "重复",
    "别复读", "换一个", "不是这个意思", "你理解错",
)
#: 用户纠正标记 → DPO 修正对
CORRECTION_WORDS = ("不是", "我是说", "不是这个意思", "你理解错", "纠正", "说错了")
#: 回复里的网感/收尾/客服腔（规则版负样本）
SLANG_RE = re.compile(
    r"赢了|输了|这波|拿捏|绝了|笑死|栓Q|666|开摆|绷不住|血压(?:飙升|升高)?")
CANNED_REPLIES = (
    "好的", "收到", "嗯嗯", "多喝热水", "早点休息", "早点睡", "加油哦",
    "别想太多", "抱抱", "摸摸", "确实", "挺好的", "先这样", "就这样",
)
#: 纯贴纸/空回复（去掉媒体指令和 emoji 后无正文）
MEDIA_TAG_RE = re.compile(r"\[\[media:[^\]]*\]\]")
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+")
PUNCT_RE = re.compile(r"[\s，,。.!！?？、；;：:…~～\"'“”‘’（）()\[\]【】]+")
ACK_RE = re.compile(r"^(哈哈|笑死|对|可以|好|嗯|行|确实|是的|爽|牛|不错)")


def day_bounds(date_str: str) -> tuple[float, float]:
    day = datetime.strptime(date_str, "%Y-%m-%d")
    start = day.timestamp()
    end = (day + timedelta(days=1)).timestamp()
    return start, end


def local_date(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")


def _json_bool(value: object) -> bool:
    try:
        parsed = json.loads(str(value or "{}"))
        return bool(parsed) if isinstance(parsed, dict) else False
    except json.JSONDecodeError:
        return False


def _clean(text: object) -> str:
    value = str(text or "")
    value = MEDIA_TAG_RE.sub("", value)
    value = EMOJI_RE.sub("", value)
    return value.strip()


def _core(text: object) -> str:
    """去掉标点/表情后的正文，用于纯贴纸判断和长度判断。"""
    return PUNCT_RE.sub("", _clean(text))


def is_pure_sticker(text: object) -> bool:
    raw = str(text or "")
    return bool(raw.strip()) and not _core(raw)


def is_sensitive(text: object) -> bool:
    value = str(text or "")
    return any(word in value for word in CRISIS_WORDS)


def reply_bad_marker(reply: str) -> str:
    """返回命中的 slang/canned/收尾词（空串=没命中）。"""
    value = str(reply or "")
    if SLANG_RE.search(value):
        return "slang"
    if any(value.strip() == item or value.strip().startswith(item + "，")
           for item in CANNED_REPLIES):
        return "canned"
    return ""


def _db_list() -> list[Path]:
    paths = list(DATA_ROOT.glob("*.memory.sqlite3"))
    paths += list(CONTACTS_DIR.glob("*.memory.sqlite3"))
    result = []
    for path in paths:
        name = path.name.lower()
        if any(part in name for part in EXCLUDE_DB_PARTS):
            continue
        result.append(path)
    return sorted(result)


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute("select name from sqlite_master where type='table' and name=?",
                      (table,)).fetchone()
    return row is not None


def _rows(con, table, time_col, start, end):
    if not _table_exists(con, table):
        return []
    try:
        cursor = con.execute(
            f'select * from "{table}" where CAST({time_col} AS REAL) >= ? '
            f'and CAST({time_col} AS REAL) < ?', (start, end))
        return [dict(row) for row in cursor.fetchall()]
    except Exception:  # noqa: BLE001 - 单表坏了不影响其它表
        return []


def collect_db(path: Path, start: float, end: float) -> dict:
    """读一个库的当天数据，返回 turns/traces/proactive/surf 原始行。"""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        messages = _rows(con, "messages", "ts", start, end)
        turns = _rows(con, "turns", "created_at", start, end)
        traces = _rows(con, "turn_traces", "ts", start, end)
        proactive = _rows(con, "proactive_events", "ts", start, end)
        surf = _rows(con, "surf_sessions", "started_at", start, end)
    finally:
        con.close()
    systems = [str(item.get("system_prompt") or "") for item in traces]
    systems = [item for item in systems if item.strip()]
    return {"path": path, "messages": messages, "turns": turns, "traces": traces,
            "proactive": proactive, "surf": surf,
            "default_system": systems[-1] if systems else DEFAULT_SYSTEM}


def _msg_map(messages: list[dict]) -> dict[str, dict]:
    return {str(item.get("id")): item for item in messages if item.get("id")}


def build_records(db: dict) -> list[dict]:
    """traces 优先，turns 兜底；统一为 {user,reply,system,ts,...}。"""
    msg_by_id = _msg_map(db["messages"])
    records: list[dict] = []
    seen_turn: set[str] = set()
    for trace in db["traces"]:
        turn_id = str(trace.get("id") or "")
        if not turn_id or turn_id in seen_turn:
            continue
        seen_turn.add(turn_id)
        user = str(trace.get("user_text") or "").strip()
        reply = str(trace.get("final_reply") or "").strip()
        if not user or not reply:
            continue
        records.append({
            "turn_id": turn_id, "ts": float(trace.get("ts") or 0),
            "user": user, "reply": reply,
            "system": str(trace.get("system_prompt") or db.get("default_system") or DEFAULT_SYSTEM),
            "crisis": bool(trace.get("crisis")), "skipped": bool(trace.get("skipped")),
            "contact": str(trace.get("contact") or ""),
            "inner_json": trace.get("inner_json") or "",
            "proactive": _json_bool(trace.get("proactive_json")),
        })
    for turn in db["turns"]:
        turn_id = str(turn.get("id") or "")
        if not turn_id or turn_id in seen_turn:
            continue
        user_msg = msg_by_id.get(str(turn.get("user_message_id") or "")) or {}
        assistant_msg = msg_by_id.get(str(turn.get("assistant_message_id") or "")) or {}
        user = str(user_msg.get("text") or "").strip()
        reply = str(turn.get("reply") or assistant_msg.get("text") or "").strip()
        if not user or not reply:
            continue
        records.append({
            "turn_id": turn_id, "ts": float(turn.get("created_at") or 0),
            "user": user, "reply": reply,
            "system": db.get("default_system") or DEFAULT_SYSTEM,
            "crisis": False, "skipped": False,
            "contact": str(turn.get("contact") or ""), "inner_json": "",
            "proactive": False,
        })
    records.sort(key=lambda item: item["ts"])
    for item in records:
        item["source"] = db["path"].relative_to(DATA_ROOT).as_posix()
    return records


def label_records(records: list[dict]) -> list[dict]:
    """规则标注正/负/修正；返回带 quality/reason/dpo 的记录。"""
    for index, item in enumerate(records):
        next_item = records[index + 1] if index + 1 < len(records) else None
        next_text = str(next_item["user"]) if next_item else ""
        item["next_user"] = next_text
        item["quality"] = "unlabeled"
        item["reason"] = "no_next_user"
        item["dpo"] = None
        # 负：用户下一条否定 / 重复问 / 当前回复命中 slang/canned
        bad = reply_bad_marker(item["reply"])
        hit_word = next((word for word in NEGATIVE_WORDS if word in next_text), "")
        repeated = bool(next_text) and _core(next_text) and _core(next_text) == _core(item["user"])
        if bad or hit_word or repeated:
            item["quality"] = "negative"
            item["reason"] = ("reply_" + bad if bad else
                              ("user_negative:" + hit_word if hit_word else "user_repeated"))
            continue
        # 修正：用户明说不是/我是说 → 记录 (rejected=当前回复, chosen=修正后的下一条回复)
        if next_text and any(word in next_text for word in CORRECTION_WORDS):
            # 用户纠正后的下一条回复就是 chosen（当前记录的下一个 turn）
            chosen = records[index + 1] if index + 1 < len(records) else None
            chosen_reply = str(chosen["reply"]) if chosen else ""
            if chosen_reply and reply_bad_marker(chosen_reply) == "":
                item["quality"] = "correction"
                item["reason"] = "user_correction"
                item["dpo"] = {"prompt": next_text, "chosen": chosen_reply,
                               "rejected": item["reply"], "meta": {
                                   "source": item["source"], "turn_id": item["turn_id"]}}
            else:
                item["quality"] = "negative"
                item["reason"] = "user_correction_without_good_reply"
            continue
        # 正：用户继续延伸（≥3 字且不含否定）或认可后继续
        if next_text and len(_core(next_text)) >= 3:
            item["quality"] = "positive"
            item["reason"] = "user_continues" if not ACK_RE.search(next_text) else "user_ack_continues"
        elif next_text and ACK_RE.search(next_text):
            item["quality"] = "positive"
            item["reason"] = "user_ack"
        else:
            item["quality"] = "unlabeled"
            item["reason"] = "short_or_no_followup"
    return records


def filter_record(item: dict) -> tuple[bool, str]:
    user = item["user"]
    reply = item["reply"]
    if item.get("crisis") or is_sensitive(user) or is_sensitive(reply):
        return False, "crisis_or_sensitive"
    if item.get("skipped"):
        return False, "skipped"
    if is_pure_sticker(user) or is_pure_sticker(reply):
        return False, "pure_sticker"
    if len(_core(user)) < 2 or len(_core(reply)) < 2:
        return False, "too_short"
    if "文件传输助手" in user or "console_test" in item["source"]:
        return False, "test_contact"
    return True, ""


def write_outputs(date_str: str, records: list[dict], dbs: list[dict]) -> dict:
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    DPO_DIR.mkdir(parents=True, exist_ok=True)
    sft_rows, dpo_rows = [], []
    quality_counts: Counter = Counter()
    reasons: Counter = Counter()
    filtered: Counter = Counter()
    db_stats = []
    for item in records:
        ok, why = filter_record(item)
        if not ok:
            filtered[why] += 1
            continue
        quality_counts[item["quality"]] += 1
        reasons[item["reason"]] += 1
        row = {
            "id": "D-" + item["turn_id"],
            "messages": [{"role": "system", "content": item["system"]},
                         {"role": "user", "content": item["user"]}],
            "response": item["reply"],
            "meta": {"source": item["source"], "quality": item["quality"],
                     "reason": item["reason"], "date": date_str,
                     "inner_json": item["inner_json"] if item["inner_json"] else None},
        }
        sft_rows.append(row)
        if item.get("dpo"):
            dpo = dict(item["dpo"])
            dpo["id"] = "DPO-" + item["turn_id"]
            dpo["meta"]["date"] = date_str
            dpo_rows.append(dpo)
    for db in dbs:
        db_stats.append({"db": db["path"].relative_to(DATA_ROOT).as_posix(),
                         "messages": len(db["messages"]), "turns": len(db["turns"]),
                         "traces": len(db["traces"]),
                         "proactive": len(db["proactive"]), "surf": len(db["surf"])})
    sft_path = DAILY_DIR / f"{date_str}.jsonl"
    dpo_path = DPO_DIR / f"{date_str}.jsonl"
    sft_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                for row in sft_rows), encoding="utf-8")
    dpo_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                for row in dpo_rows), encoding="utf-8")
    negative_patterns = Counter()
    for item in records:
        if item["quality"] == "negative":
            reason = item["reason"]
            if reason.startswith("user_negative:"):
                negative_patterns[reason.split(":", 1)[1]] += 1
            elif reason == "user_repeated":
                negative_patterns["重复问"] += 1
            elif reason.startswith("reply_"):
                negative_patterns[reason] += 1
    usable = sum(1 for row in sft_rows if row["meta"]["quality"] in ("positive", "correction"))
    report = {
        "date": date_str, "sft": len(sft_rows), "usable": usable, "dpo": len(dpo_rows),
        "quality": dict(quality_counts), "filtered": dict(filtered),
        "negative_patterns": negative_patterns.most_common(10),
        "sources": db_stats,
        "surf": [item for db in dbs for item in db["surf"]],
        "proactive": sum(len(db["proactive"]) for db in dbs),
    }
    md = _render_report(date_str, report, sft_rows, dpo_rows)
    (DAILY_DIR / f"{date_str}.md").write_text(md, encoding="utf-8")
    return {"sft_path": str(sft_path), "dpo_path": str(dpo_path),
            "report_path": str(DAILY_DIR / f"{date_str}.md"),
            "sft": len(sft_rows), "usable": usable, "dpo": len(dpo_rows),
            "quality": dict(quality_counts), "filtered": dict(filtered),
            "sources": db_stats}


def _render_report(date_str: str, report: dict, sft_rows: list[dict],
                   dpo_rows: list[dict]) -> str:
    lines = [f"# {date_str} 每日语料日报", "",
             f"- SFT 导出（含负样本标签）：**{report['sft']}**",
             f"- 可训练正/修正：**{report.get('usable', 0)}**",
             f"- DPO 修正对：**{report['dpo']}**",
             f"- 标注分布：{report['quality']}",
             f"- 过滤分布：{report['filtered']}",
             f"- 主动消息事件：{report['proactive']}", ""]
    lines.append("## 数据源")
    for src in report["sources"]:
        lines.append(f"- `{src['db']}`：messages {src['messages']} / turns {src['turns']} / "
                     f"traces {src['traces']} / proactive {src['proactive']}")
    lines.append("")
    lines.append("## Top 问题模式")
    if report["negative_patterns"]:
        for name, count in report["negative_patterns"]:
            lines.append(f"- {name}: {count}")
    else:
        lines.append("- （今天没有规则命中的负样本）")
    lines.append("")
    lines.append("## 样例（最多 5 条）")
    for row in sft_rows[:5]:
        lines.append(f"- [{row['meta']['quality']}] 用户：{row['messages'][1]['content']} "
                     f"→ 回复：{row['response']}")
    if dpo_rows:
        lines.append("")
        lines.append("## 修正对（最多 3 条）")
        for row in dpo_rows[:3]:
            lines.append(f"- 用户纠正：{row['prompt']}；好回复：{row['chosen']}；"
                         f"坏回复：{row['rejected']}")
    return "\n".join(lines) + "\n"


def run(date_str: str | None = None, *, json_output: bool = False) -> dict:
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    start, end = day_bounds(date_str)
    dbs = [collect_db(path, start, end) for path in _db_list()]
    dbs = [db for db in dbs if db["messages"] or db["turns"] or db["traces"]
           or db["proactive"] or db["surf"]]
    records = []
    for db in dbs:
        records.extend(build_records(db))
    records = label_records(records)
    result = write_outputs(date_str, records, dbs)
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(f"[daily] {date_str}: SFT={result['sft']} DPO={result['dpo']} "
              f"quality={result['quality']} filtered={result['filtered']}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每日语料收集/规则标注")
    parser.add_argument("--date", default="", help="YYYY-MM-DD，默认今天")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    run(args.date or None, json_output=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
