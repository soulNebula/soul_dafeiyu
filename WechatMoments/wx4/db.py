# -*- coding: utf-8 -*-
"""微信 4.x 数据库层（契约见 ``wx4/API.md`` 2.1）。

对外只暴露安全、可读的接口：

- :class:`Wx4DB`：定位当前登录账号 → 提取/缓存 4.x SQLCipher 密钥 →
  :meth:`Wx4DB.open` 返回解密（并合并 ``-wal``）后的只读 ``sqlite3.Connection``；
- :func:`get_db` / :func:`reset`：进程级单例（GUI 与后台线程共用）。

线程模型：``open()`` 返回的每个连接按「库 + 线程」缓存，GUI 主线程与导出线程
各拿各的连接，不会触发 ``sqlite3`` 的跨线程错误；解密产物每份按源库/WAL 快照
版本化命名，重建时不会踩到正在被读取的旧文件（Windows 不允覆盖已打开文件）。

失败（微信没启动/没登录/取不到密钥/库损坏）一律抛 :class:`wx4.Wx4Error`，
消息是可直接展示给用户的中文，不泄露 ctypes / 异常细节。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import Wx4Error, _refdb, keycache

log = logging.getLogger(__name__)

#: 联系人 dict 字段（契约固定）
CONTACT_FIELDS = ("username", "nick_name", "remark", "alias",
                  "head_img_md5", "local_type")

_STALE_TMP_SECONDS = 600      # 构建中间产物超过 10 分钟视为残留
_STALE_VERSION_SECONDS = 120  # 旧版本解密文件超过 2 分钟才清理（避开并发打开窗口）
_MAX_LIVE_VERSIONS = 12       # 同一库最多囤积的解密版本数（超过则删最老的）
_RETIRED_CONN_LIMIT = 16      # 被新快照替换下来的连接最多保留几个
_MAX_BUILD_ATTEMPTS = 4


def _ascii_safe(rel: str) -> str:
    """库相对路径 → 安全的缓存文件名（保留可读性，替换分隔符）。"""
    s = str(rel).replace("\\", "/").strip("/")
    return re.sub(r"[^\w.\-]+", "__", s)


def _as_text(value: object) -> str:
    """把 sqlite 取出的值安全转成 str（bytes 按 UTF-8 兜底解码）。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Wx4DB
