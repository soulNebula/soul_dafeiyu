"""统一口径复算：balanced accuracy（每字段按类别平均 recall），
对比 0.5B 蒸馏模型 vs render_rule 规则基线 vs teacher 目标。

用法: E:\\agent\\venv312\\Scripts\\python.exe tools/train/_compare_baselines.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
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
MODEL_JSON = agent_path(r"logs\eval_v1_llama_05b.json")
FIELDS = ("length", "advice", "ask", "emoji")
PAT = {
    "mood": re.compile(r"mood=([0-9.]+)"), "energy": re.compile(r"energy=([0-9.]+)"),
    "sharing": re.compile(r"sharing=([0-9.]+)"), "battery": re.compile(r"battery=([0-9.]+)"),
    "trust": re.compile(r"trust=([0-9.]+)"), "stage": re.compile(r"stage=(\S+)"),
    "crisis": re.compile(r"crisis=(\w+)"),
}


def state_from_text(text: str) -> dict:
    st = {}
    for k, pat in PAT.items():
        m = pat.search(text or "")
        if not m:
            continue
        v = m.group(1)
        st[k] = v if k == "stage" else (v.lower() == "true" if k == "crisis" else float(v))
    return st


def bal_acc(pairs):
    """pairs: list[(target, pred)]；返回 (balanced_acc, per_class_recall dict)。"""
    rec = defaultdict(lambda: [0, 0])
    for t, p in pairs:
        rec[str(t)][0] += 1
        rec[str(t)][1] += int(str(t) == str(p))
    recalls = {k: v[1] / v[0] for k, v in rec.items()}
    return sum(recalls.values()) / len(recalls), recalls


def main():
    rows = [json.loads(l) for l in VAL.open(encoding="utf-8") if l.strip()]
    model = {}
    if MODEL_JSON.exists():
        rec = json.load(MODEL_JSON.open(encoding="utf-8"))
        for it in rec.get("items") or []:
            if it.get("parsed_ok") and isinstance(it.get("pred"), dict):
                model[it["id"]] = it["pred"]

    targets_r, preds_rule, preds_model = {f: [] for f in FIELDS}, {f: [] for f in FIELDS}, {f: [] for f in FIELDS}
    n_model = 0
    for r in rows:
        ctx = r.get("context") or []
        sys_text = next((c.get("text") for c in ctx if c.get("role") == "system"), "")
        st_dict = state_from_text(sys_text)
        st = schema.from_state({"self": st_dict, "relationship": {"trust": st_dict.get("trust", 0.5)}},
                               float(st_dict.get("trust", 0.5)))
        rb = render.render_rule(st).behavior or {}
        tb = (r.get("target") or {}).get("behavior") or {}
        for f in FIELDS:
            targets_r[f].append(tb.get(f))
            preds_rule[f].append(rb.get(f))
        mp = model.get(r.get("id"))
        if mp:
            n_model += 1
            for f in FIELDS:
                preds_model[f].append((mp.get("behavior") or {}).get(f))
        else:
            for f in FIELDS:
                preds_model[f].append(None)

    print(f"n={len(rows)}，模型覆盖 {n_model}")
    print(f"\n{'字段':8s} {'规则基线':>10s} {'0.5B模型':>10s}  {'规则per-class':>28s}  {'模型per-class':>28s}")
    rule_means, model_means = [], []
    for f in FIELDS:
        ra, rr = bal_acc(list(zip(targets_r[f], preds_rule[f])))
        pairs = [(t, p) for t, p in zip(targets_r[f], preds_model[f]) if p is not None]
        ma, mr = bal_acc(pairs)
        rule_means.append(ra)
        model_means.append(ma)
        print(f"{f:8s} {ra:10.3f} {ma:10.3f}  {str({k: round(v,2) for k,v in rr.items()}):>28s}  {str({k: round(v,2) for k,v in mr.items()}):>28s}")
    print(f"\n{'mean':8s} {sum(rule_means)/4:10.3f} {sum(model_means)/4:10.3f}")


if __name__ == "__main__":
    main()
