# -*- coding: utf-8 -*-
"""S7 认知闸门：Claim 进入事实层前的纯规则校验（不调用 LLM）。

契约见 ``BATCH1_CONTRACT.md``：``gate_claim(claim, existing)`` 返回

    {"result": "accept"|"reject"|"pending"|"merge"|"conflict",
     "reason": str, "conflict_with": str|None}

另外附带 ``reason_code``（稳定的机器可读原因）与 ``match_id``（merge/conflict
命中的已有 Claim id），方便上层落库；核心三键与契约一致。

判定顺序（先命中先返回）：

1. 结构非法（缺 subject/predicate/object、非法 polarity/claim_type/confidence）→ reject；
2. 敏感信息（身份证 / 银行卡 / 手机号 / 密码 / 精确住址）→ reject；
3. 证据缺失、占位或 ``evidence_span`` 与内容对不上 → reject；
4. ``source_level`` = C 或 ``claim_type`` = inference → pending（不进事实层）；
5. ``confidence`` < 0.6 → pending；
6. 同 subject+predicate+object → merge；
7. 同 subject+predicate 但 object 不同，或正负相反 → conflict；
8. 其余 → accept。

``claims.py`` 可能由别的 Agent 并行编写，这里不 import 它：``claim`` 既可以是
``claims.Claim`` 数据类，也可以是字段齐全的 dict；只有谓词归一化会在
``claims.normalize_predicate`` 可用时延迟借用（不可用则退回本文件的别名表）。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Mapping

#: 进入事实层的最低置信度（含 0.6 本身）。
MIN_FACT_CONFIDENCE = 0.6

#: 参与去重/冲突比对的 Claim 状态；其余（rejected/superseded…）视为历史，不参与。
ACTIVE_STATUSES = frozenset({"active"})

CLAIM_TYPES = frozenset({"fact", "preference", "event", "plan", "relation", "inference"})
SOURCE_LEVELS = frozenset({"A", "B", "C"})

_CLAIM_TYPE_ALIASES = {
    "facts": "fact", "factual": "fact", "事实": "fact", "陈述": "fact",
    "pref": "preference", "偏好": "preference", "喜好": "preference",
    "事件": "event", "计划": "plan", "打算": "plan",
    "关系": "relation", "推断": "inference", "推测": "inference",
}

_SOURCE_LEVEL_ALIASES = {
    "用户": "A", "用户原话": "A", "原话": "A", "user": "A",
    "规则": "B", "规则推导": "B", "rule": "B",
    "模型": "C", "模型推断": "C", "推断": "C", "model": "C", "llm": "C",
}

# 英文谓词 -> 中文规范谓词：仅在 claims.normalize_predicate 不可用时兜底，
# 保证 "likes"/"like" 这类写法不会被当成两个谓词。
_PREDICATE_ALIASES = {
    "like": "喜欢", "likes": "喜欢", "liked": "喜欢", "love": "喜欢",
    "loves": "喜欢", "enjoy": "喜欢", "enjoys": "喜欢", "prefer": "喜欢",
    "prefers": "喜欢", "favorite": "喜欢", "favourite": "喜欢", "fav": "喜欢",
    "dislike": "讨厌", "dislikes": "讨厌", "hate": "讨厌", "hates": "讨厌",
    "live": "住在", "lives": "住在", "live_in": "住在", "lives_in": "住在",
    "reside": "住在", "resides_in": "住在", "based_in": "住在",
    "work": "工作", "works": "工作", "work_at": "工作", "works_at": "工作",
    "job": "工作", "occupation": "职业",
    "name": "名字", "called": "名字", "nickname": "昵称",
    "birthday": "生日", "born": "生日", "birth": "生日", "age": "年龄",
    "want": "想要", "wants": "想要", "plan": "计划", "plans": "计划",
    "allergic_to": "过敏", "allergy": "过敏",
    "eat": "吃", "eats": "吃", "drink": "喝", "drinks": "喝",
    "study": "学习", "studies": "学习", "learn": "学习",
    "use": "使用", "uses": "使用",
}

# 证据占位符：这些“原文”等于没给证据。
_PLACEHOLDER_SPANS = frozenset({
    "", "-", "--", "…", "...", "……", "无", "没有", "无证据", "未知",
    "n/a", "na", "none", "null", "nil", "tbd", "待定",
})

_TRIM_CHARS = "。，、,.!！?？;；:：\"'“”‘’()（）[]【】{}<>《》 \t\r\n"

# 主体别名：claims.py 抽不到 subject 时默认 "user"，手工构造时常写“用户”；
# 两边统一到“用户”，否则同一件事会因主体写法不同而漏掉合并/冲突。
_SUBJECT_ALIASES = frozenset({"user", "users", "我", "本人", "自己", "用户"})

# ---------------------------------------------------------------------------
# 敏感信息
# ---------------------------------------------------------------------------

# 18 位身份证：校验出生日期段，避免把普通 18 位流水号误判；不校验校验位。
_ID_CARD = re.compile(
    r"(?<!\d)[1-9]\d{5}"
    r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
    r"\d{3}[\dXx](?!\d)"
)
# 银行卡：16-19 位，允许四位分隔（空格或连字符）。
_BANK_CARD = re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4,7}(?!\d)")
# 手机号：11 位，允许 3-4-4 分隔。
_PHONE = re.compile(r"(?<!\d)1[3-9]\d(?:[ -]?\d{4}){2}(?!\d)")
# 凭据类关键词：密码/口令/验证码，出现即视为敏感（不问具体值）。
_PASSWORD = re.compile(r"(密码|口令|passwd|password|pwd|验证码|动态码|短信码)", re.IGNORECASE)
# 精确住址：门牌号、楼栋单元室，或“住在/地址…+数字门牌”。
_ADDR_ROAD = re.compile(r"(?:路|街|道|巷|大道|大街)\s*\d+\s*(?:号|弄)")
_ADDR_BUILDING = re.compile(
    r"(?:小区|花园|公寓|大厦|苑|家园|新村|路|街|道|巷).{0,12}?"
    r"\d+\s*(?:栋|幢|单元|号楼|楼|层|室|房|户)"
)
_ADDR_RESIDENCE = re.compile(
    r"(?:住在|家住|住址|地址|搬到|租在|家在|定位在).{0,20}?"
    r"\d+\s*(?:号|栋|幢|单元|楼|层|室|房|户)"
)

_SENSITIVE_CHECKS = (
    ("id_card", "身份证号", _ID_CARD),
    ("bank_card", "银行卡号", _BANK_CARD),
    ("phone", "手机号", _PHONE),
    ("password", "密码/验证码", _PASSWORD),
    ("address", "精确住址", _ADDR_RESIDENCE),
    ("address", "精确住址", _ADDR_ROAD),
    ("address", "精确住址", _ADDR_BUILDING),
)

# 谓词归一化：claims.py 可能还不存在，延迟探测一次后缓存。
_UNSET = object()
_EXTERNAL_NORMALIZER: Any = _UNSET


def _external_normalizer() -> Any:
    """返回 ``claims.normalize_predicate``；不可用时返回 None（只探测一次）。"""
    global _EXTERNAL_NORMALIZER
    if _EXTERNAL_NORMALIZER is _UNSET:
        try:
            from .claims import normalize_predicate as _normalize
        except Exception:  # 文件还没写、写坏了、或单独加载模块都不影响闸门
            _EXTERNAL_NORMALIZER = None
        else:
            _EXTERNAL_NORMALIZER = _normalize if callable(_normalize) else None
    return _EXTERNAL_NORMALIZER


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """同时支持 dict / sqlite3.Row / 数据类 / 普通对象。"""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    if isinstance(obj, (str, bytes)):
        return default
    try:
        return getattr(obj, name)
    except AttributeError:
        pass
    except Exception:
        return default
    try:
        return obj[name]
    except (KeyError, IndexError, TypeError, AttributeError):
        return default


_MISSING = object()


def _clean(value: Any) -> str:
    """NFKC 规范化后去首尾空白；None -> ""。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return unicodedata.normalize("NFKC", str(value)).strip()


