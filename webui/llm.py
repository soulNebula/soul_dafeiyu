"""DeepSeek 模型配置与分析服务（只依赖 Python 标准库）。

为什么单独成模块：
* ``web_app.py`` 需要在 HTTP 服务启动前就把配置存储和分析服务建好；
* HTTP 层（``webui/server.py``）只做参数校验与转发，真正的网络请求都在本模块的
  后台线程里跑，避免一个慢速大模型请求阻塞 SSE / 静态资源 / 媒体请求；
* 配置单独写在项目根 ``web_config.json``，与采集配置 ``config.json`` 分开，
  避免密钥混进采集配置被复制/上传。

红线：只读消息、只写本项目自己的 ``web_config.json``；密钥只在内存与本地配置里，
不写日志、不离开本机（除了发给用户自己配置的 DeepSeek ``base_url``）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from wx_core.models import (
    KIND_EMOJI,
    KIND_FILE,
    KIND_IMAGE,
    KIND_SYSTEM,
    KIND_VOICE,
)

__all__ = [
    "MODEL_CATALOG",
    "LLMConfigStore",
    "LLMService",
    "LLMError",
    "mask_api_key",
]

# 项目根目录（webui/ 的上一级）；媒体路径都是相对它记录的
ROOT = Path(__file__).resolve().parent.parent
ROOT_RESOLVED = ROOT.resolve()

# 当前唯一可用模型（官方说明：deepseek-flash 即 DeepSeek-V4.1-Flash-1017）
MODEL_CATALOG: list[dict] = [
    {
        "id": "deepseek-flash",
        "label": "DeepSeek V4.1 Flash 1017",
        "description": "1M 上下文 · 支持深度思考与图像理解",
        "base_url": "https://api.deepseek.com",
    },
]
MODEL_IDS = tuple(item["id"] for item in MODEL_CATALOG)
MODEL_LABELS = {item["id"]: item["label"] for item in MODEL_CATALOG}
DEFAULT_MODEL = MODEL_CATALOG[0]["id"]
DEFAULT_MODEL_LABEL = MODEL_CATALOG[0]["label"]
DEFAULT_BASE_URL = MODEL_CATALOG[0]["base_url"]

# 允许写进 web_config.json 的键（白名单，避免前端塞任意字段）
CONFIG_KEYS = ("api_key", "model", "base_url", "thinking", "attach_images")
API_KEY_MIN_LEN = 8
API_KEY_MAX_LEN = 512

# 分析上下文限制（与契约 4.4 一致；附件图片数量/体积也在此收口）
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_SUFFIXES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MAX_QUESTION_LEN = 4000
MAX_HISTORY_ITEMS = 12
MAX_HISTORY_CONTENT = 8000
DEFAULT_LIMIT = 80
MIN_LIMIT = 5
MAX_LIMIT = 300

# DeepSeek 请求参数：思考模式给更大的输出预算
THINKING_MAX_TOKENS = 16384
NORMAL_MAX_TOKENS = 8192
# /models 连通性测试超时；流式分析允许较长静默（思考阶段）
TEST_TIMEOUT = 20.0
STREAM_TIMEOUT = 180.0

SYSTEM_PROMPT = """你是 wx_box（一个本地微信消息只读监听工具）的分析助手。用户会给你一段微信聊天记录转写稿，或一段朋友圈动态转写稿，你需要依据这些数据用简体中文回答用户的问题。

