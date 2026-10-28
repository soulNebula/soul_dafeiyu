# -*- coding: utf-8 -*-
"""S9 用户画像蒸馏：固定 10 维，每维都要带证据，没证据不编。

契约见 ``BATCH3_CONTRACT.md``（Agent C）：

- :data:`DIMENSIONS` 是固定的十维；
- :func:`distill` 从 ``user_memories`` / ``claims`` 取证据，由 LLM 输出每维
  ``value`` + ``evidence_memory_ids``；每维至少 1 条证据，且证据 id 必须真实
  存在于本次喂进去的证据里，否则整维丢弃；
- :func:`get_profile` 读回当前画像（只含 ``active``/``pending`` 的当前版本）。

实现取舍（契约没写死、这里显式约定）：

- 证据只用 ``status='active'`` 的 ``user_memories(scope='user')`` 与
  ``claims``；``scope='self'/'world'`` 不是用户信息，不参与画像；
- ``claims`` 里 ``source_level='C'`` 是模型推断，不算证据，不参与蒸馏；
- 模型说“不确定”的维度（``uncertain=true``、``status`` 为 skip/rejected/
  conflict，或 value 就是“不确定/未知”）直接跳过，不落库；
- 同一维度只保留一个“当前版本”：写入新值时把该维旧的 ``active``/``pending``
  行标成 ``stale``，不物理删除，保留审计；同一结论重复蒸馏会刷新
  ``updated_at``，不会产生重复行；
- ``confidence`` 取模型给的（夹到 0~1），没给就用证据置信度均值；
- ``status`` 取模型给的 ``active``/``pending``，没给默认 ``active``；
- 每维 value 截到 :data:`MAX_VALUE_CHARS`（设计文档 S9 的篇幅上限）；
- 维度 key 支持英文与中文别名（如 ``job``/``职业`` → ``work``）；
- 没有证据时不调用 LLM，直接返回 ``written=0``，不编造任何一行。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

#: 固定的十个画像维度（顺序即展示顺序）。
DIMENSIONS = (
    "work",
    "city",
    "family",
    "age",
    "school",
    "health",
    "interests",
    "values",
    "style",
    "goals",
)

#: 中文标签，用于 prompt 与展示。
DIMENSION_LABELS = {
    "work": "职业/工作",
    "city": "城市/居住地",
    "family": "家庭/家人",
    "age": "年龄/阶段",
    "school": "学校/学历",
    "health": "健康/身体",
    "interests": "兴趣/爱好",
    "values": "价值观/在意什么",
    "style": "说话风格/习惯",
    "goals": "目标/近期打算",
}

#: 每维 value 的最大字数（设计文档 S9：画像要短）。
MAX_VALUE_CHARS = 120
#: 每类证据最多取多少条（按时间倒序）。
MAX_EVIDENCE_PER_SOURCE = 50
#: 证据总量上限。
MAX_EVIDENCE_TOTAL = 70
#: 单条证据文本最多保留多少字。
MAX_EVIDENCE_CHARS = 160
#: 模型漏填置信度时的默认值。
DEFAULT_CONFIDENCE = 0.6

SYSTEM_PROMPT = "你是用户画像蒸馏器，只输出 JSON。"
PURPOSE = "selflearn_profile"

#: 模型给的其他维度写法 → 契约维度。
DIMENSION_ALIASES = {
    "work": "work", "job": "work", "career": "work", "occupation": "work",
    "职业": "work", "工作": "work", "职位": "work", "职业信息": "work",
    "city": "city", "location": "city", "live": "city", "residence": "city",
    "居住地": "city", "城市": "city", "所在地": "city", "地点": "city",
    "family": "family", "家庭": "family", "家人": "family", "家庭成员": "family",
    "age": "age", "年龄": "age", "阶段": "age",
    "school": "school", "education": "school", "学校": "school",
    "学历": "school", "教育": "school", "毕业院校": "school",
    "health": "health", "健康": "health", "身体": "health", "健康状况": "health",
    "interests": "interests", "interest": "interests", "hobby": "interests",
    "hobbies": "interests", "兴趣": "interests", "爱好": "interests",
    "兴趣偏好": "interests",
    "values": "values", "value": "values", "价值观": "values",
    "价值": "values", "在意什么": "values",
    "style": "style", "language": "style", "语言风格": "style",
    "说话风格": "style", "风格": "style", "聊天风格": "style",
    "goals": "goals", "goal": "goals", "plan": "goals", "目标": "goals",
    "近期目标": "goals", "计划": "goals", "打算": "goals",
}

_STATUSES = ("active", "pending")
_SKIP_STATUSES = frozenset(
    {"skip", "skipped", "uncertain", "rejected", "conflict", "stale", "invalid", "dropped"}
)
_UNCERTAIN_VALUES = frozenset(
    {"不确定", "未知", "不知道", "不清楚", "说不好", "unknown", "uncertain",
     "n/a", "na", "null", "none"}
)
_TRIM = " \t\r\n_-—.,;:!?，。；：！？、·\"'`()（）[]【】<>《》/\\|"
_WORK_RE = re.compile(r"\s+")


def distill(path: Path | str, client) -> dict[str, Any]:
    """蒸馏一次用户画像，把可信的维度写进 ``user_profile``。

    ``client`` 需提供 ``available()`` / ``chat(messages, **kwargs)``，与
    :class:`agi.selflearn.llm.Client` 同形；也接受任何返回同样结构的 fake
    client（测试注入，不联网）。

    返回 ``{"ok", "written", "dimensions", "skipped", "evidence_count"}``：
    ``dimensions`` 是本次落库的维度 → ``{value, confidence, status,
    evidence_memory_ids}``；``skipped`` 记录被丢掉的维度及原因（便于自检）。

    没有证据时不会调用 LLM，直接 ``ok=True, written=0``；client 不可用/报错时
    ``ok=False``，但都不会抛异常。
    """
    evidence = _load_evidence(path)
    if not evidence:
        # 没证据不编：不调用模型，也不写任何一行。
        return {
            "ok": True,
            "written": 0,
            "dimensions": {},
            "skipped": [],
            "evidence_count": 0,
            "reason": "没有可用证据",
        }

    raw, error = _call_client(client, build_prompt(evidence))
    if error:
        return {
            "ok": False,
            "written": 0,
            "dimensions": {},
            "skipped": [],
            "evidence_count": len(evidence),
            "error": error,
        }

    selected, skipped = _select_items(_parse_items(raw), evidence)
    if not selected:
        return {
            "ok": True,
            "written": 0,
            "dimensions": {},
            "skipped": skipped,
            "evidence_count": len(evidence),
            "reason": "LLM 没有给出可落库的维度",
        }

    written = _store(path, selected)
    return {
        "ok": True,
        "written": written,
        "dimensions": {dim: _public(item) for dim, item in selected.items()},
        "skipped": skipped,
        "evidence_count": len(evidence),
    }


def get_profile(path: Path | str = DB_PATH) -> dict[str, dict[str, Any]]:
    """读回当前用户画像。

    返回 ``{dimension: {"value", "confidence", "status",
    "evidence_memory_ids", "updated_at"}}``，按 :data:`DIMENSIONS` 顺序，
    只包含 ``active``/``pending`` 的当前版本；同一维度有多个版本时优先
    ``active``、再取最新的。没有画像时返回 ``{}``。
    """
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT id,dimension,value,evidence_json,confidence,status,updated_at "
            "FROM user_profile WHERE status IN ('active','pending') "
            "ORDER BY updated_at DESC, id"
        ).fetchall()
    finally:
        con.close()

    current: dict[str, tuple[tuple[int, float], dict[str, Any]]] = {}
    for row in rows:
        dimension = canon_dimension(row["dimension"])
        if not dimension:
            continue
        rank = (1 if row["status"] == "active" else 0, float(row["updated_at"] or 0.0))
        if dimension in current and rank <= current[dimension][0]:
            continue
        current[dimension] = (
            rank,
            {
                "value": str(row["value"]),
                "confidence": _confidence(row["confidence"]),
                "status": str(row["status"]),
                "evidence_memory_ids": _evidence_ids(row["evidence_json"]),
                "updated_at": round(float(row["updated_at"] or 0.0), 3),
            },
        )
    return {dim: current[dim][1] for dim in DIMENSIONS if dim in current}


def render_profile(path: Path | str = DB_PATH, max_chars: int = 900) -> str:
    """把画像渲染成可直接塞进 prompt 的短文本（空画像返回空串）。"""
    profile = get_profile(path)
    lines = [
        f"- {DIMENSION_LABELS.get(dim, dim)}：{item['value']}"
        for dim, item in profile.items()
    ]
    return "\n".join(lines)[: max(0, int(max_chars))]


# ---------------------------------------------------------------------------
# 证据
# ---------------------------------------------------------------------------


def _load_evidence(path: Path | str) -> list[dict[str, Any]]:
    """从 user_memories / claims 取当前可用证据（按时间倒序）。"""
    con = db.connect(path)
    try:
        memories = con.execute(
            "SELECT id,kind,content,evidence,confidence FROM user_memories "
            "WHERE status='active' AND scope='user' "
            "ORDER BY created_at DESC, id LIMIT ?",
            (MAX_EVIDENCE_PER_SOURCE,),
        ).fetchall()
        claims = con.execute(
            "SELECT id,subject,predicate,object,polarity,time_ref,claim_type,"
            "evidence_span,confidence,source_level FROM claims "
            "WHERE status='active' AND source_level<>'C' "
            "ORDER BY created_at DESC, id LIMIT ?",
            (MAX_EVIDENCE_PER_SOURCE,),
        ).fetchall()
    finally:
        con.close()

    evidence: list[dict[str, Any]] = []
    for row in memories:
        content = _clean(row["content"])
        if not content:
            continue
        evidence.append({
            "id": str(row["id"]),
            "source": "memory",
            "kind": _clean(row["kind"]) or "fact",
            "text": content[:MAX_EVIDENCE_CHARS],
            "confidence": _confidence(row["confidence"]),
        })
    for row in claims:
        text = _claim_text(row)
        if not text:
            continue
        evidence.append({
            "id": str(row["id"]),
            "source": "claim",
            "kind": _clean(row["claim_type"]) or "fact",
            "text": text[:MAX_EVIDENCE_CHARS],
            "confidence": _confidence(row["confidence"]),
        })
    return evidence[:MAX_EVIDENCE_TOTAL]


def _claim_text(row) -> str:
    """把一条主张渲染成可读证据（原话片段一并带上，便于模型复核）。"""
    subject = _clean(row["subject"]) or "用户"
    if subject.lower() in ("user", "me", "i", "我"):
        subject = "用户"
    predicate = _clean(row["predicate"])
    obj = _clean(row["object"])
    if not predicate or not obj:
        return ""
    try:
        polarity = int(row["polarity"] or 1)
    except (TypeError, ValueError):
        polarity = 1
    core = f"{subject}不{predicate}{obj}" if polarity < 0 else f"{subject}{predicate}{obj}"
    span = _clean(row["evidence_span"])
    if span and span not in core:
        core += f"（原话：{span}）"
    time_ref = _clean(row["time_ref"])
    if time_ref:
        core += f"（时间：{time_ref}）"
    return core


def _evidence_lines(evidence: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- [{item['id']}] {item['source']}/{item['kind']}：{item['text']}"
        f"（置信度 {item['confidence']:.2f}）"
        for item in evidence
    )


def build_prompt(evidence: list[dict[str, Any]]) -> str:
    """构造蒸馏 prompt；独立出来方便测试与手工调试。"""
    labels = "\n".join(f"- {dim}：{DIMENSION_LABELS[dim]}" for dim in DIMENSIONS)
    return (
        "从下面的用户证据里蒸馏用户画像，只输出 JSON。\n\n"
        "维度只能用这些 key：\n" + labels + "\n\n"
        "规则：\n"
        "1. 只能使用给定证据，不要编造；每个 value 都必须能由证据直接支持。\n"
        "2. value 写可验证的事实，1-2 句，不堆形容词，不超过 120 字；"
        "写“每周三晚上打球”而不是“热爱运动”。\n"
        "3. evidence_memory_ids 至少 1 条，必须逐字抄下面方括号里的 id"
        "（UM- 开头是记忆，C- 开头是主张），不要自己造 id。\n"
        "4. 证据不足、互相矛盾或你不确定的维度，直接不要输出。\n"
        '5. 没有可写的就输出 {"profile": []}。\n\n'
        "输出 JSON 格式：\n"
        '{"profile":[{"dimension":"work","value":"可验证的结论",'
        '"evidence_memory_ids":["UM-..."],"confidence":0.0-1.0,'
        '"status":"active"}]}\n\n'
        "用户证据：\n" + _evidence_lines(evidence)
    )


# ---------------------------------------------------------------------------
# 校验与落库
# ---------------------------------------------------------------------------


def canon_dimension(value) -> str:
    """把模型给的维度写法归一到 :data:`DIMENSIONS`，认不出返回 ``""``。"""
    raw = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    raw = raw.strip(_TRIM)
    if not raw:
        return ""
    if raw in DIMENSION_ALIASES:
        return DIMENSION_ALIASES[raw]
    for suffix in ("维度", "dimension"):
        if raw.endswith(suffix):
            base = raw[: -len(suffix)].strip(_TRIM)
            if base in DIMENSION_ALIASES:
                return DIMENSION_ALIASES[base]
    return ""


def _select_items(
    items: list[dict], evidence: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """校验模型输出：维度合法、value 可用、至少 1 条真实证据。"""
    index = {item["id"]: item for item in evidence}
    upper = {item["id"].upper(): item["id"] for item in evidence}

    candidates: dict[str, list[dict[str, Any]]] = {}
    skipped: list[dict[str, str]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        dimension = canon_dimension(
            raw.get("dimension") or raw.get("key") or raw.get("name") or raw.get("维度")
        )
        if not dimension:
            skipped.append({
                "dimension": _clean(raw.get("dimension") or raw.get("key"))[:40],
                "reason": "unknown_dimension",
            })
            continue
        if _is_uncertain(raw):
            skipped.append({"dimension": dimension, "reason": "uncertain"})
            continue
        value = _value(raw)
        if not value:
            skipped.append({"dimension": dimension, "reason": "empty_value"})
            continue
        ids = _resolve_ids(_flatten_ids(_raw_ids(raw)), index, upper)
        if not ids:
            skipped.append({"dimension": dimension, "reason": "no_evidence"})
            continue
        fallback = _mean([index[evidence_id]["confidence"] for evidence_id in ids])
        confidence = _confidence(raw.get("confidence"), fallback=fallback)
        candidates.setdefault(dimension, []).append({
            "dimension": dimension,
            "value": value,
            "confidence": confidence,
            "status": _status(raw),
            "evidence_memory_ids": ids,
            "evidence": [index[evidence_id] for evidence_id in ids],
        })

    selected: dict[str, dict[str, Any]] = {}
    for dimension, group in candidates.items():
        best = max(
            group,
            key=lambda item: (item["confidence"], len(item["evidence_memory_ids"])),
        )
        selected[dimension] = best
        for item in group:
            if item is not best:
                skipped.append({"dimension": dimension, "reason": "duplicate"})
    ordered = {dim: selected[dim] for dim in DIMENSIONS if dim in selected}
    return ordered, skipped


def _store(path: Path | str, selected: dict[str, dict[str, Any]]) -> int:
    """写 user_profile；同维旧版本标 stale，不物理删除。"""
    now = time.time()
    con = db.connect(path)
    try:
        for dimension, item in selected.items():
            profile_id = _profile_id(dimension, item["value"])
            evidence_json = json.dumps(
                {
                    "memory_ids": item["evidence_memory_ids"],
                    "items": [
                        {
                            "id": ev["id"],
                            "source": ev["source"],
                            "kind": ev["kind"],
                            "text": ev["text"],
                            "confidence": ev["confidence"],
                        }
                        for ev in item["evidence"]
                    ],
                },
                ensure_ascii=False,
            )
            # 同一维度只留一个当前版本；旧版本留痕成 stale。
            con.execute(
                "UPDATE user_profile SET status='stale', updated_at=? "
                "WHERE dimension=? AND id<>? AND status IN ('active','pending')",
                (now, dimension, profile_id),
            )
            exists = con.execute(
                "SELECT 1 FROM user_profile WHERE id=?", (profile_id,)
            ).fetchone()
            if exists is None:
                con.execute(
                    "INSERT INTO user_profile(id,dimension,value,evidence_json,"
                    "confidence,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (profile_id, dimension, item["value"], evidence_json,
                     item["confidence"], item["status"], now),
                )
            else:
                con.execute(
                    "UPDATE user_profile SET dimension=?,value=?,evidence_json=?,"
                    "confidence=?,status=?,updated_at=? WHERE id=?",
                    (dimension, item["value"], evidence_json, item["confidence"],
                     item["status"], now, profile_id),
                )
        con.commit()
    finally:
        con.close()
    return len(selected)


def _profile_id(dimension: str, value: str) -> str:
    digest = hashlib.sha1(f"{dimension}|{value}".encode("utf-8", "replace")).hexdigest()
    return "UP-" + digest[:16]


def _public(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "value": item["value"],
        "confidence": item["confidence"],
        "status": item["status"],
        "evidence_memory_ids": list(item["evidence_memory_ids"]),
    }


# ---------------------------------------------------------------------------
# 模型输出解析
# ---------------------------------------------------------------------------


def _parse_items(raw: str) -> list[dict]:
    """从模型输出里抠出画像条目；任何解析失败都返回 ``[]``。"""
    data = _load_json(raw)
    if isinstance(data, dict):
        for key in ("profile", "dimensions", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                return _items_from_mapping(value)
        if "dimension" in data or "value" in data:
            return [data]
        if any(canon_dimension(key) for key in data):
            return _items_from_mapping(data)
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _load_json(raw: str):
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    try:
        from .llm import parse_json

        parsed = parse_json(text)
    except Exception:  # noqa: BLE001 - 解析失败不是错误路径
        parsed = None
    if isinstance(parsed, (dict, list)):
        return parsed
    for pattern in (r"\{.*\}", r"\[.*\]"):
        match = re.search(pattern, text, re.S)
        if not match:
            continue
        try:
            return json.loads(match.group(0))
        except (TypeError, ValueError):
            continue
    return None


def _items_from_mapping(mapping: dict) -> list[dict]:
    items: list[dict] = []
    for dimension, value in mapping.items():
        if isinstance(value, dict):
            item = dict(value)
            item.setdefault("dimension", dimension)
            items.append(item)
        elif isinstance(value, (str, int, float)):
            items.append({"dimension": dimension, "value": str(value)})
    return items


def _value(raw: dict) -> str:
    value = raw.get("value")
    if not isinstance(value, str):
        for key in ("summary", "content", "text", "结论"):
            if isinstance(raw.get(key), str):
                value = raw[key]
                break
    value = _clean(value)
    if not value:
        return ""
    if value.lower() in _UNCERTAIN_VALUES or value.startswith("不确定"):
        return ""
    return value[:MAX_VALUE_CHARS]


def _is_uncertain(raw: dict) -> bool:
    flag = raw.get("uncertain")
    if isinstance(flag, str):
        if flag.strip().lower() in ("true", "1", "yes", "y", "是", "不确定"):
            return True
    elif flag:
        return True
    status = str(raw.get("status") or "").strip().lower()
    if status in _SKIP_STATUSES:
        return True
    return bool(raw.get("skip") or raw.get("omit"))


def _status(raw: dict) -> str:
    status = str(raw.get("status") or "").strip().lower()
    return status if status in _STATUSES else "active"


def _raw_ids(raw: dict):
    for key in ("evidence_memory_ids", "evidence_ids", "memory_ids", "evidence",
                "sources", "source_ids", "evidenceIds"):
        if key in raw and raw[key] not in (None, "", []):
            return raw[key]
    return []


def _flatten_ids(raw) -> list[str]:
    out: list[str] = []
    if raw is None:
        return out
    if isinstance(raw, str):
        parts = re.split(r"[,，;；、\s]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        parts = list(raw)
    else:
        parts = [raw]
    for part in parts:
        if isinstance(part, dict):
            for key in ("id", "memory_id", "memory_ids", "evidence_id"):
                if key in part:
                    out.extend(_flatten_ids(part[key]))
                    break
            continue
        if isinstance(part, (list, tuple, set)):
            out.extend(_flatten_ids(part))
            continue
        token = str(part or "").strip()
        if token:
            out.append(token)
    return out


def _resolve_ids(tokens: list[str], index: dict, upper: dict) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        resolved = _resolve_id(token, index, upper)
        if resolved and resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


def _resolve_id(token: str, index: dict, upper: dict) -> str:
    value = str(token or "").strip().strip(_TRIM)
    if not value:
        return ""
    if value in index:
        return value
    if value.upper() in upper:
        return upper[value.upper()]
    base = re.split(r"[\s（(｜|,，;；:：=]+", value, maxsplit=1)[0].strip(_TRIM)
    if base in index:
        return base
    if base.upper() in upper:
        return upper[base.upper()]
    return ""


def _evidence_ids(raw) -> list[str]:
    """解析 evidence_json，兼容列表/对象/纯字符串三种历史写法。"""
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return _flatten_ids(text.strip("[]"))
    if isinstance(data, dict):
        picked = None
        for key in ("memory_ids", "evidence_memory_ids", "evidence_ids", "ids"):
            if data.get(key) not in (None, "", []):
                picked = data[key]
                break
        if picked is None:
            picked = data.get("items") or data.get("evidence") or []
        data = picked
    return _flatten_ids(data)


def _call_client(client, prompt: str) -> tuple[str, str]:
    """调用可注入 client，返回 ``(text, error)``；不抛异常、不联网。"""
    if client is None or not callable(getattr(client, "chat", None)):
        return "", "LLM 客户端不可用"
    try:
        available = getattr(client, "available", None)
        if callable(available) and not available():
            return "", "LLM 不可用"
        response = client.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            purpose=PURPOSE,
            max_tokens=1200,
            temperature=0.2,
            json_mode=True,
        )
    except Exception as exc:  # noqa: BLE001 - 网络/模型错误都按失败返回
        return "", f"{type(exc).__name__}: {str(exc)[:160]}"
    if isinstance(response, dict):
        if response.get("ok") is False:
            return "", str(response.get("error") or "LLM 调用失败")[:200]
        return str(response.get("text") or ""), ""
    return str(response or ""), ""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _clean(value) -> str:
    return _WORK_RE.sub(" ", str(value or "")).strip()


def _confidence(value, fallback: float = DEFAULT_CONFIDENCE) -> float:
    for candidate in (value, fallback):
        try:
            return round(max(0.0, min(1.0, float(candidate))), 3)
        except (TypeError, ValueError):
            continue
    return DEFAULT_CONFIDENCE


def _mean(values: list[float]) -> float:
    numbers = [float(item) for item in values if isinstance(item, (int, float))]
    return sum(numbers) / len(numbers) if numbers else DEFAULT_CONFIDENCE


__all__ = [
    "DIMENSIONS",
    "DIMENSION_LABELS",
    "MAX_VALUE_CHARS",
    "SYSTEM_PROMPT",
    "PURPOSE",
    "distill",
    "get_profile",
    "render_profile",
    "canon_dimension",
    "build_prompt",
]
