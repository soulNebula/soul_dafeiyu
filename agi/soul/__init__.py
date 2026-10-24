# -*- coding: utf-8 -*-
"""W4 灵魂层：把「对话上下文 → InnerState」蒸馏成 2B 小模型可学的 SFT 数据。

模块分工：
- ``dataset``: 采样（golden / 匿名化聊天窗口 / 合成场景）+ DeepSeek teacher 标注
  + schema 校验 + JSONL 落盘 + 断点续跑 + train/val 切分 + 成本统计。

本包只负责**造数据**，不训练、不改 ``agi/data``：
- 数据源只读：``agi/eval_data/golden_v1.jsonl``、``log/messages.jsonl``；
- 产物落在 ``agi/soul/data/``。
"""
from __future__ import annotations

__all__ = ["dataset"]
