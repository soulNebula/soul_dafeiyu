# -*- coding: utf-8 -*-
"""微信 4.x SQLCipher 解密内核（移植自只读参考 ``wx_box/recon/ref_db.py``）。

只保留数据库层需要的最小集合，全部去掉 ``wechatauto.logger`` 依赖：

1. 定位数据目录 / 账号目录（配置文件、注册表、默认 Documents 兜底）；
2. 只读扫描 ``Weixin.exe`` 进程内存里的 ``com.Tencent.WCDB.Config.Cipher``
   对象，提取每个库的 32 字节密钥（SQLCipher 4；页 1 HMAC-SHA512 强校验）；
3. 按页 AES-256-CBC 解密，合并 ``-wal`` 增量帧后产出标准 sqlite 文件；
4. 校验融合结果（``PRAGMA quick_check``，防微信 checkpoint 撕裂快照）。

本模块**不写微信目录**、不做任何注入/Hook；解密产物路径由调用方（``wx4.db``）
决定。可预期失败抛 ``Wx4Error`` 的底层等价异常（``RuntimeError`` / ``OSError``），
由 ``wx4.db`` 统一翻译成中文 ``Wx4Error``。
"""
from __future__ import annotations

import ctypes
import glob
import hashlib
import hmac as hmac_mod
import json
import logging
import os
import re
import struct
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger(__name__)

PAGE_SZ = 4096
RESERVE_SZ = 80  # IV(16) + HMAC(64)
KDF_ITER = 256000  # 主密钥→库密钥 PBKDF2 迭代（微信魔改 WCDB，实测确认）

CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
HEX_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")

WAL_HEADER_SZ = 32   # WCDB WAL 文件头
WAL_FRAME_SZ = 4120  # 帧头 24 字节（大端页号/校验等）+ 4096 加密页


# ---------------------------------------------------------------------------
# SQLCipher 4 密码学
# ---------------------------------------------------------------------------
def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def pbkdf2(passwd: bytes, salt: bytes, iters: int = KDF_ITER) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", passwd, salt, iters, dklen=32)


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()


def split_key(key: bytes) -> Tuple[bytes, Optional[bytes]]:
    """拆分密钥形态：32 字节 = 裸 key（salt 取文件头前 16 字节）；
    48 字节 = SQLCipher 4「Raw Key with Explicit Salt」（前 32B key + 后 16B salt，
    用于 cipher_plaintext_header_size 明文头模式）。"""
    if len(key) == 48:
        return key[:32], key[32:]
    return key, None


def verify_enc_key(enc_key: bytes, page1: bytes,
                   salt: Optional[bytes] = None) -> bool:
    """页 1 HMAC 校验：确认 enc_key 是否为该库的 SQLCipher 4 密钥（零误报）。"""
    if len(page1) < PAGE_SZ:
        return False
    if len(enc_key) == 48 and salt is None:
        enc_key, salt = enc_key[:32], enc_key[32:]
    if salt is None:
        salt = page1[:16]
    elif len(salt) != 16:
        return False
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = pbkdf2(enc_key, mac_salt, 2)
    hmac_data = page1[16: PAGE_SZ - RESERVE_SZ + 16]
    stored_hmac = page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


