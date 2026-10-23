# -*- coding: utf-8 -*-
"""微信 4.x 朋友圈解析层（``sns.db`` / ``SnsTimeLine``）。

接口契约见 ``wx4/API.md`` 2.2。本模块只做「读库 + XML 解析」，不下载/解密任何图片视频。

实测事实（Weixin 4.1.15.56，账号 wxid_hsqe9w1lhl1j22，7 条真实朋友圈）
------------------------------------------------------------------
4.x 的 ``sns.db`` 表结构::

    SnsTimeLine(tid INTEGER PRIMARY KEY DESC, user_name TEXT, content TEXT, pack_info_buf TEXT)

``content`` 是 3.x 同构的 XML（可直接复用 ``entity/moment_msg.py`` 的
``MomentMsg``/``TimelineObject``/``ContentObject``/``Media``），但 4.x 有两处差异：

1. ``SnsDataItem`` 下除了 ``TimelineObject`` 还多了一个**兄弟节点**
   ``LocalExtraInfo``；3.x 的 ``<CommentList>``/``<LikeUserList>`` 在 4.x XML 里**不存在**。
   评论和点赞都在 ``LocalExtraInfo`` 里：

   * ``LocalExtraInfo/comment_user_list/user_comment``：``type=2`` 且带 ``<content>``
     → **评论**（``username``/``nickname``/``content``/``type``/``create_time``）；
     另有 ``comment_content_type=1`` 的**表情包评论**（无 ``<content>``，表情在
     ``emojilist/emojiinfo``）：``content`` 用表情描述或 ``[表情]`` 占位，
     图片评论用 ``[图片]`` 占位，避免 HTML 里出现空行；
   * ``LocalExtraInfo/like_user_list/user_comment``：``type=1``，且**没有** ``<content>``
     → **点赞**（``username`` 是点赞人）。``like_flag`` 只表示「我或好友是否点过赞」，
     不代表具体的人，真正的点赞人名单以 ``like_user_list`` 为准。

2. ``TimelineObject/ContentObject`` 没有 3.x 的 ``<contentStyle>``，只有
   ``<contentSubStyle>``（实测恒为 0）；其余 ``appInfo``/``actionInfo``/``weappInfo``/
   ``streamvideo``/``LivePhoto`` 等是 4.x 新增节点，解析时忽略即可（缺字段不抛异常）。

ID 关系（实测 7/7 一致）：``TimelineObject/id`` == ``LocalExtraInfo/tid`` ==
把 SQLite 有符号 ``SnsTimeLine.tid`` 按 uint64 无符号解释后的十进制值，
例如列值 ``-3412393369079958931`` ↔ ``15034350704629592685``。
本模块的 ``feed_id`` 统一取 ``TimelineObject/id``（十进制字符串转 int，可能是 > 2**63
的大整数），取不到时依次回退 ``LocalExtraInfo/tid`` → 列 ``tid`` 的无符号值。

数据来源
--------
优先用 ``wx4.db.get_db().open("sns/sns.db")``（db-core 提供）。
在 ``wx4/db.py`` 尚未就绪的开发期，会自动降级到项目上层的只读参考实现
``wx_box/recon/ref_db.py``（仅当 ``wx4.db`` 模块不存在时才启用；db.py 落地后无需改代码
即自动切回正式接口）。可用 :func:`data_source` 查看本次实际走的是哪条路径。
"""
from __future__ import annotations

import importlib.util
import logging
import sqlite3
import sys
import threading
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import xmltodict

try:  # 正常从项目根目录运行时走这里
    from entity.comment import Comment
    from entity.moment_msg import (
        ContentObject,
        Location,
        MediaList,
        MomentMsg,
        TimelineObject,
        Url,
    )
except ImportError:  # 容错：从其它 cwd 导入 wx4.sns 时补上项目根目录
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from entity.comment import Comment
    from entity.moment_msg import (
        ContentObject,
        Location,
        MediaList,
        MomentMsg,
        TimelineObject,
        Url,
    )

from . import Wx4Error

LOG = logging.getLogger("wx4.sns")

