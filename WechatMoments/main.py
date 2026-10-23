# -*- coding: utf-8 -*-
"""WechatMoments 入口（微信 4.x 适配版）。

原版 main.py 用 ``pywxdump.read_info`` 轮询登录，有一个必现竞态：``read_info`` 很快
失败时，``gui.waiting_label`` 还是 ``None``（GUI 子线程还没建好控件），主线程直接
``gui.waiting_label.config(...)`` 抛 ``AttributeError``，窗口都来不及弹就闪退。

4.x 版：

- GUI 在 Tk 线程创建完毕后才置 ready 事件，主线程等它；
- 主线程只通过 ``gui.set_status()`` / ``gui.call_in_ui()`` 投递回调，由 Tk 主线程执行；
- 用 ``wx4.db.get_db() + self_info()`` 判断微信 4.x 是否就绪，未就绪时稳定显示
  「请启动并登录微信 4.x」并持续重试，**不崩不退出**；
- 用户关闭窗口（``gui.closed``）时才退出重试循环。
"""
import threading
import traceback
from time import sleep

from log import LOG

WAITING_TEXT = "请启动并登录微信 4.x"
RETRY_INTERVAL = 1.5


def _account_info(db) -> dict:
    """把 wx4.db 的账号信息转成 GUI/exporter 认识的 dict（兼容 3.x pywxdump 字段名）。"""
    try:
        info = db.self_info()
    except Exception:
        LOG.warning("读取本人资料失败：\n%s", traceback.format_exc())
        info = {}
    return {
        "wxid": info.get("username") or db.wxid,
        "nick_name": info.get("nick_name", ""),
        "remark": info.get("remark", ""),
        "filePath": str(db.account_dir),
        "account": db.account,
        # 4.x 密钥由 wx4 自己提取/缓存，保留 key 字段只是为了兼容原 GUI 代码
        "key": "",
    }


def _wait_wechat(gui):
    """阻塞直到微信 4.x 数据层就绪，返回登录信息 dict；GUI 被关闭时返回 None。"""
    try:
        from wx4 import Wx4Error
        from wx4.db import get_db, reset
    except Exception:
        LOG.error("wx4 数据层加载失败：\n%s", traceback.format_exc())
        gui.set_status("wx4 数据层加载失败，请检查程序文件是否完整")
        return None

    while not gui.closed:
        try:
            db = get_db()
            return _account_info(db)
        except Wx4Error as exc:
            LOG.info("微信尚未就绪：%s", exc)
            gui.set_status(WAITING_TEXT)
            reset()  # 下次重试重新定位账号/取密钥
        except Exception:
            LOG.error("连接微信 4.x 异常：\n%s", traceback.format_exc())
            gui.set_status(WAITING_TEXT)
            try:
                reset()
            except Exception:
                pass
        sleep(RETRY_INTERVAL)
    return None


def main():
    from gui.gui import Gui

    gui = Gui()
    gui_thread = threading.Thread(target=gui.run_gui, daemon=True)
    gui_thread.start()

    # 关键：等 GUI 控件建好，之后跨线程更新才安全（修复原版竞态闪退）
    if not gui.wait_ready(15):
        LOG.error("GUI 启动失败：15 秒内未就绪")
        print("GUI 启动失败，请检查运行环境（Tkinter 是否可用）")
        return

    info = _wait_wechat(gui)
    if info is None:
        LOG.info("GUI 已关闭，退出")
        return

    LOG.info("微信 4.x 已就绪：wxid=%s nick=%s", info.get("wxid"), info.get("nick_name"))
    gui.call_in_ui(lambda: gui.wechat_logged_in(info))

    # 主线程守护 Tk 主循环：GUI 线程是 daemon，主线程一旦返回，进程会立刻退出（窗口闪退）。
    # 必须在这里等用户关窗，完成整个交互流程后才结束。
    while not gui.closed:
        sleep(0.3)
    LOG.info("窗口已关闭，退出")


if __name__ == "__main__":
    main()