def decrypt_page(enc_key: bytes, page: bytes, pgno: int) -> bytes:
    """解密一页；页 1 保留/补回 16 字节明文头（48B key 为明文头模式）。"""
    iv = page[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        enc = page[16: PAGE_SZ - RESERVE_SZ]
        if len(enc_key) == 48:
            return page[:16] + aes_cbc_decrypt(enc_key[:32], iv, enc) + b"\x00" * RESERVE_SZ
        return b"SQLite format 3\x00" + aes_cbc_decrypt(enc_key, iv, enc) + b"\x00" * RESERVE_SZ
    enc = page[: PAGE_SZ - RESERVE_SZ]
    return aes_cbc_decrypt(enc_key[:32] if len(enc_key) == 48 else enc_key,
                           iv, enc) + b"\x00" * RESERVE_SZ


def decrypt_file(src: str | os.PathLike, dst: str | os.PathLike, key: bytes) -> None:
    """整库解密：按 4096 字节页 AES-CBC 解密，输出标准 sqlite 文件。"""
    size = os.path.getsize(src)
    pages = size // PAGE_SZ + (1 if size % PAGE_SZ else 0)
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for pgno in range(1, pages + 1):
            page = fin.read(PAGE_SZ)
            if not page:
                break
            if len(page) < PAGE_SZ:
                page = page + b"\x00" * (PAGE_SZ - len(page))
            fout.write(decrypt_page(key, page, pgno))


def sqlite_uri(path: str | os.PathLike) -> str:
    """只读 sqlite URI（Windows 路径转正斜杠并转义空格等字符）。"""
    from urllib.parse import quote

    return "file:%s?mode=ro" % quote(Path(path).as_posix(), safe="/:")


def make_rollback_mode(path: str | os.PathLike) -> None:
    """把解密产物的头标记从 WAL(2/2) 改成 rollback(1/1)。

    解密出的主库内容已经完整（WAL 帧也合并进来了），但页 1 头里仍留着微信的
    WAL 标记。若保持 WAL 标记，标准 sqlite 只读连接会尝试在缓存目录旁创建
    ``-wal``/``-shm`` 附属文件；改成 rollback 后只读查询干净、不产生副作用。
    明文头模式（48 字节 key）的页 1 不是标准 magic，此时跳过。
    """
    try:
        with open(path, "r+b") as f:
            head = f.read(20)
            if len(head) < 20 or head[:16] != b"SQLite format 3\x00":
                return
            if head[18:20] == b"\x01\x01":
                return
            f.seek(18)
            f.write(b"\x01\x01")
    except OSError:
        return


def check_sqlite_ok(path: str | os.PathLike) -> bool:
    """``PRAGMA quick_check`` 全库校验（含数据页/索引页，能发现 WAL 错位页）。

    微信的 ``*_fts.db`` 用自定义 FTS 分词器（``MMFtsTokenizer``），标准 sqlite
    不认识：quick_check 一碰到虚拟表就抛 "no such tokenizer"。这不是库损坏，
    只是无法在普通 sqlite 里实例化 FTS 表，所以此时退化为「schema 可读即 OK」。
    """
    import sqlite3

    conn = None
    try:
        conn = sqlite3.connect(sqlite_uri(path), uri=True)
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        except sqlite3.Error as exc:
            if _is_unknown_module_error(exc):
                try:
                    conn.execute(
                        "SELECT count(*) FROM sqlite_master").fetchone()
                    return True
                except sqlite3.Error:
                    return False
            return False
        return bool(rows) and all(str(r[0]) == "ok" for r in rows)
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _is_unknown_module_error(exc: BaseException) -> bool:
    """是否为「标准 sqlite 不认识微信自定义模块/分词器」这类错误（非数据损坏）。"""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("no such tokenizer", "no such module", "no such vtab",
                       "no such virtual table", "unable to use function")
    )