请遵守：
1. 只依据用户提供的转写稿作答，不要编造转写稿里没有的信息；信息不足时直接说明缺什么。
2. 提到具体结论时尽量带上时间、好友/作者、原话片段，方便用户核对。
3. 输出使用 Markdown（小标题、列表，必要时表格），默认简洁；用户要求详细分析时再展开。
4. 涉及待办事项、风险、金额、承诺、时间点、人物关系时单独列清单。
5. 不要输出 API Key、token 或与数据无关的隐私内容；不要建议任何发送消息/操作微信账号的行为。"""


class LLMError(ValueError):
    """带错误码的分析服务异常。

    HTTP 层（``webui/server.py``）根据 ``code`` 映射状态码；
    之所以继承 ``ValueError``，是为了兼容契约里「失败抛 ValueError」的约定。
    """

    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.message = str(message)
        self.code = str(code or "bad_request")


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def mask_api_key(api_key: str) -> str:
    """把 API Key 脱敏成 ``sk-…abcd`` 形式；空 key 返回空字符串。"""
    key = str(api_key or "").strip()
    if not key:
        return ""
    prefix = "sk-" if key.startswith("sk-") else ""
    tail = key[-4:] if len(key) > 4 else key[-2:]
    return f"{prefix}…{tail}"


def _model_label(model_id: str) -> str:
    return MODEL_LABELS.get(str(model_id or ""), str(model_id or ""))


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """把前端/JSON 里各种写法（bool/int/"true"/"false"）统一成 bool。"""
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    return bool(default)


def _normalize_base_url(value: Any) -> str:
    """校验并规范化 Base URL（去掉结尾斜杠，便于拼 /chat/completions）。"""
    text = str(value or "").strip()
    if not text:
        return DEFAULT_BASE_URL
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise LLMError("Base URL 必须是 http:// 或 https:// 开头的完整地址", "bad_request")
    return text.rstrip("/")


def _format_time(ts: Any) -> str:
    """把 epoch 秒格式化成 ``2026-10-22 05:11:53``；非法值给「时间未知」。"""
    try:
        value = float(ts or 0)
    except (TypeError, ValueError):
        return "时间未知"
    if value <= 0:
        return "时间未知"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
    except (OverflowError, OSError, ValueError):
        return "时间未知"


def _one_line(text: Any) -> str:
    """把多行文本压成一行，保证转写稿里一条消息只占一行。"""
    return " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split())


def _extract_error_detail(exc: urllib.error.HTTPError) -> str:
    """尽量从 DeepSeek 的错误响应里取一句可读 detail（失败则返回空串）。"""
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001 - 错误流本身读不到就算了
        return ""
    if not raw:
        return ""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _one_line(raw.decode("utf-8", "replace"))[:200]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return _one_line(error.get("message") or "")[:300]
        if error:
            return _one_line(error)[:300]
        if payload.get("message"):
            return _one_line(payload.get("message"))[:300]
    return ""


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    """把 DeepSeek 的 HTTP 错误码翻译成中文人话提示。"""
    status = int(getattr(exc, "code", 0) or 0)
    detail = _extract_error_detail(exc)
    if status == 401:
        return "API Key 无效或已过期，请在「模型设置」里重新填写"
    if status == 402:
        return "账户余额不足，请到 DeepSeek 开放平台充值后再试"
    if status == 429:
        return "请求过于频繁，请稍等几秒再试"
    if status == 400:
        base = "请求被 DeepSeek 拒绝（参数不合法）"
        return f"{base}：{detail}" if detail else base
    if status == 404:
        return "接口地址不正确，请检查「模型设置」里的 Base URL"
    if 500 <= status < 600:
        return f"DeepSeek 服务暂时不可用（HTTP {status}），请稍后重试"
    base = f"DeepSeek 返回 HTTP {status}"
    return f"{base}：{detail}" if detail else base


def _network_error_message(exc: BaseException) -> str:
    """把网络层异常翻译成中文人话提示。"""
    reason = getattr(exc, "reason", exc)
    text = str(reason or exc)
    lowered = text.lower()
    if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in lowered:
        return "连接 DeepSeek 超时，请检查网络后重试"
    if "getaddrinfo" in lowered or "name or service not known" in lowered or "nodename" in lowered:
        return "无法解析 DeepSeek 域名，请检查网络或 DNS 设置"
    if "certificate" in lowered or "ssl" in lowered:
        return f"HTTPS 证书校验失败，请检查网络/代理设置：{text[:160]}"
    if "refused" in lowered or "unreachable" in lowered or "reset" in lowered:
        return f"无法连接 DeepSeek 服务（{text[:160]}），请检查网络或代理设置"
    return f"无法连接 DeepSeek 服务：{text[:160]}"


def _parse_models_payload(raw: bytes) -> list[dict]:
    """解析 ``GET /models`` 的响应，返回 ``[{"id","label"}]`` 列表。"""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    items: list[dict] = []
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, list):
        for item in data[:50]:
            if isinstance(item, dict) and item.get("id"):
                model_id = str(item["id"])
                items.append({"id": model_id, "label": _model_label(model_id)})
    return items


def _normalize_usage(raw: Any) -> dict:
    """把 DeepSeek 的 usage 归一成契约 5.2 的四个字段。"""
    usage: dict = {}
    if not isinstance(raw, dict):
        return usage
    details = raw.get("completion_tokens_details")
    if not isinstance(details, dict):
        details = {}

    def _as_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    usage["prompt_tokens"] = _as_int(raw.get("prompt_tokens"))
    usage["completion_tokens"] = _as_int(raw.get("completion_tokens"))
    usage["total_tokens"] = _as_int(raw.get("total_tokens")) or (
        usage["prompt_tokens"] + usage["completion_tokens"]
    )
    reasoning = raw.get("reasoning_tokens")
    if reasoning is None:
        reasoning = details.get("reasoning_tokens")
    usage["reasoning_tokens"] = _as_int(reasoning)
    return usage


def _resolve_media_path(rel_path: str) -> Path | None:
    """把项目根相对路径解析成真实文件路径；只允许项目目录内存在的文件。"""
    text = str(rel_path or "").strip()
    if not text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return None
    try:
        resolved.relative_to(ROOT_RESOLVED)
    except ValueError:
        # 只读取项目目录内的媒体文件，防止被构造出越界路径
        return None
    return resolved if resolved.is_file() else None


def _image_candidates_from_messages(messages: list[dict]) -> list[tuple[Path, str]]:
    """挑出聊天消息里的图片候选（先只 stat，不读文件，最后取最近 6 张）。"""
    candidates: list[tuple[Path, str]] = []
    for msg in messages:
        if str(msg.get("kind") or "") not in (KIND_IMAGE, KIND_EMOJI):
            continue
        for key in ("media_path", "thumb_path"):
            rel = str(msg.get(key) or "").strip()
            if not rel:
                continue
            suffix = Path(rel).suffix.lower()
            mime = IMAGE_SUFFIXES.get(suffix)
            if not mime:
                continue
            path = _resolve_media_path(rel)
            if path is None:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size <= 0 or size > MAX_IMAGE_BYTES:
                continue
            candidates.append((path, mime))
            break
    return candidates[-MAX_IMAGES:]


_IMAGE_MIME_RE = re.compile(r"image/[a-z0-9][a-z0-9.+-]{0,31}")


def _normalize_image_mime(value: Any, fallback: Any = "image/jpeg") -> str:
    """把图片 MIME 规整成合法 ``image/*``（非法值回退，绝不拼接进 header）。"""
    for candidate in (value, fallback):
        text = str(candidate or "").strip().lower()
        if _IMAGE_MIME_RE.fullmatch(text):
            return text
    return "image/jpeg"


def _image_sources_from_moments(items: list[dict], moments: Any = None) -> list[dict]:
    """挑出朋友圈图片来源，优先用 V2 的内存媒体（thumb_key → moments.media()）。

    V2 起朋友圈图片/头像字节只放进程内存、不落盘，``media[].path`` 可能为空；
    旧版数据仍保留磁盘路径，因此这里两种都兼容。返回 source dict：
    ``{"kind": "memory", "data": bytes, "mime": str}`` 或
    ``{"kind": "file", "path": Path, "mime": str}``，最多 ``MAX_IMAGES`` 张（取最近）。
    """
    sources: list[dict] = []
    media_fn = getattr(moments, "media", None) if moments is not None else None
    for moment in items:
        if not isinstance(moment, dict):
            continue
        media = moment.get("media")
        if not isinstance(media, list):
            continue
        for entry in media:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("type") or "") != "image":
                continue
            # 1) V2：内存媒体 key（thumb_key 由 MomentsService.media(key) 返回 bytes/mime）
            thumb_key = str(entry.get("thumb_key") or "").strip()
            if thumb_key and callable(media_fn):
                try:
                    in_memory = media_fn(thumb_key)
                except Exception:  # noqa: BLE001 - 取不到就退回落盘路径
                    in_memory = None
                if isinstance(in_memory, tuple) and len(in_memory) >= 2:
                    raw = in_memory[0]
                    if isinstance(raw, (bytes, bytearray)) and 0 < len(raw) <= MAX_IMAGE_BYTES:
                        sources.append(
                            {
                                "kind": "memory",
                                "data": bytes(raw),
                                "mime": _normalize_image_mime(
                                    in_memory[1], entry.get("thumb_mime")
                                ),
                            }
                        )
                        continue
            # 2) 旧版：media/moments 磁盘文件（向前兼容）
            for field in ("path", "thumb"):
                rel = str(entry.get(field) or "").strip()
                if not rel:
                    continue
                suffix = Path(rel).suffix.lower()
                mime = IMAGE_SUFFIXES.get(suffix)
                if not mime:
                    continue
                path = _resolve_media_path(rel)
                if path is None:
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size <= 0 or size > MAX_IMAGE_BYTES:
                    continue
                sources.append({"kind": "file", "path": path, "mime": mime})
                break
    return sources[-MAX_IMAGES:]


def _load_image_sources(sources: list[dict]) -> list[str]:
    """把朋友圈图片来源读成 base64 data URL（读失败的跳过，不影响分析）。"""
    data_urls: list[str] = []
    for source in sources:
        raw: bytes | None = None
        kind = str(source.get("kind") or "")
        if kind == "memory":
            value = source.get("data")
            if isinstance(value, (bytes, bytearray)):
                raw = bytes(value)
        elif kind == "file":
            path = source.get("path")
            if isinstance(path, Path):
                try:
                    raw = path.read_bytes()
                except OSError:
                    raw = None
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            continue
        mime = _normalize_image_mime(source.get("mime"))
        data_urls.append(f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}")
    return data_urls


def _load_image_blocks(candidates: list[tuple[Path, str]]) -> list[str]:
    """把图片候选读成 base64 data URL 列表（读失败的跳过，不影响分析）。"""
    data_urls: list[str] = []
    for path, mime in candidates:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        data_urls.append(f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}")
    return data_urls


def _message_body(msg: dict) -> str:
    """把一条 Message dict 转成转写稿里的正文/占位文字。"""
    kind = str(msg.get("kind") or "")
    text = _one_line(msg.get("text"))
    extra = msg.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    size_text = _one_line(extra.get("size_text"))
    if kind == KIND_IMAGE:
        return f"[图片]（{size_text}）" if size_text else "[图片]"
    if kind == KIND_EMOJI:
        name = _one_line(extra.get("emoji_name"))
        base = f"[表情包：{name}]" if name else "[表情包]"
        return f"{base}（{size_text}）" if size_text else base
    if kind == KIND_FILE:
        name = text or _one_line(extra.get("original_name")) or "文件"
        return f"[文件] {name}（{size_text}）" if size_text else f"[文件] {name}"
    if kind == KIND_VOICE:
        duration = extra.get("duration")
        return f"[语音 {duration} 秒]" if duration else "[语音]"
    if kind == KIND_SYSTEM:
        return text or "[系统消息]"
    return text or "[未知消息]"


def _build_chat_transcript(messages: list[dict], scope_label: str) -> str:
    """把消息列表拼成中文 Markdown 转写稿（时间升序，一行一条）。

    V3 群聊：历史/实时消息的 ``extra["sender_name"]`` 是群成员显示名。此时行首
    用「群名/成员名」代替「对方」，让模型能区分群里谁说了什么；自己发的消息
    （``sender == "self"`` 或没有 sender_name）仍保持原格式（「群名 · 我」）。
    """
    if not messages:
        return f"## 消息记录（范围：{scope_label}）\n\n（该范围内暂无消息）"
    lines = [f"## 消息记录（范围：{scope_label}，共 {len(messages)} 条，按时间升序）", ""]
    for msg in messages:
        friend = _one_line(msg.get("friend"))
        is_self = str(msg.get("sender") or "") == "self"
        sender = _one_line(msg.get("sender_label")) or ("我" if is_self else "对方")
        kind_label = _one_line(msg.get("kind_label")) or "消息"
        extra = msg.get("extra")
        sender_name = _one_line(extra.get("sender_name")) if isinstance(extra, dict) else ""
        time_text = _format_time(msg.get("ts"))
        body = _message_body(msg)
        if sender_name and not is_self:
            who = f"{friend}/{sender_name}" if friend else sender_name
            if kind_label == "文字":
                # 与契约示例一致：[时间] 群名/成员名（对方）：内容
                lines.append(f"- [{time_text}] {who}（{sender}）：{body}")
            else:
                # 非文字消息保留类型标记，避免图片/文件/语音与文字混淆
                lines.append(f"- [{time_text}] {who}（{sender} · {kind_label}）：{body}")
        else:
            who = f"{friend} · {sender}" if friend else sender
            lines.append(f"- [{time_text}] {who}（{kind_label}）：{body}")
    return "\n".join(lines)


def _moment_media_markers(moment: dict) -> str:
    """朋友圈媒体标记，如 ``[图片] [视频 15 秒]``。"""
    media = moment.get("media")
    if not isinstance(media, list):
        return ""
    markers: list[str] = []
    for item in media:
        if not isinstance(item, dict):
            continue
        mtype = str(item.get("type") or "").strip()
        if mtype == "image":
            markers.append("[图片]")
        elif mtype == "video":
            duration = item.get("duration")
            markers.append(f"[视频 {duration} 秒]" if duration else "[视频]")
        elif mtype:
            markers.append(f"[{mtype}]")
    return " ".join(markers)


def _build_moments_transcript(items: list[dict], scope_label: str) -> str:
    """把朋友圈动态拼成中文 Markdown 转写稿（作者/时间/正文/位置/点赞/评论）。"""
    if not items:
        return f"## 朋友圈动态（范围：{scope_label}）\n\n（该范围内暂无朋友圈动态）"
    lines = [f"## 朋友圈动态（范围：{scope_label}，共 {len(items)} 条，按时间倒序）", ""]
    for index, moment in enumerate(items, start=1):
        author = _one_line(moment.get("author")) or "未知作者"
        when = _format_time(moment.get("create_time"))
        if when == "时间未知":
            when = _one_line(moment.get("time_text")) or "时间未知"
        lines.append(f"### {index}. {author} · {when}")
        content = _one_line(moment.get("content"))
        if content:
            lines.append(f"正文：{content}")
        location = _one_line(moment.get("location"))
        if location:
            lines.append(f"位置：{location}")
        media_text = _moment_media_markers(moment)
        if media_text:
            lines.append(f"媒体：{media_text}")
        link = moment.get("link")
        if isinstance(link, dict):
            title = _one_line(link.get("title")) or "分享"
            url = _one_line(link.get("url"))
            lines.append(f"分享：{title}" + (f"（{url}）" if url else ""))
        likes = moment.get("likes")
        if isinstance(likes, list) and likes:
            names = "、".join(_one_line(name) for name in likes if _one_line(name))
            if names:
                lines.append(f"点赞（{len(likes)}）：{names}")
        comments = moment.get("comments")
        if isinstance(comments, list) and comments:
            lines.append(f"评论（{len(comments)}）：")
            for comment in comments:
                if not isinstance(comment, dict):
                    continue
                c_author = _one_line(comment.get("author")) or "某人"
                c_text = _one_line(comment.get("content"))
                if c_text:
                    lines.append(f"- {c_author}：{c_text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _scope_default_label(scope: dict) -> str:
    stype = str(scope.get("type") or "")
    if stype == "friend":
        return str(scope.get("friend") or "") or "当前会话"
    if stype == "monitored":
        return "监听好友"
    if stype == "moments":
        return "全部朋友圈"
    if stype == "moment":
        return "朋友圈动态"
    return "全部消息"


def _validate_scope(scope: Any) -> dict:
    """校验并清洗分析范围（只保留服务端认识的字段）。"""
    if not isinstance(scope, dict):
        raise LLMError("分析范围格式不正确（应为 JSON 对象）", "bad_request")
    stype = str(scope.get("type") or "").strip()
    if stype not in ("friend", "all", "monitored", "moments", "moment"):
        raise LLMError(
            "分析范围只支持 friend / all / monitored / moments / moment",
            "bad_request",
        )
    clean: dict = {"type": stype}
    if stype == "friend":
        friend = str(scope.get("friend") or "").strip()
        if not friend:
            raise LLMError("请先选择要分析的好友/会话", "bad_request")
        clean["friend"] = friend
    elif stype == "monitored":
        names = scope.get("friend_names")
        if names is not None:
            if not isinstance(names, list):
                raise LLMError("friend_names 必须是字符串数组", "bad_request")
            clean["friend_names"] = [str(name).strip() for name in names if str(name).strip()]
    elif stype == "moment":
        moment_id = str(scope.get("id") or "").strip()
        if not moment_id:
            raise LLMError("分析单条朋友圈时缺少 id", "bad_request")
        clean["id"] = moment_id
    return clean


def _normalize_history(history: Any) -> list[dict]:
    """清洗历史对话：只留 user/assistant、最多 12 条、每条最多 8000 字。"""
    result: list[dict] = []
    if not isinstance(history, list):
        return result
    for item in history[-MAX_HISTORY_ITEMS:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        if role not in ("user", "assistant"):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        result.append({"role": role, "content": content[:MAX_HISTORY_CONTENT]})
    return result


def _normalize_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    if limit <= 0:
        limit = DEFAULT_LIMIT
    return max(MIN_LIMIT, min(limit, MAX_LIMIT))


# ---------------------------------------------------------------------------
# 配置存储
# ---------------------------------------------------------------------------
class LLMConfigStore:
    """管理项目根 ``web_config.json`` 里的 LLM 配置（原子写 + 脱敏）。

    配置结构::

        {"llm": {"api_key": "", "model": "deepseek-flash",
                 "base_url": "https://api.deepseek.com",
                 "thinking": true, "attach_images": true}}

    没有保存 key 时用环境变量 ``DEEPSEEK_API_KEY`` 兜底（不回显明文）。
    所有读写都在同一把可重入锁内完成：HTTP 线程、后台分析线程可能同时读配置。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    # ---- 读 ----
    @staticmethod
    def _defaults() -> dict:
        return {
            "api_key": "",
            "model": DEFAULT_MODEL,
            "base_url": DEFAULT_BASE_URL,
            "thinking": True,
            "attach_images": True,
        }

    def _read_raw(self) -> dict:
        """读磁盘上的原始 JSON；文件不存在/损坏都返回空 dict（不抛异常）。"""
        try:
            text = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return {}
        try:
            data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _saved_config(self) -> dict:
        """只读「已保存到磁盘」的配置（不含环境变量兜底的 key）。"""
        raw = self._read_raw()
        section = raw.get("llm")
        if not isinstance(section, dict):
            # 兼容早期把字段直接放在顶层的写法
            section = raw
        config = self._defaults()

        api_key = section.get("api_key")
        if isinstance(api_key, str):
            config["api_key"] = api_key.strip()

        model = section.get("model")
        if isinstance(model, str) and model.strip() in MODEL_IDS:
            config["model"] = model.strip()

        if section.get("base_url") is not None:
            try:
                config["base_url"] = _normalize_base_url(section.get("base_url"))
            except LLMError:
                config["base_url"] = DEFAULT_BASE_URL

        config["thinking"] = _coerce_bool(section.get("thinking"), True)
        config["attach_images"] = _coerce_bool(section.get("attach_images"), True)
        return config

    def load(self) -> dict:
        """返回完整配置（api_key 为明文；没有保存 key 时用环境变量兜底）。"""
        with self._lock:
            config = self._saved_config()
            if not config["api_key"]:
                env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
                if env_key:
                    config["api_key"] = env_key
            return config

    def public(self) -> dict:
        """返回给前端的脱敏配置（契约 4.2 的 llm 字段）。"""
        config = self.load()
        return {
            "configured": bool(config["api_key"]),
            "model": config["model"],
            "model_label": _model_label(config["model"]),
            "base_url": config["base_url"],
            "api_key_masked": mask_api_key(config["api_key"]),
            "thinking": bool(config["thinking"]),
            "attach_images": bool(config["attach_images"]),
            "busy": False,
            "models": [
                {
                    "id": item["id"],
                    "label": item["label"],
                    "description": item["description"],
                }
                for item in MODEL_CATALOG
            ],
        }

    # ---- 写 ----
    def _write(self, config: dict) -> None:
        """原子写配置（临时文件 + os.replace），并尽量收紧文件权限。"""
        payload = {"llm": {key: config.get(key) for key in CONFIG_KEYS}}
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp_name, 0o600)  # Windows 上忽略（尽量 600）
            except OSError:
                pass
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def save(self, patch: dict) -> dict:
        """保存配置补丁，返回最新的 ``public()``。

        * ``api_key`` 省略/空字符串 = 保留已保存的 key；``"clear"`` = 清空；
        * 未知键忽略（白名单），非法值抛 :class:`LLMError`。
        """
        if not isinstance(patch, dict):
            raise LLMError("配置内容必须是 JSON 对象", "bad_request")
        with self._lock:
            config = self._saved_config()  # 注意：基于磁盘内容，不把环境变量 key 写盘

            if "api_key" in patch:
                raw_key = patch.get("api_key")
                if raw_key is not None:
                    key_text = str(raw_key).strip()
                    if key_text == "clear":
                        config["api_key"] = ""
                    elif key_text:
                        if not (API_KEY_MIN_LEN <= len(key_text) <= API_KEY_MAX_LEN):
                            raise LLMError(
                                f"API Key 长度应在 {API_KEY_MIN_LEN}..{API_KEY_MAX_LEN} 个字符之间",
                                "bad_request",
                            )
                        config["api_key"] = key_text
                    # 空字符串 = 保留原 key

            if "model" in patch:
                model = str(patch.get("model") or "").strip()
                if model not in MODEL_IDS:
                    raise LLMError(f"不支持的模型：{model or '(空)'}", "bad_request")
                config["model"] = model

            if "base_url" in patch:
                config["base_url"] = _normalize_base_url(patch.get("base_url"))

            if "thinking" in patch:
                config["thinking"] = _coerce_bool(patch.get("thinking"), config["thinking"])
            if "attach_images" in patch:
                config["attach_images"] = _coerce_bool(
                    patch.get("attach_images"), config["attach_images"]
                )

            self._write(config)
        return self.public()


# ---------------------------------------------------------------------------
# 分析服务
# ---------------------------------------------------------------------------
class _Job:
    """一次正在运行的分析任务（只允许一个）。"""

    __slots__ = ("job_id", "cancel", "created_at", "response", "thread")

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.cancel = threading.Event()
        self.created_at = time.time()
        self.response: Any = None
        self.thread: threading.Thread | None = None


class LLMService:
    """DeepSeek 分析服务：配置读写 + 连通性测试 + 后台流式分析 + 取消。

    :param store: :class:`LLMConfigStore`。
    :param bridge: 事件桥（只用到 ``broadcast(event)``，用来推 ``llm_*`` 事件）。
    :param moments: 可选的朋友圈服务（有 ``scope_items(scope, limit)`` 即可）；
                    为 None 时朋友圈范围分析返回可读错误，不影响聊天分析。
    :param logger: 单参数日志函数（默认 print）；不会把 key 写进日志。
    """

    def __init__(
        self,
        store: LLMConfigStore,
        bridge: Any,
        moments: Any = None,
        *,
        logger: Callable[[str], None] = print,
    ):
        self.store = store
        self.bridge = bridge
        self._moments = moments
        self._logger = logger
        self._lock = threading.Lock()
        self._job: _Job | None = None

    # ---- 小工具 ----
    def _log(self, message: str) -> None:
        try:
            self._logger(message)
        except Exception:  # noqa: BLE001 - 日志失败不影响分析
            pass

    def _broadcast(self, event_type: str, data: dict) -> None:
        """通过事件桥推一条 SSE 事件（失败只记日志，不中断分析）。"""
        event = {"type": event_type, "data": data}
        try:
            self.bridge.broadcast(event)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[llm] 广播 {event_type} 事件失败：{exc}")

    # ---- 配置 ----
    def public_config(self) -> dict:
        """给前端的配置（脱敏）+ 当前忙碌状态。"""
        config = self.store.public()
        config["busy"] = self.is_busy()
        return config

    def save_config(self, patch: dict) -> dict:
        """保存配置并返回最新公开配置。"""
        self.store.save(patch)
        return self.public_config()

    def set_moments(self, moments: Any) -> None:
        """后置绑定朋友圈服务。

        ``web_app.py`` 可能先创建 ``LLMService`` 再创建 ``MomentsService``（朋友圈依赖
        微信登录与子进程环境，构造顺序更晚），所以留一个后置绑定的入口；朋友圈为
        ``None`` 时只是朋友圈范围分析返回可读错误，不影响聊天分析。
        """
        self._moments = moments

    def is_busy(self) -> bool:
        with self._lock:
            return self._job is not None

    # ---- 连通性测试 ----
    def test_connection(self) -> dict:
        """用 ``GET {base_url}/models`` 测试 key/网络，返回契约 3.2 的形状。"""
        config = self.store.load()
        if not config["api_key"]:
            return {
                "ok": False,
                "latency_ms": None,
                "error": "尚未配置 DeepSeek API Key，请先在「模型设置」里填写",
                "models": [],
            }
        url = config["base_url"].rstrip("/") + "/models"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Accept": "application/json",
            },
            method="GET",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=TEST_TIMEOUT) as response:
                raw = response.read()
            latency_ms = int((time.perf_counter() - started) * 1000)
            return {
                "ok": True,
                "latency_ms": latency_ms,
                "error": None,
                "models": _parse_models_payload(raw),
            }
        except urllib.error.HTTPError as exc:
            return {
                "ok": False,
                "latency_ms": None,
                "error": _http_error_message(exc),
                "models": [],
            }
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {
                "ok": False,
                "latency_ms": None,
                "error": _network_error_message(exc),
                "models": [],
            }
        except Exception as exc:  # noqa: BLE001 - 兜底给可读原因
            return {
                "ok": False,
                "latency_ms": None,
                "error": f"测试连接失败：{exc}",
                "models": [],
            }

    # ---- 取消 ----
    def cancel(self, job_id: str) -> bool:
        """取消当前任务；job_id 不匹配/没有任务时返回 False。"""
        with self._lock:
            job = self._job
            if job is None:
                return False
            if job_id and job.job_id != str(job_id):
                return False
            job.cancel.set()
            response = job.response
        if response is not None:
            # 关闭底层响应，让阻塞在 read 的后台线程立刻退出
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
        self._log(f"[llm] 已请求取消分析任务 {job.job_id}")
        return True

    # ---- 发起分析 ----
    def start_analysis(
        self,
        *,
        scope: dict,
        scope_label: str,
        question: str,
        history: list[dict],
        options: dict,
    ) -> dict:
        """校验参数、拼上下文并启动后台流式分析；成功后立即返回 job_id。"""
        config = self.store.load()
        if not config["api_key"]:
            raise LLMError(
                "尚未配置 DeepSeek API Key，请先在「模型设置」里填写", "not_configured"
            )
        if self.is_busy():
            raise LLMError("模型正在分析上一条请求，请等待完成或点击「停止」", "busy")

        clean_scope = _validate_scope(scope)
        question_text = str(question or "").strip()
        if not question_text:
            raise LLMError("问题不能为空", "bad_request")
        if len(question_text) > MAX_QUESTION_LEN:
            raise LLMError(f"问题太长（最多 {MAX_QUESTION_LEN} 字）", "bad_request")

        options = options if isinstance(options, dict) else {}
        thinking = _coerce_bool(options.get("thinking"), config["thinking"])
        attach_images = _coerce_bool(options.get("attach_images"), config["attach_images"])
        limit = _normalize_limit(options.get("limit"))
        history_items = _normalize_history(history)

        label = str(scope_label or "").strip()[:200] or _scope_default_label(clean_scope)
        scope_type = str(clean_scope["type"])

        # ---- 拼上下文（读取消息/朋友圈失败要给出可读错误，而不是 500 堆栈）----
        image_urls: list[str] = []
        if scope_type in ("moments", "moment"):
            items = self._moments_items(clean_scope, limit)
            item_count = len(items)
            transcript = _build_moments_transcript(items, label)
            if attach_images:
                # V2：图片字节在 MomentsService 内存里（thumb_key），不再落盘
                image_urls = _load_image_sources(
                    _image_sources_from_moments(items, self._moments)
                )
            if scope_type == "moment" and label == _scope_default_label(clean_scope) and items:
                # 单条动态没传 scope_label 时尽量用作者+时间，前端仍可覆盖
                author = _one_line(items[0].get("author")) or "未知作者"
                label = f"{author} 的朋友圈"
        else:
            messages = self._scope_messages(clean_scope, limit)
            item_count = len(messages)
            transcript = _build_chat_transcript(messages, label)
            if attach_images:
                image_urls = _load_image_blocks(_image_candidates_from_messages(messages))

        messages_payload: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages_payload.extend(history_items)
        messages_payload.append(self._user_message(transcript, question_text, image_urls))

        job = _Job(uuid.uuid4().hex[:16])
        with self._lock:
            if self._job is not None:
                raise LLMError("模型正在分析上一条请求，请等待完成或点击「停止」", "busy")
            self._job = job

        self._broadcast(
            "llm_start",
            {
                "job_id": job.job_id,
                "model": config["model"],
                "model_label": _model_label(config["model"]),
                "scope_label": label,
                "message_count": item_count,
                "image_count": len(image_urls),
                "thinking": thinking,
                "started_at": job.created_at,
            },
        )
        thread = threading.Thread(
            target=self._run_job,
            args=(job, messages_payload, thinking, dict(config)),
            name=f"wxbox-llm-{job.job_id}",
            daemon=True,
        )
        job.thread = thread
        thread.start()
        self._log(
            f"[llm] 开始分析：job={job.job_id} scope={label} "
            f"条数={item_count} 图片={len(image_urls)} thinking={thinking}"
        )
        return {"ok": True, "job_id": job.job_id}

    # ---- 上下文读取 ----
    def _scope_messages(self, scope: dict, limit: int) -> list[dict]:
        try:
            messages = self.bridge.scope_messages(scope, limit) or []
        except Exception as exc:  # noqa: BLE001 - 后端读消息失败要给可读原因
            raise LLMError(f"读取聊天记录失败：{exc}", "server_error") from exc
        return [item for item in messages if isinstance(item, dict)]

    def _moments_items(self, scope: dict, limit: int) -> list[dict]:
        moments = self._moments
        if moments is None:
            raise LLMError(
                "朋友圈模块不可用，无法分析朋友圈数据（请确认 webui/moments.py 已启用）",
                "server_error",
            )
        try:
            items = moments.scope_items(scope, limit) or []
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"读取朋友圈数据失败：{exc}", "server_error") from exc
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _user_message(transcript: str, question: str, image_urls: list[str]) -> dict:
        """构造最终 user 消息：转写稿 + 问题，附带图片内容块（可选）。"""
        text = (
            transcript
            + "\n\n---\n\n## 我的问题\n\n"
            + question
            + ("\n\n（已随附 " + str(len(image_urls)) + " 张图片，请一并参考。）" if image_urls else "")
        )
        if not image_urls:
            return {"role": "user", "content": text}
        blocks: list[dict] = [{"type": "text", "text": text}]
        for data_url in image_urls:
            blocks.append({"type": "image_url", "image_url": {"url": data_url}})
        return {"role": "user", "content": blocks}

    # ---- 后台流式请求 ----
    def _run_job(self, job: _Job, messages: list[dict], thinking: bool, config: dict) -> None:
        """后台线程：请求 DeepSeek 流式接口并把增量通过 SSE 推给前端。"""
        started = job.created_at
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict = {}
        last_usage_key: str | None = None
        finish_reason = ""
        cancelled = False
        failure: tuple[str, str] | None = None

        if job.cancel.is_set():
            cancelled = True
        else:
            request_body = {
                "model": config["model"],
                "messages": messages,
                "stream": True,
                "stream_options": {"include_usage": True},
                "max_tokens": THINKING_MAX_TOKENS if thinking else NORMAL_MAX_TOKENS,
                "thinking": {"type": "enabled" if thinking else "disabled"},
            }
            url = str(config["base_url"]).rstrip("/") + "/chat/completions"
            request = urllib.request.Request(
                url,
                data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {config['api_key']}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=STREAM_TIMEOUT) as response:
                    job.response = response
                    if job.cancel.is_set():
                        cancelled = True
                    else:
                        for raw_line in response:
                            if job.cancel.is_set():
                                cancelled = True
                                break
                            line = raw_line.decode("utf-8", "replace").strip()
                            if not line or line.startswith(":"):
                                continue
                            if not line.startswith("data:"):
                                continue
                            data_text = line[5:].strip()
                            if not data_text:
                                continue
                            if data_text == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_text)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(chunk, dict):
                                continue
                            # usage 可能出现在 choices 为空的最后一个 chunk
                            if chunk.get("usage"):
                                usage = _normalize_usage(chunk.get("usage"))
                                usage_key = json.dumps(usage, sort_keys=True)
                                if usage_key != last_usage_key:
                                    last_usage_key = usage_key
                                    self._broadcast(
                                        "llm_usage", {"job_id": job.job_id, "usage": usage}
                                    )
                            choices = chunk.get("choices")
                            if not isinstance(choices, list) or not choices:
                                continue
                            choice = choices[0] if isinstance(choices[0], dict) else {}
                            delta = choice.get("delta")
                            if not isinstance(delta, dict):
                                delta = {}
                            reasoning_delta = delta.get("reasoning_content")
                            if reasoning_delta:
                                reasoning_parts.append(str(reasoning_delta))
                                self._broadcast(
                                    "llm_reasoning",
                                    {"job_id": job.job_id, "delta": str(reasoning_delta)},
                                )
                            content_delta = delta.get("content")
                            if content_delta:
                                content_parts.append(str(content_delta))
                                self._broadcast(
                                    "llm_delta",
                                    {"job_id": job.job_id, "delta": str(content_delta)},
                                )
                            chunk_finish = choice.get("finish_reason")
                            if chunk_finish:
                                finish_reason = str(chunk_finish)
            except urllib.error.HTTPError as exc:
                if job.cancel.is_set():
                    cancelled = True
                else:
                    failure = (_http_error_message(exc), "llm_error")
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
                if job.cancel.is_set():
                    cancelled = True
                else:
                    failure = (_network_error_message(exc), "llm_error")
            except Exception as exc:  # noqa: BLE001 - 兜底，任何异常都要有结束事件
                if job.cancel.is_set():
                    cancelled = True
                else:
                    failure = (f"分析过程出错：{type(exc).__name__}: {exc}", "llm_error")
            finally:
                job.response = None

        elapsed_ms = int((time.time() - started) * 1000)
        if failure is not None:
            error_text, code = failure
            self._broadcast(
                "llm_error",
                {"job_id": job.job_id, "error": error_text, "code": code},
            )
            self._log(f"[llm] 分析失败 job={job.job_id}：{error_text}")
            self._finish(job)
            return

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        if cancelled:
            self._log(
                f"[llm] 分析已取消 job={job.job_id}，已生成 {len(content)} 字"
            )
        self._broadcast(
            "llm_done",
            {
                "job_id": job.job_id,
                "content": content,
                "reasoning": reasoning,
                "usage": usage,
                "elapsed_ms": elapsed_ms,
                "cancelled": cancelled,
                "finish_reason": finish_reason,
            },
        )
        self._finish(job)

    def _finish(self, job: _Job) -> None:
        """清空当前任务占用（只清自己，避免误清新任务）。"""
        job.response = None
        with self._lock:
            if self._job is job:
                self._job = None
