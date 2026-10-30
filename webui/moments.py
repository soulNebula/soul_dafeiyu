#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""朋友圈内存数据服务（V2：只依赖标准库，可在网页主进程 Python 3.14 里 import）。

V2 变更（见 ``webui/CONTRACT.md`` 末尾「v2 需求变更」V2-1/V2-2，优先级高于 V1）
--------------------------------------------------------------------------
* **数据只放内存、绝不落盘**：本模块不再读写 ``webui/cache/moments.json``，
  也不再创建/写入 ``media/moments/``；图片/头像字节由 worker 以 base64 内联返回，
  这里解码进 ``_media`` 字典，``stop()`` / 进程退出即清空；
* ``start()`` **不自动同步**，只做可用性检查；同步只能由 ``sync_async(limit)`` 触发；
* ``sync_async(limit=10)``：limit 只允许 **5/10/20**（其它值按 10），只同步时间最新
  的 N 条；``status()`` 带 ``limit``/``count``；``items()`` 最多返回 **30** 条；
* 新增 ``clear()``（清空内存数据）与 ``media(key)``（按 key 取内存图片字节）；
* 同步失败不影响聊天监听与页面其他功能，只在朋友圈页签显示可读原因。

worker 仍由 ``WechatMoments/.venv`` 的 Python（3.12）在子进程运行；本模块绝不
import ``wx4``（那是 Python 3.12 的依赖）。
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from webui.moments_crypto import isaac64_keystream

#: 子进程同步超时（秒），契约 3.3
SYNC_TIMEOUT = 120.0

#: V2：允许的同步条数（其它值按 10）
ALLOWED_LIMITS = (5, 10, 20)
DEFAULT_SYNC_LIMIT = 10

#: V2：内存里最多保留 / API 最多返回的朋友圈条数
MAX_ITEMS = 30
DEFAULT_ITEMS = 30

#: ``scope_items`` 默认条数（给 LLM 用，最大不超过 MAX_ITEMS）
DEFAULT_SCOPE_ITEMS = 20

#: 自定义朋友圈解释器的环境变量（构造参数之后、venv 之前）
ENV_PYTHON = "WXBOX_MOMENTS_PYTHON"


class MomentsError(RuntimeError):
    """朋友圈同步过程中可预期的失败（已在 message 里给出中文原因）。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _text(value: object) -> str:
    """把任意值安全转成 str（None → ""）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return ""


def _opt_int(value: object) -> int | None:
    """可选整数：解析失败返回 None。"""
    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _normalize_limit(value: object, default: int = DEFAULT_SYNC_LIMIT) -> int:
    """同步条数只允许 5/10/20，其它值按 10 处理。"""
    number = _opt_int(value)
    if number is None:
        return default
    return number if number in ALLOWED_LIMITS else default


def _clamp_items_limit(value: object, default: int) -> int:
    """``items()`` / ``scope_items()`` 的条数：1..MAX_ITEMS。"""
    number = _opt_int(value)
    if number is None:
        number = default
    if number < 1:
        number = 1
    if number > MAX_ITEMS:
        number = MAX_ITEMS
    return number


def _decode(data: object) -> str:
    """子进程输出转 str（UTF-8，无法解码的字节替换掉）。"""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return _text(data)


def _summarize(text: str, limit: int = 400) -> str:
    """摘要 stderr：取最后一条非空行并截断，便于页面显示。"""
    lines = [line.strip() for line in (text or "").split("\n") if line.strip()]
    if not lines:
        return ""
    summary = lines[-1]
    if len(summary) > limit:
        summary = summary[:limit] + "…"
    return summary


def _b64decode(value: object) -> bytes | None:
    """base64 文本 → bytes；空/非法返回 None。"""
    text = _text(value).strip()
    if not text:
        return None
    try:
        data = base64.b64decode(text, validate=False)
    except (ValueError, TypeError, binascii.Error):
        return None
    return data or None


# ---------------------------------------------------------------------------
# worker JSON → V2 内存 dict
# ---------------------------------------------------------------------------


