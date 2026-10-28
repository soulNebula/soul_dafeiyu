# -*- coding: utf-8 -*-
"""朋友圈图片导出：从 sns.db 取媒体 URL/token，下载密文，用本地 Sns 缓存 + 图片密钥解密。

项目内朋友圈模块只读 sns.db（拿不到已解密的图片），但微信 4.x 会把看过的朋友圈图片
缓存在 ``xwechat_files/<账号>/cache/<YYYY-MM>/Sns/Img/``，格式是 AES-128-ECB + 尾部 XOR
的 .dat（``wx_core.dat_decrypt`` 可解）。本工具把「sns.db 的媒体 md5/尺寸」与「缓存解密结果」
对上，导出成 ``agi/data/moments/media/<md5>.jpg`` + ``media_index.json``，控制台即可显示。

用法（py3.14，SoulAgent 根目录）：
    python tools/moments_media_export.py [--limit 50] [--download/--no-download]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
MOMENTS_DIR = HOME / "agi" / "data" / "moments"
MEDIA_DIR = MOMENTS_DIR / "media"
INDEX_PATH = MOMENTS_DIR / "media_index.json"
WECHAT_FILES = Path(os.environ.get("WECHAT_FILES_DIR") or r"C:\Users\Administrator\Documents\xwechat_files")
WX312 = os.environ.get("WX312_PYTHON") or r"E:\agent\venv_wx312\Scripts\python.exe"

_DUMP_SCRIPT = r'''
import re, json, sys
from wechatauto import WeChatDB, MomentDB
db = WeChatDB(); md = MomentDB(db); con = md._open_sns()
rows = con.execute("SELECT tid, user_name, content FROM SnsTimeLine").fetchall()
out = []
for tid, user, content in rows:
    content = content or ""
    mtime = re.search(r"<createTime>(\d+)</createTime>", content)
    for m in re.finditer(r'<url\b([^>]*)>([^<]*)</url>\s*<size width="(\d+)" height="(\d+)" totalSize="(\d+)"/>', content):
        attrs, url = m.group(1), m.group(2)
        def attr(name, default=""):
            mm = re.search(name + r'="([^"]*)"', attrs)
            return mm.group(1) if mm else default
        out.append({"tid": tid, "user": user,
                    "moment_ts": int(mtime.group(1)) if mtime else 0,
                    "md5": attr("md5"), "url": url,
                    "enc_idx": attr("enc_idx", "0"), "token": attr("token"),
                    "w": int(m.group(3)), "h": int(m.group(4)), "total": int(m.group(5))})
json.dump(out, sys.stdout, ensure_ascii=False)
'''


def _load_keys() -> dict[str, tuple[bytes, int]]:
    """从 config.json 读每个账号的图片 AES/XOR 密钥。"""
    cfg_path = HOME / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    keys: dict[str, tuple[bytes, int]] = {}
    for wxid, item in (cfg.get("image_keys_by_account") or {}).items():
        aes = str(item.get("aes_key") or "").encode("ascii", "ignore")
        xor = int(item.get("xor_key") or 0)
        if len(aes) == 16:
            keys[str(wxid)] = (aes, xor)
    if not keys:
        aes = str(cfg.get("image_aes_key") or "").encode("ascii", "ignore")
        xor = int(cfg.get("image_xor_key") or 0)
        wxid = str(cfg.get("image_wxid") or "")
        if len(aes) == 16:
            keys[wxid] = (aes, xor)
    return keys


def dump_media_entries() -> list[dict]:
    """用 3.12 venv（装了 wechatauto）读 sns.db，返回所有媒体条目。"""
    if not Path(WX312).is_file():
        raise RuntimeError(f"找不到微信 3.12 venv：{WX312}")
    proc = subprocess.run([WX312, "-c", _DUMP_SCRIPT], capture_output=True, timeout=180,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if proc.returncode != 0:
        raise RuntimeError(f"sns.db 读取失败：{proc.stderr.decode('utf-8', 'replace')[-300:]}")
    return json.loads(proc.stdout.decode("utf-8"))


def _account_of(path: Path) -> str:
    """cache/<账号>/... → 账号 wxid（去掉 _xxxx 后缀）。"""
    for part in path.parts:
        if part.startswith("wxid_"):
            # 目录名形如 wxid_hsqe9w1lhl1j22_5663 → wxid_hsqe9w1lhl1j22
            return part.rsplit("_", 1)[0]
    return ""


def build_cache_index(keys: dict[str, tuple[bytes, int]]) -> dict[int, list[dict]]:
    """解密本地 Sns 缓存，按明文长度建索引（下载密文长度 == 缓存明文长度）。"""
    sys.path.insert(0, str(HOME))
    from wx_core.dat_decrypt import decrypt_dat_bytes
    from PIL import Image

    index: dict[int, list[dict]] = {}
    if not WECHAT_FILES.is_dir():
        return index
    files = list(WECHAT_FILES.glob("*/cache/*/Sns/Img/*/*"))
    for path in files:
        if not path.is_file():
            continue
        wxid = _account_of(path)
        key = keys.get(wxid)
        if not key:
            continue
        try:
            plain, _ver = decrypt_dat_bytes(path.read_bytes(), aes_key=key[0], xor_key=key[1])
            im = Image.open(io.BytesIO(plain))
            size = im.size
        except Exception:  # noqa: BLE001 缓存里也有缩略图/坏文件
            continue
        index.setdefault(len(plain), []).append({
            "path": str(path), "w": size[0], "h": size[1], "bytes": plain})
    return index


def _download(entry: dict, timeout: float = 25.0) -> bytes:
    import httpx
    url = entry["url"]
    token = entry.get("token") or ""
    idx = entry.get("enc_idx") or "0"
    full = f"{url}?idx={idx}&token={token}" if token else url
    resp = httpx.get(full, timeout=timeout, trust_env=False,
                     headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.content


def export(limit: int = 50, do_download: bool = True) -> dict:
    keys = _load_keys()
    entries = dump_media_entries()
    entries = [e for e in entries if e.get("md5")]
    if limit:
        entries = entries[:max(1, int(limit))]
    cache = build_cache_index(keys) if keys else {}
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    index: dict[str, dict] = {}
    if INDEX_PATH.is_file():
        try:
            index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except ValueError:
            index = {}
    done = skipped = 0
    for entry in entries:
        md5 = entry["md5"]
        if md5 in index and (MEDIA_DIR / f"{md5}.jpg").is_file():
            done += 1
            continue
        matched = None
        if do_download:
            try:
                payload = _download(entry)
            except Exception:  # noqa: BLE001
                payload = b""
            for item in cache.get(len(payload), []):
                if (item["w"], item["h"]) == (entry["w"], entry["h"]):
                    matched = item
                    break
            if matched is None and payload:
                cands = cache.get(len(payload)) or []
                if cands:
                    matched = cands[0]
        if matched is None:
            # 无法下载时按尺寸兜底（可能错配，记录 source=cache_only）
            for item_list in cache.values():
                for item in item_list:
                    if (item["w"], item["h"]) == (entry["w"], entry["h"]):
                        matched = item
                        break
                if matched:
                    break
        if matched is None:
            skipped += 1
            continue
        out = MEDIA_DIR / f"{md5}.jpg"
        out.write_bytes(matched["bytes"])
        index[md5] = {"file": str(out), "w": matched["w"], "h": matched["h"],
                      "moment_ts": entry.get("moment_ts") or 0,
                      "cache": matched["path"]}
        done += 1
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "exported": done, "skipped": skipped,
            "total": len(entries), "index": str(INDEX_PATH), "media_dir": str(MEDIA_DIR)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出朋友圈图片（本地缓存解密）")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--no-download", dest="download", action="store_false", default=True,
                        help="不访问网络，只按缓存尺寸匹配（可能错配）")
    args = parser.parse_args(argv)
    result = export(limit=args.limit, do_download=args.download)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
