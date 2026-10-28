# -*- coding: utf-8 -*-
"""用户标注后端：把「人工打分/改标签」的 InnerState 标注转成 soul 训练 JSONL。

背景：W4 的 soul 蒸馏原来是 teacher 标注；用户要改成**用户标注**——界面上看回复、
打分、改 InnerState 字段，再导出成可直接训练的数据集（想训成什么样就什么样）。

三个纯函数 + 一个数据源视图（不依赖 FastAPI；Lead 负责接线到 main.py）：

- :func:`suggest_annotation`  按回复文本预填 mood/energy/stage/stance/intent/behavior，
  返回 ``{target, reasons}``（reasons 给 UI 展示“为什么这样建议”）。
- :func:`validate_annotation` 按 ``SoulAgent/agi/soul/inner_state_schema.json`` 的枚举/范围
  收敛：数值 clamp 0-1、枚举非法回默认、文本截断；未知字段/必填缺失算 error。
- :func:`save_annotations`    过滤 score、组装 soul 行（context + user_msg + target），
  写到 ``E:/agent/datasets/soul_user_<时间>.jsonl``（+ ``_val.jsonl``），写审计 ``annot.save``。
- :func:`source_view`         给 ``GET /api/wb/annot/source/{job_id}`` 用：复用
  corpusstudio 已清洗 items，返回每条 me 回复的上下文 + 上一句 you + 预填建议。

数据源（按顺序尝试）：
1. ``app.corpusstudio`` 的 ``load_items(job_id)`` / ``get_items(job_id)``（同事在写；
   不存在/没就绪 → ``{"ok": False, "error": "corpus_backend_not_ready"}``）；
2. ``idea/.data/corpus/<job_id>/items.json``（corpusstudio 的落盘缓存）；
3. 直接传 JSONL path（messages / 轮对 / ``{user,assistant}`` 格式）或内存 items 列表。

路由契约（由 Lead 接线）::

    GET  /api/wb/annot/source/{job_id}
         -> source_view(job_id) = {ok, total, items:[{id,text,prev_user_text,context,suggestion}]}
    POST /api/wb/annot/save {source, annotations, window, out_name, min_score, split, system}
         -> save_annotations(...) = {ok, out, val_out, rows, val_rows, skipped, min_score}
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import audit, paths

# --------------------------------------------------------------------------- #
# soul InnerState schema（默认值 + 枚举；SoulAgent 仓库里有 JSON schema 时以其为准）
# --------------------------------------------------------------------------- #
STAGES = ("陌生", "初识", "熟悉", "亲近", "信任")
LENGTHS = ("short", "normal", "long")
EMOJI_VALUES = ("偶尔", "不用")
STANCES = ("平和", "温和", "简短直接", "客气", "克制", "有点累", "先照顾人")
INTENTS = (
    "自然接话",
    "自然接话，可以多聊一句",
    "先听对方说完",
    "不追问，答完就好",
    "简短回应，不展开",
    "接住对方的话，给一句具体建议",
    "确认收到，追问一句进度",
    "先确认对方安全",
)
DEFAULT_STATE = {"mood": 0.55, "energy": 0.6, "sharing": 0.45,
                 "battery": 0.7, "trust": 0.5, "stage": "初识"}
DEFAULT_BEHAVIOR = {"length": "normal", "ask": True,
                    "advice": False, "emoji": "不用"}
TARGET_FIELDS = ("mood", "energy", "sharing", "battery", "trust", "stage",
                 "stance", "intent", "monologue", "behavior")
SCHEMA_REL = Path("agi") / "soul" / "inner_state_schema.json"

#: 简单情绪词典（只做“预填建议”，最终由用户改）
NEGATIVE_WORDS = ("累", "烦", "难过", "伤心", "焦虑", "压力", "崩溃", "委屈",
                  "失望", "孤独", "生气", "气死", "不想", "难受", "痛", "哭")
POSITIVE_WORDS = ("开心", "高兴", "哈哈", "笑", "喜欢", "谢谢", "太好了", "不错")
CRISIS_WORDS = ("不想活", "自杀", "轻生", "自伤", "绝望", "告别", "活不下去", "想死")
ADVICE_WORDS = ("建议", "应该", "试试", "别", "记得", "可以", "要不", "不如", "最好")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]"
)
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# --------------------------------------------------------------------------- #
# schema 读取（缓存）
# --------------------------------------------------------------------------- #
_SCHEMA_CACHE: dict[str, dict] = {}


def _schema_path() -> Path:
    return paths.soulagent_home() / SCHEMA_REL


def _load_schema() -> dict:
    """读 SoulAgent 的 inner_state_schema.json；读不到用内置枚举（不抛异常）。"""
    path = str(_schema_path())
    if path in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[path]
    schema: dict = {}
    try:
        schema = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 测试/独立环境没有 SoulAgent 时用内置
        schema = {}
    _SCHEMA_CACHE[path] = schema
    return schema


def _enum(name: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    try:
        values = _load_schema()["properties"][name].get("enum")
        if isinstance(values, list) and values:
            return tuple(str(v) for v in values)
    except Exception:  # noqa: BLE001
        pass
    return fallback


def _behavior_enum(field: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    try:
        values = _load_schema()["properties"]["behavior"]["properties"][field].get("enum")
        if isinstance(values, list) and values:
            return tuple(str(v) for v in values)
    except Exception:  # noqa: BLE001
        pass
    return fallback


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, number))


def _clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    return text[:limit]


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in ("1", "true", "yes", "y", "on", "是", "要", "可以"):
            return True
        if raw in ("0", "false", "no", "n", "off", "否", "不要", "不用"):
            return False
    return default


def _mean(items: Iterable[dict], role: str) -> str:
    """取最近一条指定 role 的原文（用于 prev_user_text）。"""
    for item in reversed(list(items)):
        if isinstance(item, dict) and str(item.get("role") or "") == role:
            return str(item.get("text") or "")
    return ""


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# --------------------------------------------------------------------------- #
# 1) 自动预填建议
# --------------------------------------------------------------------------- #
def suggest_annotation(reply_text: str, *, prev_user_text: str = "",
                       context: list | None = None) -> dict:
    """按回复文本 + 上一句用户话预填 InnerState。

    返回 ``{target: {10 字段}, reasons: [...]}``；所有规则都是启发式，UI 允许用户改。
    """
    text = str(reply_text or "").strip()
    prev = str(prev_user_text or "").strip()
    ctx = list(context or [])
    length = len(text)
    reasons: list[str] = []

    target: dict[str, Any] = dict(DEFAULT_STATE)
    target.update({
        "stance": "平和",
        "intent": "自然接话",
        "monologue": "",
        "behavior": dict(DEFAULT_BEHAVIOR),
    })
    reasons.append(
        f"默认状态 mood={DEFAULT_STATE['mood']:.2f} energy={DEFAULT_STATE['energy']:.2f} "
        f"sharing={DEFAULT_STATE['sharing']:.2f} battery={DEFAULT_STATE['battery']:.2f} "
        f"trust={DEFAULT_STATE['trust']:.2f}")

    crisis = any(word in text or word in prev for word in CRISIS_WORDS)
    negative = [word for word in NEGATIVE_WORDS if word in text or word in prev]
    positive = [word for word in POSITIVE_WORDS if word in text]

    # ---- 数值字段（默认 + 词典微调）----
    mood = float(DEFAULT_STATE["mood"])
    energy = float(DEFAULT_STATE["energy"])
    sharing = float(DEFAULT_STATE["sharing"])
    battery = float(DEFAULT_STATE["battery"])
    trust = float(DEFAULT_STATE["trust"])
    if negative:
        mood -= 0.15
        energy -= 0.05
        reasons.append(f"文本/上一句出现负面词（{'/'.join(negative[:3])}）→ mood-0.15、energy-0.05")
    if positive:
        mood += 0.08
        energy += 0.05
        reasons.append(f"文本出现正面词（{'/'.join(positive[:3])}）→ mood+0.08、energy+0.05")
    if text.count("!") + text.count("！") >= 2:
        energy += 0.08
        reasons.append("多个感叹号 → energy+0.08")
    if _EMOJI_RE.search(text):
        energy += 0.05
        reasons.append("回复含 emoji → energy+0.05")
    if any(word in prev for word in ("谢谢", "相信", "喜欢", "靠你了")):
        trust += 0.08
        reasons.append("上一句表达信任/感谢 → trust+0.08")
    if len(ctx) >= 12:
        trust += 0.05
        reasons.append(f"上下文 {len(ctx)} 轮 → trust+0.05（关系更熟）")
    if length > 45:
        sharing += 0.1
        reasons.append("回复 >45 字 → sharing+0.10（愿意多说）")
    elif length < 14:
        sharing -= 0.05
        reasons.append("回复 <14 字 → sharing-0.05（话少）")
    if crisis:
        mood = 0.2
        energy = 0.3
        sharing = 0.2
        reasons.append("检测到危机信号 → mood/energy/sharing 压到低位")

    # ---- stage：按上下文深度推断 ----
    if len(ctx) >= 16:
        stage = "信任" if trust >= 0.75 else "亲近"
    elif len(ctx) >= 8:
        stage = "熟悉"
    elif len(ctx) >= 2:
        stage = "初识"
    else:
        stage = "陌生"
    reasons.append(f"上下文 {len(ctx)} 轮 → stage={stage}")

    # ---- behavior ----
    behavior = dict(DEFAULT_BEHAVIOR)
    if length < 14:
        behavior["length"] = "short"
        reasons.append(f"回复 {length} 字 <14 → behavior.length=short")
    elif length > 45:
        behavior["length"] = "long"
        reasons.append(f"回复 {length} 字 >45 → behavior.length=long")
    else:
        behavior["length"] = "normal"
        reasons.append(f"回复 {length} 字在 14–45 → behavior.length=normal")
    ask = text.endswith("?") or text.endswith("？")
    behavior["ask"] = ask
    reasons.append(f"结尾{'是' if ask else '不是'}问号 → behavior.ask={ask}")
    advice = any(word in text for word in ADVICE_WORDS)
    behavior["advice"] = advice
    reasons.append(f"回复{'含' if advice else '不含'}建议词（建议/应该/试试/别…）→ behavior.advice={advice}")
    emoji = "偶尔" if _EMOJI_RE.search(text) else "不用"
    behavior["emoji"] = emoji
    reasons.append(f"回复{'含' if emoji == '偶尔' else '不含'} emoji → behavior.emoji={emoji}")

    # ---- stance / intent ----
    if crisis:
        stance, intent = "先照顾人", "先确认对方安全"
        behavior = {"length": "short", "ask": False, "advice": False, "emoji": "不用"}
        reasons.append("危机信号 → stance=先照顾人、intent=先确认对方安全，"
                       "behavior 固定 short/ask=false/advice=false/emoji=不用")
    elif negative and mood < 0.35:
        stance = "有点累"
        intent = "先听对方说完"
        reasons.append("负面且 mood 偏低 → stance=有点累、intent=先听对方说完")
    elif length < 14:
        stance = "简短直接"
        intent = "简短回应，不展开"
        reasons.append("回复很短 → stance=简短直接、intent=简短回应，不展开")
    elif advice:
        stance = "温和"
        intent = "接住对方的话，给一句具体建议"
        reasons.append("含建议词 → stance=温和、intent=给一句具体建议")
    elif ask:
        stance = "平和"
        intent = "自然接话，可以多聊一句"
        reasons.append("以问句收尾 → stance=平和、intent=自然接话，可以多聊一句")
    else:
        stance = "平和"
        intent = "自然接话"
        reasons.append("无明显信号 → stance=平和、intent=自然接话")

    target = {
        "mood": round(max(0.0, min(1.0, mood)), 3),
        "energy": round(max(0.0, min(1.0, energy)), 3),
        "sharing": round(max(0.0, min(1.0, sharing)), 3),
        "battery": round(max(0.0, min(1.0, battery)), 3),
        "trust": round(max(0.0, min(1.0, trust)), 3),
        "stage": stage,
        "stance": stance,
        "intent": intent,
        "monologue": _default_monologue(stance, text, crisis),
        "behavior": behavior,
    }
    reasons.append(f"monologue 预填：{target['monologue'] or '（空，可手填）'}")
    return {"target": target, "reasons": reasons}


def _default_monologue(stance: str, text: str, crisis: bool) -> str:
    """给一句 ≤40 字的内心独白建议（第一人称、不复述用户原话）。"""
    if crisis:
        return "先别急着讲道理，确认他安全"
    return {
        "简短直接": "不想多解释，说重点",
        "有点累": "有点累，不想接梗",
        "温和": "先接住，再说具体怎么办",
        "平和": "正常聊就好",
    }.get(stance, "先听，再问一句")


# --------------------------------------------------------------------------- #
# 2) 校验 / 收敛
# --------------------------------------------------------------------------- #
def validate_annotation(target: Any) -> dict:
    """按 soul InnerState schema 校验并收敛。

    返回 ``{ok, errors, warnings, target}``：

    - **errors**（致命，ok=False）：不是 dict；未知字段；缺 `behavior` 或 behavior 非 dict；
      数值字段无法转 float；stance/intent/monologue 类型不可转字符串；
    - **warnings**（已收敛，ok 仍为 True）：数值越界 clamp、stage/emoji/length 非法回默认、
      ask/advice 无法解释为布尔、文本截断、behavior 未知子字段被丢弃。
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(target, dict):
        return {"ok": False, "errors": ["target 必须是 JSON 对象"],
                "warnings": [], "target": _default_target()}

    out: dict[str, Any] = _default_target()
    unknown = [str(key) for key in target if str(key) not in TARGET_FIELDS]
    if unknown:
        errors.append(f"未知字段：{', '.join(sorted(unknown))}")

    for key in ("mood", "energy", "sharing", "battery", "trust"):
        if key not in target:
            errors.append(f"缺少必填字段：{key}")
            continue
        try:
            number = float(target[key])
        except (TypeError, ValueError):
            errors.append(f"{key} 必须是数值，收到 {target[key]!r}")
            continue
        if number < 0 or number > 1:
            warnings.append(f"{key}={number} 越界，已 clamp 到 [0,1]")
        out[key] = round(max(0.0, min(1.0, number)), 3)

    if "stage" not in target:
        errors.append("缺少必填字段：stage")
    else:
        stage = _clean_text(target["stage"], 20)
        if stage not in _enum("stage", STAGES):
            warnings.append(f"stage={target['stage']!r} 不在枚举内，回默认 {DEFAULT_STATE['stage']}")
            out["stage"] = DEFAULT_STATE["stage"]
        else:
            out["stage"] = stage

    for key, limit, default in (("stance", 30, "平和"), ("intent", 60, "自然接话"),
                                ("monologue", 40, "")):
        if key not in target:
            errors.append(f"缺少必填字段：{key}")
            continue
        value = target[key]
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            errors.append(f"{key} 必须是字符串，收到 {value!r}")
            continue
        text = _clean_text(value, limit + 1)
        if len(text) > limit:
            warnings.append(f"{key} 超过 {limit} 字，已截断")
            text = text[:limit]
        if key == "monologue":
            text = text.replace("\n", " ")
        out[key] = text or default

    if "behavior" not in target:
        errors.append("缺少必填字段：behavior")
    elif not isinstance(target["behavior"], dict):
        errors.append("behavior 必须是 JSON 对象")
    else:
        out["behavior"] = _validate_behavior(target["behavior"], warnings)

    return {"ok": not errors, "errors": errors, "warnings": warnings, "target": out}


