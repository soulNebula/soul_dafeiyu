# -*- coding: utf-8 -*-
"""pytest 全局隔离：任何测试都不许写/读真实数据目录。

背景（2026-10-24）：
- `agi.wechat_gateway.config.contact_db("好友A")` 的 sha1 前 14 位是 043e7e3743523b，
  刚好和真实数据目录里的一个联系人库同名；
- `test_proactive_worker.py` 里部分用例直接调 `run_cycle()`，没有替换 contact_db，
  于是把测试数据写进了 `agi/data/selflearn/wechat_contacts/`。
- 这里用 autouse fixture 把 contact_db 重定向到 tmp_path，从根上堵住。

背景（2026-10-25，P0-22b）：
- 运维在机上一个 `touch agi/data/wechat/STOP`（紧急停止开关）就让
  `test_wechat_draft_flow.py` 4 条全挂——测试里的 Gateway 读的是**生产 STOP 路径**，
  被真实文件拦成 `blocked`。生产文件的存在不应该影响任何测试。
- 这里再用一个 autouse fixture 把 STOP 路径重定向到 `tmp_path/"STOP"`；
  如果测试自己把 `CONFIG_PATH` 指到 tmp（如 `test_config_hot_reload` 的 `cfg_path`），
  STOP 仍跟着 `config.json` 同目录走，保持"开关与配置同目录"的语义。
  生产数据目录（`config.DATA_DIR`）下的 STOP 路径永远不会被读到。

注意：生产代码里的 `db.connect(path=DB_PATH)` 默认参数在 import 时已绑定，
本 fixture 只负责 `contact_db` / `stop_path` 这些运行时计算路径的入口；
测试仍应显式传 tmp_path。
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_pacing(monkeypatch):
    """P16-B：自适应节奏默认只在专项测试里开启。

    pacing 会改写主动消息的概率与间隔（按用户/agent 消息比例自适应），
    若在普通测试里默认生效会改变既有断言（skip reason、是否发送等）。
    这里统一关闭，`test_pacing.py` 等专项测试自行 `monkeypatch.setenv("PACING_ENABLED", "1")`。
    """
    monkeypatch.setenv("PACING_ENABLED", "0")
    yield


@pytest.fixture(autouse=True)
def _isolate_web_lookup(monkeypatch):
    """P24：联网实时信息默认在测试里关闭，避免任何测试真的发网络请求。

    专项测试（`test_web_lookup.py`）自己把 `WEB_LOOKUP_ENABLED` 设回 "1"。
    """
    monkeypatch.setenv("WEB_LOOKUP_ENABLED", "0")
    yield


@pytest.fixture(autouse=True)
def _isolate_contact_db(tmp_path, monkeypatch):
    """把联系人库路径重定向到临时目录，避免测试污染真实 agi/data。"""
    contacts = tmp_path / "_contacts"

    def _fake_contact_db(contact: str):
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(contact))
        return contacts / f"{safe}.sqlite3"

    monkeypatch.setattr("agi.wechat_gateway.config.contact_db", _fake_contact_db)
    yield


@pytest.fixture(autouse=True)
def _isolate_stop_path(tmp_path, monkeypatch):
    """P0-22b：把 STOP 路径重定向到 tmp，保证测试永远读不到生产 STOP 文件。

    - 常规测试：``config.stop_path()`` → ``tmp_path/"STOP"``；
    - 测试自己把 ``CONFIG_PATH`` 指到 tmp 时：STOP 仍为 ``CONFIG_PATH`` 同目录
      （保持"开关与配置同目录"语义，test_config_hot_reload 依赖这一点）；
    - 显式 ``config.STOP_PATH``（Path/str/callable）优先，可让单个测试自造开关；
    - 解析结果落在生产 ``config.DATA_DIR`` 下一律改写为 tmp 路径。

    另外把 ``STOP_PATH`` 复位为 None，避免上一个测试的覆盖泄漏到下一个测试。
    """
    from agi.wechat_gateway import config as gw_config

    isolated = tmp_path / "STOP"

    def _isolated_stop_path() -> Path:
        override = gw_config.STOP_PATH
        if override is not None:
            candidate = Path(override() if callable(override) else override)
        else:
            # 不含 override 的默认：与当前 CONFIG_PATH 同目录
            candidate = gw_config.default_stop_path()
        try:
            production = Path(gw_config.DATA_DIR).resolve()
            candidate.resolve().relative_to(production)
        except (ValueError, OSError):
            # 不在生产数据目录下（测试自己指到 tmp / 显式注入）→ 原样使用
            return candidate
        # 生产路径 → 重定向到 tmp，测试永远看不到真实 STOP
        return isolated

    monkeypatch.setattr(gw_config, "STOP_PATH", None)
    monkeypatch.setattr(gw_config, "stop_path", _isolated_stop_path)
    yield
