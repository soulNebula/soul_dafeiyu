# -*- coding: utf-8 -*-
"""03 两段式生成：先列有证据的 claims，再只根据 claims 写回复。"""
from __future__ import annotations

import json
import os
from typing import Any

from .llm import parse_json


def enabled() -> bool:
    """两段式（先抽有证据的 claims 再生成）——防编造，但每条回复多一次模型调用。

    优先级：env ``SELFLEARN_TWO_STAGE`` > 网关配置 ``two_stage_enabled`` > 默认关闭。
    默认已改为关闭：实测每条多 2–5 秒，聊天体验明显变慢；需要更强防编造时再打开。
    """
    env = os.environ.get("SELFLEARN_TWO_STAGE")
    if env is not None:
        return env.strip() not in ("", "0", "false", "False")
    try:
        from . import config as _cfg
        raw = json.loads(
            (_cfg.ROOT / "agi" / "data" / "wechat" / "config.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        raw = {}
    value = raw.get("two_stage_enabled") if isinstance(raw, dict) else None
    return bool(value)


def _evidence_stage(client, user_text: str, memories: list[dict]) -> list[dict]:
    if not memories:
        return []
    lines = "\n".join(
        f"- [[{m['id']}]] [{m.get('kind')}] {m.get('content')}（依据：{m.get('evidence')}）"
        for m in memories
    )
    prompt = (
        "只根据下面的长期记忆，列出这次回复可以使用的、有记忆 id 支持的事实 claims。"
        "没有证据就不要列；不要列建议、情绪和推测。"
        '只输出 JSON：{"claims":[{"claim":"一句事实","memory_ids":["UM-..."]}]}\n\n'
        "记忆：\n" + lines + "\n\n用户说：" + user_text
    )
    result = client.chat(
        [{"role": "system", "content": "你是只输出有证据 claims 的校验器。"},
         {"role": "user", "content": prompt}],
        purpose="selflearn_evidence_stage", max_tokens=600, temperature=0.1,
        json_mode=True,
    )
    if not result.get("ok"):
        return []
    obj = parse_json(str(result.get("text") or ""))
    claims = obj.get("claims") if isinstance(obj, dict) else None
    if not isinstance(claims, list):
        return []
    valid_ids = {m["id"] for m in memories}
    out = []
    for item in claims[:5]:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim") or "").strip()[:300]
        ids = [str(x) for x in (item.get("memory_ids") or []) if str(x) in valid_ids]
        if claim and ids:
            out.append({"claim": claim, "memory_ids": ids})
    return out


def generate(client, system: str, prompt: str, memories: list[dict],
             **kwargs) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回 (LLM 结果, 生成元信息)。无证据/失败时安全回退单段。"""
    if not enabled() or not memories:
        result = client.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompt}], **kwargs)
        return result, {"two_stage": False, "claims": []}
    claims = _evidence_stage(client, prompt, memories)
    if not claims:
        result = client.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompt}], **kwargs)
        return result, {"two_stage": False, "claims": []}
    block = "\n".join(
        f"- {c['claim']}（证据：{', '.join(c['memory_ids'])}）" for c in claims)
    prompt2 = (
        prompt + "\n\n只可使用下面有证据 claims；没有证据的内容不要陈述事实：\n" + block
    )
    result = client.chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": prompt2}], **kwargs)
    return result, {"two_stage": True, "claims": claims}
