# -*- coding: utf-8 -*-
"""P2-b 语气适配器评测：分布对比 + 与真人原话的 pairwise（DeepSeek 当裁判）。

对比指标（都在同一套函数下算，human 参考来自 `human_tone/profile.md` 的 self 语料）：
- 长度中位数、句尾无标点率、句号率、问号率；
- 口语词覆盖率（含 human_tone profile 的口语词表任一词的比例）；
- 单条主题数（用逗号/句号/问号切句子数近似，1=一条只说一件事）。

pairwise：对同一 context，A=真人原话、B=模型生成，问 DeepSeek
「哪条更像真人在微信里跟朋友说话」，统计模型胜/平/负。val 不够 20 条时，
不足部分从 train 抽样（标 in_sample=true，报告里分开统计）。

用法::

    E:\\agent\\venv312\\Scripts\\python.exe agi/eval_data/tone_eval.py ^
        --adapter E:\\agent\\output\\tone_qwen05_v1_smoke ^
        --limit 7 --pairwise-n 20 --out E:\\agent\\logs\\tone_eval_smoke.json
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import build_human_tone as ht  # noqa: E402

DATA_PATH = ROOT / "agi" / "eval_data" / "human_tone" / "sft.jsonl"
CORPUS_PATH = ROOT / "agi" / "eval_data" / "human_tone" / "corpus.jsonl"
DEFAULT_MODEL = Path(r"E:\agent\models\Qwen2.5-0.5B-Instruct")
CLAUSE_SPLIT = re.compile(r"[，,。！!？?；;…~、]+")
LEXICON_TERMS = tuple(ht.LEXICON)


# --------------------------------------------------------------------------- #
# 文本指标（human 与 model 用同一套）
# --------------------------------------------------------------------------- #
def topic_count(text: str) -> int:
    """单条主题数近似：按标点切出的非空片段数（1 = 一条只说一件事）。"""
    parts = [part for part in CLAUSE_SPLIT.split(str(text or "")) if part.strip()]
    return max(1, len(parts))


def colloquial_coverage(texts: list[str]) -> float:
    if not texts:
        return 0.0
    hit = sum(1 for text in texts if any(term in text for term in LEXICON_TERMS))
    return round(hit / len(texts), 4)


def text_stats(texts: list[str]) -> dict:
    texts = [text for text in texts if str(text or "").strip()]
    if not texts:
        return {"count": 0}
    lengths = [len(text) for text in texts]
    endings = [ht.ending_category(text) for text in texts]
    total = len(texts)
    return {
        "count": total,
        "length_median": statistics.median(lengths),
        "length_mean": round(statistics.mean(lengths), 2),
        "no_punct_rate": round(sum(1 for e in endings if e == "无标点") / total, 4),
        "question_rate": round(sum(1 for e in endings if e == "问号") / total, 4),
        "period_rate": round(sum(1 for e in endings if e == "句号") / total, 4),
        "exclaim_rate": round(sum(1 for e in endings if e == "感叹号") / total, 4),
        "colloquial_coverage": colloquial_coverage(texts),
        "topics_mean": round(statistics.mean(topic_count(text) for text in texts), 2),
        "topics_median": statistics.median(topic_count(text) for text in texts),
    }


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #
def load_rows(path: Path = DATA_PATH) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def build_prompt(tokenizer, messages: list[dict]) -> str:
    try:
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
            enable_thinking=False)
    except TypeError:
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True)


def load_adapter(model_path: Path, adapter: Path):
    import torch
    from peft import PeftModel

    from tools.train import smoke_lora

    model, tokenizer, _ = smoke_lora.load_model_and_tokenizer(
        str(model_path), load_4bit=False, dtype_name="bf16")
    if adapter and str(adapter):
        print(f"[tone_eval] 加载 adapter: {adapter}", flush=True)
        model = PeftModel.from_pretrained(model, str(adapter))
    model.eval()
    return model, tokenizer


def generate_replies(model, tokenizer, rows: list[dict], *, max_new_tokens: int,
                     limit: int = 0) -> list[dict]:
    import torch

    device = model.get_input_embeddings().weight.device
    outputs: list[dict] = []
    for index, row in enumerate(rows[: int(limit)] if limit else rows, 1):
        messages = [{"role": m["role"], "content": m["content"]}
                    for m in row["messages"][:-1]]
        prompt_text = build_prompt(tokenizer, messages)
        inputs = tokenizer(prompt_text, return_tensors="pt").to(device)
        started = time.perf_counter()
        with torch.no_grad():
            generated = model.generate(
                **inputs, max_new_tokens=max_new_tokens, do_sample=False,
                temperature=None, top_p=None, top_k=None,
                pad_token_id=tokenizer.eos_token_id)
        elapsed = time.perf_counter() - started
        new_tokens = generated[0][inputs["input_ids"].shape[1]:]
        text = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
        outputs.append({
            "id": row["id"], "session": row.get("session"), "split": row.get("split"),
            "context": messages, "human": row["messages"][-1]["content"],
            "model": text, "elapsed_s": round(elapsed, 3),
            "generated_tokens": int(new_tokens.shape[0]),
            "prompt_tokens": int(inputs["input_ids"].shape[1]),
        })
        if index % 5 == 0:
            print(f"[tone_eval] generate {index}/{len(rows[:limit] if limit else rows)}",
                  flush=True)
    return outputs


# --------------------------------------------------------------------------- #
# DeepSeek pairwise 裁判
# --------------------------------------------------------------------------- #
JUDGE_PROMPT = (
    "你在判断两条微信回复哪条更像真人（真人=普通中国人用微信跟朋友聊天，短、口语、"
    "经常不加标点；另一条可能是 AI 写的，可能更完整、更像客服/采访）。\n"
    "只看语气和写法，不看内容对错。\n"
    "对话上下文：\n{context}\n"
    "回复 A：{a}\n"
    "回复 B：{b}\n"
    "哪条更像真人？只回答一个词：A、B 或 tie。"
)


def pairwise_judge(pairs: list[dict], *, judge_client, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    results: list[dict] = []
    for pair in pairs:
        human = pair["human"]
        model = pair["model"]
        # 随机左右，避免位置偏差
        model_left = rng.random() < 0.5
        a, b = (model, human) if model_left else (human, model)
        context_lines = []
        for message in pair["context"][-4:]:
            who = "对方" if message["role"] == "user" else "我"
            context_lines.append(f"{who}：{message['content']}")
        prompt = JUDGE_PROMPT.format(context="\n".join(context_lines) or "（无）",
                                      a=a or "（空）", b=b or "（空）")
        started = time.perf_counter()
        result = judge_client.chat(
            [{"role": "user", "content": prompt}], purpose="tone_pairwise",
            max_tokens=16, temperature=0.2)
        elapsed = time.perf_counter() - started
        raw = str(result.get("text") or "").strip()
        verdict = "tie"
        upper = raw.upper()
        if "TIE" in upper or "平" in raw:
            verdict = "tie"
        elif re.search(r"\bA\b", upper) or raw.startswith("A"):
            verdict = "A"
        elif re.search(r"\bB\b", upper) or raw.startswith("B"):
            verdict = "B"
        # 映射回 human/model（A/B 的左右是随机的）
        if verdict == "tie":
            winner = "tie"
        elif (verdict == "A" and model_left) or (verdict == "B" and not model_left):
            winner = "model"
        else:
            winner = "human"
        results.append({
            "id": pair["id"], "in_sample": pair.get("split") != "val",
            "left": "model" if model_left else "human", "raw": raw,
            "winner": winner, "elapsed_s": round(elapsed, 3),
            "human": human, "model": model,
            "ok": bool(result.get("ok")),
        })
    return results


def summarize_pairwise(results: list[dict]) -> dict:
    def summarize(items: list[dict]) -> dict:
        total = len(items)
        counts = {"model": 0, "human": 0, "tie": 0}
        for item in items:
            counts[item["winner"]] = counts.get(item["winner"], 0) + 1
        return {"n": total, "model_win": counts["model"], "human_win": counts["human"],
                "tie": counts["tie"],
                "model_win_rate": round(counts["model"] / total, 4) if total else 0.0,
                "human_win_rate": round(counts["human"] / total, 4) if total else 0.0,
                "tie_rate": round(counts["tie"] / total, 4) if total else 0.0}

    val = [item for item in results if not item["in_sample"]]
    train = [item for item in results if item["in_sample"]]
    return {"all": summarize(results), "heldout_val": summarize(val),
            "in_sample_train": summarize(train)}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P2-b 语气 adapter 评测")
    parser.add_argument("--data", default=str(DATA_PATH))
    parser.add_argument("--corpus", default=str(CORPUS_PATH))
    parser.add_argument("--model-path", default=str(DEFAULT_MODEL))
    parser.add_argument("--adapter", default=r"E:\agent\output\tone_qwen05_v1_smoke")
    parser.add_argument("--limit", type=int, default=0, help="最多生成多少条（0=全部 val）")
    parser.add_argument("--max-new-tokens", type=int, default=64)
    parser.add_argument("--pairwise-n", type=int, default=20)
    parser.add_argument("--no-judge", action="store_true", help="跳过 DeepSeek pairwise")
    parser.add_argument("--judge-mock", action="store_true", help="用固定判词自测（不联网）")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default=r"E:\agent\logs\tone_eval_smoke.json")
    args = parser.parse_args(argv)

    rows = load_rows(Path(args.data))
    val_rows = [row for row in rows if row.get("split") == "val"]
    train_rows = [row for row in rows if row.get("split") == "train"]
    if args.limit:
        val_rows = val_rows[: int(args.limit)]
    if not val_rows:
        raise SystemExit("[tone_eval] val 为空，先跑 tone_train.py --dry-run")
    print(f"[tone_eval] val={len(val_rows)} train={len(train_rows)}", flush=True)

    model, tokenizer = load_adapter(Path(args.model_path), Path(args.adapter))
    started = time.perf_counter()
    outputs = generate_replies(model, tokenizer, val_rows,
                               max_new_tokens=args.max_new_tokens)
    gen_seconds = time.perf_counter() - started

    # 人类参考：corpus 里的 self 语料
    corpus = [json.loads(line) for line in
              Path(args.corpus).read_text(encoding="utf-8").splitlines() if line.strip()]
    human_texts = [row["text"] for row in corpus if row["speaker"] == "self"]
    human_stats = text_stats(human_texts)
    model_stats = text_stats([item["model"] for item in outputs])
    result = {
        "adapter": str(args.adapter),
        "model_path": str(args.model_path),
        "val_pairs": len(outputs),
        "generation_seconds": round(gen_seconds, 2),
        "generation_seconds_per_item": round(gen_seconds / max(1, len(outputs)), 3),
        "max_new_tokens": args.max_new_tokens,
        "human_reference": human_stats,
        "model_generated": model_stats,
        "distribution_delta": {
            key: round(model_stats.get(key, 0) - human_stats.get(key, 0), 4)
            for key in ("length_median", "no_punct_rate", "question_rate",
                        "period_rate", "colloquial_coverage", "topics_mean")
        },
        "samples": outputs,
    }

    # pairwise：val 不够就从 train 补（标记 in_sample）
    judge_available = False
    if not args.no_judge:
        judge = None
        if args.judge_mock:
            class _MockJudge:
                def chat(self, messages, **kwargs):
                    return {"ok": True, "text": "tie"}
            judge = _MockJudge()
            judge_available = True
        else:
            from agi.selflearn import llm
            client = llm.Client()
            if client.available():
                judge = client
                judge_available = True
            else:
                print("[tone_eval] 没有 DeepSeek key，跳过 pairwise", flush=True)
        if judge is not None:
            pool = list(outputs)
            rng = random.Random(args.seed)
            while len(pool) < args.pairwise_n and train_rows:
                row = train_rows[len(pool) % len(train_rows)]
                messages = [{"role": m["role"], "content": m["content"]}
                            for m in row["messages"][:-1]]
                extra = generate_replies(model, tokenizer, [row],
                                         max_new_tokens=args.max_new_tokens)[0]
                extra["split"] = "train"
                extra["context"] = messages
                pool.append(extra)
            rng.shuffle(pool)
            judged = pairwise_judge(pool[: args.pairwise_n], judge_client=judge,
                                    seed=args.seed)
            result["pairwise"] = {"judge": "deepseek" if not args.judge_mock else "mock",
                                  "summary": summarize_pairwise(judged),
                                  "details": judged}
            print("[tone_eval] pairwise: " + json.dumps(
                result["pairwise"]["summary"], ensure_ascii=False), flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    summary = {key: result[key] for key in
               ("val_pairs", "generation_seconds_per_item", "human_reference",
                "model_generated", "distribution_delta")}
    if "pairwise" in result:
        summary["pairwise"] = result["pairwise"]["summary"]["all"]
    print("[tone_eval] " + json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"[tone_eval] full report -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
