# -*- coding: utf-8 -*-
"""朋友圈视频导出器（微信 4.x 版）。

原版（3.x）从预解密好的 ``output/<dir>/videos/{md5}_{duration}.mp4`` 里按 md5 / 时长
匹配；4.x 改为直接从朋友圈 XML 的 ``media.url.md5``（type==6）调用
:func:`wx4.media.video_path` 找 4.x 本地缓存，复制到
``output/<dir>/videos/{md5}_{duration}.mp4``（目录/文件名规则与 HTML 引用保持不变）。
GUI 勾选「视频转码」时用 resource/ffmpeg.exe 转 H264，失败则回退原始文件。
"""
import shutil
import traceback
from pathlib import Path

from log import LOG


def _media_md5(media_item) -> str:
    url = getattr(media_item, "url", None)
    md5 = getattr(url, "md5", "") if url is not None else ""
    return (md5 or "").strip() if isinstance(md5, str) else ""


class VideoExporter:
    def __init__(self, dir_name):
        self.dir_name = dir_name
        Path(f'output/{self.dir_name}/videos/').mkdir(parents=True, exist_ok=True)
        # key -> 相对路径 / None
        self._saved: dict = {}

    def _save_video(self, media_item) -> str | None:
        md5 = _media_md5(media_item)
        try:
            duration = round(float(getattr(media_item, "videoDuration", 0) or 0), 2)
        except (TypeError, ValueError):
            duration = 0.0
        key = md5 or str(getattr(media_item, "id", "") or getattr(media_item, "id", ""))
        if not key:
            return None
        if key in self._saved:
            return self._saved[key]

        src = None
        try:
            import wx4.media

            src = wx4.media.video_path(media_item)
        except Exception:
            LOG.warning("调用 wx4.media.video_path 失败（md5=%s）：\n%s", md5,
                        traceback.format_exc())

        if not src or not Path(src).is_file():
            LOG.warning("朋友圈视频没有本地缓存 md5=%s", md5)
            self._saved[key] = None
            return None

        name = f"{key}_{duration}.mp4"
        target = Path(f'output/{self.dir_name}/videos/{name}')
        target.parent.mkdir(parents=True, exist_ok=True)

        # GUI 勾选「视频转码」时转 H264，失败回退复制原文件
        try:
            from decrypter.video_decrypt import is_convert_video, VideoDecrypter

            if is_convert_video() and VideoDecrypter.convert_to_h264(str(src), str(target)):
                rel = f'videos/{name}'
                self._saved[key] = rel
                return rel
        except Exception:
            LOG.warning("视频转码流程异常，回退复制原文件：\n%s",
                        traceback.format_exc())

        try:
            shutil.copyfile(src, target)
        except OSError:
            LOG.warning("复制视频失败 src=%s", src)
            self._saved[key] = None
            return None

        rel = f'videos/{name}'
        self._saved[key] = rel
        return rel

    def get_videos(self, msg) -> list[str]:
        """获取一条朋友圈的全部视频，返回 HTML 用相对路径列表。"""
        results: list[str] = []
        try:
            content = msg.timelineObject.ContentObject
        except AttributeError:
            return results
        if not content or not content.mediaList or not content.mediaList.media:
            return results

        for media_item in content.mediaList.media:
            if str(getattr(media_item, "type", "") or "") != "6":
                continue
            rel = self._save_video(media_item)
            if rel and rel not in results:
                results.append(rel)
        return results
