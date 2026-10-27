# -*- coding: utf-8 -*-
"""801 微信网关配置。

P0-5：运行时 ``agi/data/wechat/config.json`` 会覆盖 DEFAULTS，历史事故值
（间隔 30/300 秒、``quiet_hours: []``）必须自动迁移，否则新默认值在本机不生效。
迁移规则见 :func:`migrate_config`：幂等、原子写回、只升旧事故值、用户显式新值不动。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "agi" / "data" / "wechat"
CONFIG_PATH = DATA_DIR / "config.json"
CONTACT_DB_DIR = ROOT / "agi" / "data" / "selflearn" / "wechat_contacts"

DEFAULTS = {
    "enabled": False,
    # P18-A：引擎总开关（监听+自动回复+主动消息）；默认 true，与 enabled 不同：
    # enabled 是网关进程是否启动，engine_enabled 是运行中的软开关（控制台一键运行/停止）。
    "engine_enabled": True,
    "port": 801,
    "auto_reply": False,
    "dry_run": True,
    "monitored_contacts": [],
    "allowed_contacts": [],
    # P22：机器人微信昵称；私聊文本含「@昵称」时识别为 @提及，server 加 [对方@了你] 前缀。
    # 留空 = 不识别（群聊 @ 需要真实 group 支持，暂不处理）。
    "mention_name": "",
    "max_chars": 500,
    "reply_delay": 0.3,
    "stop": False,
    # 23:00–08:00 夜间静音；int 小时语义不变，start > end 即跨零点
    "quiet_hours": [23, 8],
    "rate_per_minute": 3,
    "reactive_rate_per_minute": 8,
    "daily_limit": 200,
    "send_mode": "auto",
    "admin_token": "",
    "complaint_stop": True,
    "min_interval_s": 1.5,
    "hourly_limit": 40,
    "fallback_enabled": False,
    "fallback_mode": "manual",
    # P0-4 发送容错：发送前句柄双验（send_probe 审计）开关、非确定性失败的额外重试次数
    "send_probe_enabled": True,
    "send_retry_max": 1,
    # P5-a 发送层多媒体（默认关闭：用户/决策层显式开启前，send_bubbles 一律 blocked）
    "media_enabled": False,
    "media_dirs": [],              # 追加白名单目录（默认三个目录见 agi/wechat_gateway/media.py）
    "media_max_mb": 10,            # 单文件大小上限（MB）
    "media_send_interval_ms": 120,  # mixed 多气泡间隔；实现钳制在 80–150ms
    "sticker_fallback_emoji": "😄",  # 贴纸当图片失败后的 emoji 降级
    "proactive_enabled": False,
    "proactive_contacts": [],
    # 主动检查间隔：60–7200 秒（1–120 分钟）随机（2026-10-25 用户拍板）
    "proactive_interval_min_s": 60,
    "proactive_interval_max_s": 7200,
    # 每轮检查的发送概率：在 [min, max] 随机采（旧键 proactive_probability 作为固定值兼容）
    "proactive_probability_min": 0.3,
    "proactive_probability_max": 0.9,
    # P16-B：主动节奏对等（按近 7 天用户主动量自适应；只影响本轮调度，不改写配置文件）
    #   pacing_enabled：总开关；pacing_target_ratio：目标 agent/user；容差 ±tolerance
    #   pacing_daily_cap：旧固定上限（兼容）；P33 起日上限按当天聊天量动态算：
    #   pacing_daily_cap_min/max = 动态 cap 的下/上界（默认 12/100）
    "pacing_enabled": True,
    "pacing_target_ratio": 1.0,
    "pacing_tolerance": 0.25,
    "pacing_daily_cap": 12,
    "pacing_daily_cap_min": 12,
    "pacing_daily_cap_max": 100,
    "pacing_min_interval_s": 1200,
    # P33：主动度（关系熟悉度×心情×信任 → 概率↑/间隔↓/分享偏好）；默认开
    "initiative_enabled": True,
    "proactive_until_ts": 0,
    "reply_debounce_ms": 2000,
    # 对方多久之前还在说话就不主动（秒）：15 分钟，对齐 qq-bridge 的静默要求
    "proactive_idle_s": 900,
    # P0-2：同内容去重窗口（秒）——窗口内发过相同/高相似文本就跳过
    "proactive_dedupe_window_s": 1800,
    # P0-2：同类错误连续 N 次熔断，冷却 cooldown 秒
    "proactive_breaker_threshold": 3,
    "proactive_breaker_cooldown_s": 1800,
    # P0-2：死区哨兵——超过该秒数没有任何事件就写 gateway_stale
    "gateway_stale_alert_s": 1800,
    # P3-a：内状态（小模型灵魂）接入。默认 off；env INNER_MODE / SOUL_* 优先。
    #   inner_mode: off | static | rule | model | shadow
    #   soul_server_url: 本地 llama-server 地址（model 模式）
    #   soul_timeout_s: 内状态调用超时（性能护栏，超时回退 rule）
    #   inner_shadow_only: 灰度——只记录不注入（先跑 shadow 24h）
    "inner_mode": "off",
    "soul_server_url": "http://127.0.0.1:8123",
    "soul_timeout_s": 2.0,
    "inner_shadow_only": False,
    # P15-A：推理过程采集（turn_traces）。默认开；只读采集，失败不影响主链。
    #   trace_enabled: 是否记录每条回复的上下文/生成/润色/守卫
    #   trace_max_rows: 表内最多保留多少条（超出删最旧，防库膨胀）
    "trace_enabled": True,
    "trace_max_rows": 5000,
}

#: P0-5 迁移阈值：低于这些值视为历史事故配置
MIGRATION_INTERVAL_MIN_S = 60
MIGRATION_INTERVAL_MAX_S = 7200
MIGRATION_LEGACY_INTERVAL_MIN = 120      # 旧默认 30/1800 都 < 120
MIGRATION_LEGACY_INTERVAL_MAX = 600      # 旧默认 300/5400 都 < 600
MIGRATION_QUIET_HOURS = [23, 8]
#: 旧默认/事故静音值（[] 表示未配置）
MIGRATION_LEGACY_QUIET = ([], [0, 7])
#: 旧默认 proactive_idle_s=600（10 分钟）→ 900（15 分钟，对齐 qq-bridge）
MIGRATION_IDLE_S = 900
MIGRATION_LEGACY_IDLE = 600
MIGRATION_LOG_NAME = "gateway.log"


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def migrate_config(raw: dict) -> tuple[dict, dict]:
    """把旧事故配置迁移成新默认；返回 ``(迁移后的 dict, changes)``。

    - ``proactive_interval_min_s`` < 120 → 60
    - ``proactive_interval_max_s`` < 600 → 7200
    - ``quiet_hours`` 为 ``[]`` 或 ``[0, 7]`` → ``[23, 8]``
    - 缺 ``proactive_probability_min/max`` **不写文件**，由 DEFAULTS 提供 0.3/0.9
    - 用户显式新值（min ≥ 120 / max ≥ 600 / 其他 quiet_hours）一律不动

    ``changes`` 为空表示无需迁移（幂等）。
    """
    cfg = dict(raw)
    changes: dict[str, dict] = {}

    def _upgrade(key: str, new: Any, too_old) -> None:
        old = cfg.get(key)
        if too_old(old):
            changes[key] = {"old": old, "new": new}
            cfg[key] = new

    _upgrade("proactive_interval_min_s", MIGRATION_INTERVAL_MIN_S,
             lambda v: (f := _as_float(v)) is not None
             and f < MIGRATION_LEGACY_INTERVAL_MIN
             and f != MIGRATION_INTERVAL_MIN_S)  # 新默认 60 本身 <120，不能二次迁移
    _upgrade("proactive_interval_max_s", MIGRATION_INTERVAL_MAX_S,
             lambda v: (f := _as_float(v)) is not None
             and f < MIGRATION_LEGACY_INTERVAL_MAX
             and f != MIGRATION_INTERVAL_MAX_S)
    quiet = cfg.get("quiet_hours")
    for legacy in MIGRATION_LEGACY_QUIET:
        if isinstance(quiet, list) and quiet == legacy:
            changes["quiet_hours"] = {"old": quiet, "new": list(MIGRATION_QUIET_HOURS)}
            cfg["quiet_hours"] = list(MIGRATION_QUIET_HOURS)
            break
    _upgrade("proactive_idle_s", MIGRATION_IDLE_S,
             lambda v: _as_float(v) == MIGRATION_LEGACY_IDLE
             and _as_float(v) != MIGRATION_IDLE_S)
    return cfg, changes


def _write_atomic(cfg: dict) -> None:
    """原子写回：同目录临时文件 + os.replace（保留其他键与缩进）。"""
    target = Path(CONFIG_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".migrating.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def _log_migration(changes: dict) -> None:
    entry = {
        "ts": time.time(),
        "kind": "config_migrated",
        "path": str(CONFIG_PATH),
        "changes": changes,
    }
    try:
        target = Path(CONFIG_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        with (target.parent / MIGRATION_LOG_NAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 - 日志失败不影响加载
        pass


def load() -> dict:
    """读配置；发现历史事故值时做一次幂等迁移（写回文件 + config_migrated 日志）。"""
    cfg = dict(DEFAULTS)
    raw: Any = {}
    try:
        raw = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 文件不存在/损坏：沿用 DEFAULTS（保持原行为）
        raw = {}
    if not isinstance(raw, dict) or not raw:
        return cfg
    migrated, changes = migrate_config(raw)
    if changes:
        try:
            _write_atomic(migrated)
        except Exception:  # noqa: BLE001 - 写盘失败时内存里仍是迁移后的值
            pass
        _log_migration(changes)
    cfg.update(migrated)
    return cfg


def save(cfg: dict) -> None:
    target = Path(CONFIG_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def save_atomic(cfg: dict, path: Path | str | None = None) -> None:
    """P18-A：原子写回配置（同目录临时文件 + os.replace），保留其它键。"""
    target = Path(path) if path else Path(CONFIG_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".engine.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def contact_db(contact: str) -> Path:
    safe = hashlib.sha1(str(contact).encode("utf-8")).hexdigest()[:14]
    CONTACT_DB_DIR.mkdir(parents=True, exist_ok=True)
    return CONTACT_DB_DIR / f"{safe}.sqlite3"


# --------------------------------------------------------------------------- #
# P0-22：配置热加载 + 紧急停止开关（事故防线）
# --------------------------------------------------------------------------- #
#: 每个 CONFIG_PATH 最近一次成功加载的 mtime_ns（热加载用）
_LAST_MTIME: dict[str, int] = {}

#: P0-22b：STOP 路径覆盖（测试/多实例隔离注入点）。
#: - ``None``（默认）= 与 ``CONFIG_PATH`` 同目录的 ``STOP``；
#: - ``Path``/``str`` = 直接用该路径；
#: - callable = 每次调用时求值（conftest 用它做"生产目录→tmp"的动态重定向）。
#: 用 :func:`set_stop_path` 修改；单独读写这个模块属性也生效。
STOP_PATH: Any = None


def default_stop_path() -> Path:
    """默认 STOP 路径：与当前 ``CONFIG_PATH`` 同目录（忽略 ``STOP_PATH`` 覆盖）。"""
    return Path(CONFIG_PATH).parent / "STOP"


def stop_path() -> Path:
    """紧急停止开关文件路径（已应用 ``STOP_PATH`` 注入）。

    默认与 ``config.json`` 同目录的 ``STOP``；``STOP_PATH`` 非 None 时覆盖，
    供测试/多实例隔离，避免读到生产 ``agi/data/wechat/STOP``。
    """
    override = STOP_PATH
    if override is not None:
        if callable(override):
            return Path(override())
        return Path(override)
    return default_stop_path()


def set_stop_path(path: Path | str | None) -> None:
    """设置/清除 STOP 路径覆盖（``None`` 恢复默认）。"""
    global STOP_PATH
    STOP_PATH = Path(path) if path is not None else None


def stopped() -> bool:
    """STOP 文件存在 = 立即停止一切外发（删除即恢复）。"""
    try:
        return stop_path().exists()
    except OSError:
        return False


def hot_reload(current: dict | None = None) -> tuple[dict, dict, str]:
    """每轮调度前重读配置（mtime 变了才真正重载）。

    返回 ``(新配置, changes, warning)``：
    - 文件没变 → 原样返回 current（changes 空、warning 空）
    - 文件变了且读取成功 → 返回生效配置 + 逐键 changes（old/new）
    - 读取/解析失败 → 返回 current 并给 warning（沿用旧配置，不崩）
    """
    cur = dict(current or {})
    target = Path(CONFIG_PATH)
    key = str(target)
    try:
        stat = target.stat()
    except OSError as exc:
        return cur, {}, f"{type(exc).__name__}: {exc}"
    mtime = int(stat.st_mtime_ns)
    if _LAST_MTIME.get(key) == mtime:
        return cur, {}, ""
    try:  # 先校验 JSON 可读，损坏时沿用旧配置并给 warning（load 内部会吞异常）
        json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return cur, {}, f"{type(exc).__name__}: {exc}"
    try:
        fresh = load()
    except Exception as exc:  # noqa: BLE001 - 读失败沿用旧配置
        return cur, {}, f"{type(exc).__name__}: {exc}"
    try:  # load() 里可能触发迁移写盘，记录写盘后的 mtime，避免下一轮重复读
        _LAST_MTIME[key] = int(target.stat().st_mtime_ns)
    except OSError:
        _LAST_MTIME[key] = mtime
    changes: dict[str, dict] = {}
    for name in sorted(set(cur) | set(fresh)):
        if cur.get(name) != fresh.get(name):
            changes[name] = {"old": cur.get(name), "new": fresh.get(name)}
    return fresh, changes, ""