def sqlite_text_factory(data: bytes):
    """TEXT 列解码：合法 UTF-8 返回 str，否则原样返回 bytes（二进制内容）。"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data


# ---------------------------------------------------------------------------
# WAL 增量合并
# ---------------------------------------------------------------------------
def merge_wal(dst: str | os.PathLike, wal_path: str | os.PathLike,
              key: bytes, from_frame: int = 0) -> int:
    """把加密 ``-wal`` 的帧按页号覆盖进已解密主库，返回已应用到的帧序号。

    帧结构（WCDB，大端）：[0:4] 页号, [4:8] 提交标记, [8:16] salt, [16:24] 校验。
    只合并 salt 与当前 WAL 头一致的帧：微信 checkpoint 会重置 WAL（salt+1 并清零
    写游标），旧世代帧若被合并会用过期页覆盖新数据 → 库损坏。
    """
    if not os.path.exists(dst) or not os.path.exists(wal_path):
        return 0
    out = open(dst, "r+b")
    try:
        last = from_frame
        max_pgno = 0
        with open(wal_path, "rb") as wal:
            wal_hdr = wal.read(WAL_HEADER_SZ)
            wal_salt = wal_hdr[16:24]
            wal_size = os.path.getsize(wal_path)
            n = (wal_size - WAL_HEADER_SZ) // WAL_FRAME_SZ
            for i in range(from_frame, n):
                wal.seek(WAL_HEADER_SZ + i * WAL_FRAME_SZ)
                hdr = wal.read(24)
                page = wal.read(PAGE_SZ)
                if len(page) < PAGE_SZ:
                    break
                pgno = struct.unpack(">I", hdr[:4])[0]
                last = i + 1
                if hdr[8:16] != wal_salt:
                    continue
                pt = decrypt_page(key, page, pgno)
                if pgno == 1:
                    # 明文头模式（48B key）页 1 解密后保留明文头，magic 可能不是
                    # 标准 SQLite；用偏移 16 的版本字节兜底校验。
                    if len(key) == 48:
                        if len(pt) < PAGE_SZ or pt[16:18] != b"\x01\x01":
                            continue
                    elif pt[:16] != b"SQLite format 3\x00":
                        continue
                elif pt[0] not in (0, 2, 5, 10, 13):
                    continue
                out.seek((pgno - 1) * PAGE_SZ)
                out.write(pt)
                max_pgno = max(max_pgno, pgno)
        out.flush()
        db_pages = (os.path.getsize(dst) + PAGE_SZ - 1) // PAGE_SZ
        out.seek(0)
        page1 = out.read(PAGE_SZ)
        hdr_pages = struct.unpack(">I", page1[28:32])[0]
        new_pages = max(hdr_pages, max_pgno, db_pages)
        if new_pages != hdr_pages:
            page1 = page1[:28] + struct.pack(">I", new_pages) + page1[32:]
            out.seek(0)
            out.write(page1)
        out.flush()
        return last
    finally:
        out.close()


# ---------------------------------------------------------------------------
# 进程内存扫描：Config.Cipher → 各库密钥
# ---------------------------------------------------------------------------
class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("__alignment1", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("__alignment2", wintypes.DWORD),
    ]


try:  # 只支持 Windows（微信 4.x 桌面端）
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.VirtualQueryEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_MBI), ctypes.c_size_t,
    ]
    _k32.VirtualQueryEx.restype = ctypes.c_size_t
    _k32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
except (AttributeError, OSError):  # pragma: no cover - 非 Windows
    _k32 = None


def supported() -> bool:
    """当前解释器能否读进程内存（需要 Windows + 64 位）。"""
    return _k32 is not None and sys.maxsize > 2 ** 32


def find_weixin_pids() -> List[int]:
    """列出 ``Weixin.exe`` 的 PID（微信 4.x 主进程名；无进程返回空列表）。"""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="gbk", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return []
    pids: List[int] = []
    for line in (r.stdout or "").strip().splitlines():
        parts = line.strip('"').split('","')
        if len(parts) >= 2 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def _find_bytes(h, read, needle: bytes) -> List[int]:
    """在目标进程所有可读私有内存区域里找 needle，返回命中地址列表。"""
    hits: List[int] = []
    addr = 0
    while True:
        mbi = _MBI()
        r = _k32.VirtualQueryEx(h, ctypes.c_void_p(addr),
                                ctypes.byref(mbi), ctypes.sizeof(mbi))
        if r == 0:
            break
        if (mbi.State == 0x1000
                and (mbi.Protect & 0xFF) & 0xE6
                and not (mbi.Protect & 0x100)
                and 0 < mbi.RegionSize < 0x10000000):
            buf = read(mbi.BaseAddress or 0, mbi.RegionSize)
            if buf:
                base = mbi.BaseAddress or 0
                pos = 0
                while True:
                    pos = buf.find(needle, pos)
                    if pos < 0:
                        break
                    hits.append(base + pos)
                    pos += 1
        addr = (mbi.BaseAddress or 0) + mbi.RegionSize
    return hits


def _probable_key(b: bytes) -> bool:
    return (
        len(b) == 32
        and len(set(b)) >= 15
        and b not in {b"\x00" * 32, b"\xff" * 32}
    )


def _scan_candidates_pid(pid: int, seen: Set[bytes]) -> Set[Tuple[bytes, Optional[bytes]]]:
    """扫描一个微信进程的 WCDB 配置对象，收集候选密钥材料（不做账号校验）。"""
    h = _k32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not h:
        log.debug("OpenProcess 失败（PID %s，错误码 %s）", pid, ctypes.get_last_error())
        return set()
    out: Set[Tuple[bytes, Optional[bytes]]] = set()
    try:
        def read(addr: int, n: int):
            buf = ctypes.create_string_buffer(n)
            br = ctypes.c_size_t(0)
            if (_k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n,
                                       ctypes.byref(br)) and br.value):
                return buf.raw[: br.value]
            return None

        needles = _find_bytes(h, read, CONFIG_CIPHER_NAME)
        if not needles:
            return out
        pairs = [
            struct.pack("<Q", addr) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
            for addr in needles
        ]
        needle_set = set(needles)
        for pair in pairs:
            for qaddr in _find_bytes(h, read, pair):
                node = read(qaddr - 0x10, 0x50)
                if not node or len(node) < 0x40:
                    continue
                if struct.unpack_from("<Q", node, 0x10)[0] not in needle_set:
                    continue
                if struct.unpack_from("<Q", node, 0x18)[0] != len(CONFIG_CIPHER_NAME):
                    continue
                config_ptr = struct.unpack_from("<Q", node, 0x28)[0]
                if not (0x10000 <= config_ptr < 0x800000000000):
                    continue
                obj = read(config_ptr + 0x88, 0x28)
                if not obj or len(obj) < 0x18:
                    continue
                data_ptr = struct.unpack_from("<Q", obj, 0x8)[0]
                data_len = struct.unpack_from("<Q", obj, 0x10)[0]
                if not (0 < data_len <= 1024 and 0x10000 <= data_ptr < 0x800000000000):
                    continue
                blob = read(data_ptr, int(data_len))
                if not blob or len(blob) != data_len:
                    continue
                decoded = bytes(
                    v ^ CONFIG_XOR_MASK[i % len(CONFIG_XOR_MASK)]
                    for i, v in enumerate(blob)
                )
                for m in HEX_LITERAL_RE.finditer(decoded):
                    run = m.group(1).decode().lower()
                    starts = [0]
                    if len(run) > 96:
                        starts += list(range(0, len(run) - 63, 32))
                        starts.append(len(run) - 64)
                    for s in dict.fromkeys(starts):
                        if s + 64 > len(run):
                            continue
                        cand = bytes.fromhex(run[s:s + 64])
                        if cand in seen or not _probable_key(cand):
                            continue
                        seen.add(cand)
                        out.add((cand, None))
                        if s + 96 <= len(run):
                            out.add((cand, bytes.fromhex(run[s + 64: s + 96])))
    finally:
        _k32.CloseHandle(h)
    return out


def scan_key_candidates() -> Set[Tuple[bytes, Optional[bytes]]]:
    """一次内存扫描，返回所有候选密钥材料 {(32B key, 显式 salt 或 None)}。"""
    if not supported():
        raise RuntimeError("当前环境无法读取微信进程内存（需要 64 位 Windows Python）")
    seen: Set[bytes] = set()
    out: Set[Tuple[bytes, Optional[bytes]]] = set()
    pids = find_weixin_pids()
    if not pids:
        raise RuntimeError("未检测到 Weixin.exe 进程")
    for pid in pids:
        try:
            out |= _scan_candidates_pid(pid, seen)
        except OSError as exc:
            log.debug("扫描 PID %s 内存失败：%s", pid, exc)
            continue
    return out


def read_page1(path: str | os.PathLike) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            page1 = f.read(PAGE_SZ)
    except OSError:
        return None
    return page1 if len(page1) >= PAGE_SZ else None


def keys_from_candidates(
    cands: Iterable[Tuple[bytes, Optional[bytes]]],
    db_files: Iterable[Tuple[str, str | os.PathLike, int]],
) -> Dict[str, bytes]:
    """把候选材料对每个库做页 1 HMAC 校验，返回通过的密钥。

    存储形态按库的真实布局决定：**先按标准形式（文件头 salt）校验**，通过就存
    32 字节裸密钥；只有标准形式验不过时（明文头库）才用候选自带的显式 salt，
    并存成 48 字节 key+salt。顺序反了会把标准库存成 48 字节 → 解密走错分支
    （实测产出 "file is not a database" 的文件）。
    """
    files = list(db_files)
    page1s: Dict[str, bytes] = {}
    for rel, path, _ in files:
        page1 = read_page1(path)
        if page1 is not None:
            page1s[rel] = page1
    keys: Dict[str, bytes] = {}
    for cand, salt in cands:
        for rel, _path, _size in files:
            if rel in keys:
                continue
            page1 = page1s.get(rel)
            if page1 is None:
                continue
            if verify_enc_key(cand, page1):
                keys[rel] = cand                   # 标准库：32 字节裸 key
                continue
            if salt and verify_enc_key(cand, page1, salt=salt):
                keys[rel] = cand + salt            # 明文头库：48 字节 key+salt
    return keys


def key_works(key: bytes, path: str | os.PathLike) -> bool:
    """该密钥能否通过某库的页 1 校验（文件不存在/读不到一律 False）。"""
    if not key:
        return False
    page1 = read_page1(path)
    if page1 is None:
        return False
    return verify_enc_key(key, page1)


def derive_keys_from_master(
    master_hex: str,
    db_files: Iterable[Tuple[str, str | os.PathLike, int]],
) -> Dict[str, bytes]:
    """主密钥派生逐库密钥：``PBKDF2-HMAC-SHA512(master, 库头 salt, 256000)``。

    微信 4.x 是「单一主密钥 + 每库随机盐」，只有通过页 1 校验的才返回。
    （4.1.13+ 的 cfg 主密钥提取已不可信，本函数只作为显式提供主密钥时的兜底。）
    """
    master = bytes.fromhex(master_hex)
    if len(master) != 32:
        raise ValueError("主密钥必须为 32 字节（64 位 hex）")
    keys: Dict[str, bytes] = {}
    for rel, path, _ in db_files:
        page1 = read_page1(path)
        if page1 is None:
            continue
        derived = pbkdf2(master, page1[:16], KDF_ITER)
        if verify_enc_key(derived, page1):
            keys[rel] = derived
    return keys


# ---------------------------------------------------------------------------
# 数据目录 / 账号定位
# ---------------------------------------------------------------------------
def _abs_root(path: str) -> str:
    """目录规范化：保留盘符根语义（``d:\\`` 不能变成 ``d:`` 盘符相对路径）。"""
    p = os.path.normpath(path or "")
    if re.match(r"^[A-Za-z]:$", p):
        p = p + os.sep
    return p


def _extract_path_from_config(content: str) -> Optional[str]:
    """从微信配置内容里提取数据目录（兼容 JSON 字段 / 纯路径 / 任意文本）。"""
    content = (content or "").strip().lstrip("\ufeff")
    if not content:
        return None
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            for key in ("dataDir", "data_dir", "fileSavePath", "savePath",
                        "path", "defaultFileSavePath"):
                v = obj.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, str) and re.match(r"^[A-Za-z]:[\\/]", item):
                    return item
    except ValueError:
        pass
    if re.match(r"^[A-Za-z]:[\\/]", content):
        return content
    m = re.search(r"[A-Za-z]:[\\/][^\s\x00-\x1f\"']+", content)
    if m:
        return _abs_root(m.group(0))
    return None


def _config_candidates() -> List[str]:
    """可能的微信 4.x 配置目录（按新旧版本与 32/64 位安装差异）。"""
    out: List[str] = []
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env, "")
        if base:
            out.extend([
                os.path.join(base, "Tencent", "xwechat"),
                os.path.join(base, "Tencent", "xwechat", "config"),
                os.path.join(base, "Tencent", "WeChat"),
            ])
    return out


def _registry_data_dirs() -> List[str]:
    """从注册表读可能指向数据目录的值（用户自定义保存位置时补充来源）。"""
    import winreg

    dirs: List[str] = []
    for hive, sub in (
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\xwechat"),
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\xwechat\config"),
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat"),
    ):
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    name, data, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if not isinstance(data, str) or not data.strip():
                    continue
                low = name.lower()
                if "path" in low or "dir" in low or "save" in low:
                    dirs.append(data.strip())
        finally:
            winreg.CloseKey(key)
    return dirs


def _locate_account_root(root: Optional[str]) -> Optional[str]:
    """在候选根目录下定位「包含账号目录」的目录（账号目录以有 db_storage 为准）。

    兼容 ``<root>/xwechat_files/<account>/db_storage`` 与
    ``<root>/<account>/db_storage`` 两种布局。
    """
    if not root or not os.path.isdir(root):
        return None
    root = _abs_root(root)
    candidates = [root]
    for name in ("xwechat_files", "WeChat Files", "xwechat_files_data"):
        candidates.append(os.path.join(root, name))
    seen = set()
    for cand in candidates:
        cand = _abs_root(cand)
        if cand in seen or not os.path.isdir(cand):
            continue
        seen.add(cand)
        try:
            dirs = os.listdir(cand)
        except OSError:
            continue
        if any(os.path.isdir(os.path.join(cand, d, "db_storage")) for d in dirs):
            return cand
    return None


def auto_detect_db_dir() -> Optional[str]:
    """自动定位微信 4.x 数据目录（账号目录的父目录）；找不到返回 None。

    探测顺序：配置文件 → 注册表 → 常见默认目录（``%USERPROFILE%`` 下）。
    不硬编码用户名，全部通过环境变量 / ``Path.home()`` 推导。
    """
    # 1) 配置文件
    for cfg_dir in _config_candidates():
        if not os.path.isdir(cfg_dir):
            continue
        for fp in glob.glob(os.path.join(cfg_dir, "*")):
            if os.path.isdir(fp):
                continue
            raw = None
            for enc in ("utf-8", "gbk"):
                try:
                    raw = open(fp, "r", encoding=enc).read(8192)
                    break
                except (UnicodeDecodeError, OSError):
                    continue
            if not raw:
                continue
            path = _extract_path_from_config(raw)
            if not path:
                continue
            hit = _locate_account_root(path)
            if hit:
                return hit
    # 2) 注册表
    for p in _registry_data_dirs():
        hit = _locate_account_root(p)
        if hit:
            return hit
    # 3) 常见默认目录兜底（Documents 优先）
    home = Path.home()
    for base in (home / "Documents", home):
        hit = _locate_account_root(str(base))
        if hit:
            return hit
    return None


def _to_posix_rel(rel: str) -> str:
    return str(rel).replace("\\", "/").strip("/")


def find_account_dirs(db_dir: str | os.PathLike) -> List[Path]:
    """列出 db_dir 下所有含 ``db_storage`` 子目录的账号目录（按名称排序）。"""
    out: List[Path] = []
    try:
        base = Path(db_dir)
        for child in base.iterdir():
            if child.is_dir() and (child / "db_storage").is_dir():
                out.append(child)
    except OSError:
        return []
    return sorted(out, key=lambda p: p.name)


def pick_account(db_dir: str | os.PathLike) -> str:
    """按「db_storage 下 .db 最近修改时间」挑一个账号目录名（启发式初选）。

    多账号机器上这个启发式可能选错，``wx4.db`` 会在取到密钥后用密码学校验
    （页 1 HMAC）重新给各账号打分并切换，不靠它兜底。
    """
    candidates: List[Tuple[float, str]] = []
    for d in find_account_dirs(db_dir):
        recent = 0.0
        try:
            for root, _dirs, files in os.walk(d / "db_storage"):
                for f in files:
                    if f.endswith(".db") and not f.endswith("-wal"):
                        try:
                            recent = max(recent, os.path.getmtime(os.path.join(root, f)))
                        except OSError:
                            continue
        except OSError:
            continue
        candidates.append((recent, d.name))
    if not candidates:
        raise RuntimeError("未找到任何已登录账号的数据库目录")
    candidates.sort(reverse=True)
    return candidates[0][1]


def collect_db_files(account_dir: str | os.PathLike) -> List[Tuple[str, Path, int]]:
    """收集账号目录下所有加密的 ``.db``（相对路径用正斜杠，``migrate`` 目录排除）。"""
    files: List[Tuple[str, Path, int]] = []
    base = Path(account_dir) / "db_storage"
    try:
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            name = path.name
            if not name.endswith(".db") or name.endswith("-wal") or name.endswith("-shm"):
                continue
            rel = path.relative_to(base)
            if rel.parts and rel.parts[0].lower() == "migrate":
                # migrate 下的 unspportmsg.db 是微信保留库，进程内存里没有对应密钥
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files.append((_to_posix_rel(str(rel)), path, size))
    except OSError:
        return []
    files.sort(key=lambda x: x[0])
    return files


def wxid_from_account(account: str) -> str:
    """账号目录名 → wxid（去掉末尾 4 位十六进制哈希后缀）。

    4.x 账号目录名形如 ``wxid_xxxx_5663``；优先按 4 位 hex 后缀剥，若不是
    hex（历史/自定义目录名）再退回「4 个单词字符」的宽松规则。
    """
    name = account or ""
    stripped = re.sub(r"_[0-9a-fA-F]{4}$", "", name)
    if stripped != name:
        return stripped
    return re.sub(r"_\w{4}$", "", name)


def wal_path_of(path: str | os.PathLike) -> Optional[Path]:
    wal = Path(str(path) + "-wal")
    return wal if wal.is_file() else None


__all__ = [
    "PAGE_SZ", "RESERVE_SZ", "KDF_ITER", "WAL_HEADER_SZ", "WAL_FRAME_SZ",
    "md5_hex", "pbkdf2", "aes_cbc_decrypt", "split_key", "verify_enc_key",
    "decrypt_page", "decrypt_file", "check_sqlite_ok", "make_rollback_mode",
    "sqlite_uri", "merge_wal",
    "sqlite_text_factory", "supported", "find_weixin_pids", "scan_key_candidates",
    "keys_from_candidates", "key_works", "derive_keys_from_master", "read_page1",
    "auto_detect_db_dir", "find_account_dirs", "pick_account",
    "collect_db_files", "wxid_from_account", "wal_path_of",
]
