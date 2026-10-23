"""微信视频消息解析：从数据库行找到本机缓存的原视频文件（只读）。

微信 4.x 视频消息（``local_type=43``）在数据库里是 ``<videomsg md5="..." newmd5="..."
playlength="..."/>``；本机实际视频缓存在
``<账号目录>/msg/video/<YYYY-MM>/<md5>.mp4``（同名 ``.jpg`` 为封面）。
只有**在电脑微信里播放/接收过**的视频才会留下缓存；自己发送的视频可能不缓存。

本模块只负责根据 (好友显示名, db_local_id) 定位文件路径，不复制、不转码、不落盘；
HTTP 层直接按 Range 读取原文件播放。
"""
from __future__ import annotations

import glob
import os
import re
from pathlib import Path
from typing import Callable

from wx_core import config as cfg_mod
from wx_core import wcdb

from webui.history import HistoryService, _BUILTIN_USERS

_MD5_RE = re.compile(r'(?:md5|newmd5|rawmd5|originsourcemd5)="([0-9a-fA-F]{16,64})"')
_PLAY_RE = re.compile(r'playlength="(\d+)"')
_LENGTH_RE = re.compile(r'\blength="(\d+)"')
_VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")


class VideoService:
    """按消息定位本机视频缓存文件。"""

    def __init__(self, config: dict | None = None, *, log: Callable[[str], None] | None = None):
        self._base_config = dict(config or {})
        self._history = HistoryService(config, log=log)
        self._log = log or (lambda *_a, **_k: None)

    def resolve(self, friend: str, local_id) -> dict | None:
        """返回 ``{"path": Path, "mime": str, "size": int, "cover": Path|None}``；找不到返回 None。"""
        friend = str(friend or "").strip()
        try:
            lid = int(local_id)
        except (TypeError, ValueError):
            return None
        if not friend or lid <= 0:
            return None
        reader = None
        try:
            config = dict(self._base_config)
            try:
                config.update(cfg_mod.load())
            except Exception:  # noqa: BLE001
                pass
            acc, keys, wxid = self._history._account()
            reader = wcdb.SessionReader(acc, keys, wxid)
            username = _BUILTIN_USERS.get(friend) or reader.resolve_username(friend) or ""
            if not username:
                return None
            row = _read_row(reader, username, lid)
            if row is None:
                return None
            return self._video_from_row(acc, row)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[video] 解析视频失败：{exc}")
            return None
        finally:
            if reader is not None:
                try:
                    reader.close()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------
    @staticmethod
    def _video_from_row(acc: Path, row: dict) -> dict | None:
        content = row.get("content")
        text = content if isinstance(content, str) else (
            content.decode("utf-8", "replace") if content else ""
        )
        if "<videomsg" not in text:
            return None
        md5s = []
        for value in _MD5_RE.findall(text):
            value = value.lower()
            if value not in md5s and value != "0" * len(value):
                md5s.append(value)
        if not md5s:
            return None
        root = Path(acc) / "msg" / "video"
        for md5 in md5s:
            for suffix in _VIDEO_SUFFIXES:
                hits = sorted(glob.glob(str(root / "*" / f"{md5}{suffix}")))
                if hits:
                    return _make_result(Path(hits[-1]))
        # 微信 4.x 有时用内部 hash 命名本地缓存（不等于 XML 里的 md5/newmd5），
        # 但本地文件大小与 <videomsg length="..."> 完全一致；用大小兜底。
        for size in _sizes(text):
            candidates = []
            for suffix in _VIDEO_SUFFIXES:
                for name in glob.glob(str(root / "*" / f"*{suffix}")):
                    path = Path(name)
                    try:
                        if path.is_file() and path.stat().st_size == size:
                            candidates.append(path)
                    except OSError:
                        continue
            if candidates:
                candidates.sort(key=lambda p: p.stat().st_mtime)
                return _make_result(candidates[-1])
        return None


def _sizes(text: str) -> list[int]:
    out: list[int] = []
    for raw in _LENGTH_RE.findall(text or ""):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in out:
            out.append(value)
    return out


def _make_result(path: Path) -> dict:
    cover = None
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        cand = path.with_suffix(ext)
        if cand.is_file():
            cover = cand
            break
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return {"path": path, "mime": _mime_for(path), "size": size, "cover": cover}


def _read_row(reader, username: str, local_id: int) -> dict | None:
    """读取指定 local_id 的数据库行（优先单行，失败再全量查找）。"""
    try:
        rows = reader.read_messages(username, max(0, local_id - 1), limit=1)
        if rows and int(rows[0].get("local_id") or 0) == local_id:
            return rows[0]
    except Exception:  # noqa: BLE001
        pass
    try:
        for row in reader.read_messages(username, 0, limit=100000):
            if int(row.get("local_id") or 0) == local_id:
                return row
    except Exception:  # noqa: BLE001
        return None
    return None


def _mime_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska",
    }.get(suffix, "application/octet-stream")
