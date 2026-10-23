# -*- coding: utf-8 -*-
"""微信 4.x 朋友圈媒体（图片 / 头像 / 视频封面）本地缓存定位与解密。

对外签名（见 ``wx4/API.md`` 2.3）::

    image_path(md5, account_dir=None)   -> Path | None
    avatar_path(username)               -> Path | None
    video_path(media, account_dir=None) -> Path | None
    cover_path(md5)                     -> Path | None

所有解密产物写到 ``WechatMoments/.wx4_cache/``（自动建目录），**只读**微信目录。

## 实测事实（微信 4.1.15.56，2026-10 recon 结论，别重复踩坑）

### 1. 朋友圈图片缓存位置

``<账号目录>/cache/<YYYY-MM>/Sns/Img/<2位>/<30位hex>``，无扩展名，内容为 4.x
加密 ``.dat``（V2 容器）。**文件名不是 xml ``url@md5``，也不是 URL 的 md5**：

* 实测 7 条 feed 共 14 个 ``url@md5``（32 位 hex），15 个缓存文件名是 **30 位 hex**，
  按「md5 前 2 位作目录 + 其余作文件名」「md5[:30]」「md5[-30:]」「md5(URL)/md5(thumb
  URL)，含去 query、去尺寸段、UTF-16、sha1/sha256/blake2、属性组合、下载 URL 拼接」
  等 200+ 种推导全部 **0 命中**；sns.db 及全部 20 个库里也搜不到缓存文件名。
  → 推断 4.x 用**下载时的完整 CDN URL（含当时服务端下发的 token/encfilekey）**
  做哈希命名，token 每次刷新都会变，**事后无法由 xml 反推**。
* 因此本模块的 ``image_path(md5)`` 用**内容/尺寸匹配**：
  1. 先按文件名直查（兼容将来版本真用 md5 命名的情况）；
  2. 用 ``wx4.sns`` 的原始 xml 取 ``url@md5 -> (width, height)``（``<size>``），
     再扫 ``cache/*/Sns/Img``，解密后按尺寸精确匹配；
  3. 结果按缓存文件名稳定排序，保证同一次导出内一一对应（尺寸重复时不会乱跳）。
  实测：第一页 feed 的 4 张图 1280x990 / 1050x862 / 1440x1267 / 1188x1324
  与缓存里 4 个文件尺寸**完全一致**，可唯一命中。
* 缓存里还混着 240x240 / 200x223 之类**缩略图**（``<thumb>``），尺寸与 ``<size>``
  不同；``image_path(..., allow_thumb=True)`` 时按宽高比兜底（默认关，避免错图）。
* 加密细节见 :mod:`wx4.dat`：V2 容器，AES 段要**去 PKCS#7 填充**；
  ``xor_key`` 是**十进制 82（0x52）**，不是 0x82。

### 2. 头像

4.x 头像**不走文件缓存**，而是存在 ``<账号目录>/db_storage/head_image/head_image.db``
（SQLCipher）的 ``head_image(username TEXT PRIMARY KEY, md5 TEXT, image_buffer BLOB,
update_time INTEGER)`` 表里：``image_buffer`` 就是**明文 JPEG/PNG**（实测 132x132）。
另外 ``<账号目录>/temp/head_image/<md5>`` 是下载中转的明文文件（只对最近几个人存在），
``xwechat_files/all_users/head_imgs/<u32>/<u32>`` 是 720x720/940x940 大图，但目录名
与 md5/username 无已知映射（实测 crc32/fnv/djb2/sdbm/murmur3/md5 片段全不命中），
不作为主路径。所以：

1. ``head_image.db`` 的 ``image_buffer``（主路径，经 ``wx4.db`` 解密库）；
2. ``contact.db.head_img_md5`` → ``temp/head_image/<md5>`` 明文文件（兜底）；
3. 都没有 → ``None``（微信本地没缓存该联系人头像）。

### 3. 视频

**朋友圈视频在本机没有任何落盘缓存**（实测结论）：

* 查过：``<账号目录>/cache/*/Sns/``（只有 ``Img/``、``Temp/``，没有 ``Video/``，
  ``Sns/Temp`` 为空）、``<账号目录>/msg/video/``（**空目录**）、
  ``<账号目录>/msg/attach/*/*/``（只有 ``Img``）、``<账号目录>/business/``、
  ``temp/``。
* 聊天/朋友圈小视频在 4.x 的真实位置是
  ``<账号目录>/msg/video/<YYYY-MM>/<32位md5>.mp4``，同目录还有
  同名 ``.jpg``（封面）和 ``<md5>_raw.mp4``（原始未压缩版本）。**但**用另一账号
  （398 条 feed、48 个朋友圈视频 ``url@md5``）与本地 148 个 ``msg/video`` 文件比对，
  **0 命中**；本地文件名 md5 也不出现在该账号任何 feed 的 xml md5 里。
  → 朋友圈视频是**边下边播/看完即删**，只有主动保存/聊天里的视频才留在
  ``msg/video``；朋友圈视频的临时文件在 ``cache/<月>/Sns/Temp``（本机为空）。
* 所以 ``video_path(media)`` 按「media md5 → msg/video 同名文件」查找（对已保存的
  聊天视频和将来微信把朋友圈视频缓存下来的版本有效），找不到返回 ``None``；
  ``cover_path(md5)`` 查同名 ``.jpg``（视频封面）。
  集成层拿不到视频时应该降级（不渲染 ``<video>`` / 只显示封面或占位），不要报错。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

try:
    from . import Wx4Error
except ImportError:  # pragma: no cover - 独立脚本导入兜底

    class Wx4Error(RuntimeError):  # type: ignore[no-redef]
        pass

from . import dat, image_key

# ---------------------------------------------------------------------------
# 目录与常量
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_ROOT = PROJECT_ROOT / ".wx4_cache"
IMG_DIR = CACHE_ROOT / "img"
AVATAR_DIR = CACHE_ROOT / "avatar"
VIDEO_DIR = CACHE_ROOT / "video"
INDEX_FILE = CACHE_ROOT / "sns_img_index.json"

_IMAGE_EXTS = ("jpg", "png", "gif", "bmp", "webp")
_AVATAR_EXTS = ("jpg", "png", "gif", "webp", "bmp")
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_HEX30 = re.compile(r"^[0-9a-fA-F]{30}$")
_INDEX_VERSION = 1

_lock = threading.RLock()
_index_cache: dict[str, dict[str, dict]] = {}      # account -> {cache_name: entry}
_size_map_cache: dict[str, dict[str, tuple[int, int]]] = {}  # account -> {md5: (w,h)}


def cache_root() -> Path:
    """``WechatMoments/.wx4_cache``（按需创建）。"""
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    return CACHE_ROOT


# ---------------------------------------------------------------------------
# 账号目录定位
# ---------------------------------------------------------------------------


def account_dir(account_dir: Path | str | None = None) -> Path:
    """解析账号目录：显式参数 → ``wx4.db`` 认定的账号 → 缓存最新的账号目录。"""
    if account_dir is not None:
        p = Path(account_dir)
        if not p.is_dir():
            raise Wx4Error(f"账号目录不存在：{p}")
        return p
    try:
        from .db import get_db

        p = Path(get_db().account_dir)
        if p.is_dir():
            return p
    except Exception:
        pass
    picked = image_key.pick_account_dir()
    if picked is not None:
        return picked
    root = image_key.default_data_root()
    raise Wx4Error(f"找不到微信 4.x 账号目录（{root}）；请启动并登录微信 4.x")


def _resolve_acc(value: Path | str | None) -> Path:
    """内部用：解析账号目录（避免参数名 ``account_dir`` 遮蔽同名函数）。"""
    return account_dir(value)


# ---------------------------------------------------------------------------
# 图片：解密 + 落盘缓存
# ---------------------------------------------------------------------------


def _decrypt_image_bytes(raw: bytes) -> tuple[bytes, str]:
    """把缓存文件内容解成图片明文，返回 ``(明文, 扩展名)``；失败抛 ``Wx4Error``。"""
    keys = image_key.get_image_keys()
    try:
        plain, ext = dat.decrypt_auto(raw, xor_key=keys["xor_key"], aes_key=keys["aes_key"])
    except dat.DatDecryptError as exc:
        raise Wx4Error(f"图片解密失败：{exc}") from exc
    if ext not in _IMAGE_EXTS:
        raise Wx4Error("图片解密结果不是已知图片格式（密钥可能不对）")
    return plain, ext


def _write_image_cache(md5: str, plain: bytes, ext: str) -> Path:
    """把解密后的图片写到 ``.wx4_cache/img/<md5>.<ext>``（已存在则直接复用）。"""
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    name = md5 or hashlib.md5(plain).hexdigest()
    dest = IMG_DIR / f"{name}.{ext}"
    if dest.is_file():
        try:
            if dest.stat().st_size == len(plain):
                return dest
        except OSError:
            pass
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(plain)
    tmp.replace(dest)
    return dest


def _decrypt_cache_file(path: Path, md5: str = "") -> Path | None:
    """解密一个缓存文件并返回明文图片路径；失败返回 ``None``。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        plain, ext = _decrypt_image_bytes(raw)
    except Wx4Error:
        return None
    return _write_image_cache(md5 or hashlib.md5(raw).hexdigest(), plain, ext)


