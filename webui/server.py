"""本地 HTTP / SSE 服务（只依赖 Python 标准库）。

设计要点：
* ``ThreadingHTTPServer`` + ``BaseHTTPRequestHandler``：每个连接一个线程，SSE 长连接
  不会阻塞静态资源/API 请求；``protocol_version = "HTTP/1.1"`` 支持 keep-alive；
* 同源单页应用：``/`` 返回 ``webui/static/index.html``，并把模板占位符替换成真实令牌；
* ``/media/*`` 只读映射项目 ``media/`` 目录，做路径穿越防护（拒绝 ``..``/绝对路径/目录外）；
* 所有 ``/api/*``（除 ``/api/health``）必须带令牌（``X-WXBOX-Token`` 或 ``?token=``），
  ``/media/*`` 用 ``?token=``（``<img src>`` 不能带自定义头）；
* Host 头只允许回环地址，防 DNS rebinding 攻击（恶意网页把自己域名解析到 127.0.0.1 后
  用浏览器访问本服务）；
* 不返回 ``Access-Control-Allow-Origin``，浏览器跨域页面拿不到任何数据；
* 错误统一为 ``{"ok":false,"error":"人话","code":"..."}``。

契约见 ``webui/CONTRACT.md`` 第 3.4 / 4 / 5 节。
"""
from __future__ import annotations

import hmac
import html
import json
import os
import queue
import re
import shutil
import socket
import sys
import time
import traceback
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agi import get_service as get_agi_service
from .llm import LLMError, LLMService

if TYPE_CHECKING:  # 仅类型检查用；运行时不 import，避免朋友圈模块缺失时服务起不来
    from .bridge import WebBridge
    from .moments import MomentsService

__all__ = ["WebContext", "create_server"]

WEBUI_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = WEBUI_DIR.parent
STATIC_DIR = WEBUI_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"
AGI_FILE = STATIC_DIR / "agi.html"
AGI_CORE_FILE = STATIC_DIR / "agicode.html"
MEDIA_DIR = PROJECT_ROOT / "media"
MEDIA_DIR_RESOLVED = MEDIA_DIR.resolve()
STATIC_DIR_RESOLVED = STATIC_DIR.resolve()

# 请求体上限：分析请求里最多 12 条历史，1MB 足够；防止恶意大 body 占内存
MAX_BODY_BYTES = 2 * 1024 * 1024
# SSE 心跳间隔（契约要求 15 秒）
HEARTBEAT_SECONDS = 15.0
# SSE 写超时：前端卡死不读时，最多阻塞这么久就清理连接（不影响其他连接）
SSE_WRITE_TIMEOUT = 120.0
# V2：朋友圈数据只在内存，页面最多展示 30 条；同步只允许 5/10/20 条
MAX_MOMENTS_DISPLAY = 30
MOMENTS_SYNC_LIMITS = (5, 10, 20)
DEFAULT_MOMENTS_SYNC_LIMIT = 10
# V2：历史聊天同步可选条数（0 = 该好友在本机缓存到的全部）
HISTORY_LIMITS = (0, 5, 10, 20, 50, 100)

_VERBOSE = os.environ.get("WXBOX_WEB_VERBOSE", "").strip().lower() in {"1", "true", "yes", "on"}

# 只允许回环 Host，防 DNS rebinding
LOOPBACK_HOST_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' data: blob:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "base-uri 'self'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

# 静态/媒体文件类型表（不同平台 mimetypes 库结果不一致，这里显式维护）
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".pdf": "application/pdf",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".amr": "audio/amr",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".zip": "application/zip",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}

# 允许从 webui/static/ 直接取的文件后缀白名单
ALLOWED_STATIC_SUFFIXES = frozenset(
    {".html", ".htm", ".js", ".mjs", ".css", ".json", ".map", ".txt",
     ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2"}
)

# 错误码 -> HTTP 状态码（契约 4.1 的统一错误格式）
STATUS_BY_CODE = {
    "bad_request": 400,
    "unauthorized": 401,
    "not_found": 404,
    "busy": 409,
    "not_configured": 400,
    "llm_error": 502,
    "server_error": 500,
}


def _status_for_code(code: str) -> int:
    return STATUS_BY_CODE.get(str(code or "bad_request"), 400)


@dataclass
class WebContext:
    """HTTP 服务运行所需的上下文（由 ``web_app.py`` 构造后传给 :func:`create_server`）。"""

    bridge: WebBridge
    llm: LLMService
    token: str  # 32+ 位随机十六进制；页面内嵌，/api/* 与 /media/* 都要校验
    host: str
    port: int
    mode: str  # "real" | "fake"
    version: str = "1.0.0"
    backend_note: str = ""  # 真实后端回退原因（伪后端时页面提示用）
    moments: MomentsService | None = None  # 朋友圈服务；None 时朋友圈接口返回 available=false
    history: Any | None = None  # webui.history.HistoryService；运行时不 import，None 时历史接口 503
    groups: Any | None = None  # webui.groups.GroupsService；运行时不 import，None 时群聊接口 503
    contacts: Any | None = None  # webui.contacts.ContactsService；运行时不 import，None 时联系人接口 503
    videos: Any | None = None  # webui.videos.VideoService；运行时不 import，None 时视频接口 404
    video_auto: Any | None = None  # webui.video_auto.ChatVideoAutoCache；None 时自动缓存接口 503


class _ApiError(Exception):
    """内部用：把「HTTP 状态 + 错误码 + 中文提示」从深层逻辑抛到统一出口。"""

    def __init__(
        self,
        status: int,
        message: str,
        code: str = "bad_request",
        headers: list[tuple[str, str]] | None = None,
    ):
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)
        self.code = str(code or "bad_request")
        self.headers = headers or []


class _WebHTTPServer(ThreadingHTTPServer):
    """带上下文的 ThreadingHTTPServer。"""

    daemon_threads = True
    # 关键：Windows 上 SO_REUSEADDR 会让「端口已被占用」也绑定成功，导致后端收不到请求；
    # 关掉它才能让端口占用时可靠地抛 OSError（契约 3.4 要求）。
    allow_reuse_address = False
    request_queue_size = 64

    def __init__(self, server_address: tuple, handler_class: type, context: WebContext):
        self.context = context
        self.started_at = time.time()
        super().__init__(server_address, handler_class)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """静默客户端断开类异常。

        浏览器关标签页、CDP 截完图直接断开、SSE 连接被重置时，
        ``socketserver`` 默认会把 ``ConnectionResetError [WinError 10054]`` 的整段
        traceback 打到 stderr，用户会误以为是程序错误；这些都属于正常断连，直接忽略。
        其它异常（真正的代码 bug）仍交给父类打印，便于排障。
        """
        exc = sys.exc_info()[1]
        if isinstance(
            exc,
            (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError),
        ):
            return
        super().handle_error(request, client_address)


