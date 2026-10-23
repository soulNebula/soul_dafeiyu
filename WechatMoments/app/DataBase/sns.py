# -*- coding: utf-8 -*-
"""朋友圈数据访问层（微信 4.x 实现）。

原版（3.x）直接读 pywxdump 解密出来的 ``app/Database/Msg/Sns.db``：

- ``FeedsV20(UserName, Content, FeedId)``
- ``CommentV20(FeedId, FromUserName, CommentType, Content)``
- ``SnsConfigV20(Key="6", StrValue)``（朋友圈封面）

4.x 改为走 :mod:`wx4.sns`（SQLCipher 解密后的 ``sns/SnsTimeLine`` + XML 解析），
对外保持 **同名函数、同形返回值**，这样 ``exporter/html_exporter.py`` 无需改动：

- :meth:`Sns.get_messages_in_time` -> ``[(username, xml_content, feed_id), ...]``
- :meth:`Sns.get_comment_by_feed_id` -> ``[(from_user_name, comment_type, content), ...]``
- :meth:`Sns.get_cover_url` -> ``str | None``

其中 ``xml_content`` 由 4.x 的 ``MomentMsg`` 反向序列化回 **3.x 形状**（根节点
``<TimelineObject>``，并且补上 4.x 缺失的 ``<contentStyle>``），
``html_exporter`` 里 ``xmltodict.parse + MomentMsg.from_json`` 那套解析可以原样复用。
"""
from __future__ import annotations

import os.path
import threading
import traceback
from pathlib import Path
from typing import Optional

import xmltodict

from log import LOG

# 仅保留变量名兼容（4.x 不再直接把 Sns.db 解密到这个路径）
db_path = "./app/Database/Msg/Sns.db"
lock = threading.Lock()

# 4.x 时间上限（list_moments 用闭区间，None 不便于透传时用这个）
_MAX_TS = 2 ** 63 - 1

_wx4_available: Optional[bool] = None


def is_database_exist() -> bool:
    """4.x 无需预解密文件：探测 wx4.sns 是否可导入即可。"""
    global _wx4_available
    if _wx4_available is None:
        try:
            import wx4.sns  # noqa: F401  （只探测，不连接数据库）
            _wx4_available = True
        except Exception:
            LOG.error("wx4.sns 不可用：\n%s", traceback.format_exc())
            _wx4_available = False
    return bool(_wx4_available)


def singleton(cls):
    _instance = {}

    def inner():
        if cls not in _instance:
            _instance[cls] = cls()
        return _instance[cls]

    return inner


def _default_cover_path() -> Optional[str]:
    """4.x 没有朋友圈封面表：在项目缓存目录生成一张中性占位封面。

    ``resource/template.html`` 的顶部有 ``<img src="{cover_path}">``，原版 3.x 用
    ``SnsConfigV20`` 的封面 URL 替换；4.x 无此数据，若不替换 HTML 里会残留字面量
    ``{cover_path}``（坏图）。这里返回一张本地占位图路径，``ImageExporter.save_image``
    会把它复制进 output 并正常替换，保证导出的 HTML 没有残留占位符。
    """
    try:
        cache_dir = Path(__file__).resolve().parents[2] / ".wx4_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / "default_cover.jpg"
        if not path.exists():
            from PIL import Image, ImageDraw

            image = Image.new("RGB", (900, 400), (58, 62, 70))
            draw = ImageDraw.Draw(image)
            for y in range(400):
                shade = int(58 + y / 400 * 26)
                draw.line([(0, y), (900, y)], fill=(shade, shade + 4, shade + 10))
            image.save(path, "JPEG", quality=88)
        return str(path)
    except Exception:
        LOG.warning("生成默认封面占位图失败：\n%s", traceback.format_exc())
        return None


