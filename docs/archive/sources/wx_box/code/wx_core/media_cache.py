"""微信本地缓存媒体获取（Lead 维护）。

思路：不读微信进程内存、不注入、不 Hook，只读取**当前登录用户自己的**微信数据目录
（``Documents\\xwechat_files\\<wxid>\\...``）：

* **文档**：``msg/file/<yyyy-mm>/<原始文件名>`` 是**明文**保存的，直接复制即可拿到原始文件。
* **图片 / 表情包**：4.x 缓存为加密的 ``.dat`` / 无扩展名文件（V2 格式，AES+异或），
  没有密钥无法还原；本模块先提供"探测/找最近文件"能力，等用户确认是否允许
  从微信进程内存读取一次性密钥后再接入解密（见 docs/风控与合规说明.md）。

红线：本模块只读微信数据目录，绝不写入、删除、改名；所有产出都写到自己项目的 media/ 下。
"""
from __future__ import annotations

import glob
import hashlib
import math
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

# 微信数据根目录候选（4.x 默认在 文档 下；也兼容自定义安装）
ROOT_CANDIDATES = [
    Path(os.path.expanduser("~")) / "Documents" / "xwechat_files",
    Path(os.path.expanduser("~")) / "xwechat_files",
    Path("D:/xwechat_files"),
    Path("E:/xwechat_files"),
]

DOC_EXTS = (
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv", "zip", "rar", "7z",
    "tar", "gz", "mp3", "mp4", "wav", "avi", "mov", "mkv", "apk", "exe", "msi", "psd", "ai",
    "dwg", "dxf", "py", "js", "ts", "json", "xml", "yaml", "yml", "html", "css", "sql", "db",
)

_IMG_MAGICS = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"RIFF": "webp",
    b"BM": "bmp",
    b"wxgf": "hevc",
}


_DEC_CACHE: dict[str, dict | None] = {}


@dataclass
class CacheFile:
    path: Path
    mtime: float
    size: int


def discover_roots() -> list[Path]:
    """返回所有存在的微信数据根目录。"""
    out: list[Path] = []
    seen: set[str] = set()
    for p in ROOT_CANDIDATES:
        try:
            key = str(p).lower()
            if key in seen:
                continue
            if p.is_dir():
                out.append(p)
                seen.add(key)
        except Exception:
            continue
    return out


def account_dirs() -> list[Path]:
    """返回所有账号目录（``xwechat_files/wxid_xxx_xxxx``），按真实路径去重。"""
    out: list[Path] = []
    seen: set[str] = set()
    for root in discover_roots():
        try:
            for d in root.iterdir():
                if d.is_dir() and d.name.lower().startswith("wxid_"):
                    key = str(d.resolve()).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(d)
        except Exception:
            continue
    return out


def active_account_dir() -> Path | None:
    """当前活跃账号目录：最近有 db wal 改动 / 缓存文件改动的那个。"""
    best: tuple[float, Path] | None = None
    for acc in account_dirs():
        latest = 0.0
        # 注意：不 stat 任何微信数据库文件（db_storage 属于禁止范围）
        for pat in ("cache/*/*/*", "msg/attach/*/*/*", "msg/file/*/*"):
            try:
                for f in glob.iglob(str(acc / pat), recursive=False):
                    try:
                        latest = max(latest, os.path.getmtime(f))
                    except OSError:
                        continue
            except Exception:
                continue
        if best is None or latest > best[0]:
            best = (latest, acc)
    if best is None:
        return None
    # 有多个账号目录时取最近有动静的那个（切换账号后新账号的活动文件更新）；
    # 即使闲置也返回它，交给调用方用"数据目录名/内存 cfg"进一步校验。
    return best[1]


def _newest_files(patterns: list[str], limit: int = 20) -> list[CacheFile]:
    files: list[CacheFile] = []
    for acc in account_dirs():
        for pat in patterns:
            try:
                for f in glob.iglob(str(acc / pat)):
                    try:
                        st = os.stat(f)
                    except OSError:
                        continue
                    if os.path.isfile(f):
                        files.append(CacheFile(Path(f), st.st_mtime, st.st_size))
            except Exception:
                continue
    files.sort(key=lambda x: x.mtime, reverse=True)
    return files[:limit]


