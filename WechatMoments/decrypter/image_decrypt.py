# -*- coding: utf-8 -*-
"""朋友圈图片「解密」步骤（微信 4.x 版）。

原版（3.x）是扫描 ``<微信目录>/FileStorage/Sns/Cache/<月>``，用异或 magic 解密
``.dat`` 后按 ``{主图字节数}_{缩略图字节数}.jpg`` 落盘，供 exporter 用「文件长度」
反查。

4.x 图片改为**按需解密**：``exporter/image_exporter.py`` 从朋友圈 XML 里拿到
``media.url.md5``，直接调 :func:`wx4.media.image_path` 定位并解密缓存图片（V2 .dat
由 wx4 自己处理），因此本步骤不再扫描/预拷贝，只负责推进 GUI 进度。

保留原来的几个纯函数（``get_all_month_between_dates`` / ``calculate_md5`` /
``decode`` / ``guess_image_encoding_magic``）以兼容旧调用方，但已不再使用。
"""
from __future__ import annotations

import hashlib
from datetime import date


class ImageDecrypter:

    def __init__(self, gui: 'Gui', file_path=None):
        # file_path 兼容旧签名（3.x 用账号数据目录拼 FileStorage/Sns/Cache）；4.x 不使用
        self.file_path = file_path
        self.gui = gui

    def decrypt_images(self, exporter, start_date, end_date, dir_name) -> None:
        """4.x：图片在 ``ImageExporter.get_images`` 里按需解密；这里只推进进度到 15%。"""
        try:
            self.gui.update_export_progressbar(15)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 兼容旧版纯函数（不再参与 4.x 导出流程）
    # ------------------------------------------------------------------
    @staticmethod
    def calculate_md5(file_path):
        with open(file_path, "rb") as f:
            file_content = f.read()
        return hashlib.md5(file_content).hexdigest()

    @staticmethod
    def get_all_month_between_dates(start_date, end_date) -> list[str]:
        result = []
        current_date = start_date
        while current_date <= end_date:
            result.append(current_date.strftime("%Y-%m"))
            year = current_date.year + (current_date.month // 12)
            month = current_date.month % 12 + 1
            current_date = date(year, month, 1)
        return result

    @staticmethod
    def decode(magic, buf):
        return bytearray([b ^ magic for b in list(buf)])

    @staticmethod
    def guess_image_encoding_magic(buf):
        header_code, check_code = 0xff, 0xd8
        magic = header_code ^ list(buf)[0] if buf else 0x00
        _, code = ImageDecrypter.decode(magic, buf[:2])
        if check_code == code:
            return magic
        return None
