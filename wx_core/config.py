"""配置读写（Lead 维护）。"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

DEFAULTS: dict = {
    "monitored": [],          # 监听的好友显示名列表
    "friends_cache": [],      # 上次同步到的好友名（启动时先显示，避免空白）
    "poll_interval": 1.2,     # 轮询间隔（秒），不建议低于 1 秒
    "ocr_on_change_only": True,
    "emit_history_on_start": False,   # 首次抓帧是否推送历史消息
    "auto_switch": True,      # 标题不匹配时自动切到被监听好友
    "auto_restore_window": True,      # 微信最小化时尝试自动恢复（尽量不抢焦点）
    "allow_foreground_switch": True,  # 允许"短暂前台+搜索框输入"的方式来切换会话
    "rotate_interval": 6.0,   # 监听多个好友时的轮换间隔（秒）
    "capture_gif": True,      # 动图表情包是否抓帧合成 GIF
    "export_original_media": False,  # 用微信自带「另存为」导出原图/原 GIF（用户不推荐，默认关闭）
    "image_xor_key": "",      # 图片缓存 .dat 的单字节异或密钥（十进制/0x 十六进制；配合 aes 密钥使用）
    "image_aes_key": "",      # 图片缓存 .dat 的 16 字节 AES 密钥（ASCII）
    "image_wxid": "",         # 密钥所属账号（自动提取时写入，仅作记录）
    "image_cfg_dword": "",    # 表情包密钥派生用的 seed（= cfgDword，自动提取时写入）
    "allow_screenshot_media": False,  # 用户要求：不要截图。仅调试时可临时打开
    "auto_extract_image_key": True,  # 首次运行自动从微信内存提取一次图片密钥并缓存（用户已授权）
    "decrypt_media_max_age": 86400.0,
    "decrypt_recent_within": 60.0,
    "db_direct": True,          # 数据库直读：文字精确、媒体与消息一一对应（推荐）
    "db_history_count": 20,     # 首次直读时补发的历史条数（emit_history_on_start 打开时生效）
    "db_poll_limit": 50,
    "friends_source": "recent",  # 好友列表: recent=有聊天记录的会话(按最近排序) / all=有记录的好友+群 / ui=界面滚动
    "db_keys": {},              # WCDB 数据库密钥缓存（当前账号；自动提取后写入；只在本机使用）
    "db_keys_by_account": {},   # 按账号目录缓存的数据库密钥（切换账号无需重扫）
    "image_keys_by_account": {},  # 按 wxid 缓存的图片/贴纸密钥（切换账号无需重扫）  # 新图优先按"最近 60 秒内写入的 .dat"匹配  # 解密时回看多少秒内的新 .dat
    "export_timeout": 12.0,   # 单条原图导出超时（秒）
    "copy_text_exact": True,  # 文本消息用微信「复制」拿精确原文（含表情中文名）
    "copy_text_timeout": 8.0,  # 单条精确复制超时（秒）
    "max_messages": 3000,     # 界面最多保留消息数
    "save_media": True,
    "theme": "dark",
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except FileNotFoundError:
        _write_plain(cfg)
    except Exception:
        pass
    return _apply_secrets(cfg)


def save(cfg: dict) -> None:
    payload = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    # 迁移之后（agi/data/secrets.json 存在）敏感字段不再写回明文 config.json，
    # 而是并入密钥库；写库失败就整体回退旧行为——宁可这一轮还是明文，
    # 也不能因为写库失败把用户刚提取的密钥丢掉。
    if _route_secrets(payload):
        for key in SECRET_KEYS:
            payload.pop(key, None)
    _write_plain(payload)


def _write_plain(payload: dict) -> None:
    """原子写 config.json（不含路由逻辑；load 建默认文件时也走这里）。"""
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass


# ---------------------------------------------------------------- 密钥库接入
#: 会被迁移到密钥库的敏感字段（与 tools/rotate_secrets.py 的清单保持一致）。
#: 默认行为不变：没有密钥文件时，这些字段照旧读写 config.json。
SECRET_KEYS = (
    "db_keys",
    "db_keys_by_account",
    "image_aes_key",
    "image_xor_key",
    "image_keys_by_account",
    "image_cfg_dword",
)


def _is_empty(value) -> bool:
    """空值判定：None / 空串 / 空 dict / 空 list；0 视为有值（可能是合法 xor）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (dict, list, tuple)):
        return not value
    return False


def _secrets_module():
    """尝试导入密钥库读取模块；不可用返回 None（保持旧行为，绝不抛）。

    why try/except：wx_core 可能被单独拷出去跑（仓库根不在 sys.path），
    这种情况下只应该退化成「没有密钥库」，而不是让整个采集链崩掉。
    """
    try:
        from agi import secrets_store
    except Exception:
        return None
    return secrets_store


def _apply_secrets(cfg: dict) -> dict:
    """用密钥库补齐**旧文件里为空**的敏感字段（文件优先，默认行为不变）。

    why 只在文件为空时补：老机器没有 secrets.json，行为必须逐字节一致；
    迁移过的机器 config.json 里这些字段已被清空，才会走到这里拿密钥。
    """
    module = _secrets_module()
    if module is None:
        return cfg
    try:
        overlay = module.config_section()
    except Exception:
        return cfg
    if not overlay:
        return cfg
    for key in SECRET_KEYS:
        if key in overlay and _is_empty(cfg.get(key)):
            cfg[key] = overlay[key]
    return cfg


def _route_secrets(payload: dict) -> bool:
    """把敏感字段写入密钥库；返回 True 表示「已接管，config.json 里应删掉它们」。

    只在本轮确实有非空敏感值时才路由：
    - 没有密钥文件（迁移前的机器）→ False，保持旧的明文写回；
    - 全为空（例如首次运行建默认文件）→ False，避免把空值覆盖掉密钥库里的真密钥。
    """
    values = {key: payload.get(key) for key in SECRET_KEYS if key in payload}
    if not any(not _is_empty(value) for value in values.values()):
        return False
    module = _secrets_module()
    if module is None:
        return False
    try:
        return bool(module.store_config_section(values))
    except Exception:
        return False
