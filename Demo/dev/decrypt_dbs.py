"""Recon：用扫描到的 WCDB key 解密各数据库并探索表情包消息结构。"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import io
import json
import sqlite3
import struct
import sys
from pathlib import Path

if __name__ == "__main__":
    try:
        sys.stdout = io.TextIOWrapper(sys.__stdout__.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

from Crypto.Cipher import AES

ACC = Path.home() / "Documents" / "xwechat_files" / "wxid_hsqe9w1lhl1j22_5663"
OUT = Path("recon/decrypted")
PAGE_SZ, RESERVE = 4096, 80


def derive_key_salt(page1: bytes, key32: bytes, salt16: bytes):
    """判断库布局并返回 (enc_key, salt, plaintext_header)"""
    # 形式1：标准库，salt = page1[:16]
    salt = page1[:16]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mk = hashlib.pbkdf2_hmac("sha512", key32, mac_salt, 2, 32)
    data = page1[16: PAGE_SZ - RESERVE + 16]
    stored = page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mk, data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    if hm.digest() == stored:
        return key32, salt, False
    # 形式2：明文头库，salt 由 key 材料自带
    mac_salt = bytes(b ^ 0x3A for b in salt16)
    mk = hashlib.pbkdf2_hmac("sha512", key32, mac_salt, 2, 32)
    hm = hmac_mod.new(mk, data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    if hm.digest() == stored:
        return key32, salt16, True
    return None, None, None


def decrypt_db(src: Path, dst: Path, enc_key: bytes, salt: bytes, plaintext_header: bool) -> bool:
    raw = src.read_bytes()
    n_pages = len(raw) // PAGE_SZ
    out = bytearray()
    for pg in range(n_pages):
        page = raw[pg * PAGE_SZ:(pg + 1) * PAGE_SZ]
        iv = page[PAGE_SZ - RESERVE: PAGE_SZ - RESERVE + 16]
        body = page[16:] if pg == 0 else page
        # body 去掉尾部 reserve
        enc = body[: len(body) - RESERVE]
        dec = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(enc)
        if pg == 0:
            # 第一页：保留 16 字节明文头（salt/头），拼接解密后的内容
            out += (page[:16] if plaintext_header else b"SQLite format 3\x00") + dec
        else:
            out += dec
        out += b"\x00" * RESERVE
    dst.write_bytes(bytes(out))
    head = dst.read_bytes()[:16]
    return head.startswith(b"SQLite format 3")


def main() -> int:
    keyfile = Path("recon/found_db_key5.txt")
    kv = dict(line.split("=", 1) for line in keyfile.read_text().strip().splitlines() if "=" in line)
    key32 = bytes.fromhex(kv["key"])
    salt16 = bytes.fromhex(kv["salt"])
    print("key32:", key32.hex(), "salt:", salt16.hex())
    OUT.mkdir(parents=True, exist_ok=True)
    for src in sorted(ACC.glob("db_storage/*/*.db")):
        if src.name.endswith(("-wal", "-shm")):
            continue
        page1 = src.read_bytes()[:PAGE_SZ]
        ek, salt, pth = derive_key_salt(page1, key32, salt16)
        if ek is None:
            print(f"  {src.relative_to(ACC)}: 密钥无效")
            continue
        dst = OUT / src.name
        ok = decrypt_db(src, dst, ek, salt, pth)
        print(f"  {src.relative_to(ACC)}: layout={'plaintext-header' if pth else 'standard'} -> {dst.name} ok={ok}")
        if ok:
            try:
                con = sqlite3.connect(str(dst))
                tables = [r[0] for r in con.execute(
                    "select name from sqlite_master where type='table'").fetchall()]
                con.close()
                print(f"    表({len(tables)}): {tables[:12]}{' ...' if len(tables) > 12 else ''}")
            except Exception as e:
                print("    打开失败:", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
