# -*- coding: utf-8 -*-
"""图片解密密钥：从微信进程内存**只读**提取，自验证后缓存到项目内。

## 原理（社区公开方案：``Config.Cipher`` 结构）

``Weixin.dll`` 映像里找到 ``global_config`` SSO 地标 → 指针链得到 cfg 对象::

    v18   = *(landmark - backoff)
    cfg   = *(v18 + cfg_off)
    cfgDword  = u32 @ cfg + 0x40       # 图片密钥派生源
    wxId      = std::string @ cfg + 0x48
    imageXorKey = cfgDword & 0xFF                  # 十进制字节值，如 82(0x52)
    imageAesKey = md5(str(cfgDword) + wxId)[:16]   # 16 个 ASCII hex 字符
    dbKey       = std::string 密文 @ cfg + 0x2B8   # 数据库主密钥（wx4.db 用）

不同微信小版本会挪动 ``backoff/cfg_off`` 偏移，所以这里**用"能否解密真实
``.dat``"自动验证**：枚举一批候选偏移组合，用候选 AES 密钥解最近缓存 ``.dat``
的首个密文块、校验图片魔数，命中即锁定，因此不依赖版本硬编码。

## 缓存与回退

* 成功结果缓存到 ``WechatMoments/.wx4_cache/image_key.json``（含账号、来源、
  时间戳），下次启动 0.1s 内可用，不必再碰微信进程。
* 缓存值会**再做一次 .dat 自验证**，密钥轮换/换账号后自动重新提取。
* 内存提取失败（微信未启动/无权限）时回退到已知值
  ``aes=0669b78f42c9a7d7 xor=82 wxid=wxid_hsqe9w1lhl1j22``（来自 wx_box
  对该机实测并长期缓存的 config.json），并标记 ``source="known"``。

## 红线

只读 ``OpenProcess/ReadProcessMemory``；**不写内存、不注入、不 Hook、
不修改微信任何文件**，只向本项目 ``.wx4_cache/`` 写缓存。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import struct
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

try:
    from . import Wx4Error
except ImportError:  # pragma: no cover - 独立脚本导入兜底

    class Wx4Error(RuntimeError):  # type: ignore[no-redef]
        pass


#: 已知可用值（wx_box 在该机 微信 4.1.15.56 实测并长期缓存，仅作最后兜底）
KNOWN_AES_KEY = "0669b78f42c9a7d7"
KNOWN_XOR_KEY = 82
KNOWN_WXID = "wxid_hsqe9w1lhl1j22"

_V2_MAGIC = b"\x07\x08V2\x08\x07"
_MAGICS = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF", b"wxgf")
_LANDMARK = b"global_config"

_CACHE_VERSION = 1
_lock = threading.RLock()
_memo: dict | None = None

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]


class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
                ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wintypes.DWORD), ("hModule", ctypes.c_void_p),
                ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260)]


class KeyExtractError(Wx4Error):
    """图片密钥提取失败（微信未启动 / 无权限 / 版本漂移）。"""


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / ".wx4_cache"
KEY_CACHE_FILE = CACHE_DIR / "image_key.json"


def cache_dir() -> Path:
    """项目内缓存目录（``WechatMoments/.wx4_cache``），按需创建。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


def default_data_root() -> Path:
    """微信 4.x 数据根目录（``~/Documents/xwechat_files``），可用环境变量覆盖。"""
    import os

    env = os.environ.get("WX4_DATA_ROOT") or os.environ.get("WECHATMOMENTS_WX4_ROOT")
    if env:
        return Path(env)
    return Path.home() / "Documents" / "xwechat_files"


def candidate_account_dirs(data_root: Path | None = None) -> list[Path]:
    """列出 ``xwechat_files`` 下所有账号目录（含 ``db_storage`` 或 ``cache`` 的）。"""
    root = Path(data_root) if data_root else default_data_root()
    out: list[Path] = []
    try:
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            if (d / "db_storage").is_dir() or (d / "cache").is_dir():
                out.append(d)
    except OSError:
        pass
    return out


def pick_account_dir(data_root: Path | None = None) -> Path | None:
    """当前活跃账号目录：优先 ``wx4.db`` 认定的账号，其次缓存目录最新的那个。"""
    try:
        from .db import get_db

        d = Path(get_db().account_dir)
        if d.is_dir():
            return d
    except Exception:
        pass
    best: tuple[float, Path] | None = None
    for acc in candidate_account_dirs(data_root):
        latest = 0.0
        try:
            for pat in ("cache/*/Sns/Img/*/*", "msg/attach/*/*/*/*"):
                for f in acc.glob(pat):
                    try:
                        latest = max(latest, f.stat().st_mtime)
                    except OSError:
                        continue
        except OSError:
            continue
        if best is None or latest > best[0]:
            best = (latest, acc)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# .dat 样本收集（用于自验证）
