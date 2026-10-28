"""微信内置小黄脸 Emoji 识别（😊 → 微笑）。

素材：WeChat 内置 109 个表情 PNG（来自 npm 包 ``wechat-emojis``，见 docs/README 版权说明），
放在 ``wx_core/emoji_assets/``。聊天里的 emoji 是渲染出来的小图标，
把它裁紧、与素材逐像素比较（各自合成到同一背景色）即可识别出中文名。
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from PIL import Image

ASSET_DIR = Path(__file__).resolve().parent / "emoji_assets"
SIZE = 28
MAX_MSE = 3600.0

_lock = threading.Lock()
_assets: list[tuple[str, Image.Image]] | None = None


def _load() -> list[tuple[str, Image.Image]]:
    global _assets
    if _assets is not None:
        return _assets
    out: list[tuple[str, Image.Image]] = []
    if ASSET_DIR.is_dir():
        for p in sorted(ASSET_DIR.glob("*.png")):
            try:
                out.append((p.stem, Image.open(p).convert("RGBA")))
            except Exception:
                continue
    _assets = out
    return out


def _corner_bg(img: Image.Image):
    """取块里的"底色"：出现次数最多的颜色（表情块通常是气泡底色/聊天底色）。"""
    a = np.asarray(img.convert("RGB")).reshape(-1, 3)
    if a.size == 0:
        return (30, 30, 31)
    colors, counts = np.unique(a, axis=0, return_counts=True)
    return tuple(int(v) for v in colors[int(counts.argmax())])


def _tight(img: Image.Image, bg, tol: int = 24) -> Image.Image | None:
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    diff = np.abs(a - np.array(bg, dtype=np.int16)).max(axis=2)
    mask = diff > tol
    if not mask.any():
        return None
    ys, xs = np.where(mask)
    return img.crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))


def match(img: Image.Image, bg=None) -> str | None:
    """识别 emoji 图标，返回中文名（不含方括号）；失败返回 None。"""
    assets = _load()
    if not assets or img is None or img.width < 12 or img.height < 12:
        return None
    bg = bg or _corner_bg(img)
    crop = _tight(img, bg)
    if crop is None or crop.width < 10 or crop.height < 10:
        return None
    ratio = crop.width / max(1, crop.height)
    if not (0.6 <= ratio <= 1.6):
        return None
    with _lock:
        target = np.asarray(crop.convert("RGB").resize((SIZE, SIZE), Image.LANCZOS)).astype(np.float32)
        best_score = 1e18
        best_name = None
        for name, asset in assets:
            a = asset.resize((SIZE, SIZE), Image.LANCZOS)
            base = Image.new("RGB", (SIZE, SIZE), bg)
            base.paste(a, (0, 0), a)
            arr = np.asarray(base).astype(np.float32)
            score = float(np.mean((arr - target) ** 2))
            if score < best_score:
                best_score = score
                best_name = name
        if best_name and best_score <= MAX_MSE:
            return best_name
        return None


def match_with_score(img: Image.Image, bg=None):
    """调试：返回 (名字, 分数)。"""
    assets = _load()
    if not assets or img is None:
        return None, None
    bg = bg or _corner_bg(img)
    crop = _tight(img, bg)
    if crop is None:
        return None, None
    target = np.asarray(crop.convert("RGB").resize((SIZE, SIZE), Image.LANCZOS)).astype(np.float32)
    best = (1e18, None)
    for name, asset in assets:
        a = asset.resize((SIZE, SIZE), Image.LANCZOS)
        base = Image.new("RGB", (SIZE, SIZE), bg)
        base.paste(a, (0, 0), a)
        score = float(np.mean((np.asarray(base).astype(np.float32) - target) ** 2))
        if score < best[0]:
            best = (score, name)
    return best[1], best[0]