def _validate_behavior(raw: dict, warnings: list[str]) -> dict:
    lengths = _behavior_enum("length", LENGTHS)
    emojis = _behavior_enum("emoji", EMOJI_VALUES)
    out = dict(DEFAULT_BEHAVIOR)
    unknown = [str(key) for key in raw if str(key) not in ("length", "ask", "advice", "emoji")]
    if unknown:
        warnings.append(f"behavior 未知子字段已丢弃：{', '.join(sorted(unknown))}")
    length = _clean_text(raw.get("length", DEFAULT_BEHAVIOR["length"]), 20)
    if length not in lengths:
        warnings.append(f"behavior.length={raw.get('length')!r} 非法，回 normal")
        length = "normal"
    out["length"] = length
    for key in ("ask", "advice"):
        default = bool(DEFAULT_BEHAVIOR[key])
        value = raw.get(key, default)
        if not isinstance(value, (bool, int, float, str)):
            warnings.append(f"behavior.{key}={value!r} 无法解释为布尔，回默认 {default}")
            out[key] = default
        else:
            out[key] = _as_bool(value, default)
    emoji = _clean_text(raw.get("emoji", DEFAULT_BEHAVIOR["emoji"]), 20)
    if emoji not in emojis:
        warnings.append(f"behavior.emoji={raw.get('emoji')!r} 非法，回 不用")
        emoji = "不用"
    out["emoji"] = emoji
    return out


