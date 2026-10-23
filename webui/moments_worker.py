#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""朋友圈同步子进程（由 ``webui/moments.py`` 用 WechatMoments 的 venv 启动）。

V2 协议（见 ``webui/CONTRACT.md`` 末尾「v2 需求变更」V2-2）::

    <WechatMoments/.venv/Scripts/python.exe> webui/moments_worker.py \\
        --moments-dir WechatMoments --limit 10 --inline-media

* **只输出一行 UTF-8 JSON 到 stdout**，诊断信息一律写 stderr；
* **不写任何文件**（不创建 ``media/moments/``、不写 ``webui/cache/``）：图片/头像
  以 base64 放在 JSON 里，由父进程解码进内存，进程退出即消失；
* 成功：``{"ok":true,"account":{...},"limit":N,"synced_at":<epoch>,
  "moments":[...],"stats":{...}}``；
* 失败：``{"ok":false,"error":"中文原因","code":"..."}``，且**退出码固定为 0**。

媒体策略（风控优先）
--------------------
* 本地有微信缓存原图：用 Pillow 压到最长边 ≤720 的 JPEG（质量 80），base64 内联；
* 本地没有：在线拉 CDN ``<thumb>``（150 宽）——最多 **60 张/次**、并发 ≤3、
  单请求超时 10 秒、**不重试**；请求头 ``User-Agent: MicroMessenger Client``、
  不加 Referer、http、``?idx=1&token=``；响应必须带 ``x-Enc: 1``，用
  :mod:`webui.moments_crypto` 的 ISAAC-64 解密并校验图片魔数；无 x-Enc 的
  120×120 占位图/风控页一律判失败；
* 头像：读 ``wx4.media.avatar_path`` 后压到最长边 ≤96 的 JPEG（质量 80）内联。

本文件只在 ``WechatMoments`` 的 Python 3.12 venv 里运行；网页主进程（Python 3.14）
**不会** import 它，也不会 import ``wx4``。红线：只读微信目录与进程内存（沿用
``WechatMoments`` 的一次性只读取密钥逻辑），网页端数据只进内存、不落盘。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:  # 作为包导入（测试 / 父进程复用函数时）
    from .moments_crypto import decrypt_xenc, looks_like_image, sniff_image_ext
except ImportError:  # 作为脚本直接运行（sys.path[0] = webui/）
    from moments_crypto import decrypt_xenc, looks_like_image, sniff_image_ext  # type: ignore[no-redef]

#: 允许的同步条数（其它值按 10 处理）
ALLOWED_LIMITS = (5, 10, 20)
DEFAULT_LIMIT = 10

#: 朋友圈媒体类型（4.x 与 3.x 一致）
IMAGE_TYPE = "2"
MUSIC_TYPE = "5"
VIDEO_TYPE = "6"

#: 内联图片/头像的压缩参数
IMAGE_MAX_SIDE = 720
IMAGE_QUALITY = 80
AVATAR_MAX_SIDE = 96

#: 在线补图风控参数：最多 60 张/次，并发 ≤3，超时 10 秒，不重试
ONLINE_MAX_ATTEMPTS = 60
ONLINE_MAX_WORKERS = 3
ONLINE_TIMEOUT = 10.0
ONLINE_MAX_BYTES = 8 * 1024 * 1024
#: 在线补图总预算（秒）：子进程总超时 120 秒，留足余量
INLINE_BUDGET_SECONDS = 80.0

#: 设为 ``1/true/yes/on`` 可关闭在线补图（离线测试/排障用）
ONLINE_DISABLE_ENV = "WXBOX_MOMENTS_NO_ONLINE"

#: 腾讯 CDN 要求的请求头（**不要加 Referer**，加了会返回 120×120 占位图）
_ONLINE_HEADERS = {
    "User-Agent": "MicroMessenger Client",
    "Accept": "*/*",
    "Connection": "keep-alive",
}

_MIME_BY_EXT = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}

_USAGE = (
    "用法：python webui/moments_worker.py --moments-dir <WechatMoments> "
    "--limit <5|10|20> --inline-media"
)

#: 懒加载的 Pillow 模块（缺 Pillow 时图片无法压缩，会退回在线缩略图）
_pil_image = None
_pil_image_ops = None


# ---------------------------------------------------------------------------
# 输出 / 错误
# ---------------------------------------------------------------------------


class WorkerError(RuntimeError):
    """子进程内部的可预期失败（带机器可读 code 与中文原因）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _configure_stdio() -> None:
    """把 stdout/stderr 统一成 UTF-8（Windows 控制台默认 GBK 会乱码/报错）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 极老解释器/被重定向时忽略
            pass


