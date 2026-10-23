# -*- coding: utf-8 -*-
"""「准备数据」步骤（微信 4.x 版）。

原版（3.x）这一步是用 pywxdump 把 ``MicroMsg.db / MSG*.db / Sns.db ...`` 解密并
合并到 ``app/DataBase/Msg/``，所以必须让用户先关闭微信。

4.x 改为**在线读取**：密钥通过只读内存提取、数据库由 :mod:`wx4.db` 在需要时解密到
``WechatMoments/.wx4_cache/`` 缓存，因此不需要关闭微信。这里做的是「准备数据层」：

1. 获取 :func:`wx4.db.get_db` 单例（定位账号目录、取密钥、校验可解密）；
2. 读本人资料（昵称，用于导出页/HTML 头部）；
3. 预热 ``sns.db``（朋友圈条数）与 ``contact.db``（联系人数）连接；
4. 预热 ``wx4.media``（图片/视频/头像缓存的密钥，失败不作为致命错误）。

对外仍保持 ``DatabaseDecrypter(gui, db_path, key).decrypt()``，但 ``decrypt()``
返回 ``bool`` 表示是否准备成功（GUI 据此决定是否进入导出页 / 允许重试）。
"""
from __future__ import annotations

import traceback

from log import LOG


class DatabaseDecrypter:

    def __init__(self, gui: 'Gui', db_path=None, key=None):
        # 兼容原版签名：4.x 下 db_path 即账号数据目录（也可以不给，wx4 自己会定位），
        # key 忽略（密钥由 wx4 自己提取/缓存）。
        self.db_path = db_path
        self.key = key
        self.gui = gui

    # ------------------------------------------------------------------
    # GUI 回调（旧版 GUI 里这些方法一定存在，这里全部做保护）
    # ------------------------------------------------------------------
    def _set_note(self, text: str) -> None:
        try:
            if self.gui and self.gui.decrypt_note_text is not None:
                self.gui.decrypt_note_text.set(text)
        except Exception:
            pass

    def _set_progress(self, progress: int) -> None:
        try:
            if self.gui:
                self.gui.update_decrypt_progressbar(progress)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def decrypt(self) -> bool:
        """准备 4.x 数据层；成功返回 True，失败返回 False（GUI 可重试）。"""
        self._set_progress(0)
        self._set_note("正在连接微信 4.x 数据层....")

        try:
            from wx4 import Wx4Error
            from wx4.db import get_db
        except Exception:
            LOG.error("wx4 数据层不可用：\n%s", traceback.format_exc())
            self._set_note("wx4 数据层加载失败，请检查程序文件是否完整")
            self._set_progress(0)
            return False

        try:
            db = get_db()
            self._set_progress(30)

            info = db.self_info()
            nick = info.get("nick_name") or info.get("username") or db.wxid
            self._set_note(f"已连接微信：{nick}，正在读取联系人....")
            self._set_progress(45)

            contact_count = len(db.contacts())
            self._set_progress(65)

            self._set_note("正在读取朋友圈数据....")
            from wx4 import sns as wx4_sns

            moments = wx4_sns.all_moments()
            self._set_progress(85)

            # 预热媒体层（图片密钥/缓存目录）；失败不影响导出（图片会跳过）
            self._set_note("正在准备图片/视频缓存....")
            try:
                import wx4.media  # noqa: F401
            except Exception:
                LOG.warning("wx4.media 暂不可用，图片/头像/视频可能无法导出：\n%s",
                            traceback.format_exc())

            self._set_progress(100)
            self._set_note(
                f"数据准备完成：{nick}，联系人 {contact_count} 个，"
                f"可见朋友圈 {len(moments)} 条，请点击下一步"
            )
            LOG.info("4.x 数据层准备完成：account=%s contacts=%d moments=%d",
                     db.account, contact_count, len(moments))
            return True

        except Wx4Error as exc:
            LOG.warning("准备 4.x 数据层失败：%s", exc)
            self._set_note(f"准备失败：{exc}")
            self._set_progress(0)
            return False
        except Exception as exc:  # noqa: BLE001 - 不把底层异常抛给 GUI
            LOG.error("准备 4.x 数据层异常：\n%s", traceback.format_exc())
            self._set_note(f"准备失败：{exc}")
            self._set_progress(0)
            return False
