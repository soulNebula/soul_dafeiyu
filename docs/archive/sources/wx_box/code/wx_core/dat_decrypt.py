"""微信 4.x 缓存图片 .dat 解密（V1/V2 格式）。

格式（依据公开资料 + 本地文件头实测，微信 4.1.15.56）：

V2 文件头 15 字节::

    07 08 'V' '2' 08 07 | aes_size(u32 LE) | xor_size(u32 LE) | 01
    └──── 6字节 magic ───┘└── 4字节 ──────┘└──── 4字节 ────┘ └marker

    offset=15 开始是 AES-128-ECB 密文（长度 = aes_size 向下/向上对齐到 16 的块数），
    之后是明文段，最后 xor_size 字节是"异或段"（与单字节 xor_key 异或，微信 4.x 常用 0x88）。

V1 同结构，但 AES 密钥固定为 ``b"cfcd208495d565ef"``；
V2 的 AES 密钥是每个账号一份的 16 字节密钥（由 ``wx_core/wechat_keys.py`` 从内存一次性取出并缓存）。
另有整文件单字节异或的旧格式（社区称 V3），以及已是图片明文的缓存文件。

**本模块只做纯算法，不读微信进程、不读任何微信路径**：密钥由调用方注入。
运行流程默认启用：`wechat_keys` 取到密钥后，新图片/表情包会自动解密成原始格式文件。
"""
from __future__ import annotations

import struct
from pathlib import Path

V1_MAGIC = b"\x07\x08V1\x08\x07"
V2_MAGIC = b"\x07\x08V2\x08\x07"
V1_AES_KEY = b"cfcd208495d565ef"
HEADER_LEN = 15


class DatDecryptError(Exception):
    pass


def detect_version(data: bytes) -> int:
    """返回 1 / 2；不是 V1/V2 格式返回 0。"""
    if data[:6] == V2_MAGIC:
        return 2
    if data[:6] == V1_MAGIC:
        return 1
    return 0


def _aligned_size(aes_size: int) -> int:
    """与原工具一致的 AES 密文长度计算（16 字节对齐）。"""
    if aes_size <= 0:
        return 0
    return aes_size - ~(~aes_size % 16)


def decrypt_dat_bytes(data: bytes, aes_key: bytes | None = None, xor_key: int = 0x88) -> tuple[bytes, str]:
    """解密 V1/V2 .dat 内容，返回 (明文 bytes, 格式扩展名)。

    格式扩展名靠解密结果的文件头判断（jpg/png/gif/webp/bmp/hevc），判断失败返回 ""。
    """
    if len(data) < HEADER_LEN + 16:
        raise DatDecryptError("文件太短，不是有效的 dat")
    version = detect_version(data)
    if version == 0:
        raise DatDecryptError("不是 V1/V2 dat 格式")
    key = aes_key
    if key is None:
        if version == 1:
            key = V1_AES_KEY
        else:
            raise DatDecryptError("V2 格式需要提供 16 字节 AES 密钥")
    if isinstance(key, str):
        key = key.encode("ascii", "ignore")
    key = bytes(key)
    if len(key) != 16:
        raise DatDecryptError(f"AES 密钥必须是 16 字节（当前 {len(key)}）")

    aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    aligned = _aligned_size(aes_size)
    offset = HEADER_LEN
    if aligned <= 0 or offset + aligned > len(data):
        raise DatDecryptError("aes_size 异常")
    try:
        from Crypto.Cipher import AES  # pycryptodome
        from Crypto.Util.Padding import unpad

        cipher = AES.new(key, AES.MODE_ECB)
        plain_head = cipher.decrypt(data[offset:offset + aligned])
        try:
            plain_head = unpad(plain_head, AES.block_size)
        except ValueError:
            pass  # 没有 padding 也能用
    except ImportError as exc:  # pragma: no cover
        raise DatDecryptError(f"需要 pycryptodome：{exc}") from exc
    offset += aligned
    raw_end = len(data) - xor_size
    if raw_end < offset:
        raw_end = offset
    middle = data[offset:raw_end]
    tail = data[raw_end:]
    tail_plain = bytes(b ^ xor_key for b in tail)
    result = plain_head + middle + tail_plain
    return result, sniff_image_type(result)


def decrypt_dat_file(path: str | Path, aes_key: bytes | None = None, xor_key: int = 0x88) -> tuple[bytes, str]:
    data = Path(path).read_bytes()
    return decrypt_dat_bytes(data, aes_key=aes_key, xor_key=xor_key)


_IMG_MAGICS = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"wxgf", "hevc"),
)


def sniff_image_type(data: bytes) -> str:
    for magic, ext in _IMG_MAGICS:
        if data.startswith(magic):
            return ext
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def build_v2_dat(plain: bytes, aes_key: bytes, xor_key: int = 0x88, xor_size: int = 16,
                 raw_size: int = 0, rng=None) -> bytes:
    """**仅供测试**：按 V2 格式构造一个 dat 文件（用于离线单测解密算法）。

    布局：header + AES(pad(head_plain)) + raw(明文) + xor(tail_plain)
    """
    import random

    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    rng = rng or random.Random(1234)
    aes_key = bytes(aes_key)
    raw_size = max(0, min(raw_size, len(plain) - xor_size - 16))
    head_plain = plain[: len(plain) - raw_size - xor_size]
    middle = plain[len(plain) - raw_size - xor_size: len(plain) - xor_size]
    tail_plain = plain[len(plain) - xor_size:] if xor_size else b""
    enc = AES.new(aes_key, AES.MODE_ECB).encrypt(pad(head_plain, 16))
    aes_size = max(16, len(enc) - 16)
    header = V2_MAGIC + struct.pack("<LL", aes_size, xor_size) + b"\x01"
    tail = bytes(b ^ xor_key for b in tail_plain)
    return header + enc + middle + tail


# ---------------- 统一入口（对齐 WeFlow 的 V1/V2/V3/V4 处理） ----------------

def decrypt_auto(data: bytes, xor_key: int, aes_key: str | bytes | None = None) -> tuple[bytes, str]:
    """自动判断格式并解密，返回 (明文, 扩展名)。

    - 已是图片明文 → 直接返回（V3 之前的老格式/未加密）；
    - V1/V2 容器 → AES-ECB 头 + 明文段 + 单字节异或尾（需要 AES 密钥，V1 用固定密钥）；
    - 其它 → 整文件单字节异或（WeFlow 中的 V3 格式）。
    """
    ext = sniff_image_type(data)
    if ext:
        return data, ext
    version = detect_version(data)
    key = aes_key
    if isinstance(key, str):
        key = key.encode("ascii", "ignore")
    if version in (1, 2):
        try:
            return decrypt_dat_bytes(data, aes_key=key, xor_key=xor_key)
        except DatDecryptError:
            if version == 1:
                return decrypt_dat_bytes(data, aes_key=None, xor_key=xor_key)
            raise
    # 整文件异或（V3）
    plain = bytes(b ^ xor_key for b in data)
    ext = sniff_image_type(plain)
    if ext:
        return plain, ext
    raise DatDecryptError("无法识别的 .dat 格式（既不是图片明文，也不是 V1/V2/V3）")


def decrypt_file_auto(path, xor_key: int, aes_key=None) -> tuple[bytes, str]:
    return decrypt_auto(Path(path).read_bytes(), xor_key, aes_key)
