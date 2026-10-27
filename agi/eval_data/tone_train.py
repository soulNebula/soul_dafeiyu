# -*- coding: utf-8 -*-
"""P2-b 语气适配数据管道 + LoRA 训练（target = 用户本人真实发言，不用 teacher 合成）。

核心原则（用户要求）：
- 只把 **self 真实发言** 当 target；context 是它前面 2–4 条真实对话（self/other 交替）；
- **不用 teacher 合成 target**（那就变成“照文档模仿”，用户明确否掉）；teacher 只做评测裁判；
- 合法对 <200 → 不跑正式训练，只交付「管道 + 训练脚本 + smoke」并在报告里写数据缺口。

数据来源：`agi/eval_data/human_tone/corpus.jsonl` 的**原始会话序列**
（截图 OCR 的 ordered messages + 联系人库 ordered messages，复用
`tools/build_human_tone.py` 的解析函数；target 匿名化同样复用其 `anonymize`）。

产出：
- `agi/eval_data/human_tone/sft.jsonl`：每行
  ``{id, source, session, messages:[{role,content}...], target_len, split}``
  messages 最后一条是 assistant（= 用户真实发言），之前是 system-free 的真实上下文；
  90/10 按 session 切分（同一截图/同一联系人会话不跨集）。
- 训练：`--epochs` / `--max-steps`；默认 lr 1e-4、LoRA r16/α32、seq384、bf16、grad ckpt，
  输出 adapter 到 ``E:\\agent\\output\\tone_qwen05_v1``（smoke 用 `tone_qwen05_v1_smoke`）。

用法::

    # 只造数据（小数据量时默认路径）
    E:\\agent\\venv312\\Scripts\\python.exe agi/eval_data/tone_train.py --dry-run
    # smoke（数据 <200 时）
    E:\\agent\\venv312\\Scripts\\python.exe agi/eval_data/tone_train.py --smoke ^
        --out E:\\agent\\output\\tone_qwen05_v1_smoke
    # 数据够了以后：正式训练
    E:\\agent\\venv312\\Scripts\\python.exe agi/eval_data/tone_train.py ^
        --epochs 2 --max-steps 0 --out E:\\agent\\output\\tone_qwen05_v1
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[2]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from agi.paths import agent_path


from tools import build_human_tone as ht  # noqa: E402
from tools import build_qq_tone as bq  # noqa: E402

DATA_PATH = ROOT / "agi" / "eval_data" / "human_tone" / "sft.jsonl"
DEFAULT_MODEL = agent_path(r"models\Qwen2.5-0.5B-Instruct")
DEFAULT_OUT = agent_path(r"output\tone_qwen05_v1")
DEFAULT_QQ_EXPORT = agent_path(r"qq_export\c2c")
MIN_PAIRS_FOR_FORMAL_TRAIN = 200
MAX_CONTEXT = 4
MIN_CONTEXT = 2


# --------------------------------------------------------------------------- #
# 1) 造 SFT 数据
# --------------------------------------------------------------------------- #
def collect_sequences(img_dirs: list[Path] | None = None) -> dict[str, list[dict]]:
    """按 session 收集真实会话序列：一截图=一 session；一联系人库=一 session。

    返回 {session_id: [{"speaker": "self"|"other", "text": ...}, ...]}。
    直接复用 P2-a 的 OCR/DB 解析函数（同一份清洗规则），不重写清洗逻辑。
    """
    sequences: dict[str, list[dict]] = {}
    ocr_lines = ht.load_ocr_lines()
    store = ht.ImageStore(base_dirs=[ht.SCREENSHOT_DIR, *(img_dirs or []), ht.GIRL_PARTS_DIR])
    files = sorted({str(row.get("file")) for row in ocr_lines})
    for name in files:
        if name == ht.LONG_IMAGE:
            continue
        messages = ht.ocr_image_messages(name, ocr_lines, store)
        if messages:
            sequences[f"ocr:{Path(name).stem[:12]}"] = [
                {"speaker": message["speaker"], "text": message["text"]}
                for message in messages
            ]
    for path in sorted(Path(ht.CONTACTS_DIR).glob("*.sqlite3")):
        con = sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True)
        try:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "messages" not in tables:
                rows = []
            else:
                rows = con.execute(
                    "SELECT role, text FROM messages "
                    "WHERE text IS NOT NULL AND TRIM(text)<>'' ORDER BY ts"
                ).fetchall()
        finally:
            con.close()
        if not rows:
            continue
        sequences[f"db:{path.stem[:8]}"] = [
            {"speaker": "self" if str(role) == "user" else "other", "text": str(text)}
            for role, text in rows
        ]
    return sequences


def build_pairs(sequences: dict[str, list[dict]]) -> list[dict]:
    """(前 2–4 条真实上下文 → self 真实发言) 的样本列表（含 session/source）。"""
    pairs: list[dict] = []
    for session, sequence in sequences.items():
        anonymized = [
            {"speaker": message["speaker"], "text": ht.anonymize(message["text"])}
            for message in sequence
        ]
        for index, message in enumerate(anonymized):
            if message["speaker"] != "self":
                continue
            context = anonymized[max(0, index - MAX_CONTEXT):index]
            if len(context) < MIN_CONTEXT:
                continue
            messages = [
                {"role": "assistant" if item["speaker"] == "self" else "user",
                 "content": item["text"]}
                for item in context + [message]
            ]
            if not messages[-1]["content"].strip():
                continue
            pairs.append({
                "id": f"tone-{len(pairs) + 1:04d}",
                "source": session.split(":", 1)[0],
                "session": session,
                "messages": messages,
                "target_len": len(messages[-1]["content"]),
            })
    return pairs


def build_qq_pairs(export_dir: Path, max_context: int = MAX_CONTEXT,
                   min_context: int = MIN_CONTEXT) -> list[dict]:
    """QQ c2c 导出 JSON → (前 2–4 条上下文 → self 真实发言) 对。

    复用 build_qq_tone 的清洗/匿名化口径；QQ 只有一个有效会话，全部进 train（无独立 val），
    在报告里注明这是已知局限。channel="qq"。
    """
    data_by_peer: dict[str, list[dict]] = {}
    for path in sorted(Path(export_dir).glob("*_full.json")):
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rows, list) or not rows:
            continue
        peer = str(rows[0].get("peer") or path.stem.split("_")[0])
        data_by_peer[peer] = rows
    labels = {peer: f"朋友Q{index + 1}" for index, peer in enumerate(sorted(data_by_peer))}
    pairs: list[dict] = []
    for peer, rows in sorted(data_by_peer.items()):
        label = labels[peer]
        chat_name = str(rows[0].get("chat_name") or "")
        cleaned: list[dict] = []
        for row in rows:
            text, kind = bq.classify_text(str(row.get("text") or ""))
            if kind != "text":
                continue
            text = bq.anonymize(text, label=label, chat_name=chat_name)
            if not text:
                continue
            cleaned.append({"speaker": "self" if row.get("is_self") else "other",
                            "text": text})
        for index, item in enumerate(cleaned):
            if item["speaker"] != "self":
                continue
            context = cleaned[max(0, index - max_context):index]
            if len(context) < min_context:
                continue
            messages = [{"role": "assistant" if ctx["speaker"] == "self" else "user",
                         "content": ctx["text"]}
                        for ctx in context + [item]]
            pairs.append({
                "source": "qq",
                "session": f"qq:{peer}",
                "channel": "qq",
                "messages": messages,
                "target_len": len(messages[-1]["content"]),
                "split": "train",
            })
    return pairs


def assign_split(pairs: list[dict], val_ratio: float = 0.1) -> tuple[list[dict], list[dict]]:
    """90/10 按 session 切：整段会话进同一侧；按会话样本数从小到大累计 val 到约 10%。"""
    by_session: dict[str, list[dict]] = {}
    for pair in pairs:
        by_session.setdefault(pair["session"], []).append(pair)
    total = len(pairs)
    target_val = max(1, round(total * float(val_ratio))) if total >= 5 else 0
    ordered = sorted(by_session.items(), key=lambda item: (len(item[1]), item[0]))
    val_sessions: list[str] = []
    val_count = 0
    for session, items in ordered:
        if val_count >= target_val:
            break
        val_sessions.append(session)
        val_count += len(items)
    val_set = set(val_sessions)
    for pair in pairs:
        pair["split"] = "val" if pair["session"] in val_set else "train"
    return [p for p in pairs if p["split"] == "train"], [p for p in pairs if p["split"] == "val"]


def validate_pairs(pairs: list[dict]) -> list[str]:
    """数据侧断言：字段/隐私/会话不跨集。返回问题列表（空=通过）。"""
    problems: list[str] = []
    sessions_by_split: dict[str, set] = {"train": set(), "val": set()}
    for pair in pairs:
        for key in ("id", "source", "session", "messages", "target_len", "split"):
            if key not in pair:
                problems.append(f"{pair.get('id')} 缺字段 {key}")
        messages = pair.get("messages") or []
        if len(messages) < 2:
            problems.append(f"{pair['id']} context 太短")
            continue
        if messages[-1]["role"] != "assistant":
            problems.append(f"{pair['id']} 最后一条不是 assistant（target 必须是 self 发言）")
        for message in messages:
            if message["role"] not in ("user", "assistant"):
                problems.append(f"{pair['id']} 非法 role {message['role']}")
            violations = ht.privacy_violations(message["content"])
            if violations:
                problems.append(f"{pair['id']} 隐私问题 {violations}: {message['content'][:40]}")
        sessions_by_split.setdefault(pair["split"], set()).add(pair["session"])
    leaked = sessions_by_split.get("train", set()) & sessions_by_split.get("val", set())
    if leaked:
        problems.append(f"session 跨 train/val: {sorted(leaked)}")
    return problems


def write_sft(pairs: list[dict], path: Path = DATA_PATH) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for pair in pairs:
            record = {key: pair[key] for key in
                      ("id", "source", "session", "messages", "target_len", "split", "channel")}
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    train = [p for p in pairs if p["split"] == "train"]
    val = [p for p in pairs if p["split"] == "val"]
    stats = {
        "path": str(path),
        "pairs": len(pairs),
        "train": len(train),
        "val": len(val),
        "val_ratio": round(len(val) / max(1, len(pairs)), 4),
        "sessions": len({p["session"] for p in pairs}),
        "val_sessions": sorted({p["session"] for p in val}),
        "by_source": dict(Counter(p["source"] for p in pairs)),
        "by_channel": dict(Counter(p.get("channel", "wechat") for p in pairs)),
        "target_len_median": sorted(p["target_len"] for p in pairs)[len(pairs) // 2] if pairs else 0,
        "gap_to_200": max(0, MIN_PAIRS_FOR_FORMAL_TRAIN - len(pairs)),
        "formal_train_ready": len(pairs) >= MIN_PAIRS_FOR_FORMAL_TRAIN,
    }
    return stats


# --------------------------------------------------------------------------- #
# 2) 训练
# --------------------------------------------------------------------------- #
def load_sft(path: Path = DATA_PATH) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def encode_tone_samples(tokenizer, pairs: list[dict], max_len: int) -> list[dict]:
    """复用 smoke_lora.encode_sample：prompt(context) mask，target(self 发言) 算 loss。"""
    from tools.train import smoke_lora

    encoded: list[dict] = []
    for pair in pairs:
        messages = pair["messages"]
        prompt_messages = messages[:-1]
        answer = str(messages[-1]["content"])
        enc = smoke_lora.encode_sample(tokenizer, prompt_messages, answer, max_len=max_len)
        enc["id"] = pair["id"]
        encoded.append(enc)
    return encoded


def train(args: argparse.Namespace) -> dict:
    from types import SimpleNamespace

    import torch

    from tools.train import smoke_lora

    pairs = [p for p in load_sft(Path(args.data)) if p["split"] == "train"]
    channels = Counter(p.get("channel", "wechat") for p in pairs)
    print(f"[tone_train] train channels={dict(channels)}", flush=True)
    if args.limit:
        pairs = pairs[: int(args.limit)]
    if not pairs:
        raise SystemExit("[tone_train] 没有可训练样本（--data 的 train split 为空）")
    print(f"[tone_train] train pairs={len(pairs)} model={args.model_path}", flush=True)

    model, tokenizer, _config = smoke_lora.load_model_and_tokenizer(
        str(args.model_path), load_4bit=False, dtype_name=args.dtype)
    model = smoke_lora.setup_peft(model, r=args.lora_r, alpha=args.lora_alpha,
                                  dropout=args.lora_dropout, load_4bit=False)
    encoded = encode_tone_samples(tokenizer, pairs, max_len=args.max_seq_len)
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
        "data": str(args.data),
        "train_pairs": len(pairs),
        "model_path": str(args.model_path),
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
    print("[tone_train] " + json.dumps(metrics, ensure_ascii=False), flush=True)
    return metrics


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P2-b 语气适配：造 sft.jsonl + 可选 LoRA 训练（target=用户真实发言）")
    parser.add_argument("--data", default=str(DATA_PATH), help="sft.jsonl 路径")
    parser.add_argument("--dry-run", "--build-only", dest="dry_run", action="store_true",
                        help="只造数据并打印统计，不训练")
    parser.add_argument("--model-path", default=str(DEFAULT_MODEL))
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="adapter 输出目录")
    parser.add_argument("--smoke", action="store_true",
                        help="数据 <200 时的 smoke 模式（自动限 max_steps=100）")
    parser.add_argument("--limit", type=int, default=0, help="只用前 N 条 train 样本")
    parser.add_argument("--epochs", type=int, default=2)
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
    parser.add_argument("--val-ratio", type=float, default=0.1)
    parser.add_argument("--img-dir", action="append", default=None,
                        help="额外微信截图目录（可多次指定，如 Desktop/img/new）")
    parser.add_argument("--qq-export-dir", default=str(DEFAULT_QQ_EXPORT),
                        help="QQ c2c *_full.json 目录；不存在则只造微信数据（传空字符串禁用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sequences = collect_sequences([Path(item) for item in (args.img_dir or [])])
    pairs = build_pairs(sequences)
    for pair in pairs:
        pair["channel"] = "wechat"
    assign_split(pairs, val_ratio=args.val_ratio)
    qq_pairs = build_qq_pairs(Path(args.qq_export_dir)) if args.qq_export_dir else []
    for index, pair in enumerate(qq_pairs, 1):
        pair["id"] = f"tone-qq-{index:04d}"
    pairs = pairs + qq_pairs
    problems = validate_pairs(pairs)
    if problems:
        print("[tone_train][FATAL] 数据校验失败：\n  " + "\n  ".join(problems[:10]),
              file=sys.stderr)
        return 1
    stats = write_sft(pairs, Path(args.data))
    print("[tone_train] sft: " + json.dumps(stats, ensure_ascii=False), flush=True)
    if args.dry_run:
        return 0
    if not stats["formal_train_ready"]:
        print(f"[tone_train] 合法对 {stats['pairs']} < {MIN_PAIRS_FOR_FORMAL_TRAIN}："
              f"进入 smoke 路径（formal train 需要再补 {stats['gap_to_200']} 对）", flush=True)
        if not args.smoke and args.max_steps == 0:
            args.smoke = True
    if args.smoke and args.max_steps == 0:
        args.max_steps = 100
        print("[tone_train] smoke: max_steps=100", flush=True)
    train(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
