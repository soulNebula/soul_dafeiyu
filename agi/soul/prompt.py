# -*- coding: utf-8 -*-
"""W4-D1 训练/推理 prompt 唯一真源。

训练侧（``agi/soul/dataset.py`` 的 teacher/训练数据）与上线侧（``agi/inner/inject.py``
的 ``INNER_MODE=model``，调本地灵魂模型）必须喂给模型**同一种**输入：

    [system]  SOUL_PERSONA + "\\n" + 当前状态行
    [user/assistant ...]  最近对话 turns（最多 max_turns 条）
    [user]   用户这句话

本模块是唯一实现：``dataset.py`` / ``inject.py`` 都从这里 import，禁止再复制第二份。
改动这里的任何字符串都会同时改变训练分布与线上输入——先跑
``agi/tests/test_soul_prompt.py``（它锁住与冻结数据集 ``soul_sft_v1.jsonl`` 的逐字节一致）。

注意：训练 system 里只有 ``mood/energy/sharing/battery/trust/stage/crisis`` 七个字段；
``patience/openness`` 是 rule 层的派生量，训练时不存在，**不能**进 system。
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Iterable

#: 训练 system 里出现的状态字段（顺序无关，只用于过滤/合并）
TRAIN_STATE_FIELDS = ("mood", "energy", "sharing", "battery", "trust", "stage", "crisis")
#: 推理时带上的历史轮数，必须与训练数据 MAX_CONTEXT_TURNS 一致（4 轮）：
#: 线上多带历史 = 分布外输入；test_soul_prompt.py 锁住两者相等。
MAX_TURNS = 4

# --------------------------------------------------------------------------- #
# 角色设定 / 状态档案（state_profile）
# --------------------------------------------------------------------------- #
SOUL_PERSONA = (
    "你是微信里的一个 AI 朋友。语气平和，不讨好、不客服腔；先接住对方的话，"
    "再决定要不要追问；不列清单、不说教；有情绪或危机信号时以对方安全为先。"
)

DEFAULT_STATE = {
    "mood": 0.55, "energy": 0.6, "sharing": 0.45,
    "battery": 0.7, "trust": 0.5, "stage": "初识", "crisis": False,
}

#: 同一 context 可搭配的当前状态档位；teacher 看到不同状态 → target 不同，算不同样本。
STATE_PROFILES = OrderedDict([
    ("default", {}),
    ("work", {"mood": 0.6, "energy": 0.75, "sharing": 0.5,
              "battery": 0.65, "trust": 0.6, "stage": "熟悉"}),
    ("low_mood", {"mood": 0.2, "energy": 0.3, "sharing": 0.2,
                  "battery": 0.45, "trust": 0.55, "stage": "熟悉"}),
    ("tired", {"mood": 0.45, "energy": 0.25, "sharing": 0.3,
               "battery": 0.2, "trust": 0.5, "stage": "熟悉"}),
    ("joke", {"mood": 0.75, "energy": 0.75, "sharing": 0.65,
              "battery": 0.8, "trust": 0.6, "stage": "熟悉"}),
    ("guarded", {"mood": 0.45, "energy": 0.55, "sharing": 0.1,
                 "battery": 0.6, "trust": 0.3, "stage": "初识"}),
    ("close", {"mood": 0.6, "energy": 0.6, "sharing": 0.7,
               "battery": 0.7, "trust": 0.8, "stage": "亲近"}),
])
PROFILE_NAMES = tuple(STATE_PROFILES)


def state_dict(state: Any = None) -> dict:
    """只保留训练存在的字段；缺失字段用 DEFAULT_STATE 补（不引入 patience/openness）。"""
    merged = dict(DEFAULT_STATE)
    if isinstance(state, dict):
        for key in TRAIN_STATE_FIELDS:
            value = state.get(key)
            if value is not None:
                merged[key] = value
    return merged


def profile_state(profile: str, bucket: str | None = None) -> dict:
    """state_profile + 场景 → 当前状态 dict；crisis 场景强制 crisis=True。"""
    name = profile if profile in STATE_PROFILES else "default"
    state = dict(DEFAULT_STATE)
    state.update(STATE_PROFILES[name])
    if str(bucket or "") == "crisis":
        state["crisis"] = True
    return state


def state_block_for(state: dict) -> str:
    """当前状态 dict → 一行可读文本（训练 system 与推理 system 共用）。"""
    data = state_dict(state)
    return (
        "当前状态：mood={mood:.2f} energy={energy:.2f} sharing={sharing:.2f} "
        "battery={battery:.2f} trust={trust:.2f} stage={stage} crisis={crisis}"
    ).format(
        mood=float(data["mood"]), energy=float(data["energy"]),
        sharing=float(data["sharing"]), battery=float(data["battery"]),
        trust=float(data["trust"]), stage=str(data["stage"]),
        crisis="true" if data.get("crisis") else "false",
    )


def render_system(state: dict) -> str:
    """system 提示：角色设定 + 当前状态行（训练/推理同一份）。"""
    return SOUL_PERSONA + "\n" + state_block_for(state)


def system_context(state: dict) -> str:
    """``dataset.py`` 旧名，等价 :func:`render_system`（保留兼容）。"""
    return render_system(state)


# --------------------------------------------------------------------------- #
# messages 构造（训练数据重建 / 上线推理共用）
# --------------------------------------------------------------------------- #
def _compose(system: str, turns: Iterable[dict], text: str) -> list[dict]:
    """system + 历史 turns + 最终 user；与 smoke_lora/evaluate 的构造口径一致。

    - 历史只收 user/assistant 且非空文本；
    - 最终 user 若与最后一条历史完全相同则不重复追加（避免 inference 时
      把「最近消息」里的当前用户消息再塞一次）；
    - 结尾必须是 user（兜底把最后一条转成 user，和训练/评测脚本一致）。
    """
    messages: list[dict] = [{"role": "system", "content": system}]
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        if role not in ("user", "assistant"):
            continue
        value = str(turn.get("text") or "")
        if value:
            messages.append({"role": role, "content": value})
    final = str(text or "").strip()
    if final and not (messages[-1]["role"] == "user"
                      and messages[-1]["content"] == final):
        messages.append({"role": "user", "content": final})
    if messages[-1]["role"] != "user":
        messages[-1] = {"role": "user", "content": messages[-1]["content"]}
    return messages


def build_messages(state: dict, recent: list, text: str, *,
                   max_turns: int = MAX_TURNS) -> list[dict]:
    """推理入口：当前状态 + 最近对话 + 用户这句话 → chat messages。

    ``recent`` 接受与 ``memory.recent_messages`` 同构的 ``[{"role","text"}, ...]``；
    只保留最近 ``max_turns`` 条 user/assistant，最后追加 ``text``。
    """
    turns: list[dict] = []
    for item in list(recent or []):
        if isinstance(item, dict):
            turns.append({"role": item.get("role"), "text": item.get("text")})
    limit = max(0, int(max_turns))
    if limit:
        turns = turns[-limit:]
    else:
        turns = []
    return _compose(render_system(state), turns, text)


def messages_for_item(item: dict) -> list[dict]:
    """数据集条目 → chat messages（system 取 context 第一条 system，其余 turns + user_msg）。

    供 ``evaluate.py`` / ``tools/train/smoke_lora.py`` 共用，避免各自复制构造逻辑；
    对冻结的 ``soul_sft_v1.jsonl`` 重建出的 system 与 ``context[0].text`` 逐字节相同。
    """
    context = item.get("context") or []
    system = next(
        (str(turn.get("text") or "") for turn in context
         if isinstance(turn, dict) and turn.get("role") == "system"),
        SOUL_PERSONA,
    )
    turns = [turn for turn in context
             if isinstance(turn, dict) and turn.get("role") != "system"]
    return _compose(system, turns, item.get("user_msg") or "")
