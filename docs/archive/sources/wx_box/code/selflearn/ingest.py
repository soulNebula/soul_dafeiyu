# -*- coding: utf-8 -*-
"""S0/S1/S2：JSONL 接入、清洗去重、会话切分与段切分。

只读写调用方给的 SQLite 和临时/授权 JSONL；不联网，不接触旧客户数据。
入库以 ``raw_events`` 为主，session/segment 都是由文本事件派生的可重建数据。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from . import db, pii

# kind 为这些值时，message 才算“文本消息”。其他 kind 原样保留，但不进
# session/segment，避免把系统提示、图片占位等当成聊天内容。
TEXT_KINDS = frozenset({"text", "message", "msg"})
_TEXT_KINDS_SQL = ",".join(f"'{kind}'" for kind in sorted(TEXT_KINDS))

_WS_RE = re.compile(r"[\s\u200b\u200c\u200d\ufeff]+")
_EVENT_FIELDS = frozenset({"id", "source_id", "friend", "sender", "kind", "text", "ts"})
_MAX_ERRORS = 100


def _clean(value: Any) -> str:
    """归一空白；None 视为空串。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return _WS_RE.sub(" ", value).strip()


def _id(prefix: str, *parts: Any) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part).encode("utf-8", "replace"))
        h.update(b"\x1e")
    return prefix + h.hexdigest()[:16]