def _sns_img_files(account: Path) -> list[Path]:
    """``cache/<月>/Sns/Img/<2位>/<30位>`` 下所有缓存文件（按名字排序）。"""
    out: list[Path] = []
    for base in sorted((account / "cache").glob("*/Sns/Img")):
        try:
            for sub in base.iterdir():
                if not sub.is_dir():
                    continue
                for f in sub.iterdir():
                    if f.is_file():
                        out.append(f)
        except OSError:
            continue
    out.sort(key=lambda p: p.name)
    return out


def _image_size_of(plain: bytes) -> tuple[int, int] | None:
    """读图片宽高（只用文件头，不完整解码，成本低）。"""
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(plain)) as im:
            return int(im.size[0]), int(im.size[1])
    except Exception:
        return None


def sns_img_index(account: Path | str | None = None, refresh: bool = False) -> dict[str, dict]:
    """朋友圈图片缓存索引：``{缓存文件名: {path, w, h, ext, mtime, size}}``。

    * 索引落盘到 ``.wx4_cache/sns_img_index.json``，按 ``(size, mtime)`` 增量失效；
    * 每个条目需要解密一次（单文件 ~1-3ms，微信目前只缓存几 MB，可忽略）；
    * 解密密钥拿不到时返回空索引（不抛错，让调用方走 None 降级）。
    """
    acc = account_dir(account) if not isinstance(account, Path) else account
    key = str(acc)
    with _lock:
        if not refresh and key in _index_cache:
            return _index_cache[key]

        stored = _load_index_json(key)
        entries: dict[str, dict] = {}
        for f in _sns_img_files(acc):
            try:
                st = f.stat()
            except OSError:
                continue
            old = stored.get(f.name)
            if (not refresh and old and old.get("size") == st.st_size
                    and abs(float(old.get("mtime", 0)) - st.st_mtime) < 1e-6
                    and old.get("path") == str(f)):
                entries[f.name] = old
                continue
            try:
                raw = f.read_bytes()
            except OSError:
                continue
            try:
                plain, ext = _decrypt_image_bytes(raw)
            except Wx4Error:
                continue
            wh = _image_size_of(plain)
            if wh is None:
                continue
            entries[f.name] = {
                "path": str(f), "w": wh[0], "h": wh[1], "ext": ext,
                "mtime": st.st_mtime, "size": st.st_size,
            }
        _index_cache[key] = entries
        _save_index_json(key, entries)
        return entries


