# -*- coding: utf-8 -*-
"""P2-b 线 B：语气改写 LoRA 训练（input=中性书面 + 上下文 → output=用户真实语气）。

数据：`style_sft_ai.jsonl`（tone_style_data.py --full 生成，input=AI 人格口吻），每行
`{id, channel, split, context:[{role,content}], neutral, original, same_meaning}`。

训练：prompt = context + user(neutral)，target = original（用户真实发言）；AI input = ai_input。
复用 `tools/train/smoke_lora.py` 的模型加载 / LoRA / collate / 训练循环。

用法::

    E:\\agent\\venv312\\Scripts\\python.exe agi/eval_data/tone_style_train.py ^
        --epochs 3 --out E:\\agent\\output\\tone_style_qwen05_v1
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_PATH = ROOT / "agi" / "eval_data" / "human_tone" / "style_sft_ai.jsonl"
if str(ROOT / "agi" / "eval_data") not in sys.path:
    sys.path.insert(0, str(ROOT / "agi" / "eval_data"))
from style_prompt import build_messages, extract_anchors, prompt_fingerprint  # noqa: E402
DEFAULT_MODEL = Path(r"E:\agent\models\Qwen2.5-0.5B-Instruct")
DEFAULT_OUT = Path(r"E:\agent\output\tone_style_qwen05_v1")


def load_rows(path: Path = DATA_PATH) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def encode_samples(tokenizer, rows: list[dict], max_len: int) -> list[dict]:
    from tools.train import smoke_lora

    encoded: list[dict] = []
    for row in rows:
        # 训练与推理必须用同一个 prompt 构造器；anchors 从 AI input 动态抽取
        context_text = " ".join(str(m.get("content") or "") for m in row["context"])
        anchors = extract_anchors(row["ai_input"], context_text)
        messages = build_messages(row["context"], row["ai_input"], anchors)
        enc = smoke_lora.encode_sample(tokenizer, messages, row["original"],
                                       max_len=max_len)
        enc["id"] = row["id"]
        encoded.append(enc)
    return encoded


def train(args: argparse.Namespace) -> dict:
    from types import SimpleNamespace

    from tools.train import smoke_lora

    rows = load_rows(Path(args.data))
    train_rows = [row for row in rows if (row.get("split") or "train") == "train"]
    val_rows = [row for row in rows if row.get("split") == "val"]
    if args.limit:
        train_rows = train_rows[: int(args.limit)]
    if not train_rows:
        raise SystemExit("[style_train] 没有 train 样本")
    print(f"[style_train] train={len(train_rows)} val={len(val_rows)} "
          f"channels={dict(Counter(r.get('channel') for r in train_rows))}", flush=True)

    model, tokenizer, _ = smoke_lora.load_model_and_tokenizer(
        str(args.model_path), load_4bit=False, dtype_name=args.dtype)
    model = smoke_lora.setup_peft(model, r=args.lora_r, alpha=args.lora_alpha,
                                  dropout=args.lora_dropout, load_4bit=False)
    encoded = encode_samples(tokenizer, train_rows, max_len=args.max_seq_len)
    train_args = SimpleNamespace(
        dtype=args.dtype, lr=args.lr, seed=args.seed,
        sort_chunk=64, batch_size=args.batch_size, grad_accum=args.grad_accum,
        epochs=args.epochs, max_steps=args.max_steps, max_seq_len=args.max_seq_len,
        pad_bucket=64, save_every=0, save_adapter=str(args.out),
        empty_cache_every=0, load_4bit=False, grad_checkpoint=True,
        lora_r=args.lora_r, lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout, model_path=str(args.model_path),
    )
    result = smoke_lora.run_train(model, encoded, tokenizer, train_args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(out))
    tokenizer.save_pretrained(str(out))
    metrics = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "task": "tone_style_transfer",
        "data": str(args.data),
        "train_pairs": len(train_rows),
        "val_pairs": len(val_rows),
        "model_path": str(args.model_path),
        "prompt_version": prompt_fingerprint(),
        "out": str(out),
        "config": {"epochs": args.epochs, "max_steps": args.max_steps,
                   "lr": args.lr, "batch_size": args.batch_size,
                   "grad_accum": args.grad_accum, "max_seq_len": args.max_seq_len,
                   "lora_r": args.lora_r, "lora_alpha": args.lora_alpha,
                   "dtype": args.dtype, "seed": args.seed},
        **result["summary"],
    }
    (out / "train_metrics.json").write_text(
        json.dumps({"summary": metrics, "steps": result["records"]},
                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("[style_train] " + json.dumps(metrics, ensure_ascii=False), flush=True)
    return metrics


def encode_meta(tokenizer, path: Path, max_len: int) -> dict:
    rows = load_rows(path)
    encoded = encode_samples(tokenizer, rows, max_len=max_len)
    lengths = [len(item["input_ids"]) for item in encoded]
    return {"rows": len(rows), "max_tokens": max(lengths),
            "mean_tokens": round(sum(lengths) / max(1, len(lengths)), 1)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="语气改写 LoRA 训练")
    parser.add_argument("--data", default=str(DATA_PATH))
    parser.add_argument("--model-path", default=str(DEFAULT_MODEL))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--dry-run", action="store_true", help="只 encode 看长度，不训练")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--max-steps", type=int, default=0)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--grad-accum", type=int, default=1)
    parser.add_argument("--max-seq-len", type=int, default=384)
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=32)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument("--dtype", choices=("bf16", "fp16", "fp32"), default="bf16")
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run:
        from tools.train import smoke_lora
        _model, tokenizer, _ = smoke_lora.load_model_and_tokenizer(
            str(args.model_path), load_4bit=False, dtype_name=args.dtype)
        print(json.dumps(encode_meta(tokenizer, Path(args.data),
                                     args.max_seq_len), ensure_ascii=False))
        return 0
    train(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