class _WebRequestHandler(BaseHTTPRequestHandler):
    """处理静态页面、媒体文件、REST API 与 SSE 长连接。"""

    protocol_version = "HTTP/1.1"
    server_version = "WXBOX-Web/1.0"
    sys_version = ""

    # ---- 基础 ----
    @property
    def context(self) -> WebContext:
        return self.server.context  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 标准库签名
        """访问日志默认静音（避免 SSE/心跳刷屏），设 WXBOX_WEB_VERBOSE=1 打开。"""
        if _VERBOSE:
            sys.stderr.write(f"[webui] {self.address_string()} {format % args}\n")

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        """协议层错误（请求行非法等）打到 stderr，便于排障。"""
        sys.stderr.write(f"[webui] 协议错误：{format % args}\n")

    # ---- HTTP 方法入口 ----
    def do_GET(self) -> None:  # noqa: N802 - 标准库约定
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802 - 不提供 CORS，直接 405
        self._dispatch("OPTIONS")

    def do_TRACE(self) -> None:  # noqa: N802 - 禁用 TRACE（防跨站追踪）
        self._dispatch("TRACE")

    # ---- 统一分发 ----
    def _dispatch(self, method: str) -> None:
        self._method = method
        self._responded = False
        self._query: dict[str, list[str]] = {}
        try:
            if not self._host_ok():
                self._send_error(
                    403,
                    "拒绝访问：Host 头不合法（本服务只允许通过 127.0.0.1 / localhost 访问）",
                    "unauthorized",
                )
                return
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path or "/"
            self._query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
            if method not in ("GET", "HEAD", "POST"):
                self._send_error(405, "请求方法不支持", "bad_request",
                                 headers=[("Allow", "GET, HEAD, POST")])
                return
            if method in ("GET", "HEAD"):
                # GET/HEAD 正常不带请求体；异常客户端若带了，keep-alive 下残留字节
                # 会被当成下一条请求行解析（串包 + stderr 噪音），直接关闭连接。
                try:
                    declared = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    declared = 0
                if declared > 0:
                    self.close_connection = True
            if path in ("/", "/index.html"):
                self._serve_index()
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path == "/favicon.ico":
                # 浏览器会自动请求 /favicon.ico，直接映射到前端提供的 svg
                self._serve_static("favicon.svg")
            elif path.startswith("/media/"):
                self._serve_media(path)
            elif path in ("/agi", "/agi.html"):
                self._serve_agi()
            elif path in ("/agi/core", "/agi/core.html"):
                self._serve_agi_core()
            elif path.startswith("/api/"):
                self._handle_api(path)
            else:
                self._send_error(404, "页面不存在", "not_found")
        except _ApiError as exc:
            self._send_error(exc.status, exc.message, exc.code, headers=exc.headers)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端提前断开（刷新/关标签页）属于正常情况
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - 兜底，返回 500 而不是崩掉连接线程
            sys.stderr.write(
                f"[webui] 处理请求失败 {method} {self.path}：{type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}"
            )
            try:
                self._send_error(500, "服务器内部错误，请查看控制台日志", "server_error")
            except Exception:  # noqa: BLE001
                self.close_connection = True

    # ---- Host / 令牌校验 ----
    def _host_ok(self) -> bool:
        """Host 头必须是回环地址（端口须与实际监听端口一致）。"""
        raw = (self.headers.get("Host") or "").strip().lower()
        if not raw:
            return False
        hostname, port_text = _split_host_header(raw)
        if not hostname:
            return False
        allowed = set(LOOPBACK_HOST_NAMES)
        bound_host = str(self.context.host or "").strip().strip("[]").lower()
        if bound_host and bound_host not in ("0.0.0.0", "::", ""):
            allowed.add(bound_host)
        if hostname not in allowed:
            return False
        if port_text is not None:
            try:
                if int(port_text) != int(self.context.port):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _provided_token(self) -> str:
        header_token = (self.headers.get("X-WXBOX-Token") or "").strip()
        if header_token:
            return header_token
        return self._query_first("token").strip()

    def _check_token(self) -> None:
        expected = str(self.context.token or "")
        provided = self._provided_token()
        if not expected or not provided or not hmac.compare_digest(provided, expected):
            raise _ApiError(
                401,
                "访问令牌缺失或不正确，请刷新页面后重试（请勿把地址分享给他人）",
                "unauthorized",
            )

    def _query_first(self, name: str, default: str = "") -> str:
        values = self._query.get(name) or []
        return str(values[0]) if values else default

    def _query_int(self, name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self._query_first(name, ""))
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, value))

    def _query_flag(self, name: str) -> bool:
        """解析布尔 query 参数：1/true/yes/on/y 为真，其余（含 0/false/空）为假。"""
        return self._query_first(name, "").strip().lower() in (
            "1", "true", "yes", "on", "y",
        )

    # ---- 请求体 ----
    def _read_json_body(self) -> dict:
        """读取并解析 JSON 对象请求体；非法/过大抛 400。"""
        if self._method not in ("POST", "PUT", "PATCH"):
            raise _ApiError(405, "该接口只接受 POST 请求", "bad_request")
        length_text = self.headers.get("Content-Length")
        try:
            length = int(length_text or 0)
        except (TypeError, ValueError):
            raise _ApiError(400, "请求体长度不合法", "bad_request") from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise _ApiError(400, "请求体过大（最多 2MB）", "bad_request")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _ApiError(400, f"请求体不是合法 JSON：{exc}", "bad_request") from None
        if not isinstance(payload, dict):
            raise _ApiError(400, "请求体必须是 JSON 对象", "bad_request")
        return payload

    # ---- 响应输出 ----
    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        cache_control: str | None = None,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        if self._responded:
            return
        self._responded = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            # 明确告诉客户端这条连接到此为止（错误响应、客户端要求关闭、GET 带 body 等）
            self.send_header("Connection", "close")
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        if self._method != "HEAD" and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _send_json(
        self,
        status: int,
        payload: dict,
        *,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            cache_control="no-store",
            extra_headers=extra_headers,
        )

    def _send_error(
        self,
        status: int,
        message: str,
        code: str,
        *,
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        if self._responded:
            self.close_connection = True
            return
        # 错误可能在读取请求体之前就返回（如 401 令牌校验、405 方法不符）。
        # HTTP/1.1 keep-alive 下未消费的 body 会被当作下一条请求行解析，导致串包和
        # "Bad request syntax" 之类的 stderr 噪音，所以错误响应统一关闭连接。
        self.close_connection = True
        self._send_json(
            status,
            {"ok": False, "error": str(message), "code": str(code)},
            extra_headers=headers,
        )

    def _send_file(
        self,
        path: Path,
        content_type: str,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        """流式返回一个本地文件（大文件不整块读进内存）。"""
        try:
            size = path.stat().st_size
        except OSError:
            raise _ApiError(404, "文件不存在", "not_found") from None
        if self._responded:
            return
        self._responded = True
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        if self._method == "HEAD":
            return
        try:
            with path.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile, length=64 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except OSError:
            # 文件在传输中被删/锁住：头已发出，只能断开连接让浏览器重试
            self.close_connection = True

    # ---- 静态页面 ----
    def _serve_index(self) -> None:
        self._require_get()
        try:
            raw = INDEX_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._send_bytes(
                503,
                (
                    "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
                    "<title>WXBOX · 页面未就绪</title>"
                    "<body style=\"background:#1e1f22;color:#e6e6e6;font-family:system-ui;"
                    "padding:40px;line-height:1.8\"><h2>前端页面尚未就绪</h2>"
                    "<p>webui/static/index.html 还不存在，不过 API 服务已正常启动。</p>"
                    "</body></html>"
                ).encode("utf-8"),
                "text/html; charset=utf-8",
                cache_control="no-store",
            )
            return
        except (OSError, UnicodeDecodeError) as exc:
            raise _ApiError(500, f"读取页面文件失败：{exc}", "server_error") from None
        page = (raw
                .replace("__WXBOX_TOKEN__", html.escape(str(self.context.token or ""), quote=True))
                .replace("__WXBOX_MODE__", html.escape(str(self.context.mode or "real"), quote=True))
                .replace("__WXBOX_VERSION__", html.escape(str(self.context.version or "1.0.0"), quote=True))
                .replace("__WXBOX_BACKEND_NOTE__",
                         html.escape(str(self.context.backend_note or ""), quote=True)))
        self._send_bytes(
            200,
            page.encode("utf-8"),
            "text/html; charset=utf-8",
            cache_control="no-store",
            extra_headers=[
                ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
                ("Referrer-Policy", "no-referrer"),
            ],
        )

    def _serve_agi(self) -> None:
        """渲染 AGI 草稿页（与首页同样的令牌注入与 CSP）。"""
        self._require_get()
        try:
            raw = AGI_FILE.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _ApiError(500, f"读取 AGI 页面失败：{exc}", "server_error") from None
        page = raw.replace(
            "__WXBOX_TOKEN__", html.escape(str(self.context.token or ""), quote=True)
        )
        self._send_bytes(
            200,
            page.encode("utf-8"),
            "text/html; charset=utf-8",
            cache_control="no-store",
            extra_headers=[
                ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
                ("Referrer-Policy", "no-referrer"),
            ],
        )

    def _serve_agi_core(self) -> None:
        """渲染 AGI core 页面（令牌注入/CSP 与旧 AGI 页一致）。"""
        self._require_get()
        try:
            raw = AGI_CORE_FILE.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _ApiError(500, f"读取 AGI core 页面失败：{exc}", "server_error") from None
        page = raw.replace(
            "__WXBOX_TOKEN__", html.escape(str(self.context.token or ""), quote=True)
        )
        self._send_bytes(
            200,
            page.encode("utf-8"),
            "text/html; charset=utf-8",
            cache_control="no-store",
            extra_headers=[
                ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
                ("Referrer-Policy", "no-referrer"),
            ],
        )

    # ---- AGI 草稿 API（只读 + dry-run，无发送能力） ----

    def _api_agi_status(self) -> None:
        self._send_json(200, get_agi_service().status())

    def _api_agi_drafts(self) -> None:
        only = self._query_first("qualified").strip().lower() in ("1", "true", "yes")
        self._send_json(200, get_agi_service().drafts(only_qualified=only))

    def _api_agi_generate(self) -> None:
        body = self._read_json_body()
        try:
            limit = int(body.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 30))
        result = get_agi_service().generate(limit=limit)
        self._send_json(200 if result.get("ok") else 409, result)

    # ---- AGI core API（handler 内延迟 import，避免 server 启动时依赖 core DB） ----

    @staticmethod
    def _agi_core_service():
        from agi.core import web_service

        return web_service.get_service()

    def _api_agi_core_status(self) -> None:
        try:
            result = self._agi_core_service().status()
        except Exception as exc:  # noqa: BLE001 - core DB/依赖异常给可读 503
            raise _ApiError(503, f"AGI core 状态读取失败：{exc}", "server_error") from None
        if not isinstance(result, dict):
            raise _ApiError(503, "AGI core 返回数据格式不正确", "server_error")
        self._send_json(200, result)

    def _api_agi_core_drafts(self) -> None:
        limit = self._query_int("limit", 20, 1, 50)
        try:
            result = self._agi_core_service().drafts(limit)
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(503, f"AGI core 草稿读取失败：{exc}", "server_error") from None
        if not isinstance(result, dict):
            raise _ApiError(503, "AGI core 返回数据格式不正确", "server_error")
        if result.get("ok") is False:
            raise _ApiError(
                503, str(result.get("error") or "读取 AGI core 草稿失败"), "server_error"
            )
        self._send_json(200, result)

    def _api_agi_core_cycle(self) -> None:
        body = self._read_json_body()
        text = str(body.get("text") or "")
        try:
            result = self._agi_core_service().run_cycle(text)
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(503, f"AGI core 运行失败：{exc}", "server_error") from None
        if not isinstance(result, dict):
            raise _ApiError(503, "AGI core 返回数据格式不正确", "server_error")
        if result.get("ok") is False:
            error = str(result.get("error") or "AGI core 运行失败")
            if "授权" in error:
                # consent 门禁未通过：403 让页面弹「授权」引导，而不是普通参数错误
                raise _ApiError(403, error, "unauthorized")
            # 空文本/过长等输入问题按现有风格回 400，页面可直接展示 error
            raise _ApiError(400, error, "bad_request")
        self._send_json(200, result)

    def _api_agi_core_feedback(self) -> None:
        body = self._read_json_body()
        draft_id = str(body.get("draft_id") or "").strip()
        reaction = str(body.get("reaction") or "").strip().lower()
        note = str(body.get("note") or "")
        if not draft_id:
            raise _ApiError(400, "缺少 draft_id", "bad_request")
        if reaction not in ("good", "ok", "bad", "ignore"):
            raise _ApiError(
                400, f"非法 reaction：{reaction or '(空)'}（只支持 good/ok/bad/ignore）",
                "bad_request",
            )
        try:
            result = self._agi_core_service().feedback(draft_id, reaction, note)
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(503, f"AGI core 反馈失败：{exc}", "server_error") from None
        if not isinstance(result, dict):
            raise _ApiError(503, "AGI core 返回数据格式不正确", "server_error")
        if result.get("ok") is False:
            error = str(result.get("error") or "提交反馈失败")
            if "草稿不存在" in error:
                raise _ApiError(404, error, "not_found")
            raise _ApiError(400, error, "bad_request")
        self._send_json(200, result)

    # ---- AGI core 记忆审阅 / 画像 / 授权（延迟 import，异常统一 503） ----

    def _agi_core_invoke(self, action: str, callback) -> dict:
        """延迟 import core service 并调用；异常/非 dict 返回统一转 503。"""
        try:
            service = self._agi_core_service()
            result = callback(service)
        except _ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - core DB/依赖异常给可读 503
            raise _ApiError(503, f"AGI core {action}失败：{exc}", "server_error") from None
        if not isinstance(result, dict):
            raise _ApiError(503, "AGI core 返回数据格式不正确", "server_error")
        return result

    def _send_agi_memory_result(self, result: dict, action: str) -> None:
        """记忆审批/驳回结果映射：不存在→404，其它业务失败→400，成功→200。"""
        if result.get("ok") is False:
            error = str(result.get("error") or action)
            if "不存在" in error or "not found" in error.lower():
                raise _ApiError(404, error, "not_found")
            raise _ApiError(400, error, "bad_request")
        self._send_json(200, result)

    def _api_agi_core_memories(self) -> None:
        status = self._query_first("status").strip() or "pending"
        limit = self._query_int("limit", 30, 1, 100)
        result = self._agi_core_invoke(
            "记忆列表读取", lambda service: service.memories(status, limit)
        )
        if result.get("ok") is False:
            raise _ApiError(
                503, str(result.get("error") or "读取 AGI core 记忆失败"), "server_error"
            )
        self._send_json(200, result)

    def _api_agi_core_memory_approve(self) -> None:
        body = self._read_json_body()
        memory_id = str(body.get("id") or "").strip()
        if not memory_id:
            raise _ApiError(400, "缺少 id", "bad_request")
        result = self._agi_core_invoke(
            "记忆审批", lambda service: service.approve_memory(memory_id)
        )
        self._send_agi_memory_result(result, "记忆审批失败")

    def _api_agi_core_memory_reject(self) -> None:
        body = self._read_json_body()
        memory_id = str(body.get("id") or "").strip()
        if not memory_id:
            raise _ApiError(400, "缺少 id", "bad_request")
        reason = str(body.get("reason") or "")
        result = self._agi_core_invoke(
            "记忆驳回", lambda service: service.reject_memory(memory_id, reason)
        )
        self._send_agi_memory_result(result, "记忆驳回失败")

    def _api_agi_core_profile(self) -> None:
        result = self._agi_core_invoke("画像读取", lambda service: service.profile())
        if result.get("ok") is False:
            raise _ApiError(
                503, str(result.get("error") or "读取 AGI core 画像失败"), "server_error"
            )
        self._send_json(200, result)

    def _api_agi_core_persona(self) -> None:
        result = self._agi_core_invoke("人格库读取", lambda service: service.persona())
        if result.get("ok") is False:
            raise _ApiError(503, "读取人格库失败", "server_error")
        self._send_json(200, result)

    def _api_agi_core_persona_proposals(self) -> None:
        result = self._agi_core_invoke(
            "人格提案读取", lambda service: service.persona_proposals()
        )
        if result.get("ok") is False:
            raise _ApiError(503, "读取人格提案失败", "server_error")
        self._send_json(200, result)

    def _api_agi_core_persona_promote(self) -> None:
        body = self._read_json_body()
        pid = str(body.get("id") or "").strip()
        if not pid:
            raise _ApiError(400, "缺少 id", "bad_request")
        result = self._agi_core_invoke(
            "人格提案批准", lambda service: service.promote_persona(pid)
        )
        if result.get("ok") is False:
            raise _ApiError(400, str(result.get("error") or "人格提案批准失败"), "bad_request")
        self._send_json(200, result)

    def _api_agi_core_persona_reject(self) -> None:
        body = self._read_json_body()
        pid = str(body.get("id") or "").strip()
        if not pid:
            raise _ApiError(400, "缺少 id", "bad_request")
        result = self._agi_core_invoke(
            "人格提案否决", lambda service: service.reject_persona(pid)
        )
        if result.get("ok") is False:
            raise _ApiError(400, str(result.get("error") or "人格提案否决失败"), "bad_request")
        self._send_json(200, result)

    def _api_agi_core_knowledge(self) -> None:
        query = self._query_first("q").strip()
        limit = self._query_int("limit", 20, 1, 50)
        result = self._agi_core_invoke(
            "知识库读取", lambda service: service.knowledge(query, limit)
        )
        if result.get("ok") is False:
            raise _ApiError(503, "读取知识库失败", "server_error")
        self._send_json(200, result)

    def _api_agi_core_knowledge_proposals(self) -> None:
        result = self._agi_core_invoke(
            "知识提案读取", lambda service: service.knowledge_proposals()
        )
        if result.get("ok") is False:
            raise _ApiError(503, "读取知识提案失败", "server_error")
        self._send_json(200, result)

    def _api_agi_core_knowledge_promote(self) -> None:
        body = self._read_json_body()
        kid = str(body.get("id") or "").strip()
        if not kid:
            raise _ApiError(400, "缺少 id", "bad_request")
        result = self._agi_core_invoke(
            "知识提案批准", lambda service: service.promote_knowledge(kid)
        )
        if result.get("ok") is False:
            raise _ApiError(400, str(result.get("error") or "知识提案批准失败"), "bad_request")
        self._send_json(200, result)

    def _api_agi_core_knowledge_reject(self) -> None:
        body = self._read_json_body()
        kid = str(body.get("id") or "").strip()
        if not kid:
            raise _ApiError(400, "缺少 id", "bad_request")
        result = self._agi_core_invoke(
            "知识提案否决", lambda service: service.reject_knowledge(kid)
        )
        if result.get("ok") is False:
            raise _ApiError(400, str(result.get("error") or "知识提案否决失败"), "bad_request")
        self._send_json(200, result)

    def _api_agi_core_acceptance(self) -> None:
        result = self._agi_core_invoke("验收读取", lambda service: service.acceptance())
        if result.get("ok") is False:
            raise _ApiError(503, "读取验收仪表盘失败", "server_error")
        self._send_json(200, result)

    def _api_agi_core_acceptance_demo(self) -> None:
        body = self._read_json_body()
        try:
            limit = int(body.get("limit") or 5)
        except (TypeError, ValueError):
            limit = 5
        result = self._agi_core_invoke(
            "验收副本演练", lambda service: service.acceptance_demo(limit)
        )
        if result.get("ok") is False:
            raise _ApiError(400, str(result.get("error") or "验收演练失败"), "bad_request")
        self._send_json(200, result)

    def _api_agi_core_consent(self) -> None:
        """GET 查授权状态；POST 授权/撤销（同一路径，按 method 分派）。"""
        if self._method in ("GET", "HEAD"):
            result = self._agi_core_invoke(
                "授权状态读取", lambda service: service.consent_status()
            )
            if result.get("ok") is False:
                raise _ApiError(
                    503, str(result.get("error") or "读取 AGI core 授权状态失败"),
                    "server_error",
                )
            self._send_json(200, result)
            return
        if self._method != "POST":
            raise _ApiError(
                405, "该接口只支持 GET/POST 请求", "bad_request",
                headers=[("Allow", "GET, HEAD, POST")],
            )
        body = self._read_json_body()
        action = str(body.get("action") or "").strip().lower()
        if action not in ("grant", "revoke"):
            raise _ApiError(400, "action 只支持 grant/revoke", "bad_request")
        scope = str(body.get("scope") or "").strip() or "chat"
        if action == "grant":
            result = self._agi_core_invoke(
                "授权", lambda service: service.grant_consent(scope)
            )
        else:
            result = self._agi_core_invoke(
                "撤销授权", lambda service: service.revoke_consent(scope)
            )
        if result.get("ok") is False:
            raise _ApiError(
                400, str(result.get("error") or "AGI core 授权操作失败"), "bad_request"
            )
        self._send_json(200, result)

    def _serve_static(self, rel_path: str) -> None:
        self._require_get()
        if not rel_path:
            raise _ApiError(404, "文件不存在", "not_found")
        decoded = urllib.parse.unquote(rel_path)
        normalized = decoded.replace("\\", "/")
        if "\x00" in normalized:
            raise _ApiError(404, "文件不存在", "not_found")
        segments = normalized.split("/")
        if any(segment in ("..", ".") for segment in segments):
            raise _ApiError(403, "拒绝访问：非法路径", "unauthorized")
        if normalized.startswith("/"):
            raise _ApiError(403, "拒绝访问：非法路径", "unauthorized")
        try:
            candidate = (STATIC_DIR / normalized).resolve()
        except (OSError, ValueError):
            raise _ApiError(404, "文件不存在", "not_found") from None
        try:
            candidate.relative_to(STATIC_DIR_RESOLVED)
        except ValueError:
            raise _ApiError(403, "拒绝访问：非法路径", "unauthorized") from None
        if not candidate.is_file():
            raise _ApiError(404, "文件不存在", "not_found")
        if candidate.name.lower() in ("index.html", "index.htm") and candidate.parent == STATIC_DIR_RESOLVED:
            self._serve_index()
            return
        suffix = candidate.suffix.lower()
        if suffix not in ALLOWED_STATIC_SUFFIXES:
            raise _ApiError(404, "文件不存在", "not_found")
        try:
            body = candidate.read_bytes()
        except OSError as exc:
            raise _ApiError(500, f"读取静态文件失败：{exc}", "server_error") from None
        self._send_bytes(
            200,
            body,
            CONTENT_TYPES.get(suffix, "application/octet-stream"),
            cache_control="no-cache",
            extra_headers=[("Referrer-Policy", "no-referrer")],
        )

    # ---- 媒体文件 ----
    def _serve_media(self, path: str) -> None:
        self._require_get()
        self._check_token()
        rel_path = path[len("/media/"):]
        decoded = urllib.parse.unquote(rel_path)
        normalized = decoded.replace("\\", "/")
        if not normalized or "\x00" in normalized:
            raise _ApiError(404, "文件不存在", "not_found")
        if normalized.startswith("/"):
            raise _ApiError(403, "拒绝访问：非法媒体路径", "unauthorized")
        segments = [segment for segment in normalized.split("/") if segment not in ("", ".")]
        if not segments:
            raise _ApiError(404, "文件不存在", "not_found")
        if any(segment == ".." for segment in segments):
            raise _ApiError(403, "拒绝访问：非法媒体路径", "unauthorized")
        if ":" in segments[0]:
            # Windows 盘符 / NTFS 数据流写法，一律拒绝
            raise _ApiError(403, "拒绝访问：非法媒体路径", "unauthorized")
        relative = "/".join(segments)
        try:
            candidate = (MEDIA_DIR / relative).resolve()
        except (OSError, ValueError):
            raise _ApiError(404, "文件不存在", "not_found") from None
        try:
            candidate.relative_to(MEDIA_DIR_RESOLVED)
        except ValueError:
            raise _ApiError(403, "拒绝访问：非法媒体路径", "unauthorized") from None
        if not candidate.is_file():
            raise _ApiError(404, "文件不存在", "not_found")
        headers = [("Cache-Control", "private, max-age=3600")]
        if "download" in self._query:
            headers.append(("Content-Disposition", _content_disposition(candidate.name)))
        self._send_file(candidate, _media_content_type(candidate.suffix), headers)

    # ---- API 分发 ----
    def _handle_api(self, path: str) -> None:
        # 朋友圈内存视频：/api/moments/video/<moment_id>/<index>（路径参数，需在路由表之前特判）
        if path.startswith("/api/moments/video/"):
            self._require_get()
            self._check_token()
            self._api_moments_video(path)
            return
        # 朋友圈内存媒体：/api/moments/media/<key>（key 是单段路径，需在路由表之前特判）
        if path.startswith("/api/moments/media/"):
            self._require_get()
            self._check_token()
            self._api_moments_media(path)
            return
        routes: dict[str, tuple[str, Any]] = {
            "/api/health": ("GET", self._api_health),
            "/api/bootstrap": ("GET", self._api_bootstrap),
            "/api/events": ("SSE", self._serve_events),
            "/api/monitored": ("POST", self._api_monitored),
            "/api/friends/sync": ("POST", self._api_friends_sync),
            "/api/backend/start": ("POST", self._api_backend_start),
            "/api/backend/stop": ("POST", self._api_backend_stop),
            "/api/llm/config": ("ANY", self._api_llm_config),
            "/api/llm/test": ("POST", self._api_llm_test),
            "/api/llm/analyze": ("POST", self._api_llm_analyze),
            "/api/llm/cancel": ("POST", self._api_llm_cancel),
            "/api/moments": ("GET", self._api_moments),
            "/api/moments/sync": ("POST", self._api_moments_sync),
            "/api/moments/clear": ("POST", self._api_moments_clear),
            "/api/moments/auto": ("POST", self._api_moments_auto),
            "/api/history/load": ("POST", self._api_history_load),
            "/api/messages/clear": ("POST", self._api_messages_clear),
            "/api/groups": ("GET", self._api_groups),
            "/api/contacts": ("GET", self._api_contacts),
            "/api/contacts/refresh": ("POST", self._api_contacts_refresh),
            "/api/contacts/clear": ("POST", self._api_contacts_clear),
            "/api/video": ("GET", self._api_video),
            "/api/video/auto": ("ANY", self._api_video_auto),
            "/api/agi/status": ("GET", self._api_agi_status),
            "/api/agi/drafts": ("GET", self._api_agi_drafts),
            "/api/agi/generate": ("POST", self._api_agi_generate),
            "/api/agi/core/status": ("GET", self._api_agi_core_status),
            "/api/agi/core/drafts": ("GET", self._api_agi_core_drafts),
            "/api/agi/core/cycle": ("POST", self._api_agi_core_cycle),
            "/api/agi/core/feedback": ("POST", self._api_agi_core_feedback),
            "/api/agi/core/memories": ("GET", self._api_agi_core_memories),
            "/api/agi/core/memories/approve": ("POST", self._api_agi_core_memory_approve),
            "/api/agi/core/memories/reject": ("POST", self._api_agi_core_memory_reject),
            "/api/agi/core/profile": ("GET", self._api_agi_core_profile),
            "/api/agi/core/persona": ("GET", self._api_agi_core_persona),
            "/api/agi/core/persona/proposals": ("GET", self._api_agi_core_persona_proposals),
            "/api/agi/core/persona/promote": ("POST", self._api_agi_core_persona_promote),
            "/api/agi/core/persona/reject": ("POST", self._api_agi_core_persona_reject),
            "/api/agi/core/knowledge": ("GET", self._api_agi_core_knowledge),
            "/api/agi/core/knowledge/proposals": ("GET", self._api_agi_core_knowledge_proposals),
            "/api/agi/core/knowledge/promote": ("POST", self._api_agi_core_knowledge_promote),
            "/api/agi/core/knowledge/reject": ("POST", self._api_agi_core_knowledge_reject),
            "/api/agi/core/consent": ("ANY", self._api_agi_core_consent),
            "/api/agi/core/acceptance": ("GET", self._api_agi_core_acceptance),
            "/api/agi/core/acceptance/demo": ("POST", self._api_agi_core_acceptance_demo),
        }
        route = routes.get(path)
        if route is None:
            raise _ApiError(404, "接口不存在", "not_found")
        method_kind, handler = route
        if path == "/api/health":
            # 契约 4.1：健康检查不校验令牌，方便脚本探活
            self._require_get()
            handler()
            return
        if path == "/api/llm/config":
            self._check_token()
            handler()  # GET 与 POST 语义不同，由处理函数内部分派
            return
        if path == "/api/video/auto":
            self._check_token()
            handler()  # GET 查状态 / POST 开关，由处理函数内部分派
            return
        if path == "/api/agi/core/consent":
            self._check_token()
            handler()  # GET 查授权 / POST grant/revoke，由处理函数内部分派
            return
        if method_kind == "GET":
            self._require_get()
        elif method_kind == "POST":
            self._require_post()
        elif method_kind == "SSE":
            if self._method != "GET":
                raise _ApiError(405, "实时事件流只支持 GET", "bad_request",
                                headers=[("Allow", "GET")])
        self._check_token()
        handler()

    def _require_get(self) -> None:
        if self._method not in ("GET", "HEAD"):
            raise _ApiError(405, "该接口只支持 GET 请求", "bad_request",
                            headers=[("Allow", "GET, HEAD")])

    def _require_post(self) -> None:
        if self._method != "POST":
            raise _ApiError(405, "该接口只支持 POST 请求", "bad_request",
                            headers=[("Allow", "POST")])

    # ---- API 实现：基础 ----
    def _api_health(self) -> None:
        # /api/health 不校验令牌（契约 4.1），方便浏览器/脚本探活
        started_at = float(getattr(self.server, "started_at", time.time()))
        self._send_json(
            200,
            {
                "ok": True,
                "version": self.context.version,
                "mode": self.context.mode,
                "uptime_s": round(max(0.0, time.time() - started_at), 1),
            },
        )

    def _llm_public(self) -> dict:
        try:
            config = self.context.llm.public_config()
            return config if isinstance(config, dict) else {}
        except Exception as exc:  # noqa: BLE001 - 配置读取失败也不该让首屏挂掉
            return {
                "configured": False,
                "model": "",
                "model_label": "",
                "base_url": "",
                "api_key_masked": "",
                "thinking": True,
                "attach_images": True,
                "busy": False,
                "models": [],
                "error": f"读取模型配置失败：{exc}",
            }

    def _moments_status(self) -> dict:
        """取朋友圈状态，并补齐字段（moments 为 None 时给可读的不可用原因）。

        V2 起朋友圈数据只在内存，status 额外带 ``limit``（本次同步条数 5/10/20）
        与 ``count``（当前内存条数），这里统一做兜底与类型归一。
        """
        empty = {
            "available": False,
            "reason": "朋友圈模块未启用",
            "syncing": False,
            "limit": DEFAULT_MOMENTS_SYNC_LIMIT,
            "count": 0,
            "synced_at": None,
            "account": {},
            "error": None,
            "python": "",
            "moments_dir": "",
        }
        moments = getattr(self.context, "moments", None)
        if moments is None:
            return empty
        try:
            status = moments.status()
        except Exception as exc:  # noqa: BLE001 - 朋友圈异常不能影响聊天接口
            empty["reason"] = f"读取朋友圈状态失败：{exc}"
            empty["error"] = str(exc)
            return empty
        if not isinstance(status, dict):
            return empty
        merged = dict(empty)
        merged.update(status)
        merged["available"] = bool(merged.get("available"))
        merged["syncing"] = bool(merged.get("syncing"))
        try:
            merged["count"] = int(merged.get("count") or 0)
        except (TypeError, ValueError):
            merged["count"] = 0
        try:
            limit = int(merged.get("limit") or DEFAULT_MOMENTS_SYNC_LIMIT)
        except (TypeError, ValueError):
            limit = DEFAULT_MOMENTS_SYNC_LIMIT
        merged["limit"] = limit if limit in MOMENTS_SYNC_LIMITS else DEFAULT_MOMENTS_SYNC_LIMIT
        if not isinstance(merged.get("account"), dict):
            merged["account"] = {}
        return merged

    def _api_bootstrap(self) -> None:
        try:
            snapshot = self.context.bridge.snapshot()
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(500, f"读取数据快照失败：{exc}", "server_error") from None
        if not isinstance(snapshot, dict):
            snapshot = {}
        status = self._moments_status()
        self._send_json(
            200,
            {
                "server": {
                    "version": self.context.version,
                    "mode": self.context.mode,
                    "started_at": float(getattr(self.server, "started_at", time.time())),
                    "now": time.time(),
                    "host": self.context.host,
                    "port": self.context.port,
                    "backend_note": self.context.backend_note,
                },
                "state": snapshot.get("state") if isinstance(snapshot.get("state"), dict) else {},
                "friends": snapshot.get("friends") or [],
                "messages": snapshot.get("messages") or [],
                "logs": snapshot.get("logs") or [],
                "llm": self._llm_public(),
                "moments": {
                    "status": status,
                    "count": int(status.get("count") or 0),
                    "synced_at": status.get("synced_at"),
                    "account": status.get("account") or {},
                },
                # V2：历史聊天同步能力标识（web_app 传了 HistoryService 才可用）
                "history": {
                    "available": getattr(self.context, "history", None) is not None,
                },
                # V3：群聊列表能力标识（web_app 传了 GroupsService 才可用）
                "groups": {
                    "available": getattr(self.context, "groups", None) is not None,
                },
                # V4：联系人列表（含"有历史记录"过滤 + 拼音首字母）能力标识
                "contacts": {
                    "available": getattr(self.context, "contacts", None) is not None,
                },
            },
        )

    # ---- API 实现：监听控制 ----
    def _api_monitored(self) -> None:
        payload = self._read_json_body()
        if "names" not in payload:
            raise _ApiError(400, "缺少 names 字段", "bad_request")
        raw_names = payload.get("names")
        if raw_names is None:
            raw_names = []
        if not isinstance(raw_names, list):
            raise _ApiError(400, "names 必须是好友名字符串数组", "bad_request")
        names: list[str] = []
        for item in raw_names:
            name = str(item).strip()
            if name and name not in names:
                names.append(name)
        try:
            result = self.context.bridge.set_monitored(names)
        except ValueError as exc:
            raise _ApiError(400, f"设置监听失败：{exc}", "bad_request") from None
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(500, f"设置监听失败：{exc}", "server_error") from None
        monitoring = result.get("monitoring") if isinstance(result, dict) else None
        if not isinstance(monitoring, list):
            monitoring = names
        self._send_json(200, {"ok": True, "monitoring": [str(name) for name in monitoring]})

    def _api_friends_sync(self) -> None:
        self._read_json_body()
        try:
            result = self.context.bridge.refresh_friends()
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(500, f"重新同步好友失败：{exc}", "server_error") from None
        if isinstance(result, dict) and result.get("ok") is False:
            raise _ApiError(500, str(result.get("error") or "重新同步好友失败"), "server_error")
        payload: dict = {"ok": True}
        if isinstance(result, dict) and "total" in result:
            payload["total"] = result["total"]
        self._send_json(200, payload)

    # ---- API 实现：群聊（V3，只读内存缓存） ----
    def _api_groups(self) -> None:
        """返回当前账号的群聊列表；失败只影响本接口，不影响好友/聊天。"""
        groups = getattr(self.context, "groups", None)
        if groups is None:
            raise _ApiError(
                503, "群聊列表不可用：未启用 webui/groups.py", "server_error"
            )
        refresh = self._query_flag("refresh")
        try:
            result = groups.list(refresh=refresh)
        except Exception as exc:  # noqa: BLE001 - 群聊读取失败给可读错误，不影响其它接口
            raise _ApiError(503, f"加载群聊失败：{exc}", "server_error") from None
        if not isinstance(result, dict) or result.get("ok") is False:
            error = "加载群聊失败"
            if isinstance(result, dict) and result.get("error"):
                error = str(result.get("error"))
            raise _ApiError(503, error, "server_error")
        raw_groups = result.get("groups")
        items: list[dict] = []
        if isinstance(raw_groups, list):
            for entry in raw_groups:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "").strip()
                if not name:
                    continue
                items.append(
                    {
                        "name": name,
                        "username": str(entry.get("username") or ""),
                        "kind": "group",
                        "member_hint": str(entry.get("member_hint") or ""),
                    }
                )
        self._send_json(
            200,
            {
                "ok": True,
                "count": len(items),
                "groups": items,
                "cached": bool(result.get("cached")),
            },
        )

    # ---- API 实现：联系人（V4，含"有历史记录"过滤 + 拼音首字母） ----
    def _api_contacts(self) -> None:
        """返回好友/群聊列表（默认只含有历史聊天记录的联系人）。

        结构由 ``ContactsService.list()`` 决定，这里只做参数解析、字段兜底与错误转换；
        失败只影响本接口，不影响好友/聊天/朋友圈。
        """
        contacts = getattr(self.context, "contacts", None)
        if contacts is None:
            raise _ApiError(
                503, "联系人列表不可用：未启用 webui/contacts.py", "server_error"
            )
        refresh = self._query_flag("refresh")
        include_all = self._query_flag("all")
        try:
            result = contacts.list(refresh=refresh, include_all=include_all)
        except TypeError:
            # 兼容旧版 ContactsService（只接受 refresh 参数）
            try:
                result = contacts.list(refresh=refresh)
            except Exception as exc:  # noqa: BLE001
                raise _ApiError(503, f"读取联系人失败：{exc}", "server_error") from None
        except Exception as exc:  # noqa: BLE001 - DB/密钥异常给可读错误
            raise _ApiError(503, f"读取联系人失败：{exc}", "server_error") from None
        if not isinstance(result, dict) or result.get("ok") is False:
            error = "读取联系人失败"
            if isinstance(result, dict) and result.get("error"):
                error = str(result.get("error"))
            raise _ApiError(503, error, "server_error")
        # 原样返回 ContactsService 的结构，只补缺失字段，避免前端拿到 undefined
        payload = dict(result)
        payload["ok"] = True
        payload["include_all"] = bool(payload.get("include_all", include_all))
        for key in ("friends", "groups", "history_names"):
            if not isinstance(payload.get(key), list):
                payload[key] = []
        if not isinstance(payload.get("initials"), dict):
            payload["initials"] = {}
        payload["friend_count"] = int(payload.get("friend_count") or len(payload["friends"]))
        payload["group_count"] = int(payload.get("group_count") or len(payload["groups"]))
        payload["count"] = int(
            payload.get("count") or (payload["friend_count"] + payload["group_count"])
        )
        payload["cached"] = bool(payload.get("cached"))
        self._send_json(200, payload)

    # ---- API 实现：联系人刷新/清空（账号切换后清理旧账号数据） ----
    def _update_backend_config(
        self,
        *,
        friends_cache: list[str] | None = None,
        image_wxid: str | None = None,
    ) -> None:
        """把好友缓存/账号标识写回 backend.config，并尽力落盘 config.json。

        为什么静默失败：这两个接口是"清理旧账号残留"的辅助操作，写配置失败不应
        让页面报错。空配置（测试/演示后端）只改内存、不落盘，避免把项目
        ``config.json`` 覆盖成空文件。
        """
        bridge = self.context.bridge
        backend = None
        try:
            backend = bridge.backend()
        except Exception:  # noqa: BLE001 - WebBridge.backend 可能是属性而不是方法
            backend = getattr(bridge, "backend", None)
        config = getattr(backend, "config", None)
        if not isinstance(config, dict):
            return
        config_is_empty = not config
        try:
            if friends_cache is not None:
                config["friends_cache"] = [str(name) for name in friends_cache]
            if image_wxid:
                config["image_wxid"] = str(image_wxid)
            if config_is_empty:
                return
            from wx_core import config as cfg_mod

            cfg_mod.save(config)
        except Exception:  # noqa: BLE001 - 写配置失败不影响接口结果
            pass

    def _api_contacts_refresh(self) -> None:
        """强制重读联系人，替换 bridge 好友列表；账号切换时清掉旧账号内存数据。"""
        self._read_json_body()
        contacts = getattr(self.context, "contacts", None)
        if contacts is None:
            raise _ApiError(
                503, "联系人列表不可用：未启用 webui/contacts.py", "server_error"
            )
        try:
            payload = contacts.list(refresh=True)
        except Exception as exc:  # noqa: BLE001 - DB/密钥异常给可读错误
            raise _ApiError(503, f"刷新联系人失败：{exc}", "server_error") from None
        if not isinstance(payload, dict) or payload.get("ok") is False:
            error = "刷新联系人失败"
            if isinstance(payload, dict) and payload.get("error"):
                error = str(payload["error"])
            raise _ApiError(503, error, "server_error")
        raw_friends = payload.get("friends")
        friends = [item for item in raw_friends if isinstance(item, dict)] if isinstance(
            raw_friends, list
        ) else []
        bridge_friends: list[dict] = []
        for item in friends:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            bridge_friends.append(
                {
                    "name": name,
                    "wxid": str(item.get("username") or ""),
                    "letter": str(item.get("initial") or ""),
                }
            )
        bridge = self.context.bridge
        try:
            replaced = int(bridge.replace_friends(bridge_friends) or 0)
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(500, f"更新好友列表失败：{exc}", "server_error") from None

        account_changed = bool(payload.get("account_changed"))
        account_wxid = str(payload.get("account_wxid") or "")
        monitoring: list[str] = []
        if account_changed:
            # 账号切换：旧账号的消息/监听/数据库桥全部作废，避免页面串号
            cleared = 0
            try:
                cleared = int(bridge.clear_messages() or 0)
            except Exception:  # noqa: BLE001
                cleared = 0
            try:
                bridge.broadcast({"type": "messages_cleared", "data": {"cleared": cleared}})
            except Exception:  # noqa: BLE001
                pass
            try:
                result = bridge.set_monitored([])
                if isinstance(result, dict) and isinstance(result.get("monitoring"), list):
                    monitoring = [str(name) for name in result["monitoring"]]
            except Exception:  # noqa: BLE001 - 清监听失败不应让刷新接口失败
                pass
            try:
                bridge.reset_backend_db(account_wxid)
            except Exception:  # noqa: BLE001 - 由后端下次轮询自行重建
                pass
            self._update_backend_config(
                friends_cache=[item["name"] for item in bridge_friends],
                image_wxid=account_wxid,
            )
        self._send_json(
            200,
            {
                "ok": True,
                "contacts": payload,
                "replaced_friends": replaced,
                "account_changed": account_changed,
                "monitoring": monitoring,
            },
        )

    def _api_contacts_clear(self) -> None:
        """清空联系人缓存/好友列表（切号前手动清理旧账号残留）。"""
        self._read_json_body()
        contacts = getattr(self.context, "contacts", None)
        if contacts is None:
            raise _ApiError(
                503, "联系人列表不可用：未启用 webui/contacts.py", "server_error"
            )
        clear_cache = getattr(contacts, "clear_cache", None)
        try:
            if callable(clear_cache):
                clear_cache()
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(503, f"清空联系人缓存失败：{exc}", "server_error") from None
        groups = getattr(self.context, "groups", None)
        groups_clear = getattr(groups, "clear_cache", None) if groups is not None else None
        if callable(groups_clear):
            try:
                groups_clear()
            except Exception:  # noqa: BLE001 - 群聊缓存清空失败不影响主流程
                pass
        bridge = self.context.bridge
        try:
            cleared = int(bridge.clear_friends() or 0)
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(500, f"清空好友列表失败：{exc}", "server_error") from None
        self._update_backend_config(friends_cache=[])
        self._send_json(200, {"ok": True, "cleared": cleared})

    # ---- API 实现：微信视频（只读本机缓存、支持 Range 流式播放） ----
    def _api_video(self) -> None:
        """按 (好友显示名, db local_id) 返回本机缓存的原视频，支持 Range/HEAD。"""
        friend = self._query_first("friend").strip()
        raw_local_id = self._query_first("local_id").strip()
        if not friend or not raw_local_id:
            raise _ApiError(400, "缺少 friend 或 local_id 参数", "bad_request")
        try:
            local_id = int(raw_local_id)
        except (TypeError, ValueError):
            raise _ApiError(400, "local_id 必须是整数", "bad_request") from None
        if local_id <= 0:
            raise _ApiError(400, "local_id 必须是正整数", "bad_request")
        videos = getattr(self.context, "videos", None)
        if videos is None:
            raise _ApiError(404, "视频服务不可用：未启用 webui/videos.py", "not_found")
        try:
            info = videos.resolve(friend, local_id)
        except Exception as exc:  # noqa: BLE001 - 解析失败给可读 500
            raise _ApiError(500, f"解析视频失败：{exc}", "server_error") from None
        if not isinstance(info, dict):
            raise _ApiError(
                404,
                "视频不存在或本机未缓存（只有在电脑微信播放/接收过的视频才会缓存）",
                "not_found",
            )
        # 只按 service 返回的 Path 读取，不接受请求里的任意路径（防穿越）
        path_value = info.get("path")
        path = path_value if isinstance(path_value, Path) else (
            Path(str(path_value)) if path_value else None
        )
        if path is None:
            raise _ApiError(404, "视频文件不存在", "not_found")
        try:
            if not path.is_file():
                raise _ApiError(404, "视频文件不存在", "not_found")
            total = int(path.stat().st_size)
        except _ApiError:
            raise
        except OSError as exc:  # noqa: BLE001 - 文件被删/被锁
            raise _ApiError(404, f"读取视频文件失败：{exc}", "not_found") from None
        mime = _safe_media_mime(info.get("mime"), "application/octet-stream")
        range_header = (self.headers.get("Range") or "").strip()
        if range_header:
            parsed = _parse_byte_range(range_header, total)
            if parsed is None:
                raise _ApiError(
                    416,
                    "请求的字节范围无效",
                    "bad_request",
                    headers=[("Content-Range", f"bytes */{total}")],
                )
            start, end = parsed
            self._send_video_file(path, mime, start, end, total, status=206)
        else:
            self._send_video_file(path, mime, 0, max(0, total - 1), total, status=200)

    def _send_video_file(
        self,
        path: Path,
        mime: str,
        start: int,
        end: int,
        total: int,
        *,
        status: int,
    ) -> None:
        """流式返回视频文件（206 时带 Content-Range；HEAD 只发头）。"""
        length = 0 if total <= 0 else max(0, end - start + 1)
        if self._responded:
            return
        self._responded = True
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self._method == "HEAD" or length <= 0:
            return
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except OSError:
            # 播放中文件被删/被锁：头已发出，只能断开让浏览器重试
            self.close_connection = True

    def _set_moments_listening(self, listening: bool) -> bool:
        """把「实时监听」开关联动给朋友圈自动刷新；缺失/旧版/失败都静默跳过。

        MomentsService 只在实时监听期间做朋友圈自动检查/刷新；这里不改变接口结果，
        返回是否真的调用成功，用于可选地回传 ``moments_listening``。
        """
        moments = getattr(self.context, "moments", None)
        if moments is None:
            return False
        set_listening = getattr(moments, "set_listening", None)
        if not callable(set_listening):
            return False
        try:
            set_listening(bool(listening))
            return True
        except Exception:  # noqa: BLE001 - 朋友圈联动失败不影响监听接口本身
            return False

    # ---- API 实现：实验性聊天视频自动缓存开关 ----
    def _api_video_auto(self) -> None:
        """GET 查实验性视频缓存状态；POST 开关（默认关闭，UI 自动化有风控风险）。"""
        video_auto = getattr(self.context, "video_auto", None)
        if self._method in ("GET", "HEAD"):
            if video_auto is None:
                raise _ApiError(
                    503, "实验性视频缓存未启用（未注入 webui/video_auto.py）", "server_error"
                )
            status_fn = getattr(video_auto, "status", None)
            if not callable(status_fn):
                raise _ApiError(
                    503, "当前视频缓存模块不支持状态查询（请更新 webui/video_auto.py）",
                    "server_error",
                )
            try:
                status = status_fn()
            except Exception as exc:  # noqa: BLE001
                raise _ApiError(503, f"读取视频缓存状态失败：{exc}", "server_error") from None
            self._send_json(200, {"ok": True, "status": status if isinstance(status, dict) else {}})
            return
        if self._method != "POST":
            raise _ApiError(
                405, "该接口只支持 GET/POST 请求", "bad_request",
                headers=[("Allow", "GET, HEAD, POST")],
            )
        if video_auto is None:
            raise _ApiError(
                503, "实验性视频缓存未启用（未注入 webui/video_auto.py）", "server_error"
            )
        payload = self._read_json_body()
        if "enabled" not in payload:
            raise _ApiError(400, "缺少 enabled 字段", "bad_request")
        enabled = _payload_bool(payload.get("enabled"), False)
        set_enabled = getattr(video_auto, "set_enabled", None)
        if not callable(set_enabled):
            raise _ApiError(
                503, "当前视频缓存模块不支持开关（请更新 webui/video_auto.py）",
                "server_error",
            )
        try:
            result = set_enabled(enabled)
        except Exception as exc:  # noqa: BLE001 - UI 自动化启动失败给可读错误
            raise _ApiError(503, f"设置实验性视频缓存失败：{exc}", "server_error") from None
        status_fn = getattr(video_auto, "status", None)
        status: dict = result if isinstance(result, dict) else {}
        if callable(status_fn):
            try:
                current = status_fn()
                if isinstance(current, dict):
                    status = current
            except Exception:  # noqa: BLE001 - 状态读取失败不影响开关结果
                pass
        self._send_json(
            200,
            {
                "ok": True,
                "enabled": bool(status.get("enabled", enabled)) if status else enabled,
                "status": status,
            },
        )

    def _api_backend_start(self) -> None:
        self._read_json_body()
        try:
            result = self.context.bridge.start_backend()
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(500, f"启动监听失败：{exc}", "server_error") from None
        if isinstance(result, dict) and result.get("ok") is False:
            raise _ApiError(500, str(result.get("error") or "启动监听失败"), "server_error")
        running = True
        if isinstance(result, dict) and "running" in result:
            running = bool(result.get("running"))
        # 实时监听开启后朋友圈才做自动检查/刷新（每 30 分钟一次）
        moments_listening = self._set_moments_listening(True) if running else False
        self._send_json(
            200,
            {"ok": True, "running": running, "moments_listening": moments_listening},
        )

    def _api_backend_stop(self) -> None:
        self._read_json_body()
        try:
            result = self.context.bridge.stop_backend()
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(500, f"停止监听失败：{exc}", "server_error") from None
        if isinstance(result, dict) and result.get("ok") is False:
            raise _ApiError(500, str(result.get("error") or "停止监听失败"), "server_error")
        running = False
        if isinstance(result, dict) and "running" in result:
            running = bool(result.get("running"))
        # 结束监听后立即停止朋友圈自动检查/刷新
        self._set_moments_listening(False)
        self._send_json(
            200,
            {"ok": True, "running": running, "moments_listening": False},
        )

    # ---- API 实现：LLM ----
    def _api_llm_config(self) -> None:
        if self._method == "GET":
            # 契约：GET 与 bootstrap.llm 相同（直接返回 llm 对象本身）
            self._send_json(200, self._llm_public())
            return
        if self._method != "POST":
            raise _ApiError(405, "该接口只支持 GET/POST 请求", "bad_request",
                            headers=[("Allow", "GET, POST")])
        patch = self._read_json_body()
        try:
            config = self.context.llm.save_config(patch)
        except LLMError as exc:
            raise _ApiError(_status_for_code(exc.code), exc.message, exc.code) from None
        except ValueError as exc:
            raise _ApiError(400, str(exc), "bad_request") from None
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(500, f"保存配置失败：{exc}", "server_error") from None
        self._send_json(200, {"ok": True, "config": config})

    def _api_llm_test(self) -> None:
        self._read_json_body()
        try:
            result = self.context.llm.test_connection()
        except Exception as exc:  # noqa: BLE001 - 测试连接失败也要给出可读原因
            result = {"ok": False, "latency_ms": None, "error": f"测试连接失败：{exc}", "models": []}
        if not isinstance(result, dict):
            result = {}
        self._send_json(
            200,
            {
                # 请求本身成功；连接是否成功看 error 字段（契约 4.1 示例）
                "ok": True,
                "latency_ms": result.get("latency_ms"),
                "error": result.get("error"),
                "models": result.get("models") or [],
            },
        )

    def _api_llm_analyze(self) -> None:
        payload = self._read_json_body()
        history = payload.get("history")
        options = {
            "thinking": payload.get("thinking"),
            "attach_images": payload.get("attach_images"),
            "limit": payload.get("limit"),
        }
        try:
            result = self.context.llm.start_analysis(
                scope=payload.get("scope"),
                scope_label=str(payload.get("scope_label") or ""),
                question=str(payload.get("question") or ""),
                history=history if isinstance(history, list) else [],
                options=options,
            )
        except LLMError as exc:
            raise _ApiError(_status_for_code(exc.code), exc.message, exc.code) from None
        except ValueError as exc:
            raise _ApiError(400, str(exc), "bad_request") from None
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(500, f"启动分析失败：{exc}", "server_error") from None
        job_id = str((result or {}).get("job_id") or "")
        self._send_json(200, {"ok": True, "job_id": job_id})

    def _api_llm_cancel(self) -> None:
        payload = self._read_json_body()
        job_id = str(payload.get("job_id") or "")
        try:
            cancelled = bool(self.context.llm.cancel(job_id))
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(500, f"取消分析失败：{exc}", "server_error") from None
        self._send_json(200, {"ok": True, "cancelled": cancelled})

    # ---- API 实现：朋友圈（V2：只在内存、限量、可清空） ----
    def _api_moments(self) -> None:
        status = self._moments_status()
        # 页面最多展示 30 条；?limit= 可选，超出按 30 处理
        limit = self._query_int("limit", MAX_MOMENTS_DISPLAY, 1, MAX_MOMENTS_DISPLAY)
        moments = getattr(self.context, "moments", None)
        items: list[dict] = []
        if moments is not None and status.get("available"):
            try:
                raw_items = moments.items(limit) or []
            except Exception as exc:  # noqa: BLE001 - 朋友圈读取失败不影响页面其他部分
                status = dict(status)
                status["error"] = str(exc)
                raw_items = []
            items = [item for item in raw_items if isinstance(item, dict)][:MAX_MOMENTS_DISPLAY]
        self._send_json(
            200,
            {"ok": True, "status": status, "items": items, "count": len(items)},
        )

    @staticmethod
    def _moments_limit_from_payload(payload: dict) -> int:
        """把 POST body 里的 limit 归一成 5/10/20（其它值按 10 处理，与 MomentsService 一致）。"""
        raw = payload.get("limit", DEFAULT_MOMENTS_SYNC_LIMIT)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = DEFAULT_MOMENTS_SYNC_LIMIT
        return value if value in MOMENTS_SYNC_LIMITS else DEFAULT_MOMENTS_SYNC_LIMIT

    def _api_moments_sync(self) -> None:
        payload = self._read_json_body()
        limit = self._moments_limit_from_payload(payload)
        moments = getattr(self.context, "moments", None)
        if moments is None:
            raise _ApiError(503, "朋友圈模块未启用，无法同步", "server_error")
        try:
            try:
                result = moments.sync_async(limit)
            except TypeError:
                # 兼容 v1 版 MomentsService（sync_async 不接受 limit 参数）
                result = moments.sync_async()
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(500, f"同步朋友圈失败：{exc}", "server_error") from None
        if not isinstance(result, dict):
            result = {}
        if result.get("ok") is False:
            raise _ApiError(
                500, str(result.get("error") or "同步朋友圈失败"), "server_error"
            )
        response: dict = {
            "ok": True,
            "syncing": bool(result.get("syncing", True)),
            "limit": limit,
        }
        if result.get("error"):
            response["error"] = str(result["error"])
        self._send_json(200, response)

    def _api_moments_clear(self) -> None:
        self._read_json_body()
        moments = getattr(self.context, "moments", None)
        if moments is None:
            raise _ApiError(503, "朋友圈模块未启用，无法清空", "server_error")
        clear_fn = getattr(moments, "clear", None)
        if not callable(clear_fn):
            raise _ApiError(
                503, "当前朋友圈模块不支持清空（请更新 webui/moments.py）", "server_error"
            )
        try:
            clear_fn()
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(500, f"清空朋友圈失败：{exc}", "server_error") from None
        status = self._moments_status()
        # 清空后广播一条 moments 事件，所有打开的页面同步回到空列表
        event_data = {
            "ok": True,
            "count": 0,
            "cleared": True,
            "limit": status.get("limit", DEFAULT_MOMENTS_SYNC_LIMIT),
            "synced_at": None,
            "account": status.get("account") or {},
            "error": None,
        }
        try:
            self.context.bridge.broadcast({"type": "moments", "data": event_data})
        except Exception:  # noqa: BLE001 - 广播失败不影响清空结果
            pass
        self._send_json(200, {"ok": True, "count": 0, "cleared": True})

    def _api_moments_auto(self) -> None:
        """开启/关闭朋友圈自动监听（只轮询本地 sns.db，不做微信 UI 操作）。"""
        payload = self._read_json_body()
        moments = getattr(self.context, "moments", None)
        if moments is None:
            raise _ApiError(503, "朋友圈模块未启用，无法设置自动监听", "server_error")
        if "enabled" not in payload:
            raise _ApiError(400, "缺少 enabled 字段", "bad_request")
        enabled = _payload_bool(payload.get("enabled"), False)
        interval = _normalize_auto_interval(payload.get("interval"))
        set_auto = getattr(moments, "set_auto", None)
        if not callable(set_auto):
            raise _ApiError(
                503, "当前朋友圈模块不支持自动监听（请更新 webui/moments.py）",
                "server_error",
            )
        try:
            result = set_auto(enabled, interval)
        except Exception as exc:  # noqa: BLE001 - 自动监听失败给可读错误，不影响其它接口
            raise _ApiError(503, f"设置朋友圈自动监听失败：{exc}", "server_error") from None
        # 以 service 返回（可能已做 10..300 归一）为准，同时回传最新 status
        response: dict = {"ok": True, "enabled": enabled, "interval": interval}
        if isinstance(result, dict):
            response["enabled"] = bool(result.get("enabled", enabled))
            try:
                response["interval"] = int(result.get("interval", interval))
            except (TypeError, ValueError):
                response["interval"] = interval
        try:
            status = moments.status()
        except Exception:  # noqa: BLE001 - 状态读取失败不影响设置结果
            status = None
        response["status"] = status if isinstance(status, dict) else {}
        self._send_json(200, response)

    def _api_moments_media(self, path: str) -> None:
        """返回朋友圈内存媒体字节（V2：不再落盘，清空/重启后 404）。"""
        moments = getattr(self.context, "moments", None)
        if moments is None:
            raise _ApiError(404, "媒体不存在（朋友圈模块未启用）", "not_found")
        key = urllib.parse.unquote(path[len("/api/moments/media/"):]).strip()
        if not key or len(key) > 256 or any(ch in key for ch in ("/", "\\", "\x00", "\r", "\n")):
            raise _ApiError(404, "媒体不存在", "not_found")
        media_fn = getattr(moments, "media", None)
        if not callable(media_fn):
            raise _ApiError(404, "媒体不存在（当前朋友圈模块不支持内存媒体）", "not_found")
        try:
            result = media_fn(key)
        except Exception:  # noqa: BLE001 - 媒体不存在/已清空统一 404，不要 500
            result = None
        data: bytes | None = None
        mime = "application/octet-stream"
        if isinstance(result, tuple) and len(result) >= 2:
            raw = result[0]
            if isinstance(raw, (bytes, bytearray)):
                data = bytes(raw)
                mime = _safe_media_mime(result[1])
        elif isinstance(result, (bytes, bytearray)):
            data = bytes(result)
        if not data:
            raise _ApiError(404, "媒体不存在或已清空", "not_found")
        self._send_bytes(200, data, mime, cache_control="no-store")

    def _api_moments_video(self, path: str) -> None:
        """返回朋友圈视频（服务端内存字节），支持 Range/HEAD；不落盘、不暴露路径。"""
        moments = getattr(self.context, "moments", None)
        if moments is None:
            raise _ApiError(503, "朋友圈模块未启用，无法播放视频", "server_error")
        # path = /api/moments/video/<moment_id>/<index>；moment_id 按原始路径段使用（不过度解码）
        rest = path[len("/api/moments/video/"):]
        moment_id, sep, index_text = rest.rpartition("/")
        moment_id = moment_id.strip()
        index_text = index_text.strip()
        if not sep or not moment_id or not index_text:
            raise _ApiError(400, "视频地址缺少朋友圈 id 或视频序号", "bad_request")
        try:
            index = int(index_text)
        except (TypeError, ValueError):
            raise _ApiError(400, "视频序号必须是非负整数", "bad_request") from None
        if index < 0:
            raise _ApiError(400, "视频序号必须是非负整数", "bad_request")
        video_fn = getattr(moments, "video", None)
        if not callable(video_fn):
            raise _ApiError(
                503, "当前朋友圈模块不支持视频播放（请更新 webui/moments.py）",
                "server_error",
            )
        try:
            result = video_fn(moment_id, index)
        except Exception as exc:  # noqa: BLE001 - 下载/解密异常给可读 500
            raise _ApiError(500, f"读取朋友圈视频失败：{exc}", "server_error") from None
        data: bytes | None = None
        mime = "video/mp4"
        if isinstance(result, tuple) and len(result) >= 2:
            raw = result[0]
            if isinstance(raw, (bytes, bytearray)):
                data = bytes(raw)
                mime = _safe_media_mime(result[1], "application/octet-stream")
        elif isinstance(result, (bytes, bytearray)):
            data = bytes(result)
        if not data:
            raise _ApiError(
                404, "视频未下载或解密失败（请先同步朋友圈后再试）", "not_found"
            )
        total = len(data)
        range_header = (self.headers.get("Range") or "").strip()
        if range_header:
            parsed = _parse_byte_range(range_header, total)
            if parsed is None:
                raise _ApiError(
                    416,
                    "请求的字节范围无效",
                    "bad_request",
                    headers=[("Content-Range", f"bytes */{total}"),
                             ("Accept-Ranges", "bytes")],
                )
            start, end = parsed
            self._send_bytes(
                206,
                data[start:end + 1],
                mime,
                cache_control="no-store",
                extra_headers=[("Accept-Ranges", "bytes"),
                               ("Content-Range", f"bytes {start}-{end}/{total}")],
            )
        else:
            self._send_bytes(
                200,
                data,
                mime,
                cache_control="no-store",
                extra_headers=[("Accept-Ranges", "bytes")],
            )

    # ---- API 实现：历史聊天（V2：只读本机缓存、入内存、可清空） ----
    def _api_history_load(self) -> None:
        payload = self._read_json_body()
        history = getattr(self.context, "history", None)
        if history is None:
            raise _ApiError(
                503, "历史聊天同步不可用：未启用 webui/history.py", "server_error"
            )
        friend = str(payload.get("friend") or "").strip()
        if not friend:
            raise _ApiError(400, "请先选择要同步历史记录的好友", "bad_request")
        raw_limit = payload.get("limit", 20)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            raise _ApiError(400, "limit 必须是 0/5/10/20/50/100", "bad_request") from None
        if limit not in HISTORY_LIMITS:
            raise _ApiError(400, "limit 只支持 0/5/10/20/50/100", "bad_request")
        try:
            result = history.load(friend, limit)
        except ValueError as exc:
            raise _ApiError(400, f"同步历史聊天失败：{exc}", "bad_request") from None
        except Exception as exc:  # noqa: BLE001 - 读库失败给可读中文
            raise _ApiError(500, f"同步历史聊天失败：{exc}", "server_error") from None
        if not isinstance(result, dict):
            raise _ApiError(500, "同步历史聊天失败：返回数据格式不正确", "server_error")
        if result.get("ok") is False:
            # HistoryService 用 code 区分原因；映射成统一的 HTTP 状态与错误码
            raw_code = str(result.get("code") or "")
            if raw_code == "bad_request":
                status, code = 400, "bad_request"
            elif raw_code == "not_found":
                status, code = 404, "not_found"
            elif raw_code == "history_error":
                status, code = 503, "server_error"
            else:
                status, code = 500, "server_error"
            raise _ApiError(
                status, str(result.get("error") or "同步历史聊天失败"), code
            )
        raw_items = result.get("items")
        items = (
            [item for item in raw_items if isinstance(item, dict)]
            if isinstance(raw_items, list)
            else []
        )
        add_messages = getattr(self.context.bridge, "add_messages", None)
        if items and callable(add_messages):
            try:
                add_messages(items)
            except Exception as exc:  # noqa: BLE001 - 历史已读到，但入内存失败要明确提示
                raise _ApiError(
                    500, f"历史聊天已读取，但写入内存失败：{exc}", "server_error"
                ) from None
        requested = result.get("requested")
        if requested is None:
            requested = "all" if limit == 0 else limit
        try:
            loaded = int(result.get("loaded") if result.get("loaded") is not None else len(items))
        except (TypeError, ValueError):
            loaded = len(items)
        try:
            available = int(
                result.get("available") if result.get("available") is not None else len(items)
            )
        except (TypeError, ValueError):
            available = len(items)
        self._send_json(
            200,
            {
                "ok": True,
                "friend": str(result.get("friend") or friend),
                "requested": requested,
                "loaded": loaded,
                "available": available,
                "notice": result.get("notice"),
                "items": items,
            },
        )

    def _api_messages_clear(self) -> None:
        self._read_json_body()
        clear_fn = getattr(self.context.bridge, "clear_messages", None)
        if not callable(clear_fn):
            raise _ApiError(
                503, "当前事件桥不支持清空消息（请更新 webui/bridge.py）", "server_error"
            )
        try:
            result = clear_fn()
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(500, f"清空聊天记录失败：{exc}", "server_error") from None
        cleared = 0
        if isinstance(result, dict):
            try:
                cleared = int(result.get("cleared") or 0)
            except (TypeError, ValueError):
                cleared = 0
        elif isinstance(result, (int, float)):
            cleared = int(result)
        # 广播清空事件，所有打开的页面同步清空消息区（计数/日志由 bridge 一起清）
        try:
            self.context.bridge.broadcast(
                {"type": "messages_cleared", "data": {"cleared": cleared}}
            )
        except Exception:  # noqa: BLE001
            pass
        self._send_json(200, {"ok": True, "cleared": cleared})

    # ---- SSE ----
    def _serve_events(self) -> None:
        bridge = self.context.bridge
        try:
            event_queue, sub_id = bridge.subscribe()
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(500, f"订阅实时事件失败：{exc}", "server_error") from None
        self._responded = True
        self.close_connection = True  # SSE 用「连接关闭」作为流结束，避免 keep-alive 混淆
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                self.connection.settimeout(SSE_WRITE_TIMEOUT)
            except OSError:
                pass
            self._write_sse(
                {
                    "type": "hello",
                    "data": {
                        "version": self.context.version,
                        "now": time.time(),
                        "mode": self.context.mode,
                    },
                }
            )
            while True:
                try:
                    event = event_queue.get(timeout=HEARTBEAT_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if isinstance(event, dict):
                    self._write_sse(event)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout,
                ValueError, OSError):
            # 浏览器关闭标签页 / 网络中断 / 写超时：正常清理即可，不打印堆栈
            pass
        finally:
            try:
                bridge.unsubscribe(sub_id)
            except Exception:  # noqa: BLE001
                pass
            self.close_connection = True

    def _write_sse(self, event: dict) -> None:
        """按契约格式写一条 SSE 事件：``event: <type>\\ndata: <json>\\n\\n``。"""
        event_type = str(event.get("type") or "message")
        # 事件名只允许安全字符，避免事件注入（data 是 JSON，天然安全）
        event_type = re.sub(r"[^A-Za-z0-9_.-]", "_", event_type) or "message"
        payload = json.dumps(event, ensure_ascii=False, default=str)
        chunk = f"event: {event_type}\ndata: {payload}\n\n".encode("utf-8")
        self.wfile.write(chunk)
        self.wfile.flush()


