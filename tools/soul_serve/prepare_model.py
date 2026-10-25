# -*- coding: utf-8 -*-
"""任意 PEFT adapter → 可服务 GGUF 的一键流水线（P3-c 预留接口）。

流程：
1. `tools/soul_serve/merge_adapter.py`：adapter + base HF → 合并后的 HF 目录（CPU merge）；
2. `tools/soul_serve/convert.py`：HF → f16 GGUF → Q4_K_M GGUF（llama.cpp convert + quantize）；
3. 可选 `--register`：写进 `agi/data/local_models.json`，以后可直接被 registry 启停。

**默认只验证 dry-run**，不真正合并/转换；用户要求 1.5B 先不删、预留接口，本任务不跑 1.5B。

用法::

    # 只打印计划（本任务只验证这个）
    E:\\agent\\venv312\\Scripts\\python.exe tools/soul_serve/prepare_model.py ^
        --adapter E:\\agent\\output\\tone_style_qwen15_v3 ^
        --base E:\\agent\\models\\Qwen2.5-1.5B-Instruct ^
        --name tone_style_qwen15_v3 --dry-run

    # 真跑（以后新任务用；会占 CPU/磁盘，不占训练 GPU）
    ... prepare_model.py --adapter ... --base ... --name my_model --out-dir E:\\agent\\output\\gguf
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOUL_SERVE = ROOT / "tools" / "soul_serve"
MERGE_SCRIPT = SOUL_SERVE / "merge_adapter.py"
CONVERT_SCRIPT = SOUL_SERVE / "convert.py"
DEFAULT_OUT_DIR = Path(r"E:\agent\output\gguf")
DEFAULT_LLAMA_BIN = Path(r"E:\agent\llama\cuda")
DEFAULT_CONVERT_DIR = Path(r"E:\agent\llama\convert")


def build_plan(adapter: Path, base: Path, name: str, out_dir: Path,
               merged_dir: Path, quant: str) -> dict:
    python = sys.executable
    merge_cmd = [python, str(MERGE_SCRIPT), "--base", str(base),
                 "--adapter", str(adapter), "--out", str(merged_dir)]
    convert_cmd = [python, str(CONVERT_SCRIPT), "--hf-dir", str(merged_dir),
                   "--name", name, "--out-dir", str(out_dir), "--quant", quant]
    checks = {
        "adapter_exists": adapter.exists(),
        "adapter_config": (adapter / "adapter_config.json").exists(),
        "base_exists": base.exists(),
        "merge_script": MERGE_SCRIPT.exists(),
        "convert_script": CONVERT_SCRIPT.exists(),
        "convert_hf_script": (DEFAULT_CONVERT_DIR / "convert_hf_to_gguf.py").exists(),
        "llama_quantize": (DEFAULT_LLAMA_BIN / "llama-quantize.exe").exists(),
    }
    return {"name": name, "dry_run": None,
            "steps": [
                {"step": "merge", "command": merge_cmd,
                 "output": str(merged_dir)},
                {"step": "convert+quantize", "command": convert_cmd,
                 "output": str(out_dir / f"{name}-{quant.lower()}.gguf")},
            ],
            "checks": checks,
            "register": {"config": str(ROOT / "agi/data/local_models.json"),
                         "adapter": str(adapter), "base": str(base),
                         "gguf": str(out_dir / f"{name}-{quant.lower()}.gguf")}}


def run_plan(plan: dict) -> dict:
    results = []
    for step in plan["steps"]:
        print(f"[prepare] $ {' '.join(step['command'])}", flush=True)
        proc = subprocess.run(step["command"])
        results.append({"step": step["step"], "returncode": proc.returncode})
        if proc.returncode != 0:
            raise SystemExit(f"[prepare] 步骤失败: {step['step']} (exit {proc.returncode})")
    return {"ok": True, "steps": results}


def register_model(name: str, adapter: Path, base: Path, gguf: Path,
                   port: int | None = None, role: str = "custom") -> dict:
    """把新模型写进注册表（不覆盖同名；端口可选）。"""
    from agi.selflearn import model_registry

    config = model_registry.load_config()
    if any(item.get("name") == name for item in config["models"]):
        return {"ok": False, "error": "duplicate_name", "name": name}
    config["models"].append({
        "name": name, "base": str(base), "adapter": str(adapter),
        "gguf": str(gguf), "port": port, "role": role, "status": "ready",
        "purpose": "prepare_model.py 生成",
    })
    model_registry.CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"ok": True, "config": str(model_registry.CONFIG_PATH)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任意 adapter → GGUF 一键准备")
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--merged-dir", default="")
    parser.add_argument("--quant", default="Q4_K_M")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    parser.add_argument("--register", action="store_true", help="完成后写进注册表")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--role", default="custom")
    args = parser.parse_args(argv)

    adapter = Path(args.adapter)
    base = Path(args.base)
    out_dir = Path(args.out_dir)
    merged_dir = Path(args.merged_dir) if args.merged_dir else (
        Path(r"E:\agent\output") / f"merged_{args.name}")
    plan = build_plan(adapter, base, args.name, out_dir, merged_dir, args.quant)
    plan["dry_run"] = bool(args.dry_run)
    failed = [key for key, ok in plan["checks"].items() if not ok]
    if failed:
        print(json.dumps({"ok": False, "checks": plan["checks"], "failed": failed},
                         ensure_ascii=False, indent=1))
        return 2
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=1))
        print("[prepare] dry-run：只打印计划，未合并/未转换")
        return 0
    print(json.dumps(plan, ensure_ascii=False, indent=1))
    result = run_plan(plan)
    if args.register:
        result["register"] = register_model(
            args.name, adapter, base, Path(plan["steps"][1]["output"]),
            port=args.port, role=args.role)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
