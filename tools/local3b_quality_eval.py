# -*- coding: utf-8 -*-
"""本地小模型 vs DeepSeek 的提取质量对照（P14）。

同一段真实 B 站字幕、同一 prompt、每个后端跑 3 次：
```
把这段视频字幕提炼成 JSON：{"summary":"≤60字","topics":["3-5个"],
"emotion":"整体情绪","worth_sharing":0-1,"reason":"≤30字"}
```
后端：`local3b`（8125 HTTP）、`deepseek`（API）、`transformers2b`（Qwen3.5-2B 4bit 直接推理）。

用法::
    python tools/local3b_quality_eval.py --backend local3b --runs 3
    python tools/local3b_quality_eval.py --backend deepseek --runs 3
    python tools/local3b_quality_eval.py --backend transformers2b --runs 3
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from agi.paths import agent_path
SUBTITLE = agent_path(r"tmp\bili_subtitle_sample.txt")
PROMPT_TEMPLATE = (
    "把下面这段视频字幕提炼成 JSON，只输出 JSON，不要解释：\n"
    '{"summary": "≤60字", "topics": ["3-5个"], "emotion": "整体情绪", '
    '"worth_sharing": 0或1, "reason": "≤30字"}\n\n'
    "字幕：\n{text}"
)
LOCAL3B_URL = "http://127.0.0.1:8125/v1/chat/completions"
LOCAL3B_MODEL = "qwen2.5-3b-instruct"
MODEL_2B = agent_path(r"models\Qwen3.5-2B")


def load_subtitle(max_chars: int = 3200) -> str:
    text = SUBTITLE.read_text(encoding="utf-8").strip()
    return text[: int(max_chars)]


def parse_json_text(raw: str) -> tuple[dict | None, str]:
    value = str(raw or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```[a-zA-Z]*\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    try:
        return json.loads(value), ""
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, re.S)
        if match:
            try:
                return json.loads(match.group(0)), ""
            except json.JSONDecodeError as exc:
                return None, str(exc)
        return None, "no_json"


def call_local3b(prompt: str, max_tokens: int = 400) -> dict:
    body = json.dumps({"model": LOCAL3B_MODEL,
                       "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0.2, "max_tokens": max_tokens,
                       "stream": False}).encode("utf-8")
    request = urllib.request.Request(LOCAL3B_URL, data=body,
                                     headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=300) as response:
        payload = json.loads(response.read().decode("utf-8"))
    elapsed = time.perf_counter() - started
    return {"text": payload["choices"][0]["message"]["content"],
            "elapsed_s": round(elapsed, 2), "usage": payload.get("usage") or {}}


def call_deepseek(prompt: str, max_tokens: int = 400) -> dict:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from agi.selflearn import llm
    client = llm.Client()
    if not client.available():
        raise RuntimeError("DeepSeek client unavailable")
    started = time.perf_counter()
    result = client.chat([{"role": "user", "content": prompt}],
                         purpose="p14_quality", max_tokens=max_tokens,
                         temperature=0.2, json_mode=True)
    elapsed = time.perf_counter() - started
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "deepseek_error"))
    return {"text": result.get("text") or "", "elapsed_s": round(elapsed, 2),
            "usage": result.get("usage") or {}}


class Transformers2B:
    """Qwen3.5-2B 4bit 直接推理（与 8123 0.5B 共存时用 4bit 省显存）。"""

    def __init__(self) -> None:
        from tools.train import smoke_lora
        self.model, self.tokenizer, _ = smoke_lora.load_model_and_tokenizer(
            str(MODEL_2B), load_4bit=True, dtype_name="bf16")
        try:
            smoke_lora.offload_vision_tower(self.model)
        except Exception:  # noqa: BLE001 - 纯文本推理不依赖视觉塔
            pass
        try:
            self.model.to("cuda")
        except Exception:  # noqa: BLE001 - 4bit 模型可能已在正确设备
            pass
        self.model.eval()

    def generate(self, prompt: str, max_tokens: int = 400) -> dict:
        import torch
        messages = [{"role": "user", "content": prompt}]
        text = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True)
        inputs = self.tokenizer(text, return_tensors="pt").to(
            self.model.get_input_embeddings().weight.device)
        started = time.perf_counter()
        with torch.no_grad():
            generated = self.model.generate(
                **inputs, max_new_tokens=max_tokens, do_sample=False,
                pad_token_id=self.tokenizer.eos_token_id)
        elapsed = time.perf_counter() - started
        new_tokens = generated[0][inputs["input_ids"].shape[1]:]
        return {"text": self.tokenizer.decode(new_tokens, skip_special_tokens=True),
                "elapsed_s": round(elapsed, 2),
                "usage": {"completion_tokens": int(new_tokens.shape[0])}}


def run_backend(backend: str, prompt: str, runs: int, max_tokens: int) -> list[dict]:
    results: list[dict] = []
    if backend == "transformers2b":
        engine = Transformers2B()
        caller = engine.generate
    elif backend == "deepseek":
        caller = call_deepseek
    elif backend == "local3b":
        caller = call_local3b
    else:
        raise SystemExit(f"unknown backend: {backend}")
    for index in range(1, runs + 1):
        started = time.perf_counter()
        try:
            response = caller(prompt, max_tokens=max_tokens)
            parsed, error = parse_json_text(response["text"])
            record = {"run": index, "ok": parsed is not None, "error": error,
                      "elapsed_s": response.get("elapsed_s"),
                      "usage": response.get("usage") or {},
                      "raw": response["text"], "parsed": parsed}
        except Exception as exc:  # noqa: BLE001
            record = {"run": index, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                      "elapsed_s": round(time.perf_counter() - started, 2),
                      "raw": "", "parsed": None}
        results.append(record)
        print(f"[{backend}] run {index}/{runs}: ok={record['ok']} "
              f"elapsed={record['elapsed_s']}s", flush=True)
        time.sleep(0.3)
    return results


def summarize(results: list[dict]) -> dict:
    latencies = [r["elapsed_s"] for r in results if r.get("elapsed_s") is not None]
    return {
        "runs": len(results),
        "json_valid": sum(1 for r in results if r["ok"]),
        "json_valid_rate": round(sum(1 for r in results if r["ok"]) / max(1, len(results)), 4),
        "latency_p50": round(statistics.median(latencies), 2) if latencies else None,
        "latency_min": min(latencies) if latencies else None,
        "latency_max": max(latencies) if latencies else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地小模型 vs DeepSeek 提取质量对照")
    parser.add_argument("--backend", choices=("local3b", "deepseek", "transformers2b"),
                        required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=400)
    parser.add_argument("--max-chars", type=int, default=3200)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    subtitle = load_subtitle(args.max_chars)
    prompt = PROMPT_TEMPLATE.replace("{text}", subtitle)
    results = run_backend(args.backend, prompt, args.runs, args.max_tokens)
    payload = {"backend": args.backend, "runs": results,
               "summary": summarize(results),
               "prompt_chars": len(prompt), "subtitle_chars": len(subtitle)}
    out = Path(args.out) if args.out else agent_path(f"logs/local3b_quality_{args.backend}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=1), flush=True)
    print(f"[quality] full -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
