"""微信 4.x 本地数据库（WCDB/SQLCipher）只读直读：密钥提取 + 解密 + 消息读取。

用户已明确授权（"可以忽略之前的限制"）。本模块只读内存/本地文件，不写微信任何数据。

能力：
* Config.Cipher 内存扫描取 WCDB 数据库密钥（社区公开算法，逐库 HMAC 强校验，缓存到 config）
* SQLCipher4 页解密（AES-256-CBC + HMAC-SHA512，page=4096, reserve=80）
* 应用 WAL 增量（拿到最新未落盘消息）
* 按会话读消息：文字（精确原文，含 [微笑]）、图片/文件/表情包/语音的元数据

有了它，消息文本/图片/表情/文件/语音时长都能和消息**精确对应**，不再依赖 OCR 与视觉猜测。
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac as hmac_mod
import re
import sqlite3
import struct
import tempfile
import threading
import time
from pathlib import Path

from Crypto.Cipher import AES

PAGE_SZ = 4096
RESERVE_SZ = 80
CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
HEX_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.VirtualQueryEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
_k32.VirtualQueryEx.restype = ctypes.c_size_t

_lock = threading.RLock()


class _MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", ctypes.c_ulong), ("RegionSize", ctypes.c_size_t),
                ("State", ctypes.c_ulong), ("Protect", ctypes.c_ulong), ("Type", ctypes.c_ulong)]


def _weixin_pids() -> list[int]:
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


def _regions(pid: int) -> list[tuple[int, int]]:
    h = _k32.OpenProcess(0x0400, False, pid)
    if not h:
        return []
    out: list[tuple[int, int]] = []
    try:
        addr = 0
        mbi = _MBI()
        while True:
            got = _k32.VirtualQueryEx(ctypes.c_void_p(h), ctypes.c_void_p(addr),
                                      ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not got:
                break
            base, size = int(mbi.BaseAddress or 0), int(mbi.RegionSize or 0)
            if mbi.State == 0x1000 and size and not (mbi.Protect & 0x100) and not (mbi.Protect & 0x01):
                out.append((base, size))
            addr = base + size
    finally:
        _k32.CloseHandle(h)
    return out


def _read_fn(pid: int):
    h = _k32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not h:
        return None, None

    def read(addr: int, n: int):
        buf = ctypes.create_string_buffer(n)
        br = ctypes.c_size_t(0)
        if _k32.ReadProcessMemory(ctypes.c_void_p(h), ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
            return buf.raw[: br.value]
        return None

    return h, read


def _find_bytes(read, needle: bytes, regions, max_hits: int = 64) -> list[int]:
    hits: list[int] = []
    for base, size in regions:
        step = 32 * 1024 * 1024
        for off in range(0, size, step):
            chunk = read(base + off, min(step, size - off))
            if not chunk:
                continue
            i = chunk.find(needle)
            while i >= 0:
                hits.append(base + off + i)
                if len(hits) >= max_hits:
                    return hits
                i = chunk.find(needle, i + 1)
    return hits


def _plausible(v: int) -> bool:
    return 0x10000 <= v < 0x800000000000


def _read_mem(read, addr: int, n: int):
    b = read(addr, n)
    return b if b and len(b) == n else None


def _blob_keys(blob: bytes) -> set[bytes]:
    out: set[bytes] = set()
    masks = (CONFIG_XOR_MASK, bytes(reversed(CONFIG_XOR_MASK)), b"")
    for mask in masks:
        decoded = bytes(v ^ mask[i % len(mask)] for i, v in enumerate(blob)) if mask else blob
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


def _config_candidates(read, regions) -> set[bytes]:
    needles = _find_bytes(read, CONFIG_CIPHER_NAME, regions)
    if not needles:
        return set()
    pairs = [struct.pack("<Q", a) + struct.pack("<Q", len(CONFIG_CIPHER_NAME)) for a in needles]
    cands: set[bytes] = set()
    for pair in pairs:
        for qaddr in _find_bytes(read, pair, regions):
            cfg_ptrs = []
            node = _read_mem(read, qaddr - 0x10, 0x50)
            if node and struct.unpack_from("<Q", node, 0x28)[0]:
                v = struct.unpack_from("<Q", node, 0x28)[0]
                if _plausible(v):
                    cfg_ptrs.append(v)
            for delta in range(-0x40, 0x60, 8):
                raw = _read_mem(read, qaddr + delta, 0x40)
                if not raw:
                    continue
                for off in range(0, 0x38, 8):
                    v = struct.unpack_from("<Q", raw, off)[0]
                    if _plausible(v):
                        cfg_ptrs.append(v)
            for cfg in dict.fromkeys(cfg_ptrs):
                for objoff in range(0x40, 0x200, 8):
                    obj = _read_mem(read, cfg + objoff, 0x20)
                    if not obj:
                        continue
                    for poff, loff in ((0x0, 0x8), (0x8, 0x10), (0x0, 0x10)):
                        dptr = struct.unpack_from("<Q", obj, poff)[0]
                        dlen = struct.unpack_from("<Q", obj, loff)[0]
                        if not (0 < dlen <= 1024 and _plausible(dptr)):
                            continue
                        blob = _read_mem(read, dptr, int(dlen))
                        if blob:
                            cands |= _blob_keys(blob)
    return cands


def scan_keys(verbose: bool = False) -> set[bytes]:
    """扫描当前登录微信进程，返回候选密钥材料集合（32B 或 32B+16B）。"""
    pids = _weixin_pids()
    if not pids:
        return set()
    out: set[bytes] = set()
    for pid in pids[:1]:
        h, read = _read_fn(pid)
        if not h:
            continue
        try:
            regions = _regions(pid)
            out |= _config_candidates(read, regions)
        finally:
            _k32.CloseHandle(h)
    if verbose:
        print(f"[wcdb] 候选密钥材料 {len(out)} 个")
    return out


# ---------------- 页解密 / WAL ----------------
def _verify(page1: bytes, key32: bytes, salt: bytes, plaintext: bool) -> bool:
    try:
        mm = hmac_mod.new(hashlib.pbkdf2_hmac("sha512", key32,
                                              bytes(b ^ 0x3A for b in salt), 2, 32),
                          page1[16: PAGE_SZ - RESERVE_SZ + 16], hashlib.sha512)
    except Exception:
        return False
    mm.update(struct.pack("<I", 1))
    return mm.digest() == page1[PAGE_SZ - 64: PAGE_SZ]


def match_db(page1: bytes, cands: set[bytes]):
    """返回 (key32, salt, 明文头布局) 或 None。"""
    for cand in cands:
        if len(cand) == 32:
            if _verify(page1, cand, page1[:16], False):
                return cand, page1[:16], False
        elif len(cand) == 48:
            k32, s16 = cand[:32], cand[32:]
            # 先按标准布局校验（salt 在文件头），再试明文头布局；
            # 两者都能过 HMAC 时以"解出来是不是 SQLite 头"为准，避免把 salt 当页头。
            if _verify(page1, k32, page1[:16], False):
                return k32, page1[:16], False
            if _verify(page1, k32, s16, True):
                return k32, s16, True
    return None


def derive_map(account_dir: Path, cands: set[bytes], progress=None) -> dict:
    """为账号目录下所有 .db 匹配密钥，返回 {相对路径: {key,salt,layout}}。"""
    out: dict[str, dict] = {}
    for db in sorted((account_dir / "db_storage").glob("*/*.db")):
        if db.name.endswith(("-wal", "-shm")):
            continue
        try:
            page1 = db.read_bytes()[:PAGE_SZ]
        except OSError:
            continue
        if len(page1) < PAGE_SZ:
            continue
        hit = match_db(page1, cands)
        if hit is None:
            continue
        k32, salt, plaintext = hit
        rel = str(db.relative_to(account_dir)).replace("\\", "/")
        out[rel] = {"key": k32.hex(), "salt": salt.hex(), "layout": "plaintext" if plaintext else "standard"}
        if progress:
            progress(rel)
    return out


def _decrypt_page(page: bytes, key: bytes, first: bool, plaintext: bool) -> bytes:
    iv = page[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
    enc = (page[16:] if first else page)[: PAGE_SZ - RESERVE_SZ - (16 if first else 0)]
    dec = AES.new(key, AES.MODE_CBC, iv).decrypt(enc)
    head = (page[:16] if plaintext else b"SQLite format 3\x00") if first else b""
    return head + dec + b"\x00" * RESERVE_SZ


def _decrypt_db_with_layout(path: Path, key: bytes, plaintext: bool) -> bytes:
    raw = path.read_bytes()
    n = len(raw) // PAGE_SZ
    out = bytearray()
    for pg in range(n):
        out += _decrypt_page(raw[pg * PAGE_SZ:(pg + 1) * PAGE_SZ], key, pg == 0, plaintext)
    return bytes(out)


def decrypt_db(path: Path, key: bytes, plaintext: bool) -> bytes:
    data = _decrypt_db_with_layout(path, key, plaintext)
    if data.startswith(b"SQLite format 3"):
        return data
    alt = _decrypt_db_with_layout(path, key, not plaintext)
    return alt if alt.startswith(b"SQLite format 3") else data


def load_db_bytes(path: Path, key: bytes, plaintext: bool) -> bytes:
    """解密主库并叠加 WAL 里已提交的帧（拿最新消息）。"""
    data = decrypt_db(path, key, plaintext)
    wal = path.with_name(path.name + "-wal")
    try:
        if not wal.exists():
            return data
        raw = wal.read_bytes()
    except OSError:
        return data
    if len(raw) < 32:
        return data
    try:
        magic, _ver, page_sz, _ckpt, salt1, salt2, _c1, _c2 = struct.unpack("<IIIIIIII", raw[:32])
    except struct.error:
        return data
    if page_sz != PAGE_SZ:
        return data
    out = bytearray(data)
    off = 32
    while off + 24 + PAGE_SZ <= len(raw):
        try:
            fno, dbsize, s1, s2, _x1, _x2 = struct.unpack("<IIIIII", raw[off:off + 24])
        except struct.error:
            break
        if s1 != salt1 or s2 != salt2:
            break
        page = raw[off + 24: off + 24 + PAGE_SZ]
        plain = _decrypt_page(page, key, fno == 1, plaintext)
        end = fno * PAGE_SZ
        if end > len(out):
            out += b"\x00" * (end - len(out))
        out[end - PAGE_SZ:end] = plain
        off += 24 + PAGE_SZ
    return bytes(out)


# ---------------- 会话消息读取 ----------------
def _sqlite_ok(path: Path) -> bool:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            con.execute("select count(*) from sqlite_master").fetchone()
        finally:
            con.close()
        return True
    except Exception:
        return False


def cleanup_stale_temp(max_age_hours: float = 24.0) -> int:
    """清理崩溃/被强杀留下的旧 wxdb_* 临时解密目录（超过 max_age_hours）。"""
    removed = 0
    try:
        import shutil
        root = Path(tempfile.gettempdir())
        cutoff = time.time() - max_age_hours * 3600
        for d in root.glob("wxdb_*"):
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    except Exception:
        pass
    return removed


class SessionReader:
    """解密指定账号的 message_0.db / contact.db，按会话读消息。"""

    def __init__(self, account_dir: Path, keys: dict, self_wxid: str):
        self.account_dir = Path(account_dir)
        self.keys = keys
        self.self_wxid = self_wxid
        cleanup_stale_temp()
        self._tmpdir = Path(tempfile.mkdtemp(prefix="wxdb_"))
        self._msg_path: Path | None = None
        self._contact_path: Path | None = None
        self._names: dict[int, str] = {}
        self._lock = threading.RLock()
        self._msg_stamp = (0.0, 0)
        self._msg_db: Path | None = None

    def _ensure_tmpdir(self) -> Path:
        """临时目录被系统/清理工具删掉时自动重建，避免轮询一直报 FileNotFoundError。"""
        try:
            if not self._tmpdir.is_dir():
                self._tmpdir = Path(tempfile.mkdtemp(prefix="wxdb_"))
        except Exception:
            self._tmpdir = Path(tempfile.mkdtemp(prefix="wxdb_"))
        return self._tmpdir

    def close(self) -> None:
        """清理本机临时解密副本（退出/停止监听时调用）。"""
        import shutil
        try:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        except Exception:
            pass

    # --- 内部 ---
    def _key_for(self, rel: str):
        info = self.keys.get(rel)
        if not info:
            return None
        return bytes.fromhex(info["key"]), info.get("layout") == "plaintext"

    def _materialize(self, rel: str, cache_attr: str, force: bool = False) -> Path | None:
        key = self._key_for(rel)
        src = self.account_dir / rel
        if key is None or not src.exists():
            return None
        key32, plaintext = key
        wal = src.with_name(src.name + "-wal")
        try:
            stamp = (src.stat().st_mtime, wal.stat().st_mtime if wal.exists() else 0.0)
        except OSError:
            stamp = (0.0, 0.0)
        with self._lock:
            cached = getattr(self, cache_attr + "_stamp", None)
            cur = getattr(self, cache_attr, None)
            if not force and cached == stamp and cur and Path(cur).exists():
                return Path(cur)
            self._ensure_tmpdir()
            out = self._tmpdir / Path(rel).name
            # 先尝试"主库+WAL"；若解密结果 sqlite 打不开（微信正在写 WAL 的竞态），
            # 退化为只解主库——数据稍旧但一定可用。
            for include_wal in (True, False):
                data = load_db_bytes(src, key32, plaintext) if include_wal else decrypt_db(src, key32, plaintext)
                try:
                    out.write_bytes(data)
                except OSError:
                    # 目录/文件被外部删掉了：重建临时目录再写
                    self._tmpdir = Path(tempfile.mkdtemp(prefix="wxdb_"))
                    out = self._tmpdir / Path(rel).name
                    out.write_bytes(data)
                if _sqlite_ok(out):
                    break
            setattr(self, cache_attr + "_stamp", stamp)
            setattr(self, cache_attr, out)
            return out

    def _load_names(self) -> dict[int, str]:
        """发件人映射：message 库里的 Name2Id（rowid -> user_name），不是 contact 库。"""
        if self._names:
            return self._names
        path = self._msgdb()
        names: dict[int, str] = {}
        if path:
            try:
                con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                for rowid, username in con.execute("select rowid, user_name from Name2Id"):
                    names[rowid] = username
                con.close()
            except Exception:
                pass
        self._names = names
        return names

    def resolve_username(self, display_name: str) -> str | None:
        for attempt in range(3):
            path = self._materialize("db_storage/contact/contact.db", "_contact_path",
                                     force=attempt > 0)
            if not path:
                return None
            try:
                con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                row = con.execute(
                    "select username from contact where nick_name=? or remark=? limit 1",
                    (display_name, display_name)).fetchone()
                con.close()
                if row:
                    return row[0]
            except Exception as exc:  # noqa: BLE001
                self._last_error = repr(exc)
            time.sleep(0.2)
        return None

    def display_name(self, username: str) -> str:
        path = self._materialize("db_storage/contact/contact.db", "_contact_path")
        if not path:
            return username
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            row = con.execute(
                "select coalesce(nullif(remark,''), nick_name) from contact where username=? limit 1",
                (username,)).fetchone()
            con.close()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
        return username

    def _msgdb(self) -> Path | None:
        return self._materialize("db_storage/message/message_0.db", "_msg_db")

    def _session_table(self, username: str) -> str:
        return "Msg_" + hashlib.md5(username.encode()).hexdigest()

    def read_messages(self, username: str, since_local_id: int = 0, limit: int = 50) -> list[dict]:
        """读取会话消息（local_id 递增）。返回 dict 列表，含原始 content 与元信息。"""
        names = self._load_names()
        table = self._session_table(username)
        rows = []
        for attempt in range(3):
            path = self._msgdb()
            if not path:
                return []
            try:
                con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    rows = con.execute(
                        f"select local_id, local_type, real_sender_id, create_time, message_content, WCDB_CT_message_content "
                        f"from {table} where local_id > ? order by local_id limit ?",
                        (since_local_id, limit)).fetchall()
                finally:
                    con.close()
                break
            except Exception:
                self._materialize("db_storage/message/message_0.db", "_msg_db", force=True)
                time.sleep(0.2)
        out: list[dict] = []
        for lid, ltype, sid, ctime, content, flag in rows:
            out.append({
                "local_id": int(lid),
                "local_type": int(ltype or 0),
                "sender": names.get(int(sid or 0), ""),
                "self": names.get(int(sid or 0), "") == self.self_wxid,
                "create_time": int(ctime or 0),
                "content": _decode_content(content, int(flag or 0)),
                "raw_flag": int(flag or 0),
            })
        return out


def _decode_content(content, flag: int):
    """按 WCDB 压缩标志/魔数解出 message_content。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    data = bytes(content)
    if not data:
        return ""
    if data[:4] == b"\x28\xb5\x2f\xfd" or flag & 4:
        try:
            from compression import zstd
            return zstd.decompress(data)
        except Exception:
            return data
    return data


