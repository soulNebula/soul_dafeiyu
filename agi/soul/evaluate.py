# -*- coding: utf-8 -*-
"""W4-C1 灵魂模型评测：G1 格式合法率 + G2 与 teacher 逐字段一致率。

测什么
------
模型（base 或 base+LoRA adapter）读「系统提示(含当前状态) + 最近对话 + 用户这句话」，
应该输出一个 InnerState JSON。评测两道门：

* **G1 格式**：输出能否 ``json.loads`` 且通过 ``agi.inner.schema.validate``；
  失败按 ``empty / truncated / not_json / schema_invalid`` 分类取 top。
* **G2 一致**：与数据集 ``target``（DeepSeek teacher 的标注）逐字段比：
  - ``behavior.length / ask / advice / emoji``：归一化后精确匹配（emoji 中文枚举
    允许 rare/occasionally/none/no 等别名）；
  - ``stance / intent``：严格精确 + 宽松两档（中文 bigram/英文词集合重合 ≥50% 记一致）；
  - 汇总：每字段一致率 + **五字段全对率**（stance+intent+length+ask+advice，
    不含 emoji——emoji 是噪声最大的一维）+ 仅 behavior 四字段全对率（另附六字段全对率）。

命令行
------
::

    E:\\agent\\venv312\\Scripts\\python.exe -m agi.soul.evaluate \
      --data agi/soul/data/soul_sft_v0_val.jsonl \
      --adapter E:\\agent\\output\\smoke_adapter \
      --limit 20 --out E:\\agent\\logs\\eval_v0_smoke.json

    # llama-server 全量（快 12 倍；adapter 已合并进 GGUF，不需要 --adapter）
    E:\\agent\\venv312\\Scripts\\python.exe -m agi.soul.evaluate \
      --data agi/soul/data/soul_sft_v1_val.jsonl --backend llama \
      --base-url http://127.0.0.1:8123 --limit 0 \
      --out E:\\agent\\logs\\eval_v1_llama_05b.json

* ``--adapter`` 省略 = 只评 base 模型（对照）；
* ``--limit 0`` = 全部（v1 正式评估用这个）；
* ``--temperature 0.0``（默认贪心，可复现）；
* ``--resume`` 断点续跑：读 ``--out`` 里已有 items、按 id 跳过，长跑中断/超时后接着跑；
  ``--checkpoint-every 20`` 每 20 条落盘一次，1.8h 级全量评估建议一起用；
* ``--retry-errors`` 配合 ``--resume``：把 ``client_error`` 的旧记录丢掉重跑
  （server 中途重启/超时时用；not_json/truncated 属于模型真实输出，不重跑）；
* ``--rule-baseline``：同一批 val 上跑 ``agi/inner/render.render_rule`` 的规则基线，
  输出 ``rule_baseline{length_bal, advice_bal, ask_bal, emoji_bal, mean, n, skipped}``，
  每次评测自动带「模型 vs 手写规则」对照；
* ``--data`` 参数化：v1 落盘后直接换成 ``soul_sft_v1_val.jsonl`` 即可；
* 数据行若有现成 ``prompt`` 字段（v1 之后可能会有）优先用它，否则按
  ``tools/train/smoke_lora.py`` 的 chat 构造方式重建（测试锁住两者一致）。

两种推理后端
-------------
* ``--backend hf``（默认）：本地 transformers 加载 2B/adapter，慢（HF/Python 每 token
  约 40ms 固定开销，实测 2B ~8 tok/s、0.5B ~10–13 tok/s），但显存/精度可完全控制。
* ``--backend llama --base-url http://127.0.0.1:8123``：走 task-12 的
  ``agi/soul/llama_client.py``（**不自己写 HTTP**）请求 llama-server + GGUF Q4。
  实测 llama.cpp 124.9 tok/s（HF 9–12 tok/s），全量 393 条从 82 分钟压到 ~8 分钟。
  该模式下 prompt **仍然**由 ``build_messages`` 构造、以 messages 列表交给
  llama-server 的 ``--jinja`` 渲染（不在本地套 chat_template）；JSON 约束由 client 的
  ``json_mode=True`` 负责——client 内部发**请求级** ``response_format={"type":
  "json_schema",...}``，server 启动只带 ``--jinja``、**不要带 ``-jf``**（b11857 服务端
  -jf 在 Qwen2.5 模板下会 400，见 ``tools/soul_serve/README.md``）。
  不加载 torch/权重，``peak_alloc_gb`` 记为 null。
  失败自动重试 ``--retries`` 次（``--timeout-s`` 控制单条超时），仍失败记
  ``fail=client_error`` 并继续跑剩余样本；断点续跑 ``--resume`` 同样支持。

HF 后端实现细节
----------------
刻意与 ``tools/train/smoke_lora.py`` 对齐（该脚本是训练分布）：``AutoModelForImageTextToText``
加载 Qwen3.5-2B、bf16、``device_map={"":0}``、``enable_thinking=False``、贪心、
``max_new_tokens=300``。加载逻辑是本文件自带的最小实现（不 import ``tools/``，避免跨目录依赖），
一致性由 ``agi/tests/test_soul_eval.py`` 锁住。

**视觉塔默认保留在 GPU**（推理没有反向/没有 grad checkpoint，4.12GB 权重 + 短 KV cache
在 5.6GB 可用显存里够用）。加 ``--offload-vision`` 才会把视觉塔**从模型里删掉**（不是挪 CPU）：
挪 CPU 会让 ``model.device`` 变成 cpu，``generate()`` 会把 input_ids 也送到 cpu，而文本层
embedding 在 cuda → 报 ``Expected all tensors to be on the same device``（2026-10-25 实测踩过）。
删掉视觉塔后 ``model.device`` 仍是 cuda，文本前向也不会碰它，显存还能再省 ~0.6GB。

不变量（改 prompt 前必读）
--------------------------
``evaluate.build_messages`` 必须与 ``tools/train/smoke_lora.py::build_messages`` **逐字节一致**；
``build_prompt_text`` 的 token 序列必须等于 smoke ``encode_sample`` 的 prompt 前缀
（同一 tokenizer、``enable_thinking=False``、``add_generation_prompt=True``）。
**训练和评测的 prompt 一旦不一致，G2/G1 数字就没有意义。**
2026-10-25 已实测：v0_val 全部 29 行逐 token 相同（prompt 113–167 token）；
``agi/tests/test_soul_eval.py`` 锁住这两条，谁改 prompt 谁会先看到测试红。

G2 判定口径（写进 summary，避免各人理解不同）
---------------------------------------------
* **主口径 = ``G2.balanced``**：每个字段先算 **balanced accuracy**（该字段各类 recall
  的算术平均，缺失类别跳过），length/advice/ask/emoji 四个字段再取平均。
  - length/advice 也按两类平均：永远输出多数类或永远输出少数类都只有 0.5，
    不会像"少数类 recall"那样把偏科模型算成满分；
  - ``*_minority_recall`` 仅作附加信息，不参与 mean；
  - ``majority_baselines`` 给各字段"全预测多数类"能拿到的分。
  val 里 length=short 376/393、advice=false 376/393 —— behavior_only 会被多数类撑高。
* **``--rule-baseline``**：用 ``agi/inner/render.render_rule`` 在同一批 val 上算同口径
  balanced accuracy（状态从 context[0] 的 system 文本反解），汇总输出 ``rule_baseline``，
  用于「蒸馏模型 vs 手写规则」对照；单条规则失败跳过并计数。
* ``behavior_only``（四字段全对率）兼容保留，**仅作参考**（受类别不平衡影响）；
* **stance / intent**：``strict``（精确）只作参考，``loose``（关键词重合 ≥50%）是主口径；
* ``all5`` = stance_strict + intent_strict + length + ask + advice（不含 emoji，emoji 噪声最大）；
* ``all6`` = 上述 5 项 + emoji，仅作附加参考。

显存
----
6GB 卡上顺序推理，单条 batch=1；每 ``--empty-cache-every`` 条清一次 cache，
summary 里报 torch 的 peak allocated/reserved，便于和训练 smoke 对比。

``--dry-run``：只做参数/数据/adapter 路径校验 + 加载 tokenizer 构造 3 条 prompt，
**不加载模型权重**（可以安全地在训练占卡时跑），用于检查 base/adapter 两条代码路径。

退出码：0 正常；2 参数/数据问题；3 CUDA 不可用；4 OOM（会尽量把已跑出的部分结果落盘）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HOME_DIR = Path(os.environ.get("SOUL_TRAIN_HOME", r"E:\agent"))
DEFAULT_MODEL_PATH = HOME_DIR / "models" / "Qwen3.5-2B"
DEFAULT_DATA = ROOT / "agi" / "soul" / "data" / "soul_sft_v0_val.jsonl"
DEFAULT_OUT = HOME_DIR / "logs" / "eval_output.json"
DEFAULT_SYSTEM = (
    "你是微信里的一个 AI 朋友。语气平和，不讨好、不客服腔；先接住对方的话，"
    "再决定要不要追问；不列清单、不说教；有情绪或危机信号时以对方安全为先。"
)

MAX_NEW_TOKENS = 300
BEHAVIOR_FIELDS = ("length", "ask", "advice", "emoji")
#: 五字段全对：teacher 的「语气/意图 + 三个行为开关」，emoji 单列（噪声大）
ALL5_FIELDS = ("stance_strict", "intent_strict", "length", "ask", "advice")
FIELD_RATE_KEYS = ("length", "ask", "advice", "emoji",
                   "stance_strict", "stance_loose", "intent_strict", "intent_loose")

#: G2 判定口径（写进 summary JSON + stdout，避免"各人理解不同"）
G2_CRITERIA = {
    "primary_metric": "balanced",
    "primary_note": "主口径 = G2.balanced：每个字段先算 balanced accuracy（该字段各类 recall "
                    "的算术平均，缺失类别跳过），length/advice/ask/emoji 四个字段再取平均。"
                    "length/advice 也按两类平均，所以'永远输出多数类'或'永远输出少数类'都只有 0.5，"
                    "不会像少数类 recall 那样把偏科模型算高分",
    "legacy_metric": "behavior_only",
    "legacy_note": "behavior_only = behavior 四字段全对率，保留兼容；受类别不平衡影响，"
                   "仅作参考（全预测多数类也能拿到 ~0.86）",
    "rule_baseline_note": "--rule-baseline 用 agi/inner/render.render_rule 在同一批 val 上算同口径 "
                          "balanced accuracy，输出到 rule_baseline，用于'模型 vs 手写规则'对照",
    "hard_fields": list(BEHAVIOR_FIELDS),
    "stance_intent_primary": "loose",
    "stance_intent_reference": "strict",
    "stance_intent_note": "strict 精确匹配只作参考；loose（关键词重合≥50%）为主口径",
    "all5_fields": list(ALL5_FIELDS),
    "all5_note": "all5 = stance_strict + intent_strict + length + ask + advice（不含 emoji）",
    "all6_note": "all6 = all5 + emoji，仅附加参考（emoji 枚举噪声最大）",
}

#: balanced 主口径用的四个行为字段；四项各算 balanced accuracy 再取平均
BALANCED_FIELDS = ("length", "advice", "ask", "emoji")
RARE_LABELS = {"length": "normal", "advice": True, "ask": None, "emoji": None}

#: 规则基线：从 val 行 context[0] system 文本反解当前状态（与 tools/train/_compare_baselines.py 同口径）
_STATE_PATTERNS = {
    "mood": re.compile(r"mood=([0-9.]+)"),
    "energy": re.compile(r"energy=([0-9.]+)"),
    "sharing": re.compile(r"sharing=([0-9.]+)"),
    "battery": re.compile(r"battery=([0-9.]+)"),
    "trust": re.compile(r"trust=([0-9.]+)"),
    "stage": re.compile(r"stage=(\S+)"),
    "crisis": re.compile(r"crisis=(\w+)"),
}


_LENGTH_ALIASES = {
    "short": "short", "brief": "short", "短": "short", "短句": "short", "s": "short",
    "normal": "normal", "medium": "normal", "mid": "normal", "正常": "normal", "m": "normal",
    "long": "long", "长": "long", "长句": "long", "l": "long",
}
_EMOJI_ALIASES = {
    "偶尔": "偶尔", "少量": "偶尔", "有": "偶尔", "yes": "偶尔", "true": "偶尔",
    "rare": "偶尔", "rarely": "偶尔", "occasional": "偶尔", "occasionally": "偶尔",
    "sometimes": "偶尔", "often": "偶尔",
    "不用": "不用", "无": "不用", "no": "不用", "false": "不用", "none": "不用",
    "never": "不用", "empty": "不用", "不使用": "不用", "": "不用",
}
_TRUE_WORDS = {"true", "1", "yes", "y", "on", "是", "要", "可以", "用", "true."}
_FALSE_WORDS = {"false", "0", "no", "n", "off", "否", "不要", "不用", "不"}
_WS = re.compile(r"\s+")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[A-Za-z0-9_]+")
JSON_BLOCK = re.compile(r"\{.*\}", re.S)


# --------------------------------------------------------------------------- #
# 数据 / prompt（与 tools/train/smoke_lora.py 保持一致）
# --------------------------------------------------------------------------- #
def load_items(path: Path | str, limit: int = 0) -> list[dict]:
    """读 jsonl，坏行跳过；limit<=0 表示全读。"""
    items: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if limit and len(items) >= limit:
                break
    return items


def build_messages(item: dict) -> tuple[list[dict], str]:
    """数据集条目 → (chat messages(不含 assistant), assistant 标签文本)。

    与 ``tools/train/smoke_lora.py::build_messages`` **逐行一致**（测试锁住）；
    训练和评测必须同分布，所以这里刻意复制而不是"重构"。
    """
    ctx = item.get("context") or []
    system = next((str(c.get("text") or "") for c in ctx
                   if c.get("role") == "system"), DEFAULT_SYSTEM)
    messages: list[dict] = [{"role": "system", "content": system}]
    for c in ctx:
        role = c.get("role")
        if role in ("user", "assistant"):
            text = str(c.get("text") or "")
            if text:
                messages.append({"role": role, "content": text})
    user_msg = str(item.get("user_msg") or "").strip()
    if user_msg and not (messages[-1]["role"] == "user"
                         and messages[-1]["content"] == user_msg):
        messages.append({"role": "user", "content": user_msg})
    if messages[-1]["role"] != "user":
        # 兜底：assistant 结尾的样本把最后一句当作用户输入（避免空 prompt）
        messages[-1] = {"role": "user", "content": messages[-1]["content"]}
    target = item.get("target")
    if not isinstance(target, dict):
        raise ValueError(f"样本 {item.get('id')} 缺 target(InnerState dict)")
    return messages, json.dumps(target, ensure_ascii=False)


def messages_for_item(item: dict) -> list[dict]:
    """优先用数据行里现成的 ``prompt`` 字段，否则按 context+user_msg 重建。"""
    prompt = item.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return [{"role": "user", "content": prompt.strip()}]
    return build_messages(item)[0]


def apply_chat(tokenizer, messages: list[dict]) -> str:
    """chat 模板 + 关思考（与 smoke 训练 prompt 完全同源）。"""
    try:
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
            enable_thinking=False)
    except TypeError:  # 老 tokenizer 不认 enable_thinking
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True)


def build_prompt_text(tokenizer, messages: list[dict],
                      max_prompt_tokens: int = 512) -> tuple[str, list[int]]:
    """渲染 prompt；超长时优先丢最旧的历史消息（与 smoke 的截断策略一致）。"""
    msgs = [dict(m) for m in messages]
    while True:
        text = apply_chat(tokenizer, msgs)
        ids = tokenizer(text, add_special_tokens=False)["input_ids"]
        if len(ids) <= max_prompt_tokens or len(msgs) <= 2:
            break
        del msgs[1]
    if len(ids) > max_prompt_tokens:  # 实在放不下：保留 system 头 + 尾部 user
        keep_head = min(len(ids), 64)
        head = tokenizer.decode(ids[:keep_head], skip_special_tokens=False)
        tail = tokenizer.decode(ids[-(max_prompt_tokens - keep_head):],
                                skip_special_tokens=False)
        text = head + tail
        ids = tokenizer(text, add_special_tokens=False)["input_ids"]
    return text, ids


def infer_split(path: Path | str, item: dict) -> str:
    """split 推断：行内字段优先，其次文件名里的 train/val。"""
    split = str(item.get("split") or "").strip()
    if split:
        return split
    name = Path(path).name.lower()
    for candidate in ("train", "val", "test", "dev"):
        if candidate in name:
            return candidate
    return "unknown"


# --------------------------------------------------------------------------- #
# G1：格式
# --------------------------------------------------------------------------- #
def check_format(raw_output: str, hit_max_tokens: bool = False) -> dict:
    """G1 判定：json.loads + agi.inner.schema.validate。"""
    from agi.inner import schema

    text = str(raw_output or "").strip()
    result: dict[str, Any] = {"parsed_ok": False, "fail": "", "fail_detail": "",
                              "pred": None}
    if not text:
        result["fail"] = "empty"
        return result
    # 只有「像 JSON 开头但没收尾」或真的撞了 max_new_tokens 才算截断；
    # 纯文字解释（不以下一个 { 开头）算 not_json。
    truncated_hint = bool(hit_max_tokens) or (
        text.startswith("{") and not text.endswith("}"))
    try:
        pred = json.loads(text)
    except json.JSONDecodeError as exc:
        result["fail"] = "truncated" if truncated_hint else "not_json"
        result["fail_detail"] = str(exc)[:200]
        return result
    if not isinstance(pred, dict):
        result["fail"] = "schema_invalid"
        result["fail_detail"] = "顶层不是 JSON object"
        return result
    try:
        schema.validate(pred)
    except ValueError as exc:
        result["fail"] = "schema_invalid"
        result["fail_detail"] = str(exc)[:200]
        return result
    result.update(parsed_ok=True, pred=pred)
    return result


# --------------------------------------------------------------------------- #
# G2：逐字段一致
# --------------------------------------------------------------------------- #
def _norm_text(value: Any) -> str:
    return _WS.sub(" ", str(value or "").replace("\r", " ").replace("\n", " ")).strip().lower()


def _norm_length(value: Any) -> str | None:
    raw = _norm_text(value)
    return _LENGTH_ALIASES.get(raw)


def _norm_emoji(value: Any) -> str | None:
    if value is None:
        return None
    raw = _norm_text(value)
    if raw in _EMOJI_ALIASES:
        return _EMOJI_ALIASES[raw]
    # 中文值可能带标点/空白，取首个汉字词再试一次
    if _CJK.search(raw):
        return _EMOJI_ALIASES.get(_CJK.findall(raw)[0])
    return None


def _norm_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = _norm_text(value).rstrip("。.")
    if raw in _TRUE_WORDS:
        return True
    if raw in _FALSE_WORDS:
        return False
    return None


def keywords(text: Any) -> set[str]:
    """轻量关键词集合：英文/数字按词，中文按**字**（stance/intent 都很短，
    字级集合比 bigram 更稳；空/单字也天然覆盖）。"""
    raw = _norm_text(text)
    words = {w.lower() for w in _WORD.findall(raw)}
    return words | set(_CJK.findall(raw))


def loose_match(a: Any, b: Any, threshold: float = 0.5) -> bool:
    """关键词重合 ≥ threshold 记一致（分母取较小集合，避免长短句惩罚）。"""
    ka, kb = keywords(a), keywords(b)
    if not ka or not kb:
        return _norm_text(a) == _norm_text(b) and bool(_norm_text(a))
    return len(ka & kb) / min(len(ka), len(kb)) >= threshold


def compare_fields(pred: dict | None, target: dict) -> dict[str, bool]:
    """逐字段比对；pred 非法/缺失时全部 False（仍计入分母）。"""
    pred = pred if isinstance(pred, dict) else {}
    pbeh = pred.get("behavior") if isinstance(pred.get("behavior"), dict) else {}
    tbeh = target.get("behavior") if isinstance(target.get("behavior"), dict) else {}
    match: dict[str, bool] = {}
    for field in BEHAVIOR_FIELDS:
        pv, tv = pbeh.get(field), tbeh.get(field)
        if field == "length":
            match[field] = _norm_length(pv) is not None and _norm_length(pv) == _norm_length(tv)
        elif field == "emoji":
            match[field] = _norm_emoji(pv) is not None and _norm_emoji(pv) == _norm_emoji(tv)
        else:  # ask / advice
            match[field] = _norm_bool(pv) is not None and _norm_bool(pv) == _norm_bool(tv)
    for name in ("stance", "intent"):
        pv, tv = pred.get(name), target.get(name)
        match[f"{name}_strict"] = bool(_norm_text(tv)) and _norm_text(pv) == _norm_text(tv)
        match[f"{name}_loose"] = loose_match(pv, tv) if _norm_text(pv) else False
    return match


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def _field_norm(field: str, value: Any):
    if field == "length":
        return _norm_length(value)
    if field in ("ask", "advice"):
        return _norm_bool(value)
    return _norm_emoji(value)


def _behavior_of(record: dict, which: str) -> dict:
    data = record.get(which)
    if not isinstance(data, dict):
        return {}
    behavior = data.get("behavior")
    return behavior if isinstance(behavior, dict) else {}


def _class_recalls(records: list[dict], field: str) -> dict[str, float]:
    """按 target 类别算 recall；pred 缺失/非法一律算错（保证分母是全部样本）。"""
    counts: dict[str, list[int]] = {}
    for record in records:
        target = _field_norm(field, _behavior_of(record, "target").get(field))
        if target is None:
            continue
        pred = _field_norm(field, _behavior_of(record, "pred").get(field))
        slot = counts.setdefault(str(target), [0, 0])
        slot[0] += 1
        if pred == target:
            slot[1] += 1
    return {label: hit / total for label, (total, hit) in counts.items() if total}


def _majority_baseline(records: list[dict], field: str) -> float | None:
    counts: dict[str, int] = {}
    for record in records:
        target = _field_norm(field, _behavior_of(record, "target").get(field))
        if target is not None:
            counts[str(target)] = counts.get(str(target), 0) + 1
    total = sum(counts.values())
    return round(max(counts.values()) / total, 4) if total else None


def behavior_balanced(records: list[dict]) -> dict:
    """类别不平衡下的主口径：每字段 balanced accuracy（各类 recall 平均），四字段再平均。

    * ``length_bal`` / ``advice_bal`` / ``ask_bal`` / ``emoji_bal``：该字段两类 recall 的
      算术平均（只有一类时就是该类的 recall，并在 ``class_recalls`` 里可见）；
    * ``mean``：四个字段 balanced accuracy 的平均；
    * ``*_minority_recall``：少数类 recall 仅作附加信息（不参与 mean，避免偏科模型刷分）；
    * ``majority_baselines``：各字段多数类基线（all-majority 预测能拿到的分）。
    """
    empty = {"ask_bal": None, "emoji_bal": None, "length_bal": None,
             "advice_bal": None, "mean": None,
             "length_minority_recall": None, "advice_minority_recall": None,
             "class_recalls": {}, "majority_baselines": {}, "n": len(records)}
    if not records:
        return empty
    recalls = {field: _class_recalls(records, field) for field in BALANCED_FIELDS}

    def balanced_acc(field: str) -> float | None:
        values = list(recalls[field].values())
        return round(sum(values) / len(values), 4) if values else None

    ask_bal = balanced_acc("ask")
    emoji_bal = balanced_acc("emoji")
    length_bal = balanced_acc("length")
    advice_bal = balanced_acc("advice")
    parts = [v for v in (length_bal, advice_bal, ask_bal, emoji_bal) if v is not None]
    length_min = recalls["length"].get(str(RARE_LABELS["length"]))
    advice_min = recalls["advice"].get(str(RARE_LABELS["advice"]))
    return {
        "ask_bal": ask_bal,
        "emoji_bal": emoji_bal,
        "length_bal": length_bal,
        "advice_bal": advice_bal,
        "mean": round(sum(parts) / len(parts), 4) if parts else None,
        # 附加信息：少数类 recall（不参与 mean）
        "length_minority_recall": None if length_min is None else round(length_min, 4),
        "advice_minority_recall": None if advice_min is None else round(advice_min, 4),
        "class_recalls": {field: {label: round(value, 4)
                                  for label, value in recalls[field].items()}
                          for field in BALANCED_FIELDS},
        "majority_baselines": {field: _majority_baseline(records, field)
                               for field in BALANCED_FIELDS},
        "n": len(records),
    }


def _state_from_system_text(text: str) -> dict:
    """从「当前状态：mood=0.55 energy=0.60 …」反解状态字段；解不出返回 {}。"""
    found: dict[str, Any] = {}
    raw = str(text or "")
    for key, pattern in _STATE_PATTERNS.items():
        match = pattern.search(raw)
        if not match:
            continue
        value = match.group(1)
        if key == "stage":
            found[key] = value
        elif key == "crisis":
            found[key] = value.lower() == "true"
        else:
            found[key] = float(value)
    return found


def rule_prediction_behavior(item: dict) -> dict | None:
    """val 行 → render_rule 的 behavior 预测（与 _compare_baselines.py 同口径）。

    只取 context 里 system 那条的状态；解不出状态返回 None。render_rule 的异常由调用方兜。
    """
    from agi.inner import render, schema  # 延迟 import：CI 无 torch 也能跑

    context = item.get("context") or []
    system_text = next((str(c.get("text") or "") for c in context
                        if c.get("role") == "system"), "")
    state_fields = _state_from_system_text(system_text)
    if not state_fields:
        return None
    trust = float(state_fields.get("trust", 0.5))
    state = schema.from_state(
        {"self": state_fields, "relationship": {"trust": trust}}, trust)
    rendered = render.render_rule(state)
    behavior = rendered.behavior if isinstance(rendered.behavior, dict) else {}
    return dict(behavior) or None


def rule_baseline(items: list[dict]) -> dict:
    """手写规则基线：同批 val 上算 behavior 四字段的 balanced accuracy。

    输出结构对齐 ``G2.balanced``：``{length_bal, advice_bal, ask_bal, emoji_bal, mean,
    n, skipped, class_recalls, majority_baselines}``。单条失败（状态解不出 /
    render_rule 抛异常）跳过并计数，不整批崩。
    """
    records: list[dict] = []
    skipped = 0
    for item in items:
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        if not target:
            skipped += 1
            continue
        try:
            behavior = rule_prediction_behavior(item)
        except Exception:  # noqa: BLE001 - 规则基线失败只跳过该条
            skipped += 1
            continue
        if not behavior:
            skipped += 1
            continue
        records.append({"target": target, "pred": {"behavior": behavior}})
    balanced = behavior_balanced(records)
    return {
        "length_bal": balanced["length_bal"],
        "advice_bal": balanced["advice_bal"],
        "ask_bal": balanced["ask_bal"],
        "emoji_bal": balanced["emoji_bal"],
        "mean": balanced["mean"],
        "n": balanced["n"],
        "skipped": skipped,
        "class_recalls": balanced["class_recalls"],
        "majority_baselines": balanced["majority_baselines"],
    }


def summarize(records: list[dict]) -> dict:
    """records → {G1:{...}, G2:{...}} 汇总（分母 = 全部条数，含失败项）。"""
    n = len(records)
    valid = sum(1 for r in records if r.get("parsed_ok"))
    fail_counter = Counter(r.get("fail") or "unknown"
                           for r in records if not r.get("parsed_ok"))

    def rate(key: str) -> float:
        if not n:
            return 0.0
        return round(sum(1 for r in records
                         if (r.get("field_match") or {}).get(key)) / n, 4)

    def joint(keys: tuple[str, ...]) -> float:
        if not n:
            return 0.0
        return round(sum(1 for r in records
                         if all((r.get("field_match") or {}).get(k) for k in keys)) / n, 4)

    return {
        "G1": {
            "valid_rate": round(valid / n, 4) if n else 0.0,
            "valid": valid,
            "top_fail": dict(fail_counter.most_common()),
        },
        "G2": {
            "field_rates": {k: rate(k) for k in FIELD_RATE_KEYS},
            "all5": joint(ALL5_FIELDS),
            "behavior_only": joint(BEHAVIOR_FIELDS),  # 兼容保留，仅参考
            "all6": joint(ALL5_FIELDS + ("emoji",)),
            "balanced": behavior_balanced(records),    # 主口径
        },
    }


# --------------------------------------------------------------------------- #
# 模型加载 / 生成（与 tools/train/smoke_lora.py 的加载方式一致）
# --------------------------------------------------------------------------- #
def _is_multimodal(config) -> bool:
    archs = " ".join(getattr(config, "architectures", None) or [])
    mt = str(getattr(config, "model_type", ""))
    return ("ConditionalGeneration" in archs or "VL" in archs
            or "ImageTextToText" in archs or mt.endswith("_vl")
            or "vl" in mt or "qwen3_5" in mt)


def _offload_vision_tower(model) -> str:
    """可选省显存：把视觉塔**从模型里删掉**（文本推理用不到）。

    注意不能改成 ``visual.to("cpu")``：那样 ``model.device`` 会变成 cpu
    （视觉参数在 parameters() 里排最前），``generate()`` 把 input_ids 送 cpu，
    而文本 embedding 在 cuda → 直接 device mismatch（eval 侧 2026-10-25 踩过）。
    """
    import torch

    inner = getattr(model, "model", None)
    visual = getattr(inner, "visual", None) if inner is not None else None
    if visual is None:
        return "no_visual"
    try:
        del inner.visual
        torch.cuda.empty_cache()
        return "dropped"
    except Exception as exc:  # noqa: BLE001
        return f"failed:{type(exc).__name__}"


def load_eval_model(model_path: str, adapter: str = "",
                    offload_vision: bool = False):
    """返回 (model, tokenizer, torch)。默认视觉塔留在 GPU（推理不需要 offload）。"""
    import torch
    import transformers
    from transformers import AutoConfig, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    config = AutoConfig.from_pretrained(model_path, trust_remote_code=True)
    kwargs: dict = {"trust_remote_code": True}
    try:
        tf_major = int(str(transformers.__version__).split(".")[0])
    except ValueError:
        tf_major = 5
    if tf_major >= 5:
        kwargs["dtype"] = torch.bfloat16
    else:
        kwargs["torch_dtype"] = torch.bfloat16

    candidates = [transformers.AutoModelForCausalLM]
    if _is_multimodal(config):
        candidates = [getattr(transformers, name, None) for name in
                      ("AutoModelForImageTextToText", "AutoModelForMultimodalLM",
                       "AutoModelForVision2Seq")]
        candidates = [c for c in candidates if c is not None] + candidates
    last_exc: Exception | None = None
    model = None
    for cls in candidates:
        try:
            model = cls.from_pretrained(
                model_path, device_map={"": 0}, low_cpu_mem_usage=True, **kwargs)
            print(f"[eval] 架构 {config.architectures} → {cls.__name__} 加载成功", flush=True)
            break
        except TypeError:
            kwargs.pop("dtype", None)
            kwargs["torch_dtype"] = torch.bfloat16
            last_exc = TypeError("dtype/torch_dtype 不被支持")
        except Exception as exc:  # noqa: BLE001 - 逐个 AutoClass 回退
            last_exc = exc
            print(f"[eval] {cls.__name__} 加载失败：{type(exc).__name__}: "
                  f"{str(exc)[:160]}", flush=True)
    if model is None:
        raise RuntimeError(f"所有 AutoClass 都加载失败：{last_exc}")

    if offload_vision:
        print(f"[eval] 视觉塔 offload(--offload-vision): "
              f"{_offload_vision_tower(model)}", flush=True)
    elif _is_multimodal(config):
        print("[eval] 视觉塔保留在 GPU（推理不需要 offload；需要省显存加 --offload-vision）",
              flush=True)
    else:
        print("[eval] 文本模型，无视觉塔（Qwen2.5 等走 AutoModelForCausalLM）", flush=True)
    if adapter:
        from peft import PeftModel

        # peft 默认 autocast_adapter_dtype=True 会把 bf16/fp16 adapter 权重转成 fp32
        # （训练为数值稳定），推理没必要，且实测慢 18–24%：0.5B 11.2→13.2 tok/s、
        # 2B 7.4→9.2 tok/s。推理显式保持 bf16。
        try:
            model = PeftModel.from_pretrained(
                model, adapter, autocast_adapter_dtype=False)
            print(f"[eval] adapter 已挂载（bf16 权重，autocast_adapter_dtype=False）："
                  f"{adapter}", flush=True)
        except TypeError:  # 老版本 peft 没这个参数
            model = PeftModel.from_pretrained(model, adapter)
            print(f"[eval] adapter 已挂载（旧版 peft，默认 dtype）：{adapter}", flush=True)
    model.eval()
    try:
        print(f"[eval] device: model.device={model.device} "
              f"embed={model.get_input_embeddings().weight.device} "
              f"alloc={gb(torch.cuda.memory_allocated())}GB", flush=True)
    except Exception:  # noqa: BLE001 - 仅日志
        pass
    return model, tokenizer, torch


def generate_one(model, tokenizer, prompt_text: str, torch,
                 max_new_tokens: int = MAX_NEW_TOKENS,
                 temperature: float = 0.0) -> dict:
    """单条贪心/采样生成，返回 raw 文本、token 数与耗时。"""
    device = model.get_input_embeddings().weight.device
    inputs = tokenizer(prompt_text, return_tensors="pt", add_special_tokens=False)
    inputs = {k: v.to(device) for k, v in inputs.items()}
    gen_kwargs = {
        "max_new_tokens": int(max_new_tokens),
        "pad_token_id": tokenizer.pad_token_id or tokenizer.eos_token_id,
        "eos_token_id": tokenizer.eos_token_id,
    }
    if temperature and float(temperature) > 0:
        gen_kwargs.update(do_sample=True, temperature=float(temperature), top_p=1.0)
    else:
        gen_kwargs.update(do_sample=False)
    t0 = time.perf_counter()
    with torch.inference_mode():
        output = model.generate(**inputs, **gen_kwargs)
    latency = time.perf_counter() - t0
    prompt_len = int(inputs["input_ids"].shape[1])
    new_ids = output[0, prompt_len:]
    raw = tokenizer.decode(new_ids, skip_special_tokens=True)
    return {
        "raw_output": raw,
        "generated_tokens": int(new_ids.shape[0]),
        "hit_max_tokens": int(new_ids.shape[0]) >= int(max_new_tokens),
        "latency_s": round(latency, 3),
    }


def gb(value: int) -> float:
    return round(value / 1024 ** 3, 3)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="灵魂模型评测：G1 格式 / G2 教师一致")
    p.add_argument("--data", default=str(DEFAULT_DATA), help="jsonl（train/val 均可）")
    p.add_argument("--adapter", default="", help="LoRA adapter 目录；省略=只评 base")
    p.add_argument("--model-path", default=str(DEFAULT_MODEL_PATH))
    p.add_argument("--limit", type=int, default=40, help="条数；0=全部")
    p.add_argument("--temperature", type=float, default=0.0, help="默认 0=贪心可复现")
    p.add_argument("--max-new-tokens", type=int, default=MAX_NEW_TOKENS)
    p.add_argument("--max-prompt-tokens", type=int, default=512)
    p.add_argument("--empty-cache-every", type=int, default=5,
                   help="每 N 条 torch.cuda.empty_cache() 一次（0=不清）")
    p.add_argument("--out", default="", help="汇总 JSON 输出路径（默认 E:\\agent\\logs）")
    p.add_argument("--backend", choices=("hf", "llama"), default="hf",
                   help="hf=本地 transformers（默认，慢）；llama=走 llama-server HTTP（快 12 倍）")
    p.add_argument("--base-url", default="http://127.0.0.1:8123",
                   help="--backend llama 时的 llama-server 地址")
    p.add_argument("--timeout-s", type=float, default=30.0,
                   help="--backend llama 单条请求超时秒数（默认 30）")
    p.add_argument("--retries", type=int, default=1,
                   help="--backend llama 请求失败重试次数（默认 1）")
    p.add_argument("--retry-delay-s", type=float, default=0.5,
                   help="--backend llama 重试前等待秒数（server 短暂重启时可调大，默认 0.5）")
    p.add_argument("--rule-baseline", action="store_true",
                   help="同时用 agi/inner/render.render_rule 在同一批 val 上算规则基线"
                        "（behavior 四字段 balanced accuracy），输出 rule_baseline")
    p.add_argument("--resume", action="store_true",
                   help="断点续跑：读 --out 里已有 items，跳过同 id 已评样本再继续")
    p.add_argument("--retry-errors", action="store_true",
                   help="配合 --resume：把 client_error 的旧记录丢掉重跑（server 中途挂掉时用）")
    p.add_argument("--checkpoint-every", type=int, default=0,
                   help="每 N 条把部分结果写一次 --out（0=只在结束时写；全量长跑建议 20）")
    p.add_argument("--offload-vision", action="store_true",
                   help="HF 后端把视觉塔从模型里删掉省 ~0.6GB（默认保留在 GPU）")
    p.add_argument("--dry-run", action="store_true",
                   help="只校验参数/数据/adapter 路径 + prompt 构造，不加载模型权重、不发 HTTP")
    return p


def run_dry_run(args, items: list[dict], data_path: Path, model_path: Path,
                adapter: str, out_path: Path) -> int:
    """不加载模型权重的自检：参数/数据/adapter 路径 + prompt 构造 + 汇总路径。"""
    print("[eval] dry-run：不加载模型权重，只校验路径/数据/prompt 构造", flush=True)
    print(f"[eval] dry-run model={model_path} adapter={adapter or '(base)'} "
          f"vision={'offload' if args.offload_vision else 'keep'} "
          f"data={data_path} n={len(items)} out={out_path}", flush=True)
    print(f"[eval] dry-run 口径：{json.dumps(G2_CRITERIA, ensure_ascii=False)}", flush=True)
    if getattr(args, "backend", "hf") == "llama":
        print(f"[eval] dry-run backend=llama server={args.base_url} "
              f"timeout={args.timeout_s}s（不发 HTTP）", flush=True)
        for item in items[:3]:
            messages = messages_for_item(item)
            chars = sum(len(str(m.get("content") or "")) for m in messages)
            print(f"[eval] dry-run llama id={item.get('id')} messages={len(messages)} "
                  f"chars={chars} roles={[m['role'] for m in messages]}", flush=True)
        try:
            _make_llama_client(args.base_url, args.timeout_s)
            print("[eval] dry-run llama：llama_client 入口可用（未发请求）", flush=True)
        except Exception as exc:  # noqa: BLE001 - 等 task-12 交付前警告即可
            print(f"[eval] dry-run llama 提示：{type(exc).__name__}: {exc}"
                  "（task-12 的 agi/soul/llama_client.py 交付前不可用）", file=sys.stderr)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"[eval] dry-run 输出目录可写：{out_path.parent}", flush=True)
        summary = summarize([])
        assert summary["G1"]["valid_rate"] == 0.0 and summary["G2"]["all5"] == 0.0
        print("[eval] dry-run OK（llama 后端：数据/messages/输出目录校验通过；"
              "未加载模型、未发 HTTP）", flush=True)
        return 0
    # 汇总路径本身不能崩（空 records）
    summary = summarize([])
    assert summary["G1"]["valid_rate"] == 0.0 and summary["G2"]["all5"] == 0.0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[eval] dry-run 输出目录可写：{out_path.parent}", flush=True)

    try:
        from transformers import AutoTokenizer
    except ImportError:
        print("[eval] dry-run：当前解释器没有 transformers，跳过 tokenizer prompt 自检", flush=True)
        print("[eval] dry-run OK（路径/数据/汇总校验通过；base/adapter 分支逻辑已走通）",
              flush=True)
        return 0
    tokenizer = AutoTokenizer.from_pretrained(str(model_path), trust_remote_code=True)
    prompt_lens = []
    for item in items[:3]:
        messages = messages_for_item(item)
        text, ids = build_prompt_text(tokenizer, messages, args.max_prompt_tokens)
        prompt_lens.append(len(ids))
        print(f"[eval] dry-run prompt id={item.get('id')} tokens={len(ids)} "
              f"tail={text[-28:]!r}", flush=True)
    # 与训练脚本的 prompt 不变量：能 import tools/ 就顺手核一下（改 prompt 时提前发现）
    try:
        sys.path.insert(0, str(ROOT))
        from tools.train import smoke_lora  # noqa: WPS433

        for item in items[:3]:
            messages, answer = build_messages(item)
            smoke_messages, smoke_answer = smoke_lora.build_messages(item)
            assert messages == smoke_messages and answer == smoke_answer, item.get("id")
            _, ids = build_prompt_text(tokenizer, messages, args.max_prompt_tokens)
            enc = smoke_lora.encode_sample(tokenizer, messages, answer, max_len=512)
            assert ids == enc["input_ids"][:len(ids)], item.get("id")
        print("[eval] dry-run prompt 与 tools/train/smoke_lora.py 一致（消息+token 前缀）",
              flush=True)
    except ImportError:
        print("[eval] dry-run：tools/ 不可 import，跳过 smoke 一致性核对", flush=True)
    except AssertionError as exc:
        print(f"[eval] dry-run 警告：prompt 与 smoke_lora 不一致：{exc}", file=sys.stderr)
        return 2
    print(f"[eval] dry-run OK；prompt tokens={prompt_lens}", flush=True)
    return 0


def _default_out(adapter: str) -> Path:
    if adapter:
        tag = Path(adapter).name or "adapter"
        return DEFAULT_OUT.with_name(f"eval_{tag}.json")
    return DEFAULT_OUT.with_name("eval_base.json")


def _write_result(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[eval] 已写入 {path}", flush=True)


def load_resume_records(path: Path) -> list[dict]:
    """读已有 --out JSON 的 items，用于 ``--resume`` 断点续跑；坏文件当空。"""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict)]


def select_todo(items: list[dict], done_records: list[dict]) -> list[dict]:
    """--resume 时按 id 去掉已评样本；id 缺失的样本一律重跑。"""
    done = {str(r.get("id")) for r in done_records if r.get("id") is not None}
    return [it for it in items
            if it.get("id") is None or str(it.get("id")) not in done]


def is_retryable_error(record: dict) -> bool:
    """client_error（server 挂了/超时）可重跑；not_json/truncated 是模型真实输出，不重跑。"""
    return bool(record.get("error")) or record.get("fail") == CLIENT_ERROR_LABEL


def _avg(values) -> float | None:
    nums = [float(v) for v in values
            if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return round(sum(nums) / len(nums), 1) if nums else None


def build_payload(records: list[dict], *, args, adapter: str, model_path: Path,
                  data_path: Path, elapsed: float, torch, split: str,
                  resumed: int = 0, rule_baseline_summary: dict | None = None) -> dict:
    """records → 落盘/打印用汇总 JSON（含逐条明细与判定口径）。"""
    summary = summarize(records)
    backend = getattr(args, "backend", "hf")
    if torch is not None:
        peak_alloc = gb(torch.cuda.max_memory_allocated())
        peak_reserved = gb(torch.cuda.max_memory_reserved())
    else:  # llama 后端不加载 torch/权重，显存由 server 侧另测
        peak_alloc = peak_reserved = None
    return {
        "n": len(records),
        "backend": backend,
        "server_url": getattr(args, "base_url", None) if backend == "llama" else None,
        "adapter": adapter or None,
        "model_path": str(model_path),
        "data": str(data_path),
        "temperature": args.temperature,
        "max_new_tokens": args.max_new_tokens,
        "split": split,
        "resumed": resumed,
        "elapsed_s": elapsed,
        "per_item_s": round(elapsed / len(records), 3) if records else None,
        "prompt_tokens_avg": _avg([r.get("prompt_tokens") for r in records]),
        "peak_alloc_gb": peak_alloc,
        "peak_reserved_gb": peak_reserved,
        "criteria": G2_CRITERIA,
        "rule_baseline": rule_baseline_summary,
        "G1": summary["G1"],
        "G2": summary["G2"],
        "items": records,
    }


def _make_llama_client(base_url: str, timeout_s: float):
    """复用 task-12 的 ``agi/soul/llama_client.make_client``（不自己造 HTTP 调用）。

    已与 w4-soul-data 对齐的签名：``make_client(base_url, timeout_s=30)`` →
    ``LlamaClient``（``available()`` / ``chat(messages, *, purpose, max_tokens,
    temperature, json_mode) -> dict``）。``json_mode=True`` 时 client 内部给
    llama-server 发请求级 ``response_format={"type":"json_schema",...}``
    （server 只带 ``--jinja``、不要 ``-jf``）。client 内部已兜 HTTP/超时异常，
    失败返回 ``{"ok": False, "error": ...}``。
    """
    try:
        from agi.soul import llama_client  # noqa: WPS433
    except ImportError as exc:
        raise RuntimeError(
            "agi/soul/llama_client.py 未就绪（依赖 task-12），无法使用 --backend llama"
        ) from exc
    factory = getattr(llama_client, "make_client", None)
    if callable(factory):
        return factory(base_url, timeout_s=float(timeout_s))
    cls = getattr(llama_client, "LlamaClient", None)
    if cls is not None:
        return cls(base_url, timeout_s=float(timeout_s))
    raise RuntimeError("llama_client 里找不到 make_client/LlamaClient 入口，"
                       "请把签名发给 task-13")


CLIENT_ERROR_LABEL = "client_error"


def generate_via_client(client, messages: list[dict], *,
                        max_tokens: int = MAX_NEW_TOKENS,
                        temperature: float = 0.0, retries: int = 1,
                        retry_delay_s: float = 0.5) -> dict:
    """llama-server 生成一条；client 接口与 ``agi/selflearn/llm`` 一致。

    失败重试 ``retries`` 次；仍失败返回带 ``error`` 的 dict，主循环记成
    ``client_error`` 并继续跑剩余样本（全量评测不能因为 1 条超时整体失败）。
    """
    prompt_tokens = None
    last_error = "unknown"
    t0 = time.perf_counter()
    for attempt in range(int(retries) + 1):
        try:
            result = client.chat(
                messages, purpose="soul_eval", max_tokens=int(max_tokens),
                temperature=float(temperature), json_mode=True)
        except Exception as exc:  # noqa: BLE001 - 超时/连接失败都进重试
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        else:
            if (isinstance(result, dict) and result.get("ok")
                    and isinstance(result.get("text"), str)):
                usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
                pt = result.get("prompt_tokens", usage.get("prompt_tokens"))
                ct = result.get("completion_tokens", usage.get("completion_tokens"))
                prompt_tokens = int(pt) if isinstance(pt, (int, float)) else None
                gen_tokens = int(ct) if isinstance(ct, (int, float)) else None
                return {
                    "raw_output": result["text"],
                    "generated_tokens": gen_tokens,
                    "hit_max_tokens": bool(gen_tokens is not None
                                           and gen_tokens >= int(max_tokens)),
                    "latency_s": round(time.perf_counter() - t0, 3),
                    "prompt_tokens": prompt_tokens,
                }
            if isinstance(result, dict):
                last_error = str(result.get("error") or "client 返回 ok=false")[:200]
            else:
                last_error = f"client 返回值非法：{type(result).__name__}"
        if attempt < int(retries):
            time.sleep(max(0.0, float(retry_delay_s)))
    return {"raw_output": "", "generated_tokens": None, "hit_max_tokens": False,
            "latency_s": round(time.perf_counter() - t0, 3),
            "prompt_tokens": prompt_tokens,
            "error": f"{CLIENT_ERROR_LABEL}:{last_error}"}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    backend = str(args.backend)
    data_path = Path(args.data)
    if not data_path.exists():
        print(f"[eval] 找不到数据：{data_path}", file=sys.stderr)
        return 2
    items = load_items(data_path, args.limit)
    if not items:
        print(f"[eval] 数据为空：{data_path}", file=sys.stderr)
        return 2
    model_path = Path(args.model_path)
    adapter = str(Path(args.adapter)) if args.adapter else ""
    if backend == "hf":
        if not (model_path / "config.json").exists():
            print(f"[eval] 模型目录缺 config.json：{model_path}", file=sys.stderr)
            return 2
        if adapter and not (Path(adapter) / "adapter_config.json").exists():
            print(f"[eval] adapter 目录缺 adapter_config.json：{adapter}", file=sys.stderr)
            return 2
    elif adapter:
        print("[eval] 提示：--backend llama 时 adapter 已合并进 GGUF，--adapter 参数被忽略",
              file=sys.stderr)
        adapter = ""
    out_path = Path(args.out) if args.out else _default_out(adapter)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("MODELSCOPE_CACHE", str(HOME_DIR / "cache" / "modelscope"))
    os.environ.setdefault("HF_HOME", str(HOME_DIR / "cache" / "hf"))

    print(f"[eval] data={data_path} n={len(items)} backend={backend} "
          f"adapter={adapter or '(base)'} temperature={args.temperature}", flush=True)
    rule_summary = None
    if args.rule_baseline:
        try:
            rule_summary = rule_baseline(items)
            print("[eval] rule_baseline " + json.dumps(rule_summary, ensure_ascii=False),
                  flush=True)
        except Exception as exc:  # noqa: BLE001 - 规则基线失败不影响主评测
            print(f"[eval] rule_baseline 计算失败（跳过）：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
    if args.dry_run:
        return run_dry_run(args, items, data_path, model_path, adapter, out_path)

    model = tokenizer = client = torch = None
    if backend == "hf":
        try:
            model, tokenizer, torch = load_eval_model(
                str(model_path), adapter, offload_vision=args.offload_vision)
        except Exception as exc:  # noqa: BLE001
            print(f"[eval] 模型加载失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 3
        if not torch.cuda.is_available():
            print("[eval] CUDA 不可用", file=sys.stderr)
            return 3
        print(f"[eval] gpu={torch.cuda.get_device_name(0)} "
              f"torch={torch.__version__} cuda={torch.version.cuda} "
              f"vram={gb(torch.cuda.get_device_properties(0).total_memory)}GB "
              f"alloc={gb(torch.cuda.memory_allocated())}GB", flush=True)
        torch.cuda.reset_peak_memory_stats()
    else:
        try:
            client = _make_llama_client(args.base_url, args.timeout_s)
        except Exception as exc:  # noqa: BLE001
            print(f"[eval] llama client 创建失败：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 3
        if hasattr(client, "available"):
            try:
                healthy = bool(client.available())
            except Exception:  # noqa: BLE001
                healthy = False
            if not healthy:
                print(f"[eval] llama-server 不可用：{args.base_url}；先起 server：\n"
                      f"  E:\\agent\\venv312\\Scripts\\python.exe "
                      f"tools/soul_serve/server.py --model <gguf> --port 8123 --ngl 99",
                      file=sys.stderr)
                return 3
            print("[eval] llama-server /health OK", flush=True)
        print(f"[eval] backend=llama server={args.base_url} "
              f"timeout={args.timeout_s}s retries={args.retries} json_mode=True"
              f"（prompt 仍走 build_messages，不本地套 chat_template）", flush=True)

    resumed_records = load_resume_records(out_path) if args.resume else []
    if args.retry_errors:
        kept = [r for r in resumed_records if not is_retryable_error(r)]
        dropped = len(resumed_records) - len(kept)
        if dropped:
            print(f"[eval] --retry-errors：丢弃 {dropped} 条 client_error 记录，准备重跑",
                  flush=True)
        resumed_records = kept
    todo = select_todo(items, resumed_records)
    records: list[dict] = list(resumed_records)
    resumed_n = len(records)
    if resumed_n:
        print(f"[eval] --resume：已有 {resumed_n} 条，本次待跑 {len(todo)} 条", flush=True)
    split = infer_split(data_path, items[0])
    t_start = time.perf_counter()
    oom = False
    try:
        for idx, item in enumerate(todo, 1):
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            item_split = infer_split(data_path, item)
            messages = messages_for_item(item)
            prompt_tokens = None
            if backend == "hf":
                prompt_text, prompt_ids = build_prompt_text(
                    tokenizer, messages, args.max_prompt_tokens)
                prompt_tokens = len(prompt_ids)
                try:
                    gen = generate_one(model, tokenizer, prompt_text, torch,
                                       max_new_tokens=args.max_new_tokens,
                                       temperature=args.temperature)
                except torch.cuda.OutOfMemoryError as exc:  # 单条 OOM：记失败继续
                    oom = True
                    gen = {"raw_output": "", "generated_tokens": 0,
                           "hit_max_tokens": False, "latency_s": 0.0,
                           "error": f"OOM:{str(exc)[:120]}"}
                    torch.cuda.empty_cache()
            else:
                gen = generate_via_client(
                    client, messages, max_tokens=args.max_new_tokens,
                    temperature=args.temperature, retries=args.retries,
                    retry_delay_s=args.retry_delay_s)
                prompt_tokens = gen.get("prompt_tokens")
            if gen.get("error"):
                fmt = {"parsed_ok": False, "fail": CLIENT_ERROR_LABEL,
                       "fail_detail": str(gen["error"])[:300], "pred": None}
            else:
                fmt = check_format(gen["raw_output"], gen.get("hit_max_tokens", False))
            record = {
                "id": item.get("id"),
                "split": item_split,
                "source": item.get("source"),
                "state_profile": item.get("state_profile"),
                "prompt_tokens": prompt_tokens,
                "raw_output": gen["raw_output"],
                "generated_tokens": gen["generated_tokens"],
                "hit_max_tokens": gen["hit_max_tokens"],
                "latency_s": gen["latency_s"],
                "parsed_ok": fmt["parsed_ok"],
                "fail": fmt["fail"],
                "fail_detail": fmt["fail_detail"],
                "target": target,
                "pred": fmt["pred"],
                "field_match": compare_fields(fmt["pred"], target),
            }
            if gen.get("error"):
                record["error"] = gen["error"]
            records.append(record)
            print(f"[eval] {resumed_n + idx}/{len(items)} id={record['id']} "
                  f"split={item_split} ok={record['parsed_ok']} "
                  f"fail={record['fail'] or '-'} tokens={record['generated_tokens']} "
                  f"lat={record['latency_s']}s", flush=True)
            if backend == "hf" and args.empty_cache_every \
                    and idx % args.empty_cache_every == 0:
                torch.cuda.empty_cache()
            if args.checkpoint_every and idx % args.checkpoint_every == 0:
                _write_result(out_path, build_payload(
                    records, args=args, adapter=adapter, model_path=model_path,
                    data_path=data_path,
                    elapsed=round(time.perf_counter() - t_start, 2),
                    torch=torch, split=split, resumed=resumed_n,
                    rule_baseline_summary=rule_summary))
    except KeyboardInterrupt:
        print("[eval] 收到中断，保存已跑出的部分结果", file=sys.stderr)

    elapsed = round(time.perf_counter() - t_start, 2)
    print("[eval] G2 主口径=balanced（ask/emoji balanced acc + length/advice 少数类 recall "
          "四项平均）；behavior_only 受类别不平衡影响仅作参考；"
          "stance/intent 主口径=loose（strict 仅参考）", flush=True)
    payload = build_payload(records, args=args, adapter=adapter,
                            model_path=model_path, data_path=data_path,
                            elapsed=elapsed, torch=torch, split=split,
                            resumed=resumed_n, rule_baseline_summary=rule_summary)
    print("[eval] SUMMARY " + json.dumps(
        {k: v for k, v in payload.items() if k != "items"}, ensure_ascii=False), flush=True)
    try:
        _write_result(out_path, payload)
    except OSError as exc:
        print(f"[eval] 写结果失败：{exc}", file=sys.stderr)
        return 2
    return 5 if oom else 0


if __name__ == "__main__":
    raise SystemExit(main())
