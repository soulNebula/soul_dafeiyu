"""Recon v3：cfg 结构里所有 std::string 密文 × 多种 XOR 材料组合 × SQLCipher 参数，全部用页1 HMAC 验证。"""
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
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import try_db_key as K  # noqa: E402


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

PAGE_SZ, RESERVE_SZ = 4096, 80


def verify(page1: bytes, enc_key: bytes, iter_count: int = 256000,
           hashname: str = "sha512", hmac_hash=hashlib.sha512,
           reserve: int = 80) -> bool:
    salt = page1[:16]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, 32)
    data = page1[16: PAGE_SZ - reserve + 16]
    stored = page1[PAGE_SZ - (hmac_hash().digest_size): PAGE_SZ]
    hm = hmac_mod.new(mac_key, data, hmac_hash)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored


def try_passphrase(passwd: bytes, page1s):
    for dbname, p1 in page1s:
        salt = p1[:16]
        for it in (256000, 64000, 4000):
            for hn in ("sha512", "sha1"):
                try:
                    derived = hashlib.pbkdf2_hmac(hn, passwd, salt, it, 32)
                except Exception:
                    continue
                hh = hashlib.sha512 if hn == "sha512" else hashlib.sha1
                reserve = 80 if hn == "sha512" else 48
                if verify(p1, derived, it, hn, hh, reserve):
                    return f"pbkdf2-{hn}-{it}", dbname, derived
        for raw in (passwd, (passwd + b"\x00" * 32)[:32], (passwd + b"\x00" * 48)[:48]):
            if len(raw) in (32, 48) and verify(p1, raw if len(raw) == 32 else raw[:32]):
                return "raw", dbname, raw[:32]
    return None


def main() -> int:
    acc = pick_account()
    print("活跃账号:", acc)
    page1s = []
    for db in (acc / "db_storage/emoticon/emoticon.db",
               acc / "db_storage/message/message_0.db",
               acc / "db_storage/contact/contact.db"):
        if db.exists():
            page1s.append((db.name, db.read_bytes()[:PAGE_SZ]))
    print("校验库:", [n for n, _ in page1s])
    for pid in K.weixin_pids():
        mod = K.find_module(pid)
        if not mod:
            continue
        base, size, path = mod
        material = K.extract_material(path)
        if not material:
            continue
        print(f"PID {pid} material={material.hex()}")
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
                    wx = read_remote_string(read, c + 0x48)
                    if wx.startswith("wxid"):
                        cfg = c
                        break
                if cfg:
                    break
            if not cfg:
                continue
            print("cfg:", hex(cfg))
            # 材料组合
            mats = {
                "eq": material,
                "rep": material + material,
                "rev": material[::-1],
                "rep_rev": (material + material)[::-1],
                "inv": bytes(b ^ 0xFF for b in material),
            }
            # chunk 顺序置换（4×8字节）
            chunks = [material[i:i + 8] for i in range(0, 32, 8)]
            import itertools

            for perm in itertools.permutations(range(4)):
                mats["perm" + "".join(map(str, perm))] = b"".join(chunks[i] for i in perm)
            for off in range(0x40, 0x400, 8):
                raw = K._read_remote_bytes(read, cfg + off)
                if not raw or len(raw) not in (16, 24, 32, 48, 64):
                    continue
                for mname, mat in mats.items():
                    if len(mat) >= len(raw):
                        master = bytes(a ^ b for a, b in zip(raw, mat))
                    else:
                        if len(raw) % len(mat) != 0:
                            continue
                        master = bytes(a ^ b for a, b in zip(raw, mat * (len(raw) // len(mat))))
                    variants = {"raw": master}
                    try:
                        hx = master.decode("ascii")
                        if all(ch in "0123456789abcdefABCDEF" for ch in hx):
                            variants["hex"] = bytes.fromhex(hx)
                            variants["ascii"] = master
                    except Exception:
                        pass
                    for vname, passwd in variants.items():
                        hit = try_passphrase(passwd, page1s)
                        if hit:
                            print(f"*** 命中! cfg+{off:#x} mat={mname} passwd={vname}")
                            print("   ", hit)
                            Path("recon/found_db_key3.json").write_text(
                                __import__("json").dumps(
                                    {"cfg_off": off, "mat": mname, "passwd_variant": vname,
                                     "passwd_hex": passwd.hex(), "hit": list(hit[:2]),
                                     "enc_key": hit[2].hex()}, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                            return 0
        finally:
            K.kernel32.CloseHandle(h)
    print("未命中")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