__all__ = [
    "Moment",
    "list_moments",
    "all_moments",
    "cover_url",
    "parse_feed_xml",
    "data_source",
    "set_connection_factory",
    "COMMENT_TYPE_LIKE",
    "COMMENT_TYPE_COMMENT",
]

#: sns.db 在账号数据目录下的相对路径（大小写/斜杠由 wx4.db 负责归一化）
SNS_DB_REL = "sns/sns.db"

#: LocalExtraInfo/user_comment 的 type 语义（实测）：1 = 点赞，2 = 评论
COMMENT_TYPE_LIKE = 1
COMMENT_TYPE_COMMENT = 2

#: 视频媒体类型（4.x/3.x 一致：2=图片, 5=音乐, 6=视频）
MEDIA_TYPE_IMAGE = "2"
MEDIA_TYPE_MUSIC = "5"
MEDIA_TYPE_VIDEO = "6"

_DEFAULT_END_TS = 2**63 - 1

# 微信的连接/游标不保证线程安全，模块内串行化
_LOCK = threading.RLock()

# 测试/降级注入用（见 set_connection_factory）
_conn_factory: Optional[Callable[[], object]] = None
_dev_db = None  # 保活 dev 的 WeChatDB 实例
_last_source = ""


# ---------------------------------------------------------------------------
# 对外数据结构
# ---------------------------------------------------------------------------


@dataclass
class Moment:
    """一条朋友圈。

    ``moment`` 直接复用 3.x 的 :class:`entity.moment_msg.MomentMsg`
    （``timelineObject.ContentObject.mediaList.media`` 等），GUI/exporter 无需改模板。
    """

    feed_id: int
    username: str
    create_time: int
    moment: MomentMsg
    raw_xml: str
    comments: list[Comment] = field(default_factory=list)
    likes: list[str] = field(default_factory=list)

    def media_md5s(self) -> list[str]:
        """从 ``ContentObject.mediaList`` 提取图片/视频的 URL md5（按出现顺序去重）。

        图片为 ``Media.type == "2"``、视频为 ``"6"``，两者的
        ``Media.url.md5`` 就是缓存文件名（``cache/<月>/Sns/Img/<2位>/<md5>``）。
        音乐（``type == "5"``）不算图片/视频，跳过；``LivePhoto`` 内嵌视频的 md5
        不在 Media 节点上，暂不包含。
        """
        result: list[str] = []
        media_list = _content_media_list(self.moment)
        if media_list is None:
            return result
        for media in media_list.media:
            if media is None:
                continue
            mtype = (media.type or "").strip()
            if mtype == MEDIA_TYPE_MUSIC:
                continue
            url = media.url
            md5 = url.md5.strip() if isinstance(url, Url) and url.md5 else ""
            if md5 and md5 not in result:
                result.append(md5)
        return result

    def to_dict(self) -> dict:
        """可 JSON 序列化的字典（调试/落盘用；不包含 media 的 url token 之外的东西）。"""
        return {
            "feed_id": self.feed_id,
            "username": self.username,
            "create_time": self.create_time,
            "moment": self.moment.to_dict(),
            "raw_xml": self.raw_xml,
            "comments": [
                {
                    "from_user_name": c.from_user_name,
                    "comment_type": c.comment_type,
                    "content": c.content,
                }
                for c in self.comments
            ],
            "likes": list(self.likes),
        }


# ---------------------------------------------------------------------------
# 数据源：优先 wx4.db，开发期降级 wx_box/recon/ref_db.py
# ---------------------------------------------------------------------------


def set_connection_factory(factory: Optional[Callable[[], object]]) -> None:
    """注入 ``sns.db`` 连接工厂（测试/降级用）。

    ``factory()`` 需返回一个可执行 ``SELECT ... FROM SnsTimeLine`` 的 sqlite3 连接。
    传 ``None`` 恢复默认（wx4.db → dev ref_db）。
    """
    global _conn_factory
    _conn_factory = factory


