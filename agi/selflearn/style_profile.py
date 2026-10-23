# -*- coding: utf-8 -*-
"""从真实聊天提炼的匿名说话节奏；只学风格，不保存原聊天内容。"""
from __future__ import annotations

import re

BASE_RULES = """说话像普通人，不像助手：
- 通常 1-2 句；能一句说完就一句。
- 口语、直接，不列清单、不总结、不说教。
- 不主动介绍自己能做什么，不主动强调自己是 AI。
- 对方回得很短或说“睡了/忙/不知道”时，不连续追问，收住。
- 约时间/吃饭这类事，只回关键信息，不解释一堆。"""

LOGISTICS = (
    '例：\n对方：打球去不去\n你：去啊，几点？\n'
    '对方：今晚吃饭不\n你：吃，你定地方\n'
    '对方：8 到 9\n你：行，订了'
)
EMOTIONAL = (
    '例：\n对方：今天好累\n你：咋了，先歇会儿\n'
    '对方：我有点不想上班\n你：那就先别硬扛，缓缓再说\n'
    '对方：你会不会觉得我很烦\n你：没有，你继续说'
)
LOW_INVESTMENT = (
    '例：\n对方：不知道\n你：行，那你想说再说\n'
    '对方：先不聊了，睡觉\n你：好，早点睡'
)


def style_block(text: str = "") -> str:
    value = str(text or "")
    extra = ""
    if re.search(r"打球|球|吃饭|有空|几点|约|订", value):
        extra = LOGISTICS
    elif re.search(r"累|烦|不想|难过|焦虑|喜欢|烦|心情|睡不着", value):
        extra = EMOTIONAL
    elif re.search(r"不知道|睡了|先不聊|忙|算了", value):
        extra = LOW_INVESTMENT
    return BASE_RULES + ("\n\n" + extra if extra else "")


def tune_reply(text: str, reply: str) -> str:
    """把普通聊天压回人类短句；明确要解释/步骤时不硬砍。"""
    value = str(reply or "").strip()
    if not value:
        return value
    if any(k in str(text or "") for k in ("解释", "详细", "为什么", "怎么做", "步骤", "分析", "教程")):
        return value
    lines = [ln.strip() for ln in value.splitlines() if ln.strip()]
    if lines and all(ln.startswith(('-', '*', '1.', '2.', '3.', '•')) for ln in lines):
        value = lines[0].lstrip('-*• 1234567890.') .strip()
    parts = re.split(r"(?<=[。！？!?])", value)
    parts = [p.strip() for p in parts if p.strip()]
    short = "".join(parts[:2]).strip()
    if len(short) > 80:
        short = "".join(parts[:1]).strip()
    return short or value


def strip_periods(text: str) -> str:
    """去掉对外回复里的中文句号和句末英文句点；保留问号/感叹号/逗号。"""
    value = str(text or "")
    value = value.replace("。", "")
    value = re.sub(r"(?<![0-9A-Za-z])\.(?=\s|$)", "", value)
    return value.strip()


def split_bubbles(text: str, max_bubbles: int = 1) -> list[str]:
    text = str(text or "").strip()
    if not text or max_bubbles <= 1:
        return [text] if text else []
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?])", text) if p.strip()]
    if len(parts) <= 1:
        return [text]
    return parts[:max(1, int(max_bubbles))]
