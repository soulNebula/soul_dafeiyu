# -*- coding: utf-8 -*-
"""微信 4.x 缓存图片 ``.dat`` 解密（V1/V2 容器 + V3 整文件异或），纯算法层。

本模块**不读微信进程、不读微信路径、不写任何文件**：密钥由调用方注入
（见 :mod:`wx4.image_key`），文件读写由 :mod:`wx4.media` 负责。

## 容器格式（微信 4.1.15.56 实测）

V2 文件头 15 字节::

    07 08 'V' '2' 08 07 | aes_size(u32 LE) | xor_size(u32 LE) | 01
    └──── 6 字节 magic ──┘└─── 4 字节 ────┘└─── 4 字节 ───┘ └marker

* ``offset = 15`` 起是 **AES-128-ECB** 密文段，长度 = ``aes_size`` 向上对齐到
  16 的块数（微信固定额外补一个 16 字节 PKCS#7 块，故实测
  ``aes_size=1024`` → 密文 1040 字节，解密后要**去掉尾部的 PKCS#7 填充**）。
* 之后是**明文段**（可能 0 字节，混合老格式时存在）。
* 文件最后 ``xor_size`` 字节是**单字节异或段**（与 ``xor_key`` 异或）。

V1 结构与 V2 相同，只是 AES 密钥固定为 ``b"cfcd208495d565ef"``。
另有整文件单字节异或的旧格式（社区称 V3），以及本身就是图片明文的缓存文件。

## 密钥语义（踩坑记录）

* **``xor_key`` 是十进制字节值**。``wx_box/config.json`` 里 ``"image_xor_key": "82"``
  是 **十进制 82 == 0x52**，不是十六进制 0x82；用 0x82 解出来的图能过 PIL 的
  ``Image.open``（只看文件头），但整张图是灰的/解码失败。实测用 0x52 时 JPEG
  熵编码码流里 ``0xFF`` 后接非法字节的比例是 0.2%，用 0x82 是 96%。
* ``aes_key`` 为 16 个 ASCII hex 字符（如 ``"0669b78f42c9a7d7"``）。
* AES 段解密后**必须去掉 PKCS#7 填充**，否则会在图片中间插入 16 字节垃圾，
  JPEG 只解出顶部一小条。

## 依赖

优先用项目 venv 自带的 ``cryptography``（不需要 pycryptodome）；若环境里有
``Cryptodome``（pycryptodomex）或 ``Crypto``（pycryptodome）也会自动使用。
"""
from __future__ import annotations

import struct
from pathlib import Path

try:  # 项目内导入时统一抛 Wx4Error（GUI 只显示中文信息）
    from . import Wx4Error as _Wx4Error
except ImportError:  # 单文件导入/脚本内使用时的兜底

    class _Wx4Error(RuntimeError):  # type: ignore[no-redef]
        pass


V1_MAGIC = b"\x07\x08V1\x08\x07"
V2_MAGIC = b"\x07\x08V2\x08\x07"
V1_AES_KEY = b"cfcd208495d565ef"
HEADER_LEN = 15

#: 4.x 常见异或键（十进制）。仅作调用方兜底提示，算法本身不默认使用。
DEFAULT_XOR_KEY = 82


class DatDecryptError(_Wx4Error):
    """``.dat`` 解密失败（格式不认识 / 密钥不对 / 依赖缺失）。"""


# ---------------------------------------------------------------------------
# 基础判定
# ---------------------------------------------------------------------------


def detect_version(data: bytes) -> int:
    """返回 1 / 2；不是 V1/V2 容器返回 0。"""
    if data[:6] == V2_MAGIC:
        return 2
    if data[:6] == V1_MAGIC:
        return 1
    return 0


def aligned_aes_size(aes_size: int) -> int:
    """AES 密文段长度：``aes_size`` 向上对齐到 16 字节（与原工具一致）。

    微信 V2 实际密文长度 = ``ceil(aes_size/16)*16``，且原始实现里还额外多算了
    一个块（``aes_size - ~(~aes_size % 16)``，对 1024 得 1040），与实测文件一致。
    """
    if aes_size <= 0:
        return 0
    return aes_size - ~(~aes_size % 16)