def recent_documents(limit: int = 20) -> list[CacheFile]:
    """最近接收/发送的文档（明文，可直接复制）。"""
    return _newest_files(["msg/file/*/*"], limit)


def recent_images(limit: int = 30) -> list[CacheFile]:
    """最近收发的图片缓存（.dat，V2 加密）。"""
    return _newest_files(["msg/attach/*/*/Img/*.dat"], limit)


def recent_emoticons(limit: int = 30) -> list[CacheFile]:
    """最近使用的表情包缓存。"""
    return _newest_files(["cache/*/Emoticon/*/*", "msg/attach/*/*/emoji/*"], limit)


def find_document(name: str, limit: int = 200, max_age: float = 3600.0) -> Path | None:
    """按文件卡片上的文件名找微信缓存里的原始文档。

    微信聊天里文件名可能被截断（如显示 ``2026-10-19.xlsx``，实际是
    ``BOM_Board1_Schematic1_2026-10-19.xlsx``），所以先精确匹配，再按
    "同扩展名 + 最近修改 + 名字相似度" 模糊匹配。
    """
    if not name:
        return None
    target = os.path.basename(name).strip().lower()
    if not target:
        return None
    files = _newest_files(["msg/file/*/*"], limit=limit)
    now = time.time()
    for f in files:
        if f.path.name.lower() == target:
            return f.path
    # 模糊匹配：同名后缀、较新、名字相似
    import difflib

    suffix = os.path.splitext(target)[1]
    best: tuple[float, Path] | None = None
    for f in files:
        if suffix and f.path.suffix.lower() != suffix:
            continue
        if max_age > 0 and now - f.mtime > max_age:
            continue
        stem_target = os.path.splitext(target)[0]
        stem_file = f.path.stem.lower()
        ratio = difflib.SequenceMatcher(None, stem_target, stem_file).ratio()
        # 截断显示时文件名往往是"尾部片段"，所以再用"包含"加成
        if stem_target and (stem_target in stem_file or stem_file.endswith(stem_target)):
            ratio = max(ratio, 0.8)
        if ratio >= 0.45 and (best is None or ratio > best[0]):
            best = (ratio, f.path)
    return best[1] if best else None


def copy_document(name: str, dest_dir: Path, max_age: float = 3600.0) -> Path | None:
    """把对应的原始文档复制到 dest_dir（保留原文件名），返回新路径。"""
    src = find_document(name, max_age=max_age)
    if src is None:
        return None
    try:
        if max_age > 0 and time.time() - src.stat().st_mtime > max_age:
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        base = src.name
        dest = dest_dir / base
        i = 1
        while dest.exists():
            dest = dest_dir / f"{src.stem}_{i}{src.suffix}"
            i += 1
        shutil.copy2(src, dest)
        return dest
    except Exception:
        return None


def sniff_image_type(data: bytes) -> str:
    """根据文件头判断图片类型（用于解密后确认格式）。"""
    for magic, ext in _IMG_MAGICS.items():
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def sha1_file(path: Path, limit: int = 1 << 20) -> str:
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            h.update(f.read(limit))
    except Exception:
        return ""
    return h.hexdigest()


# ---------------- 图片缓存解密（需要用户提供的 xor/AES 密钥） ----------------

def _recent_encrypted_images(max_age: float = 180.0, limit: int = 40) -> list[CacheFile]:
    """当前活跃账号最近的图片/表情包 .dat（排除缩略图 _t，优先原图/大图）。"""
    acc = active_account_dir()
    roots = [acc] if acc else account_dirs()
    out: list[CacheFile] = []
    now = time.time()
    for root in roots:
        for pat in ("msg/attach/*/*/Img/*.dat", "msg/attach/*/*/emoji/*.dat"):
            for f in glob.iglob(str(root / pat)):
                try:
                    st = os.stat(f)
                except OSError:
                    continue
                if now - st.st_mtime > max_age:
                    continue
                if os.path.basename(f).endswith("_t.dat"):
                    continue
                out.append(CacheFile(Path(f), st.st_mtime, st.st_size))
    out.sort(key=lambda x: x.mtime, reverse=True)
    return out[:limit]


