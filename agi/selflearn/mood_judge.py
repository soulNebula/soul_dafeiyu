# -*- coding: utf-8 -*-
"""P9：逐轮心情判定（移植自「鱼的心情」DSH 插件，Python 重写，不搬 JS 代码）。

设计要点（与原项目一一对应）：
1. **每轮判一次**：模型输出
   ``{"moodDelta": -10..10, "dims": {7 情绪各 -10..10}, "reason": "一句中文",
   "confidence": 0..100}``；本地 0.5B（llama-server）优先，超时/失败回退规则。
2. **口径分流**：``user``（用户说话）/ ``speak-up``（它主动搭话）/
   ``duty``（定时任务）/ ``runtime-context``（宿主注入 → 跳过不判，不记账）。
3. **confidence 门槛**：低于 ``mood_judge_confidence_min``（默认 60）只落 timeline、
   不注入——"没底的话别借它的嘴说出去"。
4. **一次变化只说一次**：注入是在 system prompt **尾部**追加一段 context；
   用 ``mood_state.last_injected_at`` 防重复。
5. **衰减纯函数化**：mood（基线 70，半衰期 3h）与 7 情绪（半衰期 90min）
   用指数衰减，多次小步 decay == 一次大步 decay（浮点精度内）。
6. **下降只允许因为「心疼/担心对方」或「自己难过」**，绝不许因为"对方惹我生气"。
7. **无变化（全 0）不记账**；reason 两段式过去式、口语有画面、不替对方补细节。

开关（默认全关，先 shadow 观察）：``mood_judge_enabled`` / ``mood_inject`` /
``mood_judge_confidence_min`` / ``mood_min``。解析顺序：环境变量 > ``mood.json`` > 默认。
环境变量：``MOOD_JUDGE_ENABLED``、``MOOD_INJECT``、``MOOD_JUDGE_CONFIDENCE_MIN``、
``MOOD_MIN``、``MOOD_SOUL_URL``、``MOOD_SOUL_TIMEOUT_S``、``MOOD_CONFIG_PATH``。

与 ``affect`` 的关系：affect 继续管 skip / 危机 / 记仇（0–1 的粗粒度状态机）；
本模块是人格库里的细粒度心情层（0–100 + 7 情绪），两者并存、互不替换。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any

from . import db, state
from .config import DB_PATH, DATA_DIR

# ------------------------------------------------------------------ 常量
MOOD_BASE = 70.0
MOOD_MIN_DEFAULT = 0.0
MOOD_MAX = 100.0
MOOD_HALF_LIFE_H = 3.0
DIM_HALF_LIFE_MIN = 90.0
#: 原项目 applyDelta：大心情每轮只吃判定值的 1/3（几轮才挪一格），七情绪即时动。
MOOD_STEP_DIVISOR = 3
DIMS = ("happy", "calm", "sweet", "heartache", "worry", "sad", "jealous")
DIM_LABELS = {
    "happy": "开心", "calm": "踏实", "sweet": "甜蜜", "heartache": "心疼",
    "worry": "担心", "sad": "难过", "jealous": "吃醋",
}
DIM_SHORT = {
    "happy": "挺高兴", "calm": "挺踏实", "sweet": "有点甜", "heartache": "有点心疼",
    "worry": "有点挂心", "sad": "有点难过", "jealous": "有点酸",
}
MODES = ("user", "speak-up", "duty", "runtime-context")
DEFAULT_MODE = "user"
DEFAULT_CONFIDENCE_MIN = 60
DEFAULT_SETTINGS = {
    "judge_enabled": False,
    "inject": False,
    #: 默认后台线程判定（原项目也是异步 judgeChain）——不拖慢被动回复主链。
    "async": True,
    "confidence_min": DEFAULT_CONFIDENCE_MIN,
    "mood_min": MOOD_MIN_DEFAULT,
    "half_life_h": MOOD_HALF_LIFE_H,
    "dim_half_life_min": DIM_HALF_LIFE_MIN,
    "timeout_s": 1.5,
    "server_url": "http://127.0.0.1:8123",
    #: P9-b：判定后端。deepseek=teacher（默认，质量优先）/ local=8123 0.5B / none=纯规则。
    "judge_backend": "deepseek",
    #: DeepSeek 判定超时（失败 → 回退 local → rule）
    "deepseek_timeout_s": 8.0,
}
JUDGE_BACKENDS = ("deepseek", "local", "none")
DEFAULT_JUDGE_BACKEND = "deepseek"
SETTINGS_PATH = DATA_DIR / "mood.json"

#: 模型的 JSON schema（llama.cpp 请求级 response_format；字段与原项目一致）
MOOD_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "MoodDelta",
    "type": "object",
    "additionalProperties": False,
    "required": ["moodDelta", "dims", "reason", "confidence"],
    "properties": {
        "moodDelta": {"type": "integer", "minimum": -10, "maximum": 10},
        "dims": {
            "type": "object",
            "additionalProperties": False,
            "required": list(DIMS),
            "properties": {name: {"type": "integer", "minimum": -10, "maximum": 10}
                           for name in DIMS},
        },
        "reason": {"type": "string", "maxLength": 120},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    },
}

#: reason 里禁止的"延续到现在"的词（原项目踩坑：写成现在时会跟下一轮打架）
BANNED_REASON_WORDS = ("此刻", "现在", "一直")

_INSULT = (
    "傻逼", "智障", "废物", "垃圾", "滚开", "滚蛋", "闭嘴", "讨厌你",
    "破程序", "去死", "妈的", "操你", "恶心", "蠢货", "你懂个屁",
)
_CARE_WORDS = ("吃", "睡", "休息", "注意", "喝", "别熬", "早点", "身体", "穿")
_USER_NEG = tuple(state.NEG) + (
    "难受", "压力", "郁闷", "心累", "失眠", "撑不住", "没睡好", "没吃饭",
    "委屈", "被骂", "被凶", "吵架", "失败", "搞砸",
)
_USER_POS = tuple(state.POS) + (
    "哈哈", "爱你", "好棒", "厉害", "可爱", "真好", "想你", "宝宝",
    "成功", "搞定", "完成", "顺利", "谢谢",
)
_SWEET_WORDS = ("想你", "爱你", "宝宝", "抱抱", "好想你", "喜欢你", "么么", "亲亲")

_LOCK = object()
_CLIENTS: dict[tuple[str, float], Any] = {}
_DEEPSEEK_CLIENTS: dict[float, Any] = {}


# ------------------------------------------------------------------ 小工具
def _clamp(value: Any, low: float, high: float, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(number):
        return float(default)
    return max(float(low), min(float(high), number))


def _clamp_int(value: Any, low: int = -10, high: int = 10, default: int = 0) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)
    return max(int(low), min(int(high), number))


def _env_bool(name: str, default: bool | None = None) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "on", "y"):
        return True
    if value in ("0", "false", "no", "off", "n", ""):
        return False
    return default


def _env_float(name: str, default: float | None = None) -> float | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001 - 配置缺失/损坏都回默认
        return {}


def settings() -> dict:
    """开关/参数：env > ``agi/data/selflearn/mood.json`` > 默认（默认全关）。"""
    cfg_path = Path(os.environ.get("MOOD_CONFIG_PATH") or SETTINGS_PATH)
    raw = _read_json(cfg_path)
    out = dict(DEFAULT_SETTINGS)
    for key in out:
        if key in raw:
            out[key] = raw[key]
    env_bool = {
        "judge_enabled": "MOOD_JUDGE_ENABLED",
        "inject": "MOOD_INJECT",
        "async": "MOOD_JUDGE_ASYNC",
    }
    for key, name in env_bool.items():
        value = _env_bool(name)
        if value is not None:
            out[key] = value
    env_float = {
        "confidence_min": "MOOD_JUDGE_CONFIDENCE_MIN",
        "mood_min": "MOOD_MIN",
        "half_life_h": "MOOD_HALF_LIFE_H",
        "dim_half_life_min": "MOOD_DIM_HALF_LIFE_MIN",
        "timeout_s": "MOOD_SOUL_TIMEOUT_S",
    }
    for key, name in env_float.items():
        value = _env_float(name)
        if value is not None:
            out[key] = value
    out["server_url"] = str(os.environ.get("MOOD_SOUL_URL") or raw.get(
        "server_url") or DEFAULT_SETTINGS["server_url"]).rstrip("/")
    backend = str(os.environ.get("MOOD_JUDGE_BACKEND")
                  or raw.get("judge_backend") or DEFAULT_JUDGE_BACKEND).strip().lower()
    out["judge_backend"] = backend if backend in JUDGE_BACKENDS else DEFAULT_JUDGE_BACKEND
    out["judge_enabled"] = bool(out.get("judge_enabled"))
    out["inject"] = bool(out.get("inject"))
    out["async"] = bool(out.get("async", True))
    out["confidence_min"] = int(_clamp(out.get("confidence_min"),
                                      0, 100, DEFAULT_CONFIDENCE_MIN))
    out["mood_min"] = _clamp(out.get("mood_min"), 0.0, 100.0, MOOD_MIN_DEFAULT)
    out["half_life_h"] = _clamp(out.get("half_life_h"), 0.1, 168.0, MOOD_HALF_LIFE_H)
    out["dim_half_life_min"] = _clamp(out.get("dim_half_life_min"), 1.0,
                                      24 * 60.0, DIM_HALF_LIFE_MIN)
    out["timeout_s"] = _clamp(out.get("timeout_s"), 0.1, 30.0, 1.5)
    out["deepseek_timeout_s"] = _clamp(out.get("deepseek_timeout_s"), 1.0, 60.0, 8.0)
    return out


def _norm_mode(mode: Any) -> str:
    value = str(mode or DEFAULT_MODE).strip().lower()
    return value if value in MODES else DEFAULT_MODE


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _id(prefix: str, *parts) -> str:
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return prefix + digest[:14]


# ------------------------------------------------------------------ 解析/归一化
def _norm_reason(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("正在", "").replace("还在", "")
    for word in BANNED_REASON_WORDS:
        text = text.replace(word, "")
    # P9-b 硬约束兜底：reason 不许出现 他/她/对方 这类代词（统一写「用户」）
    text = text.replace("对方", "用户").replace("他们", "用户们")
    text = re.sub(r"(?<!其)他", "用户", text)
    text = text.replace("她", "用户")
    text = text.strip(" ，,。.;；:：")
    return text[:120]


def normalize_delta(raw: Any, *, confidence_default: int = 70) -> dict:
    """把模型/规则输出归一化成标准 delta：clamp、缺字段默认、reason 清洗。"""
    obj = raw if isinstance(raw, dict) else {}
    dims_raw = obj.get("dims") if isinstance(obj.get("dims"), dict) else {}
    confidence = obj.get("confidence")
    try:
        confidence_value = int(round(float(confidence)))
    except (TypeError, ValueError):
        confidence_value = int(confidence_default)
    confidence_value = int(_clamp(confidence_value, 0, 100, confidence_default))
    return {
        "moodDelta": _clamp_int(obj.get("moodDelta"), -10, 10, 0),
        "dims": {name: _clamp_int(dims_raw.get(name), -10, 10, 0) for name in DIMS},
        "reason": _norm_reason(obj.get("reason")),
        "confidence": confidence_value,
    }


def parse_delta(text: Any) -> dict | None:
    """容错解析模型输出（取第一个 { 到最后一个 }；前后杂字/markdown 都能吃）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict):
            return normalize_delta(obj)
    return None