def _load_index_json(account_key: str) -> dict[str, dict]:
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _INDEX_VERSION:
        return {}
    files = data.get("accounts", {}).get(account_key, {})
    return files if isinstance(files, dict) else {}


def _save_index_json(account_key: str, entries: dict[str, dict]) -> None:
    try:
        cache_root()
        try:
            data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != _INDEX_VERSION:
                data = {"version": _INDEX_VERSION, "accounts": {}}
        except (OSError, ValueError):
            data = {"version": _INDEX_VERSION, "accounts": {}}
        data.setdefault("accounts", {})[account_key] = entries
        tmp = INDEX_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(INDEX_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 图片：md5 -> (width, height)（来自朋友圈 xml 的 <size>）
# ---------------------------------------------------------------------------

_MEDIA_BLOCK_RE = re.compile(r"<media>(.*?)</media>", re.S)
_URL_MD5_RE = re.compile(r'<url\b[^>]*\bmd5="([0-9a-fA-F]{32})"')
_SIZE_RE = re.compile(r'<size\b[^>]*\bwidth="(\d+)"[^>]*\bheight="(\d+)"')


def _media_size_from_xml(xml: str) -> list[tuple[str, tuple[int, int]]]:
    """从一条朋友圈 xml 里提取 ``[(url@md5, (width, height)), ...]``。"""
    out: list[tuple[str, tuple[int, int]]] = []
    for block in _MEDIA_BLOCK_RE.findall(xml or ""):
        m = _URL_MD5_RE.search(block)
        s = _SIZE_RE.search(block)
        if not (m and s):
            continue
        try:
            out.append((m.group(1).lower(), (int(s.group(1)), int(s.group(2)))))
        except ValueError:
            continue
    return out


def md5_sizes(account: Path | str | None = None, refresh: bool = False) -> dict[str, tuple[int, int]]:
    """``{url@md5: (width, height)}``：给 ``image_path`` 做尺寸匹配用。

    数据来自 ``wx4.sns``（4.x 时间线 xml 的 ``<size>``）；sns 层不可用时返回空 dict，
    ``image_path`` 就只剩文件名直查（会 miss，调用方按 None 降级）。
    """
    acc = account_dir(account) if not isinstance(account, Path) else account
    key = str(acc)
    with _lock:
        if not refresh and key in _size_map_cache:
            return _size_map_cache[key]
        mapping: dict[str, tuple[int, int]] = {}
        try:
            from . import sns

            for moment in sns.all_moments():
                for md5, wh in _media_size_from_xml(getattr(moment, "raw_xml", "") or ""):
                    mapping.setdefault(md5, wh)
        except Exception:
            mapping = {}
        _size_map_cache[key] = mapping
        return mapping


# ---------------------------------------------------------------------------
# 图片：对外入口
# ---------------------------------------------------------------------------


def _pick_by_size(entries: dict[str, dict], size: tuple[int, int]) -> list[str]:
    """按尺寸精确匹配，返回按文件名排序的候选缓存名列表。"""
    w, h = int(size[0]), int(size[1])
    names = [n for n, e in entries.items() if int(e.get("w", 0)) == w and int(e.get("h", 0)) == h]
    names.sort()
    return names


def _pick_by_thumb(entries: dict[str, dict], size: tuple[int, int], tolerance: float = 0.03) -> list[str]:
    """缩略图兜底：宽高比相近且尺寸不超过原图的候选（按面积从大到小）。"""
    tw, th = max(1, int(size[0])), max(1, int(size[1]))
    want = tw / th
    cands = []
    for name, e in entries.items():
        w, h = int(e.get("w", 0)), int(e.get("h", 0))
        if w <= 0 or h <= 0:
            continue
        if w > tw or h > th:      # 缩略图应该比原图小
            continue
        ratio = w / h
        if abs(ratio - want) / want <= tolerance:
            cands.append((w * h, name))
    cands.sort(reverse=True)
    return [name for _area, name in cands]


def image_path(
    md5: str,
    account_dir: Path | str | None = None,
    *,
    size: tuple[int, int] | None = None,
    allow_thumb: bool = False,
) -> Path | None:
    """朋友圈图片：由媒体 md5 找到并解密出本地图片，返回明文图片路径。

    Args:
        md5: 朋友圈 xml 里 ``<url md5="...">`` 的 32 位 md5（``Moment.media_md5s()``）。
        account_dir: 账号目录；None = 自动（``wx4.db`` / 缓存最新账号）。
        size: 可选 ``(width, height)``（xml ``<size>``）。显式传入可跳过
            「md5 → 尺寸」查询；拿不到时会自动从 ``wx4.sns`` 反查。
        allow_thumb: 尺寸精确匹配不到时，是否按宽高比回退到缩略图（默认 False，
            避免把别的图当成本图；集成层若宁可有图也不想空着可传 True）。

    Returns:
        解密后图片路径（``.wx4_cache/img/<md5>.<ext>``）；没有本地缓存则 ``None``。
    """
    md5 = (md5 or "").strip().lower()
    if not md5:
        return None
    acc = _resolve_acc(account_dir)

    # 1) 文件名直查（兼容将来微信真用 md5 命名的版本）
    try:
        for base in (acc / "cache").glob("*/Sns/Img"):
            for cand in (base / md5[:2] / md5, base / md5[:2] / md5[:30], base / md5[:2] / md5[-30:]):
                if cand.is_file():
                    got = _decrypt_cache_file(cand, md5)
                    if got is not None:
                        return got
    except OSError:
        pass

    # 2) 尺寸匹配
    entries = sns_img_index(acc)
    if not entries:
        return None
    wh = size
    if wh is None:
        wh = md5_sizes(acc).get(md5)
    if wh is None:
        return None
    for name in _pick_by_size(entries, wh):
        got = _decrypt_cache_file(Path(entries[name]["path"]), md5)
        if got is not None:
            return got
    # 3) 缩略图兜底（可选）
    if allow_thumb:
        for name in _pick_by_thumb(entries, wh):
            got = _decrypt_cache_file(Path(entries[name]["path"]), md5)
            if got is not None:
                return got
    return None


def thumb_path(md5: str, account_dir: Path | str | None = None,
               *, size: tuple[int, int] | None = None) -> Path | None:
    """朋友圈图片缩略图（``<thumb>`` 缓存，尺寸小于原图）：找不到返回 ``None``。

    与 :func:`image_path` 的区别是**只**接受比原图小的宽高比匹配候选，
    适合「原图没缓存、但列表缩略图在」的场景。
    """
    md5 = (md5 or "").strip().lower()
    if not md5:
        return None
    acc = _resolve_acc(account_dir)
    entries = sns_img_index(acc)
    if not entries:
        return None
    wh = size or md5_sizes(acc).get(md5)
    if wh is None:
        return None
    for name in _pick_by_thumb(entries, wh):
        got = _decrypt_cache_file(Path(entries[name]["path"]), md5)
        if got is not None:
            return got
    return None


def image_path_for_media(media, account_dir: Path | str | None = None,
                         *, allow_thumb: bool = False) -> Path | None:
    """便捷入口：直接传 ``entity.moment_msg.Media``（或等价 dict），自动取 md5+尺寸。"""
    return image_path(canonical_md5(media), account_dir,
                      size=canonical_size(media), allow_thumb=allow_thumb)


def canonical_md5(media) -> str:
    """从 ``Media`` / dict / ``Url`` / str 里取媒体 md5（取不到返回 ``""``）。"""
    if media is None:
        return ""
    if isinstance(media, str):
        text = media.strip().lower()
        if _HEX32.match(text) or _HEX30.match(text):
            return text
        return ""
    if isinstance(media, dict):
        for key in ("md5", "@md5"):
            v = media.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip().lower()
        for key in ("url", "thumb"):
            got = canonical_md5(media.get(key))
            if got:
                return got
        return ""
    # dataclass / 普通对象
    for attr in ("md5",):
        v = getattr(media, attr, None)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    url = getattr(media, "url", None)
    if url is not None:
        got = canonical_md5(url)
        if got:
            return got
    for attr in ("#text", "text"):
        v = getattr(media, attr, None)
        if isinstance(v, str) and _HEX32.match(v.strip()):
            return v.strip().lower()
    return ""


def canonical_size(media) -> tuple[int, int] | None:
    """从 ``Media`` / dict 里取 ``(width, height)``（xml ``<size>``；没有返回 None）。"""
    raw = media if isinstance(media, dict) else _obj_to_dict(media)
    return _size_from_media_dict(raw)


def _obj_to_dict(obj) -> dict:
    """把 dataclass（含 3.x ``entity.moment_msg.Media``）转成带 xml 字段名的 dict。"""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    out: dict = {}
    try:
        for name in getattr(obj, "__dataclass_fields__", {}) or {}:
            out[name] = getattr(obj, name, None)
    except Exception:
        return {}
    # dataclasses_json 字段名是 xml 原名（如 size/@width），这里按语义补一层
    return out


def _size_from_media_dict(raw: dict) -> tuple[int, int] | None:
    if not isinstance(raw, dict):
        return None
    node = raw.get("size") or raw.get("@size") or {}
    if isinstance(node, dict):
        try:
            w = node.get("@width") or node.get("width") or node.get("#text")
            h = node.get("@height") or node.get("height")
            if w is not None and h is not None:
                return int(str(w)), int(h)
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# 头像
# ---------------------------------------------------------------------------


def _avatar_cache_name(username: str) -> str:
    return hashlib.md5(username.encode("utf-8")).hexdigest()


def _avatar_cached(username: str) -> Path | None:
    base = _avatar_cache_name(username)
    for ext in _AVATAR_EXTS:
        p = AVATAR_DIR / f"{base}.{ext}"
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def _write_avatar(username: str, data: bytes) -> Path | None:
    ext = dat.sniff_image_type(data)
    if ext not in _AVATAR_EXTS:
        return None
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    dest = AVATAR_DIR / f"{_avatar_cache_name(username)}.{ext}"
    try:
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
    except OSError:
        return None
    return dest


def _head_image_blob(username: str, account: Path) -> tuple[bytes | None, str]:
    """从 ``head_image.db`` 取 ``(image_buffer, md5)``；失败返回 ``(None, "")``。"""
    try:
        from .db import get_db

        conn = get_db().open("head_image/head_image.db")
    except Exception:
        return None, ""
    try:
        row = conn.execute(
            "SELECT md5, image_buffer FROM head_image WHERE username = ? LIMIT 1", (username,)
        ).fetchone()
    except Exception:
        return None, ""
    if not row:
        return None, ""
    try:
        md5 = str(row[0] or "")
    except (IndexError, TypeError):
        md5 = ""
    try:
        blob = row[1]
    except IndexError:
        blob = None
    data = bytes(blob) if isinstance(blob, (bytes, bytearray, memoryview)) else None
    return (data if data else None), md5


def _head_md5_from_contact(username: str) -> str:
    """``contact.db`` 的 ``head_img_md5``（头像 md5，用于找 temp/head_image 文件）。"""
    try:
        from .db import get_db

        info = get_db().contact(username)
    except Exception:
        return ""
    if not info:
        return ""
    return str(info.get("head_img_md5") or "").strip().lower()


def _head_image_temp_file(md5: str, account: Path) -> Path | None:
    """``<账号目录>/temp/head_image/<md5>`` 是明文头像（下载中转）。"""
    if not md5:
        return None
    try:
        cand = account / "temp" / "head_image" / md5
        if cand.is_file() and cand.stat().st_size > 0:
            return cand
        # 兼容其它月目录 / 未来 HeadImg 布局
        for pat in ("cache/*/HeadImg/**/" + md5, "cache/*/Sns/HeadImg/**/" + md5):
            for p in account.glob(pat):
                if p.is_file() and p.stat().st_size > 0:
                    return p
    except OSError:
        pass
    return None


def avatar_path(username: str, account_dir: Path | str | None = None) -> Path | None:
    """联系人头像：返回本地明文头像路径，找不到返回 ``None``。

    主路径是 ``head_image.db`` 的 ``image_buffer`` BLOB（明文 JPEG/PNG）；
    兜底 ``contact.db.head_img_md5`` → ``temp/head_image/<md5>`` 中转文件。
    结果缓存到 ``.wx4_cache/avatar/<md5(username)>.<ext>``。
    """
    username = (username or "").strip()
    if not username:
        return None
    cached = _avatar_cached(username)
    if cached is not None:
        return cached
    try:
        acc = _resolve_acc(account_dir)
    except Wx4Error:
        return None

    blob, md5 = _head_image_blob(username, acc)
    if blob:
        got = _write_avatar(username, blob)
        if got is not None:
            return got
    md5 = md5 or _head_md5_from_contact(username)
    tmp = _head_image_temp_file(md5, acc)
    if tmp is not None:
        try:
            got = _write_avatar(username, tmp.read_bytes())
        except OSError:
            got = None
        if got is not None:
            return got
    return None


# ---------------------------------------------------------------------------
# 视频 / 封面
# ---------------------------------------------------------------------------


def _video_files(account: Path) -> list[Path]:
    """``msg/video/<YYYY-MM>/*`` 下的视频文件（.mp4）。"""
    out: list[Path] = []
    try:
        for f in (account / "msg" / "video").glob("*/*"):
            if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".avi", ".mkv"):
                out.append(f)
    except OSError:
        pass
    out.sort(key=lambda p: p.name)
    return out


