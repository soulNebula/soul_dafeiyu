# -*- coding: utf-8 -*-
"""W2-A 内状态层：把「情绪状态 + 信任」翻译成这一轮的行为参数。

设计约束（见 specs/09、specs/10）：
- 内状态只影响**语气与选择**，不陈述事实，不进回复正文；
- 状态是可观测、可演化、可恢复的变量，不是自由发挥的台词；
- 越界字段一律 clamp，类型非法一律 ValueError（模型模式的脏 JSON 因此可被安全回退）。

模块分工：
- ``schema``  : InnerState 数据结构 / from_state / validate
- ``render``  : 状态 → 行为参数（render_rule）→ 提示词片段（render_block/static）
- ``inject``  : 按 INNER_MODE 组装 inner_ctx，含 model 模式与回退
"""
from __future__ import annotations

__all__ = ["schema", "render", "inject"]