def _log(message: str) -> None:
    """诊断信息只写 stderr，绝不污染 stdout 的 JSON。"""
    try:
        print(f"[moments_worker] {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - 日志失败不影响结果
        pass


def _emit(payload: dict) -> None:
    """把结果作为单行 JSON 写到 stdout。

    朋友圈正文里可能出现 Unicode 行分隔符 ``U+2028/U+2029``，``json.dumps`` 默认
    不会转义它们，会让「单行 JSON」被按行处理的工具错误拆开；这里显式转义。
    """
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    text = text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - 控制台编码异常时退回二进制写
        try:
            sys.stdout.buffer.write((text + "\n").encode("utf-8", "replace"))
            sys.stdout.buffer.flush()
        except Exception:  # noqa: BLE001
            pass


def _failure_payload(exc: BaseException) -> dict:
    """把任意异常转成契约里的失败 JSON（中文原因 + code）。"""
    if isinstance(exc, WorkerError):
        return {"ok": False, "error": exc.message, "code": exc.code}
    if isinstance(exc, ImportError):
        return {
            "ok": False,
            "error": (
                f"朋友圈同步子进程缺少依赖：{exc}。"
                "请确认使用 WechatMoments 的 Python 环境（WechatMoments/.venv），"
                "或在其中执行 pip install -r WechatMoments/requirements.txt。"
            ),
            "code": "deps_missing",
        }
    name = type(exc).__name__
    text = str(exc).strip() or name
    if name == "Wx4Error":
        return {
            "ok": False,
            "error": f"读取微信数据失败：{text}。请确认微信 4.x 已启动并登录，然后重试。",
            "code": "wechat_not_ready",
        }
    if name in ("OperationalError", "DatabaseError"):
        return {"ok": False, "error": f"读取微信数据库失败：{text}", "code": "wechat_not_ready"}
    return {"ok": False, "error": f"朋友圈同步异常：{text}", "code": "internal_error"}


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def _normalize_limit(value: object) -> int:
    """同步条数只允许 5/10/20，其它值按 10 处理。"""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return number if number in ALLOWED_LIMITS else DEFAULT_LIMIT


def _parse_args(argv: list[str]) -> dict[str, str]:
    """解析命令行（手写而不是 argparse：参数错误也要输出 JSON 且退出码 0）。"""
    values = {
        "moments_dir": "",
        "limit": str(DEFAULT_LIMIT),
        "inline_media": "0",
        "media_out": "",
        "project_root": "",
        "check": "0",
    }
    with_value = {
        "--moments-dir": "moments_dir",
        "--limit": "limit",
        "--media-out": "media_out",  # V2 保留兼容，但忽略、不写任何文件
        "--project-root": "project_root",  # V2 保留兼容，但不再需要
    }
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-h", "--help"):
            raise WorkerError("bad_args", _USAGE)
        if arg == "--inline-media":
            values["inline_media"] = "1"
            index += 1
            continue
        if arg == "--check":
            values["check"] = "1"
            index += 1
            continue
        key = with_value.get(arg)
        if key is None:
            raise WorkerError("bad_args", f"无法识别的参数：{arg}。{_USAGE}")
        if index + 1 >= len(argv):
            raise WorkerError("bad_args", f"参数 {arg} 缺少取值。{_USAGE}")
        values[key] = argv[index + 1]
        index += 2
    if not values["moments_dir"]:
        raise WorkerError("bad_args", f"缺少 --moments-dir。{_USAGE}")
    return values


def _resolve_path(value: str, base: Path) -> Path:
    """相对路径按 ``base`` 解析成绝对路径。"""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


# ---------------------------------------------------------------------------
# 工具
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


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _duration(value: object) -> int | None:
    """视频时长（秒，四舍五入为整数）；解析不了返回 None。"""
    if value is None or value == "":
        return None
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return max(1, int(round(seconds)))


def _format_time(timestamp: int) -> str:
    """epoch 秒 → 本地时间 ``MM-DD HH:MM``（契约示例：``10-17 18:51``）。"""
    if not timestamp or timestamp <= 0:
        return ""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(timestamp)))
    except (OSError, OverflowError, ValueError):
        return ""


def _mime_for_image(data: bytes) -> str:
    """按图片魔数返回 MIME；不是图片返回 ``""``。"""
    return _MIME_BY_EXT.get(sniff_image_ext(data), "")


def _b64(data: bytes) -> str:
    """bytes → base64 ASCII 字符串。"""
    return base64.b64encode(data).decode("ascii") if data else ""


def _pil() -> tuple[object, object] | None:
    """懒加载 Pillow，返回 ``(Image, ImageOps)``；不可用返回 None。"""
    global _pil_image, _pil_image_ops
    if _pil_image is None:
        try:
            from PIL import Image as _Image  # type: ignore[import-not-found]
            from PIL import ImageOps as _ImageOps  # type: ignore[import-not-found]

            _pil_image = _Image
            _pil_image_ops = _ImageOps
        except Exception as exc:  # noqa: BLE001 - 无 Pillow 时退回在线缩略图
            _log(f"Pillow 不可用，本地图片将改用在线缩略图：{type(exc).__name__}: {exc}")
            return None
    if _pil_image is None:
        return None
    return _pil_image, _pil_image_ops


