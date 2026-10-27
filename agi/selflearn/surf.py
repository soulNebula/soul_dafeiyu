# -*- coding: utf-8 -*-
"""P25：网上冲浪引擎（纯逻辑，可单测；不联网、不发消息）。

用户要小鱼自己「上网冲浪」：10:00–22:00 每 30 分钟投一次骰子，骰子 + 冲浪因子
决定这一轮冲不冲、冲多久、看几条；冲浪产出（兴趣关键词 → 搜索 → 我的看法）
沉淀进兴趣库与 ``surf_sessions``，**默认不主动发消息**。

公式（用户原话 + Lead 优化，逐条实现）：

- 窗口：10:00–22:00；窗口外休眠（不投骰子）
- gap = clamp(base_min + round((0.9 - factor) * 12), 15, 45) 分钟，base_min 默认 30
- 执行：``dice >= 3 且 dice * factor >= 2.4``
- 时长：``clamp(round(2 * dice * factor), 3, 12)`` 分钟
- 条数：``randint(2, min(20, max(3, round(dice * 3.5))))``
- 因子 factor ∈ [0.2, 1.8]：
  - 初始 base = clamp(0.5 + (mood-0.5)*0.8 + (energy-0.5)*0.6 + N(0,0.25), 0.2, 1.6)
  - 执行后衰减：dice=6 → ×0.55；dice 3–5 → ×0.8（下限 0.25）
  - 每次投骰 +0.12 向 1.0 恢复（上限 1.8）
  - 用户主动触发 +0.1（上限 1.8）

本模块只做纯计算 / 状态文件 / DB 读写；线程与调度在 ``surf_worker``。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import db
from .config import DATA_DIR, DB_PATH

# ------------------------------------------------------------------ 常量
#: 状态文件（用户点名路径）
STATE_PATH = DATA_DIR / "surf_state.json"
#: 观测日志（没有 gateway recorder 时，CLI/独立运行写这里）
SURF_LOG = DATA_DIR / "surf.log"

DICE_FACES = 6
WINDOW_DEFAULT = (10, 22)
ROLL_INTERVAL_S = 1800.0
MAX_ITEMS = 20
FACTOR_MIN = 0.2
FACTOR_MAX = 1.8
FACTOR_FLOOR = 0.25
FACTOR_DECAY_DICE6 = 0.55
FACTOR_DECAY_OTHER = 0.8
FACTOR_RECOVER = 0.12
FACTOR_USER_BOOST = 0.1
EXEC_DICE_MIN = 3
EXEC_FACTOR_PRODUCT = 2.4
DURATION_MIN = 3
DURATION_MAX = 12
BASE_FACTOR_MIN = 0.2
BASE_FACTOR_MAX = 1.6
#: 无兴趣关键词时的占位主题（用户提到「天气/新闻占位」）
FALLBACK_KEYWORDS = ("今天的天气", "最近的新闻")
#: 单条 my_take 的规则模板（本地规则，不调贵 API）
MY_TAKE_TEMPLATES = (
    "「{title}」这个角度我之前没太想过",
    "关于{keyword}，这条说法挺实在的",
    "{keyword}比我想的更有门道，想再翻翻",
    "看完对{keyword}多了点具体的印象",
    "「{title}」，先记一笔，回头细看",
)

_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS surf_sessions(
  id TEXT PRIMARY KEY,
  started_at REAL NOT NULL,
  ended_at REAL,
  dice INTEGER NOT NULL DEFAULT 0,
  factor REAL NOT NULL DEFAULT 0,
  executed INTEGER NOT NULL DEFAULT 0,
  duration_min REAL NOT NULL DEFAULT 0,
  items_seen INTEGER NOT NULL DEFAULT 0,
  keywords_json TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_surf_sessions_started ON surf_sessions(started_at);
"""


