"""看 v1 训练/验证集里 behavior 字段与 state_profile / bucket 的关系，判断哪些字段是可学的。"""
import json
from collections import Counter, defaultdict

BASE = r"C:\Users\Administrator\Desktop\SoulAgent\agi\soul\data"


def load(name):
    rows = []
    with open(f"{BASE}\\{name}", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def main():
    for name in ("soul_sft_v1_train.jsonl", "soul_sft_v1_val.jsonl"):
        rows = load(name)
        print(f"\n===== {name}: {len(rows)} =====")
        for field in ("length", "advice", "ask", "emoji"):
            by_profile = defaultdict(Counter)
            for r in rows:
                b = (r.get("target") or {}).get("behavior") or {}
                by_profile[r.get("state_profile") or "?"][str(b.get(field))] += 1
            print(f"\n[{field}] by profile:")
            for p in ("default", "work", "low_mood", "tired", "joke", "guarded", "close"):
                c = by_profile.get(p, Counter())
                tot = sum(c.values()) or 1
                print(f"   {p:9s} " + "  ".join(f"{k}={v}({v/tot:.2f})" for k, v in c.most_common()))
        # 稀有类的来源 bucket
        rare_len = [r for r in rows if ((r.get("target") or {}).get("behavior") or {}).get("length") == "normal"]
        rare_adv = [r for r in rows if ((r.get("target") or {}).get("behavior") or {}).get("advice") is True]
        print(f"\nlength=normal: {len(rare_len)} 条, source={Counter(r.get('source') for r in rare_len)}, "
              f"profile={Counter(r.get('state_profile') for r in rare_len)}")
        print(f"advice=true : {len(rare_adv)} 条, source={Counter(r.get('source') for r in rare_adv)}, "
              f"profile={Counter(r.get('state_profile') for r in rare_adv)}")


if __name__ == "__main__":
    main()