def _default_target() -> dict:
    return {
        "mood": DEFAULT_STATE["mood"], "energy": DEFAULT_STATE["energy"],
        "sharing": DEFAULT_STATE["sharing"], "battery": DEFAULT_STATE["battery"],
        "trust": DEFAULT_STATE["trust"], "stage": DEFAULT_STATE["stage"],
        "stance": "平和", "intent": "自然接话", "monologue": "",
        "behavior": dict(DEFAULT_BEHAVIOR),
    }


# --------------------------------------------------------------------------- #
# 3) 数据源：corpus items / JSONL / 内存
# --------------------------------------------------------------------------- #
def _items_from_messages(messages: list) -> list[dict]:
    """messages 列表（role/content）→ corpus items（me/you）。"""
    items: list[dict] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").lower()
        if role == "user":
            item_role = "you"
        elif role in ("assistant", "ai"):
            item_role = "me"
        else:
            continue
        content = message.get("content")
        if isinstance(content, list):  # 多段 content
            content = "".join(
                str(part.get("text") or "") for part in content
                if isinstance(part, dict))
        text = str(content or "").strip()
        if text:
            items.append({"role": item_role, "text": text})
    return items


def _items_from_jsonl(path: Path) -> list[dict]:
    """读 messages JSONL / 轮对 JSONL → items。"""
    items: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, list):
            items.extend(_items_from_messages(row))
        elif isinstance(row, dict):
            if isinstance(row.get("messages"), list):
                items.extend(_items_from_messages(row["messages"]))
            elif "user" in row and "assistant" in row:
                items.extend(_items_from_messages(
                    [{"role": "user", "content": row["user"]},
                     {"role": "assistant", "content": row["assistant"]}]))
            elif "role" in row and "text" in row:
                items.extend(_items_from_messages(
                    [{"role": row["role"], "content": row["text"]}]))
    # id 补全
    for index, item in enumerate(items, 1):
        item.setdefault("id", f"i{index:03d}")
        item.setdefault("source", path.name)
    return items