# ---------------------------------------------------------------------------
# 模块级工具
# ---------------------------------------------------------------------------
def _split_host_header(value: str) -> tuple[str, str | None]:
    """拆分 Host 头，返回 ``(hostname, port_text)``；非法格式返回 ``("", None)``。"""
    text = (value or "").strip().lower()
    if not text:
        return "", None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return "", None
        hostname = text[1:end]
        rest = text[end + 1:]
        if not rest:
            return hostname, None
        if not rest.startswith(":"):
            return "", None
        port_text = rest[1:]
        return (hostname, port_text) if port_text.isdigit() else ("", None)
    if ":" not in text:
        return text, None
    if text.count(":") > 1:
        # 裸 IPv6（无端口）：直接当主机名，是否允许由调用方判断
        return text, None
    hostname, _, port_text = text.rpartition(":")
    if not port_text.isdigit():
        return "", None
    return hostname, port_text


def _media_content_type(suffix: str) -> str:
    return CONTENT_TYPES.get(str(suffix or "").lower(), "application/octet-stream")


# 朋友圈自动监听间隔：与 MomentsService.set_auto 的归一规则保持一致（10..300 秒）
AUTO_INTERVAL_DEFAULT = 30
AUTO_INTERVAL_MIN = 10
AUTO_INTERVAL_MAX = 300