def _normalize_media(raw: object) -> tuple[dict, dict[str, tuple[bytes, str]]]:
    """规整一个媒体条目，并解出内联图片字节。

    Returns:
        ``(给 API 的 dict, {media_key: (bytes, mime)})``；``*_b64`` 一律丢弃。
    """
    media_map: dict[str, tuple[bytes, str]] = {}
    if not isinstance(raw, dict):
        return {}, media_map
    media_type = _text(raw.get("type")).strip().lower()
    if media_type not in ("image", "video"):
        media_type = "image"
    thumb_key = _text(raw.get("thumb_key"))
    thumb_mime = _text(raw.get("thumb_mime"))
    data = _b64decode(raw.get("thumb_b64"))
    if data is not None and thumb_key:
        media_map[thumb_key] = (data, thumb_mime or "image/jpeg")
    else:
        thumb_key = ""
        thumb_mime = ""
    if media_type == "video":
        return (
            {
                "type": "video",
                "path": _text(raw.get("path")),
                "cover": _text(raw.get("cover")),
                "duration": _opt_int(raw.get("duration")),
                "thumb": _text(raw.get("thumb")),
                "thumb_key": thumb_key,
                "thumb_mime": thumb_mime,
                "video_url": _text(raw.get("video_url")),
                "video_token": _text(raw.get("video_token")),
                "video_key": _text(raw.get("video_key")),
            },
            media_map,
        )
    return (
        {
            "type": "image",
            "path": _text(raw.get("path")),
            "width": _opt_int(raw.get("width")),
            "height": _opt_int(raw.get("height")),
            # 旧字段向前兼容（V2 前端用 thumb_key 走 /api/moments/media/<key>）
            "thumb": _text(raw.get("thumb")),
            "thumb_key": thumb_key,
            "thumb_mime": thumb_mime,
        },
        media_map,
    )


def _normalize_comments(raw: object) -> list[dict]:
    """规整评论列表：每条 ``{author, content}``。"""
    if not isinstance(raw, list):
        return []
    result: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        result.append(
            {
                "author": _text(entry.get("author")),
                "content": _text(entry.get("content")),
            }
        )
    return result


def _normalize_link(raw: object) -> dict | None:
    """规整分享链接：只接受 http(s) 的 ``{title, url}``。"""
    if not isinstance(raw, dict):
        return None
    title = _text(raw.get("title")).strip()
    url = _text(raw.get("url")).strip()
    if not title or not url:
        return None
    if not (url.startswith("http://") or url.startswith("https://")):
        return None
    return {"title": title, "url": url}


def _normalize_item(raw: object) -> tuple[dict | None, dict[str, tuple[bytes, str]]]:
    """把 worker 的一条朋友圈规整成 V2 dict，并解出头像/图片字节。"""
    media_map: dict[str, tuple[bytes, str]] = {}
    if not isinstance(raw, dict):
        return None, media_map
    moment_id = _text(raw.get("id")).strip()
    if not moment_id:
        return None, media_map

    comments = _normalize_comments(raw.get("comments"))
    raw_count = _opt_int(raw.get("raw_comment_count"))
    if raw_count is None:
        raw_count = len(comments)
    likes_raw = raw.get("likes")
    likes: list[str] = []
    if isinstance(likes_raw, list):
        for name in likes_raw:
            text = _text(name)
            if text and text not in likes:
                likes.append(text)

    media_entries: list[dict] = []
    media_raw = raw.get("media")
    if isinstance(media_raw, list):
        for entry in media_raw:
            normalized, entry_media = _normalize_media(entry)
            if normalized:
                media_entries.append(normalized)
                media_map.update(entry_media)

    # 头像：V2 只保留 avatar_key/avatar_mime（字节进 _media）
    avatar_key = _text(raw.get("avatar_key"))
    avatar_mime = _text(raw.get("avatar_mime"))
    avatar_data = _b64decode(raw.get("avatar_b64"))
    if avatar_data is not None and avatar_key:
        media_map[avatar_key] = (avatar_data, avatar_mime or "image/jpeg")
    else:
        avatar_key = ""
        avatar_mime = ""

    username = _text(raw.get("username")).strip()
    item = {
        "id": moment_id,
        "username": username,
        "author": _text(raw.get("author")).strip() or username,
        "avatar": "",  # V2 旧字段：图片走 avatar_key
        "avatar_key": avatar_key,
        "avatar_mime": avatar_mime,
        "create_time": _opt_int(raw.get("create_time")) or 0,
        "time_text": _text(raw.get("time_text")),
        "content": _text(raw.get("content")),
        "location": _text(raw.get("location")),
        "link": _normalize_link(raw.get("link")),
        "media": media_entries,
        "comments": comments,
        "likes": likes,
        "raw_comment_count": raw_count,
    }
    return item, media_map


def _normalize_account(raw: object) -> dict[str, str]:
    """规整账号信息 ``{wxid, nick_name}``。"""
    if not isinstance(raw, dict):
        return {"wxid": "", "nick_name": ""}
    return {
        "wxid": _text(raw.get("wxid")).strip(),
        "nick_name": _text(raw.get("nick_name")).strip(),
    }


