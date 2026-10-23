# -*- coding: utf-8 -*-
"""S06 Learn Loop：缺口 → 拆解 → 检索来源 → 制卡 → 练习 → verifier → 解锁 L3。

编排一次“从不会到会”的学习闭环，只读写既有表（不改 schema）：
``learn_tasks`` / ``skill_cards`` / ``practice_runs`` / ``skill_nodes``。

硬规则（BATCH2_CONTRACT.md）：
- 没有 provenance 的卡不能写入：``write_card`` 只接受已 ``record_source``
  的来源，来源快照写进 ``skill_cards.provenance_json``；
- provenance 同时存 ``skill_nodes.provenance`` JSON（每个 skill 一份来源清单，
  ``record_source`` 追加、重复来源幂等）；
- 没有 verifier 的练习不能升级：升级只能走 ``verify_and_unlock``；
- 至少 :data:`MIN_PRACTICES` 次练习，且最后一次或累计通过，才允许 L3；
- 只升不降：等级只通过 :func:`agi.selflearn.skilltree.unlock` 修改，
  本模块从不直接写 ``skill_nodes.level``；
- 练习/验证失败记 ``learn_tasks.last_error`` 与 ``skill_nodes.fail_count``。

实现取舍（契约没写死、这里显式约定）：
- 失败一律返回 ``{"ok": False, "error": ..., "code": ...}``，不抛异常、不写脏数据；
- ``open_gap`` 返回空串表示入参非法（签名要求返回 str）；
- 写来源/制卡时若技能节点不存在，按 L0/locked 注册节点，但绝不改等级；
- verifier 结果不写 ``practice_runs``，避免验证次数被算成练习次数；
  验证结果追加到 ``skill_nodes.practice_log``（``kind="verify"``）留审计；
- ``status`` 的 ``can_unlock`` 表示“现在调 verifier 且通过就会解锁”，
  已经 L3 的技能为 False（该拿的已经拿到）。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from . import db, skilltree

#: 解锁 L3 前最少的练习次数。
MIN_PRACTICES = 3
#: 本 Loop 的目标等级。
UNLOCK_LEVEL = 3
#: 技能树最高等级，仅用于展示判断。
MAX_LEVEL = 5
#: 单条错误信息落库上限。
MAX_ERROR_CHARS = 2000
#: practice_log 保留的最近条目数（practice_runs 才是全量事实）。
PRACTICE_LOG_LIMIT = 200

_TRUE_WORDS = frozenset(
    {"1", "1.0", "true", "yes", "y", "ok", "pass", "passed", "通过", "对", "是"}
)
_ID_SPLIT = re.compile(r"[,;，；\s]+")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _clean(value) -> str:
    return str(value if value is not None else "").strip()


def _as_bool(value) -> bool:
    """把 bool/0-1/常见字符串统一成 bool；无法识别按 False（fail-closed）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == value and bool(value)  # NaN → False
    return str(value or "").strip().lower() in _TRUE_WORDS


def _new_id(prefix: str, *parts) -> str:
    seed = "|".join(str(part) for part in parts) + "|" + str(time.time_ns())
    return prefix + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:14]


def _fail(error: str, code: str = "error", **extra) -> dict:
    out = {"ok": False, "error": str(error), "code": code}
    out.update(extra)
    return out


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_list(raw) -> list:
    if isinstance(raw, list):
        return list(raw)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _stamp(value):
    """fetched_at 原样保留；没给就记当前时间。"""
    if value is None:
        return _now()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = _clean(value)
    return text or _now()