def _payload_bool(value: Any, default: bool = False) -> bool:
    """解析 JSON body 里的布尔值（bool/0/1/"true"/"false" 都接受）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    return default


def _normalize_auto_interval(value: Any) -> int:
    """自动监听间隔归一：缺省/非法 30，超出范围收敛到 10/300。"""
    if value is None or value == "":
        return AUTO_INTERVAL_DEFAULT
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return AUTO_INTERVAL_DEFAULT
    return max(AUTO_INTERVAL_MIN, min(AUTO_INTERVAL_MAX, interval))


def _parse_byte_range(value: str, total: int) -> tuple[int, int] | None:
    """解析单段 ``Range: bytes=...``，返回闭区间 ``(start, end)``。

    支持 ``bytes=start-end``、``bytes=start-``、``bytes=-suffix`` 三种写法；
    多段、越界、格式错误返回 ``None``（调用方回 416）。视频播放器只用单段 Range。
    """
    text = str(value or "").strip()
    if not text.lower().startswith("bytes=") or total <= 0:
        return None
    spec = text[len("bytes="):].strip()
    if not spec or "," in spec or "-" not in spec:
        return None
    start_text, _, end_text = spec.partition("-")
    start_text, end_text = start_text.strip(), end_text.strip()
    if start_text:
        try:
            start = int(start_text)
        except (TypeError, ValueError):
            return None
        if start < 0 or start >= total:
            return None
        if end_text:
            try:
                end = int(end_text)
            except (TypeError, ValueError):
                return None
            if end < start:
                return None
            return start, min(end, total - 1)
        return start, total - 1
    # bytes=-N：最后 N 个字节
    try:
        suffix = int(end_text)
    except (TypeError, ValueError):
        return None
    if suffix <= 0:
        return None
    if suffix >= total:
        return 0, total - 1
    return total - suffix, total - 1


_MIME_RE = re.compile(r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}")


def _safe_media_mime(value: Any, default: str = "application/octet-stream") -> str:
    """校验媒体/视频 MIME，防止把奇怪字符写进响应头。"""
    text = str(value or "").strip().lower()
    return text if _MIME_RE.fullmatch(text) else default


def _content_disposition(filename: str) -> str:
    """生成兼容中文文件名的下载头（ASCII 回退 + RFC 5987 UTF-8）。"""
    name = str(filename or "").strip() or "download"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "download"
    quoted = urllib.parse.quote(name, safe="")
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quoted}"


def create_server(context: WebContext, host: str, port: int) -> ThreadingHTTPServer:
    """创建并绑定 HTTP 服务，返回 server（不调用 ``serve_forever``）。

    端口被占用等情况由 ``ThreadingHTTPServer`` 抛 ``OSError``，交给 ``web_app.py`` 提示。
    绑定成功后把实际地址写回 ``context``（端口传 0 时前端/测试能拿到真实端口）。
    """
    server = _WebHTTPServer((host, int(port)), _WebRequestHandler, context)
    try:
        bound_host, bound_port = server.server_address[0], server.server_address[1]
        context.host = str(bound_host)
        context.port = int(bound_port)
    except (IndexError, TypeError, ValueError):  # pragma: no cover - 地址元组异常时保持原值
        pass
    return server
