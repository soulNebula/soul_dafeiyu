# -*- coding: utf-8 -*-
"""把当前 802（默认数据根）的配置/记忆导出成一个 Bot 包。

用途：已经用默认 `agi/data` 跑起来的人，不必重新建 Bot；直接：
    python tools/export_bot.py --path E:/agent/bots/802-export --password 你的密码

默认包含：配置/白名单、DeepSeek key（写 Bot secrets）、提示词、联系人库、人格、日记、
情绪状态；媒体（media_chat）默认不复制，加 `--with-media` 才复制。
不包含：admin_token（Bot 会生成新的）、微信数据库密钥（运行时会重新提取）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agi import paths as agi_paths  # noqa: E402
from agi import secrets_store  # noqa: E402
from tools import bot_profile  # noqa: E402


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _copy_tree(src: Path, dst: Path, *, skip_suffixes: tuple[str, ...] = (),
               skip_names: tuple[str, ...] = ()) -> int:
    copied = 0
    if not src.is_dir():
        return 0
    for item in src.rglob("*"):
        if not item.is_file():
            continue
        if item.name.endswith(skip_suffixes) or item.name in skip_names:
            continue
        target = dst / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied += 1
    return copied


def _map_settings(config: dict[str, Any]) -> dict[str, Any]:
    """网关 config → bot.json settings 里能直接复用的键。"""
    mapping = {
        "inner_mode": "inner_mode",
        "inner_shadow_only": "inner_shadow_only",
        "inner_schema": "inner_schema",
        "chat_vision_mode": "chat_vision_mode",
        "send_mode": "send_mode",
        "dry_run": "dry_run",
        "proactive_enabled": "proactive_enabled",
        "proactive_probability_min": "proactive_probability_min",
        "proactive_probability_max": "proactive_probability_max",
        "proactive_interval_min_s": "proactive_interval_min_s",
        "proactive_interval_max_s": "proactive_interval_max_s",
        "pacing_enabled": "pacing_enabled",
        "pacing_target_ratio": "proactive_ratio",
        "media_enabled": "media_enabled",
        "media_sticker_in_reply": "media_sticker_in_reply",
    }
    out: dict[str, Any] = {}
    for src, dst in mapping.items():
        if src in config:
            out[dst] = config[src]
    return out


def export(source_data: Path | str | None = None, *, path: Path | str,
           name: str = "802 当前配置", password: str = "",
           with_media: bool = False, include_data: bool = True) -> dict[str, Any]:
    source = Path(source_data) if source_data else agi_paths.data_dir()
    source = source.resolve()
    config = _read_json(source / "wechat" / "config.json", {})
    web = _read_json(agi_paths.web_config_path(), {})
    llm = web.get("llm") if isinstance(web, dict) else {}
    llm = llm if isinstance(llm, dict) else {}
    base_url = str(llm.get("base_url") or "https://api.deepseek.com").rstrip("/")
    model = str(llm.get("model") or config.get("llm_model") or "deepseek-flash")
    key = secrets_store.llm_api_key()
    if not password:
        import secrets as _secrets
        password = _secrets.token_urlsafe(9)
    settings = _map_settings(config)
    bot = bot_profile.create_bot(
        path, name, password=password,
        chat={"base_url": base_url, "model": model, "api_key": key},
        soul={"base_url": base_url, "model": model, "api_key": key},
        settings=settings,
    )
    root = Path(path).resolve()
    # 保留当前 802 的白名单/监听/发送限制等（写进 Bot 运行时配置；激活时不覆盖）
    runtime_cfg = dict(config)
    runtime_cfg["admin_token"] = str((bot.get("auth") or {}).get("admin_token") or "")
    runtime_cfg["enabled"] = False
    runtime_cfg["engine_enabled"] = False
    runtime_cfg.setdefault("auto_reply", False)
    (root / "data" / "wechat").mkdir(parents=True, exist_ok=True)
    (root / "data" / "wechat" / "config.json").write_text(
        json.dumps(runtime_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    copied = 0
    if include_data:
        copied += _copy_tree(source / "selflearn" / "wechat_contacts",
                             root / "data" / "selflearn" / "wechat_contacts",
                             skip_suffixes=("-shm", "-wal", ".quote_bak"))
        copied += _copy_tree(source / "diary", root / "data" / "diary")
        copied += _copy_tree(source / "persona", root / "data" / "persona")
        for filename in ("prompts.json", "llm_router.json", "local_models.json"):
            src = source / filename
            if src.is_file():
                (root / "data" / filename).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, root / "data" / filename)
                copied += 1
        mood = source / "selflearn" / "mood.json"
        if mood.is_file():
            target = root / "data" / "selflearn" / "mood.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(mood, target)
            copied += 1
        if with_media:
            copied += _copy_tree(source / "selflearn" / "media_chat",
                                 root / "data" / "selflearn" / "media_chat")
    validation = bot_profile.validate_bot(root)
    return {"ok": bool(validation.get("ok")), "root": str(root), "name": name,
            "password": password, "copied_files": copied,
            "validation": validation,
            "chat_base_url": base_url, "chat_model": model,
            "note": "Bot 已生成；打开 802 用该路径/密码加载"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出当前 802 配置为 Bot 包")
    parser.add_argument("--path", required=True, help="Bot 目录（用户自选）")
    parser.add_argument("--name", default="802 当前配置")
    parser.add_argument("--password", default="", help="802 登录密码（≥6 位；不填自动生成）")
    parser.add_argument("--with-media", action="store_true", help="复制 media_chat（可能很大）")
    parser.add_argument("--no-data", action="store_true", help="只导出配置，不复制联系人/记忆")
    args = parser.parse_args(argv)
    try:
        result = export(path=args.path, name=args.name, password=args.password,
                        with_media=args.with_media, include_data=not args.no_data)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
