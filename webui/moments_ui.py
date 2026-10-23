"""自动刷新微信电脑版朋友圈（可选，会短暂操作微信窗口）。

微信 4.x 只有客户端主动拉取/刷新朋友圈时才会把新动态写进本地 ``sns.db``。
本模块通过窗口级点击模拟用户「发现 → 朋友圈 → 刷新 → 返回聊天」，让微信自己拉取
最新朋友圈；只点击页面/刷新按钮，不点赞、不评论、不发送任何消息。

注意：这是 UI 自动化，会短暂把微信切到前台；窗口尺寸/版本变化可能导致点位失效。
默认只在真实模式、自动监听开启且微信不在前台时执行，失败只记日志。
"""
from __future__ import annotations

import time
from typing import Callable

from wx_core import win as W


def refresh_moments_window(log: Callable[[str], None] | None = None) -> dict:
    """点击微信「发现 → 朋友圈 → 刷新」，返回 ``{"ok": bool, "reason": str}``。"""
    log = log or (lambda *_a, **_k: None)
    win = W.find_wechat_window()
    if win is None:
        return {"ok": False, "reason": "未找到微信窗口"}
    if win.minimized:
        return {"ok": False, "reason": "微信窗口最小化，跳过自动刷新"}
    # 用户正在用微信时不抢焦点，避免打断输入/点击
    if W.is_foreground(win):
        return {"ok": False, "reason": "微信在前台，跳过自动刷新"}

    prev = W.user32.GetForegroundWindow()
    width = max(win.width, 1)
    height = max(win.height, 1)
    try:
        if not W.bring_to_front(win):
            return {"ok": False, "reason": "无法激活微信窗口"}
        time.sleep(0.35)
        # 发现页入口（左侧导航栏的绿色图标）
        W.real_click(win, int(width * 0.043), int(height * 0.424))
        time.sleep(0.45)
        # 发现 → 朋友圈 行
        W.real_click(win, int(width * 0.145), int(height * 0.119))
        time.sleep(1.4)
        # 朋友圈右上角刷新按钮
        W.real_click(win, int(width * 0.756), int(height * 0.064))
        time.sleep(2.0)
        # 返回聊天页，尽量恢复用户原来的界面
        W.real_click(win, int(width * 0.043), int(height * 0.173))
        time.sleep(0.3)
        log("已自动刷新微信朋友圈")
        return {"ok": True, "reason": ""}
    except Exception as exc:  # noqa: BLE001 - UI 刷新失败不能影响监听
        return {"ok": False, "reason": f"自动刷新朋友圈失败：{type(exc).__name__}: {exc}"}
    finally:
        try:
            if prev and prev != win.hwnd:
                W.user32.SetForegroundWindow(prev)
        except Exception:  # noqa: BLE001
            pass