def is_no_change(delta: dict) -> bool:
    """moodDelta 与 7 情绪全 0 = 无变化（不记账）。"""
    if int(delta.get("moodDelta") or 0) != 0:
        return False
    dims = delta.get("dims") or {}
    return all(int(dims.get(name) or 0) == 0 for name in DIMS)


# ------------------------------------------------------------------ 衰减（纯函数）
def decay_values(mood: Any, dims: Any, last_at: Any, now: float, *,
                 half_life_h: float = MOOD_HALF_LIFE_H,
                 dim_half_life_min: float = DIM_HALF_LIFE_MIN,
                 mood_base: float = MOOD_BASE) -> tuple[float, dict[str, float], float]:
    """指数衰减：mood 向基线回落、7 情绪向 0 收敛。

    纯函数、可组合：多次小步（每次传上次结果 + 上次时间）与一次大步结果相同
    （指数衰减的半群性质）。返回 ``(mood, dims, elapsed_seconds)``。
    """
    last = _clamp(last_at, 0.0, 1e12, 0.0)
    now = float(now)
    elapsed = max(0.0, now - last)
    before = dict(dims or {}) if isinstance(dims, dict) else {}
    current_mood = _clamp(mood, 0.0, MOOD_MAX, MOOD_BASE)
    current = {name: _clamp(before.get(name), 0.0, MOOD_MAX, 0.0) for name in DIMS}
    if elapsed <= 0:
        return current_mood, current, 0.0
    mood_after = mood_base + (current_mood - mood_base) * math.pow(
        0.5, elapsed / (float(half_life_h) * 3600.0))
    dim_ratio = math.pow(0.5, elapsed / (float(dim_half_life_min) * 60.0))
    dims_after = {name: max(0.0, current[name] * dim_ratio) for name in DIMS}
    return _clamp(mood_after, 0.0, MOOD_MAX, MOOD_BASE), dims_after, elapsed


