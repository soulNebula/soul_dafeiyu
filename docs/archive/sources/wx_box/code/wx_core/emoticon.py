"""微信表情包（贴纸）原文件解密（用户显式授权，本地离线解密）。

算法（社区公开实现：Wechat-Emoticon-Parser v4.0-plus，已本机验证）::

    key = md5(f"{seed}{wxid}EMOTICON").digest()[:16]
    文件 = AES-128-CBC(key 同时作为 IV, PKCS7) 加密

* ``seed`` = 账号级常量（就是图片密钥派生用的 cfgDword，十进制字符串）
* ``wxid`` = 账号目录名去掉 ``_xxxx`` 后缀的那部分
* 缓存位置：``<账号>/cache/<yyyy-mm>/Emoticon/<xx>/<md5>``、``<账号>/business/emoticon/{Persist,PersistStore,Thumb,...}``
* 解出的格式可能是 PNG / GIF / JPG / **wxgf**；wxgf 是微信自己的 HEVC 动画容器，
  用 ffmpeg 转成真正的动画 GIF 保存（保留原始动画帧）。
"""
from __future__ import annotations

import hashlib
import math
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

MAGICS = ((b"\x89PNG\r\n\x1a\n", "png"), (b"GIF87a", "gif"), (b"GIF89a", "gif"),
          (b"\xff\xd8\xff", "jpg"), (b"wxgf", "wxgf"), (b"RIFF", "webp"))
IMAGE_EXTS = ("png", "gif", "jpg", "bmp", "webp")


@dataclass
class Sticker:
    md5: str
    fmt: str                  # 解密后的原始格式
    media_path: Path          # 落到 media/stickers/ 的文件（wxgf 已转 gif）
    mtime: float = 0.0
    frames: list[np.ndarray] = field(default_factory=list)  # 抽样帧（32x32 RGB）用于匹配
    animated: bool = False
    size: tuple[int, int] = (0, 0)


_lock = threading.RLock()
_STICKERS: dict[str, Sticker] = {}
_INDEX_MTIME = 0.0
_INDEX_ACCOUNT = ""
_PROCESSED: dict[str, float] = {}   # 文件路径 -> 已处理的 mtime（增量解密）


def emoticon_key(seed: int, wxid: str) -> bytes:
    """表情包 AES 密钥（16 字节）。"""
    return hashlib.md5(f"{seed}{wxid}EMOTICON".encode()).digest()[:16]


def decrypt_bytes(data: bytes, key: bytes) -> bytes | None:
    """AES-128-CBC(key=IV) 解密表情包文件。"""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    if len(data) < 16:
        return None
    n = len(data) // 16 * 16
    try:
        dec = AES.new(key, AES.MODE_CBC, key).decrypt(data[:n])
    except Exception:
        return None
    try:
        dec = unpad(dec, 16)
    except ValueError:
        pass
    if n < len(data):
        dec += data[n:]
    return dec


def detect_format(data: bytes) -> str | None:
    for magic, ext in MAGICS:
        if data.startswith(magic):
            return ext
    return None