# ---------------------------------------------------------------------------


def dat_probes(account_dir: Path | None = None, limit: int = 3, max_age: float = 0.0) -> list[bytes]:
    """收集最近图片缓存 ``.dat`` 的首个 AES 密文块（16 字节），用于验证密钥。

    覆盖 Sns（朋友圈）与聊天图片两类缓存；``max_age>0`` 时只用该时间窗内的文件。
    """
    acc = Path(account_dir) if account_dir else pick_account_dir()
    if acc is None:
        return []
    now = time.time()
    files: list[Path] = []
    for pat in ("cache/*/Sns/Img/*/*", "msg/attach/*/*/Img/*.dat", "cache/*/Emoticon/*/*"):
        try:
            files.extend(p for p in acc.glob(pat) if p.is_file())
        except OSError:
            continue
    files.sort(key=lambda p: _safe_mtime(p), reverse=True)
    out: list[bytes] = []
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        if max_age > 0 and now - st.st_mtime > max_age:
            continue
        if st.st_size < 31:
            continue
        try:
            head = f.open("rb").read(31)
        except OSError:
            continue
        if head[:6] == _V2_MAGIC and len(head) == 31:
            out.append(head[15:31])
        if len(out) >= limit:
            break
    return out


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# 密钥校验 / 提取
# ---------------------------------------------------------------------------


def _aes_ok(aes_key: str, probes: list[bytes]) -> bool:
    """候选 AES 密钥能否把全部 probe 的首块解成图片魔数。"""
    if not aes_key or len(aes_key) != 16:
        return False
    try:
        key = aes_key.encode("ascii", "ignore")
        if len(key) != 16:
            return False
        from .dat import _aes_ecb_crypt  # 复用后端选择（cryptography / Cryptodome）
    except Exception:
        return False
    try:
        for probe in probes:
            if len(probe) != 16:
                return False
            plain = _aes_ecb_crypt(key, probe, encrypt=False)
            if not any(plain.startswith(m) for m in _MAGICS):
                return False
        return True
    except Exception:
        return False


def weixin_pids() -> list[int]:
    """微信进程 PID（按内存占用降序，主进程在前）。"""
    try:
        import psutil
    except Exception:
        return []
    got = []
    for pr in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            if (pr.info["name"] or "").lower() == "weixin.exe":
                mem = pr.info["memory_info"]
                got.append((pr.info["pid"], mem.rss if mem else 0))
        except Exception:
            continue
    got.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in got]


def _find_module(pid: int, name: str = "weixin.dll"):
    """返回 (模块基址, 模块大小, 模块路径)。"""
    snap = kernel32.CreateToolhelp32Snapshot(0x08 | 0x10, pid)
    if not snap or snap == -1:
        return None
    try:
        me = _MODULEENTRY32W()
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


def _read_remote_string(read, addr: int) -> str:
    """读 MSVC ``std::string``（SSO：size<=15 内联，否则堆指针）。"""
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


def locate_cfg(pid: int, probes: list[bytes], timeout: float = 60.0) -> dict:
    """在指定微信进程里定位 cfg，返回 ``{cfg_dword, wxid, aes_key, xor_key, meta}``。

    纯只读内存操作；找不到抛 :class:`KeyExtractError`。
    """
    mod = _find_module(pid)
    if not mod:
        raise KeyExtractError("没找到 weixin.dll 模块（微信 4.x 主模块）")
    base, size, _path = mod
    h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
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
            raise KeyExtractError("读取 weixin.dll 映像失败（权限不足？）")
        positions = []
        start = 0
        while True:
            i = image.find(_LANDMARK, start)
            if i < 0:
                break
            positions.append(base + i)
            start = i + 1
        if not positions:
            raise KeyExtractError("没找到 global_config 地标（微信版本可能更新，锚点需重采）")
        for lm in positions:
            for back in range(0x80, 0x1C1, 8):
                if time.time() > deadline:
                    raise KeyExtractError(f"扫描超时（{timeout:.0f}s），微信版本可能已变更")
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
                                return {
                                    "cfg_dword": dw,
                                    "wxid": wxid,
                                    "aes_key": aes,
                                    "xor_key": dw & 0xFF,
                                    "meta": {"back": back, "cfg_off": cfg_off,
                                             "wx_off": woff, "dw_off": dwoff,
                                             "module": hex(base), "pid": pid},
                                }
        raise KeyExtractError("扫描完成但没有找到能解密 .dat 的 cfg 组合")
    finally:
        kernel32.CloseHandle(h)


