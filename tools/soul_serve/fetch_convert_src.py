# -*- coding: utf-8 -*-
"""拉取与本地 llama-server **同 commit** 的 llama.cpp Python 转换源码。

为什么需要它
------------
- llama.cpp 的 ``convert_hf_to_gguf.py`` 与 ``gguf-py`` 强耦合：用 master 版转换脚本 +
  PyPI 旧版 ``gguf`` 会报 ``AttributeError: type object 'MODEL_ARCH' has no attribute 'DFLASH'``；
- GitHub raw 在国内经常被 reset，这里统一走 ``https://gh-proxy.com/`` 代理。

本脚本把以下文件拉到 ``--dest``（默认 ``E:\\agent\\llama\\convert``）：
- ``convert_hf_to_gguf.py``
- ``conversion/``（全部 .py，架构适配）
- ``gguf-py/gguf/``（本地 gguf 包；转换脚本会自动把它插到 sys.path，优先于 pip 版）

用法::

    E:\\agent\\venv312\\Scripts\\python.exe tools/soul_serve/fetch_convert_src.py
    # 默认 ref = 701fd25b3（对应 llama-server build b11857，见 llama-server.exe --version）
    # 换 llama.cpp 版本时把 --ref 改成新 commit 即可
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[2]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from agi.paths import agent_path


DEFAULT_REF = "701fd25b3"  # llama-server build 11857 的 commit
PROXY = "https://gh-proxy.com/"
RAW = PROXY + "https://raw.githubusercontent.com/ggml-org/llama.cpp/{ref}/"
API = PROXY + "https://api.github.com/repos/ggml-org/llama.cpp/contents/"


def curl(url: str, out: Path, retries: int = 5) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    code = 0
    for attempt in range(retries):
        proc = subprocess.run(
            ["curl", "-k", "-L", "--max-time", "180", "-s", "-o", str(out),
             "-w", "%{http_code}", url],
            capture_output=True, text=True,
        )
        try:
            code = int(proc.stdout.strip() or "0")
        except ValueError:
            code = 0
        if code == 200 and out.exists() and out.stat().st_size > 0:
            return code
        out.unlink(missing_ok=True)
        time.sleep(0.5 * (attempt + 1))
    return code


def list_dir(path: str, ref: str, dest: Path) -> list[dict]:
    tmp = dest / "_listing.json"
    code = curl(API + path + f"?ref={ref}", tmp)
    if code != 200:
        raise SystemExit(f"[fetch] listing {path} failed: http {code}")
    data = json.loads(tmp.read_text(encoding="utf-8"))
    tmp.unlink(missing_ok=True)
    return [item for item in data if item.get("type") == "file"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fetch llama.cpp converter sources at a commit")
    parser.add_argument("--dest", default=str(agent_path(r"llama\convert")))
    parser.add_argument("--ref", default=DEFAULT_REF,
                        help=f"git commit（默认 {DEFAULT_REF}，对应 build 11857；见 llama-server --version）")
    args = parser.parse_args(argv)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    raw = RAW.format(ref=args.ref)
    failed: list[str] = []

    for remote_dir, local_dir in (("conversion", "conversion"), ("gguf-py/gguf", "gguf-py/gguf")):
        files = list_dir(remote_dir, args.ref, dest)
        print(f"[fetch] {remote_dir}: {len(files)} files")
        for item in files:
            name = str(item["name"])
            if not (name.endswith(".py") or name.endswith(".json")):
                continue
            out = dest / local_dir / name
            code = curl(raw + remote_dir + "/" + name, out)
            print(f"  {name}: http={code} size={out.stat().st_size if out.exists() else 0}")
            if code != 200:
                failed.append(f"{remote_dir}/{name}")
    code = curl(raw + "convert_hf_to_gguf.py", dest / "convert_hf_to_gguf.py")
    print(f"[fetch] convert_hf_to_gguf.py http={code}")
    if code != 200:
        failed.append("convert_hf_to_gguf.py")
    if failed:
        print("[fetch] FAILED:", failed)
        return 1
    print(f"[fetch] done -> {dest} (ref {args.ref})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
