# -*- coding: utf-8 -*-
"""微信 4.x 数据访问层（WechatMoments 适配用）。

接口契约见 ``wx4/API.md``。子模块：

- ``wx4.db``    读取/解密 4.x SQLCipher 数据库（账号、联系人、通用 sqlite 连接）
- ``wx4.sns``   朋友圈时间线、评论、点赞
- ``wx4.media`` 图片/视频/头像的本地缓存定位与解密

对外统一用 :class:`Wx4Error` 表达可读的失败原因（GUI 直接显示 ``str(e)``）。
"""
from __future__ import annotations


class Wx4Error(RuntimeError):
    """微信 4.x 数据层可预期错误（未登录、取不到密钥、库缺失等）。"""


__all__ = ["Wx4Error"]