# ---------------------------------------------------------------------------
class Wx4DB:
    """微信 4.x 本地数据库读取器。

    Args:
        db_dir: 微信 4.x 数据根目录（账号目录的父目录，如
            ``%USERPROFILE%/Documents/xwechat_files``）。None = 自动定位；
            也接受直接指向某个账号目录（含 ``db_storage``）。
        account: 账号目录名（如 ``wxid_xxx_abcd``）。None = 自动选当前登录账号。
    """

    def __init__(self, db_dir: Optional[str] = None,
                 account: Optional[str] = None) -> None:
        # 连接/构建状态先初始化，__init__ 中途失败也能安全 close()
        self._conns: Dict[Tuple[str, int], Tuple[str, sqlite3.Connection]] = {}
        self._retired: List[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._build_locks: Dict[str, threading.Lock] = {}
        self._build_locks_guard = threading.Lock()
        self._keys: Dict[str, bytes] = {}
        self._rel_files: Dict[str, Tuple[Path, int]] = {}
        self._head_md5_cache: Optional[Tuple[float, Dict[str, str]]] = None
        self.unkeyed: List[str] = []
        self._closed = False
        try:
            self._setup(db_dir, account)
        except Wx4Error:
            self.close()
            raise
        except Exception as exc:  # 任何底层异常都不许把 ctypes 细节抛给 GUI
            self.close()
            raise Wx4Error("初始化微信 4.x 数据库失败：%s" % exc) from None

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _setup(self, db_dir: Optional[str], account: Optional[str]) -> None:
        self.db_dir: Path
        self.account: str
        self.account_dir: Path

        if db_dir:
            root = Path(str(db_dir)).expanduser()
            if not root.exists():
                raise Wx4Error("指定的微信数据目录不存在：%s" % root)
            if (root / "db_storage").is_dir():
                # 传的是账号目录
                self.account_dir = root
                self.db_dir = root.parent
                account_name = root.name
                if account and account_name not in (account,) and \
                        _refdb.wxid_from_account(account_name) != account:
                    raise Wx4Error(
                        "参数冲突：db_dir 指向账号目录 %s，但 account=%s"
                        % (account_name, account))
            else:
                self.db_dir = root
                account_name = None
        else:
            found = _refdb.auto_detect_db_dir()
            if not found:
                raise Wx4Error(
                    "未找到微信 4.x 数据目录，请启动并登录微信 4.x 后重试"
                    "（若微信数据不在默认位置，请检查微信设置里的文件保存路径）")
            self.db_dir = Path(found)
            account_name = None

        accounts = _refdb.find_account_dirs(self.db_dir)
        if not accounts:
            raise Wx4Error(
                "微信数据目录下没有任何已登录账号（%s），请启动并登录微信 4.x"
                % self.db_dir)
        names = {p.name: p for p in accounts}

        if account_name is None and account:
            # 允许传完整账号目录名，或只传 wxid
            if account in names:
                account_name = account
            else:
                hits = [n for n in names
                        if n == account or _refdb.wxid_from_account(n) == account]
                if not hits:
                    raise Wx4Error("找不到账号目录：%s（现有：%s）"
                                   % (account, "、".join(sorted(names))))
                account_name = hits[0]
        if account_name is None:
            try:
                account_name = _refdb.pick_account(self.db_dir)
            except RuntimeError as exc:
                raise Wx4Error(str(exc)) from None
        if account_name not in names:
            raise Wx4Error("找不到账号目录：%s（现有：%s）"
                           % (account_name, "、".join(sorted(names))))

        self.account = account_name
        self.account_dir = self.db_dir / account_name
        self._load_db_files()

        self._keys = self._load_and_validate_keys()
        if self._keys and keycache.project_keys(self.account) != self._keys:
            # 首次导入历史缓存 / 形态规范化后回写项目内缓存，之后不再依赖历史文件
            keycache.save_keys(self.account, self._keys)
        missing = self._missing_rels()
        if missing:
            self._extract_missing_keys(missing)
        if not any(self._keys.get(rel) for rel, _p, _s in self.db_files):
            raise Wx4Error(
                "无法从微信进程获取数据库密钥（账号 %s）。"
                "请启动并登录微信 4.x、保持微信窗口运行后重试；"
                "若微信以管理员身份运行，本程序也需要管理员权限。"
                % self.account)
        self.unkeyed = [rel for rel, _p, _s in self.db_files
                        if not self._key_works(rel)]

    def _load_db_files(self) -> None:
        files = _refdb.collect_db_files(self.account_dir)
        if not files:
            raise Wx4Error("账号目录里没有数据库文件：%s" % self.account_dir)
        self.db_files: List[Tuple[str, Path, int]] = files
        self._rel_files = {rel: (path, size) for rel, path, size in files}

    def _load_and_validate_keys(self) -> Dict[str, bytes]:
        """读缓存 → 逐库页 1 校验 → 规范化 48/32 字节形态；丢弃失效条目。"""
        cached = keycache.load_keys(self.account)
        keys: Dict[str, bytes] = {}
        for rel, key in cached.items():
            entry = self._rel_files.get(rel)
            if entry is None or not _refdb.key_works(key, entry[0]):
                continue
            if len(key) == 48 and _refdb.verify_enc_key(
                    key[:32], _refdb.read_page1(entry[0]) or b""):
                key = key[:32]  # 标准库误存成 key+salt → 截回裸 key
            keys[rel] = key
        return keys

    def _missing_rels(self) -> List[str]:
        return [rel for rel, _p, _s in self.db_files
                if not self._keys.get(rel) or not self._key_works(rel)]

    def _key_works(self, rel: str) -> bool:
        entry = self._rel_files.get(rel)
        key = self._keys.get(rel)
        return bool(entry and key and _refdb.key_works(key, entry[0]))

    # ------------------------------------------------------------------
    # 密钥提取（Config.Cipher 内存扫描 + 多账号自愈）
    # ------------------------------------------------------------------
    def _extract_missing_keys(self, missing: Iterable[str]) -> None:
        # 可选兜底：显式提供 4.x 主密钥（32 字节 hex）时先按库头 salt 派生。
        # 微信 4.1.13+ 的 cfg 主密钥提取已不可信，故默认不自动读 cfg，只接受
        # 环境变量显式提供（排障/版本变更时的手动逃生口）。
        master = os.environ.get("WX4_MASTER_KEY", "").strip()
        if master and not any(self._keys.get(rel) for rel, _p, _s in self.db_files):
            try:
                derived = _refdb.derive_keys_from_master(master, self.db_files)
            except ValueError:
                raise Wx4Error(
                    "环境变量 WX4_MASTER_KEY 格式错误：需要 32 字节主密钥"
                    "（64 位十六进制字符串）") from None
            if derived:
                self._keys.update(derived)
                keycache.save_keys(self.account, self._keys)
            if not self._missing_rels():
                return
        if not _refdb.supported():
            raise Wx4Error(
                "无法读取微信进程内存（当前 Python 不是 64 位 Windows 版本）。"
                "请使用 64 位 Python 运行；若微信已运行仍失败，请以管理员身份运行。")
        try:
            cands = _refdb.scan_key_candidates()
        except RuntimeError as exc:
            raise Wx4Error("无法从微信进程获取数据库密钥：%s。"
                           "请启动并登录微信 4.x 后重试。" % exc) from None
        except OSError:
            cands = set()
        if cands:
            new_keys = _refdb.keys_from_candidates(cands, self.db_files)
            self._keys.update(new_keys)
            if new_keys:
                keycache.save_keys(self.account, self._keys)
            if not any(self._keys.get(rel) for rel, _p, _s in self.db_files):
                self._switch_account_by_keys(cands)

    def _switch_account_by_keys(self, cands) -> None:
        """当前账号一把库都解不开时，用同一批候选密钥给其它账号目录打分并切换。

        多账号机器上「最近修改 .db」的启发式可能选错账号：内存里的密钥属于
        当前登录账号，拿去解别的账号会全部页 1 校验失败。这里改用密码学校验
        决定用哪个账号目录。
        """
        best: Optional[Tuple[int, str, Dict[str, bytes],
                             List[Tuple[str, Path, int]]]] = None
        for d in _refdb.find_account_dirs(self.db_dir):
            if d.name == self.account:
                continue
            files = _refdb.collect_db_files(d)
            if not files:
                continue
            keys = _refdb.keys_from_candidates(cands, files)
            if keys and (best is None or len(keys) > best[0]):
                best = (len(keys), d.name, keys, files)
        if best is None:
            return
        ok, name, keys, files = best
        log.info("按密钥校验切换账号目录：%s（%d/%d 个库可用）",
                 name, ok, len(files))
        self.account = name
        self.account_dir = self.db_dir / name
        self.db_files = files
        self._rel_files = {rel: (path, size) for rel, path, size in files}
        self._keys = {rel: k for rel, k in keys.items()
                      if _refdb.key_works(k, self._rel_files[rel][0])}
        keycache.save_keys(self.account, self._keys)

    # ------------------------------------------------------------------
    # 对外属性
    # ------------------------------------------------------------------
    @property
    def wxid(self) -> str:
        """当前账号 wxid（账号目录名去掉末尾 4 位哈希）。"""
        return _refdb.wxid_from_account(self.account)

    def key_report(self) -> Dict[str, object]:
        """密钥诊断信息（给测试/排障用，不参与业务逻辑）。"""
        ok = [rel for rel, _p, _s in self.db_files if self._key_works(rel)]
        return {
            "account": self.account,
            "wxid": self.wxid,
            "account_dir": str(self.account_dir),
            "db_files": len(self.db_files),
            "keys_ok": len(ok),
            "unkeyed": [rel for rel, _p, _s in self.db_files if rel not in ok],
        }

    # ------------------------------------------------------------------
    # open：解密 + 合并 WAL + 缓存连接
    # ------------------------------------------------------------------
    def open(self, rel: str) -> sqlite3.Connection:
        """打开 4.x 数据库并返回可读 ``sqlite3.Connection``。

        ``rel`` 形如 ``"sns/sns.db"`` / ``"contact\\contact.db"``（反斜杠、大小写
        不敏感；也接受只写文件名，前提是全库唯一）。首次调用会解密并合并
        ``-wal``（缓存在 ``.wx4_cache/db/<account>/``），之后命中缓存直接返回。
        连接按「库 + 调用线程」缓存，同一线程多次调用复用同一连接。
        """
        if self._closed:
            raise Wx4Error("数据库连接已关闭，请重新初始化 wx4.get_db()")
        rel_key = self._resolve_rel(rel)
        if not self._keys.get(rel_key):
            raise Wx4Error(
                "数据库 %s 没有可用密钥（%s）。请确认微信已启动并登录 4.x，"
                "必要时删除缓存 %s 后重试。"
                % (rel_key, self._key_summary(),
                   keycache.keys_path(self.account)))
        last_exc: Optional[Exception] = None
        for attempt in (0, 1):
            try:
                path = self._ensure_decrypted(rel_key)
            except Wx4Error:
                raise
            except OSError as exc:  # 源库被并发改名/权限不足等
                raise Wx4Error("读取数据库失败（%s）：%s"
                               % (rel_key, _short_err(exc))) from None
            try:
                return self._conn_for(rel_key, path)
            except sqlite3.Error as exc:  # 缓存文件刚被并发清理/替换
                last_exc = exc
                self._drop_thread_conn(rel_key)
                try:  # 删掉打不开的缓存，下一次循环强制重建
                    path.unlink()
                except OSError:
                    pass
                time.sleep(0.15)
        raise Wx4Error("打开数据库失败（%s）：%s"
                       % (rel_key, _short_err(last_exc)))

    def _resolve_rel(self, rel: object) -> str:
        """规范化库标识并匹配到实际存在的库（大小写不敏感 / 允许 basename）。"""
        s = str(rel or "").replace("\\", "/").strip().strip("/")
        while s.startswith("./"):
            s = s[2:]
        if not s:
            raise Wx4Error("数据库标识为空，应形如 \"sns/sns.db\"")
        low = s.lower()
        if low.startswith("db_storage/"):
            low = low[len("db_storage/"):]
            s = s[len("db_storage/"):]
        for known in self._rel_files:
            if known.lower() == low:
                return known
        if "/" not in low:
            hits = [r for r in self._rel_files if r.rsplit("/", 1)[-1].lower() == low]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise Wx4Error("数据库名 %s 不唯一，请写完整相对路径（%s）"
                               % (rel, "、".join(sorted(hits))))
        raise Wx4Error("找不到数据库：%s（可用库：%s）"
                       % (rel, "、".join(sorted(self._rel_files))))

    def _key_summary(self) -> str:
        ok = sum(1 for rel, _p, _s in self.db_files if self._keys.get(rel))
        return "已加载 %d/%d 个密钥" % (ok, len(self.db_files))

    # -- 解密产物缓存 ------------------------------------------------
    def _cache_dir(self) -> Path:
        return keycache.db_cache_dir(self.account)

    def _stamp(self, rel: str, src: Path, key: bytes) -> str:
        """源库 + WAL + 密钥的快照指纹（任何一项变化都要重建）。"""
        try:
            st = src.stat()
        except OSError:
            raise Wx4Error("数据库文件不存在或不可读：%s" % src) from None
        wal = _refdb.wal_path_of(src)
        wal_sig = "0"
        if wal is not None:
            try:
                wst = wal.stat()
                wal_sig = "%x-%x" % (wst.st_mtime_ns, wst.st_size)
            except OSError:
                wal_sig = "0"
        raw = "%s|%x|%x|%s|%s" % (rel, st.st_mtime_ns, st.st_size,
                                  wal_sig, key.hex())
        return _refdb.md5_hex(raw.encode("utf-8"))[:16]

    def _ensure_decrypted(self, rel: str) -> Path:
        src, _size = self._rel_files[rel]
        key = self._keys[rel]
        target = self._cache_dir() / ("%s.%s.sqlite" % (_ascii_safe(rel),
                                                        self._stamp(rel, src, key)))
        if target.is_file() and target.stat().st_size > 0:
            return target
        with self._build_lock(rel):
            if target.is_file() and target.stat().st_size > 0:
                return target
            return self._build(rel, src, key, target)

    def _build_lock(self, rel: str) -> threading.Lock:
        with self._build_locks_guard:
            return self._build_locks.setdefault(rel, threading.Lock())

    def _build(self, rel: str, src: Path, key: bytes, target: Path) -> Path:
        """解密 + 合并 WAL + 校验后原子发布到 ``target``。

        策略同参考实现：先解出「主库自洽快照」当底线，再尝试把 WAL 增量合并进
        副本；合并失败就退回仅主库快照（可能少最近几条朋友圈，但一定可读）。
        中间产物与目标文件同名不同后缀，**成功才 os.replace**，失败不毁旧缓存。
        """
        try:
            self._cache_dir().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise Wx4Error("无法创建解密缓存目录 %s：%s"
                           % (self._cache_dir(), exc)) from None
        self._cleanup_stale_tmp(target.parent)

        tmp = target.with_name("%s.%d.%x.tmp" % (target.name, os.getpid(),
                                                 threading.get_ident()))
        merged: Optional[Path] = None
        try:
            snap_ok = False
            for attempt in range(1, _MAX_BUILD_ATTEMPTS + 1):
                try:
                    _refdb.decrypt_file(src, tmp, key)
                    # 头标记改回 rollback 模式：只读查询就不会生成 -wal/-shm
                    _refdb.make_rollback_mode(tmp)
                    if _refdb.check_sqlite_ok(tmp):
                        snap_ok = True
                        break
                except OSError as exc:
                    log.debug("解密 %s 失败（第 %d 次）：%s", rel, attempt, exc)
                time.sleep(0.35)
            if not snap_ok:
                previous = self._previous_good(rel, target)
                if previous is not None:
                    log.warning("主库持续处于 checkpoint 撕裂状态，改用上一份可用副本：%s",
                                rel)
                    return previous
                raise Wx4Error(
                    "数据库解密结果校验失败（微信正在写入 checkpoint），"
                    "请稍后重试：%s" % rel)

            wal = _refdb.wal_path_of(src)
            applied = 0
            if wal is not None:
                try:
                    wal_size = wal.stat().st_size
                except OSError:
                    wal_size = 0
                if wal_size > _refdb.WAL_HEADER_SZ:
                    merged = target.with_name(tmp.name + ".wal")
                    for attempt in (1, 3):
                        try:
                            shutil.copyfile(tmp, merged)
                            applied = _refdb.merge_wal(merged, wal, key, 0)
                            if _refdb.check_sqlite_ok(merged):
                                _refdb.make_rollback_mode(merged)
                                if not _refdb.check_sqlite_ok(merged):
                                    raise OSError("WAL 合并结果改成 rollback 后校验失败")
                                os.replace(merged, tmp)
                                merged = None
                                break
                        except Exception as exc:  # WAL 合并是尽力而为：畸形/竞态都退化为仅主库快照
                            log.debug("WAL 合并第 %d 次失败（%s）：%s", attempt, rel, exc)
                        time.sleep(0.3)
                    if merged is not None:
                        log.warning("WAL 合并失败（微信持续写入），使用仅主库快照：%s", rel)
            try:
                os.replace(tmp, target)
            except OSError as exc:
                raise Wx4Error("写解密缓存失败（%s）：%s" % (rel, exc)) from None
            tmp = None
            self._remove_sidecars(target)
            log.debug("解密完成：%s → %s（WAL 帧 %d）", rel, target.name, applied)
        finally:
            for leftover in (tmp, merged):
                if leftover is not None and leftover.exists():
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
        self._cleanup_old_versions(rel, target)
        return target

    def _previous_good(self, rel: str, exclude: Path) -> Optional[Path]:
        """同库已存在的旧解密文件里，找一份能通过 quick_check 的当兜底。"""
        pattern = _ascii_safe(rel) + ".*.sqlite"
        best: Optional[Tuple[float, Path]] = None
        try:
            for cand in self._cache_dir().glob(pattern):
                if cand == exclude or not cand.is_file():
                    continue
                try:
                    mtime = cand.stat().st_mtime
                except OSError:
                    continue
                if best is None or mtime > best[0]:
                    best = (mtime, cand)
        except OSError:
            return None
        if best is not None:
            _refdb.make_rollback_mode(best[1])
            if _refdb.check_sqlite_ok(best[1]):
                self._remove_sidecars(best[1])
                return best[1]
        return None

    def _cleanup_old_versions(self, rel: str, keep: Path) -> None:
        """清理同库的旧解密版本（只删 2 分钟前的，避开其它线程刚拿到路径的窗口）。

        超过 ``_MAX_LIVE_VERSIONS`` 份时再放宽到 10 秒前（写空的缓存文件在
        Windows 上还会被打开中的连接占住，unlink 失败就下次再来）。
        """
        now = time.time()
        olds: List[Path] = []
        for cand in self._cache_dir().glob(_ascii_safe(rel) + ".*.sqlite"):
            if cand == keep:
                continue
            try:
                mtime = cand.stat().st_mtime
            except OSError:
                continue
            olds.append(cand)
            if now - mtime < _STALE_VERSION_SECONDS:
                continue
            try:
                cand.unlink()
                self._remove_sidecars(cand)
            except OSError:
                continue
        if len(olds) <= _MAX_LIVE_VERSIONS:
            return
        olds.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0)
        for cand in olds[: max(0, len(olds) - _MAX_LIVE_VERSIONS)]:
            try:
                if now - cand.stat().st_mtime < 10:
                    continue
                cand.unlink()
                self._remove_sidecars(cand)
            except OSError:
                continue

    @staticmethod
    def _remove_sidecars(path: Path) -> None:
        """删掉 sqlite 只读连接可能旁生的 ``-wal``/``-shm``（只针对缓存产物）。"""
        for suffix in ("-wal", "-shm"):
            try:
                Path(str(path) + suffix).unlink()
            except OSError:
                pass

    @staticmethod
    def _cleanup_stale_tmp(folder: Path) -> None:
        try:
            for cand in folder.glob("*.tmp*"):
                try:
                    if time.time() - cand.stat().st_mtime > _STALE_TMP_SECONDS:
                        cand.unlink()
                except OSError:
                    continue
        except OSError:
            pass

    # -- 连接缓存 ----------------------------------------------------
    def _conn_for(self, rel: str, path: Path) -> sqlite3.Connection:
        tid = threading.get_ident()
        key = (rel, tid)
        path_s = str(path)
        with self._conns_lock:
            rec = self._conns.get(key)
            if rec is not None:
                rec_path, conn = rec
                if rec_path == path_s:
                    return conn
                # 同库出现了新快照：旧连接不立即关闭，避免调用方手里的引用突然失效
                # （旧连接指向旧快照，仍然可读；只在新连接建立后择机回收）。
                self._retire(conn)
                self._conns.pop(key, None)
            conn = sqlite3.connect(_refdb.sqlite_uri(path), uri=True,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.text_factory = _refdb.sqlite_text_factory
            self._conns[key] = (path_s, conn)
            if len(self._conns) > 32:
                self._prune_dead_thread_conns()
            return conn

    def _retire(self, conn: sqlite3.Connection) -> None:
        """把被新快照替换下来的连接放进回收区（调用方持锁）。"""
        self._retired.append(conn)
        while len(self._retired) > _RETIRED_CONN_LIMIT:
            old = self._retired.pop(0)
            try:
                old.close()
            except sqlite3.Error:
                pass

    def _prune_dead_thread_conns(self) -> None:
        """清掉已结束线程留下的连接（调用方持锁）。线程 ident 可能被复用，但
        多留的连接只会浪费句柄；按 threading.enumerate() 的存活线程过滤即可。"""
        alive = {t.ident for t in threading.enumerate() if t.ident is not None}
        for key in [k for k in self._conns if k[1] not in alive]:
            rec = self._conns.pop(key, None)
            if rec is not None:
                try:
                    rec[1].close()
                except sqlite3.Error:
                    pass

    def _drop_thread_conn(self, rel: str) -> None:
        key = (rel, threading.get_ident())
        with self._conns_lock:
            rec = self._conns.pop(key, None)
        if rec is not None:
            try:
                rec[1].close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------
    # 联系人
    # ------------------------------------------------------------------
    def _contact_rel(self) -> str:
        hits = [rel for rel in self._rel_files
                if rel.rsplit("/", 1)[-1].lower() == "contact.db"]
        if not hits:
            raise Wx4Error("未找到联系人数据库 contact.db（账号 %s）" % self.account)
        return sorted(hits)[0]

    def _head_image_rel(self) -> Optional[str]:
        hits = [rel for rel in self._rel_files
                if rel.rsplit("/", 1)[-1].lower() == "head_image.db"]
        return sorted(hits)[0] if hits else None

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
        try:
            return [str(r[1]) for r in conn.execute(
                "PRAGMA table_info(%s)" % table)]
        except sqlite3.Error:
            return []

    def _contact_select(self, conn: sqlite3.Connection) -> str:
        """按 contact 表真实列名拼 SELECT（不同 4.x 小版本列可能有差异）。"""
        cols = {c.lower(): c for c in self._table_columns(conn, "contact")}
        if "username" not in cols:
            raise Wx4Error("contact 表结构异常：缺少 username 列")
        parts = []
        for field in CONTACT_FIELDS:
            real = cols.get(field)
            if field == "username":
                parts.append("%s AS username" % real)
            elif field == "local_type":
                parts.append("COALESCE(%s,0) AS local_type" % real if real
                             else "0 AS local_type")
            else:
                parts.append("COALESCE(%s,'') AS %s" % (real, field) if real
                             else "'' AS %s" % field)
        return "SELECT %s FROM contact" % ", ".join(parts)

    def _head_md5_map(self) -> Dict[str, str]:
        """``username -> 头像 md5``（来自 head_image.db；读不到就空表）。

        头像 md5 在导出期间基本不变，缓存 60 秒，避免逐条 ``contact()`` 时反复
        整表查询（GUI 会把联系人逐个查一遍）。
        """
        now = time.monotonic()
        cached = self._head_md5_cache
        if cached is not None and now - cached[0] < 60:
            return cached[1]
        rel = self._head_image_rel()
        if rel is None:
            return {}
        try:
            conn = self.open(rel)
            rows = conn.execute("SELECT username, md5 FROM head_image").fetchall()
        except (Wx4Error, sqlite3.Error) as exc:
            log.debug("读取 head_image.db 失败：%s", exc)
            return {}
        out: Dict[str, str] = {}
        for r in rows:
            user = _as_text(r[0]).strip()
            md5 = _as_text(r[1]).strip().lower()
            if user and md5:
                out[user] = md5
        self._head_md5_cache = (now, out)
        return out

    @staticmethod
    def _row_to_contact(row: sqlite3.Row, head_map: Dict[str, str]) -> Dict[str, object]:
        username = _as_text(row["username"])
        md5 = _as_text(row["head_img_md5"]).strip().lower()
        if not md5:
            md5 = head_map.get(username, "")
        return {
            "username": username,
            "nick_name": _as_text(row["nick_name"]),
            "remark": _as_text(row["remark"]),
            "alias": _as_text(row["alias"]),
            "head_img_md5": md5,
            "local_type": _as_int(row["local_type"], 0),
        }

    def contacts(self) -> List[Dict[str, object]]:
        """全量联系人列表（字段见 :data:`CONTACT_FIELDS`）。"""
        conn = self.open(self._contact_rel())
        try:
            rows = conn.execute(self._contact_select(conn)).fetchall()
        except sqlite3.Error as exc:
            raise Wx4Error("读取联系人失败：%s" % _short_err(exc)) from None
        head_map = self._head_md5_map()
        return [self._row_to_contact(r, head_map) for r in rows]

    def contact(self, username: str) -> Optional[Dict[str, object]]:
        """按 username 查单个联系人；不存在返回 None。"""
        user = str(username or "").strip()
        if not user:
            return None
        conn = self.open(self._contact_rel())
        sql = self._contact_select(conn) + " WHERE username=? LIMIT 1"
        try:
            row = conn.execute(sql, (user,)).fetchone()
        except sqlite3.Error as exc:
            raise Wx4Error("读取联系人失败：%s" % _short_err(exc)) from None
        if row is None:
            return None
        return self._row_to_contact(row, self._head_md5_map())

    def self_info(self) -> Dict[str, str]:
        """当前登录账号资料：``{"username", "nick_name", "remark"}``。

        联系人库不可用时退化为只含 wxid 的空资料，不抛异常（GUI 轮询要稳）。
        """
        fallback = {"username": self.wxid, "nick_name": "", "remark": ""}
        try:
            row = self.contact(self.wxid)
        except Wx4Error as exc:
            log.debug("读取本人资料失败：%s", exc)
            return fallback
        if not row:
            return fallback
        return {
            "username": _as_text(row.get("username")) or self.wxid,
            "nick_name": _as_text(row.get("nick_name")),
            "remark": _as_text(row.get("remark")),
        }

    # ------------------------------------------------------------------
    # 释放
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭所有缓存的 sqlite 连接（解密缓存文件保留，下次再开会命中）。"""
        with self._conns_lock:
            conns = [c for _p, c in self._conns.values()]
            conns += self._retired
            self._conns.clear()
            self._retired.clear()
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._closed = True

    def __enter__(self) -> "Wx4DB":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _short_err(exc: Optional[BaseException]) -> str:
    """把 sqlite/OS 异常压成一句可读中文后缀（不泄露调用栈）。"""
    if exc is None:
        return "未知错误"
    text = str(exc).strip().replace("\n", " ")
    if len(text) > 160:
        text = text[:157] + "..."
    return text or exc.__class__.__name__


# ---------------------------------------------------------------------------
# 进程级单例
# ---------------------------------------------------------------------------
_SINGLETON: Optional[Wx4DB] = None
_SINGLETON_LOCK = threading.RLock()


def get_db() -> Wx4DB:
    """返回进程级 :class:`Wx4DB` 单例。

    微信未启动/未登录/取不到密钥时抛 ``Wx4Error("...请启动并登录微信 4.x...")``，
    调用方（GUI）直接展示 ``str(e)`` 即可，不必区分底层原因。
    """
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = Wx4DB()
        return _SINGLETON


def reset() -> None:
    """释放单例（测试/切换账号/微信重启用）。"""
    global _SINGLETON
    with _SINGLETON_LOCK:
        db = _SINGLETON
        _SINGLETON = None
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


__all__ = ["Wx4DB", "get_db", "reset", "CONTACT_FIELDS"]
