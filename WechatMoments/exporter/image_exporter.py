# -*- coding: utf-8 -*-
"""朋友圈图片导出器（微信 4.x 版）。

原版（3.x）的主体逻辑是**直接 HTTP 下载**腾讯 qpic 图片（URL+idx+token），只有下载
回来不是 JPEG 时才去本地缓存按「文件长度」猜匹配。4.x 上 token 会过期、加密图下载
回来也解不开，所以这里改成**以本地缓存解密为主**：

- 从朋友圈 XML 取 ``media.url.md5``（图片缓存文件名），调
  :func:`wx4.media.image_path` 定位并解密 4.x ``.dat``；
- 解密结果复制到 ``output/<dir>/images/<md5>.<ext>``，HTML 里用相对路径引用
  （目录结构与原版一致，``html_exporter`` 不改）；
- 大图缓存不存在时由 :mod:`wx4.media` 自行退回缩略图；都没有就跳过该图（不丢整条朋友圈）；
- HTTP 下载只保留给「朋友圈封面」这类可选场景，且只接受本地路径，不联网（默认关闭）。

``get_image(link)`` / ``save_image(link, img_type)`` 两个原公开方法保留：
``html_exporter`` 只用到 ``save_image`` 处理封面。
"""
import traceback
import uuid
from pathlib import Path
from typing import Optional, Tuple

from log import LOG

# 图片文件头 -> 扩展名（4.x .dat 解密后是原图，wx4.media 已保证是图片明文）
_MAGIC_EXT = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)


def sniff_image_ext(data: bytes) -> str:
    for magic, ext in _MAGIC_EXT:
        if data.startswith(magic):
            return ext
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def _media_md5(media_item) -> str:
    """取 ``media.url.md5``（4.x 图片缓存定位用）。"""
    url = getattr(media_item, "url", None)
    md5 = getattr(url, "md5", "") if url is not None else ""
    return (md5 or "").strip() if isinstance(md5, str) else ""


class ImageExporter:
    def __init__(self, dir_name: str):
        self.dir_name = dir_name
        Path(f'output/{self.dir_name}/thumbs/').mkdir(parents=True, exist_ok=True)
        Path(f'output/{self.dir_name}/images/').mkdir(parents=True, exist_ok=True)
        # key(md5) -> 相对路径 或 None（没找到，避免重复尝试）
        self._saved: dict = {}

    # ------------------------------------------------------------------
    # 原公开 API（html_exporter 兼容）
    # ------------------------------------------------------------------
    @staticmethod
    def get_image(link: tuple) -> Optional[bytes]:
        """原版是 HTTP 下载；4.x 默认不联网，只有 link 指向本地文件时才读。"""
        url = link[0] if isinstance(link, (tuple, list)) else link
        if not url:
            return None
        try:
            path = Path(str(url))
            if path.is_file():
                return path.read_bytes()
        except (OSError, ValueError):
            pass
        return None

    def save_image(self, link: tuple, img_type: str) -> Optional[str]:
        """保存一张图（原版用于朋友圈封面 / 视频号封面）。

        4.x：``link[0]`` 可以是本地文件路径（封面占位图）或远程 URL；
        远程 URL 不再下载，直接返回 None。
        """
        if img_type not in ('image', 'thumb'):
            raise Exception("img_type 参数非法")
        content = self.get_image(link)
        if not content:
            return None
        file_name = f"{uuid.uuid4()}.{sniff_image_ext(content) or 'jpg'}"
        target = Path(f'output/{self.dir_name}/{img_type}s/{file_name}')
        target.write_bytes(content)
        return f'{img_type}s/{file_name}'

    # ------------------------------------------------------------------
    # 4.x：按 md5 从本地缓存解密
    # ------------------------------------------------------------------
    def _save_media_image(self, media_item) -> Optional[str]:
        md5 = _media_md5(media_item)
        if not md5:
            return None
        if md5 in self._saved:
            return self._saved[md5]

        rel = None
        try:
            import wx4.media

            # 优先按 Media 对象定位（可带上 XML <size>，media 层会做尺寸匹配/缩略图兜底）
            if hasattr(wx4.media, "image_path_for_media"):
                path = wx4.media.image_path_for_media(media_item, allow_thumb=True)
            else:
                try:
                    path = wx4.media.image_path(md5, allow_thumb=True)
                except TypeError:  # 旧签名不支持 allow_thumb
                    path = wx4.media.image_path(md5)
        except Exception:
            LOG.warning("调用 wx4.media 定位图片失败 md5=%s：\n%s", md5,
                        traceback.format_exc())
            path = None

        if path:
            try:
                data = Path(path).read_bytes()
                ext = sniff_image_ext(data) or Path(path).suffix.lstrip('.') or 'jpg'
                name = f"{md5}.{ext}"
                target = Path(f'output/{self.dir_name}/images/{name}')
                if not target.exists() or target.stat().st_size != len(data):
                    target.write_bytes(data)
                rel = f'images/{name}'
            except Exception:
                LOG.warning("复制图片失败 md5=%s path=%s", md5,
                            traceback.format_exc())
                rel = None
        else:
            LOG.warning("朋友圈图片没有本地缓存 md5=%s，已跳过该图", md5)

        self._saved[md5] = rel
        return rel

    def get_images(self, msg) -> list[Tuple[str, str]]:
        """获取一条朋友圈的全部图像，返回 ``[(缩略图路径, 原图路径), ...]``。

        4.x 只有一份本地缓存（大图或缩略图），两个路径给同一个文件；
        ``html_exporter`` 的 ``<img src=缩略图 full_img=原图>`` 逻辑照旧可用。
        """
        results: list[Tuple[str, str]] = []
        try:
            content = msg.timelineObject.ContentObject
        except AttributeError:
            return results
        if not content or not content.mediaList or not content.mediaList.media:
            return results

        for media_item in content.mediaList.media:
            mtype = str(getattr(media_item, "type", "") or "")
            # 2=图片；6=视频（由 VideoExporter 处理）；5=音乐、其它=链接，本地无图
            if mtype != "2":
                continue
            rel = self._save_media_image(media_item)
            if rel:
                results.append((rel, rel))
        return results

    def get_finder_images(self, msg) -> Optional[str]:
        """视频号封面（4.x 朋友圈 XML 一般不含 finderFeed；有则尽量返回本地文件）。"""
        try:
            finder = msg.timelineObject.ContentObject.finderFeed
            media = finder.mediaList.media
        except (AttributeError, TypeError):
            return None
        for media_item in media:
            rel = self._save_media_image(media_item)
            if rel:
                return rel
            thumb_url = getattr(media_item, "thumbUrl", None)
            if thumb_url:
                return self.save_image((thumb_url, "", ""), 'thumb')
        return None
