# -*- coding: utf-8 -*-
"""下载 GGUF/模型文件：断点续传 + 大小/可选 SHA256 校验。

用法：
    python tools/fetch_model.py --url https://.../model.gguf --out E:/agent/models/x.gguf
    python tools/fetch_model.py --url ... --out ... --sha256 <64位hex>
    python tools/fetch_model.py --url ... --out ... --json

说明：
- 先写 ``<out>.part``，完成后原子 rename；中断后重跑会 Range 续传；
- 服务器不支持 Range 时自动重下；
- 只做下载/校验，不启动模型；模型启动走 802 模型页或 llama-server。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _human(n: int) -> str:
    value = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def download(url: str, out: Path, *, sha256: str = "", timeout: float = 60.0,
             progress=print) -> dict:
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "url must be http(s)"}
    out = Path(out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    existing = part.stat().st_size if part.is_file() else 0
    headers = {"User-Agent": "soulagent-fetch-model"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and existing:
            # .part 已完整
            response = None
        else:
            return {"ok": False, "error": f"HTTP {exc.code}: {url}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    resumed = False
    total = 0
    try:
        if response is not None:
            status = int(getattr(response, "status", 200) or 200)
            length = int(response.headers.get("Content-Length") or 0)
            total = existing + length if status == 206 else length
            mode = "ab" if (status == 206 and existing) else "wb"
            if status == 206:
                resumed = True
            elif existing:
                progress("服务器不支持续传，重新下载")
            written = existing if mode == "ab" else 0
            next_tick = 0.0
            with response, part.open(mode) as fh:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    now = time.time()
                    if now - next_tick > 0.5:
                        pct = f" {written * 100 // total:3d}%" if total else ""
                        progress(f"{_human(written)}{pct}" + (" (resume)" if resumed else ""))
                        next_tick = now
        if not part.is_file() or part.stat().st_size <= 0:
            return {"ok": False, "error": "empty_download"}
        size = part.stat().st_size
        digest = ""
        if sha256:
            h = hashlib.sha256()
            with part.open("rb") as fh:
                for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                    h.update(chunk)
            digest = h.hexdigest()
            if digest.lower() != str(sha256).lower():
                return {"ok": False, "error": "sha256_mismatch",
                        "expected": sha256, "actual": digest, "part": str(part)}
        os.replace(part, out)
        return {"ok": True, "path": str(out), "size": size,
                "sha256": digest, "resumed": resumed}
    finally:
        try:
            response.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载模型文件（断点续传/校验）")
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--sha256", default="")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = download(args.url, Path(args.out), sha256=args.sha256,
                      timeout=args.timeout,
                      progress=(lambda *_: None) if args.json else print)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
