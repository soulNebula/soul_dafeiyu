"""Recon v2：在 cfg 结构里扫描"主密钥密文"字符串（不假设 0x2B8 偏移），
用 SQLCipher 页1 HMAC 主动验证，找到真正能解开数据库的密钥。"""
from __future__ import annotations

import ctypes
import hashlib
import hmac as hmac_mod
import struct
import sys
from pathlib import Path

sys.stdout = None  # 占位，稍后设置


def _out():
    import io
    return io.TextIOWrapper(sys.__stdout__.buffer, encoding="utf-8", errors="replace")


sys.stdout = _out()

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import try_db_key as K  # noqa: E402


def read_remote_string(read, addr: int) -> str:
    szb = read(addr + 16, 8)
    if len(szb) != 8:
        return ""
    size = struct.unpack_from("<Q", szb)[0]
    if not (0 < size <= 64):
        return ""
    if size <= 15:
        raw = read(addr, size)
    else:
        pb = read(addr, 8)
        if len(pb) != 8:
            return ""
        raw = read(struct.unpack_from("<Q", pb)[0], size)
    if not raw:
        return ""
    return raw[:size].decode("utf-8", "ignore")

PAGE_SZ, RESERVE_SZ, KDF_ITER = 4096, 80, 256000


def pick_account() -> Path:
    best = None
    for d in K.ROOT.iterdir():
        if not d.is_dir() or not d.name.startswith("wxid_"):
            continue
        try:
            wal = d / "db_storage/message/message_0.db-wal"
            m = wal.stat().st_mtime if wal.exists() else (d / "db_storage/message/message_0.db").stat().st_mtime
        except Exception:
            continue
        if best is None or m > best[0]:
            best = (m, d)
    return best[1] if best else None


def verify(page1: bytes, enc_key: bytes) -> bool:
    salt = page1[:16]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, 32)
    data = page1[16: PAGE_SZ - RESERVE_SZ + 16]
    stored = page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored


def try_master(master: bytes, page1s: list[tuple[str, bytes]]):
    variants = [("ascii", master)]
    if len(master) == 32:
        try:
            variants.append(("hex16", bytes.fromhex(master.decode("ascii"))))
        except Exception:
            pass
    if len(master) == 64:
        try:
            variants.append(("hex32", bytes.fromhex(master.decode("ascii"))))
        except Exception:
            pass
    for vname, passwd in variants:
        for dbname, p1 in page1s:
            salt = p1[:16]
            derived = hashlib.pbkdf2_hmac("sha512", passwd, salt, KDF_ITER, 32)
            if verify(p1, derived):
                return vname, dbname, derived
            # raw key 模式
            rawcands = []
            rawcands.append(passwd)
            rawcands.append((passwd + b"\x00" * 32)[:32])
            if len(passwd) == 64:
                rawcands.append(passwd[:32])
            for raw in rawcands:
                if len(raw) == 32 and verify(p1, raw):
                    return vname + "/raw", dbname, raw
    return None


def main() -> int:
    acc = pick_account()
    print("活跃账号:", acc)
    db_paths = [acc / "db_storage/emoticon/emoticon.db",
                acc / "db_storage/message/message_0.db",
                acc / "db_storage/contact/contact.db"]
    page1s = []
    for db in db_paths:
        if db.exists():
            page1s.append((db.name, db.read_bytes()[:PAGE_SZ]))
    print("校验用库:", [n for n, _ in page1s])

    for pid in K.weixin_pids():
        mod = K.find_module(pid)
        if not mod:
            continue
        base, size, path = mod
        print(f"PID {pid} dll={path}")
        material = K.extract_material(path)
        print("  material:", material.hex() if material else None)
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
            if not image:
                continue
            pos = image.find(K.LANDMARK)
            if pos < 0:
                continue
            lm = base + pos
            cfgs = []
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
                    cfg = struct.unpack_from("<Q", cb)[0]
                    if not (0x10000 < cfg < 0x800000000000):
                        continue
                    wx = read_remote_string(read, cfg + 0x48)
                    if wx.startswith("wxid"):
                        cfgs.append((cfg, back, cfg_off))
            print("  cfg 候选:", [(hex(c), b, o) for c, b, o in cfgs[:5]])
            for cfg, back, cfg_off in cfgs:
                # 扫描 cfg 结构里的 std::string（0x40..0x400）
                for off in range(0x40, 0x400, 8):
                    raw = K._read_remote_bytes(read, cfg + off)
                    if not raw:
                        continue
                    cands = []
                    if len(raw) == len(material):
                        cands.append(("xor_eq", bytes(a ^ b for a, b in zip(raw, material))))
                    elif len(material) == 32 and len(raw) == 64:
                        m2 = (material + material)
                        cands.append(("xor_rep", bytes(a ^ b for a, b in zip(raw, m2))))
                    elif len(material) == 64 and len(raw) == 32:
                        cands.append(("xor_trunc", bytes(a ^ b for a, b in zip(raw, material[:32]))))
                    for tag, master in cands:
                        ascii_hex = all(chr(b) in "0123456789abcdefABCDEF" for b in master)
                        if not ascii_hex:
                            continue
                        hit = try_master(master, page1s)
                        if hit:
                            print(f"*** 命中! cfg+{off:#x} {tag} master={master.decode()}")
                            print("   ", hit)
                            Path("recon/found_db_key2.txt").write_text(
                                f"cfg_off={off:#x}\nvariant={tag}\nmaster={master.decode()}\n"
                                f"key_variant={hit[0]}\ndb={hit[1]}\nenc_key={hit[2].hex()}\n",
                                encoding="utf-8")
                            return 0
        finally:
            K.kernel32.CloseHandle(h)
    print("未命中")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
