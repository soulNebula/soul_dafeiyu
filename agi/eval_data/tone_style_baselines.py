# -*- coding: utf-8 -*-
"""线 B 对照基线评测（C 纯规则 / D DeepSeek+few-shot / 0.5B v3 / 1.5B v3）。

统一口径（同一批 20 个 held-out 场景、同一个 DeepSeek AI 原文）：
- 原意≥4 通过率、关键信息保留率（anchors）、幻觉率、风格 pairwise、回退率、
  含回退后的最终质量（原意通过且 anchors 全保留）。
- C/D 不训练、不占 GPU；adapter 阶段需要 GPU（等 1.5B 直生/改写训练结束再跑）。

用法：
    # 先跑 C/D（API/CPU）
    python agi/eval_data/tone_style_baselines.py --stage cd
    # 训练结束后跑 adapter 两个（GPU）
    python agi/eval_data/tone_style_baselines.py --stage adapters
    # 出总表
    python agi/eval_data/tone_style_baselines.py --stage table
"""
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
    build_ai_open_messages, build_messages, build_ai_persona_system,
    extract_anchors,
)

DATA_PATH = ROOT / "agi" / "eval_data" / "human_tone" / "style_sft_ai.jsonl"
SCENARIO_PATH = Path(r"E:\agent\logs\style_baselines_scenarios.json")
CD_PATH = Path(r"E:\agent\logs\style_baselines_cd.json")
ADAPTER_PATH = Path(r"E:\agent\logs\style_baselines_adapters.json")
TABLE_PATH = Path(r"E:\agent\logs\style_baselines_table.md")
FEWSHOT_PATH = ROOT / "agi" / "eval_data" / "human_tone" / "fewshot.json"
DEFAULT_MODEL_05B = Path(r"E:\agent\models\Qwen2.5-0.5B-Instruct")
DEFAULT_MODEL_15B = Path(r"E:\agent\models\Qwen2.5-1.5B-Instruct")
N_SCENARIOS = 20


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