def _corpus_items_file(job_id: str) -> Path:
    return paths.DATA_DIR / "corpus" / job_id / "items.json"


def load_corpus_items(job_id: str) -> tuple[list[dict] | None, str | None]:
    """取 corpusstudio 清洗结果；返回 ``(items, error)``。"""
    job_id = str(job_id or "").strip()
    if not job_id or not _JOB_ID_RE.match(job_id):
        return None, "invalid_job_id"
    try:
        import importlib
        # 用 import_module 而不是 ``from . import``：测试里会 monkeypatch sys.modules
        corpusstudio = importlib.import_module("app.corpusstudio")
    except Exception:  # noqa: BLE001 - 模块未交付/依赖缺失都按未就绪处理
        corpusstudio = None
    if corpusstudio is not None:
        for name in ("load_items", "get_items"):
            loader = getattr(corpusstudio, name, None)
            if not callable(loader):
                continue
            try:
                items = loader(job_id)
            except Exception:  # noqa: BLE001 - 未就绪/无该任务
                items = None
            if isinstance(items, list):
                return _with_ids(items), None
    cache = _corpus_items_file(job_id)
    if cache.is_file():
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            items = payload.get("items") if isinstance(payload, dict) else payload
            if isinstance(items, list):
                return _with_ids(items), None
        except Exception:  # noqa: BLE001
            pass
    return None, "corpus_backend_not_ready"