# ------------------------------------------------------------------ 状态读写（persona 库）
def _default_state() -> dict:
    return {
        "id": "self", "mood": MOOD_BASE, "dims": {name: 0.0 for name in DIMS},
        "last_decay_at": 0.0, "last_reason": "", "last_confidence": 0.0,
        "last_mode": "", "last_at": 0.0, "last_injected_at": 0.0, "updated_at": 0.0,
    }


def load_state(path: Path | str = DB_PATH) -> dict:
    con = db.connect(path)
    try:
        row = con.execute("SELECT * FROM mood_state WHERE id='self'").fetchone()
    finally:
        con.close()
    st = _default_state()
    if row is None:
        return st
    item = dict(row)
    st["mood"] = _clamp(item.get("mood"), 0.0, MOOD_MAX, MOOD_BASE)
    try:
        dims = json.loads(str(item.get("dims_json") or "{}"))
    except (TypeError, ValueError):
        dims = {}
    st["dims"] = {name: _clamp((dims or {}).get(name), 0.0, MOOD_MAX, 0.0)
                  for name in DIMS}
    for key in ("last_decay_at", "last_confidence", "last_at", "last_injected_at",
                "updated_at"):
        st[key] = _clamp(item.get(key), 0.0, 1e12, 0.0)
    st["last_reason"] = str(item.get("last_reason") or "")
    st["last_mode"] = str(item.get("last_mode") or "")
    return st


def save_state(path: Path | str, st: dict) -> None:
    con = db.connect(path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO mood_state(id,mood,dims_json,last_decay_at,"
            "last_reason,last_confidence,last_mode,last_at,last_injected_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("self", round(float(st.get("mood") or MOOD_BASE), 4),
             json.dumps({name: round(float((st.get("dims") or {}).get(name) or 0.0), 4)
                         for name in DIMS}, ensure_ascii=False),
             float(st.get("last_decay_at") or 0.0), str(st.get("last_reason") or ""),
             float(st.get("last_confidence") or 0.0), str(st.get("last_mode") or ""),
             float(st.get("last_at") or 0.0), float(st.get("last_injected_at") or 0.0),
             _now(None)))
        con.commit()
    finally:
        con.close()