def _is_http_url(value: str) -> bool:
    if not value or any(ch.isspace() for ch in value):
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def _id_list(value) -> list[str] | None:
    """把 provenance_ids 归一成 id 列表；None 表示“用该 skill 的全部来源”。"""
    if value is None:
        return None
    raw_items = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    out: list[str] = []
    for item in raw_items:
        if isinstance(item, dict):
            item = item.get("id")
        for part in _ID_SPLIT.split(str(item if item is not None else "")):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def _ensure_skill(path: Path | str, skill_id: str) -> dict:
    """确保技能节点存在；不存在就注册为 L0/locked（不碰已有等级）。"""
    skilltree.ensure_nodes(path)
    con = db.connect(path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO skill_nodes"
            "(id,name,level,status,provenance,practice_log,fail_count,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (skill_id, skill_id, 0, "locked", "[]", "[]", 0, _now()),
        )
        con.commit()
        row = con.execute("SELECT * FROM skill_nodes WHERE id=?", (skill_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def _task(path: Path | str, task_id: str) -> dict | None:
    con = db.connect(path)
    try:
        row = con.execute("SELECT * FROM learn_tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _update_task(path: Path | str, task_id: str, **fields) -> None:
    allowed = {"status", "last_error", "attempts"}
    items = [(key, value) for key, value in fields.items() if key in allowed]
    if not items:
        return
    sql = "UPDATE learn_tasks SET " + ",".join(f"{key}=?" for key, _ in items) + " WHERE id=?"
    con = db.connect(path)
    try:
        con.execute(sql, [value for _, value in items] + [task_id])
        con.commit()
    finally:
        con.close()


def _practice_stats(path: Path | str, skill_id: str) -> dict:
    """按 skill 汇总练习事实（跨该 skill 的所有 task）。"""
    con = db.connect(path)
    try:
        row = con.execute(
            "SELECT count(*) AS total, coalesce(sum(p.passed),0) AS passed "
            "FROM practice_runs p JOIN learn_tasks t ON p.task_id=t.id "
            "WHERE t.skill_id=?",
            (skill_id,),
        ).fetchone()
        last = con.execute(
            "SELECT p.* FROM practice_runs p JOIN learn_tasks t ON p.task_id=t.id "
            "WHERE t.skill_id=? ORDER BY p.created_at DESC, p.rowid DESC LIMIT 1",
            (skill_id,),
        ).fetchone()
    finally:
        con.close()
    total = int(row["total"] or 0) if row else 0
    passed = int(row["passed"] or 0) if row else 0
    last_row = dict(last) if last else None
    if last_row is not None:
        last_row["passed"] = int(last_row.get("passed") or 0)
    return {
        "count": total,
        "passed": passed,
        "failed": total - passed,
        "last": last_row,
        "last_passed": bool(last_row["passed"]) if last_row else None,
    }


def _task_list(path: Path | str, skill_id: str) -> list[dict]:
    con = db.connect(path)
    try:
        counts = {
            str(row["task_id"]): (int(row["n"]), int(row["passed"]))
            for row in con.execute(
                "SELECT task_id, count(*) AS n, coalesce(sum(passed),0) AS passed "
                "FROM practice_runs GROUP BY task_id"
            ).fetchall()
        }
        rows = con.execute(
            "SELECT * FROM learn_tasks WHERE skill_id=? ORDER BY created_at ASC, id ASC",
            (skill_id,),
        ).fetchall()
    finally:
        con.close()
    out = []
    for row in rows:
        item = dict(row)
        total, passed = counts.get(str(item["id"]), (0, 0))
        item["practices"] = total
        item["passed_practices"] = passed
        out.append(item)
    return out


def _append_log(
    path: Path | str, skill_id: str, entry: dict, fail_increment: int = 0
) -> dict:
    """追加 practice_log 审计条目；需要时同时累加 fail_count。"""
    node = _ensure_skill(path, skill_id)
    log = _json_list(node.get("practice_log"))
    log.append(entry)
    if len(log) > PRACTICE_LOG_LIMIT:
        log = log[-PRACTICE_LOG_LIMIT:]
    fail_count = int(node.get("fail_count") or 0) + max(0, int(fail_increment))
    con = db.connect(path)
    try:
        con.execute(
            "UPDATE skill_nodes SET practice_log=?, fail_count=?, updated_at=? WHERE id=?",
            (_dumps(log), fail_count, _now(), skill_id),
        )
        con.commit()
    finally:
        con.close()
    return {"practice_log": log, "fail_count": fail_count}


def _load_sources(node: dict, skill_id: str) -> list[dict]:
    """读 skill_nodes.provenance；给早期没有 id 的条目补一个确定性 id。"""
    out: list[dict] = []
    for item in _json_list((node or {}).get("provenance")):
        if not isinstance(item, dict):
            continue
        source = dict(item)
        if not source.get("id"):
            seed = f"{skill_id}|{source.get('url', '')}|{source.get('quote', '')}"
            source["id"] = "SRC-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:14]
        out.append(source)
    return out


def _write_sources(path: Path | str, skill_id: str, sources: list[dict]) -> None:
    con = db.connect(path)
    try:
        con.execute(
            "UPDATE skill_nodes SET provenance=?, updated_at=? WHERE id=?",
            (_dumps(sources), _now(), skill_id),
        )
        con.commit()
    finally:
        con.close()


def _cards(path: Path | str, skill_id: str) -> list[dict]:
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT * FROM skill_cards WHERE skill_id=? ORDER BY created_at ASC, id ASC",
            (skill_id,),
        ).fetchall()
    finally:
        con.close()
    out = []
    for row in rows:
        item = dict(row)
        item["provenance"] = _json_list(item.get("provenance_json"))
        item["provenance_ids"] = [
            source.get("id")
            for source in item["provenance"]
            if isinstance(source, dict)
        ]
        out.append(item)
    return out


def _practice_ready(stats: dict) -> bool:
    return stats["count"] >= MIN_PRACTICES and bool(
        stats["last_passed"] or stats["passed"] >= MIN_PRACTICES
    )


def _missing_reasons(stats: dict, verified: bool) -> list[str]:
    missing = []
    if stats["count"] < MIN_PRACTICES:
        missing.append(f"practice {stats['count']}/{MIN_PRACTICES}")
    elif not _practice_ready(stats):
        missing.append("最后一次或累计练习未通过")
    if not verified:
        missing.append("verifier")
    return missing


# ---------------------------------------------------------------------------
# 八步编排
# ---------------------------------------------------------------------------


def open_gap(path: Path | str, skill_id: str, goal: str) -> str:
    """记录一个技能缺口，返回 learn_task id；skill_id 为空时返回空串。

    id 由 :func:`_new_id` 生成（带纳秒时间），同一缺口连续打开也不会撞主键；
    这里只写 ``learn_tasks``，不碰 ``skill_nodes.level``。
    """
    sid = _clean(skill_id)
    if not sid:
        return ""
    node = _ensure_skill(path, sid)
    text = _clean(goal) or f"掌握技能 {node.get('name') or sid}"
    task_id = _new_id("LT-", sid, text)
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO learn_tasks(id,skill_id,goal,status,attempts,last_error,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (task_id, sid, text[:500], "open", 0, "", _now()),
        )
        con.commit()
    finally:
        con.close()
    return task_id


def record_source(
    path: Path | str,
    skill_id: str,
    url: str,
    title: str,
    quote: str,
    fetched_at=None,
) -> dict:
    """把一个真实来源追加到 ``skill_nodes.provenance``（重复来源幂等）。"""
    sid = _clean(skill_id)
    if not sid:
        return _fail("skill_id 不能为空", code="bad_skill")
    link = _clean(url)
    if not _is_http_url(link):
        return _fail("来源 URL 必须是 http(s) 绝对地址", code="bad_url", url=link)
    text = _clean(quote)
    if not text:
        return _fail("来源缺少原文引用 quote，不能作为 provenance", code="missing_quote")

    node = _ensure_skill(path, sid)
    sources = _load_sources(node, sid)
    for existing in sources:
        if existing.get("url") == link and existing.get("quote") == text:
            out = {"ok": True, "duplicate": True}
            out.update(existing)
            return out

    entry = {
        "id": _new_id("SRC-", sid, link, text[:200]),
        "skill_id": sid,
        "url": link,
        "title": _clean(title) or link,
        "quote": text,
        "fetched_at": _stamp(fetched_at),
        "created_at": _now(),
    }
    sources.append(entry)
    _write_sources(path, sid, sources)
    out = {"ok": True, "duplicate": False}
    out.update(entry)
    return out


def write_card(
    path: Path | str,
    skill_id: str,
    content: str,
    example: str,
    provenance_ids=None,
) -> dict:
    """写一张有来源的卡；没有 provenance 一律拒绝。

    ``provenance_ids`` 为空列表视为“明确没有来源”而拒绝；传 None 表示使用该
    skill 已记录的全部来源；传了未知 id 也拒绝（不允许悬空引用）。
    """
    sid = _clean(skill_id)
    if not sid:
        return _fail("skill_id 不能为空", code="bad_skill")
    text = _clean(content)
    if not text:
        return _fail("卡片内容不能为空", code="empty_content")
    demo = _clean(example)

    node = _ensure_skill(path, sid)
    sources = _load_sources(node, sid)
    wanted = _id_list(provenance_ids)
    if wanted is None:
        chosen = sources
    else:
        if not wanted:
            return _fail("没有 provenance 的卡不能写入", code="missing_provenance")
        index = {str(s.get("id")): s for s in sources if s.get("id")}
        missing = [source_id for source_id in wanted if source_id not in index]
        if missing:
            return _fail(
                "provenance id 不存在：" + "、".join(missing),
                code="unknown_provenance",
                missing=missing,
            )
        chosen = [index[source_id] for source_id in wanted]
    if not chosen:
        return _fail(
            "没有 provenance 的卡不能写入：先 record_source",
            code="missing_provenance",
        )

    cid = _new_id("SC-", sid, text[:200])
    created = _now()
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO skill_cards(id,skill_id,content,example,provenance_json,"
            "status,created_at) VALUES(?,?,?,?,?,?,?)",
            (cid, sid, text, demo, _dumps(chosen), "active", created),
        )
        con.commit()
    finally:
        con.close()
    return {
        "ok": True,
        "id": cid,
        "skill_id": sid,
        "content": text,
        "example": demo,
        "provenance_ids": [source.get("id") for source in chosen],
        "provenance": chosen,
        "status": "active",
        "created_at": created,
    }


def record_practice(
    path: Path | str, task_id: str, attempt, passed, error: str = ""
) -> dict:
    """落一条练习事实：写 ``practice_runs``、更新 task 与 fail_count。"""
    tid = _clean(task_id)
    if not tid:
        return _fail("task_id 不能为空", code="bad_task")
    task = _task(path, tid)
    if task is None:
        return _fail("learn_task 不存在", code="unknown_task", task_id=tid)

    sid = _clean(task.get("skill_id"))
    ok = _as_bool(passed)
    err = _clean(error)[:MAX_ERROR_CHARS]
    if not ok and not err:
        err = "练习未通过"
    raw_attempt = attempt
    given_attempt = _clean(attempt) if not isinstance(attempt, (int, float)) else str(attempt)
    created = _now()

    con = db.connect(path)
    try:
        prior = int(
            con.execute(
                "SELECT count(*) FROM practice_runs WHERE task_id=?", (tid,)
            ).fetchone()[0]
        )
        stored_attempt = given_attempt or str(prior + 1)
        rid = _new_id("PR-", tid, stored_attempt, ok)
        con.execute(
            "INSERT INTO practice_runs(id,task_id,attempt,passed,error,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (rid, tid, stored_attempt, 1 if ok else 0, err, created),
        )
        attempts = prior + 1
        task_status = "passed" if ok else "practicing"
        last_error = "" if ok else err
        con.execute(
            "UPDATE learn_tasks SET attempts=?, status=?, last_error=? WHERE id=?",
            (attempts, task_status, last_error, tid),
        )
        con.commit()
    finally:
        con.close()

    node_state = _append_log(
        path,
        sid,
        {
            "id": rid,
            "kind": "practice",
            "task_id": tid,
            "attempt": stored_attempt,
            "passed": ok,
            "error": err,
            "created_at": created,
        },
        fail_increment=0 if ok else 1,
    )
    stats = _practice_stats(path, sid)
    return {
        "ok": True,
        "id": rid,
        "task_id": tid,
        "skill_id": sid,
        "attempt": raw_attempt if given_attempt else stored_attempt,
        "stored_attempt": stored_attempt,
        "passed": ok,
        "error": err,
        "attempts": attempts,
        "task_status": task_status,
        "last_error": last_error,
        "practices": stats["count"],
        "passed_practices": stats["passed"],
        "fail_count": node_state["fail_count"],
        "created_at": created,
    }


def verify_and_unlock(path: Path | str, task_id: str, passed, note: str = "") -> dict:
    """verifier 闸门：通过且练习次数/通过情况达标，才走 ``skilltree.unlock``。"""
    tid = _clean(task_id)
    if not tid:
        return _fail("task_id 不能为空", code="bad_task")
    task = _task(path, tid)
    if task is None:
        return _fail("learn_task 不存在", code="unknown_task", task_id=tid)

    sid = _clean(task.get("skill_id"))
    ok = _as_bool(passed)
    text = _clean(note)[:MAX_ERROR_CHARS]
    node = _ensure_skill(path, sid)
    stats = _practice_stats(path, sid)
    level = int(node.get("level") or 0)
    created = _now()
    base = {
        "task_id": tid,
        "skill_id": sid,
        "practices": stats["count"],
        "passed_practices": stats["passed"],
        "required": MIN_PRACTICES,
        "level": level,
        "verifier_passed": ok,
    }

    if not ok:
        reason = text or "verifier 未通过"
        _update_task(path, tid, status="practicing", last_error=reason)
        node_state = _append_log(
            path,
            sid,
            {
                "id": _new_id("VR-", tid, "fail"),
                "kind": "verify",
                "task_id": tid,
                "passed": False,
                "note": text,
                "error": reason,
                "created_at": created,
            },
            fail_increment=1,
        )
        return {
            **base,
            "ok": False,
            "unlocked": False,
            "reason": "verifier_failed",
            "error": reason,
            "fail_count": node_state["fail_count"],
        }

    ready = _practice_ready(stats)
    if not ready:
        if stats["count"] < MIN_PRACTICES:
            missing = [f"practice {stats['count']}/{MIN_PRACTICES}"]
        else:
            missing = ["最后一次或累计练习未通过"]
        reason = "验证通过但未满足解锁条件：" + "、".join(missing)
        _update_task(path, tid, status="practicing", last_error=reason)
        node_state = _append_log(
            path,
            sid,
            {
                "id": _new_id("VR-", tid, "gated"),
                "kind": "verify",
                "task_id": tid,
                "passed": True,
                "gated": True,
                "missing": missing,
                "note": text,
                "created_at": created,
            },
            fail_increment=0,
        )
        return {
            **base,
            "ok": False,
            "unlocked": False,
            "reason": "requirements_not_met",
            "error": reason,
            "missing": missing,
            "practice_ready": False,
            "fail_count": node_state["fail_count"],
        }

    # 只升不降：已经更高的等级不会被 L3 覆盖。
    target = max(level, UNLOCK_LEVEL)
    result = skilltree.unlock(path, sid, target)
    if not result.get("ok"):
        current = _ensure_skill(path, sid)
        return {
            **base,
            "ok": False,
            "unlocked": False,
            "reason": "unlock_failed",
            "error": "skilltree.unlock 未更新任何行",
            "fail_count": int(current.get("fail_count") or 0),
        }

    _update_task(path, tid, status="done", last_error="")
    unlocked_level = int(result.get("level") or target)
    node_state = _append_log(
        path,
        sid,
        {
            "id": _new_id("VR-", tid, "pass"),
            "kind": "verify",
            "task_id": tid,
            "passed": True,
            "level": unlocked_level,
            "note": text,
            "created_at": created,
        },
        fail_increment=0,
    )
    return {
        **base,
        "ok": True,
        "unlocked": True,
        "reason": "unlocked",
        "level": unlocked_level,
        "previous_level": level,
        "practice_ready": True,
        "fail_count": node_state["fail_count"],
        "note": text,
    }


def _summary(path: Path | str, node: dict) -> dict:
    sid = str(node.get("id") or "")
    sources = _load_sources(node, sid)
    cards = _cards(path, sid)
    tasks = _task_list(path, sid)
    stats = _practice_stats(path, sid)
    log = _json_list(node.get("practice_log"))

    last_verify = None
    last_verify_index = -1
    last_practice_index = -1
    for index, entry in enumerate(log):
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") == "verify":
            last_verify = entry
            last_verify_index = index
        else:
            last_practice_index = index
    verified = bool(
        last_verify
        and last_verify.get("passed")
        and last_verify_index > last_practice_index
    )
    practice_ready = _practice_ready(stats)
    level = int(node.get("level") or 0)
    last_error = ""
    for task in reversed(tasks):
        if task.get("last_error"):
            last_error = str(task["last_error"])
            break

    return {
        "skill_id": sid,
        "name": node.get("name") or sid,
        "level": level,
        "status": node.get("status") or "locked",
        "unlocked": level >= UNLOCK_LEVEL,
        "fail_count": int(node.get("fail_count") or 0),
        "last_error": last_error,
        "sources": sources,
        "source_count": len(sources),
        "cards": cards,
        "card_count": len(cards),
        "practice": stats,
        "practice_ready": practice_ready,
        "verified": verified,
        "can_unlock": practice_ready and verified and level < UNLOCK_LEVEL,
        "missing": _missing_reasons(stats, verified),
        "last_verify": last_verify,
        "tasks": tasks,
    }


def status(path: Path | str, skill_id: str | None = None) -> dict:
    """只读视图：单技能明细，或全技能汇总。"""
    skilltree.ensure_nodes(path)
    sid = _clean(skill_id)
    if sid:
        node = skilltree.get(path, sid)
        if node is None:
            return _fail("技能不存在", code="unknown_skill", skill_id=sid)
        return {"ok": True, **_summary(path, node)}

    skills = [_summary(path, node) for node in skilltree.list_nodes(path)]
    totals = {
        "skills": len(skills),
        "unlocked": sum(1 for item in skills if item["level"] >= UNLOCK_LEVEL),
        "sources": sum(item["source_count"] for item in skills),
        "cards": sum(item["card_count"] for item in skills),
        "practices": sum(item["practice"]["count"] for item in skills),
        "tasks": sum(len(item["tasks"]) for item in skills),
        "open_tasks": sum(
            1
            for item in skills
            for task in item["tasks"]
            if task.get("status") != "done"
        ),
        "fail_count": sum(item["fail_count"] for item in skills),
    }
    return {"ok": True, "skills": skills, "totals": totals}


__all__ = [
    "MIN_PRACTICES",
    "UNLOCK_LEVEL",
    "open_gap",
    "record_source",
    "write_card",
    "record_practice",
    "verify_and_unlock",
    "status",
]