def _canon_key(value: Any) -> str:
    """去重/比对用的键：NFKC + 小写 + 去掉全部空白和句读。"""
    text = _clean(value).lower()
    text = re.sub(r"\s+", "", text)
    return text.strip(_TRIM_CHARS)


def _norm_text(value: Any) -> str:
    """证据比对用文本：只做 NFKC，保留内部字符。"""
    return unicodedata.normalize("NFKC", _clean(value))


def _parse_polarity(value: Any) -> int | None:
    """返回 1 / -1；非法返回 None。缺省（None）按 1 处理。"""
    if value is None:
        return 1
    if isinstance(value, bool):
        return 1 if value else -1
    if isinstance(value, (int, float)):
        if value == 1:
            return 1
        if value == -1:
            return -1
        return None
    mapping = {
        "1": 1, "+1": 1, "positive": 1, "pos": 1, "true": 1, "yes": 1,
        "正": 1, "赞成": 1, "喜欢": 1,
        "-1": -1, "-": -1, "negative": -1, "neg": -1, "false": -1, "no": -1,
        "负": -1, "反对": -1, "不喜欢": -1,
    }
    return mapping.get(_canon_key(value))


def _parse_confidence(value: Any) -> float | None:
    """返回 [0,1] 内置信度；缺省按 DB 默认 0.6；非法返回 None。"""
    if value is None:
        return MIN_FACT_CONFIDENCE
    if isinstance(value, str) and not value.strip():
        return MIN_FACT_CONFIDENCE
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if confidence != confidence or not (0.0 <= confidence <= 1.0):  # NaN / 越界
        return None
    return confidence


