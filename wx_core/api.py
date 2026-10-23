"""后端抽象协议（GUI 只依赖本文件，Lead 维护）。"""
from __future__ import annotations

import queue
from typing import Protocol

from .models import Friend


class Backend(Protocol):
    """GUI 与采集核心之间的唯一接口。所有实现都必须线程安全。"""

    events: "queue.Queue[dict]"

    def start(self) -> None:
        """启动后台采集轮询线程（立即返回，不阻塞 GUI）。"""

    def stop(self) -> None:
        """停止采集线程并释放资源。"""

    def get_state(self) -> dict:
        """返回当前状态快照（含 running/wechat_found/chat_title/monitoring/error 等）。"""

    def list_friends(self, refresh: bool = False) -> list[Friend]:
        """返回好友列表；refresh=True 时触发/等待重新枚举。"""

    def set_monitored(self, names: list[str]) -> None:
        """设置监听好友（空列表 = 停止监听，但后端线程仍可运行）。"""

    def is_running(self) -> bool:
        ...
