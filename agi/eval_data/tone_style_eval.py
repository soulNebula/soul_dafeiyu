# -*- coding: utf-8 -*-
"""P2-b 线 B 评测 v2：真实链路 + 内容锚点 + 推理端护栏。"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "agi" / "eval_data"
for path in (str(EVAL_DIR), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from style_prompt import (  # noqa: E402
    build_ai_open_messages, build_messages, extract_anchors, prompt_fingerprint,
)

DATA_PATH = ROOT / "agi" / "eval_data" / "human_tone" / "style_sft_ai.jsonl"
DEFAULT_MODEL = Path(r"E:\agent\models\Qwen2.5-0.5B-Instruct")
PAIRWISE_PROMPT = (
    "用户真实原话：{human}\n模型改写：{model}\n\n"
    "哪条更像用户本人平时在微信/QQ 里的说话方式（长度、标点、口语习惯）？\n"
    "只回答：A（真实原话）、B（模型改写）或 tie。"
)


def load_rows(path: Path = DATA_PATH) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def parse_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}
    return {}


def load_adapter(model_path: Path, adapter: Path):
    from peft import PeftModel

    from tools.train import smoke_lora

    model, tokenizer, _ = smoke_lora.load_model_and_tokenizer(
        str(model_path), load_4bit=False, dtype_name="bf16")
    if adapter and str(adapter):
        print(f"[style_eval] 加载 adapter: {adapter}", flush=True)
        model = PeftModel.from_pretrained(model, str(adapter))
    model.eval()
    return model, tokenizer


def generate(model, tokenizer, messages: list[dict], *, max_new_tokens: int = 64) -> dict:
    import torch

    device = model.get_input_embeddings().weight.device
    text = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(text, return_tensors="pt").to(device)
    started = time.perf_counter()
    with torch.no_grad():
        generated = model.generate(
            **inputs, max_new_tokens=max_new_tokens, do_sample=False,
            pad_token_id=tokenizer.eos_token_id)
    new_tokens = generated[0][inputs["input_ids"].shape[1]:]
    return {"text": tokenizer.decode(new_tokens, skip_special_tokens=True).strip(),
            "elapsed_s": round(time.perf_counter() - started, 3),
            "generated_tokens": int(new_tokens.shape[0])}


def deepseek_ai_input(client, context: list[dict]) -> str:
    messages = build_ai_open_messages(context)
    for _ in range(3):
        result = client.chat(messages, purpose="style_ai_open",
                             max_tokens=200, temperature=0.45, json_mode=True)
        data = parse_json(str(result.get("text") or ""))
        text = str(data.get("ai_input") or data.get("content") or "").strip()
        if text:
            return text
        time.sleep(0.3)
    return ""


def guarded_rewrite(model, tokenizer, context, ai_input, anchors) -> dict:
    """改写 + 内容锚点护栏：缺 anchor 重试一次，仍缺则回退 AI 原文。"""
    first = generate(model, tokenizer, build_messages(context, ai_input, anchors))
    missing = [anchor for anchor in anchors if anchor not in first["text"]]
    retried = False
    fallback = False
    output = first["text"]
    if missing:
        retried = True
        retry_prompt = ai_input + ("\n\n（改写必须包含这些信息：" + "、".join(missing) + "）")
        retry = generate(model, tokenizer,
                         build_messages(context, retry_prompt, anchors))
        missing_retry = [anchor for anchor in anchors if anchor not in retry["text"]]
        if missing_retry:
            fallback = True
            output = ai_input
        else:
            output = retry["text"]
    return {"first_output": first["text"], "final_output": output,
            "anchors": anchors, "missing_first": missing, "retried": retried,
            "fallback": fallback, "elapsed_s": first["elapsed_s"]}


def judge(client, ai_input: str, output: str) -> dict:
    prompt = (
        "AI 想说的内容：" + (ai_input or "（空）") + "\n候选改写："
        + (output or "（空）") + "\n\n"
        "请判断候选改写是否与 AI 想说的内容表达同一件事，并检查是否新增事实。\n"
        "只输出 JSON：{\"same_meaning\": 1-5 的整数, \"new_facts\": \"yes\" 或 \"no\", "
        "\"note\": \"一句话说明\"}\n"
        "1=完全无关，3=部分相关但丢了关键信息，5=语义完全等价。"
    )
    for _ in range(3):
        result = client.chat([{"role": "user", "content": prompt}],
                             purpose="style_judge", max_tokens=160,
                             temperature=0.2, json_mode=True)
        data = parse_json(str(result.get("text") or ""))
        try:
            score = int(data.get("same_meaning") or 0)
        except (TypeError, ValueError):
            score = 0
        if score:
            return {"same_meaning": max(1, min(5, score)),
                    "new_facts": str(data.get("new_facts") or "no").lower() == "yes",
                    "note": str(data.get("note") or "")[:120]}
        time.sleep(0.3)
    return {"same_meaning": 0, "new_facts": False, "note": "judge_failed"}


def pairwise(client, human: str, model_text: str, rng: random.Random) -> str:
    model_left = rng.random() < 0.5
    a, b = (model_text, human) if model_left else (human, model_text)
    prompt = PAIRWISE_PROMPT.format(human=a, model=b)
    for _ in range(3):
        result = client.chat([{"role": "user", "content": prompt}],
                             purpose="style_pairwise", max_tokens=16,
                             temperature=0.2)
        raw = str(result.get("text") or "").strip().upper()
        if not raw:
            time.sleep(0.3)
            continue
        if "TIE" in raw or "平" in raw:
            return "tie"
        verdict = "A" if re.search(r"\bA\b", raw) else ("B" if re.search(r"\bB\b", raw) else "tie")
        if verdict == "tie":
            return "tie"
        return "model" if ((verdict == "A" and model_left)
                           or (verdict == "B" and not model_left)) else "human"
    return "tie"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语气改写真实链路评测（anchors + 护栏）")
    parser.add_argument("--data", default=str(DATA_PATH))
    parser.add_argument("--model-path", default=str(DEFAULT_MODEL))
    parser.add_argument("--adapter", default=r"E:\agent\output\tone_style_qwen05_v2")
    parser.add_argument("--pairwise-n", type=int, default=20)
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--max-new-tokens", type=int, default=64)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default=r"E:\agent\logs\tone_style_eval_v2.json")
    args = parser.parse_args(argv)

    rows = load_rows(Path(args.data))
    val_rows = [row for row in rows if row.get("split") == "val" and row.get("ai_input")]
    if not val_rows:
        raise SystemExit("[style_eval] 没有 val 样本")
    rng = random.Random(args.seed)
    rng.shuffle(val_rows)
    chosen = val_rows[: min(args.pairwise_n, len(val_rows))]
    print(f"[style_eval] val={len(val_rows)} chosen={len(chosen)}", flush=True)

    from agi.selflearn import llm
    client = llm.Client()
    if not client.available():
        raise SystemExit("[style_eval] 没有 DeepSeek key")
    model, tokenizer = load_adapter(Path(args.model_path), Path(args.adapter))

    results: list[dict] = []
    content_fail = 0
    for index, row in enumerate(chosen, 1):
        context = row["context"]
        ai_input = deepseek_ai_input(client, context)
        if not ai_input:
            content_fail += 1
            print(f"[style_eval] {index} DeepSeek 内容生成失败，跳过", flush=True)
            continue
        context_text = " ".join(str(m.get("content") or "") for m in context)
        anchors = extract_anchors(ai_input, context_text)
        guarded = guarded_rewrite(model, tokenizer, context, ai_input, anchors)
        judged = judge(client, ai_input, guarded["final_output"])
        verdict = pairwise(client, row["original"], guarded["final_output"], rng)
        missing_final = [anchor for anchor in anchors
                         if anchor not in guarded["final_output"]]
        results.append({
            "id": row["id"], "context": context[-3:], "ai_input": ai_input,
            "anchors": anchors, "first_output": guarded["first_output"],
            "final_output": guarded["final_output"],
            "missing_first": guarded["missing_first"],
            "missing_final": missing_final,
            "retried": guarded["retried"], "fallback": guarded["fallback"],
            "same_meaning": judged["same_meaning"], "new_facts": judged["new_facts"],
            "judge_note": judged["note"], "pairwise": verdict,
            "real_original": row["original"], "elapsed_s": guarded["elapsed_s"],
        })
        if index % 5 == 0:
            print(f"[style_eval] {index}/{len(chosen)}", flush=True)

    n = len(results) or 1
    meanings = [r["same_meaning"] for r in results]
    anchor_total = sum(len(r["anchors"]) for r in results)
    anchor_kept = sum(sum(1 for a in r["anchors"] if a in r["final_output"])
                      for r in results)
    paired = {"model": 0, "human": 0, "tie": 0}
    for r in results:
        paired[r["pairwise"]] += 1
    stats = {
        "n": len(results), "content_generation_failed": content_fail,
        "meaning_median": sorted(meanings)[len(meanings) // 2] if meanings else 0,
        "meaning_mean": round(sum(meanings) / n, 2),
        "meaning_pass_rate": round(sum(1 for s in meanings if s >= 4) / n, 4),
        "meaning_fail": sum(1 for s in meanings if s < 4),
        "anchor_count": anchor_total,
        "anchor_retention_rate": round(anchor_kept / anchor_total, 4) if anchor_total else 1.0,
        "anchor_all_kept_rate": round(sum(1 for r in results if not r["missing_final"]) / n, 4),
        "hallucination_rate": round(sum(1 for r in results if r["new_facts"]) / n, 4),
        "pairwise": paired,
        "pairwise_model_win_rate": round(paired["model"] / n, 4),
        "pairwise_human_win_rate": round(paired["human"] / n, 4),
        "guardrail_trigger_rate": round(sum(1 for r in results if r["retried"]) / n, 4),
        "guardrail_retry_success_rate": round(
            sum(1 for r in results if r["retried"] and not r["fallback"])
            / max(1, sum(1 for r in results if r["retried"])), 4),
        "fallback_rate": round(sum(1 for r in results if r["fallback"]) / n, 4),
        "final_content_pass_rate": round(
            sum(1 for r in results if (not r["missing_final"])
                and r["same_meaning"] >= 4) / n, 4),
        "prompt_version": prompt_fingerprint()["version"],
        "generation_seconds_per_item": round(sum(r["elapsed_s"] for r in results) / n, 3),
        "samples": results[: args.samples], "all": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(stats, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    print("[style_eval] " + json.dumps(
        {k: v for k, v in stats.items() if k not in ("samples", "all")},
        ensure_ascii=False), flush=True)
    print(f"[style_eval] full -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
