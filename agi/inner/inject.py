# -*- coding: utf-8 -*-
"""W2-A 注入：按 INNER_MODE 组装注入 chat prompt 的 inner_ctx。

模式：
- ``off``    → ""（默认；prompt 与改动前逐字节一致，由 chat._prompt 的空串分支保证）
- ``static`` → 固定人格段落（B 臂），不读用户状态
- ``rule``   → 用当前会话状态按阈值渲染行为指令
- ``model``  → 让 client 生成 InnerState JSON 再渲染；client 不可用 / 超时 / 非 JSON /
               字段非法 / JSON 为空壳，一律回退 rule（不抛异常，不影响主链）

model 模式有两种 client：
- **本地灵魂 client**（带 ``local_soul = True``，或调用方显式 ``is_local=True``）：
  messages 由 ``agi/soul/prompt.py::build_messages`` 构造——与 SFT 训练分布同源
  （system = SOUL_PERSONA + 当前状态行 + 最近对话 + 用户这句话）；
- **远程 DeepSeek client**：保持 W2 原有单条 prompt 路径不变（兼容旧行为）。

额外安全：``monologue`` 先 sanitize——≤40 字、无换行、去掉第二人称命令（你是/你必须…）、
不含用户原文 8 字以上片段，避免把用户原话或命令式语气带进提示词。
"""
from __future__ import annotations

import re
from typing import Any

from . import render, schema

MODES = ("off", "static", "rule", "model")
DEFAULT_MODE = "off"
MAX_MONOLOGUE = 40

_WS = re.compile(r"\s+")
_SECOND_PERSON = re.compile(r"你(?:必须|应该|需要|不能|不准|可以|得|要|是|别)")

MODEL_INSTRUCTION = (
    "你是内状态生成器（不是对话者）。根据会话状态、最近对话和用户这句话，"
    "生成这一轮的内在状态。只输出 JSON，不要输出其他文字：\n"
    '{"stance":"语气立场，不超过12字",'
    '"intent":"这轮想做什么，不超过30字",'
    '"monologue":"留在心里的一句话，不超过40字，不要出现第二人称命令，不要复述用户原话",'
    '"behavior":{"length":"short|normal|long","ask":true,"advice":true,"emoji":"偶尔|不用"}}\n'
    "要求：贴合给定状态与对话，不编造事实；不要输出解释或 markdown。"
)


def normalize_mode(mode: Any) -> str:
    """未知/空值一律回退 off（默认关闭，失败不改变行为）。"""
    value = str(mode or DEFAULT_MODE).strip().lower()
    return value if value in MODES else DEFAULT_MODE


def sanitize_monologue(text: Any, user_text: Any = "") -> str:
    """清洗内心独白：≤40 字、单行、无第二人称命令、不复述用户原文长片段。"""
    value = _WS.sub(" ", str(text or "").replace("\r", " ").replace("\n", " ")).strip()
    value = _SECOND_PERSON.sub("", value)
    value = value.strip("，,。.；;：:、 ")
    user = _WS.sub("", str(user_text or ""))
    if len(user) >= 8:
        for index in range(len(user) - 7):
            if user[index:index + 8] in value:
                value = ""
                break
    return value[:MAX_MONOLOGUE]


def _base_state(path, state: dict | None) -> schema.InnerState:
    # 延迟 import：agi.selflearn.__init__ 会导入 chat，避开自学习包的初始化环。
    from ..selflearn import state as sl_state
    from ..selflearn import trust as sl_trust

    data: dict = state if isinstance(state, dict) and state else {}
    if not data:
        try:
            data = sl_state.load(path)
        except Exception:  # noqa: BLE001
            data = {}
    try:
        trust_value = sl_trust.get(path)
    except Exception:  # noqa: BLE001
        trust_value = 0.5
    return schema.from_state(data, trust_value)


def _rule_ctx(path, text: str, state: dict | None) -> str:
    st = render.render_rule(_base_state(path, state))
    st.monologue = sanitize_monologue(st.monologue, text)
    return render.render_block(st)


def _is_local_client(client, is_local: bool | None) -> bool:
    """本地灵魂 client：显式 is_local 优先，否则看 client.local_soul 属性。"""
    if is_local is not None:
        return bool(is_local)
    try:
        return bool(getattr(client, "local_soul", False))
    except Exception:  # noqa: BLE001
        return False


