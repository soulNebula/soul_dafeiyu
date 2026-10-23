# -*- coding: utf-8 -*-
"""端到端验收（无 GUI）：把微信 4.x 的朋友圈导出成 ``output/朋友圈_*/index.html``。

用法（任意目录下执行都行）：:

    C:\\...\\WechatMoments\\.venv\\Scripts\\python.exe _dev\\e2e_export.py

流程：
1. 用 ``wx4.sns.all_moments()`` 取全部可见朋友圈（当前实测 7 条）；
2. 用 ``app/DataBase`` 适配层构造与 GUI 相同的联系人 map；
3. 用 ``exporter.html_exporter.HtmlExporter``（真实导出线程的 ``run()``）导出；
4. 断言：
   - ``index.html`` 存在；
   - HTML 里引用的本地文件（图片/缩略图/头像/封面/视频/静态资源）都存在，
     图片类能被 PIL 打开且尺寸 > 0；
   - 本人头像 ``avatars/<my_wxid>.png`` 存在；
   - HTML 内朋友圈条目数 == 时间区间内 ``wx4.sns`` 的条数；
   - 每条朋友圈的评论数/点赞数与 ``wx4.sns`` 一致（总数 + 逐条）；
5. 打印统计（导出路径、条目数、图片/头像/视频张数、评论/点赞数）。
"""
from __future__ import annotations

import datetime
import logging
import os
import re
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # html_exporter 用相对路径 output/ 和 resource/

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# root logger 是 DEBUG，PIL 解码过程会刷屏，只保留 warning
logging.getLogger("PIL").setLevel(logging.WARNING)

from PIL import Image  # noqa: E402

_REF_RE = re.compile(r'(?:src|full_img)\s*=\s*"([^"]*)"')
_IMG_PREFIXES = ("images/", "thumbs/", "avatars/", "icons/")


class FakeGui:
    """供 HtmlExporter / 各 exporter 使用的最小 GUI 替身。"""

    def __init__(self, account_info: dict):
        self.account_info = account_info
        self.export_dir_name = None
        self.export_progress = []
        self.image_decrypter = None
        self.video_decrypter = None
        self.succeeded = False

    def update_export_progressbar(self, progress):
        self.export_progress.append(progress)

    def update_decrypt_progressbar(self, progress):  # noqa: D401 - 兼容接口
        pass

    def export_succeed(self):
        self.succeeded = True


def _keep_contact(row) -> bool:
    """复刻 GUI 的 validate_contact 过滤逻辑。"""
    username = str(row[0] or "")
    c_type = row[2]
    is_misc_account = c_type in (1, 33, 513)
    return not (
        is_misc_account
        or username.startswith("gh_")
        or username.endswith("@chatroom")
        or username == "filehelper"
    )


def _build_contacts(moments) -> dict:
    from app.DataBase import micro_msg_db
    from entity.contact import Contact

    contact_map = {}
    for row in (micro_msg_db.get_contact() or []):
        if not row or len(row) < 12 or not _keep_contact(row):
            continue
        contact_map[row[0]] = Contact(*row[:12])

    # 兜底：朋友圈作者一定放进来，避免 exporter 因联系人缺失而跳过条目
    for moment in moments:
        if moment.username in contact_map:
            continue
        row = micro_msg_db.get_contact_by_username(moment.username)
        if row and len(row) >= 12:
            contact_map[moment.username] = Contact(*row[:12])
            print(f"[warn] 作者 {moment.username} 不在联系人筛选结果里，已单独补入")
    return contact_map


def _local_refs(html: str) -> list[str]:
    refs = []
    for raw in _REF_RE.findall(html):
        ref = raw.strip()
        if not ref or ref == "None":
            continue
        if ref.startswith(("http://", "https://", "data:", "#", "mailto:")):
            continue
        if ref not in refs:
            refs.append(ref)
    return refs


def _check_refs(out_dir: Path, refs: list[str]) -> dict:
    stats = {"images": 0, "thumbs": 0, "avatars": 0, "icons": 0, "videos": 0,
             "static": 0, "missing": [], "bad_images": []}
    for ref in refs:
        path = out_dir / ref
        if not path.is_file():
            stats["missing"].append(ref)
            continue
        if ref.startswith(_IMG_PREFIXES):
            try:
                with Image.open(path) as img:
                    img.load()
                    if img.size[0] <= 0 or img.size[1] <= 0:
                        raise ValueError(f"尺寸异常 {img.size}")
            except Exception as exc:
                stats["bad_images"].append(f"{ref}: {exc}")
                continue
            if ref.startswith("images/"):
                stats["images"] += 1
            elif ref.startswith("thumbs/"):
                stats["thumbs"] += 1
            elif ref.startswith("avatars/"):
                stats["avatars"] += 1
            else:
                stats["icons"] += 1
        elif ref.startswith("videos/"):
            stats["videos"] += 1
        else:
            stats["static"] += 1
    return stats