def _stable_stem(name: str) -> str:
    for suf in ("_t.dat", "_h.dat", ".dat"):
        if name.endswith(suf):
            return name[: -len(suf)]
    return name


def _decrypt_candidates(xor_key: int, aes_key: str, max_age: float) -> list[dict]:
    """解密最近所有新到的图片缓存，返回 [{stem, path, ext, size, aspect, mtime}]（带进程内缓存）。"""
    from . import dat_decrypt

    global _DEC_CACHE
    out: dict[str, dict] = {}
    groups: dict[str, list[CacheFile]] = {}
    for cf in _recent_encrypted_images(max_age=max_age):
        groups.setdefault(_stable_stem(cf.path.name), []).append(cf)
    for stem, files in groups.items():
        key = stem.lower()
        if key in _DEC_CACHE:
            item = _DEC_CACHE[key]
            if item:
                out[key] = item
            continue
        def rank(cf: CacheFile) -> int:
            if cf.path.name.endswith("_h.dat"):
                return 0
            if cf.path.name.endswith("_t.dat"):
                return 2
            return 1

        files.sort(key=rank)
        best = None
        for cf in files:
            try:
                data, ext = dat_decrypt.decrypt_file_auto(cf.path, xor_key, aes_key)
            except Exception:
                continue
            if ext not in ("png", "jpg", "gif", "bmp", "webp"):
                continue
            try:
                from PIL import Image
                import io as _io

                im = Image.open(_io.BytesIO(data))
                im.load()
                w, h = im.size
            except Exception:
                continue
            aspect = w / max(1, h)
            best = {"stem": stem, "data": data, "ext": ext, "w": w, "h": h,
                    "aspect": aspect, "mtime": cf.mtime}
            break
        _DEC_CACHE[key] = best
        if len(_DEC_CACHE) > 400:
            for k in list(_DEC_CACHE)[:200]:
                _DEC_CACHE.pop(k, None)
        if best:
            out[key] = best
    return list(out.values())


def decrypt_newest_image(xor_key: int, aes_key: str, out_dir: Path, *,
                         used: set[str] | None = None, aspect: float | None = None,
                         max_age: float = 86400.0, recent_within: float = 60.0,
                         aspect_tolerance: float = 0.2) -> Path | None:
    """挑一张未处理过的缓存图片解密并落盘为原始格式，返回路径。

    ``aspect``：聊天里这条消息气泡的宽高比，用来在多张候选图里挑最匹配的那张
    （历史回放时没有精确时间戳，宽高比是很强的匹配信号）。
    """
    used = used if used is not None else set()
    cands = _decrypt_candidates(xor_key, aes_key, max_age)
    now = time.time()
    fresh = [c for c in cands if now - c["mtime"] <= recent_within]
    pool = fresh or cands
    if aspect and not fresh:
        # 历史回放没有时间信号：用气泡宽高比过滤，太不匹配的宁可不认（回退截图）
        pool = [c for c in pool
                if c["aspect"] and abs(math.log(max(1e-3, c["aspect"] / aspect))) <= aspect_tolerance]
    if aspect:
        pool.sort(key=lambda c: abs(math.log(max(1e-3, c["aspect"] / aspect))) if c["aspect"] else 9)
    else:
        pool.sort(key=lambda c: c["mtime"], reverse=True)
    for c in pool:
        if c["stem"].lower() in used:
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"{c['stem']}.{c['ext']}"
        i = 1
        while dest.exists():
            dest = out_dir / f"{c['stem']}_{i}.{c['ext']}"
            i += 1
        try:
            dest.write_bytes(c["data"])
        except Exception:
            continue
        used.add(c["stem"].lower())
        return dest
    return None