def data_source() -> str:
    """最近一次成功打开数据源所用的路径（诊断用：``wx4.db`` / dev 降级 / injected）。"""
    return _last_source


def _open_conn():
    """返回可读的 sns.db 连接；失败统一抛 :class:`Wx4Error`。"""
    global _last_source

    if _conn_factory is not None:
        try:
            conn = _conn_factory()
        except Wx4Error:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转可读错误
            raise Wx4Error(f"打开微信 4.x 朋友圈数据库失败：{exc}") from exc
        if conn is None:
            raise Wx4Error("没有可用的微信 4.x 朋友圈数据库连接")
        _last_source = "injected"
        return conn

    try:
        from .db import get_db  # type: ignore  # db-core 提供
    except (ImportError, ModuleNotFoundError):
        get_db = None  # type: ignore[assignment]
    except Exception as exc:  # noqa: BLE001 - db.py 损坏时也要给 GUI 可读信息
        raise Wx4Error(
            f"微信 4.x 数据层 wx4.db 加载失败：{type(exc).__name__}: {exc}"
        ) from exc

    if get_db is None:
        # 开发期降级：wx4.db 还没落地时，借用只读参考实现
        conn = _open_conn_via_ref_db()
        if conn is not None:
            _last_source = "dev-fallback:wx_box/recon/ref_db.py"
            LOG.warning("wx4.db 尚未就绪，朋友圈解析降级使用 wx_box/recon/ref_db.py（开发期路径）")
            return conn
        raise Wx4Error(
            "微信 4.x 数据层 wx4.db 尚未就绪，且未找到开发期参考实现 wx_box/recon/ref_db.py"
        )

    try:
        conn = get_db().open(SNS_DB_REL)
    except Wx4Error:
        raise
    except Exception as exc:  # noqa: BLE001
        raise Wx4Error(f"打开微信 4.x 朋友圈数据库失败：{exc}") from exc
    if conn is None:
        raise Wx4Error("没有可用的微信 4.x 朋友圈数据库连接")
    _last_source = "wx4.db"
    return conn