def video_path(media, account_dir: Path | str | None = None) -> Path | None:
    """朋友圈视频：返回本地视频路径，找不到返回 ``None``。

    Args:
        media: ``Media`` 对象 / dict / 32 位 md5 字符串。

    4.x 视频**不加密**，直接返回微信缓存里的原文件路径（不复制到 .wx4_cache，
    避免几 MB~几百 MB 的重复拷贝）。

    实测本机**没有**朋友圈视频落盘缓存（详见模块文档「3. 视频」）：
    视频在 ``<账号目录>/msg/video/<月>/<md5>.mp4``（同名 ``.jpg`` 是封面，
    ``_raw.mp4`` 是原始版），但只有聊天/主动保存的视频才会留下，朋友圈视频是
    边下边播、看完即删（临时文件在 ``cache/<月>/Sns/Temp``，本机为空）。
    """
    md5 = canonical_md5(media)
    if not md5:
        return None
    try:
        acc = _resolve_acc(account_dir)
    except Wx4Error:
        return None
    cands = []
    for f in _video_files(acc):
        stem = f.stem
        if stem == md5:
            cands.append((0, f))            # 优先非 raw 版本
        elif stem == md5 + "_raw":
            cands.append((1, f))
        elif stem.startswith(md5):
            cands.append((2, f))
    if not cands:
        # 朋友圈视频临时缓存目录（本机没有，兼容将来版本）
        for pat in ("cache/*/Sns/Video/*", "cache/*/Sns/Video/*/*"):
            for p in acc.glob(pat):
                if p.is_file() and p.suffix.lower() in (".mp4", ".mov") and md5 in p.name:
                    cands.append((3, p))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1].name))
    return cands[0][1]


