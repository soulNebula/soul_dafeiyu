# -*- coding: utf-8 -*-
"""合并后的 HF 目录 → f16 GGUF → Q4_K_M GGUF（llama.cpp 官方转换 + 量化）。

前置：
- 同版本 llama.cpp 的 ``convert_hf_to_gguf.py`` 与 ``conversion/``、``gguf-py/`` 已放在
  ``E:\\agent\\llama\\convert``（用 fetch_src 按 llama-server 的 commit 拉取，避免 gguf 版本不匹配：
  b11857 = commit 701fd25b3）；
- ``E:\\agent\\venv312`` 里 torch / gguf 已安装；
- ``llama-quantize.exe`` 在 ``E:\\agent\\llama\\cuda``。

用法（在仓库根目录）::

    E:\\agent\\venv312\\Scripts\\python.exe tools/soul_serve/convert.py ^
        --hf-dir E:\\agent\\output\\merged_soul_qwen25_05b_v0 ^
        --name soul_qwen25_05b_v0 ^
        --out-dir E:\\agent\\output\\gguf

产物：``<out-dir>/<name>-f16.gguf`` 与 ``<out-dir>/<name>-q4_k_m.gguf``。
"""
from __future__ import annotations

import argparse
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


DEFAULT_LLAMA_BIN = agent_path(r"llama\cuda")
DEFAULT_CONVERT_DIR = agent_path(r"llama\convert")
DEFAULT_QUANT = "Q4_K_M"


def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("[convert] $ " + " ".join(str(part) for part in cmd), flush=True)
    started = time.time()
    proc = subprocess.run([str(part) for part in cmd], cwd=str(cwd) if cwd else None)
    if proc.returncode != 0:
        raise SystemExit(f"[convert] 命令失败（exit {proc.returncode}）: {cmd[0]}")
    print(f"[convert] ok in {time.time() - started:.1f}s", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HF dir -> f16 GGUF -> Q4_K_M GGUF")
    parser.add_argument("--hf-dir", required=True, help="合并后的 HF 目录")
    parser.add_argument("--name", required=True, help="产物文件名前缀，如 soul_qwen25_05b_v1")
    parser.add_argument("--out-dir", default=str(agent_path(r"output\gguf")), help="GGUF 输出目录")
    parser.add_argument("--llama-bin", default=str(DEFAULT_LLAMA_BIN), help="llama.cpp 二进制目录")
    parser.add_argument("--convert-dir", default=str(DEFAULT_CONVERT_DIR),
                        help="含 convert_hf_to_gguf.py / conversion/ / gguf-py/ 的目录")
    parser.add_argument("--quant", default=DEFAULT_QUANT, help="量化类型（默认 Q4_K_M）")
    parser.add_argument("--skip-quant", action="store_true", help="只转 f16，不量化")
    parser.add_argument("--outtype", default="f16", choices=["f16", "bf16", "f32"],
                        help="f16 中间产物类型（默认 f16）")
    args = parser.parse_args(argv)

    python = Path(sys.executable)
    hf_dir = Path(args.hf_dir)
    out_dir = Path(args.out_dir)
    bin_dir = Path(args.llama_bin)
    convert_dir = Path(args.convert_dir)
    convert_script = convert_dir / "convert_hf_to_gguf.py"
    quantize_bin = bin_dir / "llama-quantize.exe"
    if not hf_dir.is_dir():
        raise SystemExit(f"[convert] HF 目录不存在: {hf_dir}")
    if not convert_script.exists():
        raise SystemExit(f"[convert] 缺少转换脚本: {convert_script}（先跑 fetch_src 拉取）")
    out_dir.mkdir(parents=True, exist_ok=True)
    f16_path = out_dir / f"{args.name}-{args.outtype}.gguf"
    quant_path = out_dir / f"{args.name}-{args.quant.lower()}.gguf"

    _run([python, convert_script, str(hf_dir), "--outfile", str(f16_path),
          "--outtype", args.outtype], cwd=convert_dir)
    print(f"[convert] f16 GGUF: {f16_path} ({f16_path.stat().st_size / 1024 ** 2:.0f}MB)")

    if args.skip_quant:
        return 0
    if not quantize_bin.exists():
        raise SystemExit(f"[convert] 缺少量化工具: {quantize_bin}")
    _run([quantize_bin, str(f16_path), str(quant_path), args.quant])
    print(f"[convert] quantized: {quant_path} ({quant_path.stat().st_size / 1024 ** 2:.0f}MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
