#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信 4.x 朋友圈「在线图片」解密原语（纯标准库，Python 3.12 / 3.14 均可）。

背景
----
4.x 的朋友圈图片在本地 ``cache/*/Sns/Img`` 没缓存时，只能按 XML 里的 CDN 地址
在线拉取。CDN 返回的密文是 **ISAAC-64 密钥流 XOR**（响应头 ``x-Enc: 1``）：

* 种子 ``seed`` = 朋友圈 XML ``<url key="...">`` / ``<thumb key="...">`` 的十进制整数；
* 初始化常量用 ``0x9e3779b97f4a7c13``（注意不是 RFC 参考实现的 ``...15``）；
  其余流程与 Bob Jenkins 的 ISAAC-64 一致：``randrsl[0]=seed``、``flag=1``、
  ``randrsl`` **逆序消费**（``rsl[255] → rsl[0]``，每 256 个 64 位字重新生成）；
* 密钥流按 **大端** 逐 8 字节输出，与密文逐字节 XOR 得到明文图片。

实测向量（微信 4.1.15.56，2026-10）：``seed=2159574524626044203`` 时
密钥流前 8 字节 = ``0e96a7e3fee93215``，解密后是合法 JPEG。本模块的
``if __name__ == "__main__"`` 会自检该向量；若本机还留有
``WechatMoments/_tmp_img/thumb.enc`` 与 ``thumb_py.plain``，会额外做整文件比对。

对外接口
--------
* :func:`isaac64_keystream` —— 生成 ``size`` 字节密钥流；
* :func:`decrypt_xenc` —— 用 XML ``key``（十进制字符串）解密整个响应体；
* :func:`looks_like_image` —— 校验 JPEG/PNG/GIF/WebP/BMP 魔数（防止把
  token 过期返回的 120×120 占位图/垃圾字节写进缓存）；
* :func:`sniff_image_ext` —— 由魔数给出扩展名（worker 落盘用）。
"""
from __future__ import annotations

#: 64 位掩码
UINT64_MASK = 0xFFFFFFFFFFFFFFFF

#: ISAAC-64 初始化常量（微信 4.x 朋友圈图片实测值，注意不是 0x9e3779b97f4a7c15）
ISAAC64_INIT_CONSTANT = 0x9E3779B97F4A7C13

#: 已知实测向量：seed → 密钥流前 8 字节（十六进制）
KNOWN_KEYSTREAM_VECTOR = (2159574524626044203, "0e96a7e3fee93215")


class Isaac64:
    """ISAAC-64 伪随机数生成器（按微信朋友圈图片的初始化约定封装）。

    只实现解密需要的部分：单密钥（``randrsl[0]=seed``、其余 0）、flag=True、
    输出时逆序消费 ``randrsl``。所有运算都掩到 64 位无符号范围，等价于
    C/TS 里用 ``uint64_t`` / ``BigUint64Array`` 的实现。
    """

    def __init__(self, seed: int) -> None:
        self.mm = [0] * 256
        self.randrsl = [0] * 256
        self.aa = 0
        self.bb = 0
        self.cc = 0
        self.randcnt = 0
        seed_value = int(seed) & UINT64_MASK
        # 单密钥初始化：第一个槽位放 seed，其余为 0（与参考实现一致）
        self.randrsl[0] = seed_value
        self._init(flag=True)
        self.randcnt = 256

    @staticmethod
    def _mix(a: int, b: int, c: int, d: int, e: int, f: int, g: int, h: int):
        """ISAAC-64 的 8 路混淆函数（返回 8 个更新后的状态字）。"""
        mask = UINT64_MASK
        a = (a - e) & mask
        f ^= h >> 9
        h = (h + a) & mask

        b = (b - f) & mask
        g ^= (a << 9) & mask
        a = (a + b) & mask

        c = (c - g) & mask
        h ^= b >> 23
        b = (b + c) & mask

        d = (d - h) & mask
        a ^= (c << 15) & mask
        c = (c + d) & mask

        e = (e - a) & mask
        b ^= d >> 14
        d = (d + e) & mask

        f = (f - b) & mask
        c ^= (e << 20) & mask
        e = (e + f) & mask

        g = (g - c) & mask
        d ^= f >> 17
        f = (f + g) & mask

        h = (h - d) & mask
        e ^= (g << 14) & mask
        g = (g + h) & mask
        return a, b, c, d, e, f, g, h

    def _init(self, flag: bool) -> None:
        """标准 ISAAC-64 randinit（含两轮 mm 填充 + 首次生成）。"""
        mask = UINT64_MASK
        a = b = c = d = e = f = g = h = ISAAC64_INIT_CONSTANT

        for _ in range(4):
            a, b, c, d, e, f, g, h = self._mix(a, b, c, d, e, f, g, h)

        # 第一轮：把 seed（randrsl）混进状态
        for i in range(0, 256, 8):
            if flag:
                a = (a + self.randrsl[i]) & mask
                b = (b + self.randrsl[i + 1]) & mask
                c = (c + self.randrsl[i + 2]) & mask
                d = (d + self.randrsl[i + 3]) & mask
                e = (e + self.randrsl[i + 4]) & mask
                f = (f + self.randrsl[i + 5]) & mask
                g = (g + self.randrsl[i + 6]) & mask
                h = (h + self.randrsl[i + 7]) & mask
            a, b, c, d, e, f, g, h = self._mix(a, b, c, d, e, f, g, h)
            self.mm[i : i + 8] = [a, b, c, d, e, f, g, h]

        # 第二轮：继续把 mm 混进状态（flag=True 时）
        if flag:
            for i in range(0, 256, 8):
                a = (a + self.mm[i]) & mask
                b = (b + self.mm[i + 1]) & mask
                c = (c + self.mm[i + 2]) & mask
                d = (d + self.mm[i + 3]) & mask
                e = (e + self.mm[i + 4]) & mask
                f = (f + self.mm[i + 5]) & mask
                g = (g + self.mm[i + 6]) & mask
                h = (h + self.mm[i + 7]) & mask
                a, b, c, d, e, f, g, h = self._mix(a, b, c, d, e, f, g, h)
                self.mm[i : i + 8] = [a, b, c, d, e, f, g, h]

        self._generate()

    def _generate(self) -> None:
        """生成一批 ``randrsl[0..255]``（ISAAC-64 isaac64()）。"""
        mask = UINT64_MASK
        self.cc = (self.cc + 1) & mask
        self.bb = (self.bb + self.cc) & mask
        mm = self.mm
        rsl = self.randrsl
        aa = self.aa
        bb = self.bb
        for i in range(256):
            x = mm[i]
            if (i & 3) == 0:
                aa = (aa ^ (((aa << 21) & mask) ^ mask)) & mask  # 等价 ~(aa ^ (aa<<21))
            elif (i & 3) == 1:
                aa = (aa ^ (aa >> 5)) & mask
            elif (i & 3) == 2:
                aa = (aa ^ ((aa << 12) & mask)) & mask
            else:
                aa = (aa ^ (aa >> 33)) & mask
            aa = (mm[(i + 128) & 255] + aa) & mask
            y = (mm[(x >> 3) & 255] + aa + bb) & mask
            mm[i] = y
            bb = (mm[(y >> 11) & 255] + x) & mask
            rsl[i] = bb
        self.aa = aa
        self.bb = bb

    def next_u64(self) -> int:
        """取下一个 64 位输出（逆序消费 ``randrsl``，与参考实现一致）。"""
        if self.randcnt == 0:
            self._generate()
            self.randcnt = 256
        self.randcnt -= 1
        return self.randrsl[self.randcnt]


def isaac64_keystream(seed: int, size: int) -> bytes:
    """生成 ``size`` 字节的 ISAAC-64 密钥流（每 64 位按大端输出）。

    Args:
        seed: 十进制种子（XML ``key`` 属性转成的整数）。
        size: 需要多少字节；末尾不足 8 字节时取下一个 64 位字的高位字节。

    Returns:
        密钥流字节；``size <= 0`` 时返回 ``b""``。
    """
    want = int(size or 0)
    if want <= 0:
        return b""
    rng = Isaac64(seed)
    output = bytearray()
    blocks, remainder = divmod(want, 8)
    for _ in range(blocks):
        output.extend(rng.next_u64().to_bytes(8, "big", signed=False))
    if remainder:
        output.extend(rng.next_u64().to_bytes(8, "big", signed=False)[:remainder])
    return bytes(output)


def _parse_seed(key: str | int) -> int:
    """把 XML ``key`` 属性（十进制字符串）解析为整数种子。"""
    if isinstance(key, int):
        return key
    text = str(key or "").strip()
    if not text:
        raise ValueError("缺少朋友圈图片解密 key")
    try:
        return int(text, 10)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无效的朋友圈图片解密 key：{text!r}") from exc


def decrypt_xenc(payload: bytes, key: str | int) -> bytes:
    """用 ISAAC-64 密钥流解密朋友圈图片响应体（整段 XOR）。

    Args:
        payload: CDN 返回的密文（响应头 ``x-Enc: 1``）。
        key: XML 里的 ``key`` 属性（十进制字符串）或整数种子。

    Returns:
        解密后的字节（**调用方必须**再用 :func:`looks_like_image` 校验）。

    Raises:
        ValueError: ``key`` 缺失或不是合法整数。
    """
    raw = bytes(payload or b"")
    if not raw:
        return b""
    seed = _parse_seed(key)
    stream = isaac64_keystream(seed, len(raw))
    return bytes(a ^ b for a, b in zip(raw, stream))


def looks_like_image(data: bytes) -> bool:
    """按文件魔数判断是否是常见图片（JPEG/PNG/GIF/WebP/BMP）。"""
    return bool(sniff_image_ext(data))


def sniff_image_ext(data: bytes) -> str:
    """按魔数返回图片扩展名（不带点）；不是图片返回 ``""``。"""
    head = bytes(data or b"")[:16]
    if not head:
        return ""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(head) >= 6 and head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"BM"):
        return "bmp"
    return ""


def _selftest() -> int:
    """自检：已知密钥流向量 + 可选的整文件样本比对。"""
    seed, expected = KNOWN_KEYSTREAM_VECTOR
    got = isaac64_keystream(seed, 8).hex()
    if got != expected:
        print(f"[FAIL] 密钥流向量不符：期望 {expected}，实际 {got}")
        return 1
    print(f"[PASS] ISAAC-64 密钥流向量：seed={seed} 前 8 字节 = {got}")

    if not looks_like_image(b"\xff\xd8\xff\xe0" + b"\x00" * 8):
        print("[FAIL] JPEG 魔数识别失败")
        return 1
    if not looks_like_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8):
        print("[FAIL] PNG 魔数识别失败")
        return 1
    if looks_like_image(b"not-an-image"):
        print("[FAIL] 非图片被误判为图片")
        return 1
    print("[PASS] 图片魔数识别（JPEG/PNG/非图片）")

    # 本机若留有测试样本（Lead 抓的密文与参考明文），做一次整文件比对
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent
    candidates = [project_root / "_tmp_img", project_root / "WechatMoments" / "_tmp_img"]
    sample_dir = next(
        (folder for folder in candidates if (folder / "thumb.enc").is_file()), None
    )
    encrypted = (sample_dir / "thumb.enc") if sample_dir is not None else None
    expected_plain = (sample_dir / "thumb_py.plain") if sample_dir is not None else None
    if encrypted is not None and expected_plain is not None and expected_plain.is_file():
        plain = decrypt_xenc(encrypted.read_bytes(), seed)
        if plain == expected_plain.read_bytes():
            print(f"[PASS] 整文件解密样本一致（{len(plain)} 字节，{sniff_image_ext(plain)}）")
        else:
            print("[FAIL] 整文件解密样本不一致")
            return 1
    else:
        print("[SKIP] 未找到整文件测试样本（_tmp_img/thumb.enc + thumb_py.plain），跳过")
    print("自检通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