def _date_to_bounds(begin_date, end_date):
    begin_time = time.mktime(datetime.datetime(begin_date.year, begin_date.month,
                                               begin_date.day).timetuple())
    next_day = end_date + datetime.timedelta(days=1)
    end_time = time.mktime(datetime.datetime(next_day.year, next_day.month,
                                             next_day.day).timetuple())
    return begin_time, end_time


def main() -> int:
    failures = []

    def check(condition: bool, message: str):
        if condition:
            print(f"  [ok] {message}")
        else:
            failures.append(message)
            print(f"  [FAIL] {message}")

    print("=" * 78)
    print("WechatMoments 4.x 端到端导出验收")
    print("=" * 78)

    from wx4 import sns as wx4_sns
    from wx4.db import get_db
    from app.DataBase import sns_db, micro_msg_db
    from decrypter.image_decrypt import ImageDecrypter
    from decrypter.video_decrypt import VideoDecrypter
    from exporter.html_exporter import HtmlExporter

    db = get_db()
    moments = wx4_sns.all_moments()
    print(f"账号: {db.account} ({db.wxid})  数据源: {wx4_sns.data_source()}")
    print(f"wx4.sns 可见朋友圈: {len(moments)} 条")
    check(len(moments) > 0, f"wx4.sns 至少解析出 1 条朋友圈（实际 {len(moments)}）")
    if not moments:
        return 1

    # 时间范围：覆盖全部可见朋友圈（与 GUI 选日期等价）
    times = [m.create_time for m in moments]
    begin_date = datetime.date.fromtimestamp(min(times))
    end_date = datetime.date.fromtimestamp(max(times))
    begin_ts, end_ts = _date_to_bounds(begin_date, end_date)
    print(f"导出时间范围: {begin_date} ~ {end_date}")

    contact_map = _build_contacts(moments)
    account_info = {
        "wxid": db.wxid,
        "nick_name": db.self_info().get("nick_name", ""),
        "filePath": str(db.account_dir),
        "key": "",
    }
    gui = FakeGui(account_info)
    gui.image_decrypter = ImageDecrypter(gui, str(db.account_dir))
    gui.video_decrypter = VideoDecrypter(gui, str(db.account_dir))

    dir_name = "朋友圈_" + datetime.datetime.now().strftime("%Y_%m_%d_%H%M%S")
    gui.export_dir_name = dir_name
    out_dir = ROOT / "output" / dir_name
    print(f"导出目录: output/{dir_name}")

    exporter = HtmlExporter(gui, dir_name, contact_map, begin_date, end_date, 0)
    exporter.run()  # 真实导出流程（同步跑线程的 run）

    html_path = out_dir / "index.html"
    check(html_path.is_file(), f"index.html 存在（{html_path}）")
    html = html_path.read_text(encoding="utf-8") if html_path.is_file() else ""
    check(bool(html.strip()), "index.html 非空")

    # ---------- 引用文件检查 ----------
    refs = _local_refs(html)
    stats = _check_refs(out_dir, refs)
    check(not stats["missing"], f"HTML 引用的本地文件都存在（缺失 {len(stats['missing'])}）")
    if stats["missing"]:
        for ref in stats["missing"][:10]:
            print(f"        缺少: {ref}")
    check(not stats["bad_images"], f"引用的图片都能被 PIL 打开（失败 {len(stats['bad_images'])}）")
    for bad in stats["bad_images"][:10]:
        print(f"        坏图: {bad}")

    my_avatar = out_dir / "avatars" / f"{db.wxid}.png"
    check(my_avatar.is_file(), f"本人头像存在（avatars/{db.wxid}.png）")
    if my_avatar.is_file():
        try:
            with Image.open(my_avatar) as img:
                img.load()
                check(img.size[0] > 0, f"本人头像可打开（{img.size[0]}x{img.size[1]}）")
        except Exception as exc:
            check(False, f"本人头像可打开（异常：{exc}）")

    avatar_files = sorted((out_dir / "avatars").glob("*.png"))
    check(len(avatar_files) >= 1, f"至少导出 1 个头像（实际 {len(avatar_files)}）")

    # ---------- 条目 / 评论 / 点赞统计 ----------
    expected_moments = [
        m for m in moments
        if m.username in contact_map and begin_ts <= m.create_time <= end_ts
    ]
    item_count = html.count('class="row item"')
    print(f"HTML 条目数: {item_count}  期望: {len(expected_moments)} "
          f"（wx4.sns 全部 {len(moments)} 条）")
    check(item_count == len(expected_moments),
          f"HTML 条目数 == 时间区间内 wx4.sns 条数（{item_count} vs {len(expected_moments)}）")
    check(item_count == len(moments),
          f"全部可见朋友圈都被导出（{item_count} vs {len(moments)}）")

    # 评论/点赞：适配层 vs wx4.sns 逐条比对
    comments_mismatch = []
    likes_mismatch = []
    total_comments = 0
    total_likes = 0
    for moment in expected_moments:
        expected_comments = [
            (c.from_user_name, c.comment_type, c.content) for c in moment.comments
        ]
        actual_comments = sns_db.get_comment_by_feed_id(moment.feed_id) or []
        if actual_comments != expected_comments:
            comments_mismatch.append((moment.feed_id, len(actual_comments),
                                      len(expected_comments)))
        actual_likes = sns_db.get_likes_by_feed_id(moment.feed_id) or []
        if actual_likes != list(moment.likes):
            likes_mismatch.append((moment.feed_id, len(actual_likes), len(moment.likes)))
        total_comments += len(expected_comments)
        total_likes += len(moment.likes)

    check(not comments_mismatch,
          f"每条朋友圈评论数与 wx4.sns 一致（不一致 {len(comments_mismatch)} 条）")
    for feed_id, got, want in comments_mismatch:
        print(f"        feed_id={feed_id} 适配层 {got} 条 / wx4.sns {want} 条")
    check(not likes_mismatch,
          f"每条朋友圈点赞数与 wx4.sns 一致（不一致 {len(likes_mismatch)} 条）")
    for feed_id, got, want in likes_mismatch:
        print(f"        feed_id={feed_id} 适配层 {got} 个 / wx4.sns {want} 个")

    # 图片覆盖率（media 层能力，报告用）
    # 朋友圈图片落盘文件名为 <md5>.<ext>，封面/视频号封面是 uuid 文件名，分开统计
    feed_image_files = [
        p for p in (out_dir / "images").glob("*")
        if re.fullmatch(r"[0-9a-f]{32}\.[A-Za-z0-9]+", p.name)
    ]
    expected_images = sum(
        1 for m in expected_moments
        for media in ((m.moment.timelineObject.ContentObject.mediaList.media
                       if m.moment.timelineObject.ContentObject.mediaList else []))
        if str(getattr(media, "type", "")) == "2"
    )
    print(f"图片: 缓存命中落盘 {len(feed_image_files)} 张 / XML 期望 {expected_images} 张"
          f"（缩略图引用 {stats['thumbs']}，头像 {stats['avatars']}，视频 {stats['videos']}，"
          f"静态资源 {stats['static']}，封面/其它 {stats['images'] - len(feed_image_files)}）")
    if expected_images:
        check(len(feed_image_files) > 0,
              f"至少导出 1 张朋友圈图片（实际 {len(feed_image_files)} 张）")

    # ---------- 汇总 ----------
    print("-" * 78)
    print("统计汇总:")
    print(f"  HTML           : {html_path}")
    print(f"  条目           : {item_count} / {len(moments)}")
    print(f"  评论 / 点赞    : {total_comments} / {total_likes}")
    print(f"  图片 / 缩略图  : {stats['images']} / {stats['thumbs']}")
    print(f"  头像           : {stats['avatars']}")
    print(f"  视频           : {stats['videos']}")
    print(f"  导出进度回调   : {len(gui.export_progress)} 次，最终 {gui.export_progress[-1] if gui.export_progress else '无'}")
    print(f"  export_succeed : {gui.succeeded}")
    print(f"  输出目录大小   : {sum(p.stat().st_size for p in out_dir.rglob('*') if p.is_file()) // 1024} KB")
    print("-" * 78)

    if failures:
        print(f"E2E 结果: FAILED（{len(failures)} 项）")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("E2E 结果: PASSED ✅")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        print("E2E 结果: ERROR ❌")
        sys.exit(2)