def _open_conn_via_ref_db():
    """开发期降级：用 ``<wx_box>/recon/ref_db.py`` 打开已解密的 sns.db。

    仅在 ``wx4/db.py`` 不存在时启用；ref_db.py 只读，不会被修改。
    找不到参考实现时返回 ``None``。
    """
    global _dev_db

    ref_path = Path(__file__).resolve().parents[2] / "recon" / "ref_db.py"
    if not ref_path.is_file():
        return None

    # ref_db.py 依赖 wechatauto.logger（参考实现不需要真实日志），打桩
    if "wechatauto" not in sys.modules:
        pkg = types.ModuleType("wechatauto")
        pkg.__path__ = []  # type: ignore[attr-defined]
        logger_mod = types.ModuleType("wechatauto.logger")

        class _SilentLogger:
            def __getattr__(self, _name):
                return lambda *a, **k: None

        logger_mod.wxlog = _SilentLogger()  # type: ignore[attr-defined]
        sys.modules["wechatauto"] = pkg
        sys.modules["wechatauto.logger"] = logger_mod

    try:
        spec = importlib.util.spec_from_file_location("wx4_dev_ref_db", ref_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        db = module.WeChatDB()
        rel = next(
            (r for r, _, _ in db._db_files if r.lower().replace("\\", "/").startswith("sns/")),
            None,
        )
        if rel is None:
            return None
        conn = db._open(rel)
        _dev_db = db  # 保活，防止连接被回收
        return conn
    except Exception as exc:  # noqa: BLE001
        LOG.debug("dev ref_db 降级失败：%s", exc)
        return None


# ---------------------------------------------------------------------------
# XML 解析
# ---------------------------------------------------------------------------


def _as_text(value) -> str:
    """把 xmltodict 的任意标量安全转成 str。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value, default: int = 0) -> int:
    """把 xmltodict 的字符串/数字安全转成 int。"""
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default


def _unsigned_tid(tid) -> int:
    """把 SQLite 有符号 int64 列值按 uint64 重新解释（如 -3412... → 15034...）。"""
    try:
        return int(tid) & 0xFFFFFFFFFFFFFFFF
    except (TypeError, ValueError):
        return 0


def _mixed_dict(value, default_type: str = "1") -> Optional[dict]:
    """把 ``<url>text</url>`` / ``<url a="1">text</url>`` 规整成带 ``@type``/``#text`` 的 dict。

    ``entity.moment_msg.Url`` 要求 ``@type`` 必填、``Thumb`` 要求 ``#text`` 必填，
    这里补齐，避免 4.x 未来少字段时抛 KeyError。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return {"@type": default_type, "#text": value}
    if isinstance(value, dict):
        out = dict(value)
        out.setdefault("@type", default_type)
        out.setdefault("#text", "")
        return out
    return None


def _normalize_media(media) -> Optional[dict]:
    """规整单个 ``<media>``；4.x 的 ``LivePhoto``/``size``/``enc`` 等额外节点忽略。"""
    if not isinstance(media, dict):
        return None
    out = dict(media)
    if "url" in out:
        out["url"] = _mixed_dict(out.get("url"))
    if "thumb" in out:
        out["thumb"] = _mixed_dict(out.get("thumb"))
    if out.get("type") is not None:
        out["type"] = _as_text(out["type"])
    if out.get("id") is not None:
        out["id"] = _as_text(out["id"])
    return out


def _normalize_media_list(media_list) -> Optional[dict]:
    """规整 ``mediaList``，保证 ``media`` 一定是 list（配合 force_list={'media'}）。"""
    if media_list is None:
        return None
    if not isinstance(media_list, dict):
        return None
    items = media_list.get("media")
    if items is None:
        return {"media": []}
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return {"media": []}
    media = [_normalize_media(item) for item in items]
    return {"media": [m for m in media if m is not None]}


def _normalize_timeline_object(to_raw: dict) -> dict:
    """把 4.x 的 ``TimelineObject`` 规整成 ``MomentMsg.from_dict`` 能吃的形状。

    主要修 3 处：``ContentObject.contentStyle``（4.x 叫 contentSubStyle）、
    ``location`` 空节点、``ContentObject``/``mediaList`` 缺失。
    """
    to = dict(to_raw)
    to["username"] = _as_text(to.get("username"))
    to["createTime"] = _as_int(to.get("createTime"), 0)
    if to.get("contentDesc") is not None:
        to["contentDesc"] = _as_text(to.get("contentDesc"))
    location = to.get("location")
    if not isinstance(location, dict):
        to["location"] = {}

    co = to.get("ContentObject")
    co = dict(co) if isinstance(co, dict) else {}
    # 4.x 没有 <contentStyle>，<contentSubStyle> 是同一位置上的字段（实测恒 0）。
    if co.get("contentStyle") in (None, ""):
        co["contentStyle"] = _as_int(co.get("contentSubStyle"), 0)
    else:
        co["contentStyle"] = _as_int(co.get("contentStyle"), 0)
    for key in ("contentUrl", "title", "description"):
        if co.get(key) is not None:
            co[key] = _as_text(co[key])
    co["mediaList"] = _normalize_media_list(co.get("mediaList"))
    to["ContentObject"] = co
    return to


def _user_comment_nodes(container) -> list[dict]:
    """取 ``comment_user_list``/``like_user_list`` 下的 ``user_comment`` 列表。

    单条时 xmltodict 给 dict、多条给 list，这里统一成 list。
    """
    if not isinstance(container, dict):
        return []
    nodes = container.get("user_comment")
    if nodes is None:
        return []
    if isinstance(nodes, dict):
        return [nodes]
    if isinstance(nodes, list):
        return [n for n in nodes if isinstance(n, dict)]
    return []


def _first_attr_dict(node, container_key: str, item_key: str) -> Optional[dict]:
    """``emojilist/emojiinfo``、``imagelist/imageinfo`` 单条是 dict、多条是 list。"""
    container = node.get(container_key)
    if not isinstance(container, dict):
        return None
    value = container.get(item_key)
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict):
                return entry
    return None