# ------------------------------------------------------------------ 小工具
def clamp(value: Any, low: float, high: float, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    return max(float(low), min(float(high), number))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in ("0", "false", "off", "no", "")


def enabled() -> bool:
    """总开关 ``surf_enabled``（env ``SURF_ENABLED`` > config.SURF_ENABLED）。"""
    try:
        from .config import SURF_ENABLED
    except Exception:  # noqa: BLE001
        SURF_ENABLED = True
    return _env_bool("SURF_ENABLED", bool(SURF_ENABLED))


def window() -> tuple[int, int]:
    """冲浪窗口（本地小时，左闭右开）；env ``SURF_WINDOW="10,22"``。"""
    try:
        from .config import SURF_WINDOW
    except Exception:  # noqa: BLE001
        SURF_WINDOW = WINDOW_DEFAULT
    raw = os.environ.get("SURF_WINDOW")
    source: Any = SURF_WINDOW
    if raw:
        source = raw
    try:
        if isinstance(source, str):
            parts = [int(x) for x in source.replace("，", ",").split(",")[:2]]
        else:
            parts = [int(x) for x in list(source)[:2]]
        if len(parts) == 2 and all(0 <= h <= 24 for h in parts) and parts[0] != parts[1]:
            return parts[0], parts[1]
    except (TypeError, ValueError):
        pass
    return WINDOW_DEFAULT


def roll_interval_s() -> float:
    """基准投骰间隔（秒）；env ``SURF_ROLL_INTERVAL_S``。"""
    try:
        from .config import SURF_ROLL_INTERVAL_S
    except Exception:  # noqa: BLE001
        SURF_ROLL_INTERVAL_S = ROLL_INTERVAL_S
    return max(60.0, _env_float("SURF_ROLL_INTERVAL_S", float(SURF_ROLL_INTERVAL_S)))


def max_items() -> int:
    try:
        from .config import SURF_MAX_ITEMS
    except Exception:  # noqa: BLE001
        SURF_MAX_ITEMS = MAX_ITEMS
    return max(3, min(50, _env_int("SURF_MAX_ITEMS", int(SURF_MAX_ITEMS))))


# ------------------------------------------------------------------ 公式
def roll_dice(rng: random.Random | None = None) -> int:
    """1–6 均匀骰子。"""
    return int((rng or random).randint(1, DICE_FACES))


def gap_minutes(factor: float, base_min: float = 30.0) -> int:
    """下一轮检查间隔（分钟）：因子高 → 间隔短；夹紧 15–45 分钟。"""
    value = float(base_min) + round((0.9 - float(factor)) * 12)
    return int(clamp(round(value), 15, 45))


def should_execute(dice: int, factor: float) -> bool:
    """执行判定：点数 ≥3 且 点数×因子 ≥2.4。"""
    try:
        return int(dice) >= EXEC_DICE_MIN and int(dice) * float(factor) >= EXEC_FACTOR_PRODUCT
    except (TypeError, ValueError):
        return False


def duration_minutes(dice: int, factor: float) -> int:
    """这一轮冲浪时长（分钟）：3–12。"""
    try:
        raw = round(2 * int(dice) * float(factor))
    except (TypeError, ValueError):
        raw = DURATION_MIN
    return int(clamp(raw, DURATION_MIN, DURATION_MAX))


def items_count(dice: int, rng: random.Random | None = None,
                cap: int | None = None) -> int:
    """这一轮看几条：randint(2, min(cap, max(3, round(dice*3.5))))。"""
    try:
        upper = min(int(cap or max_items()), max(3, round(int(dice) * 3.5)))
    except (TypeError, ValueError):
        upper = 3
    upper = max(2, upper)
    return int((rng or random).randint(2, upper))


def base_factor(mood: float, energy: float, noise: float | None = None) -> float:
    """冲浪因子初值：心情/精力绑定 + 噪声，夹紧 0.2–1.6。"""
    noise = float(noise) if noise is not None else random.gauss(0.0, 0.25)
    value = 0.5 + (float(mood) - 0.5) * 0.8 + (float(energy) - 0.5) * 0.6 + noise
    return round(clamp(value, BASE_FACTOR_MIN, BASE_FACTOR_MAX), 4)


def decay_factor(factor: float, dice: int) -> float:
    """执行后衰减：dice=6 ×0.55，其余 ×0.8（下限 0.25）。"""
    rate = FACTOR_DECAY_DICE6 if int(dice) >= 6 else FACTOR_DECAY_OTHER
    return round(max(FACTOR_FLOOR, float(factor) * rate), 4)


def recover_factor(factor: float, amount: float = FACTOR_RECOVER) -> float:
    """每次投骰向 1.0 恢复 +0.12（上限 1.8）。"""
    return round(min(FACTOR_MAX, float(factor) + float(amount)), 4)


def boost_factor(factor: float, amount: float = FACTOR_USER_BOOST) -> float:
    """用户主动触发奖励 +0.1（上限 1.8）。"""
    return round(min(FACTOR_MAX, float(factor) + float(amount)), 4)


def mood_energy(db_path: Path | str | None = None) -> tuple[float, float]:
    """从 mood_judge 快照读心情/精力（0–1），不调 LLM。

    - mood：``mood_state.mood``（0–100，基线 70）/ 100；
    - energy：由 7 情绪推（happy/sweet 高 → 高；sad/worry 高 → 低），
      ``0.5 + 0.5*happy + 0.3*sweet - 0.5*sad - 0.4*worry``，夹紧 0–1。
    """
    st: dict[str, Any] = {}
    try:
        from . import mood_judge
        st = mood_judge.load_state(db_path or DB_PATH)
    except Exception:  # noqa: BLE001 - 心情层不可用不影响冲浪
        st = {}
    mood = clamp(float(st.get("mood") or 70.0) / 100.0, 0.0, 1.0, 0.7)
    dims = st.get("dims") if isinstance(st.get("dims"), dict) else {}

    def _dim(name: str) -> float:
        return clamp(float(dims.get(name) or 0.0) / 100.0, 0.0, 1.0)

    energy = clamp(0.5 + 0.5 * _dim("happy") + 0.3 * _dim("sweet")
                   - 0.5 * _dim("sad") - 0.4 * _dim("worry"), 0.0, 1.0, 0.6)
    return round(mood, 3), round(energy, 3)


def init_factor(db_path: Path | str | None = None,
                rng: random.Random | None = None) -> dict[str, Any]:
    """用当前心情/精力初始化因子。"""
    mood, energy = mood_energy(db_path)
    noise = rng.gauss(0.0, 0.25) if rng is not None else random.gauss(0.0, 0.25)
    base = base_factor(mood, energy, noise)
    return {"factor": base, "base": base, "mood": mood, "energy": energy}


# ------------------------------------------------------------------ 窗口
def _window_hours(window_hours: Any = None) -> tuple[int, int]:
    if window_hours is None:
        return window()
    try:
        start, end = window_hours
        return int(start), int(end)
    except (TypeError, ValueError):
        return window()


def in_window(now: float | None = None, window_hours: Any = None) -> bool:
    """当前小时是否在冲浪窗口内（左闭右开）。"""
    now = time.time() if now is None else float(now)
    start, end = _window_hours(window_hours)
    hour = time.localtime(now).tm_hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end   # 跨零点窗口（保留支持）


def window_bounds(now: float | None = None,
                  window_hours: Any = None) -> tuple[float, float]:
    """当天的窗口起止时间戳。"""
    now = time.time() if now is None else float(now)
    start_h, end_h = _window_hours(window_hours)
    tm = time.localtime(now)
    day = datetime(tm.tm_year, tm.tm_mon, tm.tm_mday)
    start = (day + timedelta(hours=start_h)).timestamp()
    end = (day + timedelta(hours=end_h)).timestamp()
    return start, end


def next_window_start(now: float | None = None, window_hours: Any = None) -> float:
    """下一个窗口起点：今天未到点 → 今天；已过起点（含窗口结束）→ 明天。"""
    now = time.time() if now is None else float(now)
    start, _end = window_bounds(now, window_hours)
    if now < start:
        return start
    return (datetime.fromtimestamp(start) + timedelta(days=1)).timestamp()


def next_roll_at(now: float | None = None, factor: float = 1.0,
                 window_hours: Any = None, base_min: float | None = None) -> float:
    """下一轮投骰时刻；若落在窗口外/窗口结束后，顺延到下一个窗口起点。"""
    now = time.time() if now is None else float(now)
    base = roll_interval_s() / 60.0 if base_min is None else float(base_min)
    candidate = now + gap_minutes(factor, base) * 60.0
    _start, end = window_bounds(now, window_hours)
    if candidate <= end and in_window(candidate, window_hours):
        return candidate
    return next_window_start(now, window_hours)


# ------------------------------------------------------------------ 状态文件
def default_state() -> dict[str, Any]:
    return {
        "factor": 1.0, "last_roll_ts": 0.0, "next_roll_ts": 0.0, "last_dice": 0,
        "today_date": "", "rolls_today": 0, "executed_today": 0,
        "minutes_today": 0.0, "last_session_id": "", "last_summary": "",
        "updated_at": 0.0,
    }


def load_state(path: Path | str | None = None) -> dict[str, Any]:
    """读状态文件；不存在/损坏/类型不对 → 返回默认状态（不抛）。"""
    target = Path(path) if path else Path(STATE_PATH)
    out = default_state()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return out
    if not isinstance(raw, dict):
        return out
    for key in out:
        if key in raw:
            out[key] = raw[key]
    # 类型容错
    for key in ("factor", "last_roll_ts", "next_roll_ts", "minutes_today", "updated_at"):
        out[key] = clamp(out.get(key), 0.0, 1e12, out[key])
    out["factor"] = clamp(out["factor"], FACTOR_MIN, FACTOR_MAX, 1.0)
    for key in ("last_dice", "rolls_today", "executed_today"):
        try:
            out[key] = max(0, int(out.get(key) or 0))
        except (TypeError, ValueError):
            out[key] = 0
    out["today_date"] = str(out.get("today_date") or "")
    out["last_session_id"] = str(out.get("last_session_id") or "")
    out["last_summary"] = str(out.get("last_summary") or "")[:400]
    return out


def save_state(state: dict[str, Any], path: Path | str | None = None) -> Path | None:
    """原子写状态文件（tmp + replace）；失败返回 None，不抛。"""
    target = Path(path) if path else Path(STATE_PATH)
    payload = default_state()
    payload.update({key: state[key] for key in payload if key in state})
    payload["updated_at"] = time.time()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)
        return target
    except OSError:
        return None


