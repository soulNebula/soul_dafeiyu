"""Recon：拿到 master 后，暴力尝试 SQLCipher 参数组合，找到能解开当前账号数据库的密钥。"""
from __future__ import annotations

import ctypes
import hashlib
import hmac as hmac_mod
import io
import os
import struct
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, "recon")
import try_db_key as K  # noqa: E402

PAGE_SZ, RESERVE_SZ = 4096, 80


def pick_account() -> Path:
    best = None
    for d in K.ROOT.iterdir():
        if not d.is_dir() or not d.name.startswith("wxid_"):
            continue
        m = 0.0
        for pat in ("db_storage/message/*.db-wal", "db_storage/message/*.db"):
            try:
                for f in (d / "db_storage/message").glob(pat.split("/")[-1]):
                    m = max(m, f.stat().st_mtime)
            except Exception:
                continue
        if best is None or m > best[0]:
            best = (m, d)
    return best[1] if best else None


def verify(page1: bytes, enc_key: bytes, salt: bytes | None = None) -> bool:
    if len(page1) < PAGE_SZ:
        return False
    salt = salt or page1[:16]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, 32)
    data = page1[16: PAGE_SZ - RESERVE_SZ + 16]
    stored = page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored


def candidates(master: bytes):
    yield "ascii32", master
    try:
        yield "hex2bin16", bytes.fromhex(master.decode("ascii"))
    except Exception:
        pass
    try:
        yield "hex2bin16_rev", bytes.fromhex(master.decode("ascii"))[::-1]
    except Exception:
        pass
    yield "ascii32_rev", master[::-1]
    if len(master) >= 32:
        yield "ascii32_upper", master.upper()
    # 原始 key 模式：16 字节 key 补零到 32
    try:
        b = bytes.fromhex(master.decode("ascii"))
        yield "raw_pad32", (b + b"\x00" * 32)[:32]
        yield "raw_hash32", hashlib.md5(b).digest()
    except Exception:
        pass
    yield "md5_master", hashlib.md5(master).digest()
    yield "sha256_master", hashlib.sha256(master).digest()


def try_db(master: bytes, db: Path) -> tuple[str, bytes] | None:
    page1 = db.read_bytes()[:PAGE_SZ]
    if len(page1) < PAGE_SZ:
        return None
    salt = page1[:16]
    iterations = (256000, 64000, 4000, 1)
    for name, passwd in candidates(master):
        # PBKDF2 派生模式
        for it in iterations:
            try:
                derived = hashlib.pbkdf2_hmac("sha512", passwd, salt, it, 32)
            except Exception:
                continue
            if verify(page1, derived):
                return f"{name}/pbkdf2-sha512-{it}", derived
        # 原始 key 模式（不做 PBKDF2，直接当 enc_key）
        for raw in (passwd, (passwd + b"\x00" * 32)[:32]):
            if len(raw) in (32, 48) and verify(page1, raw if len(raw) == 32 else raw[:32]):
                return f"{name}/raw", raw[:32]
        # SHA1 版本（老 SQLCipher3）
        for it in iterations:
            try:
                derived = hashlib.pbkdf2_hmac("sha1", passwd, salt, it, 32)
            except Exception:
                continue
            if verify(page1, derived):
                return f"{name}/pbkdf2-sha1-{it}", derived
    return None


def main() -> int:
    acc = pick_account()
    print("活跃账号:", acc)
    for pid in K.weixin_pids():
        got = K.locate_cfg_and_master(pid)
        if not got:
            continue
        cfg, master, meta = got
        print("cfg:", hex(cfg), "master:", master.hex(), "ascii:", master.decode("ascii", "replace"))
        for dbname in ("db_storage/emoticon/emoticon.db",
                       "db_storage/message/message_0.db",
                       "db_storage/contact/contact.db",
                       "db_storage/session/session.db"):
            db = acc / dbname
            if not db.exists():
                continue
            hit = try_db(master, db)
            print(f"  {dbname}: {hit[0] if hit else 'FAIL'}")
            if hit:
                name, key = hit
                out = Path("recon") / ("dec_" + db.name)
                K.decrypt_db(db, key, out)
                print(f"    -> {out} ({out.stat().st_size}B, cipher={name})")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