def _compress_image(data: bytes, max_side: int) -> tuple[bytes, str, int, int] | None:
    """把图片压到最长边 ≤ ``max_side`` 的 JPEG（质量 80）。

    Returns:
        ``(JPEG 字节, "image/jpeg", 宽, 高)``；解码/压缩失败返回 None。
    """
    if not data:
        return None
    modules = _pil()
    if modules is None:
        return None
    image_module, image_ops = modules
    try:
        with image_module.open(io.BytesIO(data)) as image:  # type: ignore[union-attr]
            if image_ops is not None:
                image = image_ops.exif_transpose(image)  # type: ignore[union-attr]
            image = image.convert("RGB")
            image.thumbnail((max_side, max_side))
            output = io.BytesIO()
            image.save(output, "JPEG", quality=IMAGE_QUALITY, optimize=True)
            return output.getvalue(), "image/jpeg", int(image.width), int(image.height)
    except Exception as exc:  # noqa: BLE001 - 单张图片失败不影响整体
        _log(f"压缩图片失败：{type(exc).__name__}: {exc}")
        return None


def _image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """读取图片宽高（Pillow 不可用/失败返回 ``(None, None)``）。"""
    modules = _pil()
    if modules is None:
        return None, None
    image_module, _ops = modules
    try:
        with image_module.open(io.BytesIO(data)) as image:  # type: ignore[union-attr]
            return int(image.width), int(image.height)
    except Exception:  # noqa: BLE001
        return None, None


def _check_dependencies() -> None:
    """提前检查解析朋友圈 XML 需要的第三方库，失败给可读原因。"""
    missing: list[str] = []
    for name in ("xmltodict", "dataclasses_json"):
        try:
            __import__(name)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{name}（{type(exc).__name__}）")
    if missing:
        raise WorkerError(
            "deps_missing",
            "朋友圈同步子进程缺少依赖：" + "、".join(missing)
            + "。请使用 WechatMoments 的 Python 环境（WechatMoments/.venv）运行，"
            "或在该环境执行 pip install -r WechatMoments/requirements.txt。",
        )


# ---------------------------------------------------------------------------
# raw XML：取媒体节点（在线补图用 url/thumb/token/key/size）
# ---------------------------------------------------------------------------


def _node_dict(value: object) -> dict:
    """xmltodict 的节点可能是 dict/None，这里统一成 dict。"""
    return value if isinstance(value, dict) else {}


def _raw_media_nodes(raw_xml: object) -> dict[str, dict]:
    """解析 raw XML 里的 ``ContentObject/mediaList/media``，返回按位置与 id 索引的节点。

    Returns:
        ``{"#0": node, "#1": node, ..., "<media id>": node}``；解析失败返回 ``{}``。
    """
    text = _text(raw_xml).strip()
    if not text:
        return {}
    try:
        import xmltodict

        data = xmltodict.parse(text, force_list={"media"})
    except Exception as exc:  # noqa: BLE001 - 只是拿不到在线补图信息，不影响主流程
        _log(f"解析朋友圈原始 XML 失败（跳过在线补图）：{type(exc).__name__}: {exc}")
        return {}
    root = _node_dict(data)
    item = _node_dict(root.get("SnsDataItem"))
    container = item if item else root
    timeline = _node_dict(container.get("TimelineObject"))
    content = _node_dict(timeline.get("ContentObject"))
    media_list = _node_dict(content.get("mediaList"))
    nodes = media_list.get("media")
    if isinstance(nodes, dict):
        nodes = [nodes]
    if not isinstance(nodes, list):
        return {}
    result: dict[str, dict] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        result[f"#{index}"] = node
        media_id = _text(node.get("id")).strip()
        if media_id:
            result.setdefault(media_id, node)
    return result


def _match_raw_media(raw_map: dict[str, dict], media_id: object, index: int) -> dict | None:
    """按 ``<media><id>`` 匹配 raw XML 节点；匹配不到再按位置兜底。"""
    if not raw_map:
        return None
    key = _text(media_id).strip()
    if key and key in raw_map:
        return raw_map[key]
    return raw_map.get(f"#{index}")


def _raw_media_meta(raw_node: object) -> dict:
    """从 raw XML ``<media>`` 节点提取在线拉取所需信息。

    Returns:
        ``{"url","thumb_url","token","key"}``（缺失为空字符串）。
    """
    node = _node_dict(raw_node)
    url_node = _node_dict(node.get("url"))
    thumb_node = _node_dict(node.get("thumb"))
    return {
        "url": _text(url_node.get("#text")).strip(),
        "thumb_url": _text(thumb_node.get("#text")).strip(),
        "token": _text(thumb_node.get("@token")).strip()
        or _text(url_node.get("@token")).strip(),
        "key": _text(thumb_node.get("@key")).strip()
        or _text(url_node.get("@key")).strip(),
    }