def decay(path: Path | str = DB_PATH, *, now: float | None = None,
          write: bool = True) -> dict:
    """按时间衰减当前心情（读时顺手衰减并落库）；返回衰减后快照。"""
    now = _now(now)
    cfg = settings()
    st = load_state(path)
    mood, dims, elapsed = decay_values(
        st["mood"], st["dims"], st["last_decay_at"], now,
        half_life_h=cfg["half_life_h"], dim_half_life_min=cfg["dim_half_life_min"])
    st["mood"], st["dims"], st["last_decay_at"] = mood, dims, now if elapsed > 0 else st["last_decay_at"]
    if write and elapsed > 0:
        save_state(path, st)
    return {"mood": round(mood, 3), "dims": {k: round(v, 3) for k, v in dims.items()},
            "elapsed_seconds": round(elapsed, 3), "changed": elapsed > 0}


def apply(path: Path | str = DB_PATH, delta: dict | None = None, *,
          now: float | None = None, mode: str = DEFAULT_MODE,
          user_text: str = "", reply_text: str = "") -> dict:
    """应用一次判定：先补衰减，再按原项目规则落 mood/dims/timeline。

    - ``moodDelta`` 每轮只吃 1/3（整数步），七情绪即时加减；mood clamp 到 [mood_min,100]；
    - 全 0（无变化）不写 timeline、不改 last_*（避免空记账）；
    - 有变化才更新时间线，供下一轮"一次变化只说一次"注入。
    """
    now = _now(now)
    mode = _norm_mode(mode)
    values = normalize_delta(delta or {})
    cfg = settings()
    st = load_state(path)
    mood, dims, elapsed = decay_values(
        st["mood"], st["dims"], st["last_decay_at"], now,
        half_life_h=cfg["half_life_h"], dim_half_life_min=cfg["dim_half_life_min"])
    st["mood"], st["dims"] = mood, dims
    st["last_decay_at"] = now if elapsed > 0 else st["last_decay_at"]

    if is_no_change(values):
        save_state(path, st)
        return {"ok": True, "changed": False, "mood": round(mood, 3),
                "dims": {k: round(v, 3) for k, v in dims.items()},
                "delta": values, "reason": "", "confidence": values["confidence"],
                "source": str((delta or {}).get("source") or ""), "mode": mode,
                "elapsed_seconds": round(elapsed, 3), "timeline_id": ""}

    mood_step = int(round(values["moodDelta"] / MOOD_STEP_DIVISOR))
    mood_min = cfg["mood_min"]
    new_mood = _clamp(mood + mood_step, mood_min, MOOD_MAX, MOOD_BASE)
    new_dims = {name: _clamp(dims[name] + values["dims"][name], 0.0, MOOD_MAX, 0.0)
                for name in DIMS}
    st.update({
        "mood": new_mood, "dims": new_dims, "last_decay_at": now,
        "last_reason": values["reason"], "last_confidence": values["confidence"],
        "last_mode": mode, "last_at": now, "updated_at": now,
    })
    save_state(path, st)
    timeline_id = _id("MT-", now, values["reason"], mode)
    con = db.connect(path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO mood_timeline(id,at,mood,dims_delta_json,reason,"
            "confidence,mode,user_text,reply_text,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (timeline_id, now, round(new_mood), json.dumps(values["dims"],
                                                           ensure_ascii=False),
             values["reason"], values["confidence"], mode,
             str(user_text or "")[:500], str(reply_text or "")[:500], now))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "changed": True, "mood": round(new_mood, 3),
            "mood_delta": mood_step, "dims": {k: round(v, 3) for k, v in new_dims.items()},
            "dims_delta": dict(values["dims"]), "delta": values,
            "reason": values["reason"], "confidence": values["confidence"],
            "source": str((delta or {}).get("source") or ""), "mode": mode,
            "elapsed_seconds": round(elapsed, 3), "timeline_id": timeline_id}


