# -*- coding: utf-8 -*-
"""P32 自我叙事：近 7 天素材 → DeepSeek 周记 → ≤120 字自我叙事 → core 人格层。

设计（先做地基，不接线 chat/gateway/console）：
- ``weekly_review``：汇总近 7 天日记/心情/兴趣/被否定事件/冲浪关键词 → DeepSeek 写
  第一人称 300–500 字周记 → 提炼 ≤120 字叙事 → 写 persona_layers core ``self_narrative``；
  旧值归档 ``narrative_history.jsonl``；observe→approve（approved_by=weekly_review）；
  周记存 ``agi/data/diary/weekly/YYYY-Wxx.md``；同一周文件已存在时幂等跳过。
- ``inject_line``：给 chat 注入的 ≤80 字短句（无叙事返回 ""）。
- ``current`` / ``history``：读当前叙事与归档。

测试/离线时传 ``client`` 假对象；LLM 不可用则规则兜底（不阻断日报）。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from agi.paths import data_dir as _default_data_dir

from . import db, persona_layers

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = db.DB_PATH
DEFAULT_DIARY_DIR = _default_data_dir() / "diary"
DEFAULT_WEEKLY_DIR = DEFAULT_DIARY_DIR / "weekly"
DEFAULT_HISTORY_PATH = _default_data_dir() / "selflearn" / "narrative_history.jsonl"
DEFAULT_DAILY_CORPUS_DIR = _default_data_dir() / "train" / "daily"
MAX_NARRATIVE_CHARS = 120
MAX_INJECT_CHARS = 80
WEEK_SECONDS = 7 * 86400


def week_id(when: datetime | None = None) -> str:
    when = when or datetime.now()
    iso = when.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _clean_text(text: Any) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^```[a-zA-Z]*\s*", "", value)
    value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _short(text: str, limit: int) -> str:
    value = _clean_text(text)
    return value if len(value) <= limit else value[:limit].rstrip()


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _table_exists(con, table: str) -> bool:
    # RouterConnection 的 execute 会把 sqlite_master 路由到 memory；persona 表要单独查
    candidates = []
    for name in ("persona", "memory"):
        candidate = getattr(con, name, None)
        if candidate is not None:
            candidates.append(candidate)
    candidates.append(con)
    for candidate in candidates:
        try:
            row = candidate.execute(
                "select name from sqlite_master where type='table' and name=?",
                (table,)).fetchone()
            if row:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _rows(con, table: str, time_col: str, start: float, end: float,
          limit: int = 500) -> list[dict]:
    if not _table_exists(con, table):
        return []
    try:
        cursor = con.execute(
            f'select * from "{table}" where CAST({time_col} AS REAL)>=? '
            f'and CAST({time_col} AS REAL)<? order by {time_col} desc limit ?',
            (start, end, int(limit)))
        return [dict(row) for row in cursor.fetchall()]
    except Exception:  # noqa: BLE001
        return []


def _read_diary(diary_dir: Path, start: float, end: float, limit: int = 5) -> list[str]:
    """读近 7 天日记片段（P31 可能还没落盘，目录不存在时返回空）。"""
    if not diary_dir.exists():
        return []
    snippets: list[str] = []
    for path in sorted(diary_dir.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if not (start <= mtime <= end):
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            snippets.append(text[:800])
        if len(snippets) >= limit:
            break
    return snippets


def _read_negative_events(daily_dir: Path, start: float, end: float) -> dict:
    """从 P30 daily SFT 标负样本读“被否定事件”（不存在时返回空）。"""
    counts: dict[str, int] = {}
    samples: list[str] = []
    if not daily_dir.exists():
        return {"count": 0, "patterns": {}, "samples": []}
    for path in sorted(daily_dir.glob("*.jsonl")):
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d")
        except ValueError:
            continue
        if not (start <= day.timestamp() < end):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            meta = row.get("meta") or {}
            if meta.get("quality") != "negative":
                continue
            reason = str(meta.get("reason") or "negative")
            counts[reason] = counts.get(reason, 0) + 1
            if len(samples) < 5:
                user = ((row.get("messages") or [{}])[-1].get("content") or "")
                samples.append(f"{user[:30]} → {str(row.get('response') or '')[:40]}")
    return {"count": sum(counts.values()), "patterns": counts, "samples": samples}


def collect_week(*, db_path: Path | str = DEFAULT_DB_PATH, now: datetime | None = None,
                 diary_dir: Path | str = DEFAULT_DIARY_DIR,
                 daily_dir: Path | str = DEFAULT_DAILY_CORPUS_DIR) -> dict:
    """近 7 天素材汇总（只读；所有表缺失都降级为空）。"""
    now = now or datetime.now()
    end = now.timestamp()
    start = end - WEEK_SECONDS
    con = db.connect(db_path)
    try:
        messages = _rows(con, "messages", "ts", start, end, limit=1000)
        turns = _rows(con, "turns", "created_at", start, end, limit=1000)
        feedback = _rows(con, "feedback", "created_at", start, end, limit=200)
        inner = _rows(con, "inner_states", "created_at", start, end, limit=200)
        surf = _rows(con, "surf_sessions", "started_at", start, end, limit=100)
        impressions = _rows(con, "bili_impressions", "created_at", start, end, limit=100)
        interests = []
        if _table_exists(con, "interests"):
            try:
                interests = [dict(row) for row in con.execute(
                    "select topic,weight,hits from interests order by weight desc, "
                    "last_seen_at desc limit 10").fetchall()]
            except Exception:  # noqa: BLE001
                interests = []
    finally:
        con.close()
    moods = []
    stages = {}
    for row in inner:
        state = _as_json(row.get("state_json")) or {}
        if isinstance(state, dict):
            moods.append(_as_float(state.get("mood"), 0.5))
            stage = str(state.get("stage") or "").strip()
            if stage:
                stages[stage] = stages.get(stage, 0) + 1
    topic_counts: dict[str, int] = {}
    surf_lines = []
    for row in surf:
        summary = str(row.get("summary") or "").strip()
        if summary:
            surf_lines.append(summary[:160])
        keywords = _as_json(row.get("keywords_json")) or []
        if isinstance(keywords, list):
            for item in keywords:
                topic = str(item or "").strip()
                if topic:
                    topic_counts[topic] = topic_counts.get(topic, 0) + 1
    for row in impressions:
        for item in (_as_json(row.get("topics_json")) or []):
            topic = str(item or "").strip()
            if topic:
                topic_counts[topic] = topic_counts.get(topic, 0) + 1
    diary = _read_diary(Path(diary_dir), start, end)
    negative = _read_negative_events(Path(daily_dir), start, end)
    return {
        "window": {"start": start, "end": end,
                   "start_iso": datetime.fromtimestamp(start).strftime("%Y-%m-%d"),
                   "end_iso": now.strftime("%Y-%m-%d")},
        "messages": len(messages),
        "user_messages": sum(1 for row in messages if row.get("role") == "user"),
        "turns": len(turns),
        "feedback": [{"reaction": str(row.get("reaction") or ""),
                      "note": str(row.get("note") or "")[:80]} for row in feedback],
        "mood_avg": round(sum(moods) / len(moods), 2) if moods else None,
        "stages": stages,
        "interests": [{"topic": str(row.get("topic") or ""),
                       "weight": round(_as_float(row.get("weight")), 2)}
                      for row in interests],
        "surf_topics": sorted(topic_counts.items(), key=lambda item: item[1], reverse=True)[:8],
        "surf_lines": surf_lines[:5],
        "diary": diary,
        "negative": negative,
    }


def _fallback_narrative(stats: dict) -> str:
    topics = "、".join(item[0] for item in stats.get("surf_topics", [])[:3])
    mood = stats.get("mood_avg")
    mood_text = "情绪还算平稳" if mood is None else (
        "心情偏亮" if mood >= 0.65 else ("心情偏低" if mood <= 0.4 else "情绪平稳"))
    parts = [f"这周聊了{stats.get('turns', 0)}轮、收到{stats.get('messages', 0)}条消息，{mood_text}。"]
    if topics:
        parts.append(f"最近在留意{topics}。")
    if stats.get("negative", {}).get("count"):
        parts.append("有几句话被对方说生硬，我记下了，下一句先接住再说。")
    else:
        parts.append("我还在学怎么把话说得更像自己。")
    return _short("".join(parts), MAX_NARRATIVE_CHARS)


def _fallback_weekly(stats: dict, week: str) -> str:
    topics = "、".join(item[0] for item in stats.get("surf_topics", [])[:5]) or "一些琐碎的事"
    lines = [f"这是{week}这一周。",
             f"这周我们聊了{stats.get('turns', 0)}轮，收到{stats.get('messages', 0)}条消息。",
             f"我最近在看{topics}。"]
    if stats.get("negative", {}).get("count"):
        lines.append("也有人说我某句话太生硬，我试着别急着解释，先把对方的话接住。")
    if stats.get("diary"):
        lines.append("日记里记着一些零散的心情，我还在慢慢理。")
    lines.append("我不知道这算不算更像人，但我在记住这些，也在改。")
    return _short("".join(lines), 500)


def _call_llm(client: Any, prompt: str, *, purpose: str, max_tokens: int = 700) -> str:
    if client is None:
        from agi.selflearn import llm
        client = llm.Client()
    if hasattr(client, "chat"):
        result = client.chat([{"role": "user", "content": prompt}],
                             purpose=purpose, max_tokens=max_tokens,
                             temperature=0.7)
        if isinstance(result, dict):
            if not result.get("ok", True):
                raise RuntimeError(str(result.get("error") or "llm_failed"))
            return _clean_text(result.get("text") or result.get("reply") or "")
        return _clean_text(result)
    if callable(client):
        return _clean_text(client(prompt))
    raise RuntimeError("client has no chat()")


def _weekly_prompt(stats: dict, week: str) -> str:
    lines = [
        "你是小鱼。根据下面过去 7 天的真实素材，用第一人称写一篇周记。",
        "要求：300–500 字；具体、口语、不要总结报告腔；写你注意到什么、在意什么、",
        "哪里觉得自己像人、哪里出戏、下周想怎么改；不要编造素材里没有的事实。",
        "",
        f"周次：{week}",
        f"对话轮次：{stats['turns']}，消息数：{stats['messages']}（用户 {stats['user_messages']}）",
        f"心情均值：{stats['mood_avg']}；阶段：{stats['stages']}",
    ]
    if stats["interests"]:
        lines.append("兴趣：" + "、".join(
            f"{item['topic']}({item['weight']})" for item in stats["interests"][:8]))
    if stats["surf_topics"]:
        lines.append("冲浪关键词：" + "、".join(
            f"{topic}({count})" for topic, count in stats["surf_topics"]))
    if stats["surf_lines"]:
        lines.append("冲浪摘要：" + " / ".join(stats["surf_lines"]))
    if stats["diary"]:
        lines.append("日记片段：" + " / ".join(item[:200] for item in stats["diary"][:3]))
    if stats["negative"]["count"]:
        lines.append("被否定事件：" + "；".join(
            f"{key}×{value}" for key, value in stats["negative"]["patterns"].items()))
        lines.extend("  - " + item for item in stats["negative"]["samples"])
    if stats["feedback"]:
        lines.append("反馈：" + "、".join(
            f"{item['reaction']}:{item['note']}" for item in stats["feedback"][:5]))
    return "\n".join(lines)


def _narrative_prompt(weekly: str) -> str:
    return (
        "把下面这篇周记提炼成一段不超过 120 字的自我叙事。\n"
        "第一人称，只写“我是谁/我在意什么/我正在怎么变化”，具体、克制，不要复述事件清单，"
        "不要编造周记里没有的内容。只输出叙事正文。\n\n"
        + weekly
    )


def _active_narrative_row(db_path: Path | str) -> dict | None:
    con = db.connect(db_path)
    try:
        if not _table_exists(con, "persona_layers"):
            return None
        rows = [dict(row) for row in con.execute(
            "select * from persona_layers where layer='core' and key='self_narrative' "
            "and status='active' order by updated_at desc limit 1").fetchall()]
        return rows[0] if rows else None
    finally:
        con.close()


def current(*, db_path: Path | str = DEFAULT_DB_PATH) -> dict:
    row = _active_narrative_row(db_path)
    if not row:
        return {"ok": True, "text": "", "found": False}
    return {"ok": True, "text": str(row.get("value") or ""), "found": True,
            "id": row.get("id"), "approved_by": row.get("approved_by"),
            "updated_at": row.get("updated_at"), "version": row.get("version")}


def inject_line(*, db_path: Path | str = DEFAULT_DB_PATH) -> str:
    """给 chat 注入的短句；无叙事返回空串（chat 侧空串逐字节不变）。"""
    text = _clean_text(current(db_path=db_path).get("text") or "")
    return _short(text, MAX_INJECT_CHARS)


def history(*, path: Path | str = DEFAULT_HISTORY_PATH, limit: int = 20) -> list[dict]:
    target = Path(path)
    if not target.exists():
        return []
    rows = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-int(limit):][::-1]


def _archive_old(path: Path, old: str, *, week: str, new: str) -> None:
    if not old:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), "week": week, "old": old, "new": new,
             "reason": "weekly_review"}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def maybe_weekly(*, now: datetime | None = None,
                 weekly_dir: Path | str = DEFAULT_WEEKLY_DIR,
                 enabled: bool = True) -> dict:
    """gateway 周日 23:30 检查点：返回是否该跑本周周记（不写任何文件）。

    规则：每周日 23:30 到期；到期后本周 weekly md 不存在 → should_run=True；
    已存在/未到期/开关关闭 → False。gateway 只需在周日线程里调用本函数，
    True 时再调 ``weekly_review``。
    """
    now = now or datetime.now()
    week = week_id(now)
    weekly_path = Path(weekly_dir) / f"{week}.md"
    monday = now - timedelta(days=now.isoweekday() - 1)
    due = (monday + timedelta(days=6)).replace(
        hour=23, minute=30, second=0, microsecond=0)
    next_week = due + timedelta(days=7)
    base = {"ok": True, "week": week, "due_at": due.isoformat(),
            "next_run": next_week.isoformat(), "diary_path": str(weekly_path),
            "weekly_exists": weekly_path.exists()}
    if not enabled:
        return {**base, "should_run": False, "reason": "disabled"}
    if now < due:
        return {**base, "should_run": False, "reason": "not_due",
                "next_run": due.isoformat()}
    if weekly_path.exists():
        return {**base, "should_run": False, "reason": "already_ran"}
    return {**base, "should_run": True, "reason": "due"}


def weekly_review(*, db_path: Path | str = DEFAULT_DB_PATH,
                  now: datetime | None = None, client: Any = None,
                  weekly_dir: Path | str = DEFAULT_WEEKLY_DIR,
                  history_path: Path | str = DEFAULT_HISTORY_PATH,
                  diary_dir: Path | str = DEFAULT_DIARY_DIR,
                  daily_dir: Path | str = DEFAULT_DAILY_CORPUS_DIR,
                  force: bool = False) -> dict:
    """跑一次周记/自我叙事；同周已有周记时幂等跳过（force 可重跑）。"""
    now = now or datetime.now()
    week = week_id(now)
    weekly_path = Path(weekly_dir) / f"{week}.md"
    if weekly_path.exists() and not force:
        return {"ok": True, "skipped": True, "reason": "week_exists", "week": week,
                "diary_path": str(weekly_path),
                "narrative": current(db_path=db_path).get("text") or ""}
    stats = collect_week(db_path=db_path, now=now, diary_dir=diary_dir,
                         daily_dir=daily_dir)
    llm_used = "deepseek"
    weekly_text = ""
    narrative = ""
    try:
        weekly_text = _call_llm(client, _weekly_prompt(stats, week),
                                purpose="self_narrative_weekly", max_tokens=900)
        if len(_clean_text(weekly_text)) < 120:
            raise RuntimeError("weekly_too_short")
        narrative = _call_llm(client, _narrative_prompt(weekly_text),
                              purpose="self_narrative_extract", max_tokens=300)
        if not narrative:
            raise RuntimeError("narrative_empty")
    except Exception:  # noqa: BLE001 - LLM 不可用/输出不合格：规则兜底，不阻断
        llm_used = "fallback"
        weekly_text = _fallback_weekly(stats, week)
        narrative = _fallback_narrative(stats)
    weekly_text = _short(weekly_text, 500)
    narrative = _short(narrative, MAX_NARRATIVE_CHARS)
    old_row = _active_narrative_row(db_path)
    old_text = str((old_row or {}).get("value") or "")
    changed = narrative != old_text
    if changed:
        observed = persona_layers.observe(db_path, "self_narrative", narrative,
                                        evidence=[week, "weekly_review"], layer="core")
        if not observed.get("ok"):
            return {"ok": False, "error": observed.get("error") or "observe_failed",
                    "week": week, "narrative": narrative}
        approved = persona_layers.approve(db_path, str(observed["id"]), "weekly_review")
        if not approved.get("ok"):
            return {"ok": False, "error": approved.get("error") or "approve_failed",
                    "week": week, "narrative": narrative}
        _archive_old(Path(history_path), old_text, week=week, new=narrative)
    weekly_path.parent.mkdir(parents=True, exist_ok=True)
    weekly_path.write_text(
        f"# {week} 周记\n\n"
        f"- 周期：{stats['window']['start_iso']} → {stats['window']['end_iso']}\n"
        f"- 对话轮次：{stats['turns']} · 消息：{stats['messages']}\n"
        f"- 心情均值：{stats['mood_avg']}\n\n"
        f"## 周记\n\n{weekly_text}\n\n## 自我叙事\n\n{narrative}\n",
        encoding="utf-8")
    return {"ok": True, "skipped": False, "week": week, "diary_path": str(weekly_path),
            "weekly": weekly_text, "narrative": narrative, "changed": changed,
            "llm": llm_used, "stats": stats, "archived_old": bool(old_text and changed)}


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="生成周记/自我叙事（P32）")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(weekly_review(db_path=Path(args.db), force=args.force),
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
