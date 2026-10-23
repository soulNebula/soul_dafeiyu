# -*- coding: utf-8 -*-
"""``wx4.sns`` 自测（真实微信 4.x 数据，只读）。

运行::

    C:\\Users\\Administrator\\Desktop\\wx_box\\WechatMoments\\.venv\\Scripts\\python.exe _dev\\test_sns.py

覆盖（契约见 ``wx4/API.md`` 2.2）：
1. 真实数据至少解析出 7 条朋友圈，逐条打印 create_time/username/contentDesc/
   媒体 md5/评论数/点赞数；
2. ``list_moments`` 时间闭区间过滤 + 倒序；
3. 按 ``feed_id`` 去重 + 二次调用结果稳定；
4. ``MomentMsg``/``Moment.to_dict()`` JSON 序列化不回环、可 round-trip；
5. 评论 type 语义（2=评论）、点赞 username 列表；
6. ``cover_url()`` 在 4.x 返回 None；
7. 数据层异常统一为 ``wx4.Wx4Error``。
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:  # 终端可能是 GBK，统一按 UTF-8 输出，避免中文乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from entity.comment import Comment  # noqa: E402
from entity.moment_msg import MomentMsg  # noqa: E402
from wx4 import Wx4Error  # noqa: E402
from wx4 import sns  # noqa: E402

BJT = timezone(timedelta(hours=8))
_FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    if not cond:
        _FAILS.append(name)
    print(f"[{status}] {name}" + (f"    {detail}" if detail else ""))


def fmt_ts(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts, BJT).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return f"<bad ts {ts}>"


def media_desc(moment: sns.Moment) -> str:
    """媒体摘要：type:md5 前 8 位。"""
    content = moment.moment.timelineObject.ContentObject
    media_list = getattr(content, "mediaList", None)
    if not media_list or not media_list.media:
        return "-"
    parts = []
    for media in media_list.media:
        if media is None:
            continue
        url = media.url
        md5 = url.md5 if hasattr(url, "md5") else ""
        parts.append(f"{media.type or '?'}:{str(md5)[:8]}")
    return ",".join(parts) if parts else "-"


def print_moments(moments: list[sns.Moment]) -> None:
    print("\n== 每条朋友圈 ==")
    header = (
        f"{'#':>2} {'create_time':<20} {'username':<24} {'desc':<18} "
        f"{'media':<26} {'评':>2} {'赞':>2} {'md5s':>4}"
    )
    print(header)
    print("-" * len(header))
    for idx, moment in enumerate(moments):
        desc = (moment.moment.timelineObject.contentDesc or "").replace("\n", " ")
        if len(desc) > 16:
            desc = desc[:16] + ".."
        print(
            f"{idx:>2} {fmt_ts(moment.create_time):<20} {moment.username:<24} "
            f"{desc or '<无文案>':<18} {media_desc(moment):<26} "
            f"{len(moment.comments):>2} {len(moment.likes):>2} {len(moment.media_md5s()):>4}"
        )


def print_comments_and_likes(moments: list[sns.Moment]) -> None:
    print("\n== 评论 / 点赞明细 ==")
    for moment in moments:
        if not moment.comments and not moment.likes:
            continue
        print(f"- feed_id={moment.feed_id} ({fmt_ts(moment.create_time)}, {moment.username})")
        for comment in moment.comments:
            print(
                f"    评论 type={comment.comment_type} "
                f"{comment.from_user_name}: {comment.content!r}"
            )
        if moment.likes:
            print(f"    点赞 {len(moment.likes)} 人: {', '.join(moment.likes)}")


def test_basic(moments: list[sns.Moment]) -> None:
    print("\n== 1. 基础字段 ==")
    check("至少解析出 7 条朋友圈", len(moments) >= 7, f"实际 {len(moments)} 条")
    check("feed_id 全部为正整数", all(isinstance(m.feed_id, int) and m.feed_id > 0 for m in moments))
    check("create_time 全部 > 0", all(isinstance(m.create_time, int) and m.create_time > 0 for m in moments))
    check("username 非空", all(bool(m.username) for m in moments))
    check(
        "Moment.username 与 XML 一致",
        all(m.username == m.moment.timelineObject.username for m in moments),
    )
    check(
        "Moment.create_time 与 XML 一致",
        all(m.create_time == m.moment.timelineObject.createTime for m in moments),
    )
    check("raw_xml 保留原文", all("<TimelineObject>" in m.raw_xml for m in moments))
    check(
        "media_md5s() 元素均为 32 位小写 hex",
        all(
            len(md5) == 32 and md5 == md5.lower() and all(c in "0123456789abcdef" for c in md5)
            for m in moments
            for md5 in m.media_md5s()
        ),
    )
    check(
        "至少一条有图片/视频 md5",
        sum(1 for m in moments if m.media_md5s()) >= 1,
        f"{sum(1 for m in moments if m.media_md5s())} 条有媒体",
    )
    check(
        "至少一条有评论",
        any(m.comments for m in moments),
        f"总评论 {sum(len(m.comments) for m in moments)} 条",
    )
    check(
        "至少一条有点赞",
        any(m.likes for m in moments),
        f"总点赞 {sum(len(m.likes) for m in moments)} 个",
    )
    check(
        "评论元素是 entity.comment.Comment",
        all(isinstance(c, Comment) for m in moments for c in m.comments),
    )
    check(
        "评论 comment_type 恒为 2（4.x 实测：1=点赞, 2=评论）",
        all(c.comment_type == sns.COMMENT_TYPE_COMMENT for m in moments for c in m.comments),
    )
    check(
        "评论内容非空",
        all(isinstance(c.content, str) and c.content for m in moments for c in m.comments),
    )
    placeholders = [
        c.content for m in moments for c in m.comments if c.content in ("[表情]", "[图片]")
    ]
    print(
        f"       表情包/图片评论占位: {len(placeholders)} 条"
        + (f" {placeholders}" if placeholders else "")
    )
    check(
        "点赞元素是非空 username 字符串且无重复",
        all(
            isinstance(u, str) and u and len(set(m.likes)) == len(m.likes)
            for m in moments
            for u in m.likes
        ),
    )


def test_time_filter(all_list: list[sns.Moment]) -> None:
    print("\n== 2. list_moments 时间过滤（闭区间 + 倒序） ==")
    times = sorted(m.create_time for m in all_list)
    lo, hi = times[0], times[-1]
    check("全区间等于 all_moments()", [m.feed_id for m in sns.list_moments(lo, hi)] == [m.feed_id for m in all_list])
    check(
        "时间倒序",
        all(all_list[i].create_time >= all_list[i + 1].create_time for i in range(len(all_list) - 1)),
    )

    # 单点区间（含边界）：取中间那条的时间
    mid = times[len(times) // 2]
    expect = {m.feed_id for m in all_list if m.create_time == mid}
    got = {m.feed_id for m in sns.list_moments(mid, mid)}
    check("闭区间 [t, t] 只返回该秒的 feed", got == expect, f"t={fmt_ts(mid)} 命中 {len(got)}/{len(expect)}")

    # 窄区间
    narrow = sns.list_moments(times[1], times[2])
    check(
        "窄区间结果都落在区间内",
        all(times[1] <= m.create_time <= times[2] for m in narrow),
        f"{len(narrow)} 条",
    )
    edge = {m.feed_id for m in all_list if times[1] <= m.create_time <= times[2]}
    check("窄区间结果无漏", {m.feed_id for m in narrow} == edge)

    # 空区间
    check("空区间返回 []", sns.list_moments(hi + 86400, hi + 172800) == [])
    check("倒置区间返回 []", sns.list_moments(hi, lo) == [])
    check("start_ts=None 视为不限", sns.list_moments(None, hi) == all_list)
    check("end_ts=None 视为不限", sns.list_moments(lo, None) == all_list)


def test_dedupe(moments: list[sns.Moment]) -> None:
    print("\n== 3. feed_id 去重 ==")
    feed_ids = [m.feed_id for m in moments]
    check("结果里 feed_id 唯一", len(feed_ids) == len(set(feed_ids)), f"{len(feed_ids)} 条")
    check(
        "feed_id 来自 XML TimelineObject/id（含 >2**63 的大整数）",
        all(f"<id>{m.feed_id}</id>" in m.raw_xml for m in moments),
    )
    check(
        "至少一个 feed_id > 2**63（4.x 无符号大整数）",
        any(m.feed_id > 2**63 for m in moments),
        f"max={max(feed_ids)}" if feed_ids else "",
    )

    # 二次调用稳定（同样数据源、同样顺序）
    again = sns.all_moments()
    check("二次调用结果一致", [m.feed_id for m in again] == feed_ids)

    # 直接验证去重函数：同一 feed 的「完整版」应替换「残缺版」
    if moments:
        full = moments[0]
        import copy

        # 残缺版：清空评论点赞（模拟重复拉取时信息更少的那份）
        poor = copy.deepcopy(full)
        poor.comments = []
        poor.likes = []
        fake_dup = copy.deepcopy(full)  # feed_id 相同
        merged = sns._dedupe_moments([poor, fake_dup])  # noqa: SLF001 - 自测允许触内部
        check("去重后只留 1 条", len(merged) == 1)
        check(
            "去重保留信息更全的快照",
            merged[0] is fake_dup and len(merged[0].comments) == len(full.comments),
        )
        merged2 = sns._dedupe_moments([fake_dup, poor])
        check("去重与输入顺序无关（保留更全的）", merged2[0] is fake_dup)


def test_serialization(moments: list[sns.Moment]) -> None:
    print("\n== 4. dataclass 序列化不回环 ==")
    ok_json = True
    ok_roundtrip = True
    for moment in moments:
        try:
            text = json.dumps(moment.to_dict(), ensure_ascii=False)
            ok_json = ok_json and json.loads(text)["feed_id"] == moment.feed_id
        except Exception:  # noqa: BLE001
            ok_json = False
            traceback.print_exc()
            break
        try:
            # MomentMsg 自身 to_json → from_json 不递归爆栈、字段保持
            msg = MomentMsg.from_json(moment.moment.to_json())
            ok_roundtrip = (
                ok_roundtrip
                and msg.timelineObject.username == moment.moment.timelineObject.username
                and msg.timelineObject.createTime == moment.moment.timelineObject.createTime
                and len(getattr(msg.timelineObject.ContentObject.mediaList, "media", []) or [])
                == len(
                    getattr(moment.moment.timelineObject.ContentObject.mediaList, "media", []) or []
                )
            )
        except Exception:  # noqa: BLE001
            ok_roundtrip = False
            traceback.print_exc()
            break
    check("Moment.to_dict() 可 json.dumps", ok_json)
    check("MomentMsg to_json/from_json round-trip 一致", ok_roundtrip)


def test_cover() -> None:
    print("\n== 5. cover_url ==")
    cover = sns.cover_url()
    check("4.x 无封面配置表时 cover_url() 返回 None", cover is None, repr(cover))


def test_errors() -> None:
    print("\n== 6. 异常统一为 Wx4Error ==")

    def boom():
        raise RuntimeError("模拟数据层故障")

    old = sns.data_source()
    try:
        sns.set_connection_factory(boom)
        try:
            sns.list_moments(0, 2**63 - 1)
        except Wx4Error as exc:
            check("连接工厂抛 RuntimeError 时包成 Wx4Error", True, str(exc)[:60])
        except Exception as exc:  # noqa: BLE001
            check("连接工厂抛 RuntimeError 时包成 Wx4Error", False, f"{type(exc).__name__}: {exc}")
        else:
            check("连接工厂抛 RuntimeError 时包成 Wx4Error", False, "未抛异常")

        sns.set_connection_factory(lambda: None)
        try:
            sns.list_moments(0, 2**63 - 1)
        except Wx4Error as exc:
            check("连接工厂返回 None 时抛 Wx4Error", True, str(exc)[:60])
        except Exception as exc:  # noqa: BLE001
            check("连接工厂返回 None 时抛 Wx4Error", False, f"{type(exc).__name__}: {exc}")
        else:
            check("连接工厂返回 None 时抛 Wx4Error", False, "未抛异常")

        # 坏 XML：单条不可解析时 parse_feed_xml 抛 Wx4Error
        for bad in ("", "<SnsDataItem></SnsDataItem>"):
            try:
                sns.parse_feed_xml(bad)
            except Wx4Error:
                check(f"坏 XML 抛 Wx4Error（{bad[:16]!r}）", True)
            else:
                check(f"坏 XML 抛 Wx4Error（{bad[:16]!r}）", False, "未抛异常")
    finally:
        sns.set_connection_factory(None)
        check("恢复默认数据源", sns.data_source() == old, f"{sns.data_source()!r}")


def test_missing_fields() -> None:
    """缺 LocalExtraInfo / media / contentDesc 时不得抛异常（4.x 缺字段兼容）。"""
    print("\n== 7. 缺字段兼容 ==")
    xml_no_extra = (
        '<SnsDataItem><TimelineObject><id>123</id><username>wxid_x</username>'
        "<createTime>1700000000</createTime><contentDesc>纯文字</contentDesc>"
        "<ContentObject><type>1</type><contentSubStyle>0</contentSubStyle></ContentObject>"
        "<location/></TimelineObject></SnsDataItem>"
    )
    try:
        moment = sns.parse_feed_xml(xml_no_extra, db_tid=-1, db_user_name="wxid_x")
        check(
            "无 LocalExtraInfo/media 也能解析",
            moment.feed_id == 123
            and moment.create_time == 1700000000
            and moment.comments == []
            and moment.likes == []
            and moment.media_md5s() == [],
            f"feed_id={moment.feed_id} desc={moment.moment.timelineObject.contentDesc!r}",
        )
    except Exception as exc:  # noqa: BLE001
        check("无 LocalExtraInfo/media 也能解析", False, f"{type(exc).__name__}: {exc}")

    # 空 mediaList / 空 user_comment / contentDesc 缺失
    xml_empty_media = (
        '<SnsDataItem><TimelineObject><username>wxid_x</username><createTime>1700000001</createTime>'
        "<ContentObject><type>1</type><mediaList><media>"
        "<id>1</id><type>2</type><url/></media></mediaList></ContentObject>"
        "</TimelineObject><LocalExtraInfo><comment_user_list/><like_user_list/>"
        "</LocalExtraInfo></SnsDataItem>"
    )
    try:
        moment = sns.parse_feed_xml(xml_empty_media)
        check(
            "空 media/空评论列表不抛异常",
            moment.create_time == 1700000001 and moment.media_md5s() == [] and moment.comments == [],
        )
    except Exception as exc:  # noqa: BLE001
        check("空 media/空评论列表不抛异常", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    print("=" * 100)
    print("wx4.sns 自测 —— 真实微信 4.x 朋友圈数据（只读）")
    print("=" * 100)

    try:
        moments = sns.all_moments()
    except Wx4Error as exc:
        print(f"\n[FATAL] 微信 4.x 数据不可用：{exc}")
        print("        请确认微信 4.x 已启动登录；或在 db-core 交付 wx4/db.py 前保留 wx_box/recon/ref_db.py。")
        return 2

    print(f"\n数据源: {sns.data_source()}")
    print(f"条目数: {len(moments)}")
    if moments:
        print(
            "时间范围: %s ~ %s" % (fmt_ts(moments[-1].create_time), fmt_ts(moments[0].create_time))
        )

    print_moments(moments)
    print_comments_and_likes(moments)

    total_comments = sum(len(m.comments) for m in moments)
    total_likes = sum(len(m.likes) for m in moments)
    likers = {u for m in moments for u in m.likes}
    commenters = {c.from_user_name for m in moments for c in m.comments}
    total_md5 = sum(len(m.media_md5s()) for m in moments)
    print("\n== 汇总 ==")
    print(f"朋友圈 {len(moments)} 条 | 评论 {total_comments} 条（{len(commenters)} 人） | "
          f"点赞 {total_likes} 个（{len(likers)} 人） | 图片/视频 md5 {total_md5} 个")
    print(f"有评论的 feed: {[m.feed_id for m in moments if m.comments]}")
    print(f"有点赞的 feed: {[m.feed_id for m in moments if m.likes]}")

    print("\n== 断言 ==")
    test_basic(moments)
    test_time_filter(moments)
    test_dedupe(moments)
    test_serialization(moments)
    test_cover()
    test_errors()
    test_missing_fields()

    print("\n" + "=" * 100)
    if _FAILS:
        print(f"结果: FAIL（{len(_FAILS)} 项）")
        for name in _FAILS:
            print("  - " + name)
        return 1
    print("结果: ALL PASS ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