def _comment_content(node: dict) -> str:
    """评论正文。

    4.x 实测：``comment_content_type=1`` 的评论是**表情包评论**（没有 ``<content>``，
    表情信息在 ``emojilist/emojiinfo`` 里）；``comment_imageinfo_count>0`` 的是图片评论。
    这两种在 3.x ``CommentV20`` 里也是空 Content，为了 HTML 里不出现空行，
    分别用表情描述 / ``[表情]`` / ``[图片]`` 占位。
    """
    content = _as_text(node.get("content")).strip()
    if content:
        return content
    content_type = _as_int(node.get("comment_content_type"), 0)
    emoji = _first_attr_dict(node, "emojilist", "emojiinfo")
    if content_type == 1 or emoji is not None:
        if emoji is not None:
            desc = _as_text(emoji.get("desc")).strip()
            if desc:
                return desc
        return "[表情]"
    if _as_int(node.get("comment_imageinfo_count"), 0) > 0 or _first_attr_dict(
        node, "imagelist", "imageinfo"
    ) is not None:
        return "[图片]"
    return content


def _parse_local_extra(item: dict) -> tuple[list[Comment], list[str]]:
    """解析 ``LocalExtraInfo`` → (评论列表, 点赞人 username 列表)。"""
    comments: list[Comment] = []
    likes: list[str] = []

    extra = item.get("LocalExtraInfo")
    if not isinstance(extra, dict):
        return comments, likes

    # 评论：comment_user_list/user_comment，type=2（实测），带 <content>
    timed: list[tuple[int, Comment]] = []
    for node in _user_comment_nodes(extra.get("comment_user_list")):
        ctype = _as_int(node.get("type"), COMMENT_TYPE_COMMENT)
        if ctype == COMMENT_TYPE_LIKE:  # 防御：误放进评论列表的点赞条目
            continue
        if _as_int(node.get("b_deleted"), 0) == 1:  # 已删除的评论不导出
            continue
        username = _as_text(node.get("username"))
        if not username:
            continue
        timed.append(
            (
                _as_int(node.get("create_time"), 0),
                Comment(
                    from_user_name=username,
                    comment_type=ctype,
                    content=_comment_content(node),
                ),
            )
        )
    # 按评论时间正序（微信里显示的先后顺序）
    timed.sort(key=lambda pair: pair[0])
    comments = [c for _, c in timed]

    # 点赞：like_user_list/user_comment，type=1 且无 <content>，username 即点赞人
    for node in _user_comment_nodes(extra.get("like_user_list")):
        username = _as_text(node.get("username"))
        if username and username not in likes:
            likes.append(username)

    return comments, likes


def _extract_feed_id(item: dict, to: dict, db_tid) -> int:
    """feed_id 优先 TimelineObject/id，回退 LocalExtraInfo/tid、最后回退列 tid 的无符号值。"""
    feed_id = _as_int(to.get("id"), 0)
    if feed_id <= 0:
        extra = item.get("LocalExtraInfo")
        if isinstance(extra, dict):
            feed_id = _as_int(extra.get("tid"), 0)
    if feed_id <= 0:
        feed_id = _unsigned_tid(db_tid)
    return feed_id


def _degraded_moment(to_raw: dict, db_user_name: str = "") -> MomentMsg:
    """严格解码失败时的兜底构造：保留用户名/时间/文案/位置，丢弃 media。"""
    to = to_raw if isinstance(to_raw, dict) else {}
    location_raw = to.get("location") if isinstance(to.get("location"), dict) else {}
    co_raw = to.get("ContentObject") if isinstance(to.get("ContentObject"), dict) else {}
    location = Location(
        poiName=_as_text(location_raw.get("poiName")),
        longitude=_as_text(location_raw.get("longitude")),
        latitude=_as_text(location_raw.get("latitude")),
        country=_as_text(location_raw.get("country")),
    )
    content_object = ContentObject(
        contentStyle=_as_int(co_raw.get("contentStyle"), _as_int(co_raw.get("contentSubStyle"), 0)),
        contentUrl=_as_text(co_raw.get("contentUrl")),
        title=_as_text(co_raw.get("title")),
        description=_as_text(co_raw.get("description")),
        mediaList=None,
        finderFeed=None,
    )
    timeline = TimelineObject(
        username=_as_text(to.get("username")) or _as_text(db_user_name),
        location=location,
        ContentObject=content_object,
        createTime=_as_int(to.get("createTime"), 0),
        contentDesc=_as_text(to.get("contentDesc")),
    )
    return MomentMsg(timelineObject=timeline)


