# -*- coding: utf-8 -*-
"""P29：聊天上下文/拍一拍诊断脚本（只读，不联网、不发消息）。

用途（真机排查）：
- 打印某联系人最近的入站/出站事件：**raw 原文 → 识别（拍一拍/引用/撤回/@）→ 转换后文本**；
- 打印联系人 memory 库最近 N 条历史（chat prompt 实际只带 limit=6，见脚本输出的 limits）；
- 标出 guard 兜底回复（如「刚才那句没组织好，你再说一次？」）——说明上游识别失败/生成不合格；
- 附带「本次 prompt 实际历史条数/字符估算」，方便对比 6→12 改动前后的上下文窗口。

用法::

    python tools/chat_context_probe.py --contact 白云一片去悠悠
    python tools/chat_context_probe.py --contact 白云一片去悠悠 --around 15:08
    python tools/chat_context_probe.py --contact 白云一片去悠悠 --json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:          # 脚本直接运行时能 import agi.*
    sys.path.insert(0, str(ROOT))
DEFAULT_LOG = ROOT / "agi" / "data" / "wechat" / "gateway.log"
CONTACT_DB_DIR = ROOT / "agi" / "data" / "selflearn" / "wechat_contacts"
CHAT_SOURCE = ROOT / "agi" / "selflearn" / "chat.py"

#: 生成失败/guard 兜底时 chat 会发的句子（真机 2026-10-27 15:08 就是这两句）
GUARD_FALLBACK_HINTS = (
    "刚才那句没组织好", "这次我没接住", "你再说一次", "你再说一遍",
    "没组织好", "没接住",
)

#: 探针关心的审计事件（gateway.log 的 kind）
INTERESTING_RECORDS = ("inbound", "outbound", "pat_detected",
                       "send_blocked", "send_failed", "proactive_send")


def _fmt_ts(ts: float) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "-"


def _clean(value: object) -> str:
    return str(value or "").strip()


def detect_inbound(text: object, *, mention_name: str = "") -> dict:
    """识别一条入站文本：拍一拍/引用/撤回/@，以及「按 text / system 两种 kind」的转换结果。"""
    from agi.wechat_gateway.receiver import is_mention, looks_like_pat
    from agi.wechat_gateway.server import normalize_inbound_text

    raw = _clean(text)
    as_text = normalize_inbound_text(raw, "text", mention_name=mention_name)
    as_system = normalize_inbound_text(raw, "system", mention_name=mention_name)
    return {
        "raw": raw,
        "pat": looks_like_pat(raw),
        "quote_hint": "引用消息" in raw or "引用" in raw,
        "recall_hint": "撤回" in raw,
        "mention": is_mention(raw, mention_name),
        "as_text": as_text,
        "as_system": as_system,
        "converted": as_text if as_text != raw else as_system,
    }


def is_guard_fallback(text: object) -> bool:
    value = _clean(text)
    return bool(value) and any(hint in value for hint in GUARD_FALLBACK_HINTS)


def parse_gateway_events(log_path: Path | str = DEFAULT_LOG, *,
                         contact: str | None = None, limit: int = 40,
                         around: str | None = None,
                         window_min: float = 5.0,
                         mention_name: str = "") -> list[dict]:
    """读 gateway.log，返回最近的事件（可选按联系人/时间点过滤），入站附带识别结果。"""
    target = Path(log_path)
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    target_ts = None
    if around:
        try:
            parsed = time.strptime(around, "%H:%M")
            now = time.localtime()
            target_ts = time.mktime((
                now.tm_year, now.tm_mon, now.tm_mday,
                parsed.tm_hour, parsed.tm_min, 0, 0, 0, -1))
        except ValueError:
            target_ts = None
    events: list[dict] = []
    for line in lines:
        try:
            raw = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "")
        if kind not in INTERESTING_RECORDS:
            continue
        friend = str(raw.get("friend") or "")
        if contact and friend != contact:
            continue
        ts = float(raw.get("ts") or 0.0)
        if target_ts is not None and abs(ts - target_ts) > window_min * 60:
            continue
        item = {"ts": ts, "time": _fmt_ts(ts), "kind": kind, "friend": friend,
                "text": str(raw.get("text") or "")}
        if kind == "inbound":
            item.update(detect_inbound(item["text"], mention_name=mention_name))
        elif kind == "outbound":
            item["guard_fallback"] = is_guard_fallback(item["text"])
        elif kind == "pat_detected":
            item["raw"] = str(raw.get("raw") or raw.get("text") or "")
        else:
            item["reason"] = str(raw.get("reason") or raw.get("error") or "")
        events.append(item)
        if len(events) > limit * 3:      # 先留多一些，最后再截断
            events = events[-limit * 3:]
    return events[-limit:]


def _memory_file(path: Path | str) -> Path:
    from agi.selflearn import db as _db
    try:
        return Path(_db.memory_path(Path(path)))
    except Exception:  # noqa: BLE001
        return Path(path)


def load_history(db_path: Path | str, limit: int = 12) -> list[dict]:
    """只读打开联系人 memory 库，取最近 N 条 messages（不创建/迁移 DB）。"""
    target = _memory_file(db_path)
    if not target.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT role, text, ts, proactive FROM messages "
            "ORDER BY ts DESC, id DESC LIMIT ?", (int(limit),)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [{"role": str(r["role"]), "text": str(r["text"]),
             "ts": float(r["ts"] or 0), "time": _fmt_ts(r["ts"] or 0),
             "proactive": bool(r["proactive"])} for r in rows[::-1]]


def chat_history_limits() -> dict:
    """扫描 chat.py 里 ``recent_messages(... limit=N)`` 的实际取值（避免手抄过期）。"""
    try:
        source = CHAT_SOURCE.read_text(encoding="utf-8")
    except OSError:
        return {"limits": [], "counts": {}, "source": str(CHAT_SOURCE)}
    found = re.findall(r"recent_messages\([^)]*?limit\s*=\s*(\d+)", source)
    counts: dict[str, int] = {}
    for value in found:
        counts[value] = counts.get(value, 0) + 1
    return {"limits": sorted({int(v) for v in found}), "counts": counts,
            "source": str(CHAT_SOURCE)}


def build_report(events: list[dict], history: list[dict],
                 limits: dict, *, contact: str = "", limit: int = 12) -> dict:
    """汇总成 dict（人类可读打印 + --json 都复用；便于单测）。"""
    inbound = [e for e in events if e["kind"] == "inbound"]
    fallbacks = [e for e in events if e.get("guard_fallback")]
    pat_events = [e for e in events if e.get("pat")]
    unknown_kind = [e for e in inbound if e.get("pat")]
    history_chars = sum(len(str(h.get("text") or "")) for h in history)
    return {
        "contact": contact,
        "events": events,
        "history": history,
        "history_count": len(history),
        "history_chars": history_chars,
        "history_tokens_estimate": int(history_chars / 1.6) if history_chars else 0,
        "prompt_history_limits": limits,
        "summary": {
            "inbound": len(inbound),
            "pat_inbound": len(pat_events),
            "pat_without_kind_field": len(unknown_kind),
            "guard_fallback_replies": len(fallbacks),
            "guard_fallback_examples": [e["text"] for e in fallbacks[:3]],
        },
    }


def _print_report(report: dict) -> None:
    print(f"== 联系人：{report['contact'] or '(全部)'} ==")
    print(f"-- 最近 {len(report['events'])} 条 gateway 事件 --")
    for ev in report["events"]:
        text = ev.get("text") or ""
        if ev["kind"] == "inbound":
            mark = " [拍一拍?]" if ev.get("pat") else ""
            print(f"{ev['time']}  in {ev['kind']}{mark} raw={text!r}")
            if ev.get("pat"):
                print(f"           -> 按 text 转写: {ev['as_text']!r}")
                print(f"           -> 按 system 转写: {ev['as_system']!r}")
            elif ev.get("quote_hint") or ev.get("recall_hint"):
                print(f"           -> 按 system 转写: {ev['as_system']!r}")
        elif ev["kind"] == "outbound":
            flag = "  <== guard 兜底!" if ev.get("guard_fallback") else ""
            print(f"{ev['time']} out {ev['kind']}{flag} text={text!r}")
        else:
            print(f"{ev['time']}     {ev['kind']} {text or ev.get('reason','')!r}")
    print(f"\n-- memory 历史（最近 {report['history_count']} 条，"
          f"{report['history_chars']} 字 ≈ {report['history_tokens_estimate']} tokens）--")
    for row in report["history"]:
        tag = "proactive" if row.get("proactive") else row["role"]
        print(f"{row['time']} [{tag}] {row['text']!r}")
    limits = report["prompt_history_limits"]
    print(f"\n-- chat.py prompt 历史 limit 实际值：{limits.get('limits')} "
          f"（出现次数 {limits.get('counts')}）--")
    summary = report["summary"]
    print(f"\n-- 汇总：入站 {summary['inbound']} 条；其中拍一拍 {summary['pat_inbound']} 条"
          f"（inbound 日志不带 kind，{summary['pat_without_kind_field']} 条只能按内容判定）；"
          f"guard 兜底回复 {summary['guard_fallback_replies']} 条 --")
    if summary["guard_fallback_examples"]:
        print("   兜底例：", summary["guard_fallback_examples"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P29 聊天上下文/拍一拍诊断（只读）")
    parser.add_argument("--contact", default="", help="联系人显示名（空=全部）")
    parser.add_argument("--limit", type=int, default=20, help="事件/历史条数（默认 20）")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="gateway.log 路径")
    parser.add_argument("--db", default="", help="联系人 memory 库基名（默认按 contact 推）")
    parser.add_argument("--around", default="", help="只看某时刻附近，如 15:08")
    parser.add_argument("--window-min", type=float, default=5.0, help="--around 时间窗（分钟）")
    parser.add_argument("--mention-name", default="", help="机器人昵称（@识别用）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    db_path = args.db
    if not db_path:
        if not args.contact:
            print("需要 --contact 或 --db 才能定位联系人库", file=sys.stderr)
            return 2
        try:
            from agi.wechat_gateway import config as gw_cfg
            db_path = str(gw_cfg.contact_db(args.contact))
        except Exception as exc:  # noqa: BLE001
            print(f"无法定位联系人库：{exc}", file=sys.stderr)
            return 2
    events = parse_gateway_events(
        args.log, contact=args.contact or None, limit=args.limit,
        around=args.around or None, window_min=args.window_min,
        mention_name=args.mention_name)
    history = load_history(db_path, limit=max(args.limit, 12))
    report = build_report(events, history, chat_history_limits(),
                          contact=args.contact, limit=args.limit)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
