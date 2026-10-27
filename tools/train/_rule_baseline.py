"""规则基线：用 agi/inner/render.render_rule 在同一批 val 行上算 behavior 指标，
和蒸馏模型对比——回答"小模型到底比现成规则强多少"。

状态从数据集 context[0].text 里反解（"当前状态：mood=0.55 energy=0.60 …"）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[2]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from agi.paths import agent_path


from agi.inner import render, schema  # noqa: E402

VAL = ROOT / "agi" / "soul" / "data" / "soul_sft_v1_val.jsonl"
SIZE = agent_path(r"logs\eval_v1_llama_05b.json")

PAT = {
    "mood": re.compile(r"mood=([0-9.]+)"),
    "energy": re.compile(r"energy=([0-9.]+)"),
    "sharing": re.compile(r"sharing=([0-9.]+)"),
    "battery": re.compile(r"battery=([0-9.]+)"),
    "trust": re.compile(r"trust=([0-9.]+)"),
    "stage": re.compile(r"stage=(\S+)"),
    "crisis": re.compile(r"crisis=(\w+)"),
}


def state_from_text(text: str) -> dict:
    st = {}
    for k, pat in PAT.items():
        m = pat.search(text or "")
        if not m:
            continue
        v = m.group(1)
        if k == "stage":
            st[k] = v
        elif k == "crisis":
            st[k] = v.lower() == "true"
        else:
            st[k] = float(v)
    return st


def main():
    rows = [json.loads(l) for l in VAL.open(encoding="utf-8") if l.strip()]
    model_pred = {}
    if SIZE.exists():
        rec = json.load(SIZE.open(encoding="utf-8"))
        for it in rec.get("items") or []:
            if it.get("parsed_ok") and isinstance(it.get("pred"), dict):
                model_pred[it.get("id")] = it["pred"]

    stats = {"ask": [0, 0], "emoji": [0, 0], "length": [0, 0], "advice": [0, 0]}
    per_class = {"length": {}, "advice": {}, "ask": {}, "emoji": {}}
    model_same = {"ask": 0, "emoji": 0, "length": 0, "advice": 0, "n": 0}

    for r in rows:
        ctx = r.get("context") or []
        sys_text = next((c.get("text") for c in ctx if c.get("role") == "system"), "")
        st = schema.from_state({"self": state_from_text(sys_text),
                                "relationship": {"trust": state_from_text(sys_text).get("trust", 0.5)}},
                               float(state_from_text(sys_text).get("trust", 0.5)))
        rule_st = render.render_rule(st)
        tgt = (r.get("target") or {}).get("behavior") or {}
        rb = rule_st.behavior or {}
        for k in stats:
            tv = tgt.get(k)
            pv = rb.get(k)
            n_t = per_class[k].setdefault(str(tv), [0, 0])
            n_t[0] += 1
            hit = str(tv) == str(pv)
            if hit:
                n_t[1] += 1
            stats[k][0] += 1
            stats[k][1] += int(hit)
        mp = model_pred.get(r.get("id"))
        if mp:
            model_same["n"] += 1
            for k in ("ask", "emoji", "length", "advice"):
                if str((mp.get("behavior") or {}).get(k)) == str(rb.get(k)):
                    model_same[k] += 1

    print("=== 规则基线（render_rule）vs teacher 目标，val 393 条 ===")
    for k, (n, hit) in stats.items():
        print(f"{k:7s} acc={hit / max(1, n):.3f}  (n={n})   per-class target->recall: "
              + "  ".join(f"{lab}:{c[1]}/{c[0]}" for lab, c in per_class[k].items()))
    if model_same["n"]:
        n = model_same["n"]
        print("\n=== 规则 vs 0.5B 模型预测的一致率 ===")
        for k in ("ask", "emoji", "length", "advice"):
            print(f"{k:7s} {model_same[k]}/{n} = {model_same[k] / n:.3f}")


if __name__ == "__main__":
    main()