def _with_ids(items: list) -> list[dict]:
    out: list[dict] = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row.setdefault("id", f"i{index:03d}")
        row.setdefault("role", "me" if str(row.get("speaker") or "") in ("me", "assistant")
                       else "you")
        row["role"] = "me" if str(row["role"]) in ("me", "assistant") else "you"
        row["text"] = str(row.get("text") or "")
        out.append(row)
    return out


def load_source(source: Any) -> tuple[list[dict] | None, str | None]:
    """source → items。支持：job_id / JSONL path / 内存 items / {"items":[...]}。"""
    if isinstance(source, list):
        return _with_ids(source), None
    if isinstance(source, dict):
        items = source.get("items")
        if isinstance(items, list):
            return _with_ids(items), None
        messages = source.get("messages")
        if isinstance(messages, list):
            return _with_ids(_items_from_messages(messages)), None
        return None, "invalid_source"
    raw = str(source or "").strip()
    if not raw:
        return None, "invalid_source"
    path = Path(raw).expanduser()
    try:
        if path.is_file():
            return _items_from_jsonl(path), None
    except OSError:
        pass
    return load_corpus_items(raw)


# --------------------------------------------------------------------------- #
# 4) 上下文 / 组装
# --------------------------------------------------------------------------- #
def _context_for(items: list[dict], index: int, window: int) -> list[dict]:
    start = max(0, index - max(1, int(window)))
    turns = []
    for item in items[start:index]:
        role = "assistant" if str(item.get("role")) == "me" else "user"
        text = str(item.get("text") or "").strip()
        if text:
            turns.append({"role": role, "text": text})
    return turns


