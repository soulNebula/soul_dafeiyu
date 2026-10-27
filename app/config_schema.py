# -*- coding: utf-8 -*-
"""配置 schema：工作台里每个配置项的类型/范围/说明/分组。

原则：
- 只列「人应该改」的项；内部实现细节不暴露；
- 每项声明 type/默认值/范围/是否需重启/是否敏感；
- 保存前按 schema 校验，避免把系统改坏。
"""
from __future__ import annotations

from typing import Any

# type: bool | int | float | str | str_list | hour_range
SCHEMA: list[dict[str, Any]] = [
    # ---------------- 基础 ----------------
    {"key": "enabled", "group": "基础", "type": "bool", "default": True,
     "label": "网关总开关", "desc": "关闭后整个网关不工作（等价于停机）", "restart": False},
    {"key": "engine_enabled", "group": "基础", "type": "bool", "default": True,
     "label": "引擎（监听+回复+主动）", "desc": "软停：停止监听/自动回复/主动消息；控制台与人工发送仍可用", "restart": False},
    {"key": "auto_reply", "group": "基础", "type": "bool", "default": True,
     "label": "自动回复", "desc": "关闭后只记录不回复", "restart": False},
    {"key": "dry_run", "group": "基础", "type": "bool", "default": False,
     "label": "演练模式", "desc": "只生成不发送，用于调试", "restart": False},
    {"key": "stop", "group": "基础", "type": "bool", "default": False,
     "label": "紧急停止（STOP）", "desc": "所有发送被拦截（含人工），需要手动解除", "restart": False},
    {"key": "admin_token", "group": "基础", "type": "str", "default": "8888",
     "label": "管理令牌", "desc": "控制台/网关 API 的令牌", "secret": True, "restart": True},
    {"key": "port", "group": "基础", "type": "int", "default": 801, "min": 1, "max": 65535,
     "label": "网关端口", "desc": "修改后需要重启网关", "restart": True},
    {"key": "note", "group": "基础", "type": "str", "default": "",
     "label": "备注", "desc": "给自己看的说明", "restart": False},

    # ---------------- 联系人 ----------------
    {"key": "allowed_contacts", "group": "联系人", "type": "str_list", "default": [],
     "label": "允许的联系人", "desc": "只处理这些好友的消息", "restart": True},
    {"key": "monitored_contacts", "group": "联系人", "type": "str_list", "default": [],
     "label": "监听的联系人", "desc": "实际监听的会话窗口", "restart": True},
    {"key": "proactive_contacts", "group": "联系人", "type": "str_list", "default": [],
     "label": "主动消息联系人", "desc": "允许主动发消息的联系人", "restart": False},
    {"key": "mention_name", "group": "联系人", "type": "str", "default": "",
     "label": "@ 识别名", "desc": "私聊里 @ 这个名字会被识别为点名（如：小鱼）", "restart": True},

    # ---------------- 发送节奏 ----------------
    {"key": "send_mode", "group": "发送", "type": "str", "default": "confirm", "enum": ["auto", "confirm", "off"],
     "label": "发送模式", "desc": "auto=自动发送；confirm=只生成草稿等人工批准；off=不发送", "restart": False},
    {"key": "reply_delay", "group": "发送", "type": "float", "default": 0.5, "min": 0, "max": 30,
     "label": "回复延迟（秒）", "desc": "收到消息后等待多久再生成", "restart": False},
    {"key": "reply_debounce_ms", "group": "发送", "type": "int", "default": 2000, "min": 0, "max": 60000,
     "label": "防抖（毫秒）", "desc": "连续多条消息合并成一次回复", "restart": False},
    {"key": "rate_per_minute", "group": "发送", "type": "int", "default": 3, "min": 1, "max": 60,
     "label": "每分钟上限", "desc": "主动/全部消息的发送频率上限", "restart": False},
    {"key": "hourly_limit", "group": "发送", "type": "int", "default": 40, "min": 1, "max": 500,
     "label": "每小时上限", "desc": "防止刷屏的硬上限", "restart": False},
    {"key": "daily_limit", "group": "发送", "type": "int", "default": 200, "min": 1, "max": 5000,
     "label": "每天上限", "desc": "每天最多发送条数", "restart": False},
    {"key": "quiet_hours", "group": "发送", "type": "hour_range", "default": [22, 9],
     "label": "静默时段", "desc": "该时段不主动发消息（可跨零点，如 22→9）", "restart": False},

    # ---------------- 主动消息 ----------------
    {"key": "proactive_enabled", "group": "主动", "type": "bool", "default": True,
     "label": "主动消息开关", "desc": "是否允许她主动找你", "restart": False},
    {"key": "proactive_style", "group": "主动", "type": "str", "default": "simple", "enum": ["simple", "legacy"],
     "label": "主动风格", "desc": "simple=短句自然；legacy=旧版模板", "restart": False},
    {"key": "proactive_probability_min", "group": "主动", "type": "float", "default": 0.5, "min": 0, "max": 1,
     "label": "概率下限", "desc": "每轮投骰的触发概率下限", "restart": False},
    {"key": "proactive_probability_max", "group": "主动", "type": "float", "default": 0.6, "min": 0, "max": 1,
     "label": "概率上限", "desc": "每轮投骰的触发概率上限", "restart": False},
    {"key": "proactive_interval_min_s", "group": "主动", "type": "int", "default": 3600, "min": 600, "max": 86400,
     "label": "最短间隔（秒）", "desc": "两次主动消息之间的最小间隔", "restart": False},
    {"key": "proactive_interval_max_s", "group": "主动", "type": "int", "default": 7200, "min": 600, "max": 86400,
     "label": "最长间隔（秒）", "desc": "两次主动消息之间的最大间隔", "restart": False},
    {"key": "proactive_idle_s", "group": "主动", "type": "int", "default": 900, "min": 60, "max": 86400,
     "label": "静默多久后可主动", "desc": "用户最后一条消息后多久才允许主动", "restart": False},
    {"key": "pacing_enabled", "group": "主动", "type": "bool", "default": True,
     "label": "节奏对等", "desc": "按双方消息量比例调节主动频率", "restart": False},
    {"key": "pacing_target_ratio", "group": "主动", "type": "float", "default": 1.0, "min": 0.2, "max": 3.0,
     "label": "目标比例", "desc": "它主动条数 / 你主动条数 的目标值", "restart": False},
    {"key": "pacing_daily_cap_min", "group": "主动", "type": "int", "default": 12, "min": 1, "max": 100,
     "label": "日上限-最小", "desc": "当天你没怎么聊时的上限", "restart": False},
    {"key": "pacing_daily_cap_max", "group": "主动", "type": "int", "default": 100, "min": 1, "max": 100,
     "label": "日上限-最大", "desc": "当天你聊得多时的上限（按聊天量动态计算）", "restart": False},
    {"key": "initiative_enabled", "group": "主动", "type": "bool", "default": True,
     "label": "关系主动度", "desc": "越熟/心情越好越主动（drive）", "restart": False},
    {"key": "urges_enabled", "group": "主动", "type": "bool", "default": True,
     "label": "内驱力队列", "desc": "攒'想跟他说'的欲望，合适时机优先说", "restart": False},

    # ---------------- 冲浪 ----------------
    {"key": "surf_enabled", "group": "冲浪", "type": "bool", "default": True,
     "label": "网上冲浪", "desc": "让她自己上网看东西（10–22 点投骰）", "restart": True},
    {"key": "surf_window", "group": "冲浪", "type": "hour_range", "default": [10, 22],
     "label": "冲浪时段", "desc": "只在这个时段可能冲浪", "restart": True},
    {"key": "surf_roll_interval_s", "group": "冲浪", "type": "int", "default": 1800, "min": 300, "max": 21600,
     "label": "投骰间隔（秒）", "desc": "默认每 30 分钟投一次骰子", "restart": True},
    {"key": "surf_max_items", "group": "冲浪", "type": "int", "default": 20, "min": 1, "max": 50,
     "label": "单轮最多看几条", "desc": "冲浪一轮最多浏览的条目数", "restart": False},

    # ---------------- 日记与叙事 ----------------
    {"key": "diary_enabled", "group": "日记", "type": "bool", "default": True,
     "label": "每日日记", "desc": "每天 23:00 写一篇日记", "restart": True},
    {"key": "diary_hour", "group": "日记", "type": "int", "default": 23, "min": 0, "max": 23,
     "label": "日记时间（点）", "desc": "每天几点写日记", "restart": True},
    {"key": "narrative_enabled", "group": "日记", "type": "bool", "default": True,
     "label": "周复盘/自我叙事", "desc": "每周日 23:30 复盘并更新'我是谁'", "restart": True},

    # ---------------- 记忆 ----------------
    {"key": "memory_decay_enabled", "group": "记忆", "type": "bool", "default": True,
     "label": "记忆衰减", "desc": "类海马体：不常想起的记忆逐渐变淡", "restart": True},
    {"key": "memory_purge_enabled", "group": "记忆", "type": "bool", "default": False,
     "label": "休眠记忆清理", "desc": "200 天后物理删除休眠记忆（默认仅 dry-run）", "restart": True},
    {"key": "memory_sleep_hour", "group": "记忆", "type": "int", "default": 3, "min": 0, "max": 23,
     "label": "睡眠整理时间（点）", "desc": "每天几点做记忆衰减/压缩/清理", "restart": True},
    {"key": "trace_enabled", "group": "记忆", "type": "bool", "default": True,
     "label": "推理链路记录", "desc": "每条回复记录完整 trace（含 reasoning）", "restart": False},
    {"key": "trace_max_rows", "group": "记忆", "type": "int", "default": 5000, "min": 100, "max": 200000,
     "label": "trace 保留条数", "desc": "超过后删最旧", "restart": False},

    # ---------------- 模型与内状态 ----------------
    {"key": "inner_mode", "group": "模型", "type": "str", "default": "off", "enum": ["off", "model", "rule"],
     "label": "内状态模式", "desc": "off=不生成；rule=规则；model=本地 0.5B", "restart": False},
    {"key": "inner_shadow_only", "group": "模型", "type": "bool", "default": True,
     "label": "内状态仅影子", "desc": "只记录不注入提示词", "restart": False},
    {"key": "inner_schema", "group": "模型", "type": "str", "default": "slim", "enum": ["slim", "full"],
     "label": "内状态字段", "desc": "slim=行为字段；full=全部字段", "restart": False},
    {"key": "soul_server_url", "group": "模型", "type": "str", "default": "http://127.0.0.1:8123",
     "label": "灵魂模型地址", "desc": "本地内状态服务（llama-server）", "restart": True},
    {"key": "soul_timeout_s", "group": "模型", "type": "float", "default": 2.0, "min": 0.2, "max": 30,
     "label": "灵魂模型超时（秒）", "desc": "超时回退规则", "restart": False},
    {"key": "llm_model", "group": "模型", "type": "str", "default": "deepseek-flash",
     "label": "对话模型", "desc": "主对话使用的模型", "restart": False},
    {"key": "llm_temperature", "group": "模型", "type": "float", "default": 0.7, "min": 0, "max": 2,
     "label": "温度", "desc": "越高越发散", "restart": False},
    {"key": "llm_max_tokens", "group": "模型", "type": "int", "default": 2048, "min": 128, "max": 32768,
     "label": "最大输出 token", "desc": "单次生成上限", "restart": False},

    # ---------------- 多媒体 ----------------
    {"key": "media_enabled", "group": "媒体", "type": "bool", "default": True,
     "label": "媒体能力", "desc": "图片/贴纸/GIF 的收发", "restart": False},

    # ---------------- 时间与联网 ----------------
    {"key": "inject_time_context", "group": "上下文", "type": "bool", "default": True,
     "label": "注入当前时间", "desc": "每轮告诉她现在几点", "restart": False},
    {"key": "web_lookup_enabled", "group": "上下文", "type": "bool", "default": True,
     "label": "联网查询", "desc": "天气/新闻/网页搜索（命中才查）", "restart": False},
    {"key": "weather_city", "group": "上下文", "type": "str", "default": "",
     "label": "默认城市", "desc": "问天气时的默认城市（空=IP 定位）", "restart": False},
    {"key": "news_enabled", "group": "上下文", "type": "bool", "default": True,
     "label": "少量新闻", "desc": "允许查 top3 新闻", "restart": False},

    # ---------------- 训练 ----------------
    {"key": "train_enabled", "group": "训练", "type": "bool", "default": True,
     "label": "训练工厂", "desc": "允许工作台触发收集/训练", "restart": False},
    {"key": "train_daily_hour", "group": "训练", "type": "int", "default": 3, "min": 0, "max": 23,
     "label": "语料收集时间（点）", "desc": "每天几点收集语料并出日报", "restart": True},
]