def ensure_keys(account_dir: Path, config: dict, log=None) -> dict:
    """确保账号目录下数据库密钥可用：先验证缓存，再内存扫描。返回 key map。

    按账号目录分别缓存（db_keys_by_account），切换账号时无需重新扫内存。
    """
    by_acc: dict = dict(config.get("db_keys_by_account") or {})
    if not by_acc and config.get("db_keys"):
        by_acc[account_dir.name] = dict(config["db_keys"])
    cached: dict = dict(by_acc.get(account_dir.name) or {})
    valid: dict = {}
    for rel, info in cached.items():
        try:
            page1 = (account_dir / rel).read_bytes()[:PAGE_SZ]
            k32 = bytes.fromhex(info["key"])
            salt = bytes.fromhex(info.get("salt") or "") or page1[:16]
            if _verify(page1, k32, salt, info.get("layout") == "plaintext"):
                valid[rel] = info
        except Exception:
            continue
    missing = [rel for rel in ("db_storage/message/message_0.db",
                               "db_storage/contact/contact.db")
               if rel not in valid]
    if missing:
        if log:
            log("首次读取数据库密钥（一次性只读内存扫描，约 10-20 秒）...")
        cands = scan_keys()
        if cands:
            found = derive_map(account_dir, cands)
            for rel, info in found.items():
                valid.setdefault(rel, info)
            if log:
                log(f"数据库密钥已就绪：{len(found)} 个库")
    if valid:
        by_acc[account_dir.name] = valid
        config["db_keys_by_account"] = by_acc
        config["db_keys"] = valid
    return valid
