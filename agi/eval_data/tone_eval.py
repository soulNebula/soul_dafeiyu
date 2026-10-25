# -*- coding: utf-8 -*-
"""P2-b 语气适配器评测（正式版）：分布对比（微信 vs QQ）+ 双维度 pairwise + 可读样例。

与 smoke 版相比：
- 分布对比同时对照 `profile.md`（微信）与 `qq_profile.md`（QQ）；
- pairwise 每条问两维：更像真人 / 更接得上上下文（避免上一轮“短即真人”的假胜率）；
- 只从 held-out val（微信 59 条，QQ 全 train）抽样做 pairwise，不再混 in-sample；
- 输出 12 条含上下文的人工可读样例。

用法::

    E:\\agent\\venv312\\Scripts\\python.exe agi/eval_data/tone_eval.py ^
        --adapter E:\\agent\\output\\tone_qwen05_v1 --out E:\\agent\\logs\\tone_eval_formal.json
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
PROFILE_WECHAT = ROOT / "agi" / "eval_data" / "human_tone" / "profile.md"
PROFILE_QQ = ROOT / "agi" / "eval_data" / "human_tone" / "qq_profile.md"
DEFAULT_MODEL = Path(r"E:\agent\models\Qwen2.5-0.5B-Instruct")
CLAUSE_SPLIT = re.compile(r"[，,。！!？?；;…~、]+")
LEXICON_TERMS = tuple(ht.LEXICON)


# --------------------------------------------------------------------------- #
# 指标
# --------------------------------------------------------------------------- #
def topic_count(text: str) -> int:
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
        "emoji_rate": round(sum(1 for text in texts if ht.has_emoji(text)) / total, 4),
    }


def read_profile_stats(path: Path) -> dict:
    """读 profile.md / qq_profile.md 的机器可读 JSON，取 self 分布关键指标。"""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    match = re.search(r"## \d+\. 机器可读统计.*?```json\s*(\{.*?\})\s*```", text, re.S)
    if not match:
        return {}
    profile = json.loads(match.group(1))
    ending = profile.get("ending", {})
    return {
        "self_rows": profile.get("self_rows"),
        "length_median": profile.get("length", {}).get("median"),
        "no_punct_rate": ending.get("无标点"),
        "question_rate": ending.get("问号"),
        "period_rate": ending.get("句号"),
        "exclaim_rate": ending.get("感叹号"),
        "emoji_rate": profile.get("emoji_rate"),
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
    selected = rows[: int(limit)] if limit else rows
    for index, row in enumerate(selected, 1):
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
            "channel": row.get("channel", "wechat"),
            "context": messages, "human": row["messages"][-1]["content"],
            "model": text, "elapsed_s": round(elapsed, 3),
            "generated_tokens": int(new_tokens.shape[0]),
            "prompt_tokens": int(inputs["input_ids"].shape[1]),
        })
        if index % 10 == 0:
            print(f"[tone_eval] generate {index}/{len(selected)}", flush=True)
    return outputs


# --------------------------------------------------------------------------- #
# DeepSeek 双维度 pairwise
# --------------------------------------------------------------------------- #
JUDGE_PROMPT = (
    "你在评估两条回复（A/B），只做二选一或平局。\n"
    "上下文（对方/我）：\n{context}\n\n"
    "回复 A：{a}\n回复 B：{b}\n\n"
    "请回答两个问题，每个问题输出一行，格式必须严格是：\n"
    "more_human=A|B|tie\n"
    "more_contextual=A|B|tie\n"
    "问题1 more_human：哪条更像真人（普通中国人在微信/QQ 跟朋友聊天：短、口语、常不加标点）？\n"
    "问题2 more_contextual：哪条更接得上上下文（是在回应对方刚说的话，而不是自说自话/答非所问）？"
)


def _pick_verdict(raw: str, key: str) -> str:
    match = re.search(rf"{key}\s*[=:：]\s*(A|B|tie)", raw, re.I)
    if match:
        return match.group(1).upper()
    for line in raw.splitlines():
        if key.lower() in line.lower():
            upper = line.upper()
            if "TIE" in upper or "平" in line:
                return "TIE"
            if re.search(r"\bA\b", upper):
                return "A"
            if re.search(r"\bB\b", upper):
                return "B"
    return "TIE"


def _winner(verdict: str, model_left: bool) -> str:
    if verdict == "TIE":
        return "tie"
    if (verdict == "A" and model_left) or (verdict == "B" and not model_left):
        return "model"
    return "human"


def pairwise_judge(pairs: list[dict], *, judge_client, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    results: list[dict] = []
    for pair in pairs:
        human = pair["human"]
        model = pair["model"]
        model_left = rng.random() < 0.5
        a, b = (model, human) if model_left else (human, model)
        context_lines = []
        for message in pair["context"][-4:]:
            who = "对方" if message["role"] == "user" else "我"
            context_lines.append(f"{who}：{message['content']}")
        prompt = JUDGE_PROMPT.format(context="\n".join(context_lines) or "（无）",
                                      a=a or "（空）", b=b or "（空）")
        started = time.perf_counter()
        result = {}
        raw = ""
        for attempt in range(3):
            result = judge_client.chat(
                [{"role": "user", "content": prompt}], purpose="tone_pairwise",
                max_tokens=64, temperature=0.2)
            raw = str(result.get("text") or "").strip()
            if raw:
                break
            time.sleep(0.4)
        elapsed = time.perf_counter() - started
        human_verdict = _pick_verdict(raw, "more_human")
        contextual_verdict = _pick_verdict(raw, "more_contextual")
        results.append({
            "id": pair["id"], "in_sample": pair.get("split") != "val",
            "left": "model" if model_left else "human", "raw": raw,
            "human_winner": _winner(human_verdict, model_left),
            "contextual_winner": _winner(contextual_verdict, model_left),
            "human_verdict": human_verdict, "contextual_verdict": contextual_verdict,
            "elapsed_s": round(elapsed, 3),
            "human": human, "model": model, "ok": bool(result.get("ok")),
        })
    return results


def summarize_pairwise(results: list[dict]) -> dict:
    def summarize(items: list[dict], key: str) -> dict:
        total = len(items)
        counts = {"model": 0, "human": 0, "tie": 0}
        for item in items:
            counts[item[key]] = counts.get(item[key], 0) + 1
        return {"n": total, "model_win": counts["model"], "human_win": counts["human"],
                "tie": counts["tie"],
                "model_win_rate": round(counts["model"] / total, 4) if total else 0.0,
                "human_win_rate": round(counts["human"] / total, 4) if total else 0.0,
                "tie_rate": round(counts["tie"] / total, 4) if total else 0.0}

    return {
        "human_likeness": summarize(results, "human_winner"),
        "contextual": summarize(results, "contextual_winner"),
        "per_item": results,
    }


def readable_samples(outputs: list[dict], count: int = 12) -> list[dict]:
    samples = []
    for item in outputs[:count]:
        context_lines = [f"{'对方' if m['role'] == 'user' else '我'}：{m['content']}"
                         for m in item["context"][-4:]]
        samples.append({
            "id": item["id"],
            "context": context_lines,
            "human": item["human"],
            "model": item["model"],
            "generated_tokens": item["generated_tokens"],
        })
    return samples


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P2-b 语气 adapter 正式评测")
    parser.add_argument("--data", default=str(DATA_PATH))
    parser.add_argument("--model-path", default=str(DEFAULT_MODEL))
    parser.add_argument("--adapter", default=r"E:\agent\output\tone_qwen05_v1")
    parser.add_argument("--limit", type=int, default=0, help="最多生成多少条（0=全部 val）")
    parser.add_argument("--max-new-tokens", type=int, default=64)
    parser.add_argument("--pairwise-n", type=int, default=20)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--judge-mock", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default=r"E:\agent\logs\tone_eval_formal.json")
    args = parser.parse_args(argv)

    rows = load_rows(Path(args.data))
    val_rows = [row for row in rows if row.get("split") == "val"]
    if args.limit:
        val_rows = val_rows[: int(args.limit)]
    if not val_rows:
        raise SystemExit("[tone_eval] val 为空，先跑 tone_train.py --dry-run")
    print(f"[tone_eval] val={len(val_rows)}", flush=True)

    model, tokenizer = load_adapter(Path(args.model_path), Path(args.adapter))
    started = time.perf_counter()
    outputs = generate_replies(model, tokenizer, val_rows,
                               max_new_tokens=args.max_new_tokens)
    gen_seconds = time.perf_counter() - started

    wechat_ref = read_profile_stats(PROFILE_WECHAT)
    qq_ref = read_profile_stats(PROFILE_QQ)
    corpus_path = ROOT / "agi" / "eval_data" / "human_tone" / "corpus.jsonl"
    qq_corpus_path = ROOT / "agi" / "eval_data" / "human_tone" / "qq_corpus.jsonl"
    wechat_corpus = text_stats([row["text"] for row in load_rows(corpus_path)
                                if row.get("speaker") == "self"])
    qq_corpus = text_stats([row["text"] for row in load_rows(qq_corpus_path)
                            if row.get("speaker") == "self"])
    model_stats = text_stats([item["model"] for item in outputs])
    comparison = {}
    profile_keys = ("length_median", "no_punct_rate", "question_rate",
                    "period_rate", "emoji_rate")
    corpus_keys = ("colloquial_coverage", "topics_mean")
    for key in profile_keys:
        comparison[key] = {"model": model_stats.get(key),
                           "wechat": wechat_ref.get(key), "qq": qq_ref.get(key)}
    for key in corpus_keys:
        comparison[key] = {"model": model_stats.get(key),
                           "wechat": wechat_corpus.get(key), "qq": qq_corpus.get(key)}
    result = {
        "adapter": str(args.adapter),
        "model_path": str(args.model_path),
        "val_pairs": len(outputs),
        "generation_seconds": round(gen_seconds, 2),
        "generation_seconds_per_item": round(gen_seconds / max(1, len(outputs)), 3),
        "max_new_tokens": args.max_new_tokens,
        "human_reference_wechat": wechat_ref,
        "human_reference_qq": qq_ref,
        "model_generated": model_stats,
        "comparison": comparison,
        "samples_readable": readable_samples(outputs, 12),
        "samples": outputs,
    }

    if not args.no_judge:
        judge = None
        if args.judge_mock:
            class _MockJudge:
                def chat(self, messages, **kwargs):
                    return {"ok": True, "text": "more_human=tie\nmore_contextual=tie"}
            judge = _MockJudge()
        else:
            from agi.selflearn import llm
            client = llm.Client()
            if client.available():
                judge = client
            else:
                print("[tone_eval] 没有 DeepSeek key，跳过 pairwise", flush=True)
        if judge is not None:
            rng = random.Random(args.seed)
            pool = list(outputs)
            rng.shuffle(pool)
            judged = pairwise_judge(pool[: min(args.pairwise_n, len(pool))],
                                    judge_client=judge, seed=args.seed)
            result["pairwise"] = {
                "judge": "mock" if args.judge_mock else "deepseek",
                "summary": summarize_pairwise(judged),
            }
            print("[tone_eval] pairwise: " + json.dumps(
                {k: v for k, v in result["pairwise"]["summary"].items() if k != "per_item"},
                ensure_ascii=False), flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    summary = {
        "val_pairs": result["val_pairs"],
        "generation_seconds_per_item": result["generation_seconds_per_item"],
        "comparison": comparison,
        "samples_readable": result["samples_readable"][:3],
    }
    if "pairwise" in result:
        summary["pairwise"] = {k: v for k, v in result["pairwise"]["summary"].items()
                               if k != "per_item"}
    print("[tone_eval] " + json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"[tone_eval] full report -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