_BY_KEY = {item["key"]: item for item in SCHEMA}


def by_key(key: str) -> dict | None:
    return _BY_KEY.get(key)


def groups() -> list[str]:
    seen = []
    for item in SCHEMA:
        if item["group"] not in seen:
            seen.append(item["group"])
    return seen


def validate_value(item: dict, value) -> str | None:
    """返回错误信息；None 表示通过。"""
    t = item.get("type")
    if t == "bool":
        if not isinstance(value, bool):
            return "必须是 true/false"
    elif t == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return "必须是整数"
        if "min" in item and value < item["min"]:
            return f"不能小于 {item['min']}"
        if "max" in item and value > item["max"]:
            return f"不能大于 {item['max']}"
    elif t == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "必须是数字"
        if "min" in item and float(value) < float(item["min"]):
            return f"不能小于 {item['min']}"
        if "max" in item and float(value) > float(item["max"]):
            return f"不能大于 {item['max']}"
    elif t == "str":
        if not isinstance(value, str):
            return "必须是字符串"
        if "enum" in item and value not in item["enum"]:
            return f"只能是 {item['enum']} 之一"
    elif t == "str_list":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            return "必须是字符串列表"
    elif t == "hour_range":
        if (not isinstance(value, list) or len(value) != 2
                or not all(isinstance(x, int) and 0 <= x <= 23 for x in value)):
            return "必须是 [起始小时, 结束小时]（0-23 的整数）"
    return None


def validate_all(values: dict) -> dict:
    """校验整份配置；返回 {ok, errors: [{key, message}]}。未知键忽略（由其它模块管理）。"""
    errors = []
    for key, item in _BY_KEY.items():
        if key not in values:
            continue
        err = validate_value(item, values[key])
        if err:
            errors.append({"key": key, "message": err})
    return {"ok": not errors, "errors": errors}