def reset_daily(state: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """跨天重置「今日投骰/执行/累计分钟」计数。"""
    now = time.time() if now is None else float(now)
    out = dict(state or {})
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if str(out.get("today_date") or "") != today:
        out["today_date"] = today
        out["rolls_today"] = 0
        out["executed_today"] = 0
        out["minutes_today"] = 0.0
    return out


# ------------------------------------------------------------------ 投骰
def roll(state: dict[str, Any] | None = None, *, now: float | None = None,
         rng: random.Random | None = None, db_path: Path | str | None = None,
         source: str = "scheduled") -> dict[str, Any]:
    """投一次骰并更新状态（**原地改** ``state``）；返回本轮计划。"""
    now = time.time() if now is None else float(now)
    target = state if state is not None else load_state()
    resetted = reset_daily(target, now)
    if resetted is not target:      # 跨天：把重置结果写回调用方的 dict
        target.clear()
        target.update(resetted)
    state = target
    if not float(state.get("factor") or 0):
        init = init_factor(db_path, rng)
        state["factor"] = init["factor"]
        state["base_factor"] = init["base"]
    if str(source or "") == "user":
        state["factor"] = boost_factor(state["factor"])
    factor_before = clamp(state["factor"], FACTOR_MIN, FACTOR_MAX, 1.0)
    dice = roll_dice(rng)
    executed = should_execute(dice, factor_before)
    duration = duration_minutes(dice, factor_before)
    count = items_count(dice, rng)
    factor = factor_before
    if executed:
        factor = decay_factor(factor, dice)
    state["factor"] = recover_factor(factor)
    state["last_roll_ts"] = now
    state["last_dice"] = dice
    state["rolls_today"] = int(state.get("rolls_today") or 0) + 1
    state["next_roll_ts"] = next_roll_at(now, state["factor"])
    return {
        "dice": dice, "factor_before": factor_before, "factor": state["factor"],
        "effective": round(dice * factor_before, 3), "executed": executed,
        "duration_min": duration, "items": count,
        "gap_min": gap_minutes(state["factor"]),
        "next_roll_ts": state["next_roll_ts"], "started_at": now,
        "source": str(source or "scheduled"),
    }


# ------------------------------------------------------------------ DB 表
def connect(db_path: Path | str | None = None):
    """memory 库连接 + 确保 ``surf_sessions`` 表存在。"""
    con = db.memory_connect(db_path or DB_PATH)
    try:
        con.executescript(_SESSIONS_SCHEMA)
        con.commit()
    except Exception:  # noqa: BLE001 - 表已存在等
        pass
    return con


def insert_session(session: dict[str, Any], db_path: Path | str | None = None) -> None:
    con = connect(db_path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO surf_sessions(id,started_at,ended_at,dice,factor,"
            "executed,duration_min,items_seen,keywords_json,summary,source,error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(session.get("id") or ""), float(session.get("started_at") or 0.0),
             float(session.get("ended_at") or 0.0), int(session.get("dice") or 0),
             float(session.get("factor") or 0.0), 1 if session.get("executed") else 0,
             float(session.get("duration_min") or 0.0),
             int(session.get("items_seen") or 0),
             json.dumps(session.get("keywords") or [], ensure_ascii=False),
             str(session.get("summary") or "")[:200],
             str(session.get("source") or ""), str(session.get("error") or "")[:300]))
        con.commit()
    finally:
        con.close()


def recent_sessions(limit: int = 5,
                    db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """最近几轮冲浪记录（status 展示用）。"""
    try:
        con = connect(db_path)
    except Exception:  # noqa: BLE001
        return []
    try:
        rows = con.execute(
            "SELECT * FROM surf_sessions ORDER BY started_at DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
    except Exception:  # noqa: BLE001
        return []
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["keywords"] = json.loads(item.pop("keywords_json") or "[]")
        except (TypeError, ValueError):
            item["keywords"] = []
        out.append(item)
    return out


# ------------------------------------------------------------------ 冲浪内容
def fetch(keyword: str, limit: int = 5) -> dict[str, Any]:
    """可插拔取数接口（测试/替换用）。

    默认：``web_lookup.search``（w3-latency）→ 失败时 **B站摄取兜底**。
    返回 ``{ok, items:[{title,url,summary,source}], source, error}``。
    """
    keyword = str(keyword or "").strip()
    if not keyword:
        return {"ok": False, "items": [], "source": "", "error": "empty keyword"}
    limit = max(1, int(limit or 1))
    errors: list[str] = []
    # ① 联网搜索（有就优先）
    try:
        from agi.integrations import web_lookup
        if callable(getattr(web_lookup, "enabled", None)) and web_lookup.enabled():
            result = web_lookup.search(keyword)
            items = []
            for raw in (result or {}).get("items") or []:
                title = str((raw or {}).get("title") or "").strip()
                if not title:
                    continue
                items.append({"title": title,
                              "url": str(raw.get("url") or ""),
                              "summary": str(raw.get("summary") or "")[:200],
                              "source": str((result or {}).get("source") or "网页")})
            if items:
                return {"ok": True, "items": items[:limit],
                        "source": str(result.get("source") or "web"), "error": ""}
            errors.append("web_lookup: empty")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"web_lookup: {type(exc).__name__}: {str(exc)[:60]}")
    # ② B站摄取兜底（未落地 web_lookup 时也能冲浪）
    try:
        from agi.integrations import bili_ingest
        rows = bili_ingest.search_videos(keyword, limit=limit) or []
        items = [{"title": str((row or {}).get("title") or "").strip(),
                  "url": f"https://www.bilibili.com/video/{row.get('bvid')}",
                  "summary": "", "source": "B站"}
                 for row in rows if str((row or {}).get("title") or "").strip()]
        if items:
            return {"ok": True, "items": items[:limit], "source": "bilibili", "error": ""}
        errors.append("bili_ingest: empty")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"bili_ingest: {type(exc).__name__}: {str(exc)[:60]}")
    return {"ok": False, "items": [], "source": "", "error": "; ".join(errors[-2:])}


def make_my_take(item: dict[str, Any], keyword: str = "", index: int = 0) -> str:
    """规则生成一句「我的看法」（≤60 字，不调贵 API、不编造细节）。

    ``index`` 用于同一轮里错开模板，避免几条结果说出同一句话。
    """
    title = str((item or {}).get("title") or "").strip()
    keyword = str(keyword or (item or {}).get("keyword") or "").strip()
    short = title[:16] or keyword or "这条"
    digest = int(hashlib.sha1(f"{title}|{keyword}".encode("utf-8")).hexdigest()[:6], 16)
    template = MY_TAKE_TEMPLATES[(digest + max(0, int(index))) % len(MY_TAKE_TEMPLATES)]
    return template.format(title=short, keyword=keyword or "这事")[:60]


def pick_keywords(db_path: Path | str | None = None, *, limit: int = 2) -> list[str]:
    """兴趣关键词；拿不到就用天气/新闻占位。"""
    keywords: list[str] = []
    try:
        from . import interests
        raw = interests.next_search_keywords(limit, path=db_path)
        keywords = [str(item).strip() for item in raw or [] if str(item).strip()]
    except Exception:  # noqa: BLE001
        keywords = []
    if not keywords:
        keywords = list(FALLBACK_KEYWORDS)
    return keywords[:max(1, int(limit))]


def summarize(items: list[dict[str, Any]], keywords: list[str]) -> str:
    """本轮摘要（≤200 字）：看了几条 + 关键词 + 前几条 my_take。"""
    if not items:
        return "这轮没看到什么，先记着"[:200]
    head = f"看了{len(items)}条（{'、'.join(keywords[:3])}）"
    body = "；".join(str(item.get("my_take") or "") for item in items[:4])
    text = f"{head}：{body}" if body else head
    return text[:200]


def run_session(plan: dict[str, Any], *, now: float | None = None,
                db_path: Path | str | None = None,
                fetch_fn=None, observe_fn=None,
                keywords: list[str] | None = None) -> dict[str, Any]:
    """执行一轮冲浪：取关键词 → 搜索 → 每条 my_take → 兴趣记账 → 写 session。"""
    now = time.time() if now is None else float(now)
    fetcher = fetch_fn if callable(fetch_fn) else fetch
    kws = [str(k).strip() for k in (keywords if keywords is not None
                                    else pick_keywords(db_path)) if str(k).strip()]
    if not kws:
        kws = list(FALLBACK_KEYWORDS)
    want = max(1, int(plan.get("items") or 3))
    per_kw = max(1, math.ceil(want / max(1, len(kws))))
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    sources: list[str] = []
    for keyword in kws:
        if len(items) >= want:
            break
        try:
            result = fetcher(keyword, per_kw) or {}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{keyword}: {type(exc).__name__}: {str(exc)[:60]}")
            continue
        if result.get("source"):
            sources.append(str(result.get("source")))
        if not result.get("ok"):
            if result.get("error"):
                errors.append(f"{keyword}: {str(result.get('error'))[:60]}")
            continue
        for raw in result.get("items") or []:
            item = {"title": str((raw or {}).get("title") or "").strip(),
                    "url": str((raw or {}).get("url") or ""),
                    "summary": str((raw or {}).get("summary") or "")[:200],
                    "source": str((raw or {}).get("source") or ""),
                    "keyword": keyword}
            if not item["title"]:
                continue
            item["my_take"] = make_my_take(item, keyword, index=len(items))
            items.append(item)
            if len(items) >= want:
                break
    summary = summarize(items, kws)
    session_id = "SURF-" + hashlib.sha1(
        f"{now}|{plan.get('dice')}|{'|'.join(kws)}".encode("utf-8")).hexdigest()[:14]
    # 兴趣记账（source=self；失败不影响本轮结果）
    observer = observe_fn
    if observer is None:
        try:
            from . import interests
            observer = interests.observe
        except Exception:  # noqa: BLE001
            observer = None
    if callable(observer):
        try:
            observer([str(i["title"]) for i in items[:6]] + kws,
                     source="self", path=db_path,
                     ref={"surf_session": session_id, "keywords": kws},
                     _topics=kws)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"observe: {type(exc).__name__}: {str(exc)[:60]}")
    session = {
        "id": session_id, "started_at": float(plan.get("started_at") or now),
        "ended_at": now, "dice": int(plan.get("dice") or 0),
        "factor": float(plan.get("factor") or 0.0), "executed": True,
        "duration_min": float(plan.get("duration_min") or 0.0),
        "items_seen": len(items), "keywords": kws, "summary": summary,
        "source": str(plan.get("source") or "scheduled"),
        "error": "; ".join(errors[-2:]),
    }
    try:
        insert_session(session, db_path)
    except Exception as exc:  # noqa: BLE001 - 落库失败不影响返回
        errors.append(f"db: {type(exc).__name__}: {str(exc)[:60]}")
        session["error"] = "; ".join(errors[-2:])
    return {"ok": bool(items), "id": session_id, "items": items,
            "items_seen": len(items), "keywords": kws, "summary": summary,
            "sources": sorted(set(sources)), "error": session["error"],
            "session": session}