def _parse_claim_type(value: Any) -> str | None:
    raw = _clean(value)
    if not raw:
        return "fact"
    key = _canon_key(raw)
    if key in CLAIM_TYPES:
        return key
    return _CLAIM_TYPE_ALIASES.get(key)


def _parse_source_level(value: Any) -> tuple[str, bool]:
    """返回 (level, 是否可识别)；缺省按 A 处理。"""
    raw = _clean(value)
    if not raw:
        return "A", True
    upper = raw.upper()
    if upper in SOURCE_LEVELS:
        return upper, True
    alias = _SOURCE_LEVEL_ALIASES.get(_canon_key(raw))
    if alias:
        return alias, True
    head = upper.split("(")[0].split("（")[0].strip()
    if head in SOURCE_LEVELS:
        return head, True
    return upper, False


def _canon_subject(value: Any) -> str:
    """主体归一：user / 我 / 本人 / 自己 都视为同一个用户。"""
    key = _canon_key(value)
    return "用户" if key in _SUBJECT_ALIASES else key


def _canon_predicate(value: Any) -> str:
    """谓词归一化：优先借用 claims.normalize_predicate，再套本地别名。"""
    raw = "" if value is None else str(value)
    key = _canon_key(raw)
    normalizer = _external_normalizer()
    if normalizer is not None:
        try:
            result = normalizer(raw)
        except Exception:
            result = None
        if isinstance(result, str) and result.strip():
            key = _canon_key(result)
    alias = _PREDICATE_ALIASES.get(key) or _PREDICATE_ALIASES.get(key.replace("-", "_"))
    return alias or key


def sensitive_kinds(text: str) -> list[str]:
    """返回文本命中的敏感类型（去重、按优先级）；空列表代表干净。"""
    normalized = _norm_text(text)
    if not normalized:
        return []
    kinds: list[str] = []
    for code, _label, pattern in _SENSITIVE_CHECKS:
        if code in kinds:
            continue
        if pattern.search(normalized):
            kinds.append(code)
    return kinds


def _sensitive_hit(text: str) -> tuple[str, str] | None:
    for code, label, pattern in _SENSITIVE_CHECKS:
        if pattern.search(text):
            return code, label
    return None


# ---------------------------------------------------------------------------
# 证据
# ---------------------------------------------------------------------------

_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_STOP_TOKENS = frozenset({
    "用户", "自己", "我的", "这个", "那个", "一下", "觉得", "可以",
    "就是", "还是", "已经", "什么", "怎么",
})


def _tokens(text: str) -> set[str]:
    """中文 bigram + ASCII 词，够用且不引入分词依赖。"""
    normalized = _canon_key(text)
    tokens: set[str] = set(_ASCII_WORD.findall(normalized))
    for run in _CJK_RUN.findall(normalized):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return {token for token in tokens if token and token not in _STOP_TOKENS}


def _is_placeholder(span: str) -> bool:
    key = span.strip().lower()
    if key in _PLACEHOLDER_SPANS:
        return True
    return not re.search(r"[0-9a-z\u4e00-\u9fff]", key)


def _span_matches_claim(span: str, obj: str) -> bool:
    """没有原文可对照时：evidence_span 至少要能对上 object 的关键 token。"""
    span_key = _canon_key(span)
    obj_key = _canon_key(obj)
    if not obj_key:
        return False
    if obj_key in span_key:
        return True
    obj_tokens = {token for token in _tokens(obj_key) if len(token) >= 2}
    if not obj_tokens:
        return False
    return bool(obj_tokens & _tokens(span_key))