def _meta_json(raw: dict) -> str:
    extra = {k: v for k, v in raw.items() if k not in _EVENT_FIELDS}
    try:
        return json.dumps(extra, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return "{}"


def clean_event(raw: dict) -> dict | None:
    """清洗一条原始事件；无法安全入库时返回 None。

    - friend/sender/kind 必填；id 缺失时用内容指纹兜底；
    - ts 缺失按契约落 0，非法 ts（非数字/NaN/负数）拒绝；
    - text kind 的空文本拒绝；非文本/系统事件允许空 text，保留 kind；
    - 未知字段收进 meta_json，不丢原始上下文。
    """
    if not isinstance(raw, dict):
        return None

    friend = _clean(raw.get("friend"))
    sender = _clean(raw.get("sender"))
    kind = _clean(raw.get("kind")).lower()
    if not friend or not sender or not kind:
        return None

    text, pii_hits = pii.scrub_pii(_clean(raw.get("text")))
    if kind in TEXT_KINDS and not text:
        return None

    ts_value = raw.get("ts", 0)
    if ts_value is None or (isinstance(ts_value, str) and not ts_value.strip()):
        ts = 0.0
    else:
        if isinstance(ts_value, bool):
            return None
        try:
            ts = float(ts_value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(ts) or ts < 0:
            return None

    raw_id = _clean(raw.get("id"))
    source_id = _clean(raw.get("source_id")) or raw_id
    event_id = raw_id or source_id or _id(
        "EV-", friend, sender, kind, text, repr(ts)
    )

    return {
        "id": event_id,
        "source_id": source_id,
        "friend": friend,
        "sender": sender,
        "kind": kind,
        "text": text,
        "ts": ts,
        "meta_json": _meta_json(raw),
    }


def _event_pk(source: str, event: dict) -> str:
    """数据库主键：同一 source 下同一来源 id 幂等。"""
    token = event.get("source_id") or event.get("id")
    if not token:
        token = _id("", event.get("friend"), event.get("sender"),
                    event.get("kind"), event.get("text"), event.get("ts"))
    return _id("RE-", source, token)


def ingest_jsonl(path: Path | str, jsonl_path: Path | str, *,
                 source: str = "jsonl") -> dict:
    """把 JSONL 事件幂等写入 raw_events。

    坏 JSON、空行、缺字段、空文本等分别计数；坏行不会中断整批导入。
    返回的 ``inserted`` 已去重，重复执行同一文件为 0。
    """
    src = _clean(source) or "jsonl"
    report: dict[str, Any] = {
        "ok": True,
        "source": src,
        "path": str(jsonl_path),
        "lines": 0,
        "blank": 0,
        "total": 0,
        "parsed": 0,
        "inserted": 0,
        "duplicates": 0,
        "rejected": 0,
        "invalid": 0,
        "errors": [],
    }

    con = db.connect(path)
    try:
        with Path(jsonl_path).open("r", encoding="utf-8-sig") as fh:
            for lineno, line in enumerate(fh, 1):
                report["lines"] += 1
                if not line.strip():
                    report["blank"] += 1
                    continue
                report["total"] += 1

                try:
                    raw = json.loads(line)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    report["rejected"] += 1
                    _append_error(report, lineno, f"bad json: {exc}")
                    continue
                report["parsed"] += 1

                event = clean_event(raw)
                if event is None:
                    report["rejected"] += 1
                    _append_error(report, lineno, "invalid event")
                    continue

                event_id = _event_pk(src, event)
                cur = con.execute(
                    "INSERT OR IGNORE INTO raw_events"
                    "(id,source,source_id,friend,sender,kind,text,ts,meta_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (event_id, src, event.get("source_id"), event["friend"],
                     event["sender"], event["kind"], event["text"],
                     event["ts"], event["meta_json"]),
                )
                if cur.rowcount:
                    report["inserted"] += 1
                else:
                    report["duplicates"] += 1
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()

    report["invalid"] = report["rejected"]
    report["ok"] = report["rejected"] == 0
    return report


def _append_error(report: dict, lineno: int, reason: str) -> None:
    if len(report["errors"]) < _MAX_ERRORS:
        report["errors"].append({"line": lineno, "reason": reason})
    elif len(report["errors"]) == _MAX_ERRORS:
        report["errors"].append(
            {"line": lineno, "reason": "too many errors; truncated"})


def _session_id(friend: str, start_ts: float) -> str:
    return _id("SE-", friend, repr(float(start_ts)))


def _seg_id(session_id: str, seq: int, start_ts: float, end_ts: float,
            text: str) -> str:
    return _id("SG-", session_id, seq, repr(float(start_ts)),
               repr(float(end_ts)), text)


def _delete_sessions(con, session_ids: list[str]) -> None:
    ids = list(dict.fromkeys(session_ids))
    for offset in range(0, len(ids), 400):
        chunk = ids[offset:offset + 400]
        marks = ",".join("?" for _ in chunk)
        con.execute(
            f"DELETE FROM session_segments WHERE session_id IN ({marks})", chunk)
        con.execute(f"DELETE FROM sessions WHERE id IN ({marks})", chunk)


def build_sessions(path: Path | str, *, friend: str | None = None,
                   gap_seconds: float = 1800) -> list[dict]:
    """按 friend + 时间间隔重建 sessions，并返回本次会话列表。

    只有 kind 为 text/message/msg 且 text 非空的事件进入会话；系统/媒体
    事件保留在 raw_events，但不计入 message_count。间隔严格大于
    ``gap_seconds`` 才切分。重复调用是幂等的，并会清理不再存在的旧会话。
    """
    try:
        gap = float(gap_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("gap_seconds 必须是数字") from exc
    if not math.isfinite(gap) or gap < 0:
        raise ValueError("gap_seconds 不能为负数")

    con = db.connect(path)
    try:
        sql = (
            "SELECT id,friend,sender,kind,text,ts FROM raw_events "
            f"WHERE LOWER(kind) IN ({_TEXT_KINDS_SQL}) AND TRIM(text)<>''"
        )
        params: list[Any] = []
        target_friend = _clean(friend) if friend is not None else None
        if target_friend is not None:
            sql += " AND friend=?"
            params.append(target_friend)
        sql += " ORDER BY friend,ts,id"
        rows = con.execute(sql, params).fetchall()

        sessions: list[dict] = []
        current: dict | None = None
        last_ts = 0.0
        for row in rows:
            row_friend = str(row["friend"])
            ts = float(row["ts"])
            if (current is None or row_friend != current["friend"]
                    or ts - last_ts > gap):
                current = {
                    "id": _session_id(row_friend, ts),
                    "friend": row_friend,
                    "start_ts": ts,
                    "end_ts": ts,
                    "message_count": 0,
                    "status": "active",
                }
                sessions.append(current)
            current["end_ts"] = ts
            current["message_count"] += 1
            last_ts = ts

        if target_friend is None:
            existing_rows = con.execute(
                "SELECT id,friend,start_ts,end_ts,message_count FROM sessions"
            ).fetchall()
        else:
            existing_rows = con.execute(
                "SELECT id,friend,start_ts,end_ts,message_count FROM sessions "
                "WHERE friend=?", (target_friend,)
            ).fetchall()
        existing = {str(row["id"]): row for row in existing_rows}
        new_ids = {s["id"] for s in sessions}

        for session in sessions:
            old = existing.get(session["id"])
            if old is not None and (
                str(old["friend"]) != session["friend"]
                or float(old["start_ts"]) != session["start_ts"]
                or float(old["end_ts"]) != session["end_ts"]
                or int(old["message_count"]) != session["message_count"]
            ):
                con.execute("DELETE FROM session_segments WHERE session_id=?",
                            (session["id"],))
            con.execute(
                "INSERT INTO sessions(id,friend,start_ts,end_ts,message_count,status) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "friend=excluded.friend,start_ts=excluded.start_ts,"
                "end_ts=excluded.end_ts,message_count=excluded.message_count,"
                "status=excluded.status",
                (session["id"], session["friend"], session["start_ts"],
                 session["end_ts"], session["message_count"], session["status"]),
            )

        _delete_sessions(con, [sid for sid in existing if sid not in new_ids])
        con.commit()
        return sessions
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def _split_text(text: str, limit: int) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def _new_segment(session_id: str, seq: int, start_ts: float, end_ts: float,
                 text: str, message_ids: list[str]) -> dict:
    meta = {"message_ids": list(message_ids), "message_count": len(message_ids)}
    return {
        "id": _seg_id(session_id, seq, start_ts, end_ts, text),
        "session_id": session_id,
        "seq": seq,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "text": text,
        "meta_json": json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
    }


def segment_session(path: Path | str, session_id: str, *,
                    max_chars: int = 1200) -> list[dict]:
    """把一个 session 的文本消息按 ``max_chars`` 切成可重建的分段。

    段间用换行连接消息；单条超长消息会硬切，保证任何段都不超过上限。
    段保留起止时间。重复调用会先清掉该 session 的旧段，结果幂等。
    """
    try:
        limit = int(max_chars)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("max_chars 必须是正整数") from exc
    if limit <= 0:
        raise ValueError("max_chars 必须是正整数")

    sid = _clean(session_id)
    if not sid:
        return []

    con = db.connect(path)
    try:
        session = con.execute(
            "SELECT * FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        if session is None:
            return []

        rows = con.execute(
            "SELECT id,friend,sender,kind,text,ts FROM raw_events "
            f"WHERE LOWER(kind) IN ({_TEXT_KINDS_SQL}) "
            "AND TRIM(text)<>'' AND friend=? AND ts>=? AND ts<=? "
            "ORDER BY ts,id",
            (session["friend"], session["start_ts"], session["end_ts"]),
        ).fetchall()

        segments: list[dict] = []
        parts: list[str] = []
        part_ids: list[str] = []
        part_len = 0
        part_start = 0.0
        part_end = 0.0

        def flush() -> None:
            nonlocal parts, part_ids, part_len, part_start, part_end
            if not parts:
                return
            segments.append(_new_segment(
                sid, len(segments), part_start, part_end,
                "\n".join(parts), part_ids,
            ))
            parts, part_ids, part_len = [], [], 0

        for row in rows:
            text = _clean(row["text"])
            if not text:
                continue
            ts = float(row["ts"])
            message_id = str(row["id"])

            if len(text) > limit:
                flush()
                chunks = _split_text(text, limit)
                for chunk in chunks[:-1]:
                    segments.append(_new_segment(
                        sid, len(segments), ts, ts, chunk, [message_id]))
                parts = [chunks[-1]]
                part_ids = [message_id]
                part_len = len(chunks[-1])
                part_start = ts
                part_end = ts
                continue

            candidate = part_len + (1 if parts else 0) + len(text)
            if parts and candidate > limit:
                flush()
            if not parts:
                part_start = ts
                part_ids = []
            parts.append(text)
            part_len = part_len + (1 if part_len else 0) + len(text)
            if message_id not in part_ids:
                part_ids.append(message_id)
            part_end = ts

        flush()

        con.execute("DELETE FROM session_segments WHERE session_id=?", (sid,))
        for seg in segments:
            con.execute(
                "INSERT INTO session_segments"
                "(id,session_id,seq,start_ts,end_ts,text,meta_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (seg["id"], seg["session_id"], seg["seq"], seg["start_ts"],
                 seg["end_ts"], seg["text"], seg["meta_json"]),
            )
        con.commit()
        return segments
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def stats(path: Path | str) -> dict:
    """返回接入、切分相关计数，全部来自当前 SQLite。"""
    con = db.connect(path)
    try:
        total = int(con.execute("SELECT count(*) FROM raw_events").fetchone()[0])
        text_events = int(con.execute(
            "SELECT count(*) FROM raw_events "
            f"WHERE LOWER(kind) IN ({_TEXT_KINDS_SQL}) AND TRIM(text)<>''"
        ).fetchone()[0])
        session_count = int(con.execute("SELECT count(*) FROM sessions").fetchone()[0])
        segment_count = int(con.execute(
            "SELECT count(*) FROM session_segments").fetchone()[0])
        friend_count = int(con.execute(
            "SELECT count(DISTINCT friend) FROM raw_events "
            "WHERE friend IS NOT NULL AND TRIM(friend)<>''"
        ).fetchone()[0])
        chars = int(con.execute(
            "SELECT COALESCE(sum(length(text)),0) FROM raw_events "
            f"WHERE LOWER(kind) IN ({_TEXT_KINDS_SQL}) AND TRIM(text)<>''"
        ).fetchone()[0])
        bounds = con.execute(
            "SELECT MIN(ts),MAX(ts) FROM raw_events").fetchone()
        by_kind = {
            str(row["kind"]): int(row["n"]) for row in con.execute(
                "SELECT kind,count(*) AS n FROM raw_events GROUP BY kind ORDER BY kind"
            )
        }
        by_source = {
            str(row["source"]): int(row["n"]) for row in con.execute(
                "SELECT source,count(*) AS n FROM raw_events "
                "GROUP BY source ORDER BY source"
            )
        }
    finally:
        con.close()

    return {
        "ok": True,
        "raw_events": total,
        "events": total,
        "total_events": total,
        "text_events": text_events,
        "non_text_events": total - text_events,
        "friends": friend_count,
        "sessions": session_count,
        "segments": segment_count,
        "session_segments": segment_count,
        "chars": chars,
        "characters": chars,
        "first_ts": None if bounds[0] is None else float(bounds[0]),
        "last_ts": None if bounds[1] is None else float(bounds[1]),
        "by_kind": by_kind,
        "by_source": by_source,
    }
