"""wx_box 网页端（本地 HTTP/SSE 服务 + 单页界面）。

模块划分：
* ``webui.bridge``  —— 消费 ``wx_core`` 后端事件、维护消息快照、SSE 订阅广播；
* ``webui.llm``     —— DeepSeek 模型配置与流式分析服务；
* ``webui.server``  —— 标准库 ``http.server`` 实现的 HTTP/API/SSE/静态资源服务。

入口见项目根目录 ``web_app.py``。接口契约见 ``webui/CONTRACT.md``。
"""
from __future__ import annotations

__version__ = "1.0.0"