def cover_path(md5: str, account_dir: Path | str | None = None) -> Path | None:
    """视频封面：``msg/video/<月>/<md5>.jpg``（与视频同名），找不到返回 ``None``。"""
    md5 = canonical_md5(md5)
    if not md5:
        return None
    try:
        acc = _resolve_acc(account_dir)
    except Wx4Error:
        return None
    fallback: Path | None = None
    try:
        for f in (acc / "msg" / "video").glob("*/*"):
            if not f.is_file():
                continue
            if f.suffix.lower() not in (".jpg", ".png", ".webp"):
                continue
            stem = f.stem
            if stem == md5 and f.suffix.lower() == ".jpg":
                return f
            if stem in (md5, md5 + "_thumb", md5 + "_raw") and fallback is None:
                fallback = f
    except OSError:
        pass
    return fallback


# ---------------------------------------------------------------------------
# 运维/调试
# ---------------------------------------------------------------------------


def clear_cache() -> None:
    """清空本项目缓存（``.wx4_cache``）与内存索引；不碰微信目录。"""
    global _index_cache, _size_map_cache
    import shutil

    with _lock:
        _index_cache = {}
        _size_map_cache = {}
        try:
            shutil.rmtree(CACHE_ROOT)
        except OSError:
            pass


def recon_report(account_dir: Path | str | None = None) -> dict:
    """媒体 recon 快照（给 ``_dev/test_media.py`` / GUI 诊断用）。"""
    acc = _resolve_acc(account_dir)
    report: dict = {"account_dir": str(acc)}
    try:
        report["image_cache_files"] = [str(p) for p in _sns_img_files(acc)]
    except Wx4Error:
        report["image_cache_files"] = []
    try:
        report["video_files"] = [str(p) for p in _video_files(acc)]
    except Wx4Error:
        report["video_files"] = []
    dirs = {}
    for name in ("cache", "msg/video", "msg/attach", "temp/head_image", "db_storage/head_image"):
        p = acc / name
        dirs[name] = {"exists": p.is_dir(), "files": len(list(p.glob("*"))) if p.is_dir() else 0}
    report["dirs"] = dirs
    return report


__all__ = [
    "avatar_path",
    "cache_root",
    "canonical_md5",
    "canonical_size",
    "clear_cache",
    "cover_path",
    "image_path",
    "image_path_for_media",
    "md5_sizes",
    "recon_report",
    "sns_img_index",
    "thumb_path",
    "video_path",
]