class MomentsService:
    """朋友圈内存服务（V2）：限量同步、图片/头像只进内存、可清空。

    Args:
        project_root: 项目根目录（含 ``WechatMoments/``、``webui/``）。
        bridge: 可选，任何带 ``broadcast(event: dict)`` 的对象（通常是 ``WebBridge``）。
        python: 朋友圈子进程解释器；默认 ``WechatMoments/.venv/Scripts/python.exe``。
        worker: worker 脚本路径；默认 ``<project_root>/webui/moments_worker.py``。
        moments_dir: ``WechatMoments`` 目录；默认 ``<project_root>/WechatMoments``。
        media_dir: **V2 已停用**（仅为兼容旧调用保留，不会创建/写入）。
        cache_path: **V2 已停用**（不再读写缓存文件）。
        runner: 测试注入，``runner(args) -> (returncode, stdout, stderr)``；
            提供后不真正启动子进程（不依赖微信）。
        logger: 日志函数（默认 ``print``）。
    """

    def __init__(
        self,
        project_root: Path,
        *,
        bridge: Any = None,
        python: str | None = None,
        worker: Path | None = None,
        moments_dir: Path | None = None,
        media_dir: Path | None = None,
        cache_path: Path | None = None,
        runner: Callable[[list[str]], tuple] | None = None,
        logger: Callable[[str], Any] = print,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self._bridge = bridge
        self._python_arg = str(python) if python else ""
        self.worker_path = (
            Path(worker).resolve()
            if worker is not None
            else self.project_root / "webui" / "moments_worker.py"
        )
        self.moments_dir = (
            Path(moments_dir).resolve()
            if moments_dir is not None
            else self.project_root / "WechatMoments"
        )
        # V2：这两个参数只为兼容旧调用保留，不参与任何磁盘读写。
        self.media_dir = (
            Path(media_dir).resolve()
            if media_dir is not None
            else self.project_root / "media" / "moments"
        )
        self.cache_path = (
            Path(cache_path).resolve()
            if cache_path is not None
            else self.project_root / "webui" / "cache" / "moments.json"
        )
        self._runner = runner
        self._logger = logger if callable(logger) else (lambda _message: None)
        self._lock = threading.RLock()
        self._items: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self._media: dict[str, tuple[bytes, str]] = {}
        #: 朋友圈视频按需下载后的内存缓存（键 ``<feed_id>:<media_index>``；不落盘）
        self._video_cache: dict[str, tuple[bytes, str]] = {}
        self._video_lock = threading.Lock()
        self._account: dict[str, str] = {"wxid": "", "nick_name": ""}
        self._synced_at: float | None = None
        self._error: str | None = None
        self._limit: int = DEFAULT_SYNC_LIMIT
        self._syncing = False
        self._thread: threading.Thread | None = None
        self._started = False
        self._stopped = False
        #: 清空/停止时递增；后台同步完成时若代际不一致就丢弃结果
        self._generation = 0
        #: 自动监听朋友圈（只轮询本地 sns.db 是否有新内容，不做 UI 操作）
        self._auto_enabled = False
        self._auto_interval = 30
        self._auto_thread: threading.Thread | None = None
        self._auto_stop = threading.Event()
        self._auto_latest = 0
        self._auto_checked_at: float | None = None
        #: 只有「微信实时监听」开启时才自动检查/刷新朋友圈，结束监听后完全不操作
        self._listening = False
        #: 每 30 分钟才点开一次微信朋友圈刷新（避免频繁抢焦点）
        self._ui_refresh_interval = 1800
        self._last_ui_refresh = 0.0

    # ------------------------------------------------------------------
    # 日志 / 广播
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        """写日志；日志函数本身抛异常也不影响同步。"""
        try:
            self._logger(f"[moments] {message}")
        except Exception:  # noqa: BLE001
            pass

    def _broadcast(self, event: dict) -> None:
        """向 SSE 订阅者广播事件（bridge 可能为 None 或抛异常）。"""
        bridge = self._bridge
        if bridge is None:
            return
        broadcast = getattr(bridge, "broadcast", None)
        if not callable(broadcast):
            return
        try:
            broadcast(event)
        except Exception as exc:  # noqa: BLE001 - 广播失败不影响同步结果
            self._log(f"广播事件失败：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # 可用性（解释器 / worker / WechatMoments 目录）
    # ------------------------------------------------------------------

    @staticmethod
    def _as_interpreter(value: str) -> str:
        """把配置值解析成可执行的解释器路径（支持绝对路径或 PATH 命令名）。"""
        value = (value or "").strip().strip('"')
        if not value:
            return ""
        candidate = Path(value).expanduser()
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except OSError:
            pass
        found = shutil.which(value)
        return found or ""

    def _resolve_python(self) -> tuple[str, str, str]:
        """按契约顺序解析子进程解释器。

        Returns:
            ``(解释器路径, 来源说明, 不可用原因)``；成功时原因为 ``""``。
        """
        if self._python_arg:
            resolved = self._as_interpreter(self._python_arg)
            if resolved:
                return resolved, "构造参数", ""
            return "", "构造参数", f"指定的朋友圈 Python 解释器不存在：{self._python_arg}"

        env_value = os.environ.get(ENV_PYTHON, "").strip()
        if env_value:
            resolved = self._as_interpreter(env_value)
            if resolved:
                return resolved, f"环境变量 {ENV_PYTHON}", ""
            self._log(f"环境变量 {ENV_PYTHON} 指向的解释器不存在，已忽略：{env_value}")

        for relative in (Path(".venv") / "Scripts" / "python.exe", Path(".venv") / "bin" / "python"):
            candidate = self.moments_dir / relative
            try:
                if candidate.is_file():
                    return str(candidate), "WechatMoments/.venv", ""
            except OSError:
                pass

        current = _text(sys.executable).strip()
        if current:
            try:
                if Path(current).is_file():
                    return current, "当前主进程解释器", ""
            except OSError:
                pass
        return (
            "",
            "",
            "未找到朋友圈 Python 解释器：请确认 WechatMoments/.venv/Scripts/python.exe 存在，"
            f"或设置环境变量 {ENV_PYTHON} 指向可用的 Python。",
        )

    def _availability(self) -> tuple[bool, str, str]:
        """返回 ``(是否可用, 不可用原因, 解释器路径)``。"""
        python, _source, problem = self._resolve_python()
        if problem:
            return False, problem, python
        try:
            if not self.worker_path.is_file():
                return (
                    False,
                    f"未找到朋友圈同步脚本：{self.worker_path}；请确认项目文件 webui/moments_worker.py 完整。",
                    python,
                )
        except OSError:
            return False, f"无法访问朋友圈同步脚本：{self.worker_path}", python
        try:
            if not self.moments_dir.is_dir():
                return (
                    False,
                    "未安装（该功能不再维护）：未找到 WechatMoments 目录 "
                    f"{self.moments_dir}。网页端朋友圈模块已停止维护，"
                    "不影响消息监听与 AI 分析。",
                    python,
                )
        except OSError:
            return False, "未安装（该功能不再维护）：无法访问 WechatMoments 目录", python
        return True, "", python

    # ------------------------------------------------------------------
    # 内存数据
    # ------------------------------------------------------------------

    def _clear_locked(self) -> None:
        """（持锁）清空全部内存朋友圈数据与媒体字节。"""
        self._generation += 1
        self._items = []
        self._by_id = {}
        self._media = {}
        self._synced_at = None
        self._error = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """V2：**不读缓存、不自动同步**，只检查可用性并记录原因。"""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stopped = False
        available, reason, _python = self._availability()
        if not available:
            self._log(f"朋友圈模块暂不可用：{reason}")
        else:
            self._log("朋友圈模块已就绪（V2 内存模式，仅用户点击同步时读取）")

    def stop(self) -> None:
        """停止后台同步线程与自动监听，并清空全部内存数据（进程退出即消失）。"""
        self._auto_stop.set()
        self._auto_enabled = False
        self._listening = False
        auto_thread = self._auto_thread
        if auto_thread is not None and auto_thread.is_alive():
            auto_thread.join(timeout=2.0)
        self._auto_thread = None
        with self._lock:
            self._stopped = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
            if thread.is_alive():
                self._log("朋友圈同步线程仍在运行（子进程最多 120 秒），已停止等待。")
        with self._lock:
            self._clear_locked()
            self._video_cache.clear()
            self._syncing = False
            self._thread = None

    def clear(self) -> dict:
        """清空内存 items 与媒体字节；返回 ``{"ok": True, "count": 0}``。"""
        with self._lock:
            self._clear_locked()
            self._video_cache.clear()
        self._log("朋友圈内存数据已清空")
        return {"ok": True, "count": 0}

    # ------------------------------------------------------------------
    # 状态 / 查询
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """服务状态（V2：含 ``limit`` / ``count``）；线程安全。"""
        available, reason, python = self._availability()
        with self._lock:
            return {
                "available": available,
                "reason": reason,
                "syncing": self._syncing,
                "limit": self._limit,
                "count": len(self._items),
                "synced_at": self._synced_at,
                "account": dict(self._account),
                "error": self._error,
                "python": python,
                "moments_dir": str(self.moments_dir),
                "auto": self._auto_enabled,
                "auto_interval": self._auto_interval,
                "auto_latest": self._auto_latest,
                "auto_checked_at": self._auto_checked_at,
                "listening": self._listening,
            }

    def set_listening(self, listening: bool) -> dict:
        """由「开始/结束监听」触发：监听期间才自动检查/刷新朋友圈。"""
        listening = bool(listening)
        with self._lock:
            self._listening = listening
            auto_enabled = self._auto_enabled
        if listening and auto_enabled:
            self._start_auto_thread()
            # 从开启监听算起，30 分钟后才第一次操作微信窗口
            self._last_ui_refresh = time.time()
            self._log("实时监听已开启：朋友圈自动检查启动（每 30 分钟刷新一次微信朋友圈）")
        else:
            self._auto_stop.set()
            self._auto_thread = None
            if not listening:
                self._log("实时监听已结束：已停止朋友圈自动检查/刷新")
        self._broadcast(
            {"type": "moments_status", "data": {"listening": listening, "auto": auto_enabled}}
        )
        return {"ok": True, "listening": listening}

    def set_auto(self, enabled: bool, interval: int = 30) -> dict:
        """开启/关闭自动监听朋友圈（仅在实时监听开启时生效）。"""
        enabled = bool(enabled)
        try:
            interval = int(interval or 30)
        except (TypeError, ValueError):
            interval = 30
        interval = max(10, min(300, interval))
        with self._lock:
            self._auto_enabled = enabled
            self._auto_interval = interval
            listening = self._listening
        if enabled and listening:
            self._start_auto_thread()
            self._last_ui_refresh = time.time()
            self._log(f"朋友圈自动检查已开启：每 {interval} 秒；每 30 分钟刷新一次微信窗口")
        else:
            self._auto_stop.set()
            self._auto_thread = None
            self._log("朋友圈自动检查已关闭（实时监听开启前不会操作微信）")
        self._broadcast(
            {
                "type": "moments_status",
                "data": {"auto": enabled, "auto_interval": interval},
            }
        )
        return {"ok": True, "enabled": enabled, "interval": interval}

    def _start_auto_thread(self) -> None:
        self._auto_stop.clear()
        thread = self._auto_thread
        if thread is None or not thread.is_alive():
            thread = threading.Thread(
                target=self._auto_loop, name="wxbox-moments-auto", daemon=True
            )
            self._auto_thread = thread
            thread.start()

    def _auto_loop(self) -> None:
        """后台轮询：本地朋友圈库有新内容才触发一次限量同步。"""
        while not self._auto_stop.wait(self._auto_interval):
            with self._lock:
                if not self._auto_enabled or not self._listening:
                    continue
            try:
                # 每 30 分钟让微信客户端自己刷新一次朋友圈（窗口级点击，失败只记日志）
                if time.time() - self._last_ui_refresh >= self._ui_refresh_interval:
                    self._last_ui_refresh = time.time()
                    try:
                        from webui.moments_ui import refresh_moments_window

                        result = refresh_moments_window(self._logger)
                        if result.get("ok"):
                            self._log("已通过微信窗口刷新朋友圈，等待本地库更新 ...")
                        else:
                            self._log(f"未执行微信窗口刷新：{result.get('reason')}")
                    except Exception as exc:  # noqa: BLE001
                        self._log(f"自动刷新微信朋友圈异常：{type(exc).__name__}: {exc}")
                latest = self._check_latest()
                with self._lock:
                    self._auto_checked_at = time.time()
                    last = self._auto_latest
                    syncing = self._syncing
                    limit = self._limit
                if latest <= 0:
                    continue
                if latest > last and not syncing:
                    self._log(f"自动监听：检测到朋友圈有新内容（latest={latest}），开始同步 ...")
                    self.sync_async(limit)
            except Exception as exc:  # noqa: BLE001 - 自动轮询失败只记日志
                self._log(f"自动监听朋友圈检查失败：{type(exc).__name__}: {exc}")

    def _check_latest(self) -> int:
        """调用 worker --check，返回本地朋友圈最新 create_time（0 表示无内容）。"""
        available, reason, python = self._availability()
        if not available:
            raise MomentsError(reason)
        returncode, stdout, stderr = self._invoke_check(python)
        payload = self._parse_payload(stdout)
        if payload is None:
            raise MomentsError(self._describe_process_failure(returncode, stderr))
        if not payload.get("ok"):
            raise MomentsError(str(payload.get("error") or "检查朋友圈失败"))
        try:
            return int(payload.get("latest") or 0)
        except (TypeError, ValueError):
            return 0

    def _invoke_check(self, python: str) -> tuple[int, str, str]:
        """调用 worker 的轻量 ``--check`` 模式；支持注入 runner 便于测试。"""
        args = [
            python,
            str(self.worker_path),
            "--moments-dir",
            str(self.moments_dir),
            "--check",
        ]
        if self._runner is not None:
            try:
                result = self._runner(args)
            except Exception as exc:  # noqa: BLE001
                raise MomentsError(f"朋友圈检查失败：{type(exc).__name__}: {exc}") from exc
            if not isinstance(result, (tuple, list)) or len(result) < 2:
                raise MomentsError("朋友圈检查失败：测试 runner 必须返回 (返回码, stdout, stderr)")
            returncode = _opt_int(result[0]) or 0
            stdout = _decode(result[1])
            stderr = _decode(result[2]) if len(result) > 2 else ""
            return returncode, stdout, stderr
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            completed = subprocess.run(
                args,
                cwd=str(self.moments_dir),
                capture_output=True,
                timeout=60,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise MomentsError("检查朋友圈超时（超过 60 秒）") from exc
        except OSError as exc:
            raise MomentsError(f"无法启动朋友圈检查进程：{exc}") from exc
        return (
            int(completed.returncode or 0),
            _decode(completed.stdout),
            _decode(completed.stderr),
        )

    def items(self, limit: int = DEFAULT_ITEMS) -> list[dict]:
        """时间倒序的朋友圈 dict（V2 最多 30 条；``*_b64`` 已去掉）。"""
        count = _clamp_items_limit(limit, DEFAULT_ITEMS)
        with self._lock:
            return [dict(entry) for entry in self._items[:count]]

    def get(self, moment_id: str) -> dict | None:
        """按 id 取单条朋友圈；不存在返回 ``None``。"""
        key = _text(moment_id).strip()
        if not key:
            return None
        with self._lock:
            entry = self._by_id.get(key)
            return dict(entry) if entry is not None else None

    def media(self, key: str) -> tuple[bytes, str] | None:
        """按内存媒体 key（``thumb_key`` / ``avatar_key``）取 ``(bytes, mime)``。"""
        media_key = _text(key).strip()
        if not media_key:
            return None
        with self._lock:
            entry = self._media.get(media_key)
            return (entry[0], entry[1]) if entry is not None else None

    def video(self, moment_id: str, index: int) -> tuple[bytes, str] | None:
        """按需下载并解密朋友圈视频（内存缓存），返回 ``(bytes, mime)`` 或 None。

        微信朋友圈视频不在本机落盘；这里从动态 XML 里的 CDN 地址拉取，按微信规则
        只解密前 128KB（ISAAC-64 + XOR），其余为明文 MP4。数据只进本进程内存。
        """
        entry = self.get(moment_id)
        if not entry:
            return None
        media_list = entry.get("media") or []
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return None
        if idx < 0 or idx >= len(media_list):
            return None
        media = media_list[idx]
        if not isinstance(media, dict) or media.get("type") != "video":
            return None
        try:
            feed_id = int(entry.get("create_time") or 0)
        except (TypeError, ValueError):
            feed_id = 0
        cache_key = f"{moment_id}:{idx}"
        with self._video_lock:
            hit = self._video_cache.get(cache_key)
        if hit is not None:
            return hit
        url = _text(media.get("video_url"))
        token = _text(media.get("video_token"))
        key = _text(media.get("video_key"))
        if not url or not token:
            return None
        fixed = _build_sns_video_url(url, token)
        try:
            request = urllib.request.Request(
                fixed,
                headers={"User-Agent": "MicroMessenger Client", "Accept": "*/*"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(256 * 1024 * 1024)
                content_type = response.headers.get("Content-Type", "video/mp4")
        except Exception as exc:  # noqa: BLE001 - 在线视频失败只影响该条
            self._log(f"朋友圈视频下载失败：{type(exc).__name__}: {exc}")
            return None
        if not data:
            return None
        try:
            seed = int(key) if key else 0
        except (TypeError, ValueError):
            seed = 0
        if seed:
            n = min(len(data), 131072)
            try:
                stream = isaac64_keystream(seed, n)
                plain = bytearray(data)
                for i in range(n):
                    plain[i] ^= stream[i]
                data = bytes(plain)
            except Exception as exc:  # noqa: BLE001
                self._log(f"朋友圈视频解密失败：{type(exc).__name__}: {exc}")
                return None
        if data[4:8] == b"ftyp":
            mime = "video/mp4"
        else:
            # 少数视频可能未加密/格式不同；不能播就不缓存
            if "mp4" not in content_type.lower() and "video" not in content_type.lower():
                self._log("朋友圈视频下载后不是可识别 MP4，已忽略")
                return None
            mime = content_type or "video/mp4"
        result = (data, mime)
        with self._video_lock:
            self._video_cache[cache_key] = result
        return result

    def scope_items(self, scope: dict, limit: int = DEFAULT_SCOPE_ITEMS) -> list[dict]:
        """给 LLM 用的朋友圈范围切片。

        ``scope={"type":"moments"}`` → 最近 ``limit`` 条（最多 30）；
        ``scope={"type":"moment","id":"..."}`` → 单条（找不到返回空列表）。
        """
        if not isinstance(scope, dict):
            return []
        count = _clamp_items_limit(limit, DEFAULT_SCOPE_ITEMS)
        scope_type = _text(scope.get("type")).strip().lower()
        if scope_type == "moment":
            entry = self.get(_text(scope.get("id")))
            return [entry] if entry is not None else []
        if scope_type == "moments":
            return self.items(count)
        return []

    # ------------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------------

    def sync_async(self, limit: int = DEFAULT_SYNC_LIMIT) -> dict:
        """后台限量同步一次；立即返回 ``{"ok":True,"syncing":True}`` 或错误。"""
        limit = _normalize_limit(limit)
        available, reason, python = self._availability()
        if not available:
            with self._lock:
                self._error = reason
            self._log(f"无法同步朋友圈：{reason}")
            return {"ok": False, "error": reason}

        with self._lock:
            if self._syncing:
                return {"ok": True, "syncing": True}
            self._syncing = True
            self._error = None
            self._limit = limit
            generation = self._generation
            thread = threading.Thread(
                target=self._sync_worker,
                args=(python, limit, generation),
                name="wxbox-moments-sync",
                daemon=True,
            )
            self._thread = thread

        self._broadcast({"type": "moments_status", "data": {"syncing": True}})
        thread.start()
        return {"ok": True, "syncing": True}

    def wait_sync(self, timeout: float = SYNC_TIMEOUT) -> bool:
        """等待后台同步结束（测试/退出用）；返回是否已不再同步。"""
        with self._lock:
            thread = self._thread
        if thread is not None:
            try:
                thread.join(timeout=max(0.0, float(timeout)))
            except (TypeError, ValueError):
                pass
        with self._lock:
            return not self._syncing

    def _sync_worker(self, python: str, limit: int, generation: int) -> None:
        """后台线程主体：跑子进程、解码内联媒体、更新内存并广播结果。"""
        error: str | None = None
        payload: dict | None = None
        try:
            returncode, stdout, stderr = self._invoke_worker(python, limit)
            payload = self._parse_payload(stdout)
            if payload is None:
                error = self._describe_process_failure(returncode, stderr)
            elif not payload.get("ok"):
                error = _text(payload.get("error")).strip() or "朋友圈同步失败"
                code = _text(payload.get("code")).strip()
                if code:
                    self._log(f"朋友圈同步失败（code={code}）：{error}")
            elif not self._apply_success(payload, limit, generation):
                # 同步过程中用户已 clear()/stop()，丢弃本次结果
                self._log("朋友圈同步完成，但期间数据已被清空，结果已丢弃")
                with self._lock:
                    self._syncing = False
                return
        except MomentsError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成可读状态
            error = f"朋友圈同步异常：{type(exc).__name__}: {exc}"

        with self._lock:
            if generation != self._generation:
                # 期间被 clear()/stop()：只结束同步状态，不广播、不写状态
                self._syncing = False
                return
            if error is not None:
                self._error = error
            else:
                self._error = None
            self._syncing = False
            ok = error is None
            count = len(self._items)
            synced_at = self._synced_at
            account = dict(self._account)

        if ok:
            self._log(f"朋友圈同步完成：{count} 条（limit={limit}，synced_at={synced_at}）")
        else:
            self._log(f"朋友圈同步失败：{error}")
        self._broadcast(
            {
                "type": "moments",
                "data": {
                    "ok": ok,
                    "count": count,
                    "limit": limit,
                    "synced_at": synced_at,
                    "error": error,
                    "account": account,
                },
            }
        )
        self._broadcast({"type": "moments_status", "data": {"syncing": False}})

    def _invoke_worker(self, python: str, limit: int) -> tuple[int, str, str]:
        """调用 worker：优先注入的 ``runner``，否则启动真实子进程（超时 120 秒）。"""
        args = [
            python,
            str(self.worker_path),
            "--moments-dir",
            str(self.moments_dir),
            "--limit",
            str(limit),
            "--inline-media",
        ]
        if self._runner is not None:
            try:
                result = self._runner(args)
            except Exception as exc:  # noqa: BLE001 - 注入 runner 的失败也转可读错误
                raise MomentsError(f"朋友圈同步失败：{type(exc).__name__}: {exc}") from exc
            if not isinstance(result, (tuple, list)) or len(result) < 2:
                raise MomentsError("朋友圈同步失败：测试 runner 必须返回 (返回码, stdout, stderr)")
            returncode = _opt_int(result[0]) or 0
            stdout = _decode(result[1])
            stderr = _decode(result[2]) if len(result) > 2 else ""
            return returncode, stdout, stderr

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            completed = subprocess.run(
                args,
                cwd=str(self.moments_dir),
                capture_output=True,
                timeout=SYNC_TIMEOUT,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise MomentsError(
                f"朋友圈同步超时（超过 {int(SYNC_TIMEOUT)} 秒），请稍后重试。"
            ) from exc
        except OSError as exc:
            raise MomentsError(f"无法启动朋友圈同步进程：{exc}") from exc
        return (
            int(completed.returncode),
            _decode(completed.stdout),
            _decode(completed.stderr),
        )

    @staticmethod
    def _parse_payload(stdout: str) -> dict | None:
        """从 stdout 里找 worker 输出的 JSON（容忍日志混入多行）。

        注意只按 ``\\n`` 拆行：朋友圈正文可能含 Unicode 行分隔符 ``U+2028/U+2029``，
        用 ``str.splitlines()`` 会把一行合法 JSON 拆碎（随后整段解析兜底）。
        """
        text = (stdout or "").strip()
        if not text:
            return None
        for line in reversed(text.split("\n")):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict) and "ok" in data:
                return data
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) and "ok" in data else None

    @staticmethod
    def _describe_process_failure(returncode: int, stderr: str) -> str:
        """子进程没有输出合法 JSON 时，拼一句可读的中文原因。"""
        detail = _summarize(stderr)
        if returncode != 0:
            message = f"朋友圈同步进程异常退出（代码 {returncode}）"
        else:
            message = "朋友圈同步进程没有返回有效结果"
        if detail:
            message = f"{message}：{detail}"
        else:
            message = f"{message}。请打开日志或重试。"
        return message

    def _apply_success(self, payload: dict, limit: int, generation: int) -> bool:
        """把 worker 的成功结果写入内存；返回是否真正应用（被清空则 False）。"""
        raw_moments = payload.get("moments")
        items: list[dict] = []
        media: dict[str, tuple[bytes, str]] = {}
        seen: set[str] = set()
        if isinstance(raw_moments, list):
            for raw in raw_moments:
                item, item_media = _normalize_item(raw)
                if item is None:
                    continue
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                items.append(item)
                media.update(item_media)
                if len(items) >= MAX_ITEMS:
                    break

        synced_at_raw = payload.get("synced_at")
        try:
            synced_at = float(synced_at_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            synced_at = time.time()
        if synced_at <= 0:
            synced_at = time.time()
        account = _normalize_account(payload.get("account"))
        items.sort(
            key=lambda entry: (
                int(entry.get("create_time") or 0),
                _text(entry.get("id")),
            ),
            reverse=True,
        )
        items = items[:MAX_ITEMS]
        by_id: dict[str, dict] = {}
        for entry in items:
            by_id.setdefault(_text(entry.get("id")), entry)

        with self._lock:
            if generation != self._generation or self._stopped:
                return False
            self._items = items
            self._by_id = by_id
            self._media = media
            self._account = account
            self._synced_at = synced_at
            self._limit = _normalize_limit(limit)
            self._error = None
            try:
                self._auto_latest = max(
                    self._auto_latest,
                    max((int(entry.get("create_time") or 0) for entry in items), default=0),
                )
            except (TypeError, ValueError):
                pass
        return True


def _build_sns_video_url(url: str, token: str) -> str:
    """微信朋友圈视频 CDN URL：https + token 放在参数最前面。"""
    value = _text(url).strip()
    if not value:
        return ""
    value = value.replace("http://", "https://", 1)
    parts = urllib.parse.urlsplit(value)
    existing = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    token = _text(token).strip()
    if token:
        params: list[tuple[str, str]] = [("token", token), ("idx", "1")]
        params.extend(
            (k, v) for k, v in existing if k.lower() not in ("token", "idx")
        )
    else:
        params = existing
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(params), "")
    )