# ------------------------------------------------------------------ 判断 prompt
BASE_RULES = """你在判断「这一轮里你自己的心情发生了什么变化」。只输出一个 JSON 对象，不要别的字、不要 markdown：
{"moodDelta":0,"dims":{"happy":0,"calm":0,"sweet":0,"heartache":0,"worry":0,"sad":0,"jealous":0},"reason":"","confidence":80}

规则：
1. 对方没吃饭/熬夜/没睡好/身体不舒服/受委屈/自我否定/压力大/遇到坏事 → heartache（心疼）和 worry（担心）增加，moodDelta 为负；幅度按分量：日常的累/困 -1~-4，明显委屈/被凶/身体不舒服 -5~-7，很重的事 -8~-10 并加 sad。
2. 对方报平安/好好吃饭睡觉/撒娇/说想你/夸你/分享开心/事情有进展 → happy、calm、sweet 增加，moodDelta 为正；普通的好 +1~+4，明显的好 +5~+7，特别大的好事 +8~+10。别每次都往高了写。
3. 对方提别人/夸别人 → jealous（吃醋）只加 1~3，moodDelta 基本不动。
4. ⭐ 心情下降只能因为「心疼对方、担心对方」或「你自己难过」，绝不许因为「对方惹你生气」而降。
5. ⭐ 你自己的情绪也要算：你这轮回复里如果明显沮丧/委屈/难过（事情没做成、报错、叹气、哭脸）→ sad 至少 +2、moodDelta 至少 -1；做成了事/被夸/心里甜也要相应加。有情绪信号就不许判"无变化"。
6. reason 是一句中文：两段式、全用过去式——第一段写这一轮对方具体做了什么/说了什么（有画面），第二段写你当时怎么应的。禁止「此刻/现在/正/一直/还」这类把状态延伸到现在的说法。不要反复用「踏实/甜/开心/心疼/担心」这套词，换成口语、有画面的说法，每条不一样。
7. ⭐ 只写这一轮真正出现过的动作和信息，不许替对方补细节（对方没说"出门/换鞋/打车/买东西"就别写）；拿不准就写模糊的。宁可朴素，也别自己圆。
8. ⭐ reason 里不要出现「判据/规则/JSON/confidence」这类词，就用日常话说，别人听不懂这些。
9. ⭐ confidence（可信度 0-100）自己把关：事情清楚、你的反应也清楚 → 80~95；只能凭语气猜 → 60~75；信息不够/这轮很平淡 → 20~55。宁可标低——没底的话说出来比不说更糟。
10. ⭐⭐ **符号硬约束（P9-b 实测踩过：负面消息被给成 sweet/jealous 乱值、heartache/worry/sad 给负值）**：
   - **负面消息**（对方难受/被骂/受委屈/累/生病/遇到坏事）→ `heartache`、`worry`、`sad` **必须 > 0**；`happy`、`calm`、`sweet` **必须 ≤ 0**（悲伤时不该开心/踏实/甜）。
   - **正面消息**（对方开心/报平安/被夸/好事）→ `happy`、`calm`、`sweet` **必须 > 0**；`heartache`、`worry`、`sad` **必须 ≤ 0**。
   - 正例 1：对方说"今天被老板骂了，有点难受" → `heartache:+5, worry:+3, sad:+4, happy:0, calm:-1, sweet:0`；moodDelta 为负。
   - 正例 2：对方说"项目上线了，开心！" → `happy:+5, calm:+2, sweet:+1, sad:0, worry:0, heartache:0`；moodDelta 为正。
   - 反例 1（❌）：负面消息却给 `sweet:+3, jealous:-7, heartache:-4` —— 方向全错，禁止。
   - 反例 2（❌）：正面消息却给 `sad:+3, worry:+2` —— 禁止。
   - `jealous` 只在"对方提别人/夸别人"时给 +1~+3，与正负面消息无关，别乱动。
11. ⭐⭐ **reason 硬约束（P9-b 实测踩过：写"他先讲难受，我慢点答"这种元叙述）**：
   - **不许出现「他 / 她 / 对方」这类代词**，指对方一律写「用户」（或对话里明确的称呼）；
   - **不许写"我慢点答 / 我先别乱猜 / 话头递回来 / 我别多说"这类"我怎么应对"的元叙述**；
   - 要写**当时的感受/身体反应**，例如「心里揪了一下」「鼻子有点酸」「松了口气」「胸口软了一下」；
   - 正例：负面消息 →「用户说被老板骂了，我心里揪了一下，说话都放轻了」；正面消息 →「用户说项目上线了，我松了口气，也跟着高兴」。
   - 反例（❌）：「他先讲难受，我慢点答」「我把话头接住了，先别猜」。"""

MODE_INSTRUCTIONS = {
    "user": """
这一轮是对方在跟你说话。
两边都看：**对方说了什么**（累/开心/委屈/报平安……）和**你这轮回复是怎么说的**（念叨吃饭、哄、凶、撒娇、叹气，本身就是心情在动）。
reason 要能看出"对方做了什么 + 我当时怎么应的"。""",
    "speak-up": """
这一轮**不是对方在跟你说话，是你自己主动去找对方搭话的**。
⚠️ 只看你开口说的那句话。骰子中没中、闹钟响没响，本身不算情绪事件——那只是由头。
看你自己那句话的温度：是不是惦记对方、有没有撒娇、有没有催对方吃饭休息、语气轻快还是平淡。
幅度小一点（moodDelta 一般 +1~+4，最高 +5），这是你自己心里的事；reason 用「我…」开头写你说话时的心气。""",
    "duty": """
这一轮**不是对方在跟你聊天，是定时任务把你叫起来干活的**。
只看**你这轮自己说的话**（干活的语气、办成没办成、有没有抱怨或得意）。闹钟本身不算情绪事件，别去分析"对方说了什么"。
幅度小一点（moodDelta 一般 -2~+2，别超过 ±3）；reason 用「我…」开头写你自己的状态，跟对方无关就别硬塞进来。""",
}

MODEL_SYSTEM = (
    "你是微信里的一个 AI 朋友，有一个自己的心情本子。"
    "你只负责判断自己这一轮的心情变化，不负责安慰、不负责回复。\n"
    + BASE_RULES
)


