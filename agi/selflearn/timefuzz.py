# -*- coding: utf-8 -*-
"""P1-e：统一时间模糊层（唯一真源）。

规则（用户已批准）：
- 老事情只保留模糊时间（"前阵子/更早以前/去年"）；
- **任何注入 prompt 的字符串不得出现绝对日期或精确天数**；
- 日志 / 导出 / 数据库可以保留绝对时间（只要不注入 prompt）。

所有表达时间位置的地方都应调用本模块：
``memory.render_memories`` / ``summary.context`` / ``retrieval.context`` /
``personality.context`` / ``chat._prompt``（最终兜底 strip）。
"""
from __future__ import annotations

import re
import time
from typing import Any

#: relative() 的 7 档边界（秒，**含右不含左**：age <= 阈值 命中该档）
TIER_1H_S = 3600.0
TIER_1D_S = 86400.0
TIER_3D_S = 3 * 86400.0
TIER_7D_S = 7 * 86400.0
TIER_30D_S = 30 * 86400.0
TIER_90D_S = 90 * 86400.0
TIER_365D_S = 365 * 86400.0

#: 各档说法（第一档细分「刚才 / 今天」，避免 20 小时前说"刚才"）
LABEL_JUST_NOW = "刚才"
LABEL_TODAY = "今天"
LABEL_TWO_DAYS = "这两天"
LABEL_FEW_DAYS = "前几天"
LABEL_RECENT = "这阵子"
LABEL_A_WHILE = "前阵子"
LABEL_EARLIER = "更早以前"
LABEL_LONG_AGO = "很久以前"

#: 供渲染层复用的已定义说法（与 relative 输出一致）
WARM_LABEL = LABEL_A_WHILE       # 30–90 天
COLD_LABEL = LABEL_EARLIER       # 90–365 天
COLD_OLD_LABEL = LABEL_LONG_AGO  # >365 天

#: 绝对时间片段（供 has_absolute 检测，也用于 strip_absolute 的定位）
_ABS_RE = re.compile(
    r"(?:\d{4}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?"
    r"|\d{4}\s*[-/]\s*\d{1,2}(?:\s*[-/]\s*\d{1,2})?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|\d{1,2}\s*/\s*\d{1,2}"
    r"|\d+\s*天前?|\d+\s*个?月前?|\d+\s*年前?)"
)

#: 常出现在 prompt 里的精确相对说法 → 更模糊的说法（幂等）
_CUE_SUBS = (
    ("昨天", LABEL_TWO_DAYS),
    ("前天", LABEL_FEW_DAYS),
    ("大前天", LABEL_FEW_DAYS),
)


def _as_ts(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def relative(ts: Any, now: float | None = None) -> str:
    """把时间戳转成模糊说法（7 档，边界含右不含左）。

    ≤1 小时「刚才」、≤1 天「今天」、≤3 天「这两天」、≤7 天「前几天」、
    ≤30 天「这阵子」、≤90 天「前阵子」、≤365 天「更早以前」、>365 天「很久以前」。
    未来时间/非法值不崩：未来按「刚才」，非法返回空串。
    """
    now = time.time() if now is None else float(now)
    stamp = _as_ts(ts)
    if stamp is None:
        return ""
    age = now - stamp
    if age <= TIER_1H_S:          # 含未来（age<0）按「刚才」
        return LABEL_JUST_NOW
    if age <= TIER_1D_S:
        return LABEL_TODAY
    if age <= TIER_3D_S:
        return LABEL_TWO_DAYS
    if age <= TIER_7D_S:
        return LABEL_FEW_DAYS
    if age <= TIER_30D_S:
        return LABEL_RECENT
    if age <= TIER_90D_S:
        return LABEL_A_WHILE
    if age <= TIER_365D_S:
        return LABEL_EARLIER
    return LABEL_LONG_AGO


def _label_for_days(days: float) -> str:
    return relative(time.time() - max(0.0, days) * 86400.0)


def strip_absolute(text: Any) -> str:
    """把文本里的绝对时间表述替换成模糊说法（幂等；不注入 prompt 的调用方不要用）。"""
    value = str(text or "")
    if not value:
        return value

    def _replace(match: re.Match) -> str:
        token = match.group(0)
        # 完整日期 / 年月 / 年 / ISO：无参照点，统一按"更早以前"
        if re.search(r"\d{4}\s*年", token) or re.search(r"\d{4}\s*[-/]", token):
            return LABEL_EARLIER
        # 月-日 / M/D：归到"前阵子"
        if re.search(r"\d{1,2}\s*月\s*\d{1,2}\s*日", token) or re.fullmatch(
                r"\d{1,2}\s*/\s*\d{1,2}", token):
            return LABEL_A_WHILE
        # N 天/月/年（前）：按量级映射到相对档位
        unit = match.group(0)
        number = re.search(r"\d+", unit)
        count = float(number.group(0)) if number else 0.0
        if "天" in unit:
            return _label_for_days(count)
        if "月" in unit:
            return _label_for_days(count * 30.0)
        if "年" in unit:
            return _label_for_days(count * 365.0)
        return LABEL_EARLIER

    value = _ABS_RE.sub(_replace, value)
    for cue, replacement in _CUE_SUBS:
        value = value.replace(cue, replacement)
    return value


def has_absolute(text: Any) -> list[str]:
    """返回文本里命中的绝对时间片段（空列表 = 干净，可直接进 prompt）。"""
    return [m.group(0).strip() for m in _ABS_RE.finditer(str(text or ""))]


def is_clean(text: Any) -> bool:
    """便捷判断：没有绝对时间片段。"""
    return not has_absolute(text)
