# -*- coding: utf-8 -*-
"""main.py 重试/防崩自测（不需要真实微信、不弹 Tk 窗口）。

复现原版 bug 场景：``get_db()`` 快速失败时，旧版会直接 ``gui.waiting_label.config()``
抛 AttributeError。这里用假 Gui + 假 get_db 验证新版：

1. 前 3 次失败（抛 Wx4Error）后第 4 次成功：main() 不抛异常，状态文案始终是
   「请启动并登录微信 4.x」，成功后把账号信息交给 GUI；
2. GUI 已关闭时 main() 直接退出，不做无用轮询。

用法::

    .venv\\Scripts\\python.exe _dev\\test_main_retry.py
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class FakeGui:
    def __init__(self):
        self._closed = threading.Event()
        self.status = []
        self.logged = []
        self.ui_calls = 0

    @property
    def closed(self):
        return self._closed.is_set()

    def close(self):
        self._closed.set()

    def run_gui(self):
        pass

    def wait_ready(self, timeout=None):
        return True

    def call_in_ui(self, callback):
        self.ui_calls += 1
        callback()

    def set_status(self, text):
        self.status.append(text)

    def wechat_logged_in(self, account_info):
        self.logged.append(account_info)


class FakeDB:
    wxid = "wxid_fake"
    account = "wxid_fake_1234"
    account_dir = Path("C:/fake/account_dir")

    def self_info(self):
        return {"username": "wxid_fake", "nick_name": "测试昵称", "remark": ""}


def main() -> int:
    import main as main_mod
    import gui.gui as gui_mod
    import wx4.db as wx4db
    from wx4 import Wx4Error

    main_mod.sleep = lambda _seconds: None  # 测试不真的等

    # ---------- case 1：先失败后成功，不崩 ----------
    gui = FakeGui()
    calls = {"n": 0}

    def fake_get_db():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise Wx4Error("请启动并登录微信 4.x")
        return FakeDB()

    gui_mod.Gui = lambda: gui
    wx4db.get_db = fake_get_db
    wx4db.reset = lambda: None

    main_mod.main()

    assert calls["n"] == 4, f"应重试 4 次，实际 {calls['n']} 次"
    assert gui.status, "失败期间应有状态提示"
    assert all(s == main_mod.WAITING_TEXT for s in gui.status), gui.status
    assert gui.logged, "就绪后应回调 wechat_logged_in"
    info = gui.logged[0]
    assert info["wxid"] == "wxid_fake", info
    assert info["filePath"] == str(FakeDB.account_dir), info
    assert info["nick_name"] == "测试昵称", info
    print(f"[case1] OK：失败 3 次后成功，状态文案={gui.status[0]!r}，登录信息={info}")

    # ---------- case 2：GUI 关闭后退出 ----------
    gui2 = FakeGui()
    gui2.close()
    wx4db.get_db = lambda: (_ for _ in ()).throw(AssertionError("GUI 关闭后不应再连库"))
    gui_mod.Gui = lambda: gui2

    main_mod.main()
    assert not gui2.logged, "GUI 关闭后不应再登录"
    print("[case2] OK：GUI 关闭后 main() 正常退出，不再轮询")

    print("test_main_retry: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