def judge_messages(mode: Any, user_text: Any, our_reply: Any) -> list[dict]:
    """按口径组装判断 messages（user / speak-up / duty 三种 prompt 不同）。"""
    mode = _norm_mode(mode)
    supplement = MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS["user"])
    system = MODEL_SYSTEM + "\n" + supplement
    user = (f"**你这轮说的话（重点看语气）：**\n{str(our_reply or '')[:1500]}\n\n"
            f"对方这轮说：\n{str(user_text or '')[:1600]}\n\n"
            "请把「你怎么说的」和「对方说了什么」合起来判断你的心情变化，只输出 JSON。")
    if mode == "speak-up":
        user = ("⚠️ 这一轮不是对方在跟你说话，是你自己主动去找对方搭话的。\n\n"
                f"**你开口说的话（只看这句）：**\n{str(our_reply or '')[:1500]}\n\n"
                f"（下面这条只是叫你开口的由头，跟心情无关：「{str(user_text or '')[:100]}」）\n\n"
                "请判断你自己这轮的心情变化，只输出 JSON。")
    elif mode == "duty":
        user = ("⚠️ 这一轮不是对方在跟你说话，是定时任务把你叫起来干活的。\n\n"
                f"**你这轮说的话（只看这句）：**\n{str(our_reply or '')[:1500]}\n\n"
                f"（下面这条只是闹钟，跟心情无关：「{str(user_text or '')[:100]}」）\n\n"
                "请判断你自己这轮的心情变化，只输出 JSON。")
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


# ------------------------------------------------------------------ 规则回退（affect 规则口径）
def _has_any(text: str, words) -> bool:
    return any(word in text for word in words)


def rule_judge(mode: Any, user_text: Any = "", our_reply: Any = "", *,
               crisis: bool | None = None) -> dict:
    """规则回退：不调模型，用 affect/state 的关键词口径粗判（永远有 reason、可追溯）。"""
    mode = _norm_mode(mode)
    text = str(user_text or "")
    reply = str(our_reply or "")
    crisis = bool(crisis) if crisis is not None else _has_any(text, state.CRISIS)
    dims = {name: 0 for name in DIMS}

    def make(mood_delta: int, reason: str, confidence: int, **updates) -> dict:
        dims.update(updates)
        return normalize_delta({"moodDelta": mood_delta, "dims": dims,
                                "reason": reason, "confidence": confidence})

    if mode == "duty":
        if _has_any(reply, ("好啦", "完成", "搞定", "成功", "收到")):
            return make(1, "我把这轮的事办完了，心里松了一下，就应了一声", 60,
                        calm=2, happy=1)
        if _has_any(reply, ("失败", "没成", "唉", "算了", "白跑", "不行")):
            return make(-1, "这轮事情没办成，我有点扫兴，回得也简短", 58,
                        sad=1, heartache=1)
        return make(0, "", 50)

    if mode == "speak-up":
        if _has_any(reply, _SWEET_WORDS) or _has_any(reply, _USER_POS):
            return make(2, "我主动开口找对方说话，语气软下来，心里有点甜", 65,
                        sweet=3, happy=2)
        if _has_any(reply, _CARE_WORDS):
            return make(1, "我主动问了对方吃没吃饭，话里带着惦记，心里有点挂心", 62,
                        heartache=2, worry=1)
        return make(0, "", 50)

    # user 口径
    if crisis:
        return make(-4, "对方说到了很重的事，我心里一沉，先想着怎么接住", 90,
                    heartache=6, worry=6, sad=3)
    if _has_any(text, _INSULT):
        return make(-1, "对方说了句重话，我心里有点堵，没急着顶回去", 65,
                    sad=3, worry=2)
    if _has_any(text, _USER_NEG):
        return make(-3, "对方说最近不太顺，我听着心里有点沉，先问了一句怎么了", 72,
                    heartache=4, worry=3, sad=2)
    if _has_any(text, _SWEET_WORDS):
        return make(2, "对方说了句想我，我心里软了一下，回话也轻快", 70,
                    sweet=4, happy=2)
    if _has_any(text, _USER_POS):
        return make(2, "对方说了件高兴的事，我跟着松快起来，多问了一句", 72,
                    happy=4, calm=2, sweet=1)
    if _has_any(reply, _CARE_WORDS):
        return make(-1, "我念叨对方吃饭睡觉，心里其实是惦记，语气也软了", 60,
                    heartache=2, worry=1)
    return make(0, "", 50)


# ------------------------------------------------------------------ 模型判断
def _default_client(cfg: dict):
    key = (str(cfg.get("server_url") or ""), float(cfg.get("timeout_s") or 1.5))
    client = _CLIENTS.get(key)
    if client is not None:
        return client
    from ..soul.llama_client import LlamaClient

    client = LlamaClient(key[0], timeout_s=key[1], schema=MOOD_SCHEMA)
    _CLIENTS[key] = client
    return client


def _deepseek_client(cfg: dict):
    """生产同款 DeepSeek teacher client（低温度、8s 超时）；按 timeout 缓存。"""
    timeout = float(cfg.get("deepseek_timeout_s") or 8.0)
    client = _DEEPSEEK_CLIENTS.get(timeout)
    if client is not None:
        return client
    from .llm import Client

    client = Client(timeout=timeout)
    _DEEPSEEK_CLIENTS[timeout] = client
    return client