def _prev_user(items: list[dict], index: int) -> str:
    for item in reversed(items[:index]):
        if str(item.get("role")) == "you" and str(item.get("text") or "").strip():
            return str(item["text"]).strip()
    return ""


# --------------------------------------------------------------------------- #
# 5) 保存 soul 数据集
# --------------------------------------------------------------------------- #
def _safe_out_name(out_name: str) -> str | None:
    raw = str(out_name or "").strip()
    if not raw:
        return f"soul_user_{_timestamp()}"
    # 只允许纯文件名：任何路径分隔符 / 上级目录 / 盘符都拒绝（防写到 datasets 外）
    if "/" in raw or "\\" in raw or ".." in raw or ":" in raw:
        return None
    if raw.lower().endswith(".jsonl"):
        raw = raw[:-6]
    if not re.fullmatch(r"[A-Za-z0-9_.\-\u4e00-\u9fff]{1,80}", raw):
        return None
    return raw


def _datasets_dir() -> Path:
    return paths.agent_home() / "datasets"


def save_annotations(source: Any, annotations: list[dict], *, window: int = 6,
                     out_name: str = "", min_score: int = 0, split: float = 0.1,
                     system: str = "") -> dict:
    """过滤/校验人工标注 → 写 soul JSONL（context + user_msg + target）。

    ``annotations``: ``[{item_id, target, score:int, tags:[], note:""}]``；
    score ≥ ``min_score`` 才保留。返回 ``{ok,out,val_out,rows,val_rows,skipped,min_score}``；
    无有效条目 → ``{ok: False, error: "empty_annotations"}``。
    """
    items, error = load_source(source)
    if items is None:
        return {"ok": False, "error": error or "invalid_source",
                "rows": 0, "val_rows": 0, "skipped": 0, "min_score": min_score}
    by_id = {str(item.get("id")): (index, item) for index, item in enumerate(items)}

    min_score = int(min_score or 0)
    rows: list[dict] = []
    skipped = 0
    skip_reasons: list[str] = []
    for annotation in annotations or []:
        if not isinstance(annotation, dict):
            skipped += 1
            skip_reasons.append("annotation 不是对象")
            continue
        item_id = str(annotation.get("item_id") or "").strip()
        try:
            score = int(annotation.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        if score < min_score:
            skipped += 1
            skip_reasons.append(f"{item_id or '?'}: score={score} < min_score={min_score}")
            continue
        located = by_id.get(item_id)
        if located is None:
            skipped += 1
            skip_reasons.append(f"{item_id or '?'}: item_id 不存在")
            continue
        index, item = located
        if str(item.get("role")) != "me":
            skipped += 1
            skip_reasons.append(f"{item_id}: 不是 me 回复（只有 assistant 回复需要 InnerState）")
            continue
        prev_user = _prev_user(items, index)
        if not prev_user:
            skipped += 1
            skip_reasons.append(f"{item_id}: 找不到上一条 you 消息，无法作为 user_msg")
            continue
        checked = validate_annotation(annotation.get("target"))
        if not checked["ok"]:
            skipped += 1
            skip_reasons.append(f"{item_id}: target 非法：{'; '.join(checked['errors'])}")
            continue
        context = []
        if str(system or "").strip():
            context.append({"role": "system", "text": str(system).strip()})
        context.extend(_context_for(items, index, window))
        rows.append({
            "context": context,
            "user_msg": prev_user,
            "target": checked["target"],
            "meta": {
                "item_id": item_id,
                "score": score,
                "tags": list(annotation.get("tags") or []),
                "note": str(annotation.get("note") or ""),
                "source": str(item.get("source") or ""),
                "warnings": checked["warnings"],
                "annotated_at": datetime.now().isoformat(timespec="seconds"),
            },
        })

    if not rows:
        return {"ok": False, "error": "empty_annotations", "rows": 0,
                "val_rows": 0, "skipped": skipped, "min_score": min_score,
                "skip_reasons": skip_reasons[:20]}

    name = _safe_out_name(out_name)
    if name is None:
        return {"ok": False, "error": "invalid_out_name", "rows": 0,
                "val_rows": 0, "skipped": skipped, "min_score": min_score}
    out_dir = _datasets_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"mkdir_failed: {exc}", "rows": 0,
                "val_rows": 0, "skipped": skipped, "min_score": min_score}

    try:
        split_ratio = max(0.0, min(1.0, float(split or 0.0)))
    except (TypeError, ValueError):
        split_ratio = 0.1
    val_n = int(round(len(rows) * split_ratio)) if split_ratio > 0 else 0
    if split_ratio > 0 and len(rows) >= 2 and val_n == 0:
        val_n = 1  # 小数据集 round(0.5)=0 会把验证集切没；至少留 1 条
    train_rows = rows[:len(rows) - val_n] if val_n else rows
    val_rows = rows[len(rows) - val_n:] if val_n else []

    out_path = out_dir / f"{name}.jsonl"
    val_path = out_dir / f"{name}_val.jsonl"
    out_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in train_rows) + "\n",
        encoding="utf-8")
    val_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in val_rows)
        + ("\n" if val_rows else ""),
        encoding="utf-8")

    audit.record("annot.save", str(out_path),
                 after={"rows": len(train_rows), "val_rows": len(val_rows),
                        "skipped": skipped, "min_score": min_score},
                 meta={"source": str(source)[:200], "out_name": name,
                       "window": int(window), "split": split_ratio,
                       "format": "soul_inner_state"})
    return {"ok": True, "out": str(out_path), "val_out": str(val_path),
            "rows": len(train_rows), "val_rows": len(val_rows),
            "skipped": skipped, "min_score": min_score,
            "skip_reasons": skip_reasons[:20]}


