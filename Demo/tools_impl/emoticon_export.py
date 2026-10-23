"""表情包全通路导出（移植自参考项目 Wechat-Emoticon-Parser v4.0-plus 的思路）。

导出内容：
* 商店表情包（`business/emoticon/Persist*` + `emoticon.db` 的包名/标题）
* 收藏/自定义表情（`kNonStoreEmoticonTable` 与 `cache/*/Emoticon*`）
* 解不开包名的进 `unknown/`
* wxgf 动画自动转真 GIF；其它保持原格式（png/gif/jpg/webp）

算法要点（与参考实现一致，已本机验证）：
    key = md5(f"{seed}{wxid}EMOTICON").digest()[:16]
    AES-128-CBC(key=IV, PKCS7)
其中 seed = cfgDword（图片密钥派生用的同一常量）。

用法见 `tools/export_emoticons.py`。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from wx_core import emoticon, media_cache, store, wcdb

_SYS_EMOTICON_IDS = ("notifymessage", "fmessage", "medianote", "weixin", "newsapp",
                     "filehelper", "floatbottle", "qqmail", "tmessage", "qmessage")


def _clean(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or "")).strip()
    return s or "untitled"


def _emoticon_key(config: dict) -> tuple[int, str, bytes] | None:
    seed = config.get("image_cfg_dword")
    wxid = str(config.get("image_wxid") or "").strip()
    if not wxid:
        acc = media_cache.active_account_dir()
        if acc is not None and acc.name.startswith("wxid_"):
            wxid = acc.name.rsplit("_", 1)[0] if acc.name.count("_") >= 2 else acc.name
    try:
        seed_i = int(seed)
    except (TypeError, ValueError):
        return None
    if not wxid:
        return None
    return seed_i, wxid, emoticon.emoticon_key(seed_i, wxid)


def _read_emoticon_db(config: dict, log) -> dict:
    """读出 emoticon.db 里的包名/标题/md5 映射（解不开就返回空表，只导出 unknown）。"""
    info = {"fav": set(), "pkg": {}, "caps": {}, "cap_pkg": {}, "files": {}}
    try:
        acc = media_cache.active_account_dir()
        keys = wcdb.ensure_keys(acc, config, log=log)
        if not keys:
            return info
        reader = wcdb.SessionReader(acc, keys, str(config.get("image_wxid") or ""))
        path = reader._materialize("db_storage/emoticon/emoticon.db", "_emo_db")
        if not path:
            reader.close()
            return info
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            info["fav"] = {r[0] for r in con.execute("select md5 from kNonStoreEmoticonTable")}
        except sqlite3.Error:
            pass
        try:
            info["pkg"] = dict(con.execute(
                "select package_id_, package_name_ from kStoreEmoticonPackageTable"))
        except sqlite3.Error:
            pass
        try:
            for pid, md5, lang, cap in con.execute(
                    "select package_id_, md5_, language_, caption_ from kStoreEmoticonCaptionsTable"):
                info["caps"][md5] = cap
                info["cap_pkg"][md5] = pid
        except sqlite3.Error:
            pass
        try:
            for pid, md5, typ, sort in con.execute(
                    "select package_id_, md5_, type_, sort_order_ from kStoreEmoticonFilesTable"):
                info["files"][md5] = (pid, int(sort or 0))
        except sqlite3.Error:
            pass
        con.close()
        reader.close()
    except Exception as exc:  # noqa: BLE001
        log(f"读取 emoticon.db 失败，仅按 md5 命名: {exc}", "warn")
    return info


def _iter_source_files(account_dir: Path) -> list[Path]:
    out: list[Path] = []
    base = account_dir / "business" / "emoticon"
    for sub in ("Persist", "PersistStore", "Thumb", "ThumbStore", "Temp"):
        d = base / sub
        if d.is_dir():
            out += [p for p in d.rglob("*") if p.is_file()]
    try:
        out += [p for p in (account_dir / "cache").rglob("Emoticon/*/*") if p.is_file()]
    except OSError:
        pass
    return out


def export_emoticons(config: dict, out_dir: Path | None = None, log=None,
                     limit: int | None = None) -> dict:
    """导出所有本地表情包，返回 manifest（含 packs/favorites/unknown 与统计）。"""
    log = log or (lambda *a, **k: None)
    acc = media_cache.active_account_dir()
    if acc is None:
        raise RuntimeError("找不到微信数据目录")
    key_info = _emoticon_key(config)
    if not key_info:
        raise RuntimeError("缺少图片/表情密钥（image_cfg_dword / image_wxid），请先运行一次监听或提取密钥")
    seed, wxid, key = key_info
    out_dir = Path(out_dir) if out_dir else store.MEDIA_DIR / "emoticons_export"
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = _read_emoticon_db(config, log)
    manifest: dict = {"exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "account": acc.name, "packs": {}, "favorites": [], "unknown": [],
                      "stats": {"packs": 0, "stickers": 0, "favorites": 0, "unknown": 0}}
    used_names: dict[tuple[str, str], int] = {}
    seen_md5: set[str] = set()

    def save_one(data: bytes, ext: str, md5: str, src_name: str):
        """把解密后的一个表情写到磁盘，并返回 (相对路径, 最终扩展名)。"""
        if ext == "wxgf":
            stage = out_dir / f".stage_{md5}.gif"
            if not emoticon.wxgf_to_gif(data, stage):
                return None
            ext = "gif"
        else:
            stage = out_dir / f".stage_{md5}.{ext}"
            stage.write_bytes(data)
        return stage, ext

    def place(stage: Path, ext: str, md5: str, caption: str, pack: str) -> Path:
        cap = _clean(caption) if caption else ""
        base = cap if cap and cap != "untitled" else md5
        d = out_dir / ("store" if pack else "favorite") / (_clean(pack) if pack else "")
        d.mkdir(parents=True, exist_ok=True)
        cnt = used_names.get((str(d), base), 0)
        name = f"{base}.{ext}" if cnt == 0 else f"{base}_{cnt + 1}.{ext}"
        used_names[(str(d), base)] = cnt + 1
        dst = d / name
        try:
            stage.replace(dst)
        except OSError:
            import shutil
            shutil.copy2(stage, dst)
            stage.unlink(missing_ok=True)
        return dst

    processed = 0
    for src in _iter_source_files(acc):
        if limit is not None and processed >= limit:
            break
        processed += 1
        try:
            raw = src.read_bytes()
        except OSError:
            continue
        data = emoticon.decrypt_bytes(raw, key)
        if data is None:
            continue
        parts = emoticon._split_container(data)
        if not parts:
            parts = [data]
        for part in parts:
            fmt = emoticon.detect_format(part)
            if fmt is None:
                continue
            md5 = hashlib.md5(part).hexdigest()
            if md5 in seen_md5:
                continue
            # 文件名本身可能就是 md5（Persist 目录按内容 md5 命名）
            name_md5 = src.name.split(".")[0]
            db_md5 = md5 if md5 in meta["files"] or md5 in meta["caps"] else (
                name_md5 if name_md5 in meta["files"] or name_md5 in meta["caps"] else md5)
            seen_md5.add(md5)
            saved = save_one(part, fmt, md5, src.name)
            if saved is None:
                continue
            stage, final_ext = saved
            if db_md5 in meta["files"]:
                pid, sort_order = meta["files"][db_md5]
                pack_name = _clean(meta["pkg"].get(pid, ""))
                caption = meta["caps"].get(db_md5, "")
                dst = place(stage, final_ext, md5, caption, pack_name)
                pk = manifest["packs"].setdefault(pack_name or "未命名包", {
                    "package_id": pid, "stickers": []})
                pk["stickers"].append({"md5": md5, "caption": caption, "sort": sort_order,
                                       "file": str(dst.relative_to(out_dir)).replace("\\", "/"),
                                       "ext": final_ext, "size": int(dst.stat().st_size)})
                manifest["stats"]["stickers"] += 1
            elif db_md5 in meta["fav"] or db_md5 in meta["caps"]:
                caption = meta["caps"].get(db_md5, "")
                dst = place(stage, final_ext, md5, caption, "")
                manifest["favorites"].append({
                    "md5": md5, "caption": caption,
                    "file": str(dst.relative_to(out_dir)).replace("\\", "/"),
                    "ext": final_ext, "size": int(dst.stat().st_size)})
                manifest["stats"]["favorites"] += 1
            else:
                d = out_dir / "unknown"
                d.mkdir(parents=True, exist_ok=True)
                dst = d / f"{md5}.{final_ext}"
                try:
                    stage.replace(dst)
                except OSError:
                    import shutil
                    shutil.copy2(stage, dst)
                    stage.unlink(missing_ok=True)
                manifest["unknown"].append({
                    "md5": md5, "file": f"unknown/{dst.name}", "ext": final_ext,
                    "size": int(dst.stat().st_size)})
                manifest["stats"]["unknown"] += 1

    manifest["stats"]["packs"] = len(manifest["packs"])
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"表情包导出完成：{manifest['stats']['packs']} 个包 / "
        f"{manifest['stats']['stickers']} 个商店表情 / "
        f"{manifest['stats']['favorites']} 个收藏 / "
        f"{manifest['stats']['unknown']} 个未分类 → {out_dir}")
    return manifest