def _client_available(client, *, local: bool) -> bool:
    """client 是否可用。

    非本地 client 保持 W2 原语义（没有 available() 视为不可用）；
    本地灵魂 client 的契约只有 local_soul/chat，没有 available() 时按「有 chat 即可用」，
    避免 llama_client 漏实现 available() 时静默回退 rule。
    """
    fn = getattr(client, "available", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001
            return False
    return bool(local and callable(getattr(client, "chat", None)))


def _local_messages(base: schema.InnerState, recent: list | None,
                    text: str) -> list[dict]:
    """本地灵魂模型输入：与训练分布同源（agi/soul/prompt.py 唯一真源）。

    system 只带训练时存在的 7 个字段：mood/energy/sharing/battery/trust/stage/crisis；
    patience/openness 是 rule 层派生量，训练时没有，不能进 system。
    """
    # 延迟 import：inner 包初始化期不把 soul 拉进来（soul 依赖 inner.schema）。
    from ..soul import prompt as soul_prompt

    state_dict = {
        "mood": float(base.mood), "energy": float(base.energy),
        "sharing": float(base.sharing), "battery": float(base.battery),
        "trust": float(base.trust), "stage": str(base.stage),
        "crisis": bool(base.crisis),
    }
    return soul_prompt.build_messages(state_dict, list(recent or []), str(text or ""))


def _model_ctx(path, client, text: str, recent: list | None,
               state: dict | None, *, is_local: bool | None = None) -> str:
    """模型模式；任何异常/非法返回都返回 ""（→ 上层回退 rule）。"""
    if client is None:
        return ""
    local = _is_local_client(client, is_local)
    if not _client_available(client, local=local):
        return ""

    base = _base_state(path, state)
    if local:
        # W4-D1：本地灵魂模型走训练同源 messages（system 带状态，历史是真实 turns）
        messages = _local_messages(base, recent, text)
    else:
        lines = []
        for item in list(recent or [])[-6:]:
            if not isinstance(item, dict):
                continue
            role = "用户" if item.get("role") == "user" else "你"
            lines.append(f"{role}：{str(item.get('text') or '')[:120]}")
        prompt = (
            MODEL_INSTRUCTION
            + "\n\n当前状态：mood={:.2f} energy={:.2f} sharing={:.2f} battery={:.2f} "
              "trust={:.2f} patience={:.2f} openness={:.2f} stage={} crisis={}".format(
                  float(base.mood), float(base.energy), float(base.sharing),
                  float(base.battery), float(base.trust), float(base.patience),
                  float(base.openness), base.stage,
                  "true" if base.crisis else "false")
            + "\n\n最近对话：\n" + ("\n".join(lines) or "（无）")
            + "\n\n用户刚刚说：\n" + str(text or "")
        )
        messages = [{"role": "system", "content": "你是只输出 JSON 的内状态生成器。"},
                    {"role": "user", "content": prompt}]
    try:
        result = client.chat(
            messages,
            purpose="inner_state", max_tokens=300, temperature=0.2, json_mode=True,
        )
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(result, dict) or not result.get("ok"):
        return ""
    from ..selflearn.llm import parse_json

    obj = parse_json(str(result.get("text") or ""))
    if not isinstance(obj, dict):
        return ""
    if not any(key in obj for key in ("stance", "intent", "monologue", "behavior")):
        return ""
    try:
        st = schema.validate({**obj, "source": "model"})
    except (TypeError, ValueError):
        return ""
    # 数值/关系来自真实会话状态，模型只负责行为与独白，不允许改事实
    st.mood, st.energy, st.sharing, st.battery = base.mood, base.energy, base.sharing, base.battery
    st.trust, st.stage, st.crisis = base.trust, base.stage, base.crisis
    st.patience, st.openness = base.patience, base.openness
    st.monologue = sanitize_monologue(st.monologue, text)
    return render.render_block(st)


def build_inner_ctx(path, mode: str = "off", client=None, text: str = "",
                    recent: list | None = None, state: dict | None = None,
                    *, is_local: bool | None = None) -> str:
    """构建注入 chat prompt 的内状态片段；off 模式返回空串。

    ``is_local`` 只影响 model 模式：None = 按 ``client.local_soul`` 自动判断。
    """
    mode = normalize_mode(mode)
    if mode == "off":
        return ""
    if mode == "static":
        return render.render_static()
    if mode == "rule":
        try:
            return _rule_ctx(path, text, state)
        except Exception:  # noqa: BLE001
            return ""
    block = ""
    try:
        block = _model_ctx(path, client, text, recent, state, is_local=is_local)
    except Exception:  # noqa: BLE001
        block = ""
    if block:
        return block
    try:
        return _rule_ctx(path, text, state)
    except Exception:  # noqa: BLE001
        return ""
