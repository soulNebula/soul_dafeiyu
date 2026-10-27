# -*- coding: utf-8 -*-
"""S6 原子主张抽取：LLM JSON 抽取 + 原文证据硬校验。

只负责把一段用户原话变成可审计的 Claim 并写入 ``claims`` 表；
是否采纳（合并/冲突/敏感信息/低置信度）由 S7 ``gates.py`` 决定。

硬规则（BATCH1_CONTRACT.md）：
- 每条 ``evidence_span`` 必须是入参 ``text`` 的子串，否则整条丢弃；
- ``claim_type`` 限定在 :data:`CLAIM_TYPES`；
- 抽取阶段只产出 ``source_level`` A（用户原话）或 C（模型推断）；
- 模型推断标 ``claim_type="inference", source_level="C"``，一次最多 3 条。

实现取舍（契约未写死、这里显式约定）：
- 缺 ``subject`` 时默认 ``"user"``（当前对话默认主体就是用户）；
- ``claim_type`` 收到契约外的值（如 ``opinion``/``profile``）先按
  :data:`CLAIM_TYPE_ALIASES` 映射，仍不合法则降为 ``fact``；
- ``source_level`` 只保留 A/C：模型给 B 也按 A 存（B 是规则层自己写的）；
- 偏好类谓词统一归一成 ``喜欢``，正负记在 ``polarity``（-1 表示讨厌/不喜欢），
  这样 S7 才能用 ``subject+predicate`` 判“偏好正负相反”的冲突；
- 单次最多 12 条（设计文档的防编造上限），模型推断另受 3 条限制。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import db

#: 允许的主张类型。
CLAIM_TYPES = ("fact", "preference", "event", "plan", "relation", "inference")
#: 抽取阶段只产出用户原话（A）与模型推断（C）。
SOURCE_LEVELS = ("A", "C")
#: 一次抽取最多 3 条模型推断。
MAX_INFERENCES = 3
#: 单次抽取上限，防止模型贪多导致编造。
MAX_CLAIMS = 12
#: 默认置信度（模型漏填时）。
DEFAULT_CONFIDENCE = 0.6

SYSTEM_PROMPT = "你是自学习对话体的原子主张抽取器，只输出 JSON。"
PURPOSE = "selflearn_claims"

#: 契约外但常见的类型写法 → 契约内类型。
CLAIM_TYPE_ALIASES = {
    "profile": "fact",
    "attribute": "fact",
    "opinion": "preference",
    "inclination": "preference",
    "relationship": "relation",
    "intent": "plan",
    "schedule": "plan",
    "goal": "plan",
    "guess": "inference",
    "inferred": "inference",
}

#: 谓词同义词归一；偏好类统一成“喜欢”，正负由 polarity 表达。
PREDICATE_CANON = {
    "喜爱": "喜欢", "爱好": "喜欢", "爱": "喜欢", "稀饭": "喜欢",
    "讨厌": "喜欢", "厌烦": "喜欢", "不喜欢": "喜欢", "不爱": "喜欢",
    "恨": "喜欢", "不感兴趣": "喜欢",
    "居住在": "住在", "住": "住在",
    "叫做": "叫",
    "打算": "计划", "准备": "计划",
}

#: 归一后表示“负向偏好”的原始写法。
NEGATIVE_PREDICATES = frozenset(
    {"讨厌", "厌烦", "不喜欢", "不爱", "恨", "不感兴趣"}
)

_NEGATIVE_POLARITY = frozenset(
    {"-1", "-1.0", "neg", "negative", "no", "false", "否", "反", "负"}
)
_TRIM_CHARS = " \t\r\n_-—.,;:!?，。；：！？、·\"'`()（）[]【】<>《》/\\|"
_WS_RE = re.compile(r"[\s\-_]+")


@dataclass
class Claim:
    """一条可审计的原子主张。"""

    id: str
    subject: str
    predicate: str
    object: str
    polarity: int = 1
    time_ref: str = ""
    claim_type: str = "fact"
    evidence_span: str = ""
    evidence_message_id: str = ""
    confidence: float = DEFAULT_CONFIDENCE
    source_level: str = "A"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "polarity": self.polarity,
            "time_ref": self.time_ref,
            "claim_type": self.claim_type,
            "evidence_span": self.evidence_span,
            "evidence_message_id": self.evidence_message_id,
            "confidence": self.confidence,
            "source_level": self.source_level,
        }


def normalize_predicate(predicate: str) -> str:
    """归一谓词：全半角、大小写、空白/标点、同义词。

    偏好类谓词统一成 ``"喜欢"``：正负记在 :attr:`Claim.polarity`，
    这样“喜欢猫”和“讨厌猫”会落到同一个 ``subject+predicate`` 上，
    S7 才能判出“偏好正负相反”的冲突。空输入返回 ``""``。
    """
    value = unicodedata.normalize("NFKC", str(predicate or "")).strip().lower()
    value = _WS_RE.sub("_", value).strip(_TRIM_CHARS)
    if not value:
        return ""
    return PREDICATE_CANON.get(value, value)


def build_prompt(text: str) -> str:
    """构造抽取 prompt；独立出来方便测试与手工调试。"""
    types = "|".join(CLAIM_TYPES)
    return (
        "从下面这条用户原话里抽取可审计的原子主张（claims）。"
        "只抽原话明确支持的内容，不要推测；你自己的推测最多 3 条，"
        '必须标 claim_type="inference"、source_level="C"。'
        "输出 JSON 对象："
        '{"claims":[{"subject":"user",'
        '"predicate":"归一后的短谓词（如 喜欢/住在/叫/计划/养）",'
        '"object":"宾语","polarity":1 或 -1,"time_ref":"时间或空串",'
        f'"claim_type":"{types}",'
        '"evidence_span":"原话里逐字出现的一段连续文字",'
        '"confidence":0.0-1.0,"source_level":"A"}]}。'
        "evidence_span 必须是原话的子串；找不到原文的主张不要输出。"
        '没有可抽的就输出 {"claims":[]}。\n原话：' + str(text or "")
    )


def extract_claims(path: Path | str, client, text: str, message_id: str) -> list[Claim]:
    """让 LLM 从 ``text`` 抽 claim，落库后返回通过硬校验的那些。

    ``client`` 需提供 ``available()``/``chat(messages, **kwargs)``，
    与 :class:`agi.selflearn.llm.Client` 同形；也接受任何返回同样结构的
    fake client。client 不可用、报错、JSON 解析失败都只返回空列表，不抛异常。
    """
    source_text = str(text or "")
    if not source_text.strip():
        return []
    raw = _call_client(client, build_prompt(source_text))
    if not raw.strip():
        return []

    claims: list[Claim] = []
    seen: set[str] = set()
    inferences = 0
    for item in _parse_items(raw):
        claim = _build_claim(item, source_text, message_id)
        if claim is None:
            continue
        if claim.claim_type == "inference":
            if inferences >= MAX_INFERENCES:
                continue
            inferences += 1
        if claim.id in seen:
            continue
        claims.append(claim)
        seen.add(claim.id)
        if len(claims) >= MAX_CLAIMS:
            break

    _store_claims(path, claims)
    return claims


def list_claims(path: Path | str, limit: int = 100) -> list[dict]:
    """按创建时间倒序列出 claims（便于 S7/自检读取已存主张）。"""
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT * FROM claims ORDER BY created_at DESC, id LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _claim_id(*parts) -> str:
    digest = hashlib.sha1()
    for part in parts:
        digest.update(str(part).encode("utf-8", "replace"))
        digest.update(b"\x1e")
    return "C-" + digest.hexdigest()[:16]


def _confidence(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE


def _polarity(value) -> int:
    """把模型给的 polarity 收敛成 1 / -1。"""
    if isinstance(value, bool):
        return 1 if value else -1
    if isinstance(value, (int, float)):
        return -1 if value < 0 else 1
    return -1 if str(value or "").strip().lower() in _NEGATIVE_POLARITY else 1


def _predicate_sign(predicate: str) -> int:
    """原始谓词本身是否带否定（讨厌/不喜欢…）→ -1。"""
    raw = unicodedata.normalize("NFKC", str(predicate or "")).strip().lower()
    return -1 if raw in NEGATIVE_PREDICATES else 1


def _claim_type(item: dict) -> str:
    raw = str(item.get("claim_type") or item.get("type") or "fact").strip().lower()
    raw = CLAIM_TYPE_ALIASES.get(raw, raw)
    return raw if raw in CLAIM_TYPES else "fact"


def _source_level(item: dict, claim_type: str) -> tuple[str, str]:
    """返回 (claim_type, source_level)；推断一律 C/inference。"""
    raw = str(
        item.get("source_level") or item.get("source") or "A"
    ).strip().upper()
    inferred = (
        claim_type == "inference"
        or raw == "C"
        or bool(item.get("inference"))
    )
    if inferred:
        return "inference", "C"
    # 契约规定抽取阶段只有 A/C；模型给 B（规则层）也按用户原话 A 存。
    return claim_type, "A"


def _match_span(raw_evidence, text: str) -> str | None:
    """证据必须能回到原文；返回原文中的真实子串，否则 None。

    依次尝试：原样、空白折叠、忽略空白（仍要求逐字按序出现，
    返回值是原文切片，保证仍是 ``text`` 的真子串）。
    """
    value = str(raw_evidence or "")
    for candidate in (value.strip(), _clean(value)):
        if candidate and candidate in text:
            return candidate
    return _compact_span(_clean(value), text)


def _compact_span(evidence: str, text: str) -> str | None:
    """忽略空白差异定位证据，再按原位置切回原文。"""
    compact_evidence = re.sub(r"\s+", "", evidence)
    if not compact_evidence:
        return None
    positions = [index for index, char in enumerate(text) if not char.isspace()]
    if len(compact_evidence) > len(positions):
        return None
    compact_text = "".join(text[index] for index in positions)
    start = compact_text.find(compact_evidence)
    if start < 0:
        return None
    end = start + len(compact_evidence) - 1
    return text[positions[start]:positions[end] + 1]


def _build_claim(item: dict, text: str, message_id: str) -> Claim | None:
    # 兼容模型偶尔改字段名；JSON 数组里混进非对象也直接跳过。
    if not isinstance(item, dict):
        return None
    raw_predicate = (
        item.get("predicate") or item.get("relation") or item.get("attr") or ""
    )
    predicate = normalize_predicate(raw_predicate)
    obj = _clean(
        item.get("object") or item.get("obj") or item.get("value")
        or item.get("target")
    )
    subject = _clean(item.get("subject") or item.get("who") or "user") or "user"
    if not predicate or not obj:
        return None
    span = _match_span(
        item.get("evidence_span") or item.get("evidence") or item.get("span"), text
    )
    if not span:
        return None

    claim_type, source_level = _source_level(item, _claim_type(item))
    polarity = _polarity(item.get("polarity", 1)) * _predicate_sign(raw_predicate)
    time_ref = _clean(item.get("time_ref") or item.get("time") or "")
    confidence = _confidence(item.get("confidence"))
    message_id = str(message_id or "")
    claim_id = _claim_id(
        message_id, subject, predicate, obj, polarity, claim_type, source_level
    )
    return Claim(
        id=claim_id,
        subject=subject,
        predicate=predicate,
        object=obj,
        polarity=polarity,
        time_ref=time_ref,
        claim_type=claim_type,
        evidence_span=span,
        evidence_message_id=message_id,
        confidence=confidence,
        source_level=source_level,
    )


def _parse_items(raw: str) -> list[dict]:
    """从模型输出里抠出 claims 列表；任何解析失败都返回 []。"""
    data = None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        data = data.get("claims") or data.get("items") or data.get("data")
    if not isinstance(data, list):
        try:
            from .llm import parse_json

            parsed = parse_json(raw)
        except Exception:  # noqa: BLE001 - 解析失败不是错误路径
            parsed = None
        if isinstance(parsed, dict):
            data = parsed.get("claims") or parsed.get("items") or parsed.get("data")
    if not isinstance(data, list):
        # 兜底：带 ``` 围栏或前后废话时抠出第一个 JSON 数组。
        match = re.search(r"\[.*\]", str(raw or ""), re.S)
        if match:
            try:
                loaded = json.loads(match.group(0))
            except (TypeError, ValueError):
                loaded = None
            if isinstance(loaded, list):
                data = loaded
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _call_client(client, prompt: str) -> str:
    """调用可注入 client；任何异常都降级成空串（不崩）。"""
    if client is None or not callable(getattr(client, "chat", None)):
        return ""
    try:
        available = getattr(client, "available", None)
        if callable(available) and not available():
            return ""
        response = client.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            purpose=PURPOSE,
            max_tokens=700,
            temperature=0.1,
            json_mode=True,
        )
    except Exception:  # noqa: BLE001 - 网络/模型错误都按“没抽到”处理
        return ""
    if isinstance(response, dict):
        if response.get("ok") is False:
            return ""
        return str(response.get("text") or "")
    return str(response or "")


def _store_claims(path: Path | str, claims: list[Claim]) -> None:
    if not claims:
        return
    now = time.time()
    rows = [
        (
            c.id, c.subject, c.predicate, c.object, c.polarity, c.time_ref,
            c.claim_type, c.evidence_span, c.evidence_message_id,
            c.confidence, c.source_level, "active", None, now,
        )
        for c in claims
    ]
    con = db.connect(path)
    try:
        con.executemany(
            "INSERT OR IGNORE INTO claims("
            "id,subject,predicate,object,polarity,time_ref,claim_type,"
            "evidence_span,evidence_message_id,confidence,source_level,"
            "status,conflict_group,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        con.commit()
    finally:
        con.close()


__all__ = [
    "CLAIM_TYPES",
    "SOURCE_LEVELS",
    "MAX_INFERENCES",
    "MAX_CLAIMS",
    "Claim",
    "normalize_predicate",
    "build_prompt",
    "extract_claims",
    "list_claims",
]
