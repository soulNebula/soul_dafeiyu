# -*- coding: utf-8 -*-
"""V1 PR9：B站能力入库（D3）——扫码登录 / 推荐流 / 关键词 / 展示级弹幕评论。

为什么在仓库内
--------------
`docs/plans/63` 的 D3 已拍板：V1 第 6 步（B站扫码）不能依赖仓库外的
``~/.dsh/bili-duty`` 插件，新 clone 必须可用。本包用**纯标准库**（urllib）实现：

- :mod:`agi.integrations.bili.auth` —— 扫码登录（生成/轮询/状态）、凭证加密落盘；
- :mod:`agi.integrations.bili.feed` —— 推荐流（rcmd → 热门降级）、关键词搜索；
- :mod:`agi.integrations.bili.keywords` —— 关键词解析（用户填写优先，留空走
  ``extract`` 路由让 LLM 自选，LLM 不可用时退化为标题词频）；
- :mod:`agi.integrations.bili.video` —— 展示级评论/弹幕拉取（**只给 802 展示开关用**，
  不进训练/入库主链，见 doc 64 第十节）；
- :mod:`agi.integrations.bili.worker` —— 一轮刷（限频 + 关键词过滤 + 状态落盘）。

降级：网络/接口失败一律返回 ``{"ok": False, "error": ...}`` 或抛 :class:`BiliError`
（由调用方决定展示），不伪造数据；没有凭证时推荐流走热门（匿名可用）。
"""
from __future__ import annotations

from .common import (BiliError, configure_rate_limit, credential_path,
                     credential_state,
                     http_bytes, http_json, import_legacy_credential,
                     load_credential, reset_rate_limit, save_credential)

__all__ = [
    "BiliError",
    "configure_rate_limit",
    "credential_path",
    "credential_state",
    "http_bytes",
    "http_json",
    "import_legacy_credential",
    "load_credential",
    "reset_rate_limit",
    "save_credential",
]

__version__ = "1.0.0"