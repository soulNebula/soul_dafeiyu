# -*- coding: utf-8 -*-
"""6GB 显存上的 LoRA SFT smoke：灵魂蒸馏数据集（context → InnerState JSON）。

用途
----
在正式训练之前，用 20 步、极小 batch 验证「模型能加载 → 前向/反向能跑 →
显存不爆 → loss 在动」，并打印每一步的耗时、loss、峰值显存，作为 6GB
（RTX 4050 Laptop）上正式训练参数的依据。

数据
----
默认读 ``agi/soul/data/soul_sft_v0.jsonl``（W4-A 产出：每条 = context + user_msg +
target(InnerState)）。训练样本构造成标准 chat：
``[system] [context 里的历史 user/assistant...] [user_msg] → assistant = target JSON``，
loss 只算 assistant 部分（prompt 的 label 置 -100）。

典型用法
--------
环境/模型/输出/日志都在 ``E:\\agent\\``（仓库外）：::

    # 不加载模型，只验证数据/分词/token 长度分布（秒级）
    python tools/train/smoke_lora.py --dry-run

    # 20 步 smoke（bf16 + LoRA r16 + seq512 + bs1 + grad checkpoint）
    python tools/train/smoke_lora.py --max-steps 20

    # 显存不够时的降级：seq 256 / 4bit（QLoRA）
    python tools/train/smoke_lora.py --max-seq-len 256 --max-steps 20
    python tools/train/smoke_lora.py --load-4bit --max-steps 20

    # 纯文本备选模型
    python tools/train/smoke_lora.py --model-path E:\\agent\\models\\Qwen3-1.7B

依赖见 ``tools/train/README.md``；venv ``E:\\agent\\venv312``。
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
DEFAULT_DATA = ROOT / "agi" / "soul" / "data" / "soul_sft_v0.jsonl"
# 环境、模型、输出、日志都放 E 盘（仓库外，避免大文件污染 git / C 盘用户目录）
HOME_DIR = Path(os.environ.get("SOUL_TRAIN_HOME", r"E:\agent"))
DEFAULT_MODEL_PATH = HOME_DIR / "models" / "Qwen3.5-2B"
DEFAULT_OUTPUT_DIR = HOME_DIR / "output"
DEFAULT_LOG_PATH = HOME_DIR / "logs" / "smoke_lora.jsonl"
DEFAULT_SYSTEM = (
    "你是微信里的一个 AI 朋友。语气平和，不讨好、不客服腔；先接住对方的话，"
    "再决定要不要追问；不列清单、不说教；有情绪或危机信号时以对方安全为先。"
)
# LoRA 目标模块：Qwen3.5 文本层的全注意力 + 线性注意力(GatedDeltaNet) + MLP 投影。
# 视觉塔是 qkv/proj/linear_fc*，不在其中；训练纯文本时另有 --keep-vision 控制是否
# 把视觉塔挪到 CPU 省显存（默认挪走）。
LORA_TARGET_REGEX = (
    r".*(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj|"
    r"in_proj_qkv|in_proj_z|in_proj_a|in_proj_b|out_proj)$"
)


# --------------------------------------------------------------------------- #
# 数据
# --------------------------------------------------------------------------- #
def load_items(path: Path, limit: int) -> list[dict]:
    items: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if limit and len(items) >= limit:
                break
    if not items:
        raise SystemExit(f"[smoke] 数据集为空：{path}")
    return items


def build_messages(item: dict) -> tuple[list[dict], str]:
    """数据集条目 → (chat messages(不含 assistant), assistant 文本)。"""
    ctx = item.get("context") or []
    system = next((str(c.get("text") or "") for c in ctx
                   if c.get("role") == "system"), DEFAULT_SYSTEM)
    messages: list[dict] = [{"role": "system", "content": system}]
    for c in ctx:
        role = c.get("role")
        if role in ("user", "assistant"):
            text = str(c.get("text") or "")
            if text:
                messages.append({"role": role, "content": text})
    user_msg = str(item.get("user_msg") or "").strip()
    if user_msg and not (messages[-1]["role"] == "user"
                         and messages[-1]["content"] == user_msg):
        messages.append({"role": "user", "content": user_msg})
    if messages[-1]["role"] != "user":
        # 兜底：assistant 结尾的样本把最后一句当作用户输入（避免空 prompt）
        messages[-1] = {"role": "user", "content": messages[-1]["content"]}
    target = item.get("target")
    if not isinstance(target, dict):
        raise ValueError(f"样本 {item.get('id')} 缺 target(InnerState dict)")
    return messages, json.dumps(target, ensure_ascii=False)


def encode_sample(tokenizer, messages: list[dict], answer: str,
                  max_len: int = 512) -> dict:
    """prompt(mask) + answer(labels) 拼接；超长时优先丢最旧的历史消息。"""
    eos_id = tokenizer.eos_token_id
    answer_ids = tokenizer(answer, add_special_tokens=False)["input_ids"]
    budget = max_len - len(answer_ids) - 1
    if budget < 16:  # target 本身过长：截断 JSON（smoke 只关心能否训练）
        answer_ids = answer_ids[: max_len - 17]
        budget = max_len - len(answer_ids) - 1

    msgs = list(messages)
    prompt_ids: list[int] = []
    while True:
        try:
            prompt_text = tokenizer.apply_chat_template(
                msgs, tokenize=False, add_generation_prompt=True,
                enable_thinking=False)  # Qwen3.5：关思考，但模板仍会留空 think 块
        except TypeError:  # 老 tokenizer 不认 enable_thinking
            prompt_text = tokenizer.apply_chat_template(
                msgs, tokenize=False, add_generation_prompt=True)
        prompt_ids = tokenizer(prompt_text, add_special_tokens=False)["input_ids"]
        if len(prompt_ids) <= budget or len(msgs) <= 2:
            break
        # 丢最旧的一条非 system 消息，再重渲染（保留 system 与最后一句 user）
        del msgs[1]
    if len(prompt_ids) > budget:  # 实在放不下：保留 system 头部 + 尾部 user
        keep_head = min(len(prompt_ids), 64)
        prompt_ids = prompt_ids[:keep_head] + prompt_ids[-(budget - keep_head):]

    input_ids = prompt_ids + answer_ids + [eos_id]
    labels = [-100] * len(prompt_ids) + answer_ids + [eos_id]
    return {
        "input_ids": input_ids,
        "labels": labels,
        "attention_mask": [1] * len(input_ids),
        "prompt_tokens": len(prompt_ids),
        "answer_tokens": len(answer_ids),
    }


def prepare_encoded(items: list[dict], tokenizer, max_len: int) -> list[dict]:
    encoded = []
    for item in items:
        messages, answer = build_messages(item)
        enc = encode_sample(tokenizer, messages, answer, max_len=max_len)
        enc["id"] = item.get("id")
        encoded.append(enc)
    return encoded


def rare_weight_for(item: dict, weight: float, rules: list[str]) -> float:
    """少数类样本加权：不复制数据、只放大 loss，避免过采样扭曲字段联合分布。

    rules 形如 ["length=normal", "advice=true"]；命中任一条返回 weight。
    """
    b = (item.get("target") or {}).get("behavior") or {}
    for rule in rules:
        if "=" not in rule:
            continue
        key, val = rule.split("=", 1)
        key, val = key.strip(), val.strip()
        if key == "advice":
            want = val.lower() in ("true", "1", "yes")
            if bool(b.get("advice")) is want:
                return weight
        elif str(b.get(key)) == val:
            return weight
    return 1.0


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #
def _is_multimodal(config) -> bool:
    archs = " ".join(getattr(config, "architectures", None) or [])
    mt = str(getattr(config, "model_type", ""))
    return ("ConditionalGeneration" in archs or "VL" in archs
            or "ImageTextToText" in archs or mt.endswith("_vl")
            or "vl" in mt or "qwen3_5" in mt)


def load_model_and_tokenizer(model_path: str, *, load_4bit: bool,
                             dtype_name: str):
    import torch
    import transformers
    from transformers import AutoConfig, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    config = AutoConfig.from_pretrained(model_path, trust_remote_code=True)

    dtype = {"bf16": torch.bfloat16, "fp16": torch.float16,
             "fp32": torch.float32}[dtype_name]
    load_kwargs: dict = {"trust_remote_code": True}
    try:
        tf_major = int(str(transformers.__version__).split(".")[0])
    except ValueError:
        tf_major = 5
    if load_4bit:
        try:
            from transformers import BitsAndBytesConfig
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(f"[smoke] --load-4bit 需要 bitsandbytes：{exc}")
        load_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )
    elif tf_major >= 5:
        load_kwargs["dtype"] = dtype
    else:
        load_kwargs["torch_dtype"] = dtype

    if _is_multimodal(config):
        candidates = [getattr(transformers, name, None) for name in
                      ("AutoModelForImageTextToText", "AutoModelForMultimodalLM",
                       "AutoModelForVision2Seq")]
        candidates = [c for c in candidates if c is not None]
        candidates.append(transformers.AutoModelForCausalLM)
    else:
        candidates = [transformers.AutoModelForCausalLM]
    last_exc: Exception | None = None
    model = None
    for cls in candidates:
        try:
            print(f"[smoke] 架构 {config.architectures} → 尝试 {cls.__name__} 加载",
                  flush=True)
            model = cls.from_pretrained(
                model_path, device_map={"": 0}, low_cpu_mem_usage=True, **load_kwargs)
            break
        except TypeError:
            # 老版本 transformers 不认 dtype=，改回 torch_dtype=
            load_kwargs.pop("dtype", None)
            load_kwargs["torch_dtype"] = dtype
            last_exc = TypeError("dtype/torch_dtype 参数不被支持")
        except Exception as exc:  # noqa: BLE001 - 逐个 AutoClass 回退
            last_exc = exc
            print(f"[smoke] {cls.__name__} 加载失败：{type(exc).__name__}: "
                  f"{str(exc)[:200]}", flush=True)
    if model is None:
        raise RuntimeError(f"所有 AutoClass 都加载失败，最后错误：{last_exc}")
    return model, tokenizer, config


def setup_peft(model, *, r: int, alpha: int, dropout: float, load_4bit: bool):
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

    if hasattr(model, "config"):
        model.config.use_cache = False
    if load_4bit:
        model = prepare_model_for_kbit_training(
            model, use_gradient_checkpointing=True)
    else:
        try:
            model.gradient_checkpointing_enable(
                gradient_checkpointing_kwargs={"use_reentrant": False})
        except TypeError:  # 旧版 peft/transformers
            model.gradient_checkpointing_enable()
        if hasattr(model, "enable_input_require_grads"):
            model.enable_input_require_grads()
    lora = LoraConfig(
        r=r,
        lora_alpha=alpha,
        lora_dropout=dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=LORA_TARGET_REGEX,
    )
    model = get_peft_model(model, lora)
    return model


# --------------------------------------------------------------------------- #
# 训练
# --------------------------------------------------------------------------- #
def collate(batch: list[dict], pad_token_id: int, bucket: int = 32):
    import torch

    max_len = max(len(x["input_ids"]) for x in batch)
    # 长度量化到 64 的桶：Windows 上 expandable_segments 不可用，变长张量会让
    # caching allocator 反复切新块、reserved 一路涨（实测 300 步涨到 11GB 并 spill
    # 到系统内存）。把 batch 长度对齐到少量固定桶后，块可被复用，峰值掉回显存内。
    bucket = max(1, int(bucket or 1))
    if bucket > 1:
        max_len = ((max_len + bucket - 1) // bucket) * bucket
    input_ids, labels, attn = [], [], []
    for x in batch:
        pad = max_len - len(x["input_ids"])
        input_ids.append(x["input_ids"] + [pad_token_id] * pad)
        labels.append(x["labels"] + [-100] * pad)
        attn.append(x["attention_mask"] + [0] * pad)
    return {
        "input_ids": torch.tensor(input_ids, dtype=torch.long),
        "labels": torch.tensor(labels, dtype=torch.long),
        "attention_mask": torch.tensor(attn, dtype=torch.long),
    }


def gb(n: int) -> float:
    return round(n / 1024 ** 3, 3)


def run_train(model, encoded: list[dict], tokenizer, args) -> dict:
    import torch

    # 视觉塔被挪到 CPU 后，next(model.parameters()) 可能先拿到 CPU 上的视觉参数，
    # 所以设备要按输入 embedding 的位置判断（文本训练真正用的 device）。
    try:
        device = model.get_input_embeddings().weight.device
    except Exception:  # noqa: BLE001
        device = next(model.parameters()).device
    trainable = [p for p in model.parameters() if p.requires_grad]
    n_train = sum(p.numel() for p in trainable)
    n_lora_modules = sum(1 for n, _ in model.named_modules()
                         if n.endswith(("lora_A.default", "lora_A")))
    print(f"[smoke] device={device} dtype={args.dtype} "
          f"trainable_params={n_train/1e6:.2f}M lora_modules={n_lora_modules}",
          flush=True)
    optimizer = torch.optim.AdamW(trainable, lr=args.lr,
                                  weight_decay=0.0, betas=(0.9, 0.95))
    model.train()
    torch.cuda.reset_peak_memory_stats()
    samples = list(encoded)
    random.Random(args.seed).shuffle(samples)
    # 长度分桶：Windows 上 expandable_segments 不可用，变长样本反复申请/释放会碎片化，
    # 把相近长度的样本排在一起可显著降低 reserved 峰值。
    chunk = max(1, int(getattr(args, "sort_chunk", 64)))
    if chunk > 1:
        for i in range(0, len(samples), chunk):
            samples[i:i + chunk] = sorted(
                samples[i:i + chunk], key=lambda x: len(x["input_ids"]))
    bs = max(1, args.batch_size)
    ga = max(1, args.grad_accum)
    batches = [samples[i:i + bs] for i in range(0, len(samples), bs)]
    epochs = max(1, int(getattr(args, "epochs", 1) or 1))
    steps_per_epoch = max(1, len(batches) // ga)
    wanted = steps_per_epoch * epochs
    cap = int(getattr(args, "max_steps", 0) or 0)
    steps = min(cap, wanted) if cap > 0 else wanted
    steps = max(1, steps)
    records, losses = [], []
    t_start = time.perf_counter()
    for step in range(steps):
        epoch = step // steps_per_epoch + 1
        micro_batches = [batches[(step * ga + g) % len(batches)] for g in range(ga)]
        t0 = time.perf_counter()
        optimizer.zero_grad(set_to_none=True)
        loss_sum, n_tokens = 0.0, 0
        for micro in micro_batches:
            batch = collate(micro, tokenizer.pad_token_id,
                            bucket=int(getattr(args, "pad_bucket", 32)))
            batch = {k: v.to(device) for k, v in batch.items()}
            n_tokens += int(batch["input_ids"].numel())
            with torch.autocast("cuda", dtype=torch.bfloat16,
                                enabled=(args.dtype == "bf16")):
                out = model(**batch)
                w = sum(float(s.get("weight", 1.0)) for s in micro) / len(micro)
                loss = out.loss * w / ga
            loss.backward()
            loss_sum += float(out.loss.detach().float().item())
        torch.nn.utils.clip_grad_norm_(trainable, 1.0)
        optimizer.step()
        if device.type == "cuda":
            torch.cuda.synchronize()
        dt = time.perf_counter() - t0
        item_loss = loss_sum / ga
        losses.append(item_loss)
        peak_alloc = gb(torch.cuda.max_memory_allocated()) if device.type == "cuda" else 0.0
        peak_reserved = gb(torch.cuda.max_memory_reserved()) if device.type == "cuda" else 0.0
        rec = {"step": step + 1, "epoch": epoch, "loss": round(item_loss, 4),
               "step_seconds": round(dt, 3),
               "peak_alloc_gb": peak_alloc, "peak_reserved_gb": peak_reserved,
               "tokens": n_tokens,
               "prompt_tokens": micro_batches[0][0]["prompt_tokens"],
               "answer_tokens": micro_batches[0][0]["answer_tokens"]}
        records.append(rec)
        print("[step] " + json.dumps(rec, ensure_ascii=False), flush=True)
        every = int(getattr(args, "empty_cache_every", 0) or 0)
        if every and device.type == "cuda" and (step + 1) % every == 0:
            torch.cuda.empty_cache()
        # 周期性存 adapter：断电/重启后至少能拿到中间产物，不用从头再来
        save_every = int(getattr(args, "save_every", 0) or 0)
        if save_every and (step + 1) % save_every == 0 and getattr(args, "save_adapter", ""):
            from pathlib import Path as _Path
            ckpt = _Path(str(args.save_adapter) + f"_step{step + 1}")
            try:
                model.save_pretrained(ckpt)
                print(f"[smoke] checkpoint -> {ckpt}", flush=True)
            except Exception as exc:  # noqa: BLE001 - 存盘失败不该中断训练
                print(f"[smoke] checkpoint 失败 {type(exc).__name__}: {exc}", flush=True)
    total = time.perf_counter() - t_start
    peak_reserved_end = gb(torch.cuda.max_memory_reserved()) if device.type == "cuda" else 0.0
    if peak_reserved_end > 5.85:
        print(f"[smoke] 警告：peak_reserved={peak_reserved_end}GB 已贴近 6GB 上限，"
              f"可能 spill 到共享内存（step 会变慢）；建议 --grad-accum 1 或降 seq",
              flush=True)
    summary = {
        "ok": True,
        "steps": steps,
        "epochs": epochs,
        "steps_per_epoch": steps_per_epoch,
        "total_seconds": round(total, 2),
        "seconds_per_step": round(total / max(1, steps), 3),
        "loss_first": round(losses[0], 4) if losses else None,
        "loss_last": round(losses[-1], 4) if losses else None,
        "loss_mean": round(sum(losses) / len(losses), 4) if losses else None,
        "peak_alloc_gb": gb(torch.cuda.max_memory_allocated()),
        "peak_reserved_gb": gb(torch.cuda.max_memory_reserved()),
        "trainable_params_m": round(n_train / 1e6, 2),
        "model": args.model_path,
        "dtype": args.dtype,
        "max_seq_len": args.max_seq_len,
        "batch_size": args.batch_size,
        "grad_accum": args.grad_accum,
        "load_4bit": bool(args.load_4bit),
        "grad_checkpoint": bool(args.grad_checkpoint),
        "lora": {"r": args.lora_r, "alpha": args.lora_alpha,
                 "dropout": args.lora_dropout},
    }
    return {"summary": summary, "records": records}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="6GB LoRA SFT smoke（灵魂蒸馏 InnerState）")
    p.add_argument("--model-path", default=str(DEFAULT_MODEL_PATH),
                   help="本地模型目录（默认 E:\\agent\\models\\Qwen3.5-2B）")
    p.add_argument("--data", default=str(DEFAULT_DATA), help="soul_sft_v0.jsonl")
    p.add_argument("--limit", type=int, default=0, help="最多读多少条样本（0=全部）")
    p.add_argument("--max-steps", type=int, default=0,
                   help="优化步数上限（0=按 --epochs 跑满）")
    p.add_argument("--epochs", type=int, default=1, help="遍历数据集的轮数")
    p.add_argument("--max-seq-len", type=int, default=512)
    p.add_argument("--batch-size", type=int, default=1)
    p.add_argument("--grad-accum", type=int, default=1)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--lora-r", type=int, default=16)
    p.add_argument("--lora-alpha", type=int, default=32)
    p.add_argument("--lora-dropout", type=float, default=0.05)
    p.add_argument("--dtype", choices=("bf16", "fp16", "fp32"), default="bf16")
    p.add_argument("--load-4bit", action="store_true", help="4bit QLoRA（需 bitsandbytes）")
    p.add_argument("--no-grad-checkpoint", action="store_true")
    p.add_argument("--keep-vision", action="store_true",
                   help="多模态模型默认把视觉塔挪到 CPU 省显存；加此参数保持原样")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--sort-chunk", type=int, default=128,
                   help="长度分桶的块大小（0/1=不分桶）；降低 Windows 显存碎片")
    p.add_argument("--pad-bucket", type=int, default=32,
                   help="把 batch 长度对齐到该倍数（1=不对齐）；减少显存碎片")
    p.add_argument("--empty-cache-every", type=int, default=0,
                   help="每 N 步 torch.cuda.empty_cache()（0=不调用）；碎片严重时用")
    p.add_argument("--save-every", type=int, default=0,
                   help="每 N 步存一个 adapter 快照（0=只在结束时存）；断电后不白跑")
    p.add_argument("--rare-weight", type=float, default=1.0,
                   help="少数类样本 loss 权重（1=关闭）；不复制数据，避免扭曲字段联合分布")
    p.add_argument("--rare-rules", default="length=normal,advice=true",
                   help="命中即加权的规则，逗号分隔，如 length=normal,advice=true")
    p.add_argument("--save-adapter", nargs="?", const=str(DEFAULT_OUTPUT_DIR / "smoke_adapter"),
                   default="", help="可选：smoke 后保存 LoRA adapter（默认 E:\\agent\\output\\smoke_adapter）")
    p.add_argument("--dry-run", action="store_true",
                   help="只处理数据与分词，不加载模型（依赖 tokenizer 仍需 transformers）")
    p.add_argument("--log-jsonl", default=str(DEFAULT_LOG_PATH),
                   help="把 summary/records 追加写入该 jsonl（默认 E:\\agent\\logs\\smoke_lora.jsonl）")
    return p


def offload_vision_tower(model) -> str:
    """文本训练时把多模态模型的视觉塔挪到 CPU，给 6GB 显存腾地方。"""
    import torch

    inner = getattr(model, "model", None)
    visual = getattr(inner, "visual", None) if inner is not None else None
    if visual is None:
        return "no_visual"
    try:
        visual.to("cpu")
        torch.cuda.empty_cache()
        return "cpu"
    except Exception as exc:  # noqa: BLE001 - offload 失败不影响主流程
        return f"failed:{type(exc).__name__}"


def vram() -> str:
    import torch

    if not torch.cuda.is_available():
        return "cuda=N/A"
    return (f"alloc={gb(torch.cuda.memory_allocated())}GB "
            f"reserved={gb(torch.cuda.memory_reserved())}GB")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.grad_checkpoint = not args.no_grad_checkpoint
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    # 注意：不要在 6GB 卡上全局设 PYTORCH_CUDA_ALLOC_CONF（max_split_size_mb 之类）。
    # 实测设了之后权重加载阶段 reserved 反而从 4.1GB 涨到 8.3GB。
    # 显存碎片改用「长度分桶排序 + padding 量化」在数据侧解决（见 collate/run_train）。
    os.environ.setdefault("MODELSCOPE_CACHE", str(HOME_DIR / "cache" / "modelscope"))
    os.environ.setdefault("HF_HOME", str(HOME_DIR / "cache" / "hf"))
    data_path = Path(args.data)
    if not data_path.exists():
        print(f"[smoke] 找不到数据集：{data_path}", file=sys.stderr)
        return 2
    model_path = Path(args.model_path)
    if not (model_path / "config.json").exists() and not args.dry_run:
        print(f"[smoke] 模型目录缺 config.json：{model_path}"
              f"（把权重放到 E:\\agent\\models\\Qwen3.5-2B\\ 或传 --model-path）",
              file=sys.stderr)
        return 2
    items = load_items(data_path, args.limit)
    print(f"[smoke] data={data_path} items={len(items)}", flush=True)

    from transformers import AutoTokenizer  # noqa: E402
    tokenizer = AutoTokenizer.from_pretrained(args.model_path, trust_remote_code=True)
    encoded = prepare_encoded(items, tokenizer, args.max_seq_len)
    # 少数类加权：不复制数据，避免过采样扭曲 length/advice/emoji 的联合分布
    rw = float(getattr(args, "rare_weight", 1.0) or 1.0)
    rules = [r.strip() for r in str(getattr(args, "rare_rules", "") or "").split(",") if r.strip()]
    n_rare = 0
    if rw != 1.0 and rules:
        for enc, item in zip(encoded, items):
            w = rare_weight_for(item, rw, rules)
            enc["weight"] = w
            n_rare += int(w != 1.0)
        print(f"[smoke] rare weight={rw} rules={rules} hit={n_rare}/{len(encoded)}", flush=True)
    n_tok = [len(e["input_ids"]) for e in encoded]
    n_prompt = [e["prompt_tokens"] for e in encoded]
    print(f"[smoke] tokens: n={len(encoded)} avg={sum(n_tok)/len(n_tok):.0f} "
          f"max={max(n_tok)} min={min(n_tok)} prompt_avg={sum(n_prompt)/len(n_prompt):.0f}",
          flush=True)
    if args.dry_run:
        out = {"ok": True, "dry_run": True, "items": len(encoded),
               "token_min": min(n_tok), "token_max": max(n_tok),
               "token_avg": round(sum(n_tok) / len(n_tok), 1),
               "sample": {k: encoded[0][k] for k in ("id", "prompt_tokens", "answer_tokens")}}
        print("[smoke] dry-run summary " + json.dumps(out, ensure_ascii=False), flush=True)
        return 0

    t_import = time.perf_counter()
    import torch  # noqa: E402
    print(f"[smoke] torch={torch.__version__} cuda={torch.version.cuda} "
          f"cuda_available={torch.cuda.is_available()}", flush=True)
    if not torch.cuda.is_available():
        print("[smoke] CUDA 不可用：请检查驱动 / 是否装成 CPU 版 torch", file=sys.stderr)
        return 3
    print(f"[smoke] gpu={torch.cuda.get_device_name(0)} "
          f"vram={gb(torch.cuda.get_device_properties(0).total_memory)}GB "
          f"import_seconds={time.perf_counter()-t_import:.1f}", flush=True)

    try:
        model, tokenizer, config = load_model_and_tokenizer(
            args.model_path, load_4bit=args.load_4bit, dtype_name=args.dtype)
        print(f"[smoke] 权重加载完成 {vram()}", flush=True)
        if args.keep_vision:
            print("[smoke] --keep-vision：视觉塔留在 GPU", flush=True)
        else:
            print(f"[smoke] 视觉塔 offload: {offload_vision_tower(model)} {vram()}",
                  flush=True)
        model = setup_peft(model, r=args.lora_r, alpha=args.lora_alpha,
                           dropout=args.lora_dropout, load_4bit=args.load_4bit)
        print(f"[smoke] LoRA 注入完成 {vram()}", flush=True)
        result = run_train(model, encoded, tokenizer, args)
    except torch.cuda.OutOfMemoryError as exc:
        torch.cuda.empty_cache()
        print(f"[smoke] OOM: {exc}", file=sys.stderr)
        print("[smoke] 降级建议：--max-seq-len 256 / --load-4bit / 换 Qwen2.5-1.5B-Instruct"
              " / --batch-size 1 保持", file=sys.stderr)
        return 4
    if args.save_adapter:
        Path(args.save_adapter).mkdir(parents=True, exist_ok=True)
        model.save_pretrained(args.save_adapter)
        print(f"[smoke] adapter 已保存：{args.save_adapter}", flush=True)
    print("[smoke] SUMMARY " + json.dumps(result["summary"], ensure_ascii=False), flush=True)
    if args.log_jsonl:
        with open(args.log_jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps(result["summary"], ensure_ascii=False) + "\n")
            for rec in result["records"]:
                f.write(json.dumps({"summary_ref": result["summary"].get("model"),
                                    **rec}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
