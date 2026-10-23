# -*- coding: utf-8 -*-
"""微信 4.x 数据库密钥缓存（只写项目内 ``.wx4_cache/``，绝不写微信目录）。

缓存内容：``{account: {库相对路径: 32/48 字节密钥的 hex}}``。

设计要点：
- **项目内落盘**：``WechatMoments/.wx4_cache/keys/<account>.json``（可用环境变量
  ``WXMOMENTS_CACHE_DIR`` 覆盖根目录），跨进程复用，TEMP 被清理也不丢；
- **导入历史缓存**：早期 ``recon/ref_db.py`` 把密钥写到了
  ``%LOCALAPPDATA%/wechatauto_keys/`` 和 ``%TEMP%/wechatauto_db/``，这里只读导入，
  避免重复内存扫描（导入不改变那些文件）；
- **绝不把非空缓存覆盖成空缓存**：提取偶发失败不销毁已有好缓存；
- 落盘用临时文件 + ``os.replace`` 原子替换，并保留 ``.bak``。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

log = logging.getLogger(__name__)

CACHE_DIR_ENV = "WXMOMENTS_CACHE_DIR"
LEGACY_ENV = "WXMOMENTS_LEGACY_KEYS"
_SAVE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------
def project_root() -> Path:
    """项目根目录（``wx4`` 的上一级 = WechatMoments）。"""
    return Path(__file__).resolve().parent.parent


def cache_dir() -> Path:
    """缓存根目录；默认项目内 ``.wx4_cache``。"""
    env = os.environ.get(CACHE_DIR_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return project_root() / ".wx4_cache"


def keys_path(account: str) -> Path:
    return cache_dir() / "keys" / (account + ".json")


def _keys_bak_path(account: str) -> Path:
    """``<account>.json.bak``（with_suffix 对多段后缀行为不稳定，直接拼名字）。"""
    return cache_dir() / "keys" / (account + ".json.bak")


def db_cache_dir(account: str) -> Path:
    """解密后数据库文件的缓存目录（每个账号一个子目录）。"""
    return cache_dir() / "db" / account


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _normalize_rel(rel: object) -> str:
    """库相对路径统一成正斜杠、去首尾斜杠（缓存里的键口径）。"""
    s = str(rel or "").replace("\\", "/").strip().strip("/")
    while s.startswith("./"):
        s = s[2:]
    return s


def _decode_entry(rel: object, hexkey: object) -> Optional[Tuple[str, bytes]]:
    """校验并解码一条缓存：rel 合法、hex 长度为 64 或 96 位。"""
    rel_s = _normalize_rel(rel)
    if not rel_s or not isinstance(hexkey, str):
        return None
    hex_s = hexkey.strip().lower()
    if len(hex_s) not in (64, 96):
        return None
    try:
        key = bytes.fromhex(hex_s)
    except ValueError:
        return None
    if len(key) not in (32, 48):
        return None
    return rel_s, key


def _read_json_keys(path: Path) -> Dict[str, bytes]:
    """读一个 keys.json；格式不合法 / 不存在 / 损坏时返回空 dict。"""
    out: Dict[str, bytes] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    for rel, hexkey in data.items():
        got = _decode_entry(rel, hexkey)
        if got:
            out[got[0]] = got[1]
    return out


def _legacy_key_files(account: str) -> Iterator[Path]:
    """早期 ref_db 写过的密钥缓存位置（只读导入；可用环境变量关闭）。"""
    flag = os.environ.get(LEGACY_ENV, "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return
    # %LOCALAPPDATA%/wechatauto_keys/<account>.json（ref_db 的稳定副本）
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE") or ""
    if local:
        yield Path(local) / "wechatauto_keys" / (account + ".json")
    # %TEMP%/wechatauto_db/<account>/keys.json(.bak)（ref_db 的工作缓存）
    tmp = tempfile.gettempdir()
    yield Path(tmp) / "wechatauto_db" / account / "keys.json"
    yield Path(tmp) / "wechatauto_db" / account / "keys.json.bak"


def project_keys(account: str) -> Dict[str, bytes]:
    """只读项目内缓存（不含历史缓存导入），用于判断是否需要回写。"""
    return _read_json_keys(keys_path(account))


def load_keys(account: str) -> Dict[str, bytes]:
    """加载某个账号的密钥缓存：项目缓存优先，再合并历史缓存（只读）。"""
    out: Dict[str, bytes] = {}
    for path in (keys_path(account), _keys_bak_path(account)):
        for rel, key in _read_json_keys(path).items():
            out.setdefault(rel, key)
    for path in _legacy_key_files(account):
        try:
            if not path.is_file():
                continue
            for rel, key in _read_json_keys(path).items():
                out.setdefault(rel, key)
        except OSError:
            continue
    return out


def save_keys(account: str, keys: Dict[str, bytes]) -> None:
    """把密钥写入项目缓存（原子写 + .bak；空 dict 不覆盖已有缓存）。"""
    if not keys:
        return
    path = keys_path(account)
    data = {rel: key.hex() for rel, key in keys.items() if len(key) in (32, 48)}
    if not data:
        return
    with _SAVE_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            if path.is_file():
                try:
                    bak = _keys_bak_path(account)
                    bak.write_bytes(path.read_bytes())
                except OSError:
                    pass
            os.replace(tmp, path)
        except OSError as exc:  # 缓存写失败不应影响主流程，只记日志
            log.warning("密钥缓存写入失败（%s）：%s", path, exc)


def clear_keys(account: str) -> None:
    """删除某账号的项目内密钥缓存（测试/排障用）。"""
    for path in (keys_path(account), _keys_bak_path(account)):
        try:
            path.unlink()
        except OSError:
            pass


__all__ = [
    "CACHE_DIR_ENV", "LEGACY_ENV", "project_root", "cache_dir", "keys_path",
    "db_cache_dir", "project_keys", "load_keys", "save_keys", "clear_keys",
]