# --------------------------------------------------------------------------- #
# 场景准备（C/D/adapter 共用同一批 + 同一个 AI 原文）
# --------------------------------------------------------------------------- #
def prepare_scenarios(client) -> list[dict]:
    if SCENARIO_PATH.exists():
        cached = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
        if cached:
            print(f"[baseline] 复用 {len(cached)} 个场景缓存", flush=True)
            return cached
    rows = [row for row in load_rows() if row.get("split") == "val" and row.get("ai_input")]
    rng = random.Random(42)
    rng.shuffle(rows)
    chosen = rows[:N_SCENARIOS]
    scenarios = []
    for index, row in enumerate(chosen, 1):
        messages = build_ai_open_messages(row["context"])
        ai_input = ""
        for _ in range(3):
            result = client.chat(messages, purpose="style_ai_open",
                                 max_tokens=200, temperature=0.45, json_mode=True)
            data = parse_json(str(result.get("text") or ""))
            ai_input = str(data.get("ai_input") or "").strip()
            if ai_input:
                break
            time.sleep(0.3)
        if not ai_input:
            continue
        scenarios.append({"id": row["id"], "context": row["context"],
                          "ai_input": ai_input, "real_original": row["original"]})
        if index % 5 == 0:
            print(f"[baseline] 场景 {index}/{len(chosen)}", flush=True)
    SCENARIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCENARIO_PATH.write_text(json.dumps(scenarios, ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8")
    return scenarios


# --------------------------------------------------------------------------- #
# 基线 C：纯规则口语化（内容词零改动）
# --------------------------------------------------------------------------- #
_REPLACEMENTS = (("然而", "但是"), ("因此", "所以"), ("非常", "挺"),
                 ("我认为", "我觉得"), ("此外", "另外"), ("并且", "而且"))
_SPLIT_RE = re.compile(r"(?<=[，,。！？!?；;])")


def baseline_c(text: str, rng: random.Random) -> str:
    value = str(text or "").strip()
    value = re.sub(r"[。]+$", "", value)
    for source, target in _REPLACEMENTS:
        value = value.replace(source, target)
    if len(value) > 25:
        parts = [part for part in _SPLIT_RE.split(value) if part.strip()]
        value = "".join(parts[:2]).rstrip("，,。；;")
    if rng.random() < 0.07:
        value = value + rng.choice(["吧", "啊", "呢", "呀"])
    return value.strip()


# --------------------------------------------------------------------------- #
# 基线 D：DeepSeek + 真实 few-shot 直接生成
# --------------------------------------------------------------------------- #
def fewshot_system_prompt() -> str:
    payload = json.loads(FEWSHOT_PATH.read_text(encoding="utf-8"))
    examples = [str(item.get("text") or "") for item in payload.get("examples") or []]
    lines = [build_ai_persona_system(), ""]
    lines.append("参考你平时真实发过的消息（只学语气、长度、标点，不要照抄内容）：")
    lines.extend(f"- {text}" for text in examples)
    lines.append("")
    lines.append("像微信聊天：短、少问、结尾不加句号、可以用语气词。")
    return "\n".join(lines)


def baseline_d(client, context: list[dict], system: str) -> str:
    messages = [{"role": "system", "content": system}]
    messages.extend(context[-4:])
    messages.append({"role": "user",
                     "content": "根据上面的聊天上下文，用你平时的方式发一条微信消息"
                                "（≤30 字、一句话；只输出 JSON：{\"ai_input\": \"...\"}）。"})
    for _ in range(3):
        result = client.chat(messages, purpose="style_baseline_d",
                             max_tokens=200, temperature=0.5, json_mode=True)
        data = parse_json(str(result.get("text") or ""))
        text = str(data.get("ai_input") or "").strip()
        if text:
            return text
        time.sleep(0.3)
    return ""


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #
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
    prompt = ("用户真实原话：{human}\n模型改写：{model}\n\n"
              "哪条更像用户本人平时在微信/QQ 里的说话方式（长度、标点、口语习惯）？\n"
              "只回答：A（真实原话）、B（模型改写）或 tie。").format(human=a, model=b)
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


def summarize(results: list[dict]) -> dict:
    n = len(results) or 1
    meanings = [r["same_meaning"] for r in results]
    anchor_total = sum(len(r["anchors"]) for r in results)
    anchor_kept = sum(sum(1 for a in r["anchors"] if a in r["output"]) for r in results)
    paired = {"model": 0, "human": 0, "tie": 0}
    for r in results:
        paired[r["pairwise"]] += 1
    return {
        "n": len(results),
        "meaning_pass_rate": round(sum(1 for s in meanings if s >= 4) / n, 4),
        "meaning_mean": round(sum(meanings) / n, 2),
        "anchor_retention_rate": round(anchor_kept / anchor_total, 4) if anchor_total else 1.0,
        "anchor_all_kept_rate": round(
            sum(1 for r in results if all(a in r["output"] for a in r["anchors"])) / n, 4),
        "hallucination_rate": round(sum(1 for r in results if r["new_facts"]) / n, 4),
        "pairwise_model_win_rate": round(paired["model"] / n, 4),
        "pairwise_human_win_rate": round(paired["human"] / n, 4),
        "fallback_rate": round(sum(1 for r in results if r["fallback"]) / n, 4),
        "final_quality": round(sum(
            1 for r in results
            if r["same_meaning"] >= 4 and all(a in r["output"] for a in r["anchors"])
        ) / n, 4),
        "samples": results[:1],
        "all": results,
    }


def evaluate(scenarios: list[dict], client, output_fn, *, seed: int = 42) -> dict:
    rng = random.Random(seed)
    results = []
    for index, scenario in enumerate(scenarios, 1):
        context = scenario["context"]
        ai_input = scenario["ai_input"]
        context_text = " ".join(str(m.get("content") or "") for m in context)
        anchors = extract_anchors(ai_input, context_text)
        outcome = output_fn(context, ai_input)
        output = outcome["output"]
        judged = judge(client, ai_input, output)
        verdict = pairwise(client, scenario["real_original"], output, rng)
        results.append({
            "id": scenario["id"], "context": context[-2:], "ai_input": ai_input,
            "anchors": anchors, "output": output,
            "fallback": outcome.get("fallback", False),
            "same_meaning": judged["same_meaning"], "new_facts": judged["new_facts"],
            "note": judged["note"], "pairwise": verdict,
            "real_original": scenario["real_original"],
        })
        if index % 5 == 0:
            print(f"[baseline] evaluated {index}/{len(scenarios)}", flush=True)
    return summarize(results)


def run_cd() -> dict:
    from agi.selflearn import llm
    client = llm.Client()
    if not client.available():
        raise SystemExit("[baseline] 没有 DeepSeek key")
    scenarios = prepare_scenarios(client)
    rng = random.Random(42)
    system = fewshot_system_prompt()
    payload = {}
    payload["C"] = evaluate(scenarios, client,
                            lambda context, ai: {"output": baseline_c(ai, rng),
                                                 "fallback": False})
    payload["D"] = evaluate(scenarios, client,
                            lambda context, ai: {"output": baseline_d(client, context, system),
                                                 "fallback": False})
    CD_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
    print("[baseline] CD saved:", CD_PATH)
    return payload


# --------------------------------------------------------------------------- #
# Adapter 阶段（需要 GPU；可分别跑 0.5B / 1.5B）
# --------------------------------------------------------------------------- #
def load_adapter(model_path: Path, adapter: Path):
    from peft import PeftModel

    from tools.train import smoke_lora

    model, tokenizer, _ = smoke_lora.load_model_and_tokenizer(
        str(model_path), load_4bit=False, dtype_name="bf16")
    model = PeftModel.from_pretrained(model, str(adapter))
    model.eval()
    return model, tokenizer


def generate(model, tokenizer, messages, max_new_tokens=64) -> str:
    import torch

    device = model.get_input_embeddings().weight.device
    text = tokenizer.apply_chat_template(messages, tokenize=False,
                                         add_generation_prompt=True)
    inputs = tokenizer(text, return_tensors="pt").to(device)
    with torch.no_grad():
        generated = model.generate(**inputs, max_new_tokens=max_new_tokens,
                                   do_sample=False, pad_token_id=tokenizer.eos_token_id)
    return tokenizer.decode(generated[0][inputs["input_ids"].shape[1]:],
                            skip_special_tokens=True).strip()


def guarded_output(model, tokenizer, context, ai_input, anchors) -> dict:
    first = generate(model, tokenizer, build_messages(context, ai_input, anchors))
    missing = [anchor for anchor in anchors if anchor not in first]
    fallback = False
    output = first
    if missing:
        retry_prompt = ai_input + ("\n\n（改写必须包含这些信息：" + "、".join(missing) + "）")
        retry = generate(model, tokenizer, build_messages(context, retry_prompt, anchors))
        if any(anchor not in retry for anchor in anchors):
            fallback = True
            output = ai_input
        else:
            output = retry
    return {"output": output, "fallback": fallback, "first_output": first}


def run_adapters() -> dict:
    from agi.selflearn import llm
    client = llm.Client()
    if not client.available():
        raise SystemExit("[baseline] 没有 DeepSeek key")
    scenarios = prepare_scenarios(client)
    payload = {}
    for name, model_path, adapter in (
        ("0.5B_v3", DEFAULT_MODEL_05B, Path(r"E:\agent\output\tone_style_qwen05_v3")),
        ("1.5B_v3", DEFAULT_MODEL_15B, Path(r"E:\agent\output\tone_style_qwen15_v3")),
    ):
        if not adapter.exists():
            print(f"[baseline] 跳过 {name}：adapter 不存在 {adapter}", flush=True)
            continue
        print(f"[baseline] 加载 {name}: {adapter}", flush=True)
        model, tokenizer = load_adapter(model_path, adapter)
        payload[name] = evaluate(
            scenarios, client,
            lambda context, ai, m=model, t=tokenizer: guarded_output(m, t, context, ai,
                                                                     extract_anchors(
                                                                         ai,
                                                                         " ".join(
                                                                             str(x.get("content") or "")
                                                                             for x in context)))
        )
        del model, tokenizer
    ADAPTER_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
    print("[baseline] adapters saved:", ADAPTER_PATH)
    return payload


# --------------------------------------------------------------------------- #
# 总表
# --------------------------------------------------------------------------- #
def render_table() -> str:
    methods = {}
    if CD_PATH.exists():
        methods.update(json.loads(CD_PATH.read_text(encoding="utf-8")))
    if ADAPTER_PATH.exists():
        methods.update(json.loads(ADAPTER_PATH.read_text(encoding="utf-8")))
    lines = ["# 线 B 对照总表：C / D / 0.5B v3 / 1.5B v3", ""]
    lines.append("| 方案 | 原意≥4 | 锚点保留 | 幻觉率 | 风格 pairwise | 回退率 | 含回退最终质量 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for name in ("C", "D", "0.5B_v3", "1.5B_v3"):
        item = methods.get(name)
        if not item:
            lines.append(f"| {name} | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {name} | {item['meaning_pass_rate']*100:.1f}% | "
            f"{item['anchor_retention_rate']*100:.1f}% | "
            f"{item['hallucination_rate']*100:.1f}% | "
            f"{item['pairwise_model_win_rate']*100:.1f}% | "
            f"{item['fallback_rate']*100:.1f}% | "
            f"{item['final_quality']*100:.1f}% |")
    lines.append("")
    for name in ("C", "D", "0.5B_v3", "1.5B_v3"):
        item = methods.get(name)
        if not item:
            continue
        sample = item["samples"][0]
        context = " / ".join(f"{'对方' if m['role'] == 'user' else '我'}：{m['content']}"
                             for m in sample["context"])
        lines.append(f"**{name} 样例**：ctx「{context}」｜AI「{sample['ai_input']}」"
                     f"｜输出「{sample['output']}」｜真人原话「{sample['real_original']}」")
        lines.append("")
    TABLE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="线 B 对照基线 C/D + adapter 总表")
    parser.add_argument("--stage", choices=("cd", "adapters", "table", "all"),
                        default="cd")
    args = parser.parse_args(argv)
    if args.stage in ("cd", "all"):
        run_cd()
    if args.stage in ("adapters", "all"):
        run_adapters()
    if args.stage in ("table", "all"):
        render_table()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
