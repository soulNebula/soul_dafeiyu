"""媒体落盘与消息日志（Lead 维护）。

只写项目自己的 media/ 与 log/ 目录，绝不写微信目录。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
MEDIA_DIR = ROOT / "media"
LOG_DIR = ROOT / "log"
MESSAGES_LOG = LOG_DIR / "messages.jsonl"


def ensure_dirs() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _safe_name(name: str, limit: int = 24) -> str:
    keep = []
    for ch in name:
        if ch.isalnum() or ch in "-_":
            keep.append(ch)
        elif ch in " .":
            keep.append("_")
    s = "".join(keep)[:limit]
    return s or "friend"


def save_image(friend: str, kind: str, img: Image.Image, frames: list[Image.Image] | None = None) -> str:
    """保存图片/表情包（动图会合成 GIF），返回相对项目根目录的路径。"""
    ensure_dirs()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = f"{stamp}_{_safe_name(friend)}_{kind}"
    path = MEDIA_DIR / f"{base}.png"
    i = 1
    while path.exists():
        path = MEDIA_DIR / f"{base}_{i}.png"
        i += 1
    try:
        img = img.convert("RGB")
        img.save(path)
    except Exception:
        return ""
    if frames:
        gif_path = path.with_suffix(".gif")
        try:
            fr = [f.convert("P", palette=Image.Palette.ADAPTIVE) for f in frames]
            fr[0].save(gif_path, save_all=True, append_images=fr[1:], duration=120, loop=0)
            try:
                path.unlink()
            except OSError:
                pass
            return _rel(gif_path)
        except Exception:
            return _rel(path)
    return _rel(path)


def save_file_thumb(friend: str, img: Image.Image) -> str:
    return save_image(friend, "file", img)


def rel(p: Path) -> str:
    """项目相对路径（GUI/日志用，统一正斜杠）。"""
    return _rel(p)


def _rel(p: Path) -> str:
    try:
        resolved = Path(p).resolve()
        root = ROOT.resolve()
        if root not in resolved.parents and resolved != root:
            return str(resolved)  # 项目外（如历史同步的临时目录）直接给绝对路径
        return os.path.relpath(resolved, root).replace("\\", "/")
    except Exception:
        return str(p)


def append_message(msg) -> None:
    """把消息追加到 log/messages.jsonl（一行一条，方便用户自己分析）。"""
    ensure_dirs()
    try:
        with open(MESSAGES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")
    except Exception:
        pass


def cleanup(keep_days: int = 7) -> int:
    """清理超过 keep_days 天的媒体文件，返回删除数量。"""
    if keep_days <= 0:
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    if not MEDIA_DIR.exists():
        return 0
    for p in MEDIA_DIR.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except Exception:
            continue
    return removed
