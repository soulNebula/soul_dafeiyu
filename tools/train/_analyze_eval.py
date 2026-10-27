"""分析评测结果：看各字段分布、混淆矩阵、以及"多数类基线"，判断是真学不会还是偏置问题。"""
import json
from collections import Counter, defaultdict
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[2]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from agi.paths import agent_path


EVAL = str(agent_path(r"logs\eval_v1_llama_05b.json"))
DATA = r"C:\Users\Administrator\Desktop\SoulAgent\agi\soul\data\soul_sft_v1_val.jsonl"


def main():
    rec = json.load(open(EVAL, encoding="utf-8"))
    items = rec.get("items") or rec.get("records") or []
    rows = {json.loads(l)["id"]: json.loads(l)
            for l in open(DATA, encoding="utf-8") if l.strip()}
    print("eval items", len(items))

    # 只看跑成功的
    ok = [it for it in items if it.get("parsed_ok") and isinstance(it.get("pred"), dict)]
    print("parsed ok", len(ok))

    dist = defaultdict(Counter)
    for it in ok:
        t = (rows.get(it.get("id")) or {}).get("target") or {}
        b = t.get("behavior") or {}
        for k in ("length", "ask", "advice", "emoji"):
            dist[k][str(b.get(k))] += 1
    for k, c in dist.items():
        total = sum(c.values())
        top = c.most_common(1)[0]
        print(f"[target dist] {k}: {dict(c)}  majority={top[0]} {top[1]}/{total}={top[1]/total:.3f}")

    # 混淆矩阵 + 多数类基线
    for k in ("length", "ask", "advice", "emoji"):
        cm = defaultdict(Counter)
        for it in ok:
            t = (rows.get(it.get("id")) or {}).get("target") or {}
            tb = (t.get("behavior") or {}).get(k)
            pb = (it.get("pred") or {}).get("behavior", {}) if isinstance(it.get("pred"), dict) else {}
            pb = (pb or {}).get(k)
            cm[str(tb)][str(pb)] += 1
        correct = sum(v[t_] for t_, row in cm.items() for v in [row] if False)  # placeholder
        total = sum(sum(row.values()) for row in cm.values())
        right = sum(cm[lab][lab] for lab in cm)
        print(f"\n[{k}] acc={right/total:.3f} (n={total})  confusion target->pred:")
        for lab, row in sorted(cm.items()):
            print("   ", lab, dict(row))


if __name__ == "__main__":
    main()
