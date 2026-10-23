"""Recon：微信 4.x 表情包（贴纸）本地解密 + wxgf 动图转 GIF 验证。

算法（来自开源 Wechat-Emoticon-Parser v4.0-plus，已在本机用已知 seed 验证）：
    key = md5(f"{seed}{wxid}EMOTICON").digest()[:16]
    AES-128-CBC，key 同时作为 IV，PKCS7
缓存位置：<账号>/cache/<yyyy-mm>/Emoticon/<xx>/<md5>、<账号>/business/emoticon/{Persist,Thumb,...}/*
"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

ROOT = Path.home() / "Documents" / "xwechat_files"
SEED = 3908286546
WXID = "wxid_hsqe9w1lhl1j22"
ACC = ROOT / "wxid_hsqe9w1lhl1j22_5663"
OUT = Path("recon/emoticon_dec")
MAGICS = {b"\x89PNG": "png", b"GIF8": "gif", b"\xff\xd8\xff": "jpg", b"wxgf": "wxgf", b"RIFF": "webp"}


def key_from(seed: int, wxid: str) -> bytes:
    return hashlib.md5(f"{seed}{wxid}EMOTICON".encode()).digest()[:16]


def decrypt_file(path: Path, key: bytes) -> bytes | None:
    data = path.read_bytes()
    n = len(data) // 16 * 16
    if n < 16:
        return None
    try:
        dec = AES.new(key, AES.MODE_CBC, key).decrypt(data[:n])
    except Exception:
        return None
    try:
        dec = unpad(dec, 16)
    except ValueError:
        pass
    if len(data) % 16:
        dec += data[n:]
    return dec


def detect(data: bytes) -> str | None:
    for m, ext in MAGICS.items():
        if data.startswith(m):
            return ext
    return None


def wxgf_to_gif(data: bytes, out_gif: Path) -> bool:
    import imageio_ffmpeg

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    tmp = out_gif.with_suffix(".wxgf")
    tmp.write_bytes(data)
    vf = "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
    r = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(tmp),
                        "-vf", vf, "-loop", "0", str(out_gif)], capture_output=True)
    if r.returncode != 0 or not out_gif.exists() or out_gif.stat().st_size < 100:
        # fallback: 从 VPS/SPS 截取
        cut = -1
        for marker in (b"\x00\x00\x00\x01\x40\x01", b"\x00\x00\x00\x01\x42\x01"):
            i = data.find(marker)
            if i >= 0:
                cut = i if cut < 0 else min(cut, i)
        if cut >= 0:
            tmp.write_bytes(data[cut:])
            r = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(tmp),
                                "-vf", vf, "-loop", "0", str(out_gif)], capture_output=True)
    try:
        tmp.unlink()
    except OSError:
        pass
    return out_gif.exists() and out_gif.stat().st_size > 100


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    key = key_from(SEED, WXID)
    print("emoticon key:", key.hex())
    files: list[Path] = []
    files += list((ACC / "cache").rglob("Emoticon/*/*"))
    for sub in ("Persist", "PersistStore", "Thumb", "ThumbStore"):
        d = ACC / "business" / "emoticon" / sub
        if d.is_dir():
            files += [p for p in d.rglob("*") if p.is_file()]
    files = [p for p in files if p.is_file()]
    print("候选文件:", [(str(p.relative_to(ACC)), p.stat().st_size) for p in files])
    for p in files:
        dec = decrypt_file(p, key)
        if dec is None:
            print("  解密失败", p.name)
            continue
        fmt = detect(dec)
        rel = p.relative_to(ACC)
        stem = p.name
        if fmt == "wxgf":
            gif = OUT / (stem + ".gif")
            ok = wxgf_to_gif(dec, gif)
            print(f"  {rel} -> wxgf -> gif ok={ok} size={gif.stat().st_size if ok else 0}")
        elif fmt:
            out = OUT / (stem + "." + fmt)
            out.write_bytes(dec)
            print(f"  {rel} -> {fmt} {len(dec)}B -> {out}")
        else:
            print(f"  {rel} -> 未知格式 head={dec[:8].hex()}")
            (OUT / (stem + ".bin")).write_bytes(dec)
    print("输出目录:", OUT.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
