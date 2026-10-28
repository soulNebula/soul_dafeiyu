# -*- coding: utf-8 -*-
"""通用 SFT/FFT 训练器：messages / instruction / soul 三种聊天记录格式。

设计目标
--------
- 任意聊天记录都能训练：``messages`` / ``instruction`` / ``soul``（--format auto 按行探测）；
- 默认 LoRA（peft），``--full-ft`` 全参数微调（bf16 + gradient_checkpointing +
  默认 batch1/grad-accum 8），``--load-4bit`` 走 QLoRA；
- ``--dry-run`` 只读数据 + 分词长度分布（不加载模型）；
- 每步输出 ``[sft] STEP {...}``（step/loss/lr/peak_vram_mb/seconds），结束输出
  ``[sft] SUMMARY {...}``，同时可 append 到 jsonl 供工作台解析；
- OOM 给出明确降级建议，绝不静默成功。

边界：只新增本文件；数据解析/分词/加载复用 ``tools/train/smoke_lora.py`` 的函数
（不修改它）；不依赖 agi.* 以外的业务模块。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agi.paths import agent_home  # noqa: E402
from tools.train import smoke_lora  # noqa: E402

HOME_DIR = Path(os.environ.get("SOUL_TRAIN_HOME") or str(agent_home()))
DEFAULT_DATA = ROOT / "agi" / "soul" / "data" / "soul_sft_v0.jsonl"
DEFAULT_MODEL_PATH = HOME_DIR / "models" / "Qwen2.5-0.5B-Instruct"
DEFAULT_ADAPTER = HOME_DIR / "output" / "sft_lora"
DEFAULT_FULL_OUT = HOME_DIR / "output" / "sft_full"
DEFAULT_LOG = HOME_DIR / "logs" / "sft_train.jsonl"
FORMATS = ("auto", "messages", "soul", "instruction")
GENERIC_SYSTEM = "你是一个乐于助人的 AI 助手，回答简洁、准确、自然。"
OOM_HINTS = (
    "降级建议：--max-seq-len 256/128；--batch-size 1 保持；--load-4bit（QLoRA）；"
    "换更小模型（Qwen2.5-0.5B-Instruct）；或改用 LoRA 而不是 --full-ft"
)


# --------------------------------------------------------------------------- #
# 数据格式
# --------------------------------------------------------------------------- #
def detect_item_format(item: dict) -> str:
    """按单行内容探测格式：messages / instruction / soul / ""（未知）。"""
    if not isinstance(item, dict):
        return ""
    messages = item.get("messages")
    if isinstance(messages, list) and messages:
        return "messages"
    if item.get("instruction") is not None and (
            item.get("output") is not None or item.get("response") is not None):
        return "instruction"
    if isinstance(item.get("context"), list) and item.get("target") is not None:
        return "soul"
    return ""


def _text(value) -> str:
    return str(value or "").strip()


def _messages_sample(item: dict) -> tuple[list[dict], str]:
    raw = item.get("messages") or []
    messages: list[dict] = []
    for msg in raw:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        if role not in ("system", "user", "assistant"):
            continue
        content = _text(msg.get("content") if "content" in msg else msg.get("text"))
        if content:
            messages.append({"role": role, "content": content})
    if not messages:
        raise ValueError("messages 为空或没有合法 role")
    last_assistant = -1
    for index, msg in enumerate(messages):
        if msg["role"] == "assistant":
            last_assistant = index
    if last_assistant < 0:
        raise ValueError("messages 里没有 assistant")
    answer = messages[last_assistant]["content"]
    prompt = messages[:last_assistant]
    if not prompt:
        raise ValueError("assistant 前没有 user/system")
    if prompt[-1]["role"] == "assistant":
        raise ValueError("prompt 以 assistant 结尾")
    return prompt, answer


def _instruction_sample(item: dict) -> tuple[list[dict], str]:
    instruction = _text(item.get("instruction"))
    input_text = _text(item.get("input"))
    output = _text(item.get("output") if item.get("output") is not None
                   else item.get("response"))
    if not instruction:
        raise ValueError("instruction 为空")
    if not output:
        raise ValueError("output 为空")
    user = instruction + (("\n" + input_text) if input_text else "")
    system = _text(item.get("system")) or GENERIC_SYSTEM
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}], output


def _soul_sample(item: dict) -> tuple[list[dict], str]:
    try:
        messages, answer = smoke_lora.build_messages(item)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"soul 样本非法：{type(exc).__name__}: {exc}") from exc
    if not messages or not answer:
        raise ValueError("soul 样本缺 messages/answer")
    return messages, answer


def _build_one(item: dict, fmt: str) -> tuple[list[dict], str, str]:
    actual = detect_item_format(item) if fmt == "auto" else fmt
    if actual == "messages":
        messages, answer = _messages_sample(item)
    elif actual == "instruction":
        messages, answer = _instruction_sample(item)
    elif actual == "soul":
        messages, answer = _soul_sample(item)
    else:
        raise ValueError("unknown_format")
    return messages, answer, actual


def build_samples(items: list[dict], fmt: str = "auto") -> dict:
    """把原始行变成训练样本；坏行跳过并计数，不抛整批错误。

    返回 ``{samples, skipped, formats, format}``：
    - samples: [{id, format, messages(不含 assistant), answer, source}]
    - skipped: [{index, reason}]
    - formats: 各格式计数；format: 强制格式或 ``mixed``/唯一格式
    """
    fmt = str(fmt or "auto").strip().lower()
    if fmt not in FORMATS:
        raise ValueError(f"未知 format：{fmt}（可选 {FORMATS}）")
    samples: list[dict] = []
    skipped: list[dict] = []
    formats: dict[str, int] = {}
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            skipped.append({"index": index, "reason": "not_object"})
            continue
        try:
            messages, answer, actual = _build_one(item, fmt)
        except ValueError as exc:
            skipped.append({"index": index, "reason": str(exc)[:160]})
            continue
        formats[actual] = formats.get(actual, 0) + 1
        samples.append({
            "id": item.get("id") or f"row-{index}",
            "format": actual,
            "messages": messages,
            "answer": answer,
            "source": _text(item.get("source")),
        })
    if fmt == "auto":
        label = next(iter(formats)) if len(formats) == 1 else (
            "mixed" if formats else "")
    else:
        label = fmt
    return {"samples": samples, "skipped": skipped,
            "formats": formats, "format": label}


# --------------------------------------------------------------------------- #
# 分词/统计（dry-run 用）
# --------------------------------------------------------------------------- #
def _load_tokenizer(model_path: str):
    from transformers import AutoTokenizer  # noqa: E402

    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    if getattr(tokenizer, "pad_token", None) is None:
        tokenizer.pad_token = tokenizer.eos_token
    return tokenizer


def encode_samples(samples: list[dict], tokenizer, max_len: int) -> dict:
    encoded: list[dict] = []
    errors: list[dict] = []
    for index, sample in enumerate(samples):
        try:
            enc = smoke_lora.encode_sample(
                tokenizer, sample["messages"], sample["answer"],
                max_len=max_len)
        except Exception as exc:  # noqa: BLE001
            errors.append({"index": index,
                           "reason": f"{type(exc).__name__}: {exc}"[:160]})
            continue
        enc["id"] = sample.get("id")
        enc["format"] = sample.get("format")
        encoded.append(enc)
    lengths = [len(item["input_ids"]) for item in encoded]
    truncated = sum(1 for item in encoded
                    if len(item["input_ids"]) >= max_len)
    return {"encoded": encoded, "errors": errors, "lengths": lengths,
            "truncated": truncated}


def data_stats(items: list[dict], fmt: str = "auto", tokenizer=None,
               max_len: int = 512, tokenizer_factory=None,
               model_path: str | None = None) -> dict:
    """dry-run 统计：行数/格式/长度分布/跳过/截断。不加载模型。"""
    built = build_samples(items, fmt)
    if not built["samples"]:
        return {"ok": False, "error": "no_valid_samples",
                "skipped": built["skipped"], "formats": built["formats"],
                "format": built["format"]}
    tok = tokenizer
    if tok is None:
        factory = tokenizer_factory or _load_tokenizer
        try:
            tok = factory(str(model_path or DEFAULT_MODEL_PATH))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"tokenizer_unavailable: {exc}"}
    encoded = encode_samples(built["samples"], tok, max_len)
    lengths = encoded["lengths"]
    if not lengths:
        return {"ok": False, "error": "all_samples_truncated",
                "skipped": built["skipped"] + encoded["errors"],
                "formats": built["formats"], "format": built["format"]}
    if encoded["truncated"] >= len(lengths):
        return {"ok": False, "error": "all_samples_truncated",
                "rows": len(built["samples"]), "format": built["format"],
                "formats": built["formats"], "skipped": len(built["skipped"]),
                "len_min": min(lengths), "len_max": max(lengths),
                "len_mean": round(sum(lengths) / len(lengths), 1),
                "truncated": encoded["truncated"], "max_seq_len": max_len}
    stats = {
        "ok": True, "rows": len(built["samples"]),
        "format": built["format"], "formats": built["formats"],
        "skipped": len(built["skipped"]),
        "len_min": min(lengths), "len_max": max(lengths),
        "len_mean": round(sum(lengths) / len(lengths), 1),
        "truncated": encoded["truncated"],
        "max_seq_len": max_len,
    }
    return stats


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="通用 SFT/FFT 训练器（messages/instruction/soul）")
    p.add_argument("--data", default=str(DEFAULT_DATA), help="jsonl 数据")
    p.add_argument("--model-path", default=str(DEFAULT_MODEL_PATH),
                   help="本地 HF 模型目录（真实训练用 0.5B）")
    p.add_argument("--format", choices=FORMATS, default="auto",
                   help="数据格式（auto 按行探测）")
    p.add_argument("--save-adapter", default=str(DEFAULT_ADAPTER),
                   help="LoRA adapter 输出目录（默认 %(default)s）")
    p.add_argument("--out", default="", help="--full-ft 时输出完整模型目录")
    p.add_argument("--max-steps", type=int, default=0, help="优化步数上限（0=按 epochs）")
    p.add_argument("--epochs", type=int, default=1)
    p.add_argument("--lr", type=float, default=2e-4,
                   help="学习率（FFT 建议 1e-5）")
    p.add_argument("--lora-r", type=int, default=16)
    p.add_argument("--lora-alpha", type=int, default=32)
    p.add_argument("--lora-dropout", type=float, default=0.05)
    p.add_argument("--max-seq-len", type=int, default=512)
    p.add_argument("--batch-size", type=int, default=1)
    p.add_argument("--grad-accum", type=int, default=0,
                   help="梯度累积（0=LoRA 1 / FFT 8）")
    p.add_argument("--load-4bit", action="store_true", help="QLoRA（4bit + LoRA）")
    p.add_argument("--full-ft", action="store_true",
                   help="全参数微调（bf16 + gradient checkpointing）")
    p.add_argument("--dtype", choices=("bf16", "fp16", "fp32"), default="bf16")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--limit", type=int, default=0, help="最多读多少行（0=全部）")
    p.add_argument("--log-jsonl", default=str(DEFAULT_LOG),
                   help="每步/SUMMARY 追加写入的 jsonl（默认 %(default)s）")
    p.add_argument("--dry-run", action="store_true",
                   help="只统计数据+分词长度，不加载模型")
    p.add_argument("--pad-bucket", type=int, default=32)
    p.add_argument("--sort-chunk", type=int, default=128)
    return p


def validate_args(args: argparse.Namespace) -> str:
    """参数组合校验；返回错误字符串（空串=OK）。测试直接复用。"""
    if args.full_ft and args.load_4bit:
        return "full_ft 与 load_4bit 冲突：QLoRA 只能用于 LoRA，不能全参微调"
    if args.format not in FORMATS:
        return f"format 非法：{args.format}"
    for name in ("max_seq_len", "batch_size", "grad_accum", "lora_r"):
        value = int(getattr(args, name))
        if value <= 0 and not (name == "grad_accum" and value == 0):
            return f"{name} 必须 > 0"
    if args.epochs <= 0:
        return "epochs 必须 > 0"
    if args.max_steps < 0:
        return "max_steps 不能为负"
    if args.full_ft:
        if args.dtype != "bf16":
            args.dtype = "bf16"          # FFT 强制 bf16
        if not args.out:
            args.out = str(DEFAULT_FULL_OUT)
        if args.grad_accum == 0:
            args.grad_accum = 8
    elif not str(args.save_adapter or "").strip():
        return "LoRA 模式需要 --save-adapter"
    if args.grad_accum == 0:
        args.grad_accum = 1
    return ""


def load_items(path: Path, limit: int = 0) -> tuple[list[dict], int]:
    items: list[dict] = []
    bad = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(item, dict):
                items.append(item)
            else:
                bad += 1
            if limit and len(items) >= limit:
                break
    return items, bad


# --------------------------------------------------------------------------- #
# 训练
# --------------------------------------------------------------------------- #
def gb(value: int) -> float:
    return round(value / 1024 ** 3, 3)


def load_model(args):
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA 不可用：检查驱动 / venv312 是否 CPU 版 torch")
    model, tokenizer, config = smoke_lora.load_model_and_tokenizer(
        args.model_path, load_4bit=bool(args.load_4bit), dtype_name=args.dtype)
    if args.full_ft:
        try:
            model.gradient_checkpointing_enable(
                gradient_checkpointing_kwargs={"use_reentrant": False})
        except TypeError:
            model.gradient_checkpointing_enable()
        if hasattr(model, "enable_input_require_grads"):
            model.enable_input_require_grads()
        print("[sft] full-ft: bf16 + gradient_checkpointing", flush=True)
    else:
        model = smoke_lora.setup_peft(
            model, r=args.lora_r, alpha=args.lora_alpha,
            dropout=args.lora_dropout, load_4bit=bool(args.load_4bit))
        print(f"[sft] LoRA r={args.lora_r} alpha={args.lora_alpha}", flush=True)
    return model, tokenizer


def train_model(model, tokenizer, encoded: list[dict], args) -> dict:
    import torch

    device = model.get_input_embeddings().weight.device
    trainable = [param for param in model.parameters() if param.requires_grad]
    n_trainable = sum(param.numel() for param in trainable)
    optimizer = torch.optim.AdamW(trainable, lr=args.lr, weight_decay=0.0,
                                  betas=(0.9, 0.95))
    model.train()
    torch.cuda.reset_peak_memory_stats()
    samples = list(encoded)
    random.Random(args.seed).shuffle(samples)
    chunk = max(1, int(args.sort_chunk))
    for index in range(0, len(samples), chunk):
        samples[index:index + chunk] = sorted(
            samples[index:index + chunk], key=lambda item: len(item["input_ids"]))
    batch_size = max(1, args.batch_size)
    grad_accum = max(1, args.grad_accum)
    batches = [samples[i:i + batch_size]
               for i in range(0, len(samples), batch_size)]
    if not batches:
        raise RuntimeError("没有可训练的 batch（数据为空）")
    steps_per_epoch = max(1, len(batches) // grad_accum)
    wanted = steps_per_epoch * max(1, args.epochs)
    steps = min(args.max_steps, wanted) if args.max_steps > 0 else wanted
    steps = max(1, steps)
    records: list[dict] = []
    losses: list[float] = []
    log_path = Path(args.log_jsonl) if args.log_jsonl else None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    for step in range(steps):
        micro_batches = [batches[(step * grad_accum + offset) % len(batches)]
                         for offset in range(grad_accum)]
        step_started = time.perf_counter()
        optimizer.zero_grad(set_to_none=True)
        loss_sum = 0.0
        for micro in micro_batches:
            batch = smoke_lora.collate(micro, tokenizer.pad_token_id,
                                       bucket=int(args.pad_bucket))
            batch = {key: value.to(device) for key, value in batch.items()}
            with torch.autocast("cuda", dtype=torch.bfloat16,
                                enabled=(args.dtype == "bf16")):
                output = model(**batch)
                loss = output.loss / grad_accum
            loss.backward()
            loss_sum += float(output.loss.detach().float().item())
        torch.nn.utils.clip_grad_norm_(trainable, 1.0)
        optimizer.step()
        if device.type == "cuda":
            torch.cuda.synchronize()
        seconds = time.perf_counter() - step_started
        item_loss = round(loss_sum / grad_accum, 4)
        losses.append(item_loss)
        record = {
            "step": step + 1,
            "loss": item_loss,
            "lr": args.lr,
            "peak_vram_mb": round(torch.cuda.max_memory_reserved() / 1024 ** 2, 1),
            "peak_alloc_mb": round(torch.cuda.max_memory_allocated() / 1024 ** 2, 1),
            "seconds": round(seconds, 3),
        }
        records.append(record)
        print("[sft] STEP " + json.dumps(record, ensure_ascii=False), flush=True)
        if log_path:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "step", **record},
                                        ensure_ascii=False) + "\n")
    total = time.perf_counter() - started
    summary = {
        "ok": True,
        "mode": "full_ft" if args.full_ft else "lora",
        "steps": steps,
        "epochs": args.epochs,
        "loss_first": losses[0] if losses else None,
        "loss_last": losses[-1] if losses else None,
        "loss_mean": round(sum(losses) / len(losses), 4),
        "total_seconds": round(total, 2),
        "seconds_per_step": round(total / max(1, steps), 3),
        "peak_vram_mb": round(torch.cuda.max_memory_reserved() / 1024 ** 2, 1),
        "peak_alloc_gb": gb(torch.cuda.max_memory_allocated()),
        "trainable_params_m": round(n_trainable / 1e6, 2),
        "model": args.model_path,
        "dtype": args.dtype,
        "max_seq_len": args.max_seq_len,
        "batch_size": args.batch_size,
        "grad_accum": args.grad_accum,
        "load_4bit": bool(args.load_4bit),
    }
    return {"summary": summary, "records": records}


def save_outputs(model, tokenizer, args) -> str:
    if args.full_ft:
        target = Path(args.out)
        target.mkdir(parents=True, exist_ok=True)
        model.save_pretrained(str(target))
        tokenizer.save_pretrained(str(target))
        return str(target)
    target = Path(args.save_adapter)
    target.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(target))
    return str(target)


def _run_dry(args) -> int:
    path = Path(args.data)
    if not path.exists():
        print(f"[sft] 找不到数据集：{path}", file=sys.stderr)
        return 2
    items, bad_lines = load_items(path, args.limit)
    if not items:
        print("[sft] 数据集为空（或全部坏行）", file=sys.stderr)
        return 2
    stats = data_stats(items, fmt=args.format, max_len=args.max_seq_len,
                       model_path=args.model_path)
    if not stats.get("ok"):
        print("[sft] DATA " + json.dumps(stats, ensure_ascii=False), flush=True)
        print(f"[sft] 错误：{stats.get('error')}", file=sys.stderr)
        return 2
    stats["bad_lines"] = bad_lines
    print("[sft] DATA " + json.dumps(stats, ensure_ascii=False), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    error = validate_args(args)
    if error:
        print(f"[sft] 参数错误：{error}", file=sys.stderr)
        return 2
    if args.dry_run:
        return _run_dry(args)
    data_path = Path(args.data)
    if not data_path.exists():
        print(f"[sft] 找不到数据集：{data_path}", file=sys.stderr)
        return 2
    model_path = Path(args.model_path)
    if not (model_path / "config.json").exists():
        print(f"[sft] 模型目录缺 config.json：{model_path}", file=sys.stderr)
        return 2
    items, bad_lines = load_items(data_path, args.limit)
    if not items:
        print("[sft] 数据集为空（或全部坏行）", file=sys.stderr)
        return 2
    built = build_samples(items, args.format)
    if not built["samples"]:
        print("[sft] 没有合法训练样本：" + json.dumps(built["skipped"][:5],
                                                     ensure_ascii=False),
              file=sys.stderr)
        return 2
    print(f"[sft] data rows={len(items)} valid={len(built['samples'])} "
          f"skipped={len(built['skipped'])} format={built['format']}",
          flush=True)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("MODELSCOPE_CACHE", str(HOME_DIR / "cache" / "modelscope"))
    os.environ.setdefault("HF_HOME", str(HOME_DIR / "cache" / "hf"))
    import torch  # noqa: E402
    print(f"[sft] torch={torch.__version__} cuda={torch.version.cuda}", flush=True)
    if not torch.cuda.is_available():
        print("[sft] CUDA 不可用：请检查驱动 / 是否装成 CPU 版 torch", file=sys.stderr)
        return 3
    print(f"[sft] gpu={torch.cuda.get_device_name(0)} "
          f"vram={gb(torch.cuda.get_device_properties(0).total_memory)}GB",
          flush=True)
    try:
        model, tokenizer = load_model(args)
        encoded = encode_samples(built["samples"], tokenizer, args.max_seq_len)
        if not encoded["encoded"]:
            print("[sft] 所有样本都无法编码（全被截断）：" +
                  json.dumps(encoded["errors"][:5], ensure_ascii=False),
                  file=sys.stderr)
            return 2
        result = train_model(model, tokenizer, encoded["encoded"], args)
        saved = save_outputs(model, tokenizer, args)
    except Exception as exc:  # noqa: BLE001 - 明确错误，不静默
        message = str(exc)
        if isinstance(exc, torch.cuda.OutOfMemoryError) or "out of memory" in message.lower():
            try:
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            print(f"[sft] OOM: {type(exc).__name__}: {message}", file=sys.stderr)
            print("[sft] " + OOM_HINTS, file=sys.stderr)
            return 4
        print(f"[sft] 训练失败：{type(exc).__name__}: {message}", file=sys.stderr)
        return 1
    summary = result["summary"]
    summary["output"] = saved
    summary["truncated"] = encoded["truncated"]
    summary["skipped"] = len(built["skipped"]) + len(encoded["errors"])
    if summary["peak_vram_mb"] > 5800:
        print("[sft] 警告：peak_vram 接近 6GB 上限，可能 spill；建议降 seq/用 4bit",
              flush=True)
    print("[sft] SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    if args.log_jsonl:
        log_path = Path(args.log_jsonl)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "summary", **summary},
                                    ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
