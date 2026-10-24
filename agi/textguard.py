"""出站文本消毒：把微信内部 ID 从任何要发出去/要进提示词的文本里摘掉。

背景（2026-10-24 线上事故）：好友引用了一条消息，桥接把 refermsg 的
``content`` 原样当成文字，回了一句
``你引用的是「wxid_0v0g2mpf37u322:1792801924481:1:27970b...」，想接着说什么？``
——把微信内部 ID 直接发给了真人。

上游（``wx_core.db_source``）已修成按 refermsg 类型解析；这里是**纵深防御**：
网关的出站文本、要喂给模型的引用内容，都再过一遍本模块。

注意：``agi/`` 与 ``wx_core/`` 刻意不互相 import（网关只吃 dict 事件），
所以本模块独立实现、只用标准库；纯函数、无 IO，便于单测。
"""
from __future__ import annotations

import re

#: 典型形态：``wxid_xxx:1792801924481:1:27970b…::0``（用户名:服务器ID:类型:摘要）
_COMPOSITE = re.compile(
    r"wxid_[0-9A-Za-z_-]{4,}:\d{6,}(?::\d+)*:[0-9a-fA-F]{8,}(?:::\d+)?"
)
#: 没有 wxid 前缀的复合 ID：``1792801924481:1:27970b…``（服务器ID:类型:摘要）
_BARE_COMPOSITE = re.compile(
    r"(?<![\w])\d{10,}:\d{1,3}:[0-9a-fA-F]{16,}(?:::\d+)?"
)
#: 单独的 wxid（正常聊天里不该出现）
_WXID = re.compile(r"\bwxid_[0-9a-z]{6,}\b")
#: 32 位以上裸 hash 带 ``::N`` 后缀（微信内部消息键常见形态）；
#: 只有带后缀时才摘，避免误伤用户正常讨论的 md5/sha。
_HASH_SUFFIX = re.compile(r"(?<![\w])[0-9a-fA-F]{32,}::\d+(?![\w])")

#: 命中内部 ID 时统一替换成这个（人话占位）
PLACEHOLDER = "（内部 ID）"
_PATTERNS = (_COMPOSITE, _BARE_COMPOSITE, _WXID, _HASH_SUFFIX)


def looks_like_internal_id(text: str) -> bool:
    """判断文本里是否含微信内部 ID（供调用方决定要不要整体降级）。"""
    s = str(text or "")
    return any(pat.search(s) for pat in _PATTERNS)


def strip_internal_ids(text: str, *, placeholder: str = "") -> str:
    """把内部 ID 摘掉。

    ``placeholder=""``（默认）时直接删除，用于引用内容；
    传 ``PLACEHOLDER`` 时替换成可读占位，用于必须保留句子结构的场合。
    """
    s = str(text or "")
    for pat in _PATTERNS:
        s = pat.sub(placeholder, s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip(" \t\r\n：:，,、;；")
