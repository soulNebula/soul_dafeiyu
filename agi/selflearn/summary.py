# -*- coding: utf-8 -*-
"""04/02 阶段摘要：把多轮对话压缩成可复用的长期经历。

P1-c 月度 gist（2026-10-25）：
- 90 天～1 年区间不再堆逐日摘要，而是每月一份要点（≤12 行 / ≤400 字）；
- **红线：只从该月原始 messages 重建，绝不读 summaries 表（禁止摘要的摘要）**；
- 当月高重要稳定事实（value/preference/plan & importance≥0.6）原样进「确定的事」段；
- 幂等：同一 year_month 重复生成用 UPDATE 覆盖同一行。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import db, memory
from .config import DB_PATH
from .llm import parse_json

#: 月度 gist 常量
MONTHLY_MAX_LINES = 12
MONTHLY_MAX_CHARS = 400
MONTHLY_BATCH_MESSAGES = 120      # 每批最多 120 条（默认，可被参数覆盖）
MONTHLY_BATCH_MAX_CHARS = 6000    # 每批原文最多约 6000 字（远低于 6000 token）
MONTHLY_STABLE_KINDS = ("value", "preference", "plan")
MONTHLY_STABLE_MIN_IMPORTANCE = 0.6
MONTHLY_MAX_STABLE = 8
MONTHLY_MAX_BACKFILL = 3          # nightly 最多补最近 3 个月


def _id(*parts) -> str:
    return "SUM-" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def _ensure_monthly_schema(con) -> None:
    """懒迁移：summaries 增加 kind/year_month/source（旧库自动 ALTER，幂等）。"""
    cols = {r[1] for r in con.execute("PRAGMA table_info(summaries)").fetchall()}
    for col, ddl in (("kind", "kind TEXT NOT NULL DEFAULT 'stage'"),
                     ("year_month", "year_month TEXT NOT NULL DEFAULT ''"),
                     ("source", "source TEXT NOT NULL DEFAULT 'llm'")):
        if col not in cols:
            con.execute(f"ALTER TABLE summaries ADD COLUMN {ddl}")


def latest(path: Path | str = DB_PATH) -> dict | None:
    con = db.connect(path)
    try:
        row = con.execute(
            "SELECT * FROM summaries WHERE kind<>'monthly' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is None:  # 只有月度 gist 时退回最新一条，保持 cli 可用
            row = con.execute(
                "SELECT * FROM summaries ORDER BY created_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def context(path: Path | str = DB_PATH, max_chars: int = 900,
            include_months: int = 2) -> str:
    """最新阶段摘要优先，其次附最近 N 份月度 gist（更早以前的要点），总长 ≤ max_chars。"""
    con = db.connect(path)
    try:
        stage = con.execute(
            "SELECT * FROM summaries WHERE kind<>'monthly' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        months = [dict(r) for r in con.execute(
            "SELECT * FROM summaries WHERE kind='monthly' "
            "ORDER BY year_month DESC, created_at DESC LIMIT ?",
            (max(0, int(include_months)),)).fetchall()]
    finally:
        con.close()
    parts = []
    if stage is not None:
        parts.append(str(stage["content"] or ""))
    if months:
        block = "\n\n更早以前的要点：\n" + "\n\n".join(
            str(m["content"] or "") for m in months)
        parts.append(block)
    text = "".join(parts)
    return text[:max(0, int(max_chars))]


def build(path: Path | str = DB_PATH, client=None, min_turns: int = 6,
          force: bool = False) -> dict[str, Any]:
    if client is None or not getattr(client, "available", lambda: False)():
        return {"ok": False, "error": "LLM 不可用"}
    con = db.connect(path)
    try:
        turns = con.execute("SELECT count(*) FROM turns").fetchone()[0]
        last = con.execute("SELECT max(created_at) FROM summaries").fetchone()[0]
        msgs = [dict(r) for r in con.execute(
            "SELECT role,text,ts FROM messages WHERE ts>? ORDER BY ts ASC LIMIT 40",
            (float(last or 0),)).fetchall()]
    finally:
        con.close()
    if not force and (int(turns) < min_turns or len(msgs) < 4):
        return {"ok": True, "created": False, "reason": "对话样本不足"}
    if not msgs:
        return {"ok": True, "created": False, "reason": "没有新消息"}
    dialog = "\n".join(f"{'用户' if m['role'] == 'user' else 'AI'}：{m['text']}"
                       for m in msgs[-30:])
    prompt = (
        "把下面这段长期对话压缩成一段阶段摘要，供以后的自己使用。"
        "只保留：用户明确说过的事实/偏好、关系进展、未解决的话题和承诺。"
        "不要编造，不要评价，不要写建议。只输出 JSON："
        '{"summary":"不超过 300 字","open_threads":["未解决话题，最多3条"]}\n\n' + dialog
    )
    result = client.chat(
        [{"role": "system", "content": "你是长期记忆压缩器。"},
         {"role": "user", "content": prompt}],
        purpose="selflearn_summary", max_tokens=700, temperature=0.2, json_mode=True,
    )
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "LLM 失败"}
    obj = parse_json(str(result.get("text") or ""))
    content = str(obj.get("summary") or "").strip() if isinstance(obj, dict) else ""
    if not content:
        return {"ok": False, "error": "摘要为空"}
    threads = obj.get("open_threads") if isinstance(obj, dict) else []
    sid = _id(content, time.time())
    con = db.connect(path)
    try:
        con.execute("INSERT INTO summaries(id,content,evidence_json,turns_count,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (sid, content[:800], json.dumps({"open_threads": threads or []},
                                                     ensure_ascii=False), int(turns), time.time()))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "created": True, "id": sid, "summary": content,
            "open_threads": threads or []}


# --------------------------------------------------------------------------- #
# P1-c：月度 gist（90 天～1 年；只从原始 messages 重建，禁止摘要的摘要）
# --------------------------------------------------------------------------- #
def _month_bounds(year_month: str) -> tuple[float, float, str]:
    """'YYYY-MM' → (月初 ts, 下月初 ts, 中文标签)。"""
    text = str(year_month or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
    if not m:
        raise ValueError(f"非法月份：{year_month!r}（应为 YYYY-MM）")
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        raise ValueError(f"非法月份：{year_month!r}")
    start = datetime.datetime(year, month, 1).timestamp()
    if month == 12:
        end = datetime.datetime(year + 1, 1, 1).timestamp()
    else:
        end = datetime.datetime(year, month + 1, 1).timestamp()
    return start, end, f"{year}年{month}月"


def _prev_months(now: float, count: int = MONTHLY_MAX_BACKFILL) -> list[str]:
    """当前月之前的 count 个月（最近的在前）。"""
    dt = datetime.datetime.fromtimestamp(float(now))
    year, month = dt.year, dt.month
    out: list[str] = []
    for _ in range(max(0, int(count))):
        month -= 1
        if month == 0:
            year -= 1
            month = 12
        out.append(f"{year:04d}-{month:02d}")
    return out


_STOP_BIGRAMS = frozenset({
    "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "就是", "可以", "没有",
    "不是", "一个", "现在", "今天", "明天", "昨天", "时候", "自己", "觉得", "知道",
    "这样", "还是", "然后", "因为", "所以", "但是", "如果", "已经", "真的", "有点",
    "东西", "事情", "一些", "这种", "那种", "的话", "起来", "下来", "出来", "过来",
})


def _rule_points(label: str, messages: list[dict]) -> list[str]:
    """规则式要点：月份 + 条数 + 高频主题词（client 不可用/失败时兜底）。"""
    counter: Counter[str] = Counter()
    for msg in messages:
        if str(msg.get("role") or "") != "user":
            continue
        text = re.sub(r"[\s\W_]+", "", str(msg.get("text") or ""))
        for i in range(len(text) - 1):
            gram = text[i:i + 2]
            if gram in _STOP_BIGRAMS:
                continue
            counter[gram] += 1
    topics = [gram for gram, _ in counter.most_common(5)]
    points = [f"共 {len(messages)} 条消息"]
    if topics:
        points.append("高频主题：" + "、".join(topics))
    return points


def _stable_facts(con, start: float, end: float) -> list[str]:
    """当月高重要稳定事实（value/preference/plan & importance≥0.6），原样保留。"""
    try:
        rows = con.execute(
            "SELECT content, kind, importance FROM user_memories WHERE status='active' "
            "AND kind IN ({}) AND importance>=? AND created_at>=? AND created_at<? "
            "ORDER BY importance DESC, created_at ASC LIMIT 20".format(
                ",".join("?" for _ in MONTHLY_STABLE_KINDS)),
            (*MONTHLY_STABLE_KINDS, MONTHLY_STABLE_MIN_IMPORTANCE, start, end)).fetchall()
    except Exception:  # noqa: BLE001 - 旧库缺列时不阻断
        return []
    out: list[str] = []
    for row in rows:
        content = str(row["content"] or "").strip()
        if content and content not in out:
            out.append(content)
    return out[:MONTHLY_MAX_STABLE]


def _compose_gist(label: str, points: list[str], stable: list[str]) -> str:
    """月份标题 + 要点 + 「确定的事」（稳定事实原样，不改写）。"""
    clean_points = []
    for raw in points or []:
        point = re.sub(r"\s+", " ", str(raw or "")).strip().lstrip("-•· ").strip()
        if point and point not in clean_points:
            clean_points.append(point)
    clean_points = clean_points[:MONTHLY_MAX_LINES]
    stable_lines: list[str] = []
    for raw in stable or []:
        content = str(raw or "").strip()
        if content and content not in stable_lines:
            stable_lines.append(content)
    stable_lines = stable_lines[:MONTHLY_MAX_STABLE]

    head = f"【{label}要点】"
    stable_block = ""
    if stable_lines:
        stable_block = "\n【确定的事】\n" + "\n".join(f"- {line}" for line in stable_lines)
    budget = MONTHLY_MAX_CHARS - len(head) - len(stable_block)
    body_lines: list[str] = []
    for point in clean_points:
        line = f"- {point}"
        candidate = "\n".join(body_lines + [line])
        if len(candidate) > budget:
            break
        body_lines.append(line)
    content = head + ("\n" + "\n".join(body_lines) if body_lines else "") + stable_block
    if len(content) > MONTHLY_MAX_CHARS and not stable_lines:
        content = content[:MONTHLY_MAX_CHARS]
    return content


def _llm_points(client, prompt: str, *, purpose: str, max_tokens: int = 700) -> list[str] | None:
    """调用一次 LLM 取要点列表；失败重试 1 次，两次都失败返回 None。"""
    for _ in range(2):
        try:
            result = client.chat(
                [{"role": "system", "content": "你是长期记忆压缩器，只输出 JSON。"},
                 {"role": "user", "content": prompt}],
                purpose=purpose, max_tokens=max_tokens, temperature=0.2, json_mode=True)
        except Exception:  # noqa: BLE001
            result = None
        if not isinstance(result, dict) or not result.get("ok"):
            continue
        obj = parse_json(str(result.get("text") or ""))
        points = None
        if isinstance(obj, dict):
            points = obj.get("points") or obj.get("items") or obj.get("gist")
        elif isinstance(obj, list):
            points = obj
        if isinstance(points, str):
            points = [points]
        if isinstance(points, list):
            clean = [str(x).strip() for x in points if str(x).strip()]
            if clean:
                return clean
    return None


def _batches(messages: list[dict], max_messages: int) -> list[list[dict]]:
    """按条数 + 字符预算切批（每批约 ≤6000 字，远低于 6000 token）。"""
    batches: list[list[dict]] = []
    current: list[dict] = []
    chars = 0
    for msg in messages:
        cost = len(str(msg.get("text") or "")) + 8
        if current and (len(current) >= max(1, int(max_messages))
                        or chars + cost > MONTHLY_BATCH_MAX_CHARS):
            batches.append(current)
            current, chars = [], 0
        current.append(msg)
        chars += cost
    if current:
        batches.append(current)
    return batches


def build_monthly(path: Path | str, year_month: str, client=None, *,
                  max_messages_per_round: int = MONTHLY_BATCH_MESSAGES,
                  now: float | None = None) -> dict[str, Any]:
    """生成某个月的 gist（只读原始 messages，不读 summaries → 不是摘要的摘要）。

    - 该月 messages 分批总结（每批 ≤ max_messages_per_round 条 / ≤6000 字）；
    - 再合并成 ≤12 行 / ≤400 字的月 gist；
    - 当月高重要稳定事实原样进「确定的事」段；
    - 幂等：同 year_month 重复生成 UPDATE 同一行，不新增。
    """
    ts = time.time() if now is None else float(now)
    start, end, label = _month_bounds(year_month)
    con = db.connect(path)
    try:
        messages = [dict(r) for r in con.execute(
            "SELECT id, role, text, ts FROM messages WHERE ts>=? AND ts<? "
            "ORDER BY ts ASC, id ASC", (start, end)).fetchall()]
        stable = _stable_facts(con, start, end)
    finally:
        con.close()

    result: dict[str, Any] = {
        "ok": True, "created": False, "year_month": year_month, "label": label,
        "messages": len(messages), "batches": 0, "llm_calls": 0,
        "stable_facts": stable, "source": "rule",
    }
    if not messages:
        result["reason"] = "no_messages"
        return result

    points: list[str] = []
    source = "rule"
    llm_calls = 0
    batches = _batches(messages, max_messages_per_round) if messages else []
    available = client is not None and getattr(client, "available", lambda: False)()
    if available:
        collected: list[str] = []
        failed = False
        for batch in batches:
            dialog = "\n".join(
                f"{'用户' if m.get('role') == 'user' else 'AI'}：{str(m.get('text') or '')[:300]}"
                for m in batch)
            prompt = (
                "下面是某个月的**原始对话**片段。只提取：用户明确说过的事实/偏好/计划、"
                "关系进展、未解决的话题。不要评价、不要建议、不要编造。"
                '只输出 JSON：{"points":["一句要点", ...]}（最多 8 条）。\n\n' + dialog)
            got = _llm_points(client, prompt, purpose="selflearn_monthly_batch")
            llm_calls += 1
            if got is None:
                failed = True
                break
            collected.extend(got)
        if not failed and collected:
            merge_prompt = (
                "把下面这些要点合并成最多 12 行、每行不超过 30 字的月度要点；"
                "去重、保留具体事实，不要新增内容。只输出 JSON：{\"points\":[...]}。\n\n"
                + "\n".join(f"- {p}" for p in collected[:60]))
            merged = _llm_points(client, merge_prompt,
                                 purpose="selflearn_monthly_merge", max_tokens=600)
            llm_calls += 1
            points = merged or collected
            source = "llm"
    if source != "llm" or not points:
        points = _rule_points(label, messages)
        source = "rule"
        llm_calls = 0 if not available else llm_calls

    content = _compose_gist(label, points, stable)
    evidence = json.dumps({
        "type": "monthly", "year_month": year_month, "source": source,
        "messages": len(messages), "batches": len(batches),
        "stable_facts": stable,
    }, ensure_ascii=False)

    con = db.connect(path)
    try:
        existing = con.execute(
            "SELECT id FROM summaries WHERE kind='monthly' AND year_month=? "
            "ORDER BY created_at DESC LIMIT 1", (year_month,)).fetchone()
        if existing is not None:
            sid = str(existing["id"])
            con.execute(
                "UPDATE summaries SET content=?, evidence_json=?, turns_count=?, "
                "source=?, created_at=? WHERE id=?",
                (content, evidence, len(messages), source, ts, sid))
            created = False
        else:
            sid = _id("monthly", year_month)
            con.execute(
                "INSERT OR REPLACE INTO summaries(id,content,evidence_json,turns_count,"
                "created_at,kind,year_month,source) VALUES(?,?,?,?,?,?,?,?)",
                (sid, content, evidence, len(messages), ts, "monthly", year_month, source))
            created = True
        con.commit()
    finally:
        con.close()
    result.update({"created": created, "id": sid, "content": content,
                   "source": source, "batches": len(batches), "llm_calls": llm_calls})
    return result


def latest_monthly_month(path: Path | str = DB_PATH) -> str:
    con = db.connect(path)
    try:
        row = con.execute(
            "SELECT max(year_month) AS ym FROM summaries WHERE kind='monthly'").fetchone()
        return str(row["ym"] or "") if row is not None else ""
    finally:
        con.close()


def maintenance_monthly(path: Path | str, client=None, *, now: float | None = None,
                        max_backfill: int = MONTHLY_MAX_BACKFILL) -> dict[str, Any]:
    """nightly 调度：只补「上次生成月」之后、最近 max_backfill 个月内的 gist。

    每月最多一次：上次生成月 == 当前月-1 时什么都不做。
    """
    ts = time.time() if now is None else float(now)
    last = latest_monthly_month(path)
    candidates = _prev_months(ts, max_backfill)       # 最近 3 个月，最近的在前
    targets = sorted(m for m in candidates if m > last)  # 缺的月份，从旧到新
    targets = targets[:max(0, int(max_backfill))]
    generated: list[dict] = []
    skipped: list[str] = []
    for month in targets:
        try:
            info = build_monthly(path, month, client, now=ts)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{month}: {type(exc).__name__}: {exc}")
            continue
        if info.get("created"):
            generated.append(info)
        else:
            skipped.append(f"{month}: {info.get('reason') or 'unchanged'}")
    return {"ok": True, "last_month": last, "targets": targets,
            "generated": generated, "skipped": skipped,
            "created": len(generated)}

