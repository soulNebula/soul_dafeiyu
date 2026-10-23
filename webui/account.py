"""当前登录微信账号检测（读取本地登录元数据，只读、不碰微信进程内存）。

为什么不用 ``media_cache.active_account_dir()``：它按“媒体缓存最新改动”判断，
切换账号后旧账号的缓存时间可能更新，会误判成旧账号（实测：切到“瀑布”后仍返回
“白云”，导致网页一直显示上一个账号的好友/群聊）。

更可靠的信号是微信自身的登录目录：
``<xwechat_files>/all_users/login/<wxid>/key_info.db(-wal)`` 的最近写入时间，
只有当前登录账号会持续更新它。找不到时才回退 ``active_account_dir``。
"""
from __future__ import annotations

import os
from pathlib import Path

from wx_core import media_cache


def _account_dir_for(wxid: str) -> Path | None:
    for d in media_cache.account_dirs():
        name = d.name
        if name == wxid or name.startswith(wxid + "_"):
            return d
    return None


def _login_scores() -> list[tuple[float, str]]:
    """返回 [(最新写入时间, wxid)]（来自 all_users/login/<wxid>/key_info.db*）。"""
    scores: list[tuple[float, str]] = []
    for root in media_cache.discover_roots():
        login_root = root / "all_users" / "login"
        try:
            entries = list(login_root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            wxid = entry.name
            latest = 0.0
            for name in ("key_info.db", "key_info.db-wal", "key_info.db-shm"):
                try:
                    latest = max(latest, os.path.getmtime(str(entry / name)))
                except OSError:
                    continue
            if latest > 0:
                scores.append((latest, wxid))
    scores.sort(reverse=True)
    return scores


def detect_active_account(config: dict | None = None) -> tuple[Path | None, str]:
    """返回 ``(账号目录, wxid)``；识别失败时回退旧逻辑（可能为 None）。"""
    for _mtime, wxid in _login_scores():
        acc = _account_dir_for(wxid)
        if acc is not None:
            return acc, wxid
    acc = media_cache.active_account_dir()
    if acc is None:
        return None, ""
    name = acc.name
    wxid = name.rsplit("_", 1)[0] if name.startswith("wxid_") and name.count("_") >= 2 else name
    return acc, wxid


def account_wxid(config: dict | None = None) -> str:
    _acc, wxid = detect_active_account(config)
    return wxid
