"""Recon：从 cfg 结构取主密钥（cfg+0x2B8 密文 XOR dll movabs 材料），解密 SQLCipher 数据库，
并探索 emoticon.db / message DB 里表情包（贴纸）对应的原始加密信息。

用户已授权本地解密（忽略此前限制）。全部只读微信进程内存/本地文件。
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac as hmac_mod
import io
import os
import struct
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_VM_READ, PROCESS_QUERY_INFORMATION = 0x0010, 0x0400
LANDMARK = b"global_config"
V2_MAGIC = b"\x07\x08V2\x08\x07"
MASTER_DLL_PATTERN = bytes.fromhex(
    "83ec404889d64889cb0f57c00f1142100f11024c8bb1c8020000"
    "4883b9d0020000107209488b9bb8020000eb074881c3b8020000"
    "4d85f60f880a0200004983fe10736d4c89761048c746180f0000"
    "000f10030f110648b8"
)
MASTER_DLL_VERIFY = (b"488944242048b8", b"488944242848b8", b"488944243048b8")
PAGE_SZ = 4096
RESERVE_SZ = 80
KDF_ITER = 256000

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
                ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wintypes.DWORD), ("hModule", ctypes.c_void_p),
                ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260)]


ROOT = Path.home() / "Documents" / "xwechat_files"


def weixin_pids():
    import psutil

    out = []
    for pr in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            if (pr.info["name"] or "").lower() == "weixin.exe":
                out.append((pr.info["pid"], pr.info["memory_info"].rss))
        except Exception:
            continue
    out.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in out]


def find_module(pid, name="weixin.dll"):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    snap = k32.CreateToolhelp32Snapshot(0x08 | 0x10, pid)
    if not snap or snap == -1:
        return None
    try:
        me = MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(me)
        ok = k32.Module32FirstW(snap, ctypes.byref(me))
        while ok:
            if me.szModule and me.szModule.lower() == name:
                return int(me.modBaseAddr or 0), int(me.modBaseSize), me.szExePath
            me.dwSize = ctypes.sizeof(me)
            ok = k32.Module32NextW(snap, ctypes.byref(me))
    finally:
        k32.CloseHandle(snap)
    return None


def extract_material(dll_path: str) -> bytes | None:
    try:
        data = Path(dll_path).read_bytes()
    except OSError:
        return None
    hit = data.find(MASTER_DLL_PATTERN)
    if hit < 0:
        return None
    take = min(200, len(data) - hit)
    hex_txt = data[hit:hit + take].hex()
    hex_txt = hex_txt[len(MASTER_DLL_PATTERN) * 2:]
    key = ""
    for vf in MASTER_DLL_VERIFY:
        if len(hex_txt) < 30 or hex_txt[16:30] != vf.decode():
            return None
        key += hex_txt[0:16]
        hex_txt = hex_txt[30:]
    if len(hex_txt) < 16:
        return None
    key += hex_txt[0:16]
    return bytes.fromhex(key)


def _read_remote_bytes(read, addr: int):
    szb = read(addr + 16, 8)
    if len(szb) != 8:
        return None
    size = struct.unpack_from("<Q", szb)[0]
    if not (0 < size <= 0x400):
        return None
    capb = read(addr + 24, 4)
    cap = struct.unpack_from("<I", capb)[0] if len(capb) == 4 else 0
    if (cap | 0xF) == 0xF:
        data = read(addr, size)
    else:
        pb = read(addr, 8)
        if len(pb) != 8:
            return None
        data = read(struct.unpack_from("<Q", pb)[0], size)
    if not data or len(data) != size:
        return None
    return data


def locate_cfg_and_master(pid: int):
    mod = find_module(pid)
    if not mod:
        return None
    base, size, path = mod
    h = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return None

    def read(addr, n):
        buf = ctypes.create_string_buffer(n)
        br = ctypes.c_size_t(0)
        if kernel32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
            return buf.raw[: br.value]
        return b""

    try:
        image = read(base, size)
        if not image:
            return None
        pos = image.find(LANDMARK)
        if pos < 0:
            return None
        lm = base + pos
        material = extract_material(path)
        if not material:
            print("  dll 特征码没匹配到")
            return None
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
                cipher = _read_remote_bytes(read, cfg + 0x2B8)
                if not cipher or len(cipher) != len(material):
                    continue
                master = bytes(a ^ b for a, b in zip(cipher, material))
                return cfg, master, {"back": back, "cfg_off": cfg_off, "material": material}
    finally:
        kernel32.CloseHandle(h)
    return None


def derive_and_verify(master: bytes, db_path: Path) -> bytes | None:
    """返回 SQLCipher enc_key（32B）并验证 page1 HMAC。"""
    page1 = db_path.read_bytes()[:PAGE_SZ]
    if len(page1) < PAGE_SZ:
        return None
    salt = page1[:16]
    for master_use in (master, master[:32], master[-32:] if len(master) > 32 else master):
        derived = hashlib.pbkdf2_hmac("sha512", master_use, salt, KDF_ITER, 32)
        mac_salt = bytes(b ^ 0x3A for b in salt)
        mac_key = hashlib.pbkdf2_hmac("sha512", derived, mac_salt, 2, 32)
        data = page1[16: PAGE_SZ - RESERVE_SZ + 16]
        stored = page1[PAGE_SZ - 64: PAGE_SZ]
        hm = hmac_mod.new(mac_key, data, hashlib.sha512)
        hm.update(struct.pack("<I", 1))
        if hm.digest() == stored:
            return derived
    return None


def decrypt_db(db_path: Path, enc_key: bytes, out_path: Path) -> bool:
    from Crypto.Cipher import AES

    raw = db_path.read_bytes()
    n_pages = len(raw) // PAGE_SZ
    out = bytearray()
    for pg in range(n_pages):
        page = raw[pg * PAGE_SZ:(pg + 1) * PAGE_SZ]
        iv = page[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
        if pg == 0:
            enc = page[16: PAGE_SZ - RESERVE_SZ]
            dec = b"SQLite format 3\x00" + AES.new(enc_key, AES.MODE_CBC, iv).decrypt(enc) + b"\x00" * RESERVE_SZ
        else:
            enc = page[: PAGE_SZ - RESERVE_SZ]
            dec = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(enc) + b"\x00" * RESERVE_SZ
        out += dec
    out_path.write_bytes(bytes(out))
    return True


def main() -> int:
    acc = None
    for d in ROOT.iterdir():
        if d.is_dir() and d.name.startswith("wxid_") and (d / "db_storage/emoticon/emoticon.db").exists():
            acc = d
            break
    if acc is None:
        print("找不到账号目录/emoticon.db")
        return 1
    print("账号目录:", acc)
    for pid in weixin_pids():
        print("PID", pid, "提取 cfg/主密钥 ...")
        got = locate_cfg_and_master(pid)
        if not got:
            continue
        cfg, master, meta = got
        print(f"  cfg={cfg:#x} master={master.hex()} meta={meta}")
        for dbname in ("db_storage/emoticon/emoticon.db",
                       "db_storage/message/message_0.db",
                       "db_storage/contact/contact.db"):
            db = acc / dbname
            if not db.exists():
                continue
            key = derive_and_verify(master, db)
            print(f"  {dbname}: key={'OK' if key else 'FAIL'}")
            if key:
                out = Path("recon") / ("dec_" + db.name)
                decrypt_db(db, key, out)
                print(f"    已解密 -> {out} ({out.stat().st_size} bytes)")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