# ---------------------------------------------------------------------------
# 在线缩略图（CDN + ISAAC-64 解密，只进内存）
# ---------------------------------------------------------------------------


def _online_thumbs_enabled() -> bool:
    """在线补图是否启用（``WXBOX_MOMENTS_NO_ONLINE`` 可关闭）。"""
    value = _text(os.environ.get(ONLINE_DISABLE_ENV)).strip().lower()
    return value not in ("1", "true", "yes", "on")


def _build_cdn_url(url: object, token: object) -> str:
    """把 XML 里的 CDN 地址改造成可下载的 http URL（追加 ``idx=1&token=...``）。

    * 强制 http（实测 https 拉不到微信 CDN 图片）；
    * 去掉旧 token/idx，避免重复参数；
    * 保持原路径尺寸后缀（``/150`` 缩略图、``/0`` 原图）。
    """
    value = _text(url).strip()
    if not value:
        return ""
    lower = value.lower()
    if lower.startswith("https://"):
        value = "http://" + value[len("https://"):]
    elif not lower.startswith("http://"):
        return ""
    base, separator, query = value.partition("?")
    kept: list[str] = []
    if separator:
        for part in query.split("&"):
            if not part:
                continue
            name = part.partition("=")[0].strip().lower()
            if name in ("token", "idx"):
                continue
            kept.append(part)
    if kept:
        base = f"{base}?{'&'.join(kept)}"
    token_text = _text(token).strip()
    if token_text:
        connector = "&" if "?" in base else "?"
        base = f"{base}{connector}idx=1&token={urllib.parse.quote(token_text, safe='')}"
    return base