# --------------------------------------------------------------------------- #
# 6) 路由用：source 视图（items + 上下文 + 建议）
# --------------------------------------------------------------------------- #
def source_view(job_id: str, *, window: int = 6) -> dict:
    """``GET /api/wb/annot/source/{job_id}``：返回可标注的 me 回复 + 上下文 + 预填建议。"""
    items, error = load_corpus_items(job_id)
    if items is None:
        return {"ok": False, "error": error or "corpus_backend_not_ready", "items": []}
    annotated = []
    for index, item in enumerate(items):
        if str(item.get("role")) != "me":
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        context = _context_for(items, index, window)
        prev_user = _prev_user(items, index)
        suggestion = suggest_annotation(text, prev_user_text=prev_user,
                                        context=context)
        annotated.append({
            "id": str(item.get("id")),
            "role": "me",
            "text": text,
            "source": str(item.get("source") or ""),
            "prev_user_text": prev_user,
            "context": context,
            "suggestion": suggestion,
        })
    return {"ok": True, "job_id": str(job_id), "total": len(items),
            "items": annotated, "window": int(window)}


__all__ = [
    "suggest_annotation", "validate_annotation", "save_annotations",
    "source_view", "load_corpus_items", "load_source",
    "STAGES", "LENGTHS", "EMOJI_VALUES", "TARGET_FIELDS",
]
