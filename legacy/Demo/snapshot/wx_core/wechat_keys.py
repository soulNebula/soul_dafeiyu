"""从微信进程内存提取图片解密密钥（用户显式授权，只读，不写内存/不注入）。

原理（社区公开方案：Config.Cipher 结构）::

    Weixin.dll 镜像里找到 "global_config" SSO 地标
      → 指针链：v18 = *(landmark - backoff)，cfg = *(v18 + cfg_off)
      → cfg + 0x40 = cfgDword（u32）
      → cfg + 0x48 = wxId（MSVC std::string）
    imageXorKey = cfgDword & 0xFF
    imageAesKey = md5(str(cfgDword) + wxId).hexdigest()[:16]

不同微信小版本会挪动 backoff/cfg_off 偏移，所以这里**用"能否解密最近 .dat"自动验证**：
枚举一批候选偏移组合，用 AES 解 .dat 首块、校验图片魔数，命中即锁定，因此不依赖版本硬编码。

密钥只在内存里读一次，成功后缓存到 config.json 的 image_xor_key / image_aes_key，
之后解密图片完全本地运算，不再碰微信进程。

红线：只读 OpenProcess/ReadProcessMemory/VirtualQueryEx；不写内存、不注入、不 Hook、
不修改微信任何文件。仅在用户配置 auto_extract_image_key=true（默认开启，用户已授权）时调用。
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import struct
import sys
import time
from ctypes import wintypes
from pathlib import Path

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MAGICS = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF", b"wxgf")
LANDMARK = b"global_config"
V2_MAGIC = b"\x07\x08V2\x08\x07"

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
                ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wintypes.DWORD), ("hModule", ctypes.c_void_p),
                ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260)]


class KeyExtractError(RuntimeError):
    pass


def weixin_pids() -> list[int]:
    import psutil

    got = []
    for pr in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            if (pr.info["name"] or "").lower() == "weixin.exe":
                got.append((pr.info["pid"], pr.info["memory_info"].rss))
        except Exception:
            continue
    got.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in got]


def _find_module(pid: int, name: str = "weixin.dll"):
    snap = kernel32.CreateToolhelp32Snapshot(0x08 | 0x10, pid)
    if not snap or snap == -1:
        return None
    try:
        me = MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(me)
        ok = kernel32.Module32FirstW(snap, ctypes.byref(me))
        while ok:
            if me.szModule and me.szModule.lower() == name:
                return int(me.modBaseAddr or 0), int(me.modBaseSize), me.szExePath
            me.dwSize = ctypes.sizeof(me)
            ok = kernel32.Module32NextW(snap, ctypes.byref(me))
    finally:
        kernel32.CloseHandle(snap)
    return None


def _dat_probes(limit: int = 3, account_dir=None) -> list[bytes]:
    """V2 图片缓存的加密首块（16 字节），用于验证密钥。

    account_dir 指定时只用该账号目录下的 .dat（换账号后必须用新账号的样本验证，
    否则会拿旧账号的密钥去套新账号，永远验不过）。
    """
    from . import media_cache

    out: list[bytes] = []
    if account_dir is not None:
        try:
            files = sorted(account_dir.glob("msg/attach/*/*/Img/*.dat"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            files = []
        for f in files[:120]:
            try:
                with open(f, "rb") as fh:
                    head = fh.read(31)
            except OSError:
                continue
            if head[:6] == V2_MAGIC and len(head) == 31:
                out.append(head[15:31])
            if len(out) >= limit:
                break
        return out
    for cf in media_cache.recent_images(limit=60):
        try:
            with open(cf.path, "rb") as f:
                head = f.read(31)
        except OSError:
            continue
        if head[:6] == V2_MAGIC and len(head) == 31:
            out.append(head[15:31])
        if len(out) >= limit:
            break
    return out


def _aes_ok(aes_key: str, probes: list[bytes]) -> bool:
    try:
        from Crypto.Cipher import AES

        cipher = AES.new(aes_key.encode("ascii")[:16], AES.MODE_ECB)
        for probe in probes:
            if not any(cipher.decrypt(probe).startswith(m) for m in MAGICS):
                return False
        return True
    except Exception:
        return False


def _read_remote_string(read, addr: int) -> str:
    """读 MSVC std::string（SSO：size<=15 内联，否则堆指针）。"""
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


def locate_cfg(pid: int, probes: list[bytes], timeout: float = 60.0):
    """定位 cfg 并返回 (cfg_dword, wxid, aes_key, xor_key, offsets)。

    失败抛 KeyExtractError。该函数是纯只读内存操作。
    """
    mod = _find_module(pid)
    if not mod:
        raise KeyExtractError("没找到 weixin.dll 模块")
    base, size, _path = mod
    h = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        raise KeyExtractError(f"OpenProcess 失败（可能需要管理员权限），err={ctypes.get_last_error()}")

    def read(addr: int, n: int) -> bytes:
        buf = ctypes.create_string_buffer(n)
        br = ctypes.c_size_t(0)
        if kernel32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
            return buf.raw[: br.value]
        return b""

    deadline = time.time() + timeout
    try:
        image = read(base, size)
        if not image:
            raise KeyExtractError("读取 weixin.dll 映像失败")
        positions = []
        start = 0
        while True:
            i = image.find(LANDMARK, start)
            if i < 0:
                break
            positions.append(base + i)
            start = i + 1
        if not positions:
            raise KeyExtractError("没找到 global_config 地标（微信版本可能更新）")
        for lm in positions:
            for back in range(0x80, 0x1C1, 8):
                if time.time() > deadline:
                    raise KeyExtractError("扫描超时")
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
                    for woff in (0x48, 0x50, 0x40, 0x58):
                        wxid = _read_remote_string(read, cfg + woff)
                        if not wxid.startswith("wxid") or len(wxid) < 8:
                            continue
                        for dwoff in range(0x30, 0x51, 4):
                            dwb = read(cfg + dwoff, 4)
                            if len(dwb) != 4:
                                continue
                            dw = struct.unpack_from("<I", dwb)[0]
                            aes = hashlib.md5((str(dw) + wxid).encode()).hexdigest()[:16]
                            if _aes_ok(aes, probes):
                                return dw, wxid, aes, dw & 0xFF, {
                                    "back": back, "cfg_off": cfg_off,
                                    "wx_off": woff, "dw_off": dwoff,
                                    "module": hex(base),
                                }
        raise KeyExtractError("扫描完成但没有找到能解密 .dat 的 cfg 组合")
    finally:
        kernel32.CloseHandle(h)


def extract_image_keys(config: dict | None = None, force: bool = False,
                       account_dir=None) -> dict | None:
    """提取图片密钥（失败返回 None）。成功返回 {"xor_key":int,"aes_key":str,"wxid":str}。

    force=True 或 account_dir 指定时忽略 config 里的缓存（换账号场景）。
    """
    if (not force and account_dir is None and config
            and str(config.get("image_xor_key") or "").strip()
            and str(config.get("image_aes_key") or "").strip()):
        try:
            xor = int(str(config["image_xor_key"]).strip(), 0)
            return {"xor_key": xor, "aes_key": str(config["image_aes_key"]).strip(),
                    "wxid": str(config.get("image_wxid") or ""), "source": "config"}
        except ValueError:
            return None
    probes = _dat_probes(account_dir=account_dir)
    if not probes:
        return None
    last_err = ""
    for pid in weixin_pids():
        try:
            dw, wxid, aes, xor, meta = locate_cfg(pid, probes)
            return {"xor_key": xor, "aes_key": aes, "wxid": wxid, "cfg_dword": dw,
                    "source": "memory", "meta": meta}
        except KeyExtractError as exc:
            last_err = str(exc)
            continue
    if last_err:
        print(f"[wechat_keys] 提取失败: {last_err}", file=sys.stderr)
    return None