def parse_feed_xml(xml, db_tid=None, db_user_name: str = "") -> Moment:
    """把 ``SnsTimeLine.content`` 解析成 :class:`Moment`。

    :param xml: ``SnsTimeLine.content`` 原文（可以是 bytes）
    :param db_tid: 可选，``SnsTimeLine.tid`` 列值（有符号），仅在 XML 里取不到
        ``TimelineObject/id``/``LocalExtraInfo/tid`` 时用来兜底 ``feed_id``
    :param db_user_name: 可选，``SnsTimeLine.user_name`` 列值，XML 里 username 为空时兜底
    :raises Wx4Error: XML 为空、不是朋友圈 XML、或缺 ``TimelineObject`` 等不可恢复错误
    """
    text = _as_text(xml).strip()
    if not text:
        raise Wx4Error("朋友圈 XML 内容为空")

    try:
        data = xmltodict.parse(text, force_list={"media"})
    except Exception as exc:  # noqa: BLE001 - xmltodict.ExpatError 等
        raise Wx4Error(f"朋友圈 XML 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise Wx4Error("朋友圈 XML 解析失败：根节点缺失")

    item = data.get("SnsDataItem")
    if not isinstance(item, dict):
        # 兼容：个别版本/测试样本直接以 TimelineObject 为根
        if isinstance(data.get("TimelineObject"), dict):
            item = {
                "TimelineObject": data["TimelineObject"],
                "LocalExtraInfo": data.get("LocalExtraInfo"),
            }
        else:
            raise Wx4Error("朋友圈 XML 缺少 SnsDataItem/TimelineObject 节点")

    to_raw = item.get("TimelineObject")
    if not isinstance(to_raw, dict):
        raise Wx4Error("朋友圈 XML 缺少 TimelineObject 节点")

    try:
        to = _normalize_timeline_object(to_raw)
        moment = MomentMsg.from_dict({"TimelineObject": to})
    except Exception as exc:  # noqa: BLE001 - dataclasses_json 缺字段/类型不符
        LOG.warning("朋友圈 XML 严格解析失败，使用兜底解析（会丢 media）：%s", exc)
        to = to_raw
        moment = _degraded_moment(to_raw, db_user_name)

    comments, likes = _parse_local_extra(item)

    timeline_object = moment.timelineObject
    create_time = _as_int(getattr(timeline_object, "createTime", 0), 0)
    username = _as_text(getattr(timeline_object, "username", "")) or _as_text(db_user_name)
    feed_id = _extract_feed_id(item, to, db_tid)

    return Moment(
        feed_id=feed_id,
        username=username,
        create_time=create_time,
        moment=moment,
        raw_xml=text,
        comments=comments,
        likes=likes,
    )


# ---------------------------------------------------------------------------
# 读库 / 去重
# ---------------------------------------------------------------------------


def _content_media_list(moment: MomentMsg) -> Optional[MediaList]:
    """安全取 ``timelineObject.ContentObject.mediaList``。"""
    timeline = getattr(moment, "timelineObject", None)
    content = getattr(timeline, "ContentObject", None) if timeline is not None else None
    if content is None:
        return None
    return getattr(content, "mediaList", None)


def _richness(moment: Moment) -> tuple[int, int, int]:
    """同一条 feed 多份快照时，取「信息最全」的那份。"""
    return (
        len(moment.comments) + len(moment.likes),
        len(moment.media_md5s()),
        int(moment.create_time),
    )


def _dedupe_moments(moments: Iterable[Moment]) -> list[Moment]:
    """按 ``feed_id`` 去重（保留评论/点赞/媒体信息最多的快照，其次保留先出现的）。"""
    best: dict[int, Moment] = {}
    order: list[int] = []
    for moment in moments:
        old = best.get(moment.feed_id)
        if old is None:
            best[moment.feed_id] = moment
            order.append(moment.feed_id)
        elif _richness(moment) > _richness(old):
            best[moment.feed_id] = moment
    return [best[feed_id] for feed_id in order]


def _read_rows(conn) -> list[tuple]:
    """``SELECT tid, user_name, content FROM SnsTimeLine``。"""
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT tid, user_name, content FROM SnsTimeLine ORDER BY tid DESC")
            return list(cursor.fetchall())
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            raise Wx4Error(
                "微信 4.x sns.db 中找不到 SnsTimeLine 表，当前微信版本可能不受支持"
            ) from exc
        raise Wx4Error(f"读取微信 4.x 朋友圈表失败：{exc}") from exc
    except sqlite3.Error as exc:
        raise Wx4Error(f"读取微信 4.x 朋友圈表失败：{exc}") from exc


def _load_moments() -> list[Moment]:
    """读全部朋友圈（已去重、未排序），失败统一抛 Wx4Error。"""
    with _LOCK:
        conn = _open_conn()
        rows = _read_rows(conn)
        moments: list[Moment] = []
        failures = 0
        last_error = ""
        for row in rows:
            try:
                tid, user_name, content = row[0], row[1], row[2]
            except (TypeError, IndexError):
                failures += 1
                continue
            try:
                moments.append(parse_feed_xml(content, db_tid=tid, db_user_name=user_name))
            except Wx4Error as exc:
                failures += 1
                last_error = str(exc)
                LOG.warning("跳过一条无法解析的朋友圈（tid=%s）：%s", tid, exc)
        if rows and not moments:
            raise Wx4Error(f"微信 4.x 朋友圈全部解析失败（{failures} 条）：{last_error}")
        if failures:
            LOG.warning("朋友圈解析：成功 %d 条，跳过 %d 条", len(moments), failures)
        return _dedupe_moments(moments)


# ---------------------------------------------------------------------------
# 对外查询接口
# ---------------------------------------------------------------------------


def list_moments(start_ts: int, end_ts: int) -> list[Moment]:
    """按 ``create_time`` 闭区间 ``[start_ts, end_ts]`` 取朋友圈，按时间倒序返回。

    ``start_ts``/``end_ts`` 允许传 ``None`` 表示该端不限。时间取自
    ``TimelineObject/createTime``（秒）。4.x 的 ``SnsTimeLine`` 表没有独立的时间列
    （时间在 XML 里），所以先读全表再在 Python 侧过滤；单表最多几千行，毫秒级。
    """
    lo = 0 if start_ts is None else _as_int(start_ts, 0)
    hi = _DEFAULT_END_TS if end_ts is None else _as_int(end_ts, _DEFAULT_END_TS)
    if hi < lo:
        return []
    result = [m for m in _load_moments() if lo <= m.create_time <= hi]
    result.sort(key=lambda m: (m.create_time, m.feed_id), reverse=True)
    return result


def all_moments() -> list[Moment]:
    """全部朋友圈，按时间倒序。"""
    return list_moments(0, _DEFAULT_END_TS)


def cover_url() -> str | None:
    """朋友圈封面图 URL。

    3.x 取 ``SnsConfigV20`` 表 ``Key="6"``；4.1.15.56 的 sns.db **没有**任何
    config/cover 表（实测 13 张表：SnsAdTimeLine/SnsDraft/SnsErrorMessage/
    SnsIgnoredDataItem/SnsMainTimeLineBreakFlag/SnsMessage_tmp3/SnsNoteVoice/
    SnsPendingDraftDeletionTable/SnsPublishTask/SnsTimeLine/SnsTopItem_1/
    SnsUserTimeLineBreakFlagV2/sqlite_sequence），因此按契约返回 ``None``，
    导出 HTML 时用默认背景即可。
    """
    return None
