# -*- coding: utf-8 -*-
"""朋友圈头像导出器（微信 4.x 版）。

原版（3.x）从 ``Misc.db`` 的 ``ContactHeadImg1.smallHeadBuf`` 取小头像 BLOB。
4.x 改为调 :func:`wx4.media.avatar_path`（头像缓存/head_image.db 的明文图片），
统一转成 PNG 存到 ``output/<dir>/avatars/<username>.png``，HTML 引用路径不变；
取不到头像时返回模板自带的 ``icons/empty-avatar.jpg``（并在 output 里生成占位图，
避免出现坏图链接）。
"""
import io
import traceback
from pathlib import Path

from PIL import Image
from log import LOG

_EMPTY_AVATAR_REL = 'icons/empty-avatar.jpg'


class AvatarExporter:
    def __init__(self, dir_name: str):
        self.dir_name = dir_name
        # 头像是否已保存好 Key: userName value: 相对路径
        self._saved_map = {}
        Path(f'output/{self.dir_name}/avatars/').mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def _ensure_empty_avatar(self) -> None:
        """模板里引用了 icons/empty-avatar.jpg，但资源包里没有，导出时补一张灰色占位图。"""
        path = Path(f'output/{self.dir_name}/{_EMPTY_AVATAR_REL}')
        if path.exists():
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new('RGB', (100, 100), (216, 216, 216)).save(path, 'JPEG')
        except Exception:
            LOG.warning("生成空头像占位图失败：\n%s", traceback.format_exc())

    def _save_avatar(self, user_name: str) -> str:
        """从 4.x 头像缓存解出一张图，转 PNG 存盘；失败返回空头像占位。"""
        try:
            import wx4.media

            avatar_path = wx4.media.avatar_path(user_name)
        except Exception:
            LOG.warning("调用 wx4.media.avatar_path(%s) 失败：\n%s", user_name,
                        traceback.format_exc())
            avatar_path = None

        if avatar_path:
            try:
                image = Image.open(io.BytesIO(Path(avatar_path).read_bytes()))
                image = image.convert("RGBA") if image.mode not in ("RGB", "RGBA") else image
                image.save(f'output/{self.dir_name}/avatars/{user_name}.png', 'PNG')
                return f'avatars/{user_name}.png'
            except Exception:
                LOG.warning("保存头像失败 user=%s path=%s：\n%s", user_name, avatar_path,
                            traceback.format_exc())

        self._ensure_empty_avatar()
        return _EMPTY_AVATAR_REL

    # ------------------------------------------------------------------
    def get_avatar_path(self, userName) -> str:
        user_name = str(userName or "")
        if not user_name:
            self._ensure_empty_avatar()
            return _EMPTY_AVATAR_REL
        if user_name in self._saved_map:
            return self._saved_map[user_name]
        rel = self._save_avatar(user_name)
        self._saved_map[user_name] = rel
        return rel
