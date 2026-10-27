# -*- coding: utf-8 -*-
"""20 条端到端验收：llama-server（GGUF）G1 合法率 / 延迟 / tok/s，可选与 HF 同 adapter 对比。

用法::

    E:\\agent\\venv312\\Scripts\\python.exe tools/soul_serve/smoke_eval.py ^
        --gguf E:\\agent\\output\\gguf\\soul_qwen25_05b_v1-q4_k_m.gguf ^
        --limit 20 --ngl 99 --out E:\\agent\\logs\\llama_v1_smoke.json

加 ``--hf-dir E:\\agent\\output\\merged_soul_qwen25_05b_v1`` 会额外做 HF 同 adapter 的
字段一致性抽查（前 --hf-compare 条，默认 5 条，GPU 忙时不要用）。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
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


from agi.inner import schema  # noqa: E402
from agi.selflearn import llm as teacher_llm  # noqa: E402 - parse_json 容错解析
from agi.soul import llama_client  # noqa: E402
from tools.soul_serve.server import start_server, stop_server, wait_healthy  # noqa: E402

DEFAULT_DATA = ROOT / "agi" / "soul" / "data" / "soul_sft_v1_val.jsonl"
COMPARE_FIELDS = ("stance", "intent", "length", "ask", "advice", "emoji")


def load_rows(path: Path, limit: int) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return rows[:max(1, int(limit))]


def row_messages(row: dict) -> list[dict]:
    messages = [{"role": str(c.get("role")), "content": str(c.get("text") or "")}
                for c in row.get("context") or []]
    messages.append({"role": "user", "content": str(row.get("user_msg") or "")})
    return messages


def flatten(target: dict) -> dict:
    behavior = target.get("behavior") or {}
    return {
        "stance": str(target.get("stance") or ""),
        "intent": str(target.get("intent") or ""),
        "length": str(behavior.get("length") or ""),
        "ask": bool(behavior.get("ask")),
        "advice": bool(behavior.get("advice")),
        "emoji": str(behavior.get("emoji") or ""),
    }


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return round(ordered[index], 3)


def run_hf_compare(hf_dir: Path, rows: list[dict], count: int, max_new_tokens: int,
                   device_pref: str = "auto") -> dict:
    """用 transformers 加载合并后的 HF 模型（贪心）跑前 count 条，返回 pred 列表。"""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if device_pref == "cpu":
        device = "cpu"
    elif device_pref == "cuda":
        device = "cuda"
    else:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    print(f"[smoke] HF compare: dir={hf_dir} device={device} dtype={dtype}")
    tokenizer = AutoTokenizer.from_pretrained(str(hf_dir), trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        str(hf_dir), torch_dtype=dtype, device_map=None,
        low_cpu_mem_usage=True, trust_remote_code=True,
    ).to(device).eval()
    predictions: list[dict] = []
    for row in rows[:max(0, count)]:
        messages = row_messages(row)
        text = tokenizer.apply_chat_template(messages, tokenize=False,
                                             add_generation_prompt=True)
        inputs = tokenizer(text, return_tensors="pt").to(device)
        started = time.time()
        with torch.no_grad():
            output = model.generate(**inputs, max_new_tokens=max_new_tokens,
                                    do_sample=False, temperature=None, top_p=None, top_k=None,
                                    pad_token_id=tokenizer.eos_token_id)
        generated = tokenizer.decode(output[0][inputs["input_ids"].shape[1]:],
                                     skip_special_tokens=True)
        predictions.append({"id": row.get("id"), "elapsed_s": round(time.time() - started, 3),
                            "raw": generated})
    del model
    if device == "cuda":
        torch.cuda.empty_cache()
    return {"device": device, "predictions": predictions}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="llama-server end-to-end smoke eval")
    parser.add_argument("--gguf", required=True, help="Q4_K_M GGUF 路径")
    parser.add_argument("--val", default=str(DEFAULT_DATA), help="v1_val JSONL")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--port", type=int, default=8128)
    parser.add_argument("--ngl", type=int, default=99, help="GPU 层数（0=CPU）")
    parser.add_argument("--ctx", type=int, default=2048)
    parser.add_argument("--max-new-tokens", type=int, default=300)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--out", default=str(agent_path(r"logs\llama_smoke.json")))
    parser.add_argument("--log", default=str(agent_path(r"logs\llama_server_smoke.log")))
    parser.add_argument("--hf-dir", default="", help="可选：合并后的 HF 目录，做一致性抽查")
    parser.add_argument("--hf-compare", type=int, default=5)
    parser.add_argument("--hf-device", default="auto", choices=["auto", "cpu", "cuda"],
                        help="HF 对比用哪个设备（auto=cuda 可用则 cuda；CPU 慢但可与训练共存）")
    args = parser.parse_args(argv)

    rows = load_rows(Path(args.val), args.limit)
    proc = start_server(args.gguf, port=args.port, ngl=args.ngl, ctx=args.ctx,
                        log_path=args.log)
    result: dict = {"gguf": args.gguf, "n": len(rows), "ngl": args.ngl,
                    "max_new_tokens": args.max_new_tokens,
                    "temperature": args.temperature}
    try:
        t0 = time.time()
        if not wait_healthy(args.port, timeout_s=300):
            raise SystemExit(f"[smoke] server 未 healthy，见 {args.log}")
        result["server_start_s"] = round(time.time() - t0, 2)
        client = llama_client.make_client(f"http://127.0.0.1:{args.port}",
                                          timeout_s=300)
        details: list[dict] = []
        latencies: list[float] = []
        tok_rates: list[float] = []
        prompt_tokens: list[int] = []
        completion_tokens: list[int] = []
        valid = 0
        for index, row in enumerate(rows, 1):
            messages = row_messages(row)
            started = time.time()
            res = client.chat(messages, purpose="smoke", max_tokens=args.max_new_tokens,
                              temperature=args.temperature, json_mode=True)
            elapsed = time.time() - started
            pred = None
            ok_json = False
            parse_error = ""
            if res.get("ok"):
                try:
                    pred = json.loads(res.get("text") or "")
                    schema.validate(pred)
                    ok_json = True
                except Exception as exc:  # noqa: BLE001
                    parse_error = f"{type(exc).__name__}: {exc}"
            if ok_json:
                valid += 1
            usage = res.get("usage") or {}
            timings = res.get("timings") or {}
            latencies.append(elapsed)
            prompt_tokens.append(int(usage.get("prompt_tokens") or 0))
            completion_tokens.append(int(usage.get("completion_tokens") or 0))
            predicted_per_s = timings.get("predicted_per_second")
            if predicted_per_s:
                tok_rates.append(float(predicted_per_s))
            details.append({
                "id": row.get("id"), "ok": bool(res.get("ok")), "json_valid": ok_json,
                "elapsed_s": round(elapsed, 3), "usage": usage,
                "predicted_per_second": predicted_per_s,
                "raw": res.get("text") or "", "error": res.get("error") or parse_error,
                "target": row.get("target"), "user_msg": row.get("user_msg"),
            })
            print(f"[smoke] {index}/{len(rows)} {row.get('id')} "
                  f"ok={res.get('ok')} json={ok_json} {elapsed:.2f}s "
                  f"{predicted_per_s or '?'} tok/s", flush=True)
        result.update({
            "g1_valid": valid,
            "g1_valid_rate": round(valid / max(1, len(rows)), 4),
            "latency_s": {"mean": round(statistics.mean(latencies), 3),
                          "p50": pct(latencies, 0.5), "p95": pct(latencies, 0.95),
                          "max": round(max(latencies), 3)},
            "tok_per_s": {"mean": round(statistics.mean(tok_rates), 2) if tok_rates else None,
                          "min": round(min(tok_rates), 2) if tok_rates else None},
            "tokens": {"prompt_mean": round(statistics.mean(prompt_tokens), 1),
                       "completion_mean": round(statistics.mean(completion_tokens), 1)},
            "details": details,
        })
    finally:
        stop_server(proc)

    if args.hf_dir:
        hf = run_hf_compare(Path(args.hf_dir), rows, args.hf_compare,
                            args.max_new_tokens, device_pref=args.hf_device)
        compare: list[dict] = []
        for pred, llama in zip(hf["predictions"], result["details"]):
            try:
                hf_target = schema.validate(
                    teacher_llm.parse_json(pred["raw"]) or {}).to_dict()
                hf_flat = flatten(hf_target)
            except Exception:  # noqa: BLE001 - HF 输出不合法也照实记
                hf_flat = {}
            try:
                llama_flat = flatten(json.loads(llama["raw"]))
            except Exception:  # noqa: BLE001
                llama_flat = {}
            row = {"id": pred["id"], "hf_elapsed_s": pred["elapsed_s"],
                   "llama_json_valid": llama["json_valid"],
                   "hf_json_valid": bool(hf_flat), "hf_raw": pred["raw"],
                   "llama_raw": llama["raw"], "fields": {}}
            for field in COMPARE_FIELDS:
                row["fields"][field] = (hf_flat.get(field) == llama_flat.get(field))
            row["match_all"] = all(row["fields"].values())
            compare.append(row)
        result["hf_compare"] = {
            "hf_dir": str(args.hf_dir), "device": hf["device"], "n": len(compare),
            "match_all_rate": round(sum(1 for r in compare if r["match_all"]) /
                                    max(1, len(compare)), 4),
            "details": compare,
        }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = {key: result[key] for key in
               ("n", "g1_valid_rate", "latency_s", "tok_per_s", "tokens")}
    if "hf_compare" in result:
        summary["hf_compare_match_all"] = result["hf_compare"]["match_all_rate"]
    print("[smoke] summary:", json.dumps(summary, ensure_ascii=False))
    print(f"[smoke] full report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
