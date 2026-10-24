# -*- coding: utf-8 -*-
"""把 peft LoRA adapter 合并进 base HF 模型，输出可直接转 GGUF 的 HF 目录。

用法（在仓库根目录）::

    E:\\agent\\venv312\\Scripts\\python.exe tools/soul_serve/merge_adapter.py ^
        --base E:\\agent\\models\\Qwen2.5-0.5B-Instruct ^
        --adapter E:\\agent\\output\\soul_qwen25_05b_v0 ^
        --out E:\\agent\\output\\merged_soul_qwen25_05b_v0

说明：
- 默认 ``--device cpu``：merge 只做权重加法，CPU 即可，不占 GPU（训练还在跑时也能用）；
- 输出是标准 HF 目录（config/tokenizer/*.safetensors），下一步 convert.py 用它转 GGUF。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="merge peft adapter into base HF model")
    parser.add_argument("--base", required=True, help="base HF 模型目录")
    parser.add_argument("--adapter", required=True, help="peft adapter 目录（adapter_config.json）")
    parser.add_argument("--out", required=True, help="输出合并后 HF 目录")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"],
                        help="cpu 不占 GPU（默认）；cuda 更快但会占显存")
    parser.add_argument("--dtype", default="float16", choices=["float16", "bfloat16", "float32"])
    args = parser.parse_args(argv)

    if args.device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""  # 明确不初始化 CUDA

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    dtype = {"float16": torch.float16, "bfloat16": torch.bfloat16,
             "float32": torch.float32}[args.dtype]
    base_dir = Path(args.base)
    adapter_dir = Path(args.adapter)
    out_dir = Path(args.out)
    if not (adapter_dir / "adapter_config.json").exists():
        raise SystemExit(f"[merge] adapter 目录里没有 adapter_config.json: {adapter_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    print(f"[merge] base={base_dir}")
    print(f"[merge] adapter={adapter_dir}")
    print(f"[merge] device={args.device} dtype={args.dtype}")
    tokenizer = AutoTokenizer.from_pretrained(str(base_dir), trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        str(base_dir), torch_dtype=dtype, device_map=None,
        low_cpu_mem_usage=True, trust_remote_code=True,
    )
    if args.device == "cuda":
        model = model.to("cuda")
    else:
        model = model.to("cpu")
    model = PeftModel.from_pretrained(model, str(adapter_dir), is_trainable=False)
    merged = model.merge_and_unload()
    merged = merged.to("cpu")
    merged.save_pretrained(str(out_dir), safe_serialization=True, max_shard_size="2GB")
    tokenizer.save_pretrained(str(out_dir))
    # 部分 Qwen 目录带 generation_config / configuration.json，尽力复制
    for extra in ("generation_config.json", "configuration.json"):
        src = base_dir / extra
        if src.exists():
            (out_dir / extra).write_bytes(src.read_bytes())

    weights = sorted(out_dir.glob("*.safetensors"))
    total = sum(p.stat().st_size for p in weights)
    print(f"[merge] done in {time.time() - started:.1f}s -> {out_dir}")
    print(f"[merge] safetensors: {[p.name for p in weights]} total={total / 1024 ** 3:.2f}GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