# ------------------------------------------------------------------ 状态汇总
def status(*, now: float | None = None, state: dict[str, Any] | None = None,
           state_path: Path | str | None = None,
           db_path: Path | str | None = None, engine_enabled: bool = True,
           enabled_override: bool | None = None,
           window_hours: Any = None) -> dict[str, Any]:
    """给 ``GET /api/surf/status`` / CLI 用的状态快照。"""
    now = time.time() if now is None else float(now)
    state = reset_daily(state if state is not None else load_state(state_path), now)
    start, end = window_bounds(now, window_hours)
    next_roll = float(state.get("next_roll_ts") or 0.0)
    if not next_roll:
        if in_window(now, window_hours):
            next_roll = now + gap_minutes(
                float(state.get("factor") or 1.0), roll_interval_s() / 60.0) * 60.0
            if next_roll > end:
                next_roll = next_window_start(now, window_hours)
        else:
            next_roll = next_window_start(now, window_hours)
    mood, energy = mood_energy(db_path)
    sessions = recent_sessions(3, db_path)
    return {
        "ok": True,
        "enabled": bool(enabled() if enabled_override is None else enabled_override),
        "engine_enabled": bool(engine_enabled),
        "window": list(_window_hours(window_hours)),
        "in_window": in_window(now, window_hours),
        "window_start_ts": start, "window_end_ts": end,
        "now": now,
        "factor": round(float(state.get("factor") or 0.0), 3),
        "base_factor": round(base_factor(mood, energy, 0.0), 3),
        "mood": mood, "energy": energy,
        "last_dice": int(state.get("last_dice") or 0),
        "last_roll_ts": float(state.get("last_roll_ts") or 0.0),
        "next_roll_ts": next_roll,
        "next_roll_in_s": max(0.0, round(next_roll - now, 1)) if next_roll else 0.0,
        "today": {"date": state.get("today_date"),
                  "rolls": int(state.get("rolls_today") or 0),
                  "executed": int(state.get("executed_today") or 0),
                  "minutes": round(float(state.get("minutes_today") or 0.0), 1)},
        "last_summary": str(state.get("last_summary") or "")[:200],
        "recent_sessions": sessions,
    }
