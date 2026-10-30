# -*- coding: utf-8 -*-
"""04 待审提案状态机：draft → gray → merged / rejected。

``transition`` 仍只负责状态迁移；唯一的"落地"入口是 :func:`apply_merged`：
- ``merged`` 的 ``persona_trait`` 提案会写入 **core 人格层**（approved_by 记
  ``proposal:<id>``，changelog 留痕）；重复 apply 幂等返回 ``already_applied``；
- 其它 kind 一律返回 ``unsupported_kind``（不静默），由人工按类型补 apply 实现；
- 提案状态变 merged 时 ``transition`` 自动调用 apply，并在返回值里带
  ``applied`` / ``apply_error``（apply 失败不改写状态迁移本身的成功）。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

#: 契约里的四个状态；merged / rejected 为终态
STATUSES = ("draft", "gray", "merged", "rejected")
INITIAL = "draft"
TERMINAL = ("merged", "rejected")
#: 只有下面这些转换合法；终态没有出边
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("gray", "rejected"),
    "gray": ("merged", "rejected"),
    "merged": (),
    "rejected": (),
}
#: 动作别名 → 目标状态。动作只由人/CLI 显式调用，不自动触发
ACTION_ALIASES: dict[str, str] = {
    "gray": "gray",
    "to_gray": "gray",
    "submit": "gray",
    "review": "gray",
    "rollout": "gray",
    "grayscale": "gray",
    "merge": "merged",
    "merged": "merged",
    "to_merged": "merged",
    "approve": "merged",
    "approved": "merged",
    "accept": "merged",
    "accepted": "merged",
    "promote": "merged",
    "reject": "rejected",
    "rejected": "rejected",
    "to_rejected": "rejected",
    "deny": "rejected",
    "decline": "rejected",
}
#: 04 规格里的提案类型；``persona_trait`` 是当前唯一有 apply 实现的类型
KINDS = ("new_variant", "rule_patch", "knob_adjust", "persona_patch",
         "persona_trait")
#: 已实现 apply 的 kind；其它 kind 会明确返回 unsupported_kind
APPLICABLE_KINDS = ("persona_trait",)

_EPSILON = 1e-6
_MAX_KIND = 64
_MAX_TARGET = 200


def _text(value: Any, limit: int) -> str:
    """清掉多余空白并截断；kind/target 只用于展示与分组。"""
    return " ".join(str(value or "").split())[:limit]


def _clean_id(value: Any) -> str:
    return str(value or "").strip()[:120]


def _decode(raw: Any, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _encode(field: str, value: Any) -> tuple[str, str]:
    """返回 (json_text, error)；不允许 NaN/Infinity 混进库。"""
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False), ""
    except (TypeError, ValueError) as exc:
        return "", f"{field} 必须是可 JSON 序列化的数据：{exc}"


def _normalise_payload(value: Any) -> tuple[dict[str, Any] | None, str]:
    """payload 必须是 JSON 对象；兼容以 JSON 字符串传入的对象。"""
    if value is None:
        return None, "payload 不能为空"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, "payload 不能为空"
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return None, "payload 必须是 JSON 对象"
    if not isinstance(value, dict):
        return None, "payload 必须是 JSON 对象"
    return value, ""


def _normalise_evidence(value: Any) -> Any:
    """evidence 落库前归一：None→[]；JSON 字符串还原；普通文本当单条证据。"""
    if value is None:
        return []
    if isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return [text]
        if isinstance(parsed, (list, dict)):
            return parsed
        return [parsed]
    return [value]


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    status = str(row["status"] or "").strip().lower()
    return {
        "id": row["id"],
        "kind": row["kind"],
        "target": row["target"],
        "payload": _decode(row["payload_json"], {}),
        "payload_json": row["payload_json"],
        "evidence": _decode(row["evidence_json"], []),
        "evidence_json": row["evidence_json"],
        "status": status,
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "next": list(TRANSITIONS.get(status, ())),
        "terminal": status in TERMINAL,
    }


def _new_id(con: sqlite3.Connection, now: float) -> str:
    """按 04 规格生成 P-YYYYMMDD-NNN。"""
    prefix = "P-" + time.strftime("%Y%m%d", time.localtime(now)) + "-"
    total = con.execute(
        "SELECT count(*) FROM learning_proposals WHERE id LIKE ?", (prefix + "%",)
    ).fetchone()[0]
    seq = int(total or 0) + 1
    while con.execute("SELECT 1 FROM learning_proposals WHERE id=?",
                      (f"{prefix}{seq:03d}",)).fetchone() is not None:
        seq += 1
    return f"{prefix}{seq:03d}"


def get(path: Path | str = DB_PATH, proposal_id: Any = "") -> dict[str, Any] | None:
    """按 id 读一条提案；不存在返回 None。"""
    pid = _clean_id(proposal_id)
    if not pid:
        return None
    con = db.connect(path)
    try:
        row = con.execute(
            "SELECT * FROM learning_proposals WHERE id=?", (pid,)).fetchone()
    finally:
        con.close()
    return _row_dict(row) if row is not None else None


def apply_merged(path: Path | str = DB_PATH, proposal_id: Any = "") -> dict[str, Any]:
    """把状态为 **merged** 的提案落到线上（当前唯一 apply 点）。

    支持的 kind：``persona_trait`` → core 人格层（``target`` 作 key、
    ``payload["value"]`` 作 value，approved_by 记 ``proposal:<id>`` 并写 changelog）。

    - 非 merged → ``code="not_merged"``；
    - 其它 kind → ``code="unsupported_kind"``（明确报错，不静默）；
    - 已 apply 过 → ``already_applied=True``，不重复写；
    - 任何异常 → ``code="apply_failed"`` 并把异常摘要放进 ``error``。
    """
    pid = _clean_id(proposal_id)
    if not pid:
        return {"ok": False, "error": "proposal_id 不能为空", "code": "bad_id"}
    item = get(path, pid)
    if item is None:
        return {"ok": False, "error": "提案不存在", "code": "not_found", "id": pid}
    status = str(item.get("status") or "").strip().lower()
    if status != "merged":
        return {"ok": False, "error": "只有 merged 提案可以 apply",
                "code": "not_merged", "id": pid, "status": status}
    kind = str(item.get("kind") or "").strip()
    if kind not in APPLICABLE_KINDS:
        return {"ok": False,
                "error": f"unknown proposal kind: {kind}（no apply implementation）",
                "code": "unsupported_kind", "id": pid, "kind": kind}
    payload = item.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    target = str(item.get("target") or "").strip()
    value = str(payload.get("value") or "").strip()
    if not target or not value:
        return {"ok": False, "error": "persona_trait 需要 target 与 payload.value",
                "code": "invalid_payload", "id": pid, "kind": kind}
    approved_by = "proposal:" + pid
    try:
        # 函数内 import：避免 proposals ↔ persona_layers 的加载顺序环
        from . import persona_layers
        result = persona_layers.apply_core(path, target, value, approved_by,
                                           evidence=[approved_by])
    except Exception as exc:  # noqa: BLE001 - apply 失败必须可见，不能吞掉
        return {"ok": False,
                "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                "code": "apply_failed", "id": pid, "kind": kind}
    out: dict[str, Any] = {
        "ok": bool(result.get("ok")), "id": pid, "kind": kind,
        "target": target, "value": value, "approved_by": approved_by,
        "applied": bool(result.get("applied")),
        "already_applied": bool(result.get("already_applied")),
    }
    if result.get("id"):
        out["persona_layer_id"] = result.get("id")
    if not result.get("ok"):
        out["error"] = str(result.get("error") or "apply_failed")
        out["code"] = str(result.get("code") or "apply_failed")
    return out


def create(path: Path | str = DB_PATH, kind: Any = "", target: Any = "",
           payload: Any = None, evidence: Any = None) -> dict[str, Any]:
    """新建提案，状态固定为 draft；生成 id 与时间，不触发任何学习动作。"""
    kind_text = _text(kind, _MAX_KIND)
    if not kind_text:
        return {"ok": False, "error": "kind 不能为空"}
    target_text = _text(target, _MAX_TARGET)
    if not target_text:
        return {"ok": False, "error": "target 不能为空"}
    payload_obj, error = _normalise_payload(payload)
    if error:
        return {"ok": False, "error": error}
    payload_json, error = _encode("payload", payload_obj)
    if error:
        return {"ok": False, "error": error}
    evidence_obj = _normalise_evidence(evidence)
    evidence_json, error = _encode("evidence", evidence_obj)
    if error:
        return {"ok": False, "error": error}

    now = time.time()
    con = db.connect(path)
    try:
        proposal_id = ""
        for _ in range(5):
            proposal_id = _new_id(con, now)
            try:
                con.execute(
                    "INSERT INTO learning_proposals(id,kind,target,payload_json,"
                    "evidence_json,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (proposal_id, kind_text, target_text, payload_json,
                     evidence_json, INITIAL, now, now),
                )
                con.commit()
                break
            except sqlite3.IntegrityError:
                con.rollback()
        else:
            return {"ok": False, "error": "生成提案 id 失败，请重试"}
        row = con.execute(
            "SELECT * FROM learning_proposals WHERE id=?", (proposal_id,)).fetchone()
    finally:
        con.close()
    if row is None:
        return {"ok": False, "error": "提案写入后未找到"}
    return {"ok": True, **_row_dict(row)}


def transition(path: Path | str = DB_PATH, proposal_id: Any = "",
               action: Any = "") -> dict[str, Any]:
    """按状态机迁移提案；非法动作/转换一律拒绝且不改库。"""
    pid = _clean_id(proposal_id)
    if not pid:
        return {"ok": False, "error": "proposal_id 不能为空"}
    action_text = str(action or "").strip().lower()
    target_status = ACTION_ALIASES.get(action_text, "")
    if not target_status:
        return {"ok": False,
                "error": f"未知动作：{action!r}；只允许 gray、merge、reject（及常见别名）",
                "id": pid}

    con = db.connect(path)
    try:
        row = con.execute(
            "SELECT * FROM learning_proposals WHERE id=?", (pid,)).fetchone()
        if row is None:
            return {"ok": False, "error": "提案不存在", "id": pid}
        raw_status = str(row["status"] or "").strip()
        current = raw_status.lower()
        allowed = TRANSITIONS.get(current)
        if allowed is None:
            return {"ok": False, "error": f"提案状态异常：{raw_status!r}",
                    "id": pid, "status": current, "allowed": []}
        if not allowed:
            return {"ok": False, "error": f"提案已是终态 {current}，不能再变更",
                    "id": pid, "status": current, "allowed": []}
        if target_status not in allowed:
            return {"ok": False,
                    "error": f"不允许 {current}→{target_status}；"
                             f"只允许 {current}→{' / '.join(allowed)}",
                    "id": pid, "status": current, "allowed": list(allowed)}
        # 至少 +1µs，保证 updated_at 严格递增，便于审计排序
        updated_at = max(time.time(), float(row["updated_at"] or 0.0) + _EPSILON)
        cur = con.execute(
            "UPDATE learning_proposals SET status=?, updated_at=? "
            "WHERE id=? AND status=?",
            (target_status, updated_at, pid, raw_status),
        )
        con.commit()
        if int(cur.rowcount or 0) == 0:
            return {"ok": False, "error": "提案状态已被其他操作改变，请刷新后重试",
                    "id": pid}
        fresh = con.execute(
            "SELECT * FROM learning_proposals WHERE id=?", (pid,)).fetchone()
    finally:
        con.close()
    if fresh is None:
        return {"ok": False, "error": "提案不存在", "id": pid}
    result: dict[str, Any] = {
        "ok": True, **_row_dict(fresh), "action": action_text,
        "previous_status": current,
    }
    # merged 是唯一的"落地"时机：自动 apply，并显式回报结果/错误（不静默）。
    if str(fresh["status"] or "").strip().lower() == "merged":
        try:
            applied = apply_merged(path, pid)
        except Exception as exc:  # noqa: BLE001 - apply 异常不能让 transition 假成功
            applied = {"ok": False,
                       "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                       "code": "apply_failed"}
        result["applied"] = bool(applied.get("applied"))
        result["apply"] = applied
        if not applied.get("ok"):
            result["apply_error"] = str(
                applied.get("error") or applied.get("code") or "apply_failed")
    return result


def _status_filter(status: Any) -> tuple[str, ...]:
    if status is None or (isinstance(status, str) and not status.strip()):
        return ()
    if isinstance(status, str):
        values = [status]
    else:
        try:
            values = list(status)
        except TypeError:
            values = [status]
    wanted: list[str] = []
    for value in values:
        text = str(value or "").strip().lower()
        if text not in STATUSES:
            raise ValueError(f"未知状态：{value!r}；只允许 {', '.join(STATUSES)}")
        if text not in wanted:
            wanted.append(text)
    if not wanted:
        raise ValueError("status 过滤不能为空")
    return tuple(wanted)


def list_proposals(path: Path | str = DB_PATH, status: Any = None) -> list[dict[str, Any]]:
    """按状态过滤提案；status 可为单个状态、状态列表或 None（全部）。

    未知状态属于调用方错误，抛 ``ValueError``，避免静默返回空列表。
    """
    wanted = _status_filter(status)
    con = db.connect(path)
    try:
        if wanted:
            marks = ",".join("?" for _ in wanted)
            rows = con.execute(
                "SELECT * FROM learning_proposals WHERE lower(status) IN "
                f"({marks}) ORDER BY created_at DESC, id DESC", wanted).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM learning_proposals "
                "ORDER BY created_at DESC, id DESC").fetchall()
    finally:
        con.close()
    return [_row_dict(row) for row in rows]


def can_transition(status: Any, action: Any) -> bool:
    """纯函数：判断某状态能否执行该动作；给 CLI/网页禁用按钮用。"""
    current = str(status or "").strip().lower()
    target = ACTION_ALIASES.get(str(action or "").strip().lower(), "")
    return bool(target) and target in TRANSITIONS.get(current, ())


__all__ = [
    "ACTION_ALIASES", "APPLICABLE_KINDS", "INITIAL", "KINDS", "STATUSES",
    "TERMINAL", "TRANSITIONS", "apply_merged", "can_transition", "create",
    "get", "list_proposals", "transition",
]