def extract_image_keys(account_dir: Path | None = None) -> dict | None:
    """从微信进程内存提取图片密钥，失败返回 ``None``。

    返回 ``{"aes_key": str, "xor_key": int, "wxid": str, "source": "memory", ...}``。
    ``xor_key`` 是**十进制字节值**（如 82）。
    """
    probes = dat_probes(account_dir)
    if not probes:
        # 没有样本就无法自验证；内存扫描整体跳过（避免拿到错密钥）
        return None
    last_err = ""
    for pid in weixin_pids():
        try:
            got = locate_cfg(pid, probes)
        except KeyExtractError as exc:
            last_err = str(exc)
            continue
        got["source"] = "memory"
        return got
    if last_err:
        print(f"[wx4.image_key] 内存提取失败：{last_err}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# 缓存读写
# ---------------------------------------------------------------------------


def load_cached_keys(account_dir: Path | None = None) -> dict | None:
    """读 ``.wx4_cache/image_key.json``；内容不完整/账号不符返回 ``None``。"""
    try:
        raw = KEY_CACHE_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
        return None
    aes = str(data.get("aes_key") or "").strip().lower()
    if len(aes) != 16:
        return None
    try:
        xor = int(str(data.get("xor_key")).strip(), 0)
    except (TypeError, ValueError):
        xor = KNOWN_XOR_KEY
    acc = str(data.get("account") or "")
    if account_dir is not None and acc and Path(acc).name != Path(account_dir).name:
        return None
    return {
        "aes_key": aes,
        "xor_key": xor & 0xFF,
        "wxid": str(data.get("wxid") or ""),
        "source": "cache",
        "account": acc,
        "saved_at": data.get("saved_at"),
    }


def save_cached_keys(keys: dict, account_dir: Path | None = None) -> None:
    """把密钥写入项目缓存（原子写：先写 .tmp 再 replace）。"""
    acc = Path(account_dir) if account_dir else pick_account_dir()
    payload = {
        "version": _CACHE_VERSION,
        "aes_key": str(keys.get("aes_key") or ""),
        "xor_key": int(keys.get("xor_key", KNOWN_XOR_KEY)) & 0xFF,
        "wxid": str(keys.get("wxid") or ""),
        "account": str(acc) if acc else "",
        "source": str(keys.get("source") or ""),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        cache_dir()
        tmp = KEY_CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(KEY_CACHE_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def get_image_keys(force: bool = False, account_dir: Path | str | None = None) -> dict:
    """取得图片解密密钥（进程内单例）。

    顺序：进程内记忆 → ``.wx4_cache/image_key.json`` → 微信进程内存提取 →
    已知值兜底。返回 ``{"aes_key","xor_key","wxid","source"}``；
    ``xor_key`` 为十进制字节值。全都拿不到时抛 :class:`KeyExtractError`。
    """
    global _memo
    acc = Path(account_dir) if account_dir else None
    with _lock:
        if _memo is not None and not force:
            return dict(_memo)

        probes = dat_probes(acc)
        candidates: list[dict] = []

        cached = load_cached_keys(acc)
        if cached:
            candidates.append(cached)
        if not candidates or force:
            mem = extract_image_keys(acc)
            if mem:
                candidates.append(mem)
        # 已知值兜底
        candidates.append({"aes_key": KNOWN_AES_KEY, "xor_key": KNOWN_XOR_KEY,
                           "wxid": KNOWN_WXID, "source": "known"})

        for cand in candidates:
            aes = str(cand.get("aes_key") or "").strip().lower()
            xor = int(cand.get("xor_key", KNOWN_XOR_KEY)) & 0xFF
            if probes and not _aes_ok(aes, probes):
                continue
            result = {"aes_key": aes, "xor_key": xor,
                      "wxid": str(cand.get("wxid") or ""),
                      "source": str(cand.get("source") or "")}
            _memo = result
            if result["source"] in ("memory",):
                save_cached_keys(result, acc)
            elif result["source"] == "known":
                # 已知值也落盘，便于下次直接命中
                save_cached_keys(result, acc)
            return dict(result)

        raise KeyExtractError(
            "取不到能解密缓存图片的密钥：微信可能未启动/未登录，或版本变更需重采锚点"
        )


def reset() -> None:
    """清空进程内记忆（换账号/测试用）。"""
    global _memo
    with _lock:
        _memo = None


__all__ = [
    "KeyExtractError",
    "KNOWN_AES_KEY",
    "KNOWN_XOR_KEY",
    "KNOWN_WXID",
    "cache_dir",
    "candidate_account_dirs",
    "dat_probes",
    "default_data_root",
    "extract_image_keys",
    "get_image_keys",
    "load_cached_keys",
    "locate_cfg",
    "pick_account_dir",
    "reset",
    "save_cached_keys",
    "weixin_pids",
]