_IMG_MAGICS = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"wxgf", "hevc"),
)


def sniff_image_type(data: bytes) -> str:
    """按文件头判断图片/视频类型，返回 ``jpg/png/gif/bmp/webp/hevc`` 或 ``""``。"""
    for magic, ext in _IMG_MAGICS:
        if data.startswith(magic):
            return ext
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "mp4"
    return ""


def is_image(data: bytes) -> bool:
    """是否已是可识别的图片明文（未加密/已解密）。"""
    return sniff_image_type(data) in ("jpg", "png", "gif", "bmp", "webp")


# ---------------------------------------------------------------------------
# AES-ECB 后端（cryptography 优先，其次 pycryptodomex/pycryptodome）
# ---------------------------------------------------------------------------


def _aes_ecb_crypt(key: bytes, data: bytes, encrypt: bool) -> bytes:
    """AES-128-ECB 加/解密一块数据（长度需为 16 的倍数）。"""
    if len(key) != 16:
        raise DatDecryptError(f"AES 密钥必须是 16 字节（当前 {len(key)}）")
    if len(data) % 16:
        raise DatDecryptError("AES 数据长度必须是 16 的倍数")
    try:  # 1) 项目 venv 自带的 cryptography
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        cipher = Cipher(algorithms.AES(key), modes.ECB())
        ctx = cipher.encryptor() if encrypt else cipher.decryptor()
        return ctx.update(data) + ctx.finalize()
    except ImportError:
        pass
    try:  # 2) pycryptodomex（import 名 Cryptodome）
        from Cryptodome.Cipher import AES as _AES

        return _AES.new(key, _AES.MODE_ECB).encrypt(data) if encrypt else _AES.new(key, _AES.MODE_ECB).decrypt(data)
    except ImportError:
        pass
    try:  # 3) pycryptodome（import 名 Crypto）
        from Crypto.Cipher import AES as _AES  # type: ignore[no-redef]

        return _AES.new(key, _AES.MODE_ECB).encrypt(data) if encrypt else _AES.new(key, _AES.MODE_ECB).decrypt(data)
    except ImportError as exc:
        raise DatDecryptError(
            "缺少 AES 依赖：需要 cryptography 或 pycryptodomex/pycryptodome"
        ) from exc


def _pkcs7_unpad(data: bytes) -> bytes:
    """去掉 PKCS#7 填充（仅在尾部确实是合法填充时，避免误删真实数据）。"""
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and len(data) >= pad and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def _as_key_bytes(key: str | bytes | None, version: int) -> bytes:
    if key is None:
        if version == 1:
            return V1_AES_KEY
        raise DatDecryptError("V2 格式需要提供 16 字节 AES 密钥")
    if isinstance(key, str):
        key = key.encode("ascii", "ignore")
    key = bytes(key)
    return key


# ---------------------------------------------------------------------------
# 解密
# ---------------------------------------------------------------------------


def decrypt_dat_bytes(
    data: bytes, aes_key: str | bytes | None = None, xor_key: int = DEFAULT_XOR_KEY
) -> tuple[bytes, str]:
    """解密 V1/V2 ``.dat`` 内容，返回 ``(明文 bytes, 扩展名)``。

    ``xor_key`` 是**十进制字节值**（微信 4.x 实测 82）。
    扩展名由解密结果文件头判定（jpg/png/gif/webp/bmp/hevc），判断不出返回 ``""``。
    """
    if len(data) < HEADER_LEN + 16:
        raise DatDecryptError(f"文件太短（{len(data)} 字节），不是有效的 dat")
    version = detect_version(data)
    if version == 0:
        raise DatDecryptError("不是 V1/V2 dat 容器（文件头 magic 不匹配）")
    key = _as_key_bytes(aes_key, version)

    aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    aligned = aligned_aes_size(aes_size)
    offset = HEADER_LEN
    if aligned <= 0 or offset + aligned > len(data):
        raise DatDecryptError(f"aes_size 异常（aes_size={aes_size}, 文件 {len(data)} 字节）")
    plain_head = _pkcs7_unpad(_aes_ecb_crypt(key, data[offset:offset + aligned], encrypt=False))

    offset += aligned
    raw_end = len(data) - max(0, int(xor_size))
    if raw_end < offset:  # xor_size 异常大时容忍，退化为"无异或明文段"
        raw_end = len(data)
    middle = data[offset:raw_end]
    tail = data[raw_end:]
    xor_key = int(xor_key) & 0xFF
    tail_plain = bytes(b ^ xor_key for b in tail) if tail else b""
    result = plain_head + middle + tail_plain
    return result, sniff_image_type(result)