def wxgf_to_gif(data: bytes, out_gif: Path, timeout: float = 30.0) -> bool:
    """wxgf(HEVC 动画) → 动画 GIF（保留动画）。"""
    try:
        import imageio_ffmpeg

        ff = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return False
    tmp = out_gif.with_suffix(".wxgf")
    try:
        tmp.write_bytes(data)
        vf = "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
        r = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(tmp),
                            "-vf", vf, "-loop", "0", str(out_gif)],
                           capture_output=True, timeout=timeout)
        if r.returncode != 0 or not out_gif.exists() or out_gif.stat().st_size < 100:
            cut = -1
            for marker in (b"\x00\x00\x00\x01\x40\x01", b"\x00\x00\x00\x01\x42\x01"):
                i = data.find(marker)
                if i >= 0:
                    cut = i if cut < 0 else min(cut, i)
            if cut >= 0:
                tmp.write_bytes(data[cut:])
                subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(tmp),
                                "-vf", vf, "-loop", "0", str(out_gif)],
                               capture_output=True, timeout=timeout)
        return out_gif.exists() and out_gif.stat().st_size > 100
    except Exception:
        return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def wxgf_to_png(data: bytes, out_png: Path, timeout: float = 30.0) -> bool:
    """wxgf(HEVC) 图片 -> 第一帧 PNG（聊天图片用；贴纸仍走 GIF）。"""
    try:
        import imageio_ffmpeg

        ff = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return False
    tmp = out_png.with_suffix(".wxgf")
    try:
        tmp.write_bytes(data)
        r = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                            "-i", str(tmp), "-frames:v", "1", str(out_png)],
                           capture_output=True, timeout=timeout)
        if r.returncode != 0 or not out_png.exists() or out_png.stat().st_size < 100:
            cut = -1
            for marker in (bytes([0, 0, 0, 1, 0x40, 1]), bytes([0, 0, 0, 1, 0x42, 1])):
                i = data.find(marker)
                if i >= 0:
                    cut = i if cut < 0 else min(cut, i)
            if cut >= 0:
                tmp.write_bytes(data[cut:])
                subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                                "-i", str(tmp), "-frames:v", "1", str(out_png)],
                               capture_output=True, timeout=timeout)
        return out_png.exists() and out_png.stat().st_size > 100
    except Exception:
        return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _sample_frames(path: Path, count: int = 4, size: int = 32) -> tuple[list[np.ndarray], bool, tuple[int, int]]:
    """抽样几帧（32x32）用于视觉匹配；返回 (frames, animated, (w,h))。"""
    frames: list[np.ndarray] = []
    animated = False
    dims = (0, 0)
    try:
        im = Image.open(path)
        n = getattr(im, "n_frames", 1) or 1
        animated = n > 1
        dims = im.size
        picks = sorted(set([0, n // 4, n // 2, (3 * n) // 4, n - 1]))
        for idx in picks[:count + 1]:
            im.seek(min(idx, n - 1))
            fr = im.convert("RGBA").resize((size, size), Image.LANCZOS)
            base = Image.new("RGB", (size, size), (30, 30, 31))
            base.paste(fr, (0, 0), fr)
            frames.append(np.asarray(base).astype(np.float32))
    except Exception:
        return [], animated, dims
    return frames, animated, dims


def _split_container(data: bytes) -> list[bytes]:
    """拆分 PersistStore 里多个表情拼接的容器（按 PNG/GIF/JPEG 结构找结束）。"""
    out: list[bytes] = []
    i, n = 0, len(data)
    while i < n - 3:
        fmt = detect_format(data[i:])
        if fmt is None:
            i += 1
            continue
        end = None
        if fmt == "png":
            j = data.find(b"IEND", i)
            end = j + 8 if j >= 0 else None
        elif fmt == "gif":
            j = data.rfind(b"\x3b", i, min(n, i + 4 * 1024 * 1024))
            end = j + 1 if j >= 0 else None
        elif fmt == "jpg":
            j = data.find(b"\xff\xd9", i)
            end = j + 2 if j >= 0 else None
        elif fmt == "wxgf":
            nxt = None
            for k in range(i + 4, n - 3):
                if detect_format(data[k:]):
                    nxt = k
                    break
            end = nxt if nxt is not None else n
        if end is None or end - i < 16:
            i += 1
            continue
        out.append(data[i:end])
        i = end
    return out


def _sticker_files(account_dir: Path) -> list[Path]:
    files: list[Path] = []
    try:
        files += [p for p in (account_dir / "cache").rglob("Emoticon/*/*") if p.is_file()]
    except Exception:
        pass
    for sub in ("Persist", "PersistStore", "Thumb", "ThumbStore", "Temp"):
        d = account_dir / "business" / "emoticon" / sub
        if d.is_dir():
            try:
                files += [p for p in d.rglob("*") if p.is_file()]
            except Exception:
                pass
    return files


def build_index(account_dir: Path, seed: int, wxid: str, out_dir: Path,
                force: bool = False) -> dict[str, Sticker]:
    """扫描并解密账号下所有表情包，返回 {md5: Sticker}。"""
    global _INDEX_MTIME, _INDEX_ACCOUNT
    files = _sticker_files(account_dir)
    latest = max((p.stat().st_mtime for p in files), default=0.0)
    with _lock:
        if (not force and _STICKERS and latest <= _INDEX_MTIME
                and _INDEX_ACCOUNT == str(account_dir)):
            return dict(_STICKERS)
        key = emoticon_key(seed, wxid)
        out_dir.mkdir(parents=True, exist_ok=True)
        # 增量：上次账号相同才复用已有索引；只解密新/改动过的文件
        index: dict[str, Sticker] = dict(_STICKERS) if (_INDEX_ACCOUNT == str(account_dir)) else {}
        if force:
            index = {}
            _PROCESSED.clear()
        for p in files:
            if _INDEX_ACCOUNT == str(account_dir) and not force:
                try:
                    if _PROCESSED.get(str(p)) == p.stat().st_mtime:
                        continue
                except OSError:
                    continue
            try:
                mtime = p.stat().st_mtime
                raw = p.read_bytes()
            except OSError:
                continue
            dec = decrypt_bytes(raw, key)
            if dec is None:
                continue
            parts = _split_container(dec) if len(dec) > 8 else []
            if not parts:
                parts = [dec]
            for part in parts:
                fmt = detect_format(part)
                if fmt is None:
                    continue
                md5 = hashlib.md5(part).hexdigest()
                if fmt == "wxgf":
                    media = out_dir / f"{md5}.gif"
                    if not media.exists() or media.stat().st_size < 100:
                        if not wxgf_to_gif(part, media):
                            continue
                    # 同时保留微信原始容器（HEVC/wxgf），满足"原始数据"需求
                    raw_path = out_dir / f"{md5}.wxgf"
                    if not raw_path.exists():
                        raw_path.write_bytes(part)
                else:
                    media = out_dir / f"{md5}.{fmt}"
                    if not media.exists():
                        media.write_bytes(part)
                if not media.exists():
                    continue
                frames, animated, dims = _sample_frames(media)
                index[md5] = Sticker(md5=md5, fmt=fmt, media_path=media, mtime=mtime,
                                     frames=frames, animated=animated, size=dims)
        for p in files:
            try:
                _PROCESSED[str(p)] = p.stat().st_mtime
            except OSError:
                continue
        _STICKERS.clear()
        _STICKERS.update(index)
        _INDEX_MTIME = latest
        _INDEX_ACCOUNT = str(account_dir)
        return dict(index)


def match_sticker(block_img: Image.Image, bg=(30, 30, 31),
                  stickers: dict[str, Sticker] | None = None,
                  prefer_newer_within: float = 300.0,
                  used: set[str] | None = None,
                  recent_within: float = 180.0) -> Sticker | None:
    """用聊天里截到的表情块匹配库里的原文件；返回最匹配的 Sticker。

    匹配策略（从强到弱）：
    1. 时间戳绑定：新收到的贴纸通常会在几秒内落一个缓存文件 —— 如果"最近 N 秒内新增
       且没被用过的贴纸"只有一个，直接用它（实时监听下基本是 1:1 精确）；
    2. 视觉匹配：对候选（有时间候选就只在时间候选里比）算帧差，取最小；
       最高分与次高分太接近（歧义）时宁可不认，避免张冠李戴；
    3. 都没有则返回 None（不保存截图、不硬塞一个错的）。
    """
    stickers = stickers if stickers is not None else dict(_STICKERS)
    used = used if used is not None else set()
    if stickers:
        stickers = {k: v for k, v in stickers.items() if k not in used}
    if not stickers or block_img is None:
        return None
    # 把块裁紧并缩放到 32x32（合成聊天背景）
    rgb = np.asarray(block_img.convert("RGB")).astype(np.int16)
    diff = np.abs(rgb - np.array(bg, dtype=np.int16)).max(axis=2)
    if diff.max() <= 26:
        return None
    ys, xs = np.where(diff > 26)
    crop = block_img.crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    target = crop.convert("RGB").resize((32, 32), Image.LANCZOS)
    tarr = np.asarray(target).astype(np.float32)
    now = time.time()
    fresh = [st for st in stickers.values() if now - st.mtime <= recent_within]
    pool = fresh or list(stickers.values())
    scored: list[tuple[float, Sticker]] = []
    for st in pool:
        if not st.frames:
            continue
        score = min(float(np.mean((fr - tarr) ** 2)) for fr in st.frames)
        if now - st.mtime <= prefer_newer_within:
            score *= 0.85
        scored.append((score, st))
    if not scored:
        return None
    # 时间戳强绑定：最近新增且唯一 -> 直接认定（实时监听下的 1:1 情形）
    if len(fresh) == 1:
        st = fresh[0]
        # 宽高比不要太离谱（贴纸显示比例基本保持）
        if st.size[0] and st.size[1]:
            want = crop.width / max(1, crop.height)
            have = st.size[0] / st.size[1]
            if have > 0 and abs(math.log(max(1e-3, want / have))) <= 0.45:
                return st
    scored.sort(key=lambda x: x[0])
    best_score, best = scored[0]
    # 歧义保护：第二候选和第一差不多时，不猜
    if len(scored) > 1 and scored[1][0] < best_score * 1.25 and scored[1][0] - best_score < 900:
        return None
    limit = 5200.0 if best.animated else 2600.0
    if best_score > limit:
        return None
    return best
