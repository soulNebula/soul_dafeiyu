# -*- coding: utf-8 -*-
"""P5-a 发送层多媒体：类型规范 / 能力矩阵 / 白名单校验 / 贴纸解析。

背景：wxauto4 **只有** ``Chat.SendMsg``（文本）与 ``Chat.SendFiles``（文件），
没有原生"表情/贴纸"API。所以本模块定义"能发什么、怎么降级"，实际发送在
:mod:`agi.wechat_gateway.sender` 的 ``send_bubbles``。

items 规范（**对外契约，字段不要改名**，决策层 task-60 直接依赖）::

    {"type": "text"|"emoji_text"|"image"|"gif"|"sticker",
     "text": "...",      # text / emoji_text 必填（emoji_text 就是含 Unicode emoji 的文本）
     "path": "...",      # image / gif / sticker 可选（绝对路径或白名单目录内相对名）
     "name": "..."}      # sticker/image 可用名字定位（如 "666" / "亲亲"）

- ``text`` / ``emoji_text``：走 ``SendMsg``（emoji 只是普通字符）；
- ``image`` / ``gif``：走 ``SendFiles``（GIF 由微信客户端渲染成动图）；
- ``sticker``：**没有原生 API** → 降级链 ``SendFiles(图)`` → ``emoji_text`` → 纯文本，
  由 sender 执行；``.wxgf`` 是微信专有格式，不能作为文件发出去，解析时返回 None。

白名单目录：``wx_core/emoji_assets``、``media/stickers``、``agi/data/selflearn/media``，
外加运行时配置 ``media_dirs``。扩展名只允许 png/jpg/jpeg/gif/webp，单文件 ≤10MB。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]

EMOJI_ASSET_DIR = ROOT / "wx_core" / "emoji_assets"
STICKER_DIR = ROOT / "media" / "stickers"
INBOUND_MEDIA_DIR = ROOT / "agi" / "data" / "selflearn" / "media"

#: 默认白名单目录（顺序即解析优先级）
MEDIA_ROOT_DIR = ROOT / "media"          # 工作台媒体目录（文档/图片/贴纸素材）
DEFAULT_MEDIA_DIRS: tuple[Path, ...] = (EMOJI_ASSET_DIR, STICKER_DIR, INBOUND_MEDIA_DIR,
                                       MEDIA_ROOT_DIR)

MEDIA_TYPES: tuple[str, ...] = ("text", "emoji_text", "image", "gif", "sticker", "file")
TEXT_TYPES = frozenset({"text", "emoji_text"})
FILE_TYPES = frozenset({"image", "gif", "sticker"})
ALLOWED_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
#: P0-144 文档类：PDF/Word/Excel/PPT/压缩包/文本等（file 类型专用）
FILE_EXTS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".md", ".zip", ".rar", ".7z", ".tar", ".gz", ".csv", ".json",
})
#: 微信专有贴纸格式：可以存，但不能当文件发给对方
UNSENDABLE_EXTS = frozenset({".wxgf"})

DEFAULT_MAX_MB = 10.0
DEFAULT_FILE_MAX_MB = 50.0
DEFAULT_INTERVAL_MS = 120
MIN_INTERVAL_MS = 80
MAX_INTERVAL_MS = 150
DEFAULT_FALLBACK_EMOJI = "😄"
MAX_TEXT_CHARS = 500


# --------------------------------------------------------------------------- #
# 配置读取
# --------------------------------------------------------------------------- #
def _cfg_get(cfg: dict | None, key: str, default: Any) -> Any:
    if isinstance(cfg, dict) and cfg.get(key) is not None:
        return cfg.get(key)
    return default


def media_dirs(cfg: dict | None = None) -> list[Path]:
    """白名单目录 = 三个默认目录 + 配置 ``media_dirs``（去重、保持顺序）。"""
    dirs: list[Path] = list(DEFAULT_MEDIA_DIRS)
    raw = _cfg_get(cfg, "media_dirs", []) or []
    if isinstance(raw, (list, tuple, set)):
        for entry in raw:
            try:
                dirs.append(Path(str(entry)))
            except (TypeError, ValueError):
                continue
    seen: set[str] = set()
    unique: list[Path] = []
    for path in dirs:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def max_bytes(cfg: dict | None = None) -> int:
    try:
        mb = float(_cfg_get(cfg, "media_max_mb", DEFAULT_MAX_MB))
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_MB
    return int(max(0.001, mb) * 1024 * 1024)


def file_max_bytes(cfg: dict | None = None) -> int:
    """文档（type=file）大小上限：``media_file_max_mb`` 默认 50MB。"""
    try:
        mb = float(_cfg_get(cfg, "media_file_max_mb", DEFAULT_FILE_MAX_MB))
    except (TypeError, ValueError):
        mb = DEFAULT_FILE_MAX_MB
    return int(max(0.001, mb) * 1024 * 1024)


def send_interval_s(cfg: dict | None = None) -> float:
    """气泡间隔（秒）：配置毫秒钳制在 [80, 150]ms，避免抢焦点。"""
    try:
        ms = float(_cfg_get(cfg, "media_send_interval_ms", DEFAULT_INTERVAL_MS))
    except (TypeError, ValueError):
        ms = DEFAULT_INTERVAL_MS
    ms = min(float(MAX_INTERVAL_MS), max(float(MIN_INTERVAL_MS), ms))
    return ms / 1000.0


def fallback_emoji(cfg: dict | None = None) -> str:
    value = _cfg_get(cfg, "sticker_fallback_emoji", DEFAULT_FALLBACK_EMOJI)
    return str(value or DEFAULT_FALLBACK_EMOJI).strip() or DEFAULT_FALLBACK_EMOJI


def media_enabled(cfg: dict | None = None) -> bool:
    return bool(_cfg_get(cfg, "media_enabled", False))


# --------------------------------------------------------------------------- #
# 能力矩阵
# --------------------------------------------------------------------------- #
def capabilities(cfg: dict | None = None) -> dict[str, Any]:
    """当前发送层能力矩阵（wxauto4 实际能力 + ``media_enabled`` 开关）。

    ``sticker_native`` 恒为 False（wxauto4 没有贴纸 API，原生方案见 task-61 预留）；
    ``sticker_as_image`` 表示"贴纸当图片发"的降级可用。
    """
    return {
        "text": True,
        "unicode_emoji": True,      # SendMsg 直接支持 Unicode emoji 字符
        "image": True,              # SendFiles(png/jpg/...)
        "gif": True,                # SendFiles(.gif)，客户端渲染为动图
        "file": True,               # SendFiles(pdf/doc/zip/...)，失败不降级图片
        "sticker_native": False,    # 预留：wxauto4 无贴纸面板 API
        "sticker_as_image": True,   # 降级：贴纸图当图片发
        "mixed": True,              # 多气泡按顺序逐条发
        "media_enabled": media_enabled(cfg),
    }


# --------------------------------------------------------------------------- #
# 路径解析 / 校验
# --------------------------------------------------------------------------- #
def _is_inside(path: Path, dirs: Iterable[Path]) -> bool:
    try:
        target = path.resolve()
    except OSError:
        target = path
    for directory in dirs:
        try:
            target.relative_to(Path(directory).resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _candidate_paths(name_or_path: str, dirs: Iterable[Path]) -> list[Path]:
    raw = str(name_or_path or "").strip()
    if not raw:
        return []
    path = Path(raw)
    candidates: list[Path] = []
    if path.is_absolute():
        candidates.append(path)
    else:
        candidates.append(ROOT / path)
        try:
            candidates.append(Path.cwd() / path)
        except OSError:
            pass
        for directory in dirs:
            candidates.append(Path(directory) / path)
    return candidates


def find_file(name_or_path: str | Path | None, cfg: dict | None = None) -> Path | None:
    """按名字/路径在（cwd、仓库根、白名单目录）里找第一个存在的文件。"""
    if name_or_path is None:
        return None
    raw = str(name_or_path).strip()
    if not raw:
        return None
    for candidate in _candidate_paths(raw, media_dirs(cfg)):
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _path_from_item(item: dict) -> str:
    return str(item.get("path") or item.get("name") or "").strip()


def validate_item(item: Any, *, cfg: dict | None = None) -> tuple[bool, str]:
    """校验一条 media item；返回 ``(ok, error_code)``。

    error_code ∈ {not_a_dict, bad_type, missing_text, text_too_long, missing_path,
    not_found, outside_dirs, bad_ext, wxgf_unsupported, too_large, not_a_file}
    """
    if not isinstance(item, dict):
        return False, "not_a_dict"
    kind = str(item.get("type") or "").strip().lower()
    if kind not in MEDIA_TYPES:
        return False, "bad_type"
    if kind in TEXT_TYPES:
        text = str(item.get("text") or "")
        if not text.strip():
            return False, "missing_text"
        if len(text) > MAX_TEXT_CHARS:
            return False, "text_too_long"
        return True, ""
    # 文件类
    raw = _path_from_item(item)
    if not raw:
        return False, "missing_path"
    found = find_file(raw, cfg)
    if found is None:
        return False, "not_found"
    ext = found.suffix.lower()
    if ext in UNSENDABLE_EXTS:
        return False, "wxgf_unsupported"
    allowed_exts = FILE_EXTS if kind == "file" else ALLOWED_EXTS
    if ext not in allowed_exts:
        return False, "bad_ext"
    if not _is_inside(found, media_dirs(cfg)):
        return False, "outside_dirs"
    try:
        if not found.is_file():
            return False, "not_a_file"
        size = found.stat().st_size
    except OSError:
        return False, "not_found"
    limit = file_max_bytes(cfg) if kind == "file" else max_bytes(cfg)
    if size > limit:
        return False, "too_large"
    if size <= 0:
        return False, "empty_file"
    return True, ""


# --------------------------------------------------------------------------- #
# 贴纸解析（含 .wxgf 不可发）
# --------------------------------------------------------------------------- #
def resolve_sticker(name_or_path: str | Path | None, cfg: dict | None = None) -> str | None:
    """按名字/路径找贴纸文件；``.wxgf``（微信专有）不能发 → 返回 None。

    返回可发送的绝对路径字符串；找不到/不可发一律 None（调用方走 emoji/文本降级）。
    """
    found = find_file(name_or_path, cfg)
    if found is None:
        return None
    if found.suffix.lower() in UNSENDABLE_EXTS:
        return None
    if found.suffix.lower() not in ALLOWED_EXTS:
        return None
    if not _is_inside(found, media_dirs(cfg)):
        return None
    return str(found)


def suggest_sticker_alternative(name_or_path: str | Path | None,
                                cfg: dict | None = None) -> str | None:
    """``.wxgf`` 贴纸的替代品：同目录同名但扩展名可发送（.gif/.png…）。"""
    found = find_file(name_or_path, cfg)
    if found is None or found.suffix.lower() not in UNSENDABLE_EXTS:
        return None
    for ext in (".gif", ".png", ".webp", ".jpg", ".jpeg"):
        alternative = found.with_suffix(ext)
        try:
            if alternative.is_file():
                return str(alternative)
        except OSError:
            continue
    return None


def item_label(item: dict) -> str:
    """审计日志用的简短名字（贴纸名/文件名/文本前缀）。"""
    if not isinstance(item, dict):
        return ""
    kind = str(item.get("type") or "")
    if kind in TEXT_TYPES:
        return str(item.get("text") or "")[:40]
    name = str(item.get("name") or "").strip()
    if name:
        return name[:60]
    raw = _path_from_item(item)
    return Path(raw).name[:60] if raw else ""
