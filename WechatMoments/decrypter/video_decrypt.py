# -*- coding: utf-8 -*-
"""朋友圈视频「解密」步骤（微信 4.x 版）。

原版（3.x）扫描 ``<微信目录>/FileStorage/Sns/Cache/<月>``，用 filetype 识别 mp4，
按 ``{md5}_{duration}.mp4`` 复制到 ``output/<dir>/videos/``，并可选 ffmpeg 转码。

4.x 改为**按需复制/转码**：``exporter/video_exporter.py`` 从朋友圈 XML 的
``media.url.md5``（type==6）调 :func:`wx4.media.video_path` 找到 4.x 本地缓存，
复制到 ``output/<dir>/videos/{md5}_{duration}.mp4``（HTML 引用相对路径不变）。

本步骤只保存「是否转码」开关并推进 GUI 进度到 30%（视频在后面逐条处理）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback

import log

# 由 decrypt_videos 写入，VideoExporter 读取（GUI 的「视频转码」勾选项）
_CONVERT_VIDEO = False


def is_convert_video() -> bool:
    return _CONVERT_VIDEO


class VideoDecrypter:

    def __init__(self, gui: 'Gui', file_path=None):
        # file_path 兼容旧签名；4.x 不使用
        self.file_path = file_path
        self.gui = gui

    @staticmethod
    def get_ffmpeg_path():
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            resource_dir = getattr(sys, '_MEIPASS', os.path.abspath(os.path.dirname(__file__)))
            return os.path.join(os.path.dirname(resource_dir), 'resource', 'ffmpeg.exe')
        return os.path.join(os.getcwd(), 'resource', 'ffmpeg.exe')

    @staticmethod
    def get_ffprobe_path():
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            resource_dir = getattr(sys, '_MEIPASS')
            return os.path.join(os.path.dirname(resource_dir), 'resource', 'ffprobe.exe')
        return os.path.join(os.getcwd(), 'resource', 'ffprobe.exe')

    @staticmethod
    def get_output_path(dir_name, md5, duration):
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            resource_dir = getattr(sys, '_MEIPASS')
            return os.path.join(os.path.dirname(resource_dir), 'output', dir_name, 'videos',
                                f'{md5}_{duration}.mp4')
        return os.path.join(os.getcwd(), 'output', dir_name, 'videos', f'{md5}_{duration}.mp4')

    def get_video_duration(self, video_path) -> float:
        """用 ffprobe 取时长（秒）；失败返回 0。"""
        ffprobe_path = self.get_ffprobe_path()
        if not os.path.exists(ffprobe_path):
            log.LOG.error("Wrong ffprobe path:" + ffprobe_path)
            return 0
        ffprobe_cmd = (f'"{ffprobe_path}"  -i "{video_path}" -show_entries format=duration '
                       f'-v quiet -of csv="p=0"')
        try:
            p = subprocess.Popen(ffprobe_cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, shell=True)
            out, err = p.communicate()
            if not out:
                return 0
            return float(out.decode('utf-8', 'ignore').strip() or 0)
        except Exception:
            log.LOG.error("获取视频时长失败：\n%s", traceback.format_exc())
            return 0

    @staticmethod
    def convert_to_h264(input_path: str, output_path: str) -> bool:
        """用 resource/ffmpeg.exe 把 H265 转成 H264；失败返回 False。"""
        ffmpeg_path = VideoDecrypter.get_ffmpeg_path()
        if not os.path.exists(ffmpeg_path):
            log.LOG.error("Wrong ffmpeg path:" + ffmpeg_path)
            return False
        cmd = (f'"{ffmpeg_path}" -loglevel quiet -i "{input_path}" '
               f'-c:v libx264 "{output_path}"')
        try:
            subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return os.path.exists(output_path)
        except Exception:
            log.LOG.error("视频转码失败：\n%s", traceback.format_exc())
            return False

    def decrypt_videos(self, exporter, start_date, end_date, dir_name, convert_video) -> None:
        """4.x：视频在 ``VideoExporter.get_videos`` 里按需复制/转码；这里只记开关+进度。"""
        global _CONVERT_VIDEO
        _CONVERT_VIDEO = bool(convert_video)
        try:
            self.gui.update_export_progressbar(30)
        except Exception:
            pass