def _fetch_online_thumb(
    task: dict,
    *,
    timeout: float,
) -> tuple[str, bytes, str]:
    """下载并解密一张在线缩略图（不落盘）。

    Returns:
        ``(状态, 明文图片字节, MIME)``；状态 ``"ok"`` 表示成功，其余为失败分类
        （``http_400`` / ``token_expired`` / ``placeholder`` / ``decrypt_fail`` /
        ``network`` / ``empty`` / ``too_large`` / ``budget``）。
    """
    url = _build_cdn_url(task.get("url"), task.get("token"))
    key = _text(task.get("key")).strip()
    if not url or not key:
        return "no_source", b"", ""
    request = urllib.request.Request(url, headers=_ONLINE_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(timeout))) as response:
            x_enc = _text(response.headers.get("x-Enc")).strip().lower()
            raw = response.read(ONLINE_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        code = _int(getattr(exc, "code", 0))
        try:
            exc.close()
        except Exception:  # noqa: BLE001
            pass
        if code == 400:
            return "http_400", b"", ""
        if code in (401, 403, 404, 410):
            return "token_expired", b"", ""
        return f"http_{code}", b"", ""
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        _log(
            f"在线缩略图网络失败（feed={task.get('feed_id')} idx={task.get('index')}）："
            f"{type(exc).__name__}: {exc}"
        )
        return "network", b"", ""
    except Exception as exc:  # noqa: BLE001 - 任何下载异常都只算失败
        _log(
            f"在线缩略图下载异常（feed={task.get('feed_id')} idx={task.get('index')}）："
            f"{type(exc).__name__}: {exc}"
        )
        return "network", b"", ""
    if not raw:
        return "empty", b"", ""
    if len(raw) > ONLINE_MAX_BYTES:
        return "too_large", b"", ""
    # 只有 CDN 明确声明加密才解密；没有 x-Enc 的响应是 token 过期后的占位图/风控页，
    # 绝不能当图片使用（本机实测占位图为 120×120 且无 x-Enc）。
    if x_enc not in ("1", "true"):
        return "placeholder", b"", ""
    try:
        plain = decrypt_xenc(raw, key)
    except Exception as exc:  # noqa: BLE001 - key 非法等
        _log(f"在线缩略图解密失败（feed={task.get('feed_id')} idx={task.get('index')}）：{exc}")
        return "decrypt_fail", b"", ""
    mime = _mime_for_image(plain)
    if not mime:
        return "decrypt_fail", b"", ""
    return "ok", plain, mime


def _fill_online_images(
    tasks: list[dict],
    *,
    deadline: float,
    errors: dict[str, int],
) -> int:
    """并发补齐在线缩略图（把 base64 写回 media dict），返回成功张数。"""
    if not tasks:
        return 0
    done = 0

    def run_task(task: dict) -> tuple[dict, str, bytes, str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return task, "budget", b"", ""
        timeout = max(1.0, min(ONLINE_TIMEOUT, remaining))
        status, data, mime = _fetch_online_thumb(task, timeout=timeout)
        return task, status, data, mime

    with ThreadPoolExecutor(
        max_workers=ONLINE_MAX_WORKERS, thread_name_prefix="wxbox-thumb"
    ) as pool:
        futures = [pool.submit(run_task, task) for task in tasks]
        for future in as_completed(futures):
            try:
                task, status, data, mime = future.result()
            except Exception as exc:  # noqa: BLE001 - 单张失败不影响整体
                _log(f"在线缩略图任务异常：{type(exc).__name__}: {exc}")
                errors["exception"] = errors.get("exception", 0) + 1
                continue
            if status == "ok" and data:
                entry = task["entry"]
                # 唯一内存 key（不能直接用 XML 里的 CDN key：同一 feed 的所有图共用它，
                # 会导致 /api/moments/media/<key> 全部命中同一张图）
                entry["thumb_key"] = f"t_{task.get('feed_id')}_{task.get('index')}"
                entry["thumb_mime"] = mime
                entry["thumb_b64"] = _b64(data)
                width, height = _image_dimensions(data)
                if width and height:
                    entry["width"] = width
                    entry["height"] = height
                done += 1
            else:
                code = status if status else "unknown"
                errors[code] = errors.get(code, 0) + 1
    return done


# ---------------------------------------------------------------------------
# 朋友圈 → 内存 dict（图片/头像 base64 内联）
# ---------------------------------------------------------------------------


class _ItemBuilder:
    """把 ``wx4.sns.Moment`` 转成 V2 契约 dict（媒体只放内存 base64）。"""

    def __init__(
        self,
        *,
        name_map: dict[str, str],
        wx_media,
        online_enabled: bool = True,
    ) -> None:
        self.name_map = name_map
        self.wx_media = wx_media
        self.online_enabled = online_enabled
        self.stats = {
            "images_local": 0,
            "images_online": 0,
            "images_missing": 0,
            "avatars": 0,
            "videos": 0,
        }
        self.online_tasks: list[dict] = []
        self.online_errors: dict[str, int] = {}
        self.online_skipped = 0
        self._avatar_cache: dict[str, dict] = {}

    # -- 名字 -------------------------------------------------------------

    def display_name(self, username: str) -> str:
        """username → 显示名（备注优先，其次昵称，最后 wxid 原值）。"""
        username = _text(username).strip()
        if not username:
            return ""
        return self.name_map.get(username) or username

    # -- 头像 -------------------------------------------------------------

    def _avatar_fields(self, username: str) -> dict:
        """头像内联字段：``avatar_key/mime/b64``（失败为空字符串）。

        头像压缩到最长边 ≤96 的 JPEG（质量 80），同一作者每次同步只处理一次。
        """
        username = _text(username).strip()
        empty = {
            "avatar": "",
            "avatar_key": "",
            "avatar_mime": "",
            "avatar_b64": "",
        }
        if not username:
            return empty
        cached = self._avatar_cache.get(username)
        if cached is not None:
            return dict(cached)
        result = dict(empty)
        try:
            source = self.wx_media.avatar_path(username)
            if source is not None:
                try:
                    raw = Path(source).read_bytes()
                except OSError:
                    raw = b""
                compressed = _compress_image(raw, AVATAR_MAX_SIDE)
                if compressed is not None:
                    data, mime, _width, _height = compressed
                    digest = hashlib.sha1(username.encode("utf-8")).hexdigest()
                    result.update(
                        {
                            "avatar_key": f"a_{digest}",
                            "avatar_mime": mime,
                            "avatar_b64": _b64(data),
                        }
                    )
                    self.stats["avatars"] += 1
        except Exception as exc:  # noqa: BLE001 - 头像失败不影响朋友圈内容
            _log(f"读取头像失败（{username}）：{type(exc).__name__}: {exc}")
        self._avatar_cache[username] = result
        return dict(result)

    # -- 图片 -------------------------------------------------------------

    @staticmethod
    def _empty_thumb_fields() -> dict:
        return {"thumb": "", "thumb_key": "", "thumb_mime": "", "thumb_b64": ""}

    def _queue_online(
        self,
        feed_id: int,
        index: int,
        media_id: object,
        raw_xml: object,
        entry: dict,
    ) -> None:
        """本地没有图片时，排队一个「在线拉缩略图」任务（最多 60 个，不重试）。"""
        if len(self.online_tasks) >= ONLINE_MAX_ATTEMPTS:
            self.online_skipped += 1
            return
        meta = _raw_media_meta(_match_raw_media(_raw_media_nodes(raw_xml), media_id, index))
        url = meta["thumb_url"] or meta["url"]
        if not url or not meta["key"]:
            self.online_errors["no_source"] = self.online_errors.get("no_source", 0) + 1
            return
        self.online_tasks.append(
            {
                "feed_id": feed_id,
                "index": index,
                "url": url,
                "token": meta["token"],
                "key": meta["key"],
                "entry": entry,
            }
        )

    def _build_image(
        self,
        feed_id: int,
        index: int,
        node,
        raw_xml: object,
    ) -> dict:
        """图片：本地原图优先（压到 ≤720 JPEG）；否则排队在线 thumb（150 宽）。"""
        entry: dict = {
            "type": "image",
            "path": "",
            "width": None,
            "height": None,
        }
        entry.update(self._empty_thumb_fields())
        thumb_key = f"t_{feed_id}_{index}"

        raw = b""
        try:
            source = self.wx_media.image_path_for_media(node)
            if source is not None:
                try:
                    raw = Path(source).read_bytes()
                except OSError:
                    raw = b""
        except Exception as exc:  # noqa: BLE001 - 单张图片失败不影响整条朋友圈
            _log(f"读取本地图片失败（feed={feed_id} idx={index}）：{type(exc).__name__}: {exc}")
            raw = b""
        if raw:
            compressed = _compress_image(raw, IMAGE_MAX_SIDE)
            if compressed is not None:
                data, mime, width, height = compressed
                entry.update(
                    {
                        "width": width,
                        "height": height,
                        "thumb_key": thumb_key,
                        "thumb_mime": mime,
                        "thumb_b64": _b64(data),
                    }
                )
                self.stats["images_local"] += 1
                return entry

        if self.online_enabled:
            self._queue_online(
                feed_id, index, getattr(node, "id", None), raw_xml, entry
            )
        return entry

    def _build_video(self, feed_id: int, index: int, node, raw_xml: object) -> dict:
        """视频：不内联视频本体，但带上 CDN 信息，网页端按需下载/解密播放。"""
        import re

        raw_map = _raw_media_nodes(raw_xml)
        raw_node = _match_raw_media(raw_map, getattr(node, "id", None), index)
        meta = _raw_media_meta(raw_node)
        raw_text = _text(raw_xml)
        enc_key = ""
        try:
            match = re.search(r'<enc\s+key="(\d+)"', raw_text, re.I)
            if match:
                enc_key = match.group(1)
        except Exception:  # noqa: BLE001
            pass
        video_url = meta.get("url", "")
        video_token = meta.get("token", "")
        if not video_token and video_url:
            try:
                from urllib.parse import parse_qsl, urlsplit

                video_token = dict(parse_qsl(urlsplit(video_url).query)).get("token", "")
            except Exception:  # noqa: BLE001
                video_token = ""
        entry: dict = {
            "type": "video",
            "path": "",
            "cover": "",
            "duration": _duration(getattr(node, "videoDuration", None)),
            "video_url": video_url,
            "video_token": video_token,
            "video_key": enc_key,
        }
        entry.update(self._empty_thumb_fields())
        self.stats["videos"] += 1
        return entry

    def _build_media(self, feed_id: int, content_object, raw_xml: object) -> list[dict]:
        media_list = getattr(content_object, "mediaList", None)
        nodes = getattr(media_list, "media", None) or []
        result: list[dict] = []
        for index, node in enumerate(nodes):
            if node is None:
                continue
            media_type = _text(getattr(node, "type", "")).strip()
            try:
                if media_type == IMAGE_TYPE:
                    result.append(self._build_image(feed_id, index, node, raw_xml))
                elif media_type == VIDEO_TYPE:
                    result.append(self._build_video(feed_id, index, node, raw_xml))
                elif media_type == MUSIC_TYPE:
                    continue  # 音乐分享不内联
            except Exception as exc:  # noqa: BLE001
                _log(f"处理媒体失败（feed={feed_id} idx={index}）：{type(exc).__name__}: {exc}")
        return result

    @staticmethod
    def _build_link(content_object, media_items: list[dict]) -> dict | None:
        """分享链接卡片：有标题 + http(s) 链接、且没有图片/视频时才生成。"""
        if content_object is None or media_items:
            return None
        title = _text(getattr(content_object, "title", "")).strip()
        url = _text(getattr(content_object, "contentUrl", "")).strip()
        if not title or not url:
            return None
        if not (url.startswith("http://") or url.startswith("https://")):
            return None
        return {"title": title, "url": url}

    # -- 单条朋友圈 -------------------------------------------------------

    def build(self, moment) -> dict | None:
        feed_id = _int(getattr(moment, "feed_id", 0))
        if feed_id <= 0:
            return None
        moment_msg = getattr(moment, "moment", None)
        timeline = getattr(moment_msg, "timelineObject", None)
        if timeline is None:
            return None
        username = (
            _text(getattr(timeline, "username", ""))
            or _text(getattr(moment, "username", ""))
        ).strip()
        create_time = _int(getattr(timeline, "createTime", 0))
        if create_time <= 0:
            create_time = _int(getattr(moment, "create_time", 0))
        content = _text(getattr(timeline, "contentDesc", "")).strip()
        location_obj = getattr(timeline, "location", None)
        location = _text(getattr(location_obj, "poiName", "")).strip()
        content_object = getattr(timeline, "ContentObject", None)
        raw_xml = getattr(moment, "raw_xml", "")

        media_items = self._build_media(feed_id, content_object, raw_xml)
        comments = [
            {
                "author": self.display_name(_text(getattr(comment, "from_user_name", ""))),
                "content": _text(getattr(comment, "content", "")),
            }
            for comment in (getattr(moment, "comments", None) or [])
        ]
        likes: list[str] = []
        for username_like in getattr(moment, "likes", None) or []:
            name = self.display_name(_text(username_like))
            if name and name not in likes:
                likes.append(name)

        item = {
            "id": str(feed_id),
            "username": username,
            "author": self.display_name(username) or username,
            "create_time": create_time,
            "time_text": _format_time(create_time),
            "content": content,
            "location": location,
            "link": self._build_link(content_object, media_items),
            "media": media_items,
            "comments": comments,
            "likes": likes,
            "raw_comment_count": len(comments),
        }
        item.update(self._avatar_fields(username))
        return item


def _build_name_map(db, account: dict[str, str]) -> dict[str, str]:
    """username → 显示名映射（备注优先，其次昵称；读联系人失败则退化为 wxid）。"""
    mapping: dict[str, str] = {}
    try:
        contacts = db.contacts()
    except Exception as exc:  # noqa: BLE001 - 联系人库异常不应阻断朋友圈同步
        _log(f"读取联系人失败，作者名将显示 wxid：{type(exc).__name__}: {exc}")
        contacts = []
    for contact in contacts or []:
        if not isinstance(contact, dict):
            continue
        username = _text(contact.get("username")).strip()
        if not username:
            continue
        display = (
            _text(contact.get("remark")).strip()
            or _text(contact.get("nick_name")).strip()
            or username
        )
        mapping[username] = display
    wxid = account.get("wxid") or ""
    nick_name = account.get("nick_name") or ""
    if wxid:
        mapping[wxid] = nick_name or mapping.get(wxid) or wxid
    return mapping


def _safe_self_info(db) -> dict[str, str]:
    """读取本人资料；失败时退化为仅 wxid。"""
    try:
        info = db.self_info()
        if isinstance(info, dict):
            return {
                "username": _text(info.get("username")),
                "nick_name": _text(info.get("nick_name")),
            }
    except Exception as exc:  # noqa: BLE001
        _log(f"读取本人资料失败：{type(exc).__name__}: {exc}")
    return {"username": _text(getattr(db, "wxid", "")), "nick_name": ""}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run(opts: dict[str, str]) -> dict:
    """执行一次限量同步，返回成功 JSON 载荷（失败抛异常，由 :func:`main` 兜底）。"""
    started = time.time()
    started_mono = time.monotonic()
    cwd = Path.cwd()
    moments_dir = _resolve_path(opts["moments_dir"], cwd)
    limit = _normalize_limit(opts.get("limit"))
    if not moments_dir.is_dir():
        raise WorkerError("moments_dir_missing", f"未找到 WechatMoments 目录：{moments_dir}")
    try:
        os.chdir(moments_dir)
    except OSError as exc:
        raise WorkerError("internal_error", f"无法进入 WechatMoments 目录：{exc}") from exc
    if str(moments_dir) not in sys.path:
        sys.path.insert(0, str(moments_dir))

    _check_dependencies()

    # 延迟 import：依赖缺失/环境不对时要能转成可读 JSON 而不是崩在 import 处。
    from wx4 import Wx4Error  # noqa: F401  （用于 except）
    from wx4 import media as wx_media
    from wx4 import sns
    from wx4.db import get_db

    try:
        db = get_db()
    except Wx4Error as exc:
        raise WorkerError(
            "wechat_not_ready",
            f"读取微信 4.x 数据失败：{exc}。请确认微信已启动并登录，然后重试。",
        ) from exc

    account = _safe_self_info(db)
    if not account.get("wxid"):
        account["wxid"] = _text(getattr(db, "wxid", ""))
    name_map = _build_name_map(db, account)

    try:
        all_moments = sns.all_moments()
    except Wx4Error as exc:
        raise WorkerError(
            "wechat_not_ready",
            f"读取朋友圈失败：{exc}。请确认微信已启动并登录，然后在朋友圈里刷新后重试。",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise WorkerError(
            "parse_error", f"读取朋友圈失败：{type(exc).__name__}: {exc}"
        ) from exc
    if all_moments is None:
        all_moments = []
    # 只取时间最新的 N 条（sns.all_moments() 已按时间倒序）
    selected = list(all_moments)[:limit]

    online_enabled = _online_thumbs_enabled()
    builder = _ItemBuilder(
        name_map=name_map,
        wx_media=wx_media,
        online_enabled=online_enabled,
    )
    items: list[dict] = []
    seen: set[str] = set()
    failures = 0
    for moment in selected:
        try:
            item = builder.build(moment)
        except Exception as exc:  # noqa: BLE001 - 单条失败跳过，不影响整体
            failures += 1
            _log(f"跳过一条无法处理的朋友圈：{type(exc).__name__}: {exc}")
            continue
        if not item:
            failures += 1
            continue
        moment_id = item["id"]
        if moment_id in seen:
            continue
        seen.add(moment_id)
        items.append(item)

    # 在线补草稿图（只对本次 N 条、最多 60 张、并发 ≤3、超时 10s、不重试）。
    online_done = 0
    if online_enabled and builder.online_tasks:
        deadline = started_mono + INLINE_BUDGET_SECONDS
        try:
            online_done = _fill_online_images(
                builder.online_tasks, deadline=deadline, errors=builder.online_errors
            )
        except Exception as exc:  # noqa: BLE001 - 在线补图失败不能拖垮整体同步
            _log(f"在线补图阶段异常：{type(exc).__name__}: {exc}")
        builder.stats["images_online"] = online_done
        _log(
            f"在线缩略图：尝试 {len(builder.online_tasks)}，成功 {online_done}，"
            f"失败 {sum(builder.online_errors.values())}，跳过 {builder.online_skipped}，"
            f"分类 {builder.online_errors or '无'}"
        )
    builder.stats["images_missing"] = sum(
        1
        for item in items
        for media in item.get("media") or []
        if isinstance(media, dict) and media.get("type") == "image" and not media.get("thumb_key")
    )

    return {
        "ok": True,
        "account": {
            "wxid": _text(account.get("wxid")),
            "nick_name": _text(account.get("nick_name")),
        },
        "limit": limit,
        "synced_at": time.time(),
        "moments": items,
        "stats": {
            "moments": len(items),
            "limit": limit,
            **builder.stats,
            "online_attempts": len(builder.online_tasks),
            "online_skipped": builder.online_skipped,
            "thumb_errors": dict(builder.online_errors),
            "inline_media": True,
            "failures": failures,
            "elapsed_ms": int((time.time() - started) * 1000),
        },
    }


def run_check(opts: dict[str, str]) -> dict:
    """轻量检查朋友圈是否有新内容（不读媒体/不内联图片），供网页端自动监听轮询。"""
    started = time.time()
    cwd = Path.cwd()
    moments_dir = _resolve_path(opts["moments_dir"], cwd)
    if not moments_dir.is_dir():
        raise WorkerError("moments_dir_missing", f"未找到 WechatMoments 目录：{moments_dir}")
    try:
        os.chdir(moments_dir)
    except OSError as exc:
        raise WorkerError("internal_error", f"无法进入 WechatMoments 目录：{exc}") from exc
    if str(moments_dir) not in sys.path:
        sys.path.insert(0, str(moments_dir))
    _check_dependencies()

    from wx4 import Wx4Error
    from wx4 import sns
    from wx4.db import get_db

    try:
        db = get_db()
    except Wx4Error as exc:
        raise WorkerError(
            "wechat_not_ready",
            f"读取微信 4.x 数据失败：{exc}。请确认微信已启动并登录，然后重试。",
        ) from exc
    account = _safe_self_info(db)
    if not account.get("wxid"):
        account["wxid"] = _text(getattr(db, "wxid", ""))
    try:
        moments = sns.all_moments() or []
    except Wx4Error as exc:
        raise WorkerError(
            "wechat_not_ready",
            f"读取朋友圈失败：{exc}。请确认微信已启动并登录，然后在朋友圈里刷新后重试。",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise WorkerError("parse_error", f"读取朋友圈失败：{type(exc).__name__}: {exc}") from exc
    latest = 0
    for moment in moments:
        try:
            value = int(getattr(moment, "create_time", 0) or 0)
        except (TypeError, ValueError):
            value = 0
        latest = max(latest, value)
    return {
        "ok": True,
        "check": True,
        "account": {
            "wxid": _text(account.get("wxid")),
            "nick_name": _text(account.get("nick_name")),
        },
        "latest": latest,
        "count": len(moments),
        "checked_at": time.time(),
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def main(argv: list[str] | None = None) -> int:
    """入口：任何异常都转成单行 JSON，退出码固定 0。"""
    _configure_stdio()
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        options = _parse_args(arguments)
        payload = run_check(options) if options.get("check") == "1" else run(options)
    except Exception as exc:  # noqa: BLE001 - 契约要求失败也输出 JSON、退出码 0
        payload = _failure_payload(exc)
        _log(f"失败（{payload.get('code')}）：{payload.get('error')}")
    _emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
