"""Recon v5：按 wechatauto 的「Config.Cipher 内存扫描」主路径提取 WCDB 数据库密钥。

原理（社区公开实现，微信 4.1+ 主路径）：
  1. 进程内存里找 `com.Tencent.WCDB.Config.Cipher` 字符串；
  2. 找 {字符串指针, len} 这对 16 字节对象（定位到 string 的 node）；
  3. node[0x28] = config 对象指针；config+0x88 是 {data_ptr, data_len}；
  4. 读出 blob 后与 27 字节代码掩码 XOR → 得到 `x'<64hex key><32hex salt>'` 明文；
  5. 64hex = 32 字节 key，96hex = key+salt（明文头库）；用 SQLCipher 页1 HMAC 强校验。
本版本（4.1.15.56）若结构偏移有变化，用自适应扫描兜底。
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac as hmac_mod
import io
import re
import struct
import sys
from pathlib import Path

if __name__ == "__main__":
    try:
        sys.stdout = io.TextIOWrapper(sys.__stdout__.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import try_db_key as K  # noqa: E402

CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
HEX_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
ACC = Path.home() / "Documents" / "xwechat_files" / "wxid_hsqe9w1lhl1j22_5663"
PAGE_SZ = 4096
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", ctypes.c_ulong), ("RegionSize", ctypes.c_size_t),
                ("State", ctypes.c_ulong), ("Protect", ctypes.c_ulong),
                ("Type", ctypes.c_ulong)]


k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.VirtualQueryEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t


def regions(pid: int):
    """返回可读内存区 [(base, size)]"""
    h = k32.OpenProcess(0x0400, False, pid)  # QUERY_INFORMATION
    if not h:
        return []
    out = []
    try:
        addr = 0
        mbi = MEMORY_BASIC_INFORMATION()
        while True:
            got = k32.VirtualQueryEx(ctypes.c_void_p(h), ctypes.c_void_p(addr),
                                     ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not got:
                break
            base = int(mbi.BaseAddress or 0)
            size = int(mbi.RegionSize or 0)
            if (mbi.State == MEM_COMMIT and size and not (mbi.Protect & PAGE_GUARD)
                    and not (mbi.Protect & PAGE_NOACCESS)):
                out.append((base, size))
            addr = base + size
    finally:
        k32.CloseHandle(h)
    return out


def read_mem(read, addr, n):
    buf = read(addr, n)
    return buf if buf and len(buf) == n else None


def find_bytes(read, needle: bytes, max_hits: int = 64) -> list[int]:
    """扫描进程可读内存，返回 needle 出现的地址列表。"""
    hits: list[int] = []
    for base, size in regions_read:
        step = 32 * 1024 * 1024
        for off in range(0, size, step):
            chunk = read(base + off, min(step, size - off))
            if not chunk:
                continue
            start = 0
            while True:
                i = chunk.find(needle, start)
                if i < 0:
                    break
                hits.append(base + off + i)
                if len(hits) >= max_hits:
                    return hits
                start = i + 1
    return hits


def verify(page1: bytes, key: bytes, reserve: int = 80) -> bool:
    salt = page1[:16]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, 32)
    data = page1[16: PAGE_SZ - reserve + 16]
    stored = page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored


def verify_salted(page1: bytes, key: bytes, salt: bytes) -> bool:
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, 32)
    data = page1[16: PAGE_SZ - 80 + 16]
    stored = page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored


def key_candidates_from_blob(blob: bytes) -> set[bytes]:
    out: set[bytes] = set()
    for mask in (CONFIG_XOR_MASK, bytes(reversed(CONFIG_XOR_MASK)), b""):
        try:
            if mask:
                decoded = bytes(v ^ mask[i % len(mask)] for i, v in enumerate(blob))
            else:
                decoded = blob
        except Exception:
            continue
        for m in HEX_RE.finditer(decoded):
            run = m.group(1).decode().lower()
            starts = [0]
            if len(run) > 96:
                starts += list(range(0, len(run) - 63, 32))
                starts.append(len(run) - 64)
            for s in dict.fromkeys(starts):
                if s + 64 > len(run):
                    continue
                try:
                    out.add(bytes.fromhex(run[s:s + 64]))
                    if s + 96 <= len(run):
                        out.add(bytes.fromhex(run[s:s + 96]))
                except ValueError:
                    continue
    return out


def plausible_config_ptr(v: int) -> bool:
    return 0x10000 <= v < 0x800000000000


def parse_obj_candidates(read, config_ptr: int) -> set[bytes]:
    """从 config 对象周围自适应扫描 {data_ptr, data_len} 结构，收集密钥候选。"""
    cands: set[bytes] = set()
    for objoff in range(0x40, 0x200, 8):
        raw = read_mem(read, config_ptr + objoff, 0x20)
        if not raw:
            continue
        for poff, loff in ((0x0, 0x8), (0x8, 0x10), (0x0, 0x10), (0x10, 0x8)):
            data_ptr = struct.unpack_from("<Q", raw, poff)[0]
            data_len = struct.unpack_from("<Q", raw, loff)[0]
            if not (0 < data_len <= 1024 and plausible_config_ptr(data_ptr)):
                continue
            blob = read_mem(read, data_ptr, int(data_len))
            if not blob:
                continue
            cands |= key_candidates_from_blob(blob)
    return cands


def main() -> int:
    dbs = [ACC / "db_storage/message/message_0.db",
           ACC / "db_storage/emoticon/emoticon.db",
           ACC / "db_storage/contact/contact.db"]
    page1s = {db.name: db.read_bytes()[:PAGE_SZ] for db in dbs if db.exists()}
    print("校验库:", list(page1s))

    for pid in K.weixin_pids()[:1]:
        print("PID:", pid)
        h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            print("  OpenProcess 失败")
            continue

        def read(addr, n):
            buf = ctypes.create_string_buffer(n)
            br = ctypes.c_size_t(0)
            if k32.ReadProcessMemory(ctypes.c_void_p(h), ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
                return buf.raw[: br.value]
            return None

        try:
            global regions_read
            regions_read = regions(pid)
            print("  可读区数:", len(regions_read), "总大小约",
                  round(sum(s for _, s in regions_read) / 2**20), "MB")
            print("  查找 Config.Cipher 字符串 ...")
            needles = find_bytes(read, CONFIG_CIPHER_NAME)
            print("  字符串命中:", [hex(a) for a in needles])
            if not needles:
                continue
            pairs = [struct.pack("<Q", a) + struct.pack("<Q", len(CONFIG_CIPHER_NAME)) for a in needles]
            all_cands: set[bytes] = set()
            mode = 0
            for pair in pairs:
                for qaddr in find_bytes(read, pair):
                    # 标准布局
                    node = read_mem(read, qaddr - 0x10, 0x50)
                    cfg_ptr = None
                    if node:
                        if (struct.unpack_from("<Q", node, 0x10)[0] in needles
                                and struct.unpack_from("<Q", node, 0x18)[0] == len(CONFIG_CIPHER_NAME)):
                            v = struct.unpack_from("<Q", node, 0x28)[0]
                            if plausible_config_ptr(v):
                                cfg_ptr = v
                    if cfg_ptr:
                        mode += 1
                        all_cands |= parse_obj_candidates(read, cfg_ptr)
                    # 自适应：在 qaddr 周围找 config 指针
                    for delta in range(-0x40, 0x60, 8):
                        raw = read_mem(read, qaddr + delta, 0x40)
                        if not raw:
                            continue
                        for off in range(0, 0x38, 8):
                            v = struct.unpack_from("<Q", raw, off)[0]
                            if plausible_config_ptr(v):
                                all_cands |= parse_obj_candidates(read, v)
            print(f"  标准布局命中 node 数: {mode}，候选密钥材料: {len(all_cands)}")
            for cand in all_cands:
                if len(cand) == 32:
                    for dname, p1 in page1s.items():
                        if verify(p1, cand):
                            print(f"*** 成功! 标准库 {dname} key={cand.hex()}")
                            Path("recon/found_db_key5.txt").write_text(f"{dname}\nkey={cand.hex()}\n", encoding="utf-8")
                            return 0
                elif len(cand) == 48:
                    k32_, salt = cand[:32], cand[32:]
                    for dname, p1 in page1s.items():
                        if verify(p1, k32_) or verify_salted(p1, k32_, salt):
                            print(f"*** 成功! 库 {dname} key+salt={cand.hex()}")
                            Path("recon/found_db_key5.txt").write_text(f"{dname}\nkey={k32_.hex()}\nsalt={salt.hex()}\n", encoding="utf-8")
                            return 0
            print("  未命中")
        finally:
            k32.CloseHandle(h)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
