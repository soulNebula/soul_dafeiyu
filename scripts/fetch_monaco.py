# -*- coding: utf-8 -*-
"""下载并本地化 Monaco 编辑器（离线可用）。用法：python scripts/fetch_monaco.py

产物：web/vendor/monaco/vs（约 14MB，git 忽略）
优先源：npmmirror（国内可用），失败可换 unpkg。
"""
from __future__ import annotations

import io
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "web" / "vendor" / "monaco"
VERSION = "0.52.0"
SOURCES = [
    f"https://registry.npmmirror.com/monaco-editor/-/monaco-editor-{VERSION}.tgz",
    f"https://unpkg.com/monaco-editor@{VERSION}/monaco-editor-{VERSION}.tgz",
]


def main() -> int:
    if (DEST / "vs" / "loader.js").is_file():
        print(f"已存在：{DEST / 'vs' / 'loader.js'}，跳过")
        return 0
    data = None
    for url in SOURCES:
        try:
            print("下载", url)
            with urllib.request.urlopen(url, timeout=120) as r:
                data = r.read()
            break
        except Exception as exc:  # noqa: BLE001
            print("失败：", exc)
    if not data:
        print("全部源失败，请手动下载 monaco-editor tgz 并解压 min/vs 到", DEST)
        return 1
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name.startswith("package/min/vs")]
        tar.extractall(ROOT / ".data" / "monaco_tmp", members=members)
    src = ROOT / ".data" / "monaco_tmp" / "package" / "min" / "vs"
    import shutil
    target = DEST / "vs"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(src, target)
    shutil.rmtree(ROOT / ".data" / "monaco_tmp", ignore_errors=True)
    print("完成：", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