def decrypt_dat_file(
    path: str | Path, aes_key: str | bytes | None = None, xor_key: int = DEFAULT_XOR_KEY
) -> tuple[bytes, str]:
    """解密单个 ``.dat`` 文件，返回 ``(明文 bytes, 扩展名)``。"""
    return decrypt_dat_bytes(Path(path).read_bytes(), aes_key=aes_key, xor_key=xor_key)


def decrypt_auto(data: bytes, xor_key: int = DEFAULT_XOR_KEY,
                 aes_key: str | bytes | None = None) -> tuple[bytes, str]:
    """自动判断格式并解密，返回 ``(明文, 扩展名)``。

    顺序：已是图片明文 → 直接返回；V1/V2 容器 → AES 段 + 明文段 + 异或尾；
    其余 → 整文件单字节异或（V3 旧格式）。全部失败抛 :class:`DatDecryptError`。
    """
    ext = sniff_image_type(data)
    if ext:
        return data, ext
    version = detect_version(data)
    key = aes_key
    if isinstance(key, str):
        key = key.encode("ascii", "ignore")
    if version in (1, 2):
        # V1 的 AES 密钥是**固定**的，调用方传入的 aes_key 只对 V2 有意义；
        # 传错时不能让 AES 解出垃圾却当成成功（实测过），所以 V1 强制用内置密钥。
        return decrypt_dat_bytes(data, aes_key=None if version == 1 else key, xor_key=xor_key)
    plain = bytes(b ^ (int(xor_key) & 0xFF) for b in data)
    ext = sniff_image_type(plain)
    if ext:
        return plain, ext
    raise DatDecryptError("无法识别的 .dat 格式（不是图片明文，也不是 V1/V2/V3）")


def decrypt_file_auto(path: str | Path, xor_key: int = DEFAULT_XOR_KEY, aes_key: str | bytes | None = None) -> tuple[bytes, str]:
    """同 :func:`decrypt_auto`，但直接读文件。"""
    return decrypt_auto(Path(path).read_bytes(), xor_key=xor_key, aes_key=aes_key)


# ---------------------------------------------------------------------------
# 测试辅助：构造 V2 dat（仅供 _dev 单测，生产路径不使用）
# ---------------------------------------------------------------------------


def build_v2_dat(plain: bytes, aes_key: bytes, xor_key: int = DEFAULT_XOR_KEY,
                 xor_size: int = 16, raw_size: int = 0) -> bytes:
    """按 V2 布局构造一个 ``.dat``（用于离线自测解密算法）。

    布局：``header + AES(pad(head_plain)) + raw(明文) + xor(tail_plain)``。
    """
    if isinstance(aes_key, str):
        aes_key = aes_key.encode("ascii")
    xor_key = int(xor_key) & 0xFF
    xor_size = max(0, min(xor_size, len(plain) - 16))
    raw_size = max(0, min(raw_size, len(plain) - xor_size - 16))
    head_plain = plain[: len(plain) - raw_size - xor_size]
    middle = plain[len(plain) - raw_size - xor_size: len(plain) - xor_size]
    tail_plain = plain[len(plain) - xor_size:] if xor_size else b""
    pad_len = 16 - (len(head_plain) % 16)
    enc = _aes_ecb_crypt(aes_key, head_plain + bytes([pad_len]) * pad_len, encrypt=True)
    aes_size = max(16, len(head_plain))
    header = V2_MAGIC + struct.pack("<LL", aes_size, xor_size) + b"\x01"
    tail = bytes(b ^ xor_key for b in tail_plain)
    return header + enc + middle + tail
