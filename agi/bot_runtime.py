# -*- coding: utf-8 -*-
"""Bot 运行时：活跃 Bot 指针 + 每个 Bot 的运行时数据根/环境变量。

设计（见 docs/plans/63）：
- Bot 包在用户自选目录；运行时数据根是 ``<bot>/data``；
- 主仓 ``agi/data/active_bot.json`` 只保存“当前激活哪个 Bot”（控制面，不属于任何 Bot）；
- 切换 Bot 会让 801/802 带以下环境变量重启：``SOULAGENT_DATA_DIR`` /
  ``SOULAGENT_SECRETS`` / ``SOULAGENT_WEB_CONFIG`` / ``SOULAGENT_LOG_DIR``。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
GLOBAL_DIR = REPO_ROOT / "agi" / "data"
ACTIVE_PATH = GLOBAL_DIR / "active_bot.json"
SWITCH_STATUS_PATH = GLOBAL_DIR / "bot_switch_status.json"


def read_active() -> dict[str, Any] | None:
    try:
        data = json.loads(ACTIVE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or not data.get("path"):
        return None
    return data


def write_active(bot_path: str | Path, *, bot: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(bot_path).expanduser().resolve()
    payload = {"path": str(root), "activated_at": int(time.time())}
    if isinstance(bot, dict):
        payload.update({"bot_id": bot.get("bot_id"), "name": bot.get("name"),
                        "mode": bot.get("mode")})
    GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    return payload


def clear_active() -> None:
    ACTIVE_PATH.unlink(missing_ok=True)


def write_switch_status(payload: dict[str, Any]) -> None:
    GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    data = {"at": int(time.time()), **payload}
    SWITCH_STATUS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")


def read_switch_status() -> dict[str, Any] | None:
    try:
        data = json.loads(SWITCH_STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_runtime(bot_path: str | Path, *, bot: dict[str, Any] | None = None) -> dict[str, Any]:
    """创建/补齐 Bot 的运行时数据根，返回环境变量与路径。

    不覆盖已有配置（用户可能有白名单/发送模式）；只创建缺失文件。
    """
    root = Path(bot_path).expanduser().resolve()
    data = root / "data"
    logs = root / "logs"
    for sub in ("wechat", "selflearn", "diary", "logs"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    auth = (bot or {}).get("auth") or {}
    providers = (bot or {}).get("providers") or {}
    chat = providers.get("chat") if isinstance(providers, dict) else None
    chat = chat if isinstance(chat, dict) else {}
    soul = providers.get("soul") if isinstance(providers, dict) else None
    soul = soul if isinstance(soul, dict) else {}
    vision = providers.get("vision") if isinstance(providers, dict) else None
    vision = vision if isinstance(vision, dict) else {}

    config_path = data / "wechat" / "config.json"
    settings = (bot or {}).get("settings") or {}
    # Bot 创建时选的内状态/识图/主动消息等，第一次激活写进运行时配置；
    # 已有配置不覆盖（用户后续在 802 设置的优先）。
    initial = {
        "enabled": False,
        "engine_enabled": False,
        "auto_reply": False,
        "dry_run": bool(settings.get("dry_run", False)),
        "send_mode": str(settings.get("send_mode") or "confirm"),
        "admin_token": str(auth.get("admin_token") or ""),
        "allowed_contacts": [],
        "monitored_contacts": [],
        "inner_mode": str(settings.get("inner_mode") or "off"),
        "inner_shadow_only": bool(settings.get("inner_shadow_only", True)),
        "inner_schema": str(settings.get("inner_schema") or "slim"),
        "chat_vision_mode": str(settings.get("chat_vision_mode") or "local_caption"),
        "proactive_enabled": bool(settings.get("proactive_enabled", False)),
        "proactive_probability_min": float(settings.get("proactive_probability_min", 0.3)),
        "proactive_probability_max": float(settings.get("proactive_probability_max", 0.9)),
        "proactive_interval_min_s": int(settings.get("proactive_interval_min_s", 60)),
        "proactive_interval_max_s": int(settings.get("proactive_interval_max_s", 7200)),
        "pacing_enabled": bool(settings.get("pacing_enabled", True)),
        "pacing_target_ratio": float(settings.get("proactive_ratio", 1.0) if isinstance(settings.get("proactive_ratio"), (int, float)) else 1.0),
        "media_enabled": bool(settings.get("media_enabled", False)),
        # 云端灵魂：内状态用独立 OpenAI 兼容 provider（key 在 Bot secrets.providers.soul）
        "soul_provider_mode": "cloud" if (soul or {}).get("base_url") else "local",
        "soul_provider_base_url": str((soul or {}).get("base_url") or ""),
        "soul_provider_model": str((soul or {}).get("model") or ""),
        "vision_provider_mode": "cloud" if (vision or {}).get("base_url") else "local",
        "vision_provider_base_url": str((vision or {}).get("base_url") or ""),
        "vision_provider_model": str((vision or {}).get("model") or ""),
    }
    _write_json(config_path, initial)
    # 老 Bot（导出时还没有 provider 键）补齐；不覆盖用户已设置的值
    try:
        if config_path.is_file():
            current = json.loads(config_path.read_text(encoding="utf-8"))
            changed = False
            for key, value in (("soul_provider_mode", initial["soul_provider_mode"]),
                               ("soul_provider_base_url", initial["soul_provider_base_url"]),
                               ("soul_provider_model", initial["soul_provider_model"]),
                               ("vision_provider_mode", initial["vision_provider_mode"]),
                               ("vision_provider_base_url", initial["vision_provider_base_url"]),
                               ("vision_provider_model", initial["vision_provider_model"])):
                if key not in current:
                    current[key] = value
                    changed = True
            if changed:
                config_path.write_text(
                    json.dumps(current, ensure_ascii=False, indent=2) + chr(10),
                    encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    _write_json(data / "web_config.json", {
        "llm": {
            "api_key": "",
            "model": str(chat.get("model") or "deepseek-flash"),
            "base_url": str(chat.get("base_url") or "https://api.deepseek.com"),
            "thinking": True,
            "attach_images": True,
        }
    })
    env = {
        "SOULAGENT_DATA_DIR": str(data),
        "SOULAGENT_LOG_DIR": str(logs),
        "SOULAGENT_WEB_CONFIG": str(data / "web_config.json"),
        "SOULAGENT_CONFIG_PATH": str(data / "config.json"),
        "SOULAGENT_SECRETS": str(root / "secrets.json"),
    }
    return {"root": str(root), "data": str(data), "logs": str(logs), "env": env,
            "config": str(config_path)}


def apply_env(env: dict[str, str], target: dict[str, str] | None = None) -> dict[str, str]:
    merged = dict(os.environ if target is None else target)
    merged.update({str(k): str(v) for k, v in env.items()})
    return merged
