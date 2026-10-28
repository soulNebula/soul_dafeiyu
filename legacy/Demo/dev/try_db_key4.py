"""Recon v4：广谱尝试推 WCDB(SQLCipher) 数据库口令。

候选来源：
 1. cfg 结构里 0x40..0x1000 范围内所有 std::string 样式的 blob（含 XOR 材料解码变体）；
 2. 由已知 seed(cfgDword=图片密钥派生源) + wxid 派生的各种组合。
每个候选都用 SQLCipher 页1 HMAC 强校验（SHA512/256000、SHA1/64000、raw key、48 字节 key+salt）。
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac as hmac_mod
import io
import struct
import sys
from pathlib import Path

try:
    sys.stdout = io.TextIOWrapper(sys.__stdout__.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, "recon")
import try_db_key as K  # noqa: E402

SEED = 3908286546
WXID = "wxid_hsqe9w1lhl1j22"
ACC = Path.home() / "Documents" / "xwechat_files" / "wxid_hsqe9w1lhl1j22_5663"
PAGE_SZ = 4096


def page1(db: Path) -> bytes | None:
    try:
        data = db.read_bytes()[:PAGE_SZ]
    except OSError:
        return None
    return data if len(data) == PAGE_SZ else None


def verify_sha512(page: bytes, enc_key: bytes, reserve: int = 80) -> bool:
    salt = page[:16]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, 32)
    data = page[16: PAGE_SZ - reserve + 16]
    stored = page[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored


def verify_sha512_salted(page: bytes, enc_key: bytes, salt: bytes) -> bool:
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, 32)
    data = page[16: PAGE_SZ - 80 + 16]
    stored = page[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored


def verify_sha1(page: bytes, enc_key: bytes) -> bool:
    salt = page[:16]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha1", enc_key, mac_salt, 2, 20)
    data = page[16: PAGE_SZ - 48 + 16]
    stored = page[PAGE_SZ - 20: PAGE_SZ]
    hm = hmac_mod.new(mac_key, data, hashlib.sha1)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored


def try_candidate(passwd: bytes, page: bytes, tag: str):
    salt = page[:16]
    # PBKDF2-SHA512 常见迭代
    for it in (256000, 64000, 4000):
        try:
            dk = hashlib.pbkdf2_hmac("sha512", passwd, salt, it, 32)
        except Exception:
            break
        if verify_sha512(page, dk):
            return f"{tag}/pbkdf2-sha512-{it}", dk
    # PBKDF2-SHA1 (老 WCDB)
    for it in (64000, 4000):
        try:
            dk = hashlib.pbkdf2_hmac("sha1", passwd, salt, it, 32)
        except Exception:
            break
        if verify_sha1(page, dk):
            return f"{tag}/pbkdf2-sha1-{it}", dk
    # raw key（32 字节 / 48 字节 key+salt）
    if len(passwd) in (32, 48):
        if verify_sha512(page, passwd[:32] if len(passwd) == 48 else passwd):
            return f"{tag}/raw32", passwd[:32]
        if len(passwd) == 48 and verify_sha512_salted(page, passwd[:32], passwd[32:]):
            return f"{tag}/raw48", passwd[:32]
    return None


def passphrase_candidates() -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    seed_s = str(SEED)
    out.append(("seed_str", seed_s.encode()))
    out.append(("seed_hex", format(SEED, "x").encode()))
    out.append(("seed_le4", struct.pack("<I", SEED)))
    out.append(("seed_be4", struct.pack(">I", SEED)))
    out.append(("wxid", WXID.encode()))
    for suffix in ("", "DB", "WCDB", "SQLITE", "WECHAT", "KEY", "PASS"):
        for base in (f"{SEED}{WXID}", f"{WXID}{SEED}", f"{SEED}", f"{WXID}"):
            try:
                out.append((f"md5({base}{suffix})", hashlib.md5(f"{base}{suffix}".encode()).digest()))
                out.append((f"md5hex({base}{suffix})", hashlib.md5(f"{base}{suffix}".encode()).hexdigest()[:32].encode()))
            except Exception:
                pass
    out.append(("md5_seed_wxid", hashlib.md5(f"{SEED}{WXID}".encode()).digest()))
    out.append(("sha256_seed_wxid", hashlib.sha256(f"{SEED}{WXID}".encode()).digest()))
    out.append(("md5_wxid_seed", hashlib.md5(f"{WXID}{SEED}".encode()).digest()))
    return out


def blob_candidates(cfg: int, read, material: bytes) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for off in range(0x40, 0x1000, 8):
        raw = K._read_remote_bytes(read, cfg + off)
        if not raw or not (16 <= len(raw) <= 512):
            continue
        variants = [("raw", raw)]
        if len(material) == 32:
            variants.append(("xor_rep", bytes(a ^ b for a, b in zip(raw, (material * ((len(raw) // 32) + 1))[: len(raw)]))))
            if len(raw) == 32:
                variants.append(("xor_eq", bytes(a ^ b for a, b in zip(raw, material))))
        for vname, data in variants:
            tag = f"cfg+{off:#x}/{vname}"
            out.append((tag, data))
            # ASCII hex / x'..' 形式再解一层
            try:
                txt = data.decode("ascii")
                if txt.startswith("x'") and txt.endswith("'"):
                    hx = txt[2:-1]
                    if len(hx) in (64, 96):
                        out.append((tag + "/xquote", bytes.fromhex(hx)))
                if len(txt) in (32, 64, 96) and all(c in "0123456789abcdefABCDEF" for c in txt):
                    out.append((tag + "/hex", bytes.fromhex(txt)))
            except Exception:
                pass
    return out


def main() -> int:
    db = ACC / "db_storage/message/message_0.db"
    page = page1(db)
    if page is None:
        print("找不到 message_0.db")
        return 1
    print("目标库:", db, page[:8].hex())
    print("== 阶段1: seed/wxid 派生口令 ==")
    for tag, passwd in passphrase_candidates():
        hit = try_candidate(passwd, page, tag)
        if hit:
            print(f"*** 命中! {tag} -> {hit[0]}")
            Path("recon/found_db_key4.txt").write_text(f"{hit[0]}\nenc_key={hit[1].hex()}\n", encoding="utf-8")
            return 0
    print("  未命中")

    print("== 阶段2: cfg 结构里的 blob ==")
    for pid in K.weixin_pids():
        mod = K.find_module(pid)
        if not mod:
            continue
        base, size, dll = mod
        material = K.extract_material(dll)
        if not material:
            continue
        h = K.kernel32.OpenProcess(K.PROCESS_VM_READ | K.PROCESS_QUERY_INFORMATION, False, pid)
        if not h:
            continue

        def read(addr, n):
            buf = ctypes.create_string_buffer(n)
            br = ctypes.c_size_t(0)
            if K.kernel32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
                return buf.raw[: br.value]
            return b""

        try:
            image = read(base, size)
            pos = image.find(K.LANDMARK)
            if pos < 0:
                continue
            lm = base + pos
            cfg = None
            for back in range(0x80, 0x1C1, 8):
                v18b = read(lm - back, 8)
                if len(v18b) != 8:
                    continue
                v18 = struct.unpack_from("<Q", v18b)[0]
                if not (0x10000 < v18 < 0x800000000000):
                    continue
                for cfg_off in range(0x40, 0x91, 8):
                    cb = read(v18 + cfg_off, 8)
                    if len(cb) != 8:
                        continue
                    c = struct.unpack_from("<Q", cb)[0]
                    if not (0x10000 < c < 0x800000000000):
                        continue
                    if read_remote_wxid(read, c):
                        cfg = c
                        break
                if cfg:
                    break
            if not cfg:
                continue
            print("  cfg:", hex(cfg), "候选 blob 数:", len(blob_candidates(cfg, read, material)))
            for tag, cand in blob_candidates(cfg, read, material):
                hit = try_candidate(cand, page, tag)
                if hit:
                    print(f"*** 命中! {tag} -> {hit[0]} key={hit[1].hex()}")
                    Path("recon/found_db_key4.txt").write_text(
                        f"{tag}\npasswd={cand.hex()}\nenc_key={hit[1].hex()}\n", encoding="utf-8")
                    return 0
            print("  未命中")
        finally:
            K.kernel32.CloseHandle(h)
    return 1


def read_remote_wxid(read, addr: int) -> bool:
    szb = read(addr + 0x48 + 16, 8)
    if len(szb) != 8:
        return False
    size = struct.unpack_from("<Q", szb)[0]
    if not (0 < size <= 64):
        return False
    raw = read(addr + 0x48, size) if size <= 15 else b""
    if not raw:
        pb = read(addr + 0x48, 8)
        if len(pb) != 8:
            return False
        raw = read(struct.unpack_from("<Q", pb)[0], size)
    return raw.startswith(b"wxid")


if __name__ == "__main__":
    raise SystemExit(main())
