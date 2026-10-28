# -*- coding: utf-8 -*-
"""配置 schema（精简版）：只保留 IDEA 保留功能的必要配置。

分组：基础 / 模型 / 训练。聊天、草稿、发送模式、Git、编辑器等配置已随功能删除。
"""
from __future__ import annotations

from typing import Any

SCHEMA: list[dict[str, Any]] = [
    # ---------------- 基础 ----------------
    {"key": "enabled", "group": "基础", "type": "bool", "default": True,
     "label": "网关总开关", "desc": "关闭后网关不工作", "restart": False},
    {"key": "engine_enabled", "group": "基础", "type": "bool", "default": True,
     "label": "引擎（监听+回复）", "desc": "软停引擎；控制台仍可用", "restart": False},
    {"key": "admin_token", "group": "基础", "type": "str", "default": "8888",
     "label": "管理令牌", "desc": "控制台/网关 API 的令牌", "secret": True, "restart": True},
    {"key": "port", "group": "基础", "type": "int", "default": 801, "min": 1, "max": 65535,
     "label": "网关端口", "desc": "修改后需要重启网关", "restart": True},
    {"key": "note", "group": "基础", "type": "str", "default": "",
     "label": "备注", "desc": "给自己看的说明", "restart": False},

    # ---------------- 模型 ----------------
    {"key": "inner_mode", "group": "模型", "type": "str", "default": "off",
     "enum": ["off", "model", "rule"],
     "label": "内状态模式", "desc": "off=不生成；rule=规则；model=本地 0.5B", "restart": False},
    {"key": "inner_shadow_only", "group": "模型", "type": "bool", "default": True,
     "label": "内状态仅影子", "desc": "只记录不注入提示词", "restart": False},
    {"key": "inner_schema", "group": "模型", "type": "str", "default": "slim",
     "enum": ["slim", "full"], "label": "内状态字段",
     "desc": "slim=行为字段；full=全部字段", "restart": False},
    {"key": "soul_server_url", "group": "模型", "type": "str",
     "default": "http://127.0.0.1:8123",
     "label": "灵魂模型地址", "desc": "本地内状态服务（llama-server）", "restart": True},
    {"key": "soul_timeout_s", "group": "模型", "type": "float", "default": 2.0,
     "min": 0.2, "max": 30, "label": "灵魂模型超时（秒）",
     "desc": "超时回退规则", "restart": False},
    {"key": "llm_model", "group": "模型", "type": "str", "default": "deepseek-flash",
     "label": "对话模型", "desc": "主对话使用的模型", "restart": False},
    {"key": "llm_temperature", "group": "模型", "type": "float", "default": 0.7,
     "min": 0, "max": 2, "label": "温度", "desc": "越高越发散", "restart": False},
    {"key": "llm_max_tokens", "group": "模型", "type": "int", "default": 2048,
     "min": 128, "max": 32768, "label": "最大输出 token",
     "desc": "单次生成上限", "restart": False},

    # ---------------- 训练 ----------------
    {"key": "train_enabled", "group": "训练", "type": "bool", "default": True,
     "label": "训练工厂", "desc": "允许工作台触发收集/训练", "restart": False},
    {"key": "train_daily_hour", "group": "训练", "type": "int", "default": 3,
     "min": 0, "max": 23, "label": "语料收集时间（点）",
     "desc": "每天几点收集语料并出日报", "restart": True},
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
    """校验整份配置；返回 {ok, errors: [{key, message}]}。未知键忽略。"""
    errors = []
    for key, item in _BY_KEY.items():
        if key not in values:
            continue
        err = validate_value(item, values[key])
        if err:
            errors.append({"key": key, "message": err})
    return {"ok": not errors, "errors": errors}