def _source_text_of(claim: Any, override: str | None) -> str:
    if override is not None:
        return str(override)
    for name in ("source_text", "evidence_text", "text"):
        value = _field(claim, name, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


# ---------------------------------------------------------------------------
# 与已有 Claim 比对
# ---------------------------------------------------------------------------

def _iter_claims(existing: Any) -> tuple[Any, ...]:
    if existing is None:
        return ()
    # Mapping / sqlite3.Row（有 keys 但不是 Mapping）/ 单个 Claim 都按单条处理。
    if isinstance(existing, Mapping) or isinstance(existing, (str, bytes)):
        return (existing,)
    if hasattr(existing, "keys"):
        return (existing,)
    try:
        return tuple(existing)
    except TypeError:
        return (existing,)


def _ident(item: Any) -> str:
    ident = _field(item, "id", None)
    if ident is not None and str(ident).strip():
        return str(ident)
    return "{}|{}|{}".format(
        _field(item, "subject", ""), _field(item, "predicate", ""), _field(item, "object", ""))


def _match_existing(
    subject_key: str,
    predicate_key: str,
    object_key: str,
    polarity: int,
    existing: Any,
) -> tuple[str | None, Any, str | None]:
    """返回 (动作, 命中的已有 Claim, 冲突细分)；对象相同优先于对象不同。"""
    candidates: list[Any] = []
    for item in _iter_claims(existing):
        if _field(item, "subject", _MISSING) is _MISSING:
            continue
        status = _canon_key(_field(item, "status", "active")) or "active"
        if status not in ACTIVE_STATUSES:
            continue
        # 只有事实层里的 Claim 才参与合并/冲突：C 级、低置信度、非事实类型都跳过。
        level, _known = _parse_source_level(_field(item, "source_level", "A"))
        if level == "C":
            continue
        if _parse_claim_type(_field(item, "claim_type", "fact")) == "inference":
            continue
        existing_conf = _parse_confidence(_field(item, "confidence", None))
        if existing_conf is None or existing_conf < MIN_FACT_CONFIDENCE:
            continue
        if _canon_subject(_field(item, "subject", "")) != subject_key:
            continue
        if _canon_predicate(_field(item, "predicate", "")) != predicate_key:
            continue
        candidates.append(item)

    for item in candidates:
        if _canon_key(_field(item, "object", "")) != object_key:
            continue
        if _parse_polarity(_field(item, "polarity", 1)) == polarity:
            return "merge", item, "same"
        return "conflict", item, "polarity"
    if candidates:
        return "conflict", candidates[0], "object"
    return None, None, None


# ---------------------------------------------------------------------------
# 结果构造
# ---------------------------------------------------------------------------

def _result(
    result: str,
    code: str,
    reason: str,
    conflict_with: str | None = None,
    match_id: str | None = None,
) -> dict[str, Any]:
    return {
        "result": result,
        "reason": reason,
        "reason_code": code,
        "conflict_with": conflict_with,
        "match_id": match_id,
    }


def _accept() -> dict[str, Any]:
    return _result("accept", "accepted", "证据、来源与置信度均通过，进入事实层")


def _reject(code: str, reason: str) -> dict[str, Any]:
    return _result("reject", code, reason)


def _pending(code: str, reason: str) -> dict[str, Any]:
    return _result("pending", code, reason)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def gate_claim(
    claim: Any,
    existing: Iterable[Any] | None = None,
    *,
    source_text: str | None = None,
) -> dict[str, Any]:
    """S7 认知闸门：一条 Claim 是否可进事实层，或该合并/报冲突。

    参数
    ----
    claim:
        契约里的 ``Claim`` 数据类，或字段齐全的 dict（subject / predicate /
        object / polarity / claim_type / evidence_span / confidence /
        source_level）。可选携带 ``source_text`` 原文用于严格证据比对。
    existing:
        已有 Claim 列表（dict / sqlite3.Row / 数据类均可）；只比对
        ``status == active`` 且非 C 级的记录。传单个对象也接受。
    source_text:
        可选原文；给了就要求 ``evidence_span`` 是原文子串，否则退回
        “span 与 object 对得上”的宽松校验。

    返回
    ----
    dict，含契约三键 ``result`` / ``reason`` / ``conflict_with``；另附
    ``reason_code`` 与 ``match_id`` 便于落库（merge/conflict 时非空）。
    """
    if claim is None or isinstance(claim, (str, bytes)):
        return _reject("invalid_claim", "不是合法的 Claim：需要对象或 dict")

    subject = _clean(_field(claim, "subject", ""))
    if not subject:
        return _reject("missing_subject", "缺少 subject，丢弃")

    predicate = _clean(_field(claim, "predicate", ""))
    if not predicate:
        return _reject("missing_predicate", "缺少 predicate，丢弃")

    obj = _clean(_field(claim, "object", ""))
    if not obj:
        return _reject("missing_object", "缺少 object，丢弃")

    polarity = _parse_polarity(_field(claim, "polarity", None))
    if polarity is None:
        return _reject(
            "invalid_polarity",
            "polarity 非法：{!r}，只能是 1 / -1".format(_field(claim, "polarity", None)),
        )

    claim_type = _parse_claim_type(_field(claim, "claim_type", None))
    if claim_type is None:
        return _reject(
            "invalid_claim_type",
            "claim_type 非法：{!r}，只能是 {}".format(
                _field(claim, "claim_type", None), "|".join(sorted(CLAIM_TYPES))),
        )

    confidence = _parse_confidence(_field(claim, "confidence", None))
    if confidence is None:
        return _reject(
            "invalid_confidence",
            "confidence 非法：{!r}，应为 0~1 的数字".format(_field(claim, "confidence", None)),
        )

    level, level_known = _parse_source_level(_field(claim, "source_level", None))

    span = _clean(_field(claim, "evidence_span", ""))

    # 1) 敏感信息：哪怕证据/来源有问题，也先按隐私拒掉，绝不进 pending。
    blob = "\n".join((subject, predicate, obj, span))
    sensitive = _sensitive_hit(unicodedata.normalize("NFKC", blob))
    if sensitive is not None:
        code, label = sensitive
        return _reject("sensitive_" + code, "敏感信息（{}）拒绝进入事实层".format(label))

    # 2) 证据存在性 + evidence_span 匹配。
    if not span:
        return _reject("evidence_missing", "证据缺失：evidence_span 为空，丢弃")
    if _is_placeholder(span):
        return _reject("evidence_placeholder", "证据无效：evidence_span 只是占位内容")
    source = _source_text_of(claim, source_text)
    if source:
        if _canon_key(span) not in _canon_key(source):
            return _reject(
                "evidence_span_not_in_source",
                "证据不匹配：evidence_span 不是原文子串",
            )
    elif not _span_matches_claim(span, obj):
        return _reject(
            "evidence_span_mismatch",
            "证据不匹配：evidence_span 与 claim 内容（object）对不上",
        )

    # 3) 来源与置信度：不满足就挂起，不进事实层。
    if not level_known:
        return _pending(
            "unknown_source_level",
            "未知 source_level={!r}，不能当事实，转待定".format(
                _field(claim, "source_level", None)),
        )
    if level == "C":
        return _pending(
            "source_level_c",
            "source_level=C（模型推断）不进事实层，转待定",
        )
    if claim_type == "inference":
        return _pending(
            "inference_type",
            "claim_type=inference 不进事实层，转待定",
        )
    if confidence < MIN_FACT_CONFIDENCE:
        return _pending(
            "low_confidence",
            "置信度 {:.2f} < {:.2f}，转待定".format(confidence, MIN_FACT_CONFIDENCE),
        )

    # 4) 去重与冲突。
    subject_key = _canon_subject(subject)
    predicate_key = _canon_predicate(predicate)
    object_key = _canon_key(obj)
    action, item, detail = _match_existing(
        subject_key, predicate_key, object_key, polarity, existing)
    if action == "merge":
        ident = _ident(item)
        return _result(
            "merge", "merged_duplicate",
            "与已有 Claim「{}」subject+predicate+object 相同，合并".format(ident),
            match_id=ident,
        )
    if action == "conflict":
        ident = _ident(item)
        if detail == "polarity":
            reason = "与已有 Claim「{}」冲突：同 subject+predicate+object 但正负相反".format(ident)
            code = "conflict_polarity"
        else:
            reason = "与已有 Claim「{}」冲突：同 subject+predicate 但 object 不同（{} ≠ {}）".format(
                ident, _clean(_field(item, "object", "")), obj)
            code = "conflict_object"
        return _result("conflict", code, reason, conflict_with=ident, match_id=ident)

    return _accept()


__all__ = [
    "ACTIVE_STATUSES",
    "CLAIM_TYPES",
    "MIN_FACT_CONFIDENCE",
    "gate_claim",
    "sensitive_kinds",
]
