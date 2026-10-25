# -*- coding: utf-8 -*-
"""P2-c QQ 私聊语气语料构建器：qq-bak-chat-history-export 的 *_full.json → qq_corpus.jsonl + qq_profile.md。

输入：`E:\\agent\\qq_export\\c2c\\<peer>_<昵称>_full.json`（每个私聊会话一个 JSON 数组，
      由 `qq-bak-chat-history-export/skills/.../export_ntqq_chat.py --chat-type c2c` 导出）。
输出：
  - `agi/eval_data/human_tone/qq_corpus.jsonl`：字段与微信 corpus 一致
    `{id, source="qq", speaker, text, ts_hint, lang, count}`；direction/is_self=True 为 self。
  - `agi/eval_data/human_tone/qq_profile.md`：self 画像 + 与微信 self 画像的差异 + 合并训练建议
    （文末机器可读 JSON，测试用来复算）。

清洗规则（与微信版一致的口径）：
  - 剔除图片/文件/表情/视频/语音/card/转发等非文本消息；
  - 剔除系统消息（撤回/拍了拍/红包/加好友/入群等）；
  - 长度 > 200 的长文/转发剔除；
  - 同一会话内完全相同文本去重并累计 count；
  - 隐私：QQ 昵称→`朋友Q1..N`、QQ号/手机号→`[号码]`、链接/路径/邮箱→占位、
    真实姓名不落盘（复用 `tools/build_human_tone.py` 的脱敏/扫描函数，保证与微信同一标准）。

用法::

    E:\\agent\\venv312\\Scripts\\python.exe tools/build_qq_tone.py --dry-run
    E:\\agent\\venv312\\Scripts\\python.exe tools/build_qq_tone.py
    # 数据还没导入时只会得到很少的 c2c 消息，profile 会标注"数据不足"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import build_human_tone as ht  # noqa: E402

DEFAULT_EXPORT_DIR = Path(r"E:\agent\qq_export\c2c")
OUT_DIR = ROOT / "agi" / "eval_data" / "human_tone"
WECHAT_PROFILE = OUT_DIR / "profile.md"
CORPUS_NAME = "qq_corpus.jsonl"
PROFILE_NAME = "qq_profile.md"
LABEL_PREFIX = "朋友Q"
MAX_LEN = 200
MIN_SELF_FOR_MERGE = 200   # 与 P2-b 正式训练门槛一致

KIND_PATTERNS = (
    ("image", re.compile(r"^\[(?:图片|image|图像)", re.I)),
    ("file", re.compile(r"^\[(?:文件|file)", re.I)),
    ("emoji", re.compile(r"^\[(?:表情|emoji|动画表情|贴纸|sticker)", re.I)),
    ("video", re.compile(r"^\[(?:视频|video)", re.I)),
    ("voice", re.compile(r"^\[(?:语音|voice)", re.I)),
    ("card", re.compile(r"^\[(?:card|json|xml|链接|分享)", re.I)),
    ("forward", re.compile(r"^\[(?:聊天记录|转发|合并转发)", re.I)),
    ("recall", re.compile(r"^\[(?:撤回|引用|回复)", re.I)),
)
SYSTEM_PATTERNS = (
    "撤回了一条消息", "拍了拍", "以上是打招呼", "你已添加", "红包", "领取了",
    "开启了好友验证", "加入群聊", "退出了群聊", "群公告", "修改群名",
    "消息已发出", "对方拒收", "已读",
)


# --------------------------------------------------------------------------- #
# 解析 / 清洗
# --------------------------------------------------------------------------- #
def load_export(paths: list[Path]) -> list[dict]:
    """读所有 *_full.json，摊平成消息列表（带 session=peer）。"""
    messages: list[dict] = []
    for path in sorted(paths):
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            messages.append(row)
    return messages


def classify_text(text: str) -> tuple[str, str]:
    """返回 (清理后文本, kind)；kind=text 才可用。"""
    value = str(text or "").strip()
    if not value:
        return "", "empty"
    if value.startswith("["):
        for kind, pattern in KIND_PATTERNS:
            if pattern.match(value):
                return "", kind
    if any(pattern in value for pattern in SYSTEM_PATTERNS):
        return "", "system"
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > MAX_LEN:
        return "", "too_long"
    if not ht.CJK_RE.search(value) and value.lower() not in ht.ASCII_ALLOW:
        return "", "non_cjk"
    return value, "text"


def anonymize(text: str, *, label: str = "", chat_name: str = "") -> str:
    """昵称→朋友Qn + 复用微信版号码/链接/路径/邮箱占位，保证两套语料同一隐私标准。"""
    value = str(text or "")
    for name in (chat_name,):
        if name and len(name) >= 2:
            value = value.replace(name, label)
    # 兜底：把任何 2 字以上中文昵称式的 “@某人” 也抹掉
    value = ht.anonymize(value)
    return value.strip()


def build_corpus(messages: list[dict]) -> tuple[list[dict], dict]:
    """消息列表 → (去重后的 corpus, 构建信息)。"""
    by_peer: dict[str, list[dict]] = {}
    for message in messages:
        peer = str(message.get("peer") or message.get("sender_uin") or "")
        if not peer:
            continue
        by_peer.setdefault(peer, []).append(message)

    labels = {peer: f"{LABEL_PREFIX}{index + 1}" for index, peer in enumerate(sorted(by_peer))}
    rows: list[dict] = []
    dropped: Counter = Counter()
    per_session: dict[str, dict] = {}
    for peer, session_messages in by_peer.items():
        label = labels[peer]
        chat_name = str(session_messages[0].get("chat_name") or "")
        self_count = 0
        for message in session_messages:
            speech, kind = classify_text(message.get("text") or "")
            dropped[kind] += 1
            if kind != "text":
                continue
            text = anonymize(speech, label=label, chat_name=chat_name)
            if not text:
                dropped["empty_after_anonymize"] += 1
                continue
            speaker = "self" if message.get("is_self") else "other"
            if speaker == "self":
                self_count += 1
            rows.append({
                "source": "qq",
                "speaker": speaker,
                "text": text,
                "ts_hint": str(message.get("time") or message.get("timestamp") or ""),
                "peer": peer,
            })
        per_session[peer] = {"label": label, "messages": len(session_messages),
                             "self": self_count, "chat_name": label}

    # 去重：相同 speaker+text 只留一条并累计 count（跨会话也合并，与微信 corpus 一致）
    deduped: "OrderedDict[tuple, dict]" = OrderedDict()
    for row in rows:
        key = (row["speaker"], row["text"])
        if key in deduped:
            deduped[key]["count"] += 1
            continue
        deduped[key] = {**row, "count": 1}
    corpus = []
    for index, record in enumerate(deduped.values(), 1):
        corpus.append({
            "id": f"qq-{index:04d}",
            "source": "qq",
            "speaker": record["speaker"],
            "text": record["text"],
            "ts_hint": record["ts_hint"],
            "lang": "zh",
            "count": record["count"],
        })
    info = {
        "sessions": len(by_peer),
        "session_detail": per_session,
        "raw_messages": len(messages),
        "dropped": dict(dropped),
        "corpus_rows": len(corpus),
        "self_rows": sum(1 for row in corpus if row["speaker"] == "self"),
        "other_rows": sum(1 for row in corpus if row["speaker"] == "other"),
        "self_total": sum(row["count"] for row in corpus if row["speaker"] == "self"),
    }
    return corpus, info


# --------------------------------------------------------------------------- #
# 画像
# --------------------------------------------------------------------------- #
def read_wechat_self_stats() -> dict:
    """读微信 profile.md 的机器可读 JSON（没有就返回空 dict）。"""
    if not WECHAT_PROFILE.exists():
        return {}
    text = WECHAT_PROFILE.read_text(encoding="utf-8")
    match = re.search(r"## 8\. 机器可读统计.*?```json\s*(\{.*?\})\s*```", text, re.S)
    if not match:
        return {}
    try:
        profile = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return {
        "self_rows": profile.get("self_rows"),
        "length_median": profile.get("length", {}).get("median"),
        "no_punct_rate": profile.get("ending", {}).get("无标点"),
        "question_rate": profile.get("ending", {}).get("问号"),
        "period_rate": profile.get("ending", {}).get("句号"),
        "emoji_rate": profile.get("emoji_rate"),
    }


def recommend_merge(qq_stats: dict, wechat: dict) -> dict:
    """对比 QQ/微信 self 画像，给出合并训练建议。"""
    if not wechat or qq_stats.get("count", 0) < 20:
        return {"decision": "hold", "reason": "QQ self 样本 <20，先不并入训练；等导入历史消息补量后再评估"}
    median_gap = abs(float(qq_stats["length_median"]) - float(wechat["length_median"]))
    punct_gap = abs(float(qq_stats["no_punct_rate"]) - float(wechat["no_punct_rate"]))
    if median_gap <= 3 and punct_gap <= 0.10:
        return {"decision": "merge", "reason": "长度中位数差 ≤3 字且无标点率差 ≤10pp，可直接合并、按渠道加权"}
    return {
        "decision": "separate_or_weight",
        "reason": (f"长度中位数差 {median_gap:g} 字、无标点率差 {punct_gap*100:g}pp：建议分开训"
                   "（QQ/微信各一个 adapter 或加 channel token）或按渠道加权，别混成一锅"),
    }


def compute_profile(corpus: list[dict], info: dict) -> dict:
    self_texts = [row["text"] for row in corpus if row["speaker"] == "self"]
    stats = ht.basic_text_stats(self_texts)
    lexicon: Counter = Counter()
    for text in self_texts:
        for word in ht.LEXICON:
            hits = text.count(word)
            if hits:
                lexicon[word] += hits
    openings: Counter = Counter(text[:2] for text in self_texts if text)
    wechat = read_wechat_self_stats()
    qq_stats = {
        "count": len(self_texts),
        "self_total": info.get("self_total", 0),
        "length_median": stats["length_median"],
        "length_mean": stats["length_mean"],
        "length_dist": stats["length_dist"],
        "ending": stats["ending_dist"],
        "emoji_rate": stats["emoji_rate"],
        "lexicon_top": lexicon.most_common(20),
        "openings_top": openings.most_common(10),
    }
    comparison = {}
    for key in ("length_median", "no_punct_rate", "question_rate", "period_rate", "emoji_rate"):
        wechat_value = wechat.get(key)
        qq_value = ({"length_median": qq_stats["length_median"],
                     "no_punct_rate": qq_stats["ending"]["无标点"],
                     "question_rate": qq_stats["ending"]["问号"],
                     "period_rate": qq_stats["ending"]["句号"],
                     "emoji_rate": qq_stats["emoji_rate"]}.get(key))
        comparison[key] = {"qq": qq_value, "wechat": wechat_value,
                           "delta": (round(qq_value - wechat_value, 4)
                                     if qq_value is not None and wechat_value is not None else None)}
    self_counts = [item.get("self", 0) for item in info.get("session_detail", {}).values()]
    top_self = max(self_counts) if self_counts else 0
    merged_total = qq_stats["count"] + (wechat.get("self_rows") or 0)
    qq_base = max(qq_stats["count"], top_self)  # 去重后的 count 可能少于含重复的 top_self
    session_concentration = {
        "top_session_self": top_self,
        "top_session_share_of_qq": round(top_self / qq_base, 4) if qq_base else 0.0,
        "top_session_share_of_merged": round(top_self / merged_total, 4) if merged_total else 0.0,
        "merged_self_estimate": merged_total,
        "cap_threshold": 0.40,
        "cap_applied": False,
        "cap_note": (
            f"QQ 只有 1 个有效私聊会话（其余会话没有有效文本），单会话占 QQ self "
            f"{top_self / qq_base:.1%}；但占 QQ+微信合并 self 约 "
            f"{top_self / merged_total:.1%}（< 40% 阈值），所以没有按会话重采样。"
            "建议训练时把 QQ 当独立渠道加权（≤0.4），不要把单好友会话当成全部语气分布。"
        ) if merged_total else "无 self 样本",
    }
    return {
        "generated_by": "tools/build_qq_tone.py",
        "sessions": info.get("sessions", 0),
        "session_detail": info.get("session_detail", {}),
        "raw_messages": info.get("raw_messages", 0),
        "dropped": info.get("dropped", {}),
        "corpus_rows": info.get("corpus_rows", 0),
        "self_rows": qq_stats["count"],
        "self_total": qq_stats["self_total"],
        "other_rows": info.get("other_rows", 0),
        "length": {"median": qq_stats["length_median"], "mean": qq_stats["length_mean"],
                   "dist": qq_stats["length_dist"]},
        "ending": qq_stats["ending"],
        "emoji_rate": qq_stats["emoji_rate"],
        "lexicon_top": qq_stats["lexicon_top"],
        "openings_top": qq_stats["openings_top"],
        "wechat_self": wechat,
        "comparison": comparison,
        "session_concentration": session_concentration,
        "merge_recommendation": recommend_merge(
            {"count": qq_stats["count"], "length_median": qq_stats["length_median"],
             "no_punct_rate": qq_stats["ending"]["无标点"]}, wechat),
        "enough_for_formal_merge": qq_stats["count"] >= MIN_SELF_FOR_MERGE,
        "coverage_note": (
            "数据来自用户「手机→电脑迁移」写进本地 nt_msg.db 的 c2c 历史（time 覆盖 2023-10 至 2024-02+）；"
            "MsgBackup/ 下的手机备份文件用 PC 账号 key 无法解密（手机侧不同 key/参数），未纳入。"
            "c2c 原始 653 条里大部分是图片/表情/卡片，清洗后 641 条进入解析、532 条进 corpus。"
        ),
        "privacy_note": (
            "QQ 昵称一律换成 朋友Q1..N；号码/链接/路径/邮箱复用微信版占位；"
            "密钥与原始库只留在本机工具目录，不进仓库。"
        ),
    }


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "—"


def render_profile_md(profile: dict) -> str:
    lines: list[str] = []
    lines.append("# QQ 私聊语气画像（self 语料）")
    lines.append("")
    lines.append("> 由 `tools/build_qq_tone.py` 从 qq-bak-chat-history-export 的 `*_full.json` 自动生成；"
                 "数字与 `qq_corpus.jsonl` 可复算。")
    lines.append("")
    lines.append("## 1. 语料概况")
    lines.append("")
    lines.append(f"- 私聊会话：**{profile['sessions']} 个**；原始消息 {profile['raw_messages']} 条")
    lines.append(f"- **self 发言：{profile['self_rows']} 条唯一（共 {profile['self_total']} 次）**；"
                 f"other 上下文 {profile['other_rows']} 条")
    lines.append(f"- 丢弃统计：`{json.dumps(profile['dropped'], ensure_ascii=False)}`")
    lines.append(f"- 覆盖说明：{profile['coverage_note']}")
    lines.append("")
    lines.append("## 2. 长度分布（self）")
    lines.append("")
    lines.append(f"- 中位数：**{profile['length']['median']}** 字；均值 {profile['length']['mean']} 字")
    lines.append("")
    lines.append("| 长度（字） | 占比 |")
    lines.append("| --- | --- |")
    for bucket, value in profile["length"]["dist"].items():
        lines.append(f"| {bucket} | {pct(value)} |")
    lines.append("")
    lines.append("## 3. 句尾标点分布（self）★ 关键数字")
    lines.append("")
    lines.append("| 句尾 | 占比 |")
    lines.append("| --- | --- |")
    labels = {"无标点": "无标点", "问号": "问号 ？", "感叹号": "感叹号 ！",
              "句号": "句号 。", "省略号": "省略号 …"}
    for category, value in profile["ending"].items():
        lines.append(f"| {labels.get(category, category)} | {pct(value)} |")
    lines.append("")
    lines.append("## 4. 口语词 Top20（self）")
    lines.append("")
    lines.append("| 词 | 次数 |")
    lines.append("| --- | --- |")
    for word, count in profile["lexicon_top"]:
        lines.append(f"| {word} | {count} |")
    lines.append("")
    lines.append("## 5. 与微信 self 语料的差异")
    lines.append("")
    lines.append("| 指标 | QQ self | 微信 self | 差（QQ-微信） |")
    lines.append("| --- | --- | --- | --- |")
    for key, label in (("length_median", "长度中位数"), ("no_punct_rate", "无标点率"),
                       ("question_rate", "问号率"), ("period_rate", "句号率"),
                       ("emoji_rate", "emoji 使用率")):
        item = profile["comparison"].get(key, {})
        lines.append(f"| {label} | {item.get('qq')} | {item.get('wechat')} | {item.get('delta')} |")
    lines.append("")
    conc = profile.get("session_concentration", {})
    lines.append("**会话集中度 / 重采样**：")
    lines.append("")
    lines.append(f"- 最大会话 self 占 QQ：{conc.get('top_session_share_of_qq')}；"
                 f"占 QQ+微信合并 self：{conc.get('top_session_share_of_merged')}；"
                 f"阈值 {conc.get('cap_threshold')}，是否重采样：{conc.get('cap_applied')}")
    lines.append(f"- {conc.get('cap_note', '')}")
    lines.append("")
    lines.append("**合并训练建议**：" + profile["merge_recommendation"]["reason"])
    lines.append("")
    lines.append(f"- 是否达到正式并入训练门槛（self ≥ {MIN_SELF_FOR_MERGE} 条）："
                 f"**{'是' if profile['enough_for_formal_merge'] else '否'}**")
    lines.append("")
    lines.append("## 6. 数据来源与取舍")
    lines.append("")
    lines.append("- 本机 QQ 本地库 `nt_db`（1024 字节头剥除后 SQLCipher4 解密，key 只留在本机工具目录）")
    lines.append("- 手机备份 `MsgBackup/8F2AB1.../` 4 个文件：PC 账号 key 不适用（手机侧 key/参数），未纳入")
    lines.append("- 剔除：图片/文件/表情/视频/语音/card/系统消息/长度 >200 的转发")
    lines.append("- " + profile["privacy_note"])
    lines.append("")
    lines.append("## 7. 机器可读统计（测试用，勿删）")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(profile, ensure_ascii=False, indent=1, sort_keys=True))
    lines.append("```")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build(export_dir: Path = DEFAULT_EXPORT_DIR, out_dir: Path = OUT_DIR,
          *, dry_run: bool = False, limit: int = 0) -> dict:
    paths = sorted(export_dir.glob("*_full.json"))
    messages = load_export(paths)
    if limit:
        messages = messages[: int(limit)]
    corpus, info = build_corpus(messages)
    profile = compute_profile(corpus, info)
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        corpus_path = out_dir / CORPUS_NAME
        with corpus_path.open("w", encoding="utf-8") as fh:
            for row in corpus:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        (out_dir / PROFILE_NAME).write_text(render_profile_md(profile), encoding="utf-8")
    return {"corpus": corpus, "profile": profile, "info": info,
            "export_files": [path.name for path in paths]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QQ 私聊导出 → 语气语料 + 画像")
    parser.add_argument("--export-dir", default=str(DEFAULT_EXPORT_DIR))
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    parser.add_argument("--dry-run", action="store_true", help="只构建并打印汇总，不写盘")
    parser.add_argument("--limit", type=int, default=0, help="只用前 N 条原始消息（调试用）")
    args = parser.parse_args(argv)
    result = build(Path(args.export_dir), Path(args.out_dir),
                   dry_run=args.dry_run, limit=args.limit)
    profile = result["profile"]
    summary = {
        "export_files": len(result["export_files"]),
        "sessions": profile["sessions"],
        "raw_messages": profile["raw_messages"],
        "corpus_rows": profile["corpus_rows"],
        "self_rows": profile["self_rows"],
        "other_rows": profile["other_rows"],
        "ending": profile["ending"],
        "length_median": profile["length"]["median"],
        "merge_decision": profile["merge_recommendation"]["decision"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if args.dry_run:
        print("[dry-run] 未写盘")
    else:
        print(f"[ok] 写出 {Path(args.out_dir) / CORPUS_NAME} / {Path(args.out_dir) / PROFILE_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