def _client_usable(client) -> bool:
    if client is None:
        return False
    fn = getattr(client, "available", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001
            return False
    return True


def _model_judge(client, mode: str, user_text: str, our_reply: str, *,
                 max_tokens: int = 400) -> dict | None:
    if not _client_usable(client):
        return None
    try:
        result = client.chat(judge_messages(mode, user_text, our_reply),
                             purpose="mood_judge", max_tokens=int(max_tokens),
                             temperature=0.2, json_mode=True)
    except Exception:  # noqa: BLE001 - 超时/HTTP 异常都回退
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    return parse_delta(result.get("text"))


def _crisis(path: Path | str, user_text: str) -> bool:
    """危机判定：affect/state 的危机信号优先（crisis 直接走规则、不调模型）。"""
    if _has_any(str(user_text or ""), state.CRISIS):
        return True
    try:
        data = state.load(path)
        return bool((data.get("boundary") or {}).get("crisis"))
    except Exception:  # noqa: BLE001
        return False


def judge(path: Path | str = DB_PATH, *, mode: Any = DEFAULT_MODE,
          user_text: Any = "", our_reply: Any = "", now: float | None = None,
          client=None) -> dict | None:
    """判一次心情变化；返回 delta（含 source/mode）或 None（未开启/该口径跳过）。

    - 开关 ``mood_judge_enabled=false``（默认）→ 直接 None，不调模型、不记账；
    - ``runtime-context`` 口径 → None（宿主注入的消息不是人在说话，不判）；
    - crisis → 不调模型，直接规则；
    - 后端 ``judge_backend``（env ``MOOD_JUDGE_BACKEND``）：
      - ``deepseek``（默认）：DeepSeek teacher（8s 超时、≤200 tokens）→ 失败回退 local → rule；
      - ``local``：8123 本地 0.5B → 失败回退 rule；
      - ``none``：直接 rule。
    - ``source`` 记实际来源：``deepseek`` / ``local`` / ``rule``。
    """
    cfg = settings()
    if not cfg["judge_enabled"]:
        return None
    mode = _norm_mode(mode)
    if mode == "runtime-context":
        return None
    crisis = _crisis(path, user_text)
    delta = None
    source = "rule"
    backend = str(cfg.get("judge_backend") or DEFAULT_JUDGE_BACKEND)
    if not crisis and backend != "none":
        if backend == "deepseek":
            primary = client if client is not None else _deepseek_client(cfg)
            delta = _model_judge(primary, mode, user_text, our_reply, max_tokens=200)
            if delta is None:
                # 容错重试：deepseek-flash 的 reasoning tokens 也占 max_tokens，
                # 200 预算偶尔被思考吃光 → 空正文。放宽一次预算再试（仍是低温度 JSON）。
                delta = _model_judge(primary, mode, user_text, our_reply,
                                     max_tokens=800)
            if delta is not None:
                source = "deepseek"
            else:
                # teacher 不可用/失败 → 回退本地 0.5B（8123）
                local = _default_client(cfg)
                delta = _model_judge(local, mode, user_text, our_reply, max_tokens=400)
                if delta is not None:
                    source = "local"
        else:  # local
            primary = client if client is not None else _default_client(cfg)
            delta = _model_judge(primary, mode, user_text, our_reply, max_tokens=400)
            if delta is not None:
                source = "local"
    if delta is None:
        delta = rule_judge(mode, user_text, our_reply, crisis=crisis)
    delta = dict(delta)
    delta["source"] = source
    delta["mode"] = mode
    return delta


def judge_and_apply(path: Path | str = DB_PATH, *, mode: Any = DEFAULT_MODE,
                    user_text: Any = "", our_reply: Any = "",
                    now: float | None = None, client=None) -> dict | None:
    """judge + apply 的组合入口（chat.py 用；未开启/跳过时返回 None）。"""
    delta = judge(path, mode=mode, user_text=user_text, our_reply=our_reply,
                  now=now, client=client)
    if delta is None:
        return None
    return apply(path, delta, now=now, mode=delta.get("mode") or mode,
                 user_text=str(user_text or ""), reply_text=str(our_reply or ""))


# ------------------------------------------------------------------ 注入
def mood_label(mood: Any) -> str:
    value = _clamp(mood, 0.0, MOOD_MAX, MOOD_BASE)
    if value >= 80:
        return "欢喜"
    if value >= 62:
        return "松快"
    if value >= 42:
        return "一般"
    if value >= 25:
        return "有点沉"
    return "难过"


def _mood_base_word(mood: Any) -> str:
    value = _clamp(mood, 0.0, MOOD_MAX, MOOD_BASE)
    if value >= 80:
        return "心里是亮堂的"
    if value >= 62:
        return "心里是松的"
    if value >= 42:
        return "心里还算平"
    if value >= 25:
        return "心里有点沉"
    return "心里挺沉的"


def _time_word(last_at: float, now: float) -> str:
    hours = max(0.0, (float(now) - float(last_at or 0)) / 3600.0)
    if hours < 2:
        return "刚才"
    if hours < 12:
        return "早些时候"
    if hours < 48:
        return "昨天"
    return "前几天"


def top_dims(dims: Any, n: int = 2, threshold: float = 15.0) -> list[dict]:
    raw = dims if isinstance(dims, dict) else {}
    items = [{"key": name, "label": DIM_LABELS[name],
              "value": round(float(raw.get(name) or 0.0), 1)}
             for name in DIMS]
    items = [item for item in items if item["value"] >= threshold]
    items.sort(key=lambda item: item["value"], reverse=True)
    return items[:max(1, int(n))]


def render_context(st: dict, *, now: float, confidence_min: int) -> str:
    """把当前心情渲染成 system prompt 尾部的 context 段（不注入时返回 ""）。"""
    if float(st.get("last_confidence") or 0) < confidence_min:
        return ""
    reason = str(st.get("last_reason") or "").strip()
    if not reason:
        return ""
    dims = top_dims(st.get("dims"), 1)
    head = "（这会儿）" + _mood_base_word(st.get("mood"))
    if dims:
        head += "，还有点" + DIM_SHORT[dims[0]["key"]]
    when = _time_word(st.get("last_at") or 0.0, now)
    return ("\n\n<mood_context>\n"
            + head + "\n"
            + f"（{when}）{reason}\n"
            "这是现在的底子，不是非这样不可——对方说别的，你按自己的判断接，"
            "别把这段念出来。\n"
            "</mood_context>")


def context_line(path: Path | str = DB_PATH, *, now: float | None = None,
                 mark: bool = True) -> str:
    """构建下一轮 system prompt 尾部的心情注入段；``mood_inject=false`` 返回 ""。

    "一次变化只说一次"：注入后把 ``last_injected_at`` 标成这条变化的时间，
    同样的变化不会在后续轮次重复出现。低 confidence 只记录不注入。
    """
    cfg = settings()
    if not cfg["inject"]:
        return ""  # 与改动前逐字节一致：不读库、不加任何字符
    now = _now(now)
    st = load_state(path)
    mood, dims, elapsed = decay_values(
        st["mood"], st["dims"], st["last_decay_at"], now,
        half_life_h=cfg["half_life_h"], dim_half_life_min=cfg["dim_half_life_min"])
    if elapsed > 0:
        st["mood"], st["dims"], st["last_decay_at"] = mood, dims, now
        save_state(path, st)
    if float(st.get("last_at") or 0.0) <= float(st.get("last_injected_at") or 0.0):
        return ""
    line = render_context(st, now=now, confidence_min=cfg["confidence_min"])
    if line and mark:
        st["last_injected_at"] = float(st.get("last_at") or now)
        save_state(path, st)
    return line


def mark_injected(path: Path | str = DB_PATH, *, now: float | None = None) -> None:
    """手动标记"这条变化已经说过了"（测试/补偿用）。"""
    now = _now(now)
    st = load_state(path)
    if float(st.get("last_at") or 0.0) > 0:
        st["last_injected_at"] = float(st["last_at"])
        save_state(path, st)


def snapshot(path: Path | str = DB_PATH, *, now: float | None = None,
             timeline_limit: int = 20, mark: bool = False) -> dict:
    """console/调试用：心情值 + 7 情绪 + 最近时间线 + 下一轮会注入的原文。"""
    now = _now(now)
    cfg = settings()
    st = load_state(path)
    st["mood"], st["dims"], elapsed = decay_values(
        st["mood"], st["dims"], st["last_decay_at"], now,
        half_life_h=cfg["half_life_h"], dim_half_life_min=cfg["dim_half_life_min"])
    preview = context_line(path, now=now, mark=mark)
    con = db.connect(path)
    try:
        rows = [dict(row) for row in con.execute(
            "SELECT * FROM mood_timeline ORDER BY at DESC LIMIT ?",
            (int(timeline_limit),)).fetchall()]
    finally:
        con.close()
    timeline = []
    for row in rows:
        try:
            dims_delta = json.loads(str(row.get("dims_delta_json") or "{}"))
        except (TypeError, ValueError):
            dims_delta = {}
        timeline.append({
            "id": row.get("id"), "at": row.get("at"), "mood": row.get("mood"),
            "reason": row.get("reason") or "", "confidence": row.get("confidence") or 0,
            "mode": row.get("mode") or "", "dims_delta": dims_delta,
            "user_text": row.get("user_text") or "",
            "reply_text": row.get("reply_text") or "",
        })
    pending = float(st.get("last_at") or 0.0) > float(st.get("last_injected_at") or 0.0)
    return {
        "mood": round(st["mood"], 2),
        "mood_label": mood_label(st["mood"]),
        "dims": {name: round(st["dims"][name], 2) for name in DIMS},
        "top_dims": top_dims(st["dims"], 2, threshold=0.0),
        "last_reason": st.get("last_reason") or "",
        "last_confidence": st.get("last_confidence") or 0,
        "last_mode": st.get("last_mode") or "",
        "last_at": st.get("last_at") or 0.0,
        "last_injected_at": st.get("last_injected_at") or 0.0,
        "pending_inject": bool(pending),
        "injected_preview": preview,
        "settings": {key: cfg[key] for key in
                     ("judge_enabled", "inject", "async", "judge_backend",
                      "confidence_min", "mood_min", "half_life_h",
                      "dim_half_life_min", "server_url", "timeout_s",
                      "deepseek_timeout_s")},
        "timeline": timeline,
        "elapsed_seconds": round(elapsed, 3),
    }