def _serialize_moment_to_3x_xml(moment) -> str:
    """把 ``wx4.sns.Moment.moment``（MomentMsg）还原成 3.x ``FeedsV20.Content`` 的 XML。

    ``MomentMsg`` 的字段名由 ``entity/moment_msg.py`` 的 dataclasses_json 配置决定，
    ``to_dict()`` 产出 ``{"TimelineObject": {...}}``；再用 xmltodict.unparse 写回 XML。
    """
    data = moment.moment.to_dict()
    timeline = data.get("TimelineObject") or data.get("timelineObject") or {}
    timeline = dict(timeline)

    content = timeline.get("ContentObject")
    if isinstance(content, dict):
        content = dict(content)
        content.setdefault("contentStyle", 0)
        try:
            content["contentStyle"] = int(content["contentStyle"] or 0)
        except (TypeError, ValueError):
            content["contentStyle"] = 0
        # 3.x 用 contentStyle==3 表示「链接/分享」卡片；4.x 都塞在 contentSubStyle(0)。
        # 有 contentUrl+title 且没有视频时补成 3，让 HTML 走原来的链接样式分支。
        media_items = ((content.get("mediaList") or {}).get("media") or [])
        has_video = any(
            isinstance(m, dict) and str(m.get("type")) == "6" for m in media_items
        )
        if (
            content["contentStyle"] == 0
            and not has_video
            and content.get("contentUrl")
            and content.get("title")
        ):
            content["contentStyle"] = 3
        timeline["ContentObject"] = content

    return xmltodict.unparse({"TimelineObject": timeline}, full_document=False)


@singleton
class Sns:
    def __init__(self):
        # 兼容原版属性（GUI/其它模块可能读取 open_flag）
        self.DB = None
        self.cursor = None
        self.open_flag = False
        self._lock = threading.RLock()
        # 最近一次查询缓存：feed_id -> Moment，避免每条 feed 都重新读库
        self._moments: dict = {}
        self._last_range = None
        self.init_database()

    def init_database(self, path=None):
        # 4.x：连接由 wx4 惰性提供；密钥在 decrypter/db_decrypt 阶段预热，这里不连库
        self.open_flag = is_database_exist()
        return self.open_flag

    def _load(self, start_time, end_time):
        import wx4.sns as wx4_sns

        moments = wx4_sns.list_moments(int(start_time), int(end_time))
        with self._lock:
            self._moments = {m.feed_id: m for m in moments}
            self._order = [m.feed_id for m in moments]
            self._last_range = (int(start_time), int(end_time))
        return moments

    def _all_moments(self):
        import wx4.sns as wx4_sns

        moments = wx4_sns.all_moments()
        with self._lock:
            self._moments = {m.feed_id: m for m in moments}
            self._order = [m.feed_id for m in moments]
            self._last_range = (0, _MAX_TS)
        return moments

    def _get_moment(self, feed_id):
        with self._lock:
            moment = self._moments.get(feed_id)
        if moment is not None:
            return moment
        try:
            moments = self._all_moments()
        except Exception:
            LOG.error("读取朋友圈失败：\n%s", traceback.format_exc())
            return None
        for m in moments:
            if m.feed_id == feed_id:
                return m
        return None

    def get_messages_in_time(self, start_time, end_time):
        """按时间闭区间取朋友圈，返回 ``[(username, xml_content, feed_id), ...]``。

        失败时返回空列表并写日志（保持 GUI 不崩），e2e 脚本会另外断言条目数。
        """
        if not self.init_database():
            return []
        try:
            moments = self._load(start_time, end_time)
        except Exception:
            LOG.error("读取微信 4.x 朋友圈失败：\n%s", traceback.format_exc())
            return []

        rows = []
        for moment in moments:
            try:
                xml_content = _serialize_moment_to_3x_xml(moment)
            except Exception:
                LOG.error("朋友圈 XML 回写失败 feed_id=%s：\n%s", moment.feed_id,
                          traceback.format_exc())
                continue
            rows.append((moment.username, xml_content, moment.feed_id))
        return rows

    def get_comment_by_feed_id(self, feed_id):
        """返回 ``[(from_user_name, comment_type, content), ...]``（同原 CommentV20）。"""
        moment = self._get_moment(feed_id)
        if moment is None:
            return []
        return [(c.from_user_name, c.comment_type, c.content) for c in moment.comments]

    def get_likes_by_feed_id(self, feed_id) -> list[str]:
        """返回点赞人 username 列表（4.x 新增能力，原版没有单独接口）。"""
        moment = self._get_moment(feed_id)
        if moment is None:
            return []
        return list(moment.likes)

    def get_cover_url(self) -> Optional[str]:
        """朋友圈封面：3.x 读 SnsConfigV20；4.x 无封面数据，返回本地占位图路径。"""
        try:
            import wx4.sns as wx4_sns

            cover = wx4_sns.cover_url()
            if cover:
                return cover
        except Exception:
            LOG.error("读取朋友圈封面失败：\n%s", traceback.format_exc())
        return _default_cover_path()

    def close(self):
        # 连接由 wx4.db 单例管理，这里无需关闭
        self.open_flag = False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


if __name__ == '__main__':
    pass
